from __future__ import annotations

import json
from datetime import UTC, datetime

import pyarrow as pa
import pytest
from pyiceberg.expressions import AlwaysTrue

from microcast import site
from microcast.lake import catalog as lake
from microcast.lake.schemas import NWP_ALIGNED_SCHEMA, NWP_FIELDS, NWP_POINT_SCHEMA, OBS_QC_SCHEMA, OBS_SCHEMA


class _Registry:
    stations = []


def _site(monkeypatch, tmp_path, catalog, reports=None):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    if reports:
        for name, value in reports.items():
            (reports_dir / name).write_text(value)
    monkeypatch.setattr(site, "reports_dir", lambda: reports_dir)
    monkeypatch.setattr(site, "get_catalog", lambda: catalog)
    monkeypatch.setattr(site.registry, "load", lambda: _Registry())
    return site.build(tmp_path / "out")


def test_build_initialized_empty_lake(monkeypatch, tmp_path, catalog):
    lake.init_lake(catalog)
    out = _site(monkeypatch, tmp_path, catalog)
    data = json.loads((out / "data.json").read_text())
    assert data["backtest"] is None
    assert data["gate"] == []
    assert data["leaderboard"] == []
    assert data["monthly"] == []
    assert data["analytics"] == {"by_lead": [], "by_hour": [], "pit": [], "raw_bias": []}
    assert data["freshness"]["hrrr"] == {"first": None, "last": None, "cycles": 0}


def test_build_with_lake_data_and_no_reports(monkeypatch, tmp_path, catalog):
    lake.init_lake(catalog)
    t = datetime(2026, 10, 4, 12, tzinfo=UTC)
    lake.append_bronze(
        "bronze.nwp_point",
        pa.Table.from_pylist(
            [
                {
                    "model": "hrrr",
                    "product": "sfc",
                    "init_time": t,
                    "lead_min": 60,
                    "valid_time": t.replace(hour=13),
                    "point_id": "LOCAL",
                    "variable": "t2m",
                    "level": "heightAboveGround:2",
                    "member": 0,
                    "k": 0,
                    "value": 12.0,
                    "grid_lat": 47.98,
                    "grid_lon": -122.72,
                    "grid_dist_m": 500.0,
                    "ingest_batch": "test/nwp",
                    "ingested_at": t,
                }
            ],
            schema=NWP_POINT_SCHEMA.as_arrow(),
        ),
        catalog,
        batches=["test/nwp"],
    )
    lake.append_bronze(
        "bronze.obs",
        pa.Table.from_pylist(
            [
                {
                    "source": "madis",
                    "station_id": "LOCAL",
                    "obs_time": t,
                    "variable": "t2m",
                    "value": 11.5,
                    "qc_flag": "V",
                    "ingest_batch": "test/obs",
                    "ingested_at": t,
                }
            ],
            schema=OBS_SCHEMA.as_arrow(),
        ),
        catalog,
        batches=["test/obs"],
    )
    aligned = {field: None for field in NWP_FIELDS}
    aligned.update(
        model="hrrr", init_time=t, lead_min=60, valid_time=t.replace(hour=13), point_id="LOCAL", ingested_at=t
    )
    lake.replace_partition(
        "silver.nwp_aligned",
        pa.Table.from_pylist([aligned], schema=NWP_ALIGNED_SCHEMA.as_arrow()),
        AlwaysTrue(),
        catalog,
    )
    lake.replace_partition(
        "silver.obs_qc",
        pa.Table.from_pylist(
            [
                {
                    "source": "madis",
                    "station_id": "LOCAL",
                    "obs_time": t,
                    "variable": "t2m",
                    "value": 11.5,
                    "qc_flag": "ok",
                    "ingested_at": t,
                }
            ],
            schema=OBS_QC_SCHEMA.as_arrow(),
        ),
        AlwaysTrue(),
        catalog,
    )

    data = json.loads((_site(monkeypatch, tmp_path, catalog) / "data.json").read_text())
    assert data["backtest"] is None
    assert data["leaderboard"] == []
    assert data["freshness"]["hrrr"]["cycles"] == 1
    station = data["freshness"]["stations"]
    assert station[0]["station_id"] == "LOCAL"
    assert station[0]["source"] == "madis"
    assert station[0]["rows"] == 1
    assert station[0]["flagged"] == 0.0


def test_partial_reports_are_rejected(monkeypatch, tmp_path, catalog):
    lake.init_lake(catalog)
    with pytest.raises(RuntimeError, match="backtest reports are incomplete"):
        _site(monkeypatch, tmp_path, catalog, {"leaderboard.csv": "model,point\nraw_hrrr,all\n"})


def test_existing_reports_populate_leaderboard(monkeypatch, tmp_path, catalog):
    lake.init_lake(catalog)
    reports = {
        "leaderboard.csv": "model,point,variable,lead\nraw_hrrr,all,t2m,1-3 h\n",
        "monthly_skill.csv": "model,variable,month,skill\nraw_hrrr,t2m,2025-01,0\n",
        "backtest_meta.json": (
            '{"generated_at":"2025-01-01T00:00:00Z","git_sha":"abc","gold_snapshot_id":2980748795174509963}'
        ),
    }
    data = json.loads((_site(monkeypatch, tmp_path, catalog, reports) / "data.json").read_text())
    assert data["backtest"]["git_sha"] == "abc"
    assert data["backtest"]["gold_snapshot_id"] == "2980748795174509963"
    assert data["leaderboard"][0]["model"] == "raw_hrrr"
