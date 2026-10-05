"""silver transforms on synthetic bronze rows."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest

from microcast.lake.schemas import NWP_POINT_SCHEMA, OBS_SCHEMA
from microcast.transform import silver

INIT = datetime(2026, 7, 1, 20, tzinfo=UTC)


def _nwp(rows: list[dict]) -> pa.Table:
    base = dict(
        model="hrrr", product="sfc", init_time=INIT, lead_min=60, valid_time=INIT + timedelta(hours=1),
        point_id="home", level="heightAboveGround:2", member=0, grid_lat=37.76, grid_lon=-122.43,
        grid_dist_m=1000.0, ingest_batch="b", ingested_at=INIT,
    )  # fmt: skip
    return pa.Table.from_pylist([base | r for r in rows], schema=NWP_POINT_SCHEMA.as_arrow())


def test_idw_and_kelvin():
    # Nearer cell (500 m) weighs twice the farther one (1000 m): (2*290 + 1*293) / 3 = 291 K.
    b = _nwp([
        dict(variable="t2m", k=0, value=290.0, grid_dist_m=500.0),
        dict(variable="t2m", k=1, value=293.0, grid_dist_m=1000.0),
    ])  # fmt: skip
    row = silver.build_nwp_aligned(b).to_pylist()[0]
    assert row["t2m"] == pytest.approx(291 - 273.15)
    assert row["t2m_spread"] == pytest.approx(3.0)
    assert row["gust"] is None


def test_latest_ingest_wins():
    later = INIT + timedelta(hours=2)
    b = _nwp([
        dict(variable="t2m", k=0, value=290.0),
        dict(variable="t2m", k=0, value=280.0, ingested_at=later),
    ])  # fmt: skip
    row = silver.build_nwp_aligned(b).to_pylist()[0]
    assert row["t2m"] == pytest.approx(280 - 273.15)
    assert row["ingested_at"] == later


@pytest.mark.parametrize(
    ("lon", "expected_dir"),
    [(-97.5, 180.0), (-122.43, 180.0 - 0.6225146 * 24.93)],
)
def test_wind_rotated_to_earth_relative(lon, expected_dir):
    # A 10 m/s grid-relative southerly. On the central meridian grid north is true north.
    # West of it, grid north points ~15.5 degrees west of true north at SF, so the wind
    # blows toward ~344.5 and comes from ~164.5 (SSE).
    b = _nwp([
        dict(variable="u10", level="heightAboveGround:10", k=0, value=0.0, grid_lon=lon),
        dict(variable="v10", level="heightAboveGround:10", k=0, value=10.0, grid_lon=lon),
    ])  # fmt: skip
    row = silver.build_nwp_aligned(b).to_pylist()[0]
    assert row["wind10"] == pytest.approx(10.0)
    assert row["wdir10"] == pytest.approx(expected_dir, abs=0.05)
    assert math.hypot(row["u10"], row["v10"]) == pytest.approx(10.0)


def test_cloud_levels_split():
    b = _nwp([
        dict(variable="gh", level="cloudBase:0", k=0, value=300.0),
        dict(variable="gh", level="cloudCeiling:0", k=0, value=450.0),
    ])  # fmt: skip
    row = silver.build_nwp_aligned(b).to_pylist()[0]
    assert (row["cloud_base"], row["ceiling"]) == (300.0, 450.0)


def test_obs_qc_dedupes_and_flags():
    t = INIT
    rows = [
        dict(variable="t2m", value=15.0, ingested_at=t),
        dict(variable="t2m", value=16.0, ingested_at=t + timedelta(days=1)),  # correction
        dict(variable="gust", value=120.0, ingested_at=t),
        dict(variable="pm25_b", value=3333.56, ingested_at=t),  # fouled PurpleAir channel
    ]
    base = dict(source="iem_asos", station_id="KSFO", obs_time=t, qc_flag=None, ingest_batch="b")
    out = silver.build_obs_qc(pa.Table.from_pylist([base | r for r in rows], schema=OBS_SCHEMA.as_arrow()))
    got = {r["variable"]: (r["value"], r["qc_flag"]) for r in out.to_pylist()}
    assert got == {"t2m": (16.0, "ok"), "gust": (120.0, "range"), "pm25_b": (3333.56, "range")}
