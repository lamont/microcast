"""Paired ablations: a model against its twin without one feature group, on identical rows.

Exploratory runs that write only ``data/reports/experiments/<name>/``, never
the lake or MLflow, so the stored rolling backtest (gold.forecasts/scores)
stays the one the site shows. Each run records the snapshots it read.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import numpy as np
import pandas as pd

from microcast import backtest


def paired_change(scores: pd.DataFrame, model: str, base: str, n_boot: int = 1000, seed: int = 7) -> pd.DataFrame:
    """CRPS change of ``model`` vs ``base`` (negative = ``model`` better), day-block bootstrap 95% CI.

    One row per (variable, lead bucket, point group): ``all``, plus each station.
    """
    key = ["variable", "point_id", "init_time", "lead_min"]
    m = scores[scores.model_name == model][[*key, "valid_time", "crps"]]
    b = scores[scores.model_name == base][[*key, "crps"]].rename(columns={"crps": "crps_base"})
    p = m.merge(b, on=key).assign(bucket=lambda d: backtest.lead_bucket(d.lead_min))
    rows = []
    rng = np.random.default_rng(seed)
    for (variable, bucket), g in p.groupby(["variable", "bucket"]):
        for point, gp in [("all", g), *g.groupby("point_id")]:
            daily = gp.groupby(gp.valid_time.dt.floor("D"))[["crps", "crps_base"]].sum()
            change = daily.crps.sum() / daily.crps_base.sum() - 1
            idx = rng.integers(0, len(daily), size=(n_boot, len(daily)))
            boot = daily.crps.to_numpy()[idx].sum(axis=1) / daily.crps_base.to_numpy()[idx].sum(axis=1) - 1
            lo, hi = np.percentile(boot, [2.5, 97.5])
            rows.append(dict(variable=variable, lead=bucket, point=point, n=len(gp), change=change, lo=lo, hi=hi))
    return pd.DataFrame(rows).assign(model=model, base=base)


def run(
    name: str,
    gold: pd.DataFrame,
    pairs: list[tuple[str, str]],
    targets: list[str],
    *,
    holdout_groups: list[list[str]] | None = None,
    meta: dict | None = None,
    out_dir=None,
    log: Callable[[str], None] = print,
) -> pd.DataFrame:
    """Score each (model, base) pair on rolling folds, or leave-stations-out if ``holdout_groups``."""
    names = sorted({backtest.BASELINE, *(m for pair in pairs for m in pair)})
    if holdout_groups is None:
        scored = backtest.run(gold, names, targets, log=log)
    else:
        scored = backtest.run_holdout(gold, names, targets, holdout_groups, log=log)
    scored = backtest.common_rows(scored)
    changes = pd.concat([paired_change(scored, m, b) for m, b in pairs], ignore_index=True)
    board = backtest.leaderboard(scored, groups={"original": backtest.ORIGINAL_STATIONS})
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        changes.to_csv(out_dir / "paired.csv", index=False)
        board.to_csv(out_dir / "leaderboard.csv", index=False)
        (out_dir / "meta.json").write_text(json.dumps({"name": name, **(meta or {})}, indent=2, default=str))
    return changes
