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
* ``gbm_purpleair``: the same model and settings plus the PurpleAir transect
  features (``pa_*`` columns from gold.network_features). The pair is an
  ablation: any difference in skill is what the transect adds.
* ``gbm_terrain``: ``gbm_residual`` with the point's terrain (``st_*`` columns
  from silver.static_features) in place of its identity, so what it learns
  carries to points it never saw.
* ``gbm_nwp`` / ``gbm_nwp_terrain``: HRRR, time and lead only, without and
  with terrain; no obs from the point itself. The forecast for a place
  with no station (the ride, the house today), scored on held-out stations.
* ``gbm_nwp_neighbours`` / ``gbm_terrain_neighbours``: those with the nearest
  other stations' errors at init and rolling bias (``nb*`` columns,
  ``transform.neighbours``) added: without and with the point's own obs.
* ``gbm_terrain_regime`` / ``gbm_nwp_terrain_regime``: ``gbm_terrain`` and
  ``gbm_nwp_terrain`` plus the marine-layer regime (``regime_columns``): HRRR's
  925 and 850 mb temperature less its 2 m temperature (the inversion), its
  925 mb wind and its PBL height. Ablations: the pair's difference is what the
  regime adds, with and without the point's own obs.
* ``gbm_terrain_lagged``: ``gbm_terrain`` plus the time-lagged HRRR ensemble
  for its target (``lag_*``: the earlier cycles' forecasts for the same hour,
  their spread and the run-to-run change). ``gbm_terrain_lagged_sigma`` also
  makes sigma grow with the lagged spread (``SpreadSigma``), so the interval
  widens when successive runs disagree.
* ``gbm_terrain_upwind`` / ``gbm_nwp_terrain_upwind``: ``gbm_terrain`` and
  ``gbm_nwp_terrain`` plus the marine push as the MADIS stations see it
  (``up_*`` columns from gold.upwind_features): coast, ridge and east-side
  changes and anomalies, and minutes since the coast's push. With and
  without the point's own obs.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np
import pandas as pd

from microcast.lake.schemas import LAG_FIELDS, NWP_FIELDS, REGIME_FIELDS, TARGETS


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


def nwp_columns() -> list[str]:
    return [f"nwp_{f}" for f in NWP_FIELDS] + ["hour_sin", "hour_cos", "doy_sin", "doy_cos", "lead_min"]


REGIME_DERIVED = ["inv925", "inv850", "wind925", "onshore925"]
GATE_DIR = 270.0  # the Golden Gate's axis, roughly: wind from the west is onshore


def regime_columns() -> list[str]:
    return [f"nwp_{f}" for f in REGIME_FIELDS] + REGIME_DERIVED


def with_regime(df: pd.DataFrame) -> pd.DataFrame:
    """Derived regime inputs: inversion strength over the point, and the flow at 925 mb."""
    out = df.copy()
    out["inv925"] = df.nwp_t925 - df.nwp_t2m  # > 0: warmer aloft, a capped marine layer
    out["inv850"] = df.nwp_t850 - df.nwp_t2m
    out["wind925"] = np.hypot(df.nwp_u925, df.nwp_v925)
    # Component of the 925 mb wind blowing from GATE_DIR (onshore through the Gate), m/s.
    out["onshore925"] = -(df.nwp_u925 * np.sin(np.radians(GATE_DIR)) + df.nwp_v925 * np.cos(np.radians(GATE_DIR)))
    return out


def feature_columns() -> list[str]:
    cols = nwp_columns()
    cols += [f"{p}_{t}" for t in TARGETS for p in ("obs_last", "err_at_init", "bias14", "std14")]
    return cols + ["point"]


