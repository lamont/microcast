"""Bronze copies row for row between catalogs, resumably."""

from __future__ import annotations

from datetime import UTC, datetime

import pyarrow as pa
from pyiceberg.catalog import load_catalog

from microcast.lake import catalog as lake
from microcast.lake.copy import copy_bronze
from microcast.lake.schemas import OBS_SCHEMA


def _catalog(path):
    (path / "warehouse").mkdir(parents=True)
    return load_catalog("t", type="sql", uri=f"sqlite:///{path / 'c.db'}", warehouse=(path / "warehouse").as_uri())


def _obs(month: int, value: float, ingested: datetime) -> pa.Table:
    t = datetime(2026, month, 15, tzinfo=UTC)
    row = dict(source="iem_asos", station_id="KSFO", obs_time=t, variable="t2m", value=value, qc_flag=None)
    return pa.Table.from_pylist(
        [row | dict(ingest_batch=f"b{month}", ingested_at=ingested)], schema=OBS_SCHEMA.as_arrow()
    )


def test_copy_preserves_rows_and_resumes(tmp_path):
    src, dst = _catalog(tmp_path / "src"), _catalog(tmp_path / "dst")
    lake.init_lake(src)
    first = datetime(2026, 1, 1, tzinfo=UTC)
    lake.append_bronze("bronze.obs", _obs(1, 10.0, first), src, batches=["b1"])
    lake.append_bronze("bronze.obs", _obs(1, 11.0, first.replace(day=2)), src, batches=["b1"])  # a correction
    lake.append_bronze("bronze.obs", _obs(2, 12.0, first), src, batches=["b2"])

    assert copy_bronze(src, dst, log=lambda _: None)["bronze.obs"] == 3
    got = dst.load_table("bronze.obs").scan().to_arrow().sort_by("ingested_at")
    want = src.load_table("bronze.obs").scan().to_arrow().sort_by("ingested_at")
    assert got.equals(want)  # both versions of the corrected row, ingested_at intact
    assert lake.recorded_batches("bronze.obs", dst) == {"b1", "b2"}

    lake.append_bronze("bronze.obs", _obs(3, 13.0, first), src, batches=["b3"])
    assert copy_bronze(src, dst, log=lambda _: None)["bronze.obs"] == 1  # only the new month
