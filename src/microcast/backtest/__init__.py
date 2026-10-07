"""Rolling-origin backtests, scoring, and the leaderboard.

Protocol (design doc, Backtesting and evaluation):

* Monthly folds: train on every row whose truth was known before the test
  month starts, test on that month, step a month. Every model sees the same
  folds.
* Leave-stations-out (``run_holdout``): the stations split into groups; each
  group is tested on models trained only on the other stations, in quarterly
  rolling folds. This is the test for points with no station of their own.
* Lead 0 is not scored: issued 55 min after its valid time, it is a hindcast.
  Leads 1-6 are ~5 min to ~5 h ahead of issue.
* Scores: Gaussian CRPS (primary), MAE of the mean, and p10-p90 coverage.
  Skill = 1 - CRPS / CRPS(raw_hrrr) on the same rows, with a 95% CI from a
  day-block bootstrap (days resampled whole, since errors within a day are
  correlated).
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pyarrow as pa
from scipy.stats import norm

from microcast import models as zoo
from microcast.lake.schemas import FORECASTS_SCHEMA, SCORES_SCHEMA

BASELINE = "raw_hrrr"
Z90 = norm.ppf(0.9)
LEAD_BUCKETS = [(1, 3, "1-3 h"), (4, 6, "4-6 h")]


def crps_gaussian(mu: np.ndarray, sigma: np.ndarray, y: np.ndarray) -> np.ndarray:
    z = (y - mu) / sigma
    return sigma * (z * (2 * norm.cdf(z) - 1) + 2 * norm.pdf(z) - 1 / np.sqrt(np.pi))


def lead_bucket(lead_min: pd.Series) -> pd.Series:
    h = lead_min // 60
    out = pd.Series(pd.NA, index=lead_min.index, dtype="string")
    for lo, hi, label in LEAD_BUCKETS:
        out[(h >= lo) & (h <= hi)] = label
    return out


@dataclass(frozen=True)
class Fold:
    name: str
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def monthly_folds(init_times: pd.Series, min_train_months: int = 3) -> list[Fold]:
    first = init_times.min().tz_convert(None).to_period("M").to_timestamp().tz_localize("UTC")
    last = init_times.max()
    folds, start = [], first + pd.DateOffset(months=min_train_months)
    while start <= last:
        end = start + pd.DateOffset(months=1)
        folds.append(Fold(f"{start:%Y-%m}", start, end))
        start = end
    return folds


def station_groups(stations: list[str], n_groups: int, seed: int = 7) -> list[list[str]]:
    """A fixed random split of the stations into ``n_groups`` near-equal groups."""
    order = np.random.default_rng(seed).permutation(sorted(stations))
    return [sorted(order[i::n_groups].tolist()) for i in range(n_groups)]


def _scored_frame(name: str, target: str, fold: str, test: pd.DataFrame, pred: pd.DataFrame, run_id: str = ""):
    return pd.DataFrame(
        {
            "model_name": name,
            "run_id": run_id,
            "kind": "backtest",
            "fold": fold,
            "variable": target,
            "point_id": test.point_id,
            "init_time": test.init_time,
            "issue_time": test.issue_time,
            "lead_min": test.lead_min,
            "valid_time": test.valid_time,
            "obs": test[f"obs_{target}"],
            "mu": pred.mu,
            "sigma": pred.sigma,
        }
    )


def _score(df: pd.DataFrame) -> pd.DataFrame:
    df["p10"], df["p50"], df["p90"] = df.mu - Z90 * df.sigma, df.mu, df.mu + Z90 * df.sigma
    df["error"] = df.mu - df.obs
    df["crps"] = crps_gaussian(df.mu.to_numpy(), df.sigma.to_numpy(), df.obs.to_numpy())
    df["in_p10_p90"] = ((df.obs >= df.p10) & (df.obs <= df.p90)).astype("int32")
    return df


def run_holdout(
    gold: pd.DataFrame,
    model_names: list[str],
    targets: list[str],
    groups: list[list[str]],
    *,
    min_train_months: int = 3,
    fold_months: int = 3,
    log: Callable[[str], None] = print,
) -> pd.DataFrame:
    """Leave-stations-out: train on the other groups before the fold, test on this group in it."""
    out = []
    for target in targets:
        rows = gold[gold[f"obs_{target}"].notna() & gold[f"nwp_{target}"].notna()]
        folds = monthly_folds(rows.init_time, min_train_months)[::fold_months]
        for name in model_names:
            for fold in folds:
                end = fold.test_start + pd.DateOffset(months=fold_months)
                for k, group in enumerate(groups):
                    held = rows.point_id.isin(group)
                    train = rows[~held & (rows.valid_time < fold.test_start)]
                    test = rows[held & (rows.init_time >= fold.test_start) & (rows.init_time < end)]
                    test = test[test.lead_min >= 60]
                    if test.empty or train.empty:
                        continue
                    model = zoo.make(name, target)
                    model.fit(train)
                    out.append(_scored_frame(name, target, f"{fold.name}/g{k}", test, model.predict(test)))
            log(f"  {target} {name}: {len(folds)} folds x {len(groups)} groups")
    return _score(pd.concat(out, ignore_index=True))


def run(
    gold: pd.DataFrame,
    model_names: list[str],
    targets: list[str],
    *,
    min_train_months: int = 3,
    run_ids: dict[tuple[str, str], str] | None = None,
    log: Callable[[str], None] = print,
) -> pd.DataFrame:
    """Backtest every model on every target; returns scored forecast rows."""
    run_ids = run_ids or {}
    out = []
    for target in targets:
        rows = gold[gold[f"obs_{target}"].notna() & gold[f"nwp_{target}"].notna()]
        folds = monthly_folds(rows.init_time, min_train_months)
        for name in model_names:
            for fold in folds:
                train = rows[rows.valid_time < fold.test_start]
                # Test rows belong to the month their cycle ran in, so the last cycles' leads
                # into the next month don't make a near-empty extra fold.
                test = rows[(rows.init_time >= fold.test_start) & (rows.init_time < fold.test_end)]
                test = test[test.lead_min >= 60]
                if test.empty or train.empty:
                    continue
                model = zoo.make(name, target)
                model.fit(train)
                pred = model.predict(test)
                out.append(_scored_frame(name, target, fold.name, test, pred, run_ids.get((name, target), "")))
            log(f"  {target} {name}: {len(folds)} folds")
    return _score(pd.concat(out, ignore_index=True))


def to_tables(scored: pd.DataFrame) -> tuple[pa.Table, pa.Table]:
    """Split scored rows into gold.forecasts and gold.scores Arrow tables."""
    f = pa.Table.from_pandas(scored[FORECASTS_SCHEMA.column_names], preserve_index=False)
    s = pa.Table.from_pandas(scored[SCORES_SCHEMA.column_names], preserve_index=False)
    return f.cast(FORECASTS_SCHEMA.as_arrow()), s.cast(SCORES_SCHEMA.as_arrow())


# --- leaderboard ------------------------------------------------------------------


KEY = ["variable", "point_id", "init_time", "lead_min"]


def common_rows(scores: pd.DataFrame) -> pd.DataFrame:
    """Only forecasts every model made, so models are compared on identical cases.

    A model that needs an input some rows lack (a station that came online
    later, a feature with gaps) forecasts fewer rows; without this it would be
    scored on an easier or harder subset than its rivals.
    """
    n_models = scores.groupby("variable").model_name.transform("nunique")
    per_key = scores.groupby(KEY).model_name.transform("nunique")
    return scores[per_key == n_models]


def _paired(scores: pd.DataFrame, model: str) -> pd.DataFrame:
    """Model rows joined to baseline rows for the same forecast."""
    key = ["variable", "point_id", "init_time", "lead_min"]
    m = scores[scores.model_name == model]
    b = scores[scores.model_name == BASELINE][key + ["crps"]].rename(columns={"crps": "crps_base"})
    return m.merge(b, on=key)


def bootstrap_skill(paired: pd.DataFrame, n: int = 1000, seed: int = 7) -> tuple[float, float, float]:
    """Skill and 95% CI, resampling whole days."""
    day = paired.valid_time.dt.floor("D")
    daily = paired.groupby(day)[["crps", "crps_base"]].sum()
    skill = 1 - daily.crps.sum() / daily.crps_base.sum()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(daily), size=(n, len(daily)))
    m, b = daily.crps.to_numpy()[idx].sum(axis=1), daily.crps_base.to_numpy()[idx].sum(axis=1)
    lo, hi = np.percentile(1 - m / b, [2.5, 97.5])
    return float(skill), float(lo), float(hi)


# The phase 1 gate's stations. Pooled separately so the gate stays comparable as
# network stations join the backtest (D11).
ORIGINAL_STATIONS = ("FTPC1", "KOAK", "KSFO", "SFOC1")


def leaderboard(
    scores: pd.DataFrame, n_boot: int = 1000, groups: dict[str, Collection[str]] | None = None
) -> pd.DataFrame:
    """One row per (variable, model, lead bucket, point), on common rows only.

    ``point`` is a station id, ``all`` (every station pooled), or the name of a
    group in ``groups`` (those stations pooled).
    """
    scores = common_rows(scores).assign(bucket=lead_bucket(scores.lead_min))
    rows = []
    for model in sorted(scores.model_name.unique()):
        paired = _paired(scores, model)
        for (variable, bucket), g in paired.groupby(["variable", "bucket"]):
            pooled = [(name, g[g.point_id.isin(ids)]) for name, ids in (groups or {}).items()]
            for point, gp in [("all", g), *[(n, x) for n, x in pooled if len(x)], *g.groupby("point_id")]:
                skill, lo, hi = bootstrap_skill(gp, n_boot)
                rows.append(
                    dict(
                        variable=variable,
                        model=model,
                        lead=bucket,
                        point=point,
                        n=len(gp),
                        crps=gp.crps.mean(),
                        mae=gp.error.abs().mean(),
                        bias=gp.error.mean(),
                        coverage_80=gp.in_p10_p90.mean(),
                        skill=skill,
                        skill_lo=lo,
                        skill_hi=hi,
                    )
                )
    return pd.DataFrame(rows)


def monthly_skill(scores: pd.DataFrame) -> pd.DataFrame:
    """Skill vs baseline per month (of the forecast cycle): performance over time."""
    scores = common_rows(scores)
    rows = []
    for model in sorted(scores.model_name.unique()):
        paired = _paired(scores, model)
        month = paired.init_time.dt.tz_convert(None).dt.to_period("M").astype(str)
        g = paired.groupby([paired.variable, month])
        agg = g.agg(crps=("crps", "mean"), crps_base=("crps_base", "mean"), mae=("error", lambda e: e.abs().mean()))
        agg["skill"] = 1 - agg.crps / agg.crps_base
        agg["n"] = g.size()
        rows.append(agg.reset_index().rename(columns={"init_time": "month"}).assign(model=model))
    return pd.concat(rows, ignore_index=True)
