"""Explicit geographic holdout orchestration tests."""

from __future__ import annotations

import json

import click
import pandas as pd
import pytest
from typer.testing import CliRunner

from microcast import backtest, pipeline
from microcast.cli import app


def _gold() -> pd.DataFrame:
    init = pd.date_range("2026-06-01", "2026-10-02", freq="12h", tz="UTC")
    rows = []
    for point in ("REF1", "REF2", "NEIGH1", "NEIGH2"):
        for t in init:
            for lead in range(0, 7):
                rows.append(
                    {
                        "point_id": point,
                        "init_time": t,
                        "issue_time": t + pd.Timedelta(minutes=55),
                        "lead_min": lead * 60,
                        "valid_time": t + pd.Timedelta(hours=lead),
                        "nwp_t2m": 10.0,
                        "obs_t2m": 11.0,
                    }
                )
    return pd.DataFrame(rows)


def test_explicit_holdout_is_september_only_and_does_not_leak(monkeypatch, tmp_path):
    seen_fit: list[set[str]] = []
    seen_cutoff: list[pd.Timestamp] = []

    class Spy:
        def __init__(self, target):
            self.target = target

        def fit(self, train):
            seen_fit.append(set(train.point_id))
            seen_cutoff.append(train.valid_time.max())

        def predict(self, test):
            return pd.DataFrame({"mu": test.nwp_t2m, "sigma": 1.0}, index=test.index)

    monkeypatch.setattr(backtest.zoo, "make", lambda name, target: Spy(target))
    monkeypatch.setattr(pipeline, "load_gold", lambda models, log: (_gold(), {"gold": 1}))
    monkeypatch.setattr(pipeline, "reports_dir", lambda: tmp_path)

    pipeline.run_holdout(
        ["gbm_nwp"],
        ["t2m"],
        heldout_stations=["NEIGH1", "NEIGH2"],
        start="2026-06-01",
        end="2026-10-01",
        log=lambda _: None,
    )

    assert seen_fit and all(s == {"REF1", "REF2"} for s in seen_fit)
    assert all(t < pd.Timestamp("2026-09-01", tz="UTC") for t in seen_cutoff)
    scores = pd.read_parquet(tmp_path / "holdout" / "scores.parquet")
    assert set(scores.point_id) == {"NEIGH1", "NEIGH2"}
    assert scores.init_time.dt.month.eq(9).all()
    assert scores.lead_min.between(60, 360).all()
    assert not scores.valid_time.ge(pd.Timestamp("2026-10-01", tz="UTC")).any()
    keys = ["point_id", "init_time", "lead_min"]
    cases = [
        set(map(tuple, group[keys].itertuples(index=False, name=None))) for _, group in scores.groupby("model_name")
    ]
    assert len(cases) == 2 and cases[0] == cases[1]


def test_explicit_holdout_rejects_unknown_or_all_stations(monkeypatch):
    gold = _gold()
    monkeypatch.setattr(pipeline, "load_gold", lambda models, log: (gold, {"gold": 1}))
    with pytest.raises(ValueError, match="not present"):
        pipeline.run_holdout(["gbm_nwp"], ["t2m"], heldout_stations=["NOPE"], log=lambda _: None)
    with pytest.raises(ValueError, match="at least one training station"):
        pipeline.run_holdout(
            ["gbm_nwp"], ["t2m"], heldout_stations=["REF1", "REF2", "NEIGH1", "NEIGH2"], log=lambda _: None
        )


def test_explicit_holdout_rejects_blank_selection(monkeypatch):
    monkeypatch.setattr(pipeline, "load_gold", lambda models, log: (_gold(), {"gold": 1}))
    with pytest.raises(ValueError, match="at least one non-empty station ID"):
        pipeline.run_holdout(["gbm_nwp"], ["t2m"], heldout_stations=[" ", "\t"], log=lambda _: None)


