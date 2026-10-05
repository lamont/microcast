"""Forecasters: every technique implements ``fit`` / ``predict`` on gold rows.

Each model predicts a Gaussian (``mu``, ``sigma``) for one target at each row,
so every model is scored with the same CRPS. Phase 1 has models 0, 1 and 3 of
the design's zoo:

* ``raw_hrrr``: HRRR's value; sigma from its historical RMS error per lead.
  The spread keeps the baseline fair: a point forecast's CRPS is its MAE,
  which would flatter any probabilistic challenger.
* ``bias_rolling``: HRRR plus its trailing 14-day mean error at that point and
  lead (an as-of feature in gold).
* ``gbm_residual``: LightGBM on the residual (obs - HRRR) from every gold
  feature, with sigma from held-out residual RMS per lead.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np
import pandas as pd

from microcast.lake.schemas import NWP_FIELDS, TARGETS


class Forecaster(Protocol):
    name: str
    target: str

    def fit(self, train: pd.DataFrame) -> None: ...

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        """Rows aligned with ``X``: columns ``mu`` and ``sigma``."""
        ...


def _rms_by_lead(lead: pd.Series, resid: pd.Series) -> pd.Series:
    return np.sqrt((resid**2).groupby(lead).mean())


def _sigma_for(leads: pd.Series, table: pd.Series, floor: float = 0.1) -> np.ndarray:
    fallback = float(table.median()) if len(table) else 1.0
    return np.maximum(leads.map(table).fillna(fallback).to_numpy(), floor)


class RawNWP:
    def __init__(self, target: str, model: str = "hrrr"):
        self.target, self.name = target, f"raw_{model}"
        self.sigma_by_lead = pd.Series(dtype="float64")

    def fit(self, train: pd.DataFrame) -> None:
        resid = train[f"obs_{self.target}"] - train[f"nwp_{self.target}"]
        self.sigma_by_lead = _rms_by_lead(train.lead_min, resid)

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        mu = X[f"nwp_{self.target}"].to_numpy()
        return pd.DataFrame({"mu": mu, "sigma": _sigma_for(X.lead_min, self.sigma_by_lead)}, index=X.index)


class BiasRolling:
    name = "bias_rolling"

    def __init__(self, target: str):
        self.target = target
        self.sigma_by_lead = pd.Series(dtype="float64")

    def _mu(self, X: pd.DataFrame) -> pd.Series:
        return X[f"nwp_{self.target}"] + X[f"bias14_{self.target}"].fillna(0.0)

    def fit(self, train: pd.DataFrame) -> None:
        self.sigma_by_lead = _rms_by_lead(train.lead_min, train[f"obs_{self.target}"] - self._mu(train))

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame(
            {"mu": self._mu(X).to_numpy(), "sigma": _sigma_for(X.lead_min, self.sigma_by_lead)}, index=X.index
        )


def feature_columns() -> list[str]:
    cols = [f"nwp_{f}" for f in NWP_FIELDS] + ["hour_sin", "hour_cos", "doy_sin", "doy_cos", "lead_min"]
    cols += [f"{p}_{t}" for t in TARGETS for p in ("obs_last", "err_at_init", "bias14", "std14")]
    return cols + ["point"]


class GBMResidual:
    name = "gbm_residual"
    params = dict(
        objective="regression",
        learning_rate=0.03,
        num_leaves=31,
        min_data_in_leaf=50,
        feature_fraction=0.8,
        bagging_fraction=0.8,
        bagging_freq=1,
        lambda_l2=1.0,
        verbose=-1,
        seed=7,
    )

    def __init__(self, target: str, valid_frac: float = 0.15, max_rounds: int = 2000):
        self.target, self.valid_frac, self.max_rounds = target, valid_frac, max_rounds
        self.points: list[str] = []
        self.booster = None
        self.sigma_by_lead = pd.Series(dtype="float64")

    def _X(self, df: pd.DataFrame) -> pd.DataFrame:
        X = df.reindex(columns=feature_columns()).copy()
        X["point"] = pd.Categorical(df.point_id, categories=self.points)
        return X

    def fit(self, train: pd.DataFrame) -> None:
        import lightgbm as lgb

        train = train.sort_values("valid_time")
        self.points = sorted(train.point_id.unique())
        y = train[f"obs_{self.target}"] - train[f"nwp_{self.target}"]
        cut = int(len(train) * (1 - self.valid_frac))  # time-ordered holdout, never shuffled
        dtrain = lgb.Dataset(self._X(train.iloc[:cut]), y.iloc[:cut])
        dvalid = lgb.Dataset(self._X(train.iloc[cut:]), y.iloc[cut:], reference=dtrain)
        self.booster = lgb.train(
            self.params,
            dtrain,
            num_boost_round=self.max_rounds,
            valid_sets=[dvalid],
            callbacks=[lgb.early_stopping(100, verbose=False)],
        )
        held = train.iloc[cut:]
        resid = y.iloc[cut:] - self.booster.predict(self._X(held), num_iteration=self.booster.best_iteration)
        self.sigma_by_lead = _rms_by_lead(held.lead_min, resid)

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        delta = self.booster.predict(self._X(X), num_iteration=self.booster.best_iteration)
        mu = X[f"nwp_{self.target}"].to_numpy() + delta
        return pd.DataFrame({"mu": mu, "sigma": _sigma_for(X.lead_min, self.sigma_by_lead)}, index=X.index)


MODELS = {"raw_hrrr": RawNWP, "bias_rolling": BiasRolling, "gbm_residual": GBMResidual}


def make(name: str, target: str) -> Forecaster:
    return MODELS[name](target)
