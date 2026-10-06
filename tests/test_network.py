"""gold.network_features: PurpleAir transect features and their as-of timing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest

from microcast.lake.schemas import NETWORK_FEATURES_SCHEMA, OBS_QC_SCHEMA
from microcast.transform import network

T0 = datetime(2026, 7, 1, tzinfo=UTC)
H = timedelta(hours=1)


def _obs(rows: list[tuple[str, datetime, float]], flag: str = "ok") -> pa.Table:
    return pa.Table.from_pylist(
        [
            dict(
                source="purpleair", station_id=s, obs_time=t, variable="t_sensor", value=v, qc_flag=flag, ingested_at=t
            )
            for s, t, v in rows
        ],
        schema=OBS_QC_SCHEMA.as_arrow(),
    )


def _get(t: pa.Table, init: datetime, feature: str):
    hits = [r["value"] for r in t.to_pylist() if r["init_time"] == init and r["feature"] == feature]
    return hits[0] if hits else None


SENSORS = {"pa_1": -122.50, "pa_2": -122.49, "pa_3": -122.44, "pa_4": -122.43}  # two west, two east


def test_changes_use_only_hours_complete_at_init():
    # pa_1 warms 1 degree an hour; the hour starting at 05:00 jumps by 10.
    rows = [("pa_1", T0 + h * H, float(h) + (10 if h == 5 else 0)) for h in range(8)]
    out = network.build_features(_obs(rows), SENSORS)
    assert out.schema == NETWORK_FEATURES_SCHEMA.as_arrow()
    # Cycle 05Z: the last complete hour starts at 04:00, so the jump is not visible yet.
    assert _get(out, T0 + 5 * H, "pa_1_d1h") == pytest.approx(1.0)
    assert _get(out, T0 + 5 * H, "pa_1_d3h") == pytest.approx(3.0)
    # Cycle 06Z sees the 05:00 hour.
    assert _get(out, T0 + 6 * H, "pa_1_d1h") == pytest.approx(11.0)
    # Nothing an hour back for the first hour: no feature rather than a made-up one.
    assert _get(out, T0 + H, "pa_1_d1h") is None


def test_anomaly_is_against_the_same_hour_on_earlier_days():
    # 20 degrees every day at 14:00 for two weeks, then 15 today; other hours are noise.
    rows = [("pa_1", T0 + d * 24 * H + 14 * H, 20.0) for d in range(14)]
    rows += [("pa_1", T0 + d * 24 * H + 3 * H, 99.0) for d in range(15)]
    rows.append(("pa_1", T0 + 14 * 24 * H + 14 * H, 15.0))
    out = network.build_features(_obs(rows), SENSORS)
    assert _get(out, T0 + 14 * 24 * H + 15 * H, "pa_1_anom") == pytest.approx(-5.0)


def test_west_minus_east_and_flagged_rows():
    t = T0 + 30 * H
    hist = [(s, t - d * 24 * H, 10.0) for s in SENSORS for d in range(1, 4)]
    now = [("pa_1", t, 8.0), ("pa_2", t, 8.0), ("pa_3", t, 12.0), ("pa_4", t, 12.0)]
    out = network.build_features(pa.concat_tables([_obs(hist + now), _obs([("pa_1", t, 50.0)], flag="range")]), SENSORS)
    # The flagged 50 degree reading is ignored; west is 2 below normal, east 2 above.
    assert _get(out, t + H, "pa_west_east_anom") == pytest.approx(-4.0)
    assert _get(out, t + H, "pa_mean_anom") == pytest.approx(0.0)
