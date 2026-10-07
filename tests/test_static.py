"""Terrain features on a synthetic grid, and the leave-stations-out split."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from microcast import backtest
from microcast.ingest.dem import Dem
from microcast.transform import static

CELL = 10.0
X0, Y1 = 500_000.0, 4_200_000.0


def _dem(z: np.ndarray) -> Dem:
    return Dem(z.astype("float32"), X0, Y1, CELL)


def _xy(row: float, col: float) -> tuple[float, float]:
    return X0 + (col + 0.5) * CELL, Y1 - (row + 0.5) * CELL


def test_shelter_positive_behind_a_ridge_and_negative_on_top():
    # Ocean (z = -5) west of column 100, flat land at 10 m, a 100 m ridge at columns 150-160.
    z = np.full((300, 400), 10.0)
    z[:, :100] = -5.0
    z[:, 150:160] = 100.0
    dem = _dem(z)
    ground = np.maximum(dem.z, 0)
    lee = _xy(150, 200)  # 400 m east of the ridge, wind from the west (270)
    sx_lee = static.shelter(dem, ground, *lee, 10 + 3, 270, 1000)
    assert sx_lee > 5
    top = _xy(150, 155)
    assert static.shelter(dem, ground, *top, 100 + 3, 270, 1000) < 0
    # Upwind fetch from the lee point back to the water: 100 columns, 1 km.
    water = static.water_mask(dem.z, CELL)
    assert static.fetch(dem, water, *lee, 270) == pytest.approx(1000, abs=2 * CELL)
    assert static.fetch(dem, water, *lee, 90) == static.FETCH_CAP_M


def test_water_mask_takes_flat_sea_surface_but_not_land():
    rng = np.random.default_rng(0)
    z = 1.5 + rng.normal(0, 0.02, (300, 300))  # sea surface at +1.5 m, as off Marin
    z[:, 150:] = 1.5 + np.abs(rng.normal(0, 3.0, (300, 150)))  # rough land, never below the sea
    water = static.water_mask(z.astype("float32"), CELL)
    assert water[:, :40].all()
    assert not water[:, 260:].any()


def test_features_have_every_column_and_elevation_clipped():
    z = np.full((300, 300), 50.0)
    z[:, :100] = -20.0
    pts = pd.DataFrame({"point_id": ["sea", "land"], "lat": [0.0, 0.0], "lon": [0.0, 0.0]})
    dem = _dem(z)
    # Place the points by UTM, then back to lat/lon for the public API.
    from pyproj import Transformer

    inv = Transformer.from_crs("EPSG:32610", "EPSG:4326", always_xy=True)
    for i, (r, c) in enumerate([(150, 50), (150, 200)]):
        pts.loc[i, "lon"], pts.loc[i, "lat"] = inv.transform(*_xy(r, c))
    out = static.features(dem, pts, [0.0, 30.0]).set_index("point_id")
    assert list(out.columns) == static.FEATURES
    assert out.loc["sea", "elev_m"] == 0 and out.loc["sea", "dist_water_m"] == 0
    assert out.loc["land", "elev_m"] == pytest.approx(50)
    assert out.loc["land", "elev_minus_hrrr_m"] == pytest.approx(20)
    assert out.loc["land", "fetch_250_m"] > 900


def test_station_groups_partition_the_stations():
    stations = [f"S{i}" for i in range(20)]
    groups = backtest.station_groups(stations, 6)
    assert len(groups) == 6 and sorted(s for g in groups for s in g) == sorted(stations)
    assert {len(g) for g in groups} <= {3, 4}
    assert groups == backtest.station_groups(list(reversed(stations)), 6)


def test_holdout_never_trains_on_the_tested_station(monkeypatch):
    seen: list[set[str]] = []

    class Spy:
        name, target = "spy", "t2m"

        def fit(self, train):
            seen.append(set(train.point_id))

        def predict(self, X):
            return pd.DataFrame({"mu": X.nwp_t2m, "sigma": 1.0}, index=X.index)

    monkeypatch.setattr(backtest.zoo, "make", lambda name, target: Spy())
    init = pd.date_range("2025-04-01", "2025-12-31", freq="6h", tz="UTC")
    rows = []
    for p in ("A", "B", "C"):
        for t in init:
            rows.append((p, t, 60, t + pd.Timedelta(hours=1), 10.0, 11.0))
    gold = pd.DataFrame(rows, columns=["point_id", "init_time", "lead_min", "valid_time", "nwp_t2m", "obs_t2m"])
    gold["issue_time"] = gold.init_time + pd.Timedelta(minutes=55)
    scored = backtest.run_holdout(gold, ["spy"], ["t2m"], [["A"], ["B", "C"]], log=lambda _: None)
    assert seen and all(s in ({"B", "C"}, {"A"}) for s in seen)
    for fold, g in scored.groupby("fold"):
        held = {"A"} if fold.endswith("g0") else {"B", "C"}
        assert set(g.point_id) == held
