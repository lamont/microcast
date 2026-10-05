"""Scoring maths and an end-to-end backtest on synthetic gold rows."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from microcast import backtest
from microcast.lake.schemas import FORECASTS_SCHEMA, SCORES_SCHEMA, TRAINING_SCHEMA


def test_crps_gaussian_known_values():
    # At the mean with sigma 1: 2*phi(0) - 1/sqrt(pi).
    assert backtest.crps_gaussian(np.array([0.0]), np.array([1.0]), np.array([0.0]))[0] == pytest.approx(
        0.23370, abs=1e-5
    )
    # Far from a sharp forecast, CRPS tends to the absolute error.
    assert backtest.crps_gaussian(np.array([0.0]), np.array([1e-3]), np.array([5.0]))[0] == pytest.approx(5.0, abs=1e-3)


def test_monthly_folds_leave_training_months():
    vt = pd.Series(pd.date_range("2025-04-01", "2025-09-15", freq="h", tz="UTC"))
    folds = backtest.monthly_folds(vt, min_train_months=3)
    assert [f.name for f in folds] == ["2025-07", "2025-08", "2025-09"]


def _gold(days: int = 153, seed: int = 0) -> pd.DataFrame:
    """HRRR runs 2 degrees cold with noise; the rolling bias is the true bias."""
    rng = np.random.default_rng(seed)
    init = pd.date_range("2025-04-01", periods=days * 8, freq="3h", tz="UTC")
    df = pd.DataFrame([(i, lead) for i in init for lead in range(0, 7)], columns=["init_time", "lead_min"]).assign(
        lead_min=lambda d: d.lead_min * 60
    )
    df = df.reindex(columns=TRAINING_SCHEMA.column_names)
    df["init_time"] = df.init_time.fillna(pd.Series(np.repeat(init, 7)))
    df["lead_min"] = np.tile(np.arange(7) * 60, len(init))
    df["point_id"] = "KSFO"
    df["issue_time"] = df.init_time + pd.Timedelta(minutes=55)
    df["valid_time"] = df.init_time + pd.to_timedelta(df.lead_min, unit="m")
    truth = 15 + 5 * np.sin(2 * np.pi * df.valid_time.dt.hour / 24) + rng.normal(0, 0.5, len(df))
    df["obs_t2m"] = truth
    df["nwp_t2m"] = truth - 2 + rng.normal(0, 1.0, len(df))
    df["bias14_t2m"] = 2.0
    df["hour_sin"] = np.sin(2 * np.pi * df.valid_time.dt.hour / 24)
    return df


def test_corrected_models_beat_raw_on_a_biased_baseline():
    scored = backtest.run(_gold(), ["raw_hrrr", "bias_rolling", "gbm_residual"], ["t2m"], log=lambda _: None)
    assert (scored.lead_min >= 60).all()  # lead 0 is never scored
    # Leads from the last cycles verify in the next month but stay in their cycle's fold.
    assert scored.valid_time.max().month == 9 and set(scored.fold) == {"2025-07", "2025-08"}
    board = backtest.leaderboard(scored, n_boot=200)
    allpts = board[board.point == "all"].set_index(["model", "lead"])
    for model in ("bias_rolling", "gbm_residual"):
        for lead in ("1-3 h", "4-6 h"):
            row = allpts.loc[(model, lead)]
            assert row.skill > 0.3 and row.skill_lo > 0
    assert allpts.loc[("raw_hrrr", "1-3 h")].skill == pytest.approx(0.0)
    # Sigma is fitted on training residuals, so every model's p10-p90 band is roughly calibrated;
    # the corrected ones win by being sharper, not by covering more.
    for model in ("raw_hrrr", "bias_rolling", "gbm_residual"):
        assert 0.7 < allpts.loc[(model, "1-3 h")].coverage_80 < 0.9
    f, s = backtest.to_tables(scored.assign(run_id="r"))
    assert f.schema == FORECASTS_SCHEMA.as_arrow() and s.schema == SCORES_SCHEMA.as_arrow()
    monthly = backtest.monthly_skill(scored)
    assert set(monthly.columns) >= {"variable", "month", "model", "skill", "n"}


def test_models_are_compared_on_common_rows_only():
    scored = backtest.run(_gold(), ["raw_hrrr", "bias_rolling"], ["t2m"], log=lambda _: None)
    # bias_rolling loses a week of forecasts (say its input station was down).
    gap = (scored.model_name == "bias_rolling") & (scored.valid_time.dt.month == 8) & (scored.valid_time.dt.day <= 7)
    kept = scored[~gap]
    board = backtest.leaderboard(kept, n_boot=50)
    n = board[(board.point == "all")].groupby("model").n.sum()
    # The baseline is scored only where the challenger also forecast.
    assert n["raw_hrrr"] == n["bias_rolling"] == (kept.model_name == "bias_rolling").sum()
