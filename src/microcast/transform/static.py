"""Static terrain and exposure features per point -> silver.static_features.

Computed once per point from the 3DEP 10 m grid (``ingest.dem``) and HRRR's own
terrain height, for every registry point and every station in bronze.stations.
Long like gold.network_features: one row per (point, feature).

Features (``FEATURES``; bearings are where the wind comes from):

* ``elev_m``: ground elevation, water clipped to 0 (piers and the bridge sit over
  bathymetry).

Water is ``elevation <= 0`` (3DEP carries bathymetry), plus flat cells below
2 m: off Marin, outside the topobathy, the lidar saw the sea surface at
+1-2 m.
* ``hrrr_hgt_m``, ``elev_minus_hrrr_m``: HRRR's terrain at the point (the same
  inverse-distance weighting of 4 cells as silver) and how far the real ground
  is above it: the lapse-rate correction HRRR can't make itself.
* ``slope_deg``, ``aspect_sin``, ``aspect_cos``: from the grid smoothed to 90 m.
* ``tpi_300_m``, ``tpi_2000_m``: elevation minus the mean around it, ridge (+)
  or hollow (-).
* ``dist_water_m``: to the nearest water (ocean or bay).
* ``fetch_250_m``, ``fetch_290_m``: distance over land upwind to open water,
  capped at 20 km: how long marine air has been over land.
* ``sx_250_1km``, ``sx_250_5km``, ``sx_070_5km``: Winstral's shelter index, the
  steepest upwind horizon angle (degrees) within that distance, averaged over a
  +-30 degree sector. Positive is sheltered. 250 is the sea breeze through the
  Golden Gate, 070 the offshore wind.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd
import pyarrow as pa
from pyiceberg.expressions import AlwaysTrue
from pyproj import Transformer

from microcast.ingest.dem import Dem
from microcast.lake.schemas import STATIC_FEATURES_SCHEMA

_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32610", always_xy=True)

FEATURES = [
    "elev_m",
    "hrrr_hgt_m",
    "elev_minus_hrrr_m",
    "slope_deg",
    "aspect_sin",
    "aspect_cos",
    "tpi_300_m",
    "tpi_2000_m",
    "dist_water_m",
    "fetch_250_m",
    "fetch_290_m",
    "sx_250_1km",
    "sx_250_5km",
    "sx_070_5km",
]
WATER_FLAT_MAX_M = 2.0
WATER_ROUGHNESS_M = 0.15  # std over 200 m: land (runways included) is metres, open water centimetres
SENSOR_HEIGHT_M = 3.0  # the shelter index is measured from just above the ground
FETCH_CAP_M = 20_000.0


def _sample(dem: Dem, grid: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    from scipy.ndimage import map_coordinates

    r, c = dem.rc(x, y)
    return map_coordinates(grid, [r, c], order=1, mode="nearest")


def _ray(x: float, y: float, bearing: float, dists: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Points along a bearing (degrees from north; UTM grid north is within 1 degree of true here)."""
    b = np.radians(bearing)
    return x + np.sin(b) * dists, y + np.cos(b) * dists


def shelter(dem: Dem, ground: np.ndarray, x: float, y: float, z0: float, bearing: float, dmax: float) -> float:
    """Winstral Sx: mean over a +-30 degree sector of the max upwind horizon angle."""
    dists = np.arange(dem.cell * 2, dmax + dem.cell, dem.cell * 2)
    angles = []
    for b in bearing + np.arange(-30, 31, 10):
        z = _sample(dem, ground, *_ray(x, y, b, dists))
        angles.append(np.degrees(np.arctan((z - z0) / dists)).max())
    return float(np.mean(angles))


def fetch(dem: Dem, water: np.ndarray, x: float, y: float, bearing: float) -> float:
    """Distance upwind to the first water cell, capped at ``FETCH_CAP_M``."""
    dists = np.arange(0.0, FETCH_CAP_M + dem.cell, dem.cell)
    wet = _sample(dem, water.astype("float32"), *_ray(x, y, bearing, dists)) >= 0.5
    return float(dists[np.argmax(wet)]) if wet.any() else FETCH_CAP_M


def _box_mean(a: np.ndarray, radius_m: float, cell: float) -> np.ndarray:
    from scipy.ndimage import uniform_filter

    return uniform_filter(a, size=2 * round(radius_m / cell) + 1, mode="nearest")


def water_mask(z: np.ndarray, cell: float) -> np.ndarray:
    m = _box_mean(z, 100, cell)
    rough = np.sqrt(np.maximum(_box_mean(z * z, 100, cell) - m * m, 0.0))
    return (z <= 0.0) | ((z <= WATER_FLAT_MAX_M) & (rough < WATER_ROUGHNESS_M))


