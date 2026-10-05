"""Paths and environment-driven settings shared by every module.

Everything location-specific (home coordinates, data directory, catalog URI)
comes from the environment or a git-ignored ``.env`` file, never from YAML in git.
"""

from __future__ import annotations

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
