"""Upwind (MADIS) push features on synthetic 10-minute obs."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from microcast.transform import upwind

T0 = pd.Timestamp("2026-07-01T00:00Z")


def _obs(days: int = 16, push_at: pd.Timestamp | None = None) -> pd.DataFrame:
    t = pd.date_range(T0, periods=days * 144, freq="10min")
    rows = []
    for sid in ("604PG", "D5422", "C5988"):
        temp = np.full(len(t), 15.0)
        rh = np.full(len(t), 70.0)
        if push_at is not None and sid == "604PG":
            after = t >= push_at
            temp[after] -= 3.0  # a 3 C drop and 15-point RH jump in one bucket
            rh[after] += 15.0
        rows += [dict(station_id=sid, obs_time=x, variable="t2m", value=v) for x, v in zip(t, temp, strict=True)]
        rows += [dict(station_id=sid, obs_time=x, variable="rh2m", value=v) for x, v in zip(t, rh, strict=True)]
    return pd.DataFrame(rows)


def _wide(long: pd.DataFrame) -> pd.DataFrame:
    return long.pivot(index="init_time", columns="feature", values="value")


def test_changes_anomalies_and_push_age():
    push = pd.Timestamp("2026-07-15T21:20Z")  # 14:20 local
    f = _wide(upwind.build_features(_obs(push_at=push)))
    at = f.loc[pd.Timestamp("2026-07-15T22:00Z")]  # uses the 21:50-22:00 bucket
    # The coast group mean: 604PG down 3 C, D5422 flat.
    assert at.up_coast_t2m_d1h == pytest.approx(-1.5)
    assert at.up_coast_rh_d1h == pytest.approx(7.5)
    assert at.up_coast_t2m_anom == pytest.approx(-1.5)  # 14 days of 15 C before it
    assert at.up_coast_east_t2m_anom == pytest.approx(-1.5)
    assert at.up_push_age_min == pytest.approx(40)  # onset bucket 21:20 -> 22:00
    before = f.loc[pd.Timestamp("2026-07-15T21:00Z")]
    assert np.isnan(before.get("up_push_age_min", np.nan))  # not seen yet
    assert before.up_coast_t2m_d1h == pytest.approx(0.0)


def test_only_whole_hours_and_no_anomaly_without_history():
    f = _wide(upwind.build_features(_obs(days=3)))
    assert (f.index.minute == 0).all()
    assert "up_coast_t2m_anom" not in f or f.up_coast_t2m_anom.isna().all()