def test_holdout_metadata_normalizes_ids_and_describes_group_training(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "load_gold", lambda models, log: (_gold(), {"gold": 1}))
    monkeypatch.setattr(pipeline, "reports_dir", lambda: tmp_path)

    class Spy:
        def __init__(self, target):
            self.target = target

        def fit(self, train):
            pass

        def predict(self, test):
            return pd.DataFrame({"mu": test.nwp_t2m, "sigma": 1.0}, index=test.index)

    monkeypatch.setattr(backtest.zoo, "make", lambda name, target: Spy(target))
    pipeline.run_holdout(["gbm_nwp"], ["t2m"], heldout_stations=[" NEIGH2", "NEIGH1", "NEIGH1 "], log=lambda _: None)
    meta = json.loads((tmp_path / "holdout" / "meta.json").read_text())
    assert meta["heldout_stations"] == ["NEIGH1", "NEIGH2"]
    assert meta["training_stations"] == ["REF1", "REF2"]
    assert meta["training_stations_by_group"]["0"] == ["REF1", "REF2"]


def test_grouped_holdout_metadata_does_not_claim_one_training_set(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "load_gold", lambda models, log: (_gold(), {"gold": 1}))
    monkeypatch.setattr(pipeline, "reports_dir", lambda: tmp_path)

    class Spy:
        def __init__(self, target):
            self.target = target

        def fit(self, train):
            pass

        def predict(self, test):
            return pd.DataFrame({"mu": test.nwp_t2m, "sigma": 1.0}, index=test.index)

    monkeypatch.setattr(backtest.zoo, "make", lambda name, target: Spy(target))
    pipeline.run_holdout(["gbm_nwp"], ["t2m"], n_groups=2, log=lambda _: None)
    meta = json.loads((tmp_path / "holdout" / "meta.json").read_text())
    assert meta["training_stations"] is None
    assert len(meta["training_stations_by_group"]) == 2


@pytest.mark.parametrize("selection", ["", " ", ",,,"])
def test_cli_rejects_blank_explicit_holdout_stations(monkeypatch, selection):
    runner = CliRunner()
    called = False

    def run(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("microcast.pipeline.run_holdout", run)
    result = runner.invoke(app, ["backtest", "--holdout", "--holdout-stations", selection])
    assert result.exit_code != 0
    assert "non-empty" in click.unstyle(result.stderr)  # CI forces Rich colour
    assert not called

    result = runner.invoke(app, ["backtest", "--holdout-stations", " "])
    assert result.exit_code != 0
    assert "requires --holdout" in click.unstyle(result.stderr)
    assert not called


def test_cli_dispatches_normalized_station_list_and_dates(monkeypatch):
    runner = CliRunner()
    seen = {}

    def run(models, targets, **kwargs):
        seen.update(models=models, targets=targets, **kwargs)
        return pd.DataFrame()

    monkeypatch.setattr("microcast.pipeline.run_holdout", run)
    monkeypatch.setattr("microcast.cli._print_board", lambda board: None)
    result = runner.invoke(
        app,
        [
            "backtest",
            "--holdout",
            "--models",
            "gbm_nwp",
            "--targets",
            "t2m",
            "--holdout-stations",
            " NEIGH2,NEIGH1 ",
            "--start",
            "2026-06-01",
            "--end",
            "2026-10-01",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["models"] == ["gbm_nwp"]
    assert seen["targets"] == ["t2m"]
    assert seen["heldout_stations"] == ["NEIGH2", "NEIGH1"]
    assert seen["start"].isoformat() == "2026-06-01T00:00:00+00:00"
    assert seen["end"].isoformat() == "2026-10-01T00:00:00+00:00"


def test_cli_default_holdout_keeps_station_selection_unset(monkeypatch):
    runner = CliRunner()
    seen = {}

    def run(models, targets, **kwargs):
        seen.update(kwargs)
        return pd.DataFrame()

    monkeypatch.setattr("microcast.pipeline.run_holdout", run)
    monkeypatch.setattr("microcast.cli._print_board", lambda board: None)
    result = runner.invoke(app, ["backtest", "--holdout"])
    assert result.exit_code == 0, result.output
    assert seen["heldout_stations"] is None
