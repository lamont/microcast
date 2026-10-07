"""USGS 3DEP elevation over the kept HRRR window, as one 10 m grid in UTM 10N.

The 3DEP image service resamples its best available source (1 m lidar over
San Francisco) to whatever grid is asked for. The window is fetched as 10 km
tiles (a whole-window request times out at the gateway) and stitched; each
tile's box is on the 10 m grid, so the pixels line up. Water carries
bathymetry (the bay and the ocean are negative), which makes
``elevation <= 0`` a usable water mask.

The grid is kept at ``data/terrain/dem_utm10_10m.npz``: fetched once, read
from disk after that.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from io import BytesIO

import numpy as np
import requests
from pyproj import Transformer

from microcast import settings

EXPORT_URL = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
CELL_M = 10.0
# UTM 10N box: the HRRR window (ingest.hrrr_zarr.WINDOW_BOUNDS) plus ~3 km, so the
# upwind scans from points near its edges still see terrain.
BOX = (524_000.0, 578_000.0, 4_153_000.0, 4_203_000.0)  # x0, x1, y0, y1
TILE_M = 10_000.0

_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32610", always_xy=True)


@dataclass(frozen=True)
class Dem:
    z: np.ndarray  # (rows, cols) metres; row 0 is the north edge
    x0: float  # west edge of column 0
    y1: float  # north edge of row 0
    cell: float = CELL_M

    def rc(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Fractional (row, col) of UTM coordinates, cell centres at integers."""
        return (self.y1 - y) / self.cell - 0.5, (x - self.x0) / self.cell - 0.5

    def at(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        """Bilinear elevation at lat/lon."""
        from scipy.ndimage import map_coordinates

        x, y = _TO_UTM.transform(np.asarray(lon), np.asarray(lat))
        r, c = self.rc(np.asarray(x), np.asarray(y))
        return map_coordinates(self.z, [r, c], order=1, mode="nearest")


def _fetch(box: tuple[float, float, float, float], cell: float) -> np.ndarray:
    import tifffile

    x0, x1, y0, y1 = box
    w, h = round((x1 - x0) / cell), round((y1 - y0) / cell)
    params = {
        "bbox": f"{x0},{y0},{x1},{y1}",
        "bboxSR": 32610,
        "imageSR": 32610,
        "size": f"{w},{h}",
        "format": "tiff",
        "pixelType": "F32",
        "interpolation": "RSP_BilinearInterpolation",
        "compression": "None",
        "f": "image",
    }
    r = requests.get(EXPORT_URL, params=params, timeout=300)
    r.raise_for_status()
    z = tifffile.imread(BytesIO(r.content)).astype("float32")
    if z.shape != (h, w):
        raise ValueError(f"3DEP returned {z.shape}, asked for {(h, w)}")
    return z


def _fetch_tiled(box: tuple[float, float, float, float], cell: float, workers: int = 4) -> np.ndarray:
    from concurrent.futures import ThreadPoolExecutor

    x0, x1, y0, y1 = box
    z = np.full((round((y1 - y0) / cell), round((x1 - x0) / cell)), np.nan, dtype="float32")
    tiles = [
        (tx, min(tx + TILE_M, x1), ty, min(ty + TILE_M, y1))
        for tx in np.arange(x0, x1, TILE_M)
        for ty in np.arange(y0, y1, TILE_M)
    ]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for t, block in zip(tiles, pool.map(lambda t: _fetch(t, cell), tiles), strict=True):
            r0, c0 = round((y1 - t[3]) / cell), round((t[0] - x0) / cell)
            z[r0 : r0 + block.shape[0], c0 : c0 + block.shape[1]] = block
    if np.isnan(z).any():
        raise ValueError("3DEP tiles left gaps in the grid")
    return z


@cache
def load() -> Dem:
    path = settings.data_dir() / "terrain" / "dem_utm10_10m.npz"
    if not path.exists():
        z = _fetch_tiled(BOX, CELL_M)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.stem + ".tmp.npz")
        np.savez_compressed(tmp, z=z, box=np.array(BOX), cell=CELL_M)
        tmp.replace(path)
    with np.load(path) as f:
        x0, _, _, y1 = f["box"]
        return Dem(f["z"], float(x0), float(y1), float(f["cell"]))
