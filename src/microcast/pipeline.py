"""Backtest orchestration: gold -> models -> gold.forecasts/scores, MLflow, reports.

Kept out of ``backtest`` so the scoring maths stays free of I/O. Each
(model, target) is one MLflow run in the target's experiment; the run records
the gold snapshot it read, and that snapshot is tagged ``mlflow-<run_id>`` so
expiry never removes it (docs/decisions.md D1).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pandas as pd
from pyiceberg.expressions import EqualTo

from microcast import backtest, settings
from microcast.lake.catalog import get_catalog, replace_partition, tag_snapshot


def reports_dir() -> Path:
    path = settings.data_dir() / "reports"
    path.mkdir(exist_ok=True)
    return path


def _mlflow():
    import mlflow

    if not os.environ.get("MLFLOW_TRACKING_URI"):
        mlflow.set_tracking_uri(f"sqlite:///{settings.data_dir() / 'mlflow.db'}")
    return mlflow


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=settings.REPO_ROOT
        )
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=settings.REPO_ROOT)
        return out.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")
    except OSError:
        return "unknown"


def run_backtests(
    model_names: list[str], targets: list[str], *, log: Callable[[str], None] = print
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Backtest, store forecasts and scores, log to MLflow; returns (leaderboard, monthly)."""
    mlflow = _mlflow()
    table = get_catalog().load_table("gold.training_examples")
    snapshot_id = table.current_snapshot().snapshot_id
    gold = table.scan(snapshot_id=snapshot_id).to_pandas()
    log(f"gold.training_examples @ {snapshot_id}: {len(gold)} rows")
    if backtest.BASELINE not in model_names:
        model_names = [backtest.BASELINE, *model_names]

    scored, run_ids = [], {}
    for target in targets:
        mlflow.set_experiment(target)
        for name in model_names:
            with mlflow.start_run(run_name=f"{name}-backtest") as r:
                run_ids[name, target] = r.info.run_id
                mlflow.set_tags({"git_sha": _git_sha(), "kind": "backtest"})
                mlflow.log_params(
                    {
                        "model": name,
                        "target": target,
                        "gold_snapshot_id": snapshot_id,
                        "rows": len(gold),
                        "stations": ",".join(sorted(gold.point_id.unique())),
                        "valid_from": str(gold.valid_time.min()),
                        "valid_to": str(gold.valid_time.max()),
                    }
                )
                part = backtest.run(gold, [name], [target], run_ids=run_ids, log=log)
                scored.append(part)
                tag_snapshot("gold.training_examples", f"mlflow-{r.info.run_id}", snapshot_id)
    scored = pd.concat(scored, ignore_index=True)

    forecasts, scores = backtest.to_tables(scored)
    replace_partition("gold.forecasts", forecasts, EqualTo("kind", "backtest"))
    replace_partition("gold.scores", scores, EqualTo("kind", "backtest"))
    log(f"gold.forecasts / gold.scores: {len(scored)} backtest rows")

    board = backtest.leaderboard(scored, groups={"original": backtest.ORIGINAL_STATIONS})
    monthly = backtest.monthly_skill(scored)
    for (name, target), run_id in run_ids.items():
        rows = board[(board.model == name) & (board.variable == target) & (board.point == "all")]
        mlflow.set_experiment(target)
        with mlflow.start_run(run_id=run_id):
            for _, row in rows.iterrows():
                tag = row.lead.replace(" ", "").replace("-", "_")
                mlflow.log_metrics(
                    {
                        f"crps_{tag}": row.crps,
                        f"mae_{tag}": row.mae,
                        f"skill_{tag}": row.skill,
                        f"skill_lo_{tag}": row.skill_lo,
                        f"coverage80_{tag}": row.coverage_80,
                    }
                )
    write_reports(board, monthly, snapshot_id, run_ids)
    return board, monthly


def write_reports(board: pd.DataFrame, monthly: pd.DataFrame, snapshot_id: int, run_ids: dict) -> Path:
    out = reports_dir()
    board.to_csv(out / "leaderboard.csv", index=False)
    monthly.to_csv(out / "monthly_skill.csv", index=False)
    meta = {
        "gold_snapshot_id": snapshot_id,
        "git_sha": _git_sha(),
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "runs": {f"{m}/{t}": r for (m, t), r in run_ids.items()},
    }
    (out / "backtest_meta.json").write_text(json.dumps(meta, indent=2))
    return out
