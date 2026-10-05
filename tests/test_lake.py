from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pyarrow as pa
import pytest
from pyiceberg.expressions import EqualTo

from microcast.lake import catalog as lake
from microcast.lake.schemas import NWP_POINT_SCHEMA


def _rows(batch: str, value: float, n: int = 3) -> pa.Table:
    t0 = datetime(2026, 10, 4, 12, tzinfo=UTC)
    return pa.Table.from_pylist(
        [
            {
                "model": "hrrr",
                "product": "sfc",
                "init_time": t0,
                "lead_min": 60 * i,
                "valid_time": t0.replace(hour=12 + i),
                "point_id": "home",
                "variable": "t2m",
                "level": "heightAboveGround:2",
                "member": 0,
                "k": 0,
                "value": value,
                "grid_lat": 37.76,
                "grid_lon": -122.44,
                "grid_dist_m": 900.0,
                "ingest_batch": batch,
                "ingested_at": datetime.now(UTC),
            }
            for i in range(n)
        ],
        schema=NWP_POINT_SCHEMA.as_arrow(),
    )


def test_init_is_idempotent(catalog):
    assert set(lake.init_lake(catalog)) == {"bronze.nwp_point", "bronze.obs"}
    assert lake.init_lake(catalog) == []


def test_bronze_appends_keep_every_batch(catalog):
    lake.init_lake(catalog)
    s1 = lake.append_bronze("bronze.nwp_point", _rows("hrrr/2026-10-04T12/f000/m0", 288.0), catalog)
    # A rerun of the same batch is a new append, not an overwrite.
    s2 = lake.append_bronze("bronze.nwp_point", _rows("hrrr/2026-10-04T12/f000/m0", 289.0), catalog)
    table = catalog.load_table("bronze.nwp_point")
    assert len(table.snapshots()) == 2
    assert table.scan().to_arrow().num_rows == 6
    # The first snapshot is still readable exactly as it was.
    assert table.scan(snapshot_id=s1).to_arrow().num_rows == 3
    assert s1 != s2


def test_bronze_refuses_overwrite(catalog):
    lake.init_lake(catalog)
    with pytest.raises(ValueError, match="append-only"):
        lake.replace_partition("bronze.nwp_point", _rows("b", 1.0), EqualTo("model", "hrrr"), catalog)


def test_tagged_snapshot_survives_expiry(catalog):
    lake.init_lake(catalog)
    s1 = lake.append_bronze("bronze.nwp_point", _rows("b1", 1.0), catalog)
    lake.append_bronze("bronze.nwp_point", _rows("b2", 2.0), catalog)
    lake.tag_snapshot("bronze.nwp_point", "mlflow-test", s1, catalog)
    table = catalog.load_table("bronze.nwp_point")
    table.maintenance.expire_snapshots().older_than(datetime.now(UTC)).commit()
    table = catalog.load_table("bronze.nwp_point")
    assert s1 in {s.snapshot_id for s in table.snapshots()}
    assert table.scan(snapshot_id=table.snapshot_by_name("mlflow-test").snapshot_id).to_arrow().num_rows == 3


def test_duckdb_reads_latest_batch(catalog):
    lake.init_lake(catalog)
    lake.append_bronze("bronze.nwp_point", _rows("b", 288.0), catalog)
    lake.append_bronze("bronze.nwp_point", _rows("b", 289.0), catalog)
    con = duckdb.connect()
    view = lake.to_duckdb("bronze.nwp_point", con, catalog=catalog)
    # Silver-style dedupe: latest ingest wins per natural key.
    latest = con.sql(
        f"""
        select value from {view}
        qualify row_number() over (
            partition by model, init_time, lead_min, point_id, variable, level, member, k
            order by ingested_at desc) = 1
        """
    ).fetchall()
    assert {v for (v,) in latest} == {289.0}
