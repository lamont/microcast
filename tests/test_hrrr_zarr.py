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


@pytest.fixture
def fake_archive(monkeypatch):
    """Every field reads as its lead hour (fcst) or 0 (anl); one cycle can be marked missing."""
    missing: set[datetime] = set()

    def read(url, cells, with_time):
        if any(f"{m:%Y%m%d_%H}z" in url for m in missing):
            return None
        if not with_time:
            return np.zeros((1, len(cells)))
        return np.repeat(np.arange(1, 19, dtype="float64")[:, None], len(cells), axis=1)

    monkeypatch.setattr(hz, "_read_cells", read)
    monkeypatch.setattr(hz, "neighbours", lambda registry, k=4: CELLS)
    return missing


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
