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


def test_leaderboard_pools_named_station_groups():
    a, b = _gold(), _gold(seed=1).assign(point_id="C5988")
    scored = backtest.run(
        pd.concat([a, b], ignore_index=True), ["raw_hrrr", "bias_rolling"], ["t2m"], log=lambda _: None
    )
    board = backtest.leaderboard(scored, n_boot=50, groups={"original": ["KSFO"], "empty": ["NOPE"]})
    n = board[board.model == "bias_rolling"].groupby("point").n.sum()
    assert n["original"] == n["KSFO"] and n["all"] == n["KSFO"] + n["C5988"]
    assert "empty" not in n  # a group with no scored stations gets no row


def test_gbm_purpleair_uses_the_network_features():
    from microcast import models

    g = _gold(days=120)
    rng = np.random.default_rng(3)
    # A network signal that explains part of HRRR's error the base features can't see.
    g["pa_west_east_anom"] = rng.normal(0, 1, len(g))
    g["obs_t2m"] = g.obs_t2m + 1.5 * g.pa_west_east_anom
    scored = backtest.run(g, ["raw_hrrr", "gbm_residual", "gbm_purpleair"], ["t2m"], log=lambda _: None)
    crps = scored.groupby("model_name").crps.mean()
    assert crps["gbm_purpleair"] < 0.8 * crps["gbm_residual"]
    with pytest.raises(ValueError, match="pa_"):
        models.make("gbm_purpleair", "t2m").fit(_gold(days=30))


def test_regime_inputs_are_derived():
    from microcast import models as zoo

    df = pd.DataFrame({"nwp_t2m": [15.0], "nwp_t925": [22.0], "nwp_t850": [20.0], "nwp_u925": [5.0], "nwp_v925": [0.0]})
    out = zoo.with_regime(df)
    assert out.inv925[0] == pytest.approx(7.0) and out.inv850[0] == pytest.approx(5.0)
    assert out.onshore925[0] == pytest.approx(5.0)  # a westerly blows straight in through the Gate
    assert out.wind925[0] == pytest.approx(5.0)
    assert set(zoo.regime_columns()) <= set(out.columns) | {"nwp_hpbl"}


def test_spread_sigma_grows_with_spread():
    from microcast import models as zoo

    rng = np.random.default_rng(0)
    spread = pd.Series(rng.uniform(0.2, 2.0, 5000))
    lead = pd.Series(np.full(5000, 60))
    resid = pd.Series(rng.normal(0, np.sqrt(0.25 + spread**2)))
    s = zoo.SpreadSigma()
    s.fit(lead, spread, resid)
    a, b = s.coef[60]
    assert a == pytest.approx(0.25, abs=0.1) and b == pytest.approx(1.0, abs=0.15)
    narrow, wide, none = s.predict(pd.Series([60, 60, 60]), pd.Series([0.2, 2.0, np.nan]))
    assert narrow < wide and none == pytest.approx(s.rms[60])


def test_stack_uses_every_input_and_spread_sigma():
    from microcast import models as zoo

    g = _gold(days=60)
    rng = np.random.default_rng(4)
    n = len(g)
    g["st_elev"] = 10.0
    g["up_coast_t2m_anom"] = rng.normal(0, 1, n)
    for f in ("t925", "t850", "u925", "v925", "hpbl"):
        g[f"nwp_{f}"] = rng.normal(0, 1, n)
    g["lag_n_t2m"], g["lag_mean_t2m"], g["lag_d1_t2m"] = 6.0, g.nwp_t2m, 0.0
    g["lag_sd_t2m"] = rng.uniform(0.2, 2.0, n)
    m = zoo.make("gbm_terrain_stack", "t2m")
    m.fit(g)
    assert {"st_elev", "up_coast_t2m_anom", "inv925", "onshore925", "lag_sd_t2m"} <= set(m.cols)
    x = g.tail(2).assign(lag_sd_t2m=[0.2, 2.0], lead_min=60)
    narrow, wide = m.predict(x).sigma
    assert narrow <= wide


def test_paired_change_is_relative_crps_on_shared_rows():
    from microcast.backtest import ablation

    t = pd.date_range("2026-07-01", periods=40, freq="D", tz="UTC")
    base = pd.DataFrame({"model_name": "a", "variable": "t2m", "point_id": "X", "init_time": t, "lead_min": 60,
                         "valid_time": t, "crps": 1.0})  # fmt: skip
    better = base.assign(model_name="b", crps=0.9)
    out = ablation.paired_change(pd.concat([base, better]), "b", "a", n_boot=200)
    row = out[out.point == "all"].iloc[0]
    assert row.change == pytest.approx(-0.1) and row.lo == pytest.approx(-0.1) and row.hi == pytest.approx(-0.1)
