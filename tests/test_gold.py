"""gold.training_examples: hour snapping, gust fill, and the as-of rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest

from microcast.lake.schemas import NWP_ALIGNED_SCHEMA, NWP_FIELDS, OBS_QC_SCHEMA, TRAINING_SCHEMA
from microcast.transform import gold

T0 = datetime(2026, 7, 1, 0, tzinfo=UTC)
H = timedelta(hours=1)


def _nwp(cycles: int, leads=(0, 1, 2), t2m=10.0) -> pa.Table:
    rows = []
    for c in range(cycles):
        for lead in leads:
            init = T0 + c * H
            row = dict.fromkeys(NWP_FIELDS) | dict(t2m=t2m, gust=5.0, wind10=3.0)
            rows.append(
                row
                | dict(model="hrrr", init_time=init, lead_min=60 * lead, valid_time=init + lead * H, point_id="KSFO")
                | dict(ingested_at=init)
            )
    return pa.Table.from_pylist(rows, schema=NWP_ALIGNED_SCHEMA.as_arrow())


def _obs(rows: list[tuple[datetime, str, float]]) -> pa.Table:
    return pa.Table.from_pylist(
        [
            dict(source="iem_asos", station_id="KSFO", obs_time=t, variable=v, value=x, qc_flag="ok", ingested_at=t)
            for t, v, x in rows
        ],
        schema=OBS_QC_SCHEMA.as_arrow(),
    )


def _by_key(t: pa.Table) -> dict:
    return {(r["init_time"], r["lead_min"]): r for r in t.to_pylist()}


def test_obs_snapped_to_nearest_report_and_gust_filled():
    obs = _obs([
        (T0 + H - timedelta(minutes=4), "t2m", 12.0),   # :56 -> 01:00
        (T0 + H + timedelta(minutes=15), "t2m", 99.0),  # farther from 01:00, ignored
        (T0 + H - timedelta(minutes=4), "wind10", 4.0),  # no gust reported: gust = wind
        (T0 + 2 * H - timedelta(minutes=4), "wind10", 4.0),
        (T0 + 2 * H - timedelta(minutes=4), "gust", 9.0),
        (T0 + 3 * H - timedelta(minutes=30), "t2m", 50.0),  # 30 min off: no truth for 03:00
    ])  # fmt: skip
    rows = _by_key(gold.build_training(_nwp(2), obs))
    r = rows[T0, 60]
    assert (r["obs_t2m"], r["obs_gust"]) == (12.0, 4.0)
    assert rows[T0, 120]["obs_gust"] == 9.0
    assert rows[T0 + H, 120]["obs_t2m"] is None
    assert gold.build_training(_nwp(1), obs).schema == TRAINING_SCHEMA.as_arrow()


def test_rolling_bias_only_sees_truth_before_issue_time():
    # Obs 2 degrees above HRRR at every hour; a huge error at 05:00.
    obs = _obs([(T0 + h * H, "t2m", 12.0 if h != 5 else 40.0) for h in range(12)])
    rows = _by_key(gold.build_training(_nwp(8), obs))
    # Cycle 04Z issues at 04:55: lead-1 residuals up to valid 04:00 are visible (2.0 each),
    # the 05:00 spike is not.
    assert rows[T0 + 4 * H, 60]["bias14_t2m"] == pytest.approx(2.0)
    # Cycle 05Z issues at 05:55, after 05:00 verified: the spike now counts.
    assert rows[T0 + 5 * H, 0]["bias14_t2m"] > 2.0
    # Lead 1 of the first cycle verifies at 01:00, after its 00:55 issue: nothing to see yet.
    assert rows[T0, 60]["bias14_t2m"] is None
    # Lead 0 verifies at init, before issue: it is a hindcast, which is why backtests skip it.
    assert rows[T0, 0]["bias14_t2m"] == pytest.approx(2.0)


def test_error_at_init_uses_this_cycles_f00():
    obs = _obs([(T0 + h * H, "t2m", 11.5) for h in range(4)])
    rows = _by_key(gold.build_training(_nwp(2), obs))
    assert rows[T0 + H, 120]["err_at_init_t2m"] == pytest.approx(1.5)
    assert rows[T0 + H, 120]["obs_last_t2m"] == 11.5