def features(dem: Dem, points: pd.DataFrame, hrrr_hgt: Iterable[float]) -> pd.DataFrame:
    """``points``: point_id, lat, lon. Returns one row per point, a column per feature."""
    from scipy.ndimage import distance_transform_edt

    ground = np.maximum(dem.z, 0.0)
    water = water_mask(dem.z, dem.cell)
    smooth = _box_mean(ground, 40, dem.cell)  # 90 m box
    gy, gx = np.gradient(smooth, dem.cell)  # rows run south, so gy is -dz/dnorth
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    aspect = np.degrees(np.arctan2(-gx, gy)) % 360  # direction the slope faces
    tpi = {r: ground - _box_mean(ground, r, dem.cell) for r in (300, 2000)}
    dist_water = distance_transform_edt(~water) * dem.cell

    x, y = (np.asarray(v) for v in _TO_UTM.transform(points.lon.to_numpy(), points.lat.to_numpy()))
    elev = _sample(dem, ground, x, y)
    asp = np.radians(_sample(dem, aspect, x, y))
    out = pd.DataFrame(
        {
            "point_id": points.point_id.to_numpy(),
            "elev_m": elev,
            "hrrr_hgt_m": np.asarray(list(hrrr_hgt), dtype="float64"),
            "slope_deg": _sample(dem, slope, x, y),
            "aspect_sin": np.sin(asp),
            "aspect_cos": np.cos(asp),
            "tpi_300_m": _sample(dem, tpi[300], x, y),
            "tpi_2000_m": _sample(dem, tpi[2000], x, y),
            "dist_water_m": _sample(dem, dist_water, x, y),
        }
    )
    out["elev_minus_hrrr_m"] = out.elev_m - out.hrrr_hgt_m
    z0 = elev + SENSOR_HEIGHT_M
    for b in (250, 290):
        out[f"fetch_{b}_m"] = [fetch(dem, water, xi, yi, b) for xi, yi in zip(x, y, strict=True)]
    for b, dmax in ((250, 1000), (250, 5000), (70, 5000)):
        out[f"sx_{b:03d}_{dmax // 1000}km"] = [
            shelter(dem, ground, xi, yi, zi, b, dmax) for xi, yi, zi in zip(x, y, z0, strict=True)
        ]
    return out[["point_id", *FEATURES]]


def hrrr_terrain(points: pd.DataFrame) -> np.ndarray:
    """HRRR's surface height at each point: the 4 nearest cells, inverse-distance weighted as in silver."""
    from microcast.ingest import hrrr_zarr
    from microcast.registry import VirtualPoint

    url = f"{hrrr_zarr._cycle_url(pd.Timestamp('2025-07-15T00:00Z').to_pydatetime(), 'anl')}/surface/HGT/surface/HGT"
    hgt = hrrr_zarr._read_window(url, with_time=False)[0]
    r0, _, c0, _ = hrrr_zarr.window()
    vps = [VirtualPoint(p.point_id, p.point_id, 0, p.lat, p.lon, None) for p in points.itertuples()]
    cells = pd.DataFrame(
        [(n.point_id, hgt[n.row - r0, n.col - c0], n.dist_m) for n in hrrr_zarr.point_neighbours(vps)],
        columns=["point_id", "h", "dist_m"],
    )
    cells["w"] = 1.0 / cells.dist_m.clip(lower=100.0)
    cells["hw"] = cells.h * cells.w
    agg = cells.groupby("point_id")[["hw", "w"]].sum()
    return (agg.hw / agg.w).reindex(points.point_id).to_numpy()


def to_long(wide: pd.DataFrame) -> pa.Table:
    long = wide.melt(id_vars="point_id", var_name="feature", value_name="value")
    return pa.Table.from_pandas(long, preserve_index=False).cast(STATIC_FEATURES_SCHEMA.as_arrow())


def wide(long: pa.Table) -> pd.DataFrame:
    """One row per point_id, one column per feature."""
    return long.to_pandas().pivot(index="point_id", columns="feature", values="value").reset_index()


def rebuild(points: pd.DataFrame) -> pd.DataFrame:
    """Recompute silver.static_features in full; returns the wide table."""
    from microcast.ingest import dem
    from microcast.lake.catalog import replace_partition

    points = points.drop_duplicates("point_id").reset_index(drop=True)
    out = features(dem.load(), points, hrrr_terrain(points))
    replace_partition("silver.static_features", to_long(out), AlwaysTrue())
    return out