class GBMResidual:
    name = "gbm_residual"
    needs: tuple[str, ...] = ()  # extra inputs the pipeline joins onto gold: "network", "static"
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
        self.cols: list[str] = []
        self.booster = None
        self.sigma_by_lead = pd.Series(dtype="float64")

    def columns(self, df: pd.DataFrame) -> list[str]:
        return feature_columns()

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Columns derived from gold before training and prediction; none by default."""
        return df

    def _X(self, df: pd.DataFrame) -> pd.DataFrame:
        X = self.prepare(df).reindex(columns=self.cols).copy()
        if "point" in self.cols:
            X["point"] = pd.Categorical(df.point_id, categories=self.points)
        return X

    def fit(self, train: pd.DataFrame) -> None:
        import lightgbm as lgb

        train = train.sort_values("valid_time")
        self.points = sorted(train.point_id.unique())
        self.cols = self.columns(train)
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

    def predict_mu(self, X: pd.DataFrame) -> np.ndarray:
        delta = self.booster.predict(self._X(X), num_iteration=self.booster.best_iteration)
        return X[f"nwp_{self.target}"].to_numpy() + delta

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        mu = self.predict_mu(X)
        return pd.DataFrame({"mu": mu, "sigma": _sigma_for(X.lead_min, self.sigma_by_lead)}, index=X.index)


class GBMPurpleAir(GBMResidual):
    name = "gbm_purpleair"
    needs = ("network",)

    def columns(self, df: pd.DataFrame) -> list[str]:
        pa_cols = sorted(c for c in df.columns if c.startswith("pa_"))
        if not pa_cols:
            raise ValueError("gbm_purpleair needs the pa_* network features joined onto gold")
        return [*feature_columns()[:-1], *pa_cols, "point"]


def _static_cols(df: pd.DataFrame) -> list[str]:
    cols = sorted(c for c in df.columns if c.startswith("st_"))
    if not cols:
        raise ValueError("terrain models need the st_* static features joined onto gold")
    return cols


class GBMTerrain(GBMResidual):
    name = "gbm_terrain"
    needs = ("static",)

    def columns(self, df: pd.DataFrame) -> list[str]:
        return [*feature_columns()[:-1], *_static_cols(df)]


class GBMNWP(GBMResidual):
    name = "gbm_nwp"

    def columns(self, df: pd.DataFrame) -> list[str]:
        return nwp_columns()


class GBMNWPTerrain(GBMResidual):
    name = "gbm_nwp_terrain"
    needs = ("static",)

    def columns(self, df: pd.DataFrame) -> list[str]:
        return [*nwp_columns(), *_static_cols(df)]


class GBMNWPNeighbours(GBMResidual):
    name = "gbm_nwp_neighbours"
    needs = ("static", "neighbours")

    def columns(self, df: pd.DataFrame) -> list[str]:
        from microcast.transform import neighbours

        return [*nwp_columns(), *_static_cols(df), *neighbours.columns(df)]


class GBMTerrainNeighbours(GBMResidual):
    name = "gbm_terrain_neighbours"
    needs = ("static", "neighbours")

    def columns(self, df: pd.DataFrame) -> list[str]:
        from microcast.transform import neighbours

        return [*feature_columns()[:-1], *_static_cols(df), *neighbours.columns(df)]


class GBMTerrainRegime(GBMTerrain):
    name = "gbm_terrain_regime"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return with_regime(df)

    def columns(self, df: pd.DataFrame) -> list[str]:
        return [*super().columns(df), *regime_columns()]


class GBMNWPTerrainRegime(GBMNWPTerrain):
    name = "gbm_nwp_terrain_regime"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return with_regime(df)

    def columns(self, df: pd.DataFrame) -> list[str]:
        return [*super().columns(df), *regime_columns()]


class GBMTerrainUpwind(GBMTerrain):
    name = "gbm_terrain_upwind"
    needs = ("static", "upwind")

    def columns(self, df: pd.DataFrame) -> list[str]:
        from microcast.transform import upwind

        return [*super().columns(df), *upwind.columns(df)]


class GBMNWPTerrainUpwind(GBMNWPTerrain):
    name = "gbm_nwp_terrain_upwind"
    needs = ("static", "upwind")

    def columns(self, df: pd.DataFrame) -> list[str]:
        from microcast.transform import upwind

        return [*super().columns(df), *upwind.columns(df)]


def lag_columns(target: str) -> list[str]:
    return [f for f in LAG_FIELDS if f.endswith(f"_{target}")]


class GBMTerrainLagged(GBMTerrain):
    name = "gbm_terrain_lagged"

    def columns(self, df: pd.DataFrame) -> list[str]:
        return [*super().columns(df), *lag_columns(self.target)]


class SpreadSigma:
    """sigma^2 = a + b * spread^2 per lead, fit by least squares on squared held-out residuals.

    The classic spread-skill relation: where the members disagree, the error
    is larger. ``b`` is kept >= 0 and sigma floored; without a spread a row
    falls back to its lead's plain RMS.
    """

    def __init__(self):
        self.coef: dict[int, tuple[float, float]] = {}
        self.rms = pd.Series(dtype="float64")

    def fit(self, lead: pd.Series, spread: pd.Series, resid: pd.Series) -> None:
        self.rms = _rms_by_lead(lead, resid)
        for m, g in pd.DataFrame({"lead": lead, "s2": spread**2, "r2": resid**2}).dropna().groupby("lead"):
            if len(g) < 100 or g.s2.var() == 0:
                continue
            b = max(float(np.cov(g.s2, g.r2)[0, 1] / g.s2.var()), 0.0)
            a = max(float(g.r2.mean() - b * g.s2.mean()), 0.01)
            self.coef[int(m)] = (a, b)

    def predict(self, lead: pd.Series, spread: pd.Series) -> np.ndarray:
        base = _sigma_for(lead, self.rms)
        a = lead.map({m: c[0] for m, c in self.coef.items()})
        b = lead.map({m: c[1] for m, c in self.coef.items()})
        scaled = np.sqrt(a + b * spread.fillna(0.0) ** 2).to_numpy()
        return np.maximum(np.where(np.isfinite(scaled) & spread.notna().to_numpy(), scaled, base), 0.1)


class LaggedSigma:
    """Mixin: sigma from the lagged runs' spread (``SpreadSigma``), fitted on the model's held-out slice."""

    def fit(self, train: pd.DataFrame) -> None:
        super().fit(train)
        train = train.sort_values("valid_time")
        held = train.iloc[int(len(train) * (1 - self.valid_frac)) :]
        resid = held[f"obs_{self.target}"] - self.predict_mu(held)
        self.sigma_model = SpreadSigma()
        self.sigma_model.fit(held.lead_min, held[f"lag_sd_{self.target}"], resid)

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        sigma = self.sigma_model.predict(X.lead_min, X[f"lag_sd_{self.target}"])
        return pd.DataFrame({"mu": self.predict_mu(X), "sigma": sigma}, index=X.index)


class GBMTerrainLaggedSigma(LaggedSigma, GBMTerrainLagged):
    name = "gbm_terrain_lagged_sigma"


class GBMTerrainRegimeUpwind(GBMTerrainUpwind):
    name = "gbm_terrain_regime_upwind"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return with_regime(df)

    def columns(self, df: pd.DataFrame) -> list[str]:
        return [*super().columns(df), *regime_columns()]


class GBMTerrainStack(LaggedSigma, GBMTerrainRegimeUpwind):
    """Everything adopted 2026-10-18 for points with their own obs: terrain, regime, upwind, lagged runs."""

    name = "gbm_terrain_stack"

    def columns(self, df: pd.DataFrame) -> list[str]:
        return [*super().columns(df), *lag_columns(self.target)]


MODELS = {
    "raw_hrrr": RawNWP,
    "bias_rolling": BiasRolling,
    "gbm_residual": GBMResidual,
    "gbm_purpleair": GBMPurpleAir,
    "gbm_terrain": GBMTerrain,
    "gbm_nwp": GBMNWP,
    "gbm_nwp_terrain": GBMNWPTerrain,
    "gbm_nwp_neighbours": GBMNWPNeighbours,
    "gbm_terrain_neighbours": GBMTerrainNeighbours,
    "gbm_terrain_regime": GBMTerrainRegime,
    "gbm_nwp_terrain_regime": GBMNWPTerrainRegime,
    "gbm_terrain_lagged": GBMTerrainLagged,
    "gbm_terrain_lagged_sigma": GBMTerrainLaggedSigma,
    "gbm_terrain_upwind": GBMTerrainUpwind,
    "gbm_nwp_terrain_upwind": GBMNWPTerrainUpwind,
    "gbm_terrain_regime_upwind": GBMTerrainRegimeUpwind,
    "gbm_terrain_stack": GBMTerrainStack,
}


def make(name: str, target: str) -> Forecaster:
    return MODELS[name](target)
