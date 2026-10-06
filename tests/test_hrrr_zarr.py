"""hrrrzarr reader and backfill, offline: chunk reads and grid lookup are stubbed."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

import numpy as np
import pytest

from microcast.ingest import hrrr_zarr as hz
from microcast.lake import catalog as lake
from microcast.lake.schemas import NWP_POINT_SCHEMA

INIT = datetime(2025, 7, 15, 21, tzinfo=UTC)
CELLS = [hz.Neighbour("home", k, 500, 200 + k, 37.76, -122.43, 1000.0 * k) for k in range(4)]


WINDOW = (490, 510, 195, 210)  # rows, cols around CELLS


@pytest.fixture
def fake_archive(monkeypatch, tmp_path):
    """Every field reads as its lead hour (fcst) or 0 (anl); one cycle can be marked missing.

    ``missing.reads`` counts archive reads, to show the kept window is reused.
    """
    missing = _Archive()
    reads = missing.reads
    shape = (WINDOW[1] - WINDOW[0], WINDOW[3] - WINDOW[2])

    def read(url, with_time):
        reads["n"] += 1
        if any(f"{m:%Y%m%d_%H}z" in url for m in missing):
            return None
        if not with_time:
            return np.zeros((1, *shape), dtype="float32")
        return np.broadcast_to(np.arange(1, 19, dtype="float32")[:, None, None], (18, *shape)).copy()

    monkeypatch.setenv("MICROCAST_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(hz, "window", lambda: WINDOW)
    monkeypatch.setattr(hz, "_read_window", read)
    monkeypatch.setattr(hz, "neighbours", lambda registry, k=4: CELLS)
    monkeypatch.setattr(hz, "point_neighbours", lambda points, k=4: CELLS)
    return missing


class _Archive(set):
    """The missing cycles, plus a count of archive reads."""

    def __init__(self):
        super().__init__()
        self.reads = Counter()


def test_cycles_rotate_the_kept_hour():
    kept = hz.cycles(datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 4, tzinfo=UTC), stride_h=3)
    assert len(kept) == 24  # 72 hours / 3
    # Over 3 days each hour of day appears exactly once, so every lead sees every valid hour.
    assert Counter(t.hour for t in kept) == {h: 1 for h in range(24)}
    assert hz.cycles(INIT, INIT.replace(hour=23), 1) == [INIT, INIT.replace(hour=22)]


def test_fetch_cycle_rows_match_bronze_schema(fake_archive):
    t = hz.fetch_cycle(INIT, CELLS, [0, 1, 6])
    assert t.schema == NWP_POINT_SCHEMA.as_arrow()
    assert t.num_rows == len(hz.VARIABLES) * 3 * len(CELLS)
    df = t.to_pandas()
    # f00 from the analysis, f01..f06 from fcst time index lead-1.
    assert set(df.loc[df.lead_min == 0, "value"]) == {0.0}
    assert set(df.loc[df.lead_min == 360, "value"]) == {6.0}
    assert (df.valid_time - df.init_time).max() == np.timedelta64(6, "h")
    assert set(df.ingest_batch) == {"hrrrzarr/2025-07-15T21"}
    # cloud base and ceiling share a short name; the level keeps them apart (D3).
    assert set(df.loc[df.variable == "gh", "level"]) == {"cloudBase:0", "cloudCeiling:0"}


def test_missing_cycle_raises(fake_archive):
    fake_archive.add(INIT)
    with pytest.raises(hz.CycleMissing):
        hz.fetch_cycle(INIT, CELLS, [0, 1])


def test_backfill_resumes_and_skips_gaps(fake_archive, catalog, monkeypatch):
    monkeypatch.setattr(lake, "get_catalog", lambda: catalog)
    lake.init_lake(catalog)
    start, end = INIT.replace(hour=0), INIT.replace(hour=6)
    fake_archive.add(INIT.replace(hour=3))

    first = hz.backfill(None, start, end, [0, 1], workers=2, commit_every=2, log=lambda _: None)
    assert first == {"skipped": 0, "fetched": 5, "missing": 1, "rows": 5 * len(hz.VARIABLES) * 2 * len(CELLS)}
    assert len(catalog.load_table("bronze.nwp_point").snapshots()) == 3  # 2 + 2 + 1

    fake_archive.clear()
    second = hz.backfill(None, start, end, [0, 1], workers=2, log=lambda _: None)
    assert (second["skipped"], second["fetched"], second["missing"]) == (5, 1, 0)
    assert lake.recorded_batches("bronze.nwp_point", catalog) == {
        hz.batch_id("hrrr", INIT.replace(hour=h)) for h in range(6)
    }


def test_window_is_kept_and_reused(fake_archive):
    hz.fetch_cycle(INIT, CELLS, [0, 1])
    first = fake_archive.reads["n"]
    assert first == 2 * len(hz.VARIABLES) and hz.tile_path(INIT).exists()
    again = hz.fetch_cycle(INIT, CELLS, [0, 6])  # other leads, same cycle: read from disk
    assert fake_archive.reads["n"] == first
    assert set(again.to_pandas().query("lead_min == 360").value) == {6.0}


def test_points_outside_the_window_are_refused(fake_archive):
    far = [hz.Neighbour("far", 0, 900, 900, 38.5, -121.0, 0.0)]
    with pytest.raises(ValueError, match="outside the kept window"):
        hz.fetch_cycle(INIT, far, [1])


def test_points_pass_records_its_own_batches(fake_archive, catalog, monkeypatch):
    monkeypatch.setattr(lake, "get_catalog", lambda: catalog)
    lake.init_lake(catalog)
    start, end = INIT.replace(hour=0), INIT.replace(hour=2)
    hz.backfill(None, start, end, [1], log=lambda _: None)
    reads = fake_archive.reads["n"]
    stats = hz.backfill(None, start, end, [1], points=[], tag="net1", log=lambda _: None)
    assert stats["fetched"] == 2 and fake_archive.reads["n"] == reads  # all from the kept window
    assert hz.batch_id("hrrr", start, "net1") in lake.recorded_batches("bronze.nwp_point", catalog)


def test_read_window_stitches_across_chunks(monkeypatch):
    from numcodecs import Blosc

    monkeypatch.setattr(hz, "window", lambda: (148, 152, 10, 12))  # straddles chunk rows 0 and 1
    blocks = {f"0.{cy}.0": np.full((2, hz.CHUNK, hz.CHUNK), cy + 1, dtype="<f4") for cy in (0, 1)}
    monkeypatch.setattr(hz, "_get", lambda url: Blosc().encode(blocks[url.rsplit("/", 1)[1]].tobytes()))
    out = hz._read_window("x", with_time=True)
    assert out.shape == (2, 4, 2)
    assert (out[:, :2] == 1).all() and (out[:, 2:] == 2).all()
