"""Paths and environment-driven settings shared by every module.

Everything location-specific (home coordinates, data directory, catalog URI)
comes from the environment or a git-ignored ``.env`` file, never from YAML in git.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(REPO_ROOT / ".env")


def data_dir() -> Path:
    """Root for local lake files, GRIB cache and the SQLite catalog."""
    path = Path(os.environ.get("MICROCAST_DATA_DIR", REPO_ROOT / "data")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir() -> Path:
    return Path(os.environ.get("MICROCAST_CONFIG_DIR", REPO_ROOT / "config")).expanduser()


def places_file() -> Path:
    """The registry file: an explicit override, else a private local copy, else the example."""
    if override := os.environ.get("MICROCAST_PLACES"):
        return Path(override).expanduser()
    local = config_dir() / "places.local.yaml"
    return local if local.exists() else config_dir() / "places.yaml"


def _bounds(name: str, default: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        values = tuple(float(x.strip()) for x in raw.split(","))
    except ValueError as exc:
        raise ValueError(f"{name} must be lat_min,lat_max,lon_min,lon_max") from exc
    if len(values) != 4 or not all(math.isfinite(x) for x in values):
        raise ValueError(f"{name} must contain four finite numbers: lat_min,lat_max,lon_min,lon_max")
    lat0, lat1, lon0, lon1 = values
    if not (-90 <= lat0 < lat1 <= 90 and -180 <= lon0 < lon1 <= 180):
        raise ValueError(f"{name} bounds must be ordered and within latitude/longitude ranges")
    return values


def hrrr_bounds() -> tuple[float, float, float, float]:
    """Kept HRRR window: the MADIS network box plus SFO, Oakland and Marin by default."""
    return _bounds("MICROCAST_HRRR_BOUNDS", (37.55, 37.95, -122.70, -122.15))


def madis_bounds() -> tuple[float, float, float, float]:
    return _bounds("MICROCAST_MADIS_BOUNDS", (37.60, 37.90, -122.62, -122.33))
