"""Runtime relocation bounds and the current terrain portability boundary."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from microcast import settings
from microcast.ingest import hrrr_zarr, madis
from microcast.ingest.dem import Dem
from microcast.lake import catalog as lake
from microcast.transform import static


def test_bounds_defaults_and_validation(monkeypatch):
    monkeypatch.delenv("MICROCAST_HRRR_BOUNDS", raising=False)
    monkeypatch.delenv("MICROCAST_MADIS_BOUNDS", raising=False)
    assert settings.hrrr_bounds() == (37.55, 37.95, -122.70, -122.15)
    assert settings.madis_bounds() == (37.60, 37.90, -122.62, -122.33)

    monkeypatch.setenv("MICROCAST_HRRR_BOUNDS", "47,48,-123,-122")
    assert settings.hrrr_bounds() == (47.0, 48.0, -123.0, -122.0)
    monkeypatch.setenv("MICROCAST_HRRR_BOUNDS", "48,47,-123,-122")
    with pytest.raises(ValueError, match="ordered"):
        settings.hrrr_bounds()


def test_hrrr_window_reads_environment_each_time_and_checks_grid(monkeypatch):
    class IdentityProjection:
        def __call__(self, lon, lat, inverse=False):
            return lon, lat

    monkeypatch.setattr(hrrr_zarr, "_PROJ", IdentityProjection())
    monkeypatch.setattr(hrrr_zarr, "grid_axes", lambda: (np.arange(-130.0, -110.0), np.arange(30.0, 60.0)))
    monkeypatch.setenv("MICROCAST_HRRR_BOUNDS", "37,38,-123,-122")
    sf_window = hrrr_zarr.window()
    monkeypatch.setenv("MICROCAST_HRRR_BOUNDS", "47,48,-123,-122")
    assert hrrr_zarr.window() != sf_window

    monkeypatch.setenv("MICROCAST_HRRR_BOUNDS", "80,81,-123,-122")
    with pytest.raises(ValueError, match="exceed the HRRR grid"):
        hrrr_zarr.window()


def _madis_file() -> bytes:
    timestamp = datetime(2025, 6, 1, 20, tzinfo=UTC).timestamp()
    ds = xr.Dataset(
        {
            "stationId": ("recNum", np.array([b"LOCAL", b"SF"], dtype="S5")),
            "dataProvider": ("recNum", np.array([b"CWOP", b"CWOP"], dtype="S4")),
            "latitude": ("recNum", np.array([47.98, 37.76], dtype="f4")),
            "longitude": ("recNum", np.array([-122.72, -122.43], dtype="f4")),
            "elevation": ("recNum", np.array([60.0, 54.0], dtype="f4")),
            "observationTime": ("recNum", np.array([timestamp, timestamp], dtype="f8")),
            "temperature": ("recNum", np.array([288.15, 289.15], dtype="f4")),
        }
    )
    return gzip.compress(ds.to_netcdf(engine="scipy"))


def test_madis_backfill_uses_configured_box(monkeypatch, catalog):
    monkeypatch.setenv("MICROCAST_MADIS_BOUNDS", "47.9,48.1,-122.8,-122.6")
    monkeypatch.setattr(madis, "fetch_hour", lambda _: _madis_file())
    monkeypatch.setattr(lake, "get_catalog", lambda: catalog)
    lake.init_lake(catalog)

    start = datetime(2025, 6, 1, 20, tzinfo=UTC)
    stats = madis.backfill(start, start.replace(hour=21), workers=1, log=lambda _: None)
    assert stats == {"skipped": 0, "fetched": 1, "missing": 0, "rows": 1}
    station = catalog.load_table("bronze.stations").scan().to_arrow().to_pydict()
    assert station["station_id"] == ["LOCAL"]


def test_dem_and_static_rebuild_reject_outside_points_before_dem_load(monkeypatch):
    dem = Dem(np.zeros((10, 10), dtype="float32"), 500_000, 4_100_100, 10)
    with pytest.raises(ValueError, match="cached DEM bounds"):
        dem.at(np.array([37.0]), np.array([-122.0]))

    monkeypatch.setattr("microcast.ingest.dem.load", lambda: pytest.fail("DEM must not load for unsupported terrain"))
    points = pd.DataFrame({"point_id": ["local"], "lat": [47.98], "lon": [-122.72]})
    with pytest.raises(ValueError, match="built-in DEM builder bounds"):
        static.rebuild(points)


def test_dem_rejects_nonfinite_coordinates():
    dem = Dem(np.zeros((10, 10), dtype="float32"), 524_000, 4_153_100, 10)
    with pytest.raises(ValueError, match="cached DEM bounds"):
        dem.validate_xy(np.array([np.nan]), np.array([4_153_000.0]))


def test_hrrr_neighbours_reject_nonfinite_and_outside_grid(monkeypatch):
    class IdentityProjection:
        def __call__(self, lon, lat, inverse=False):
            return (lon, lat)

    monkeypatch.setattr(hrrr_zarr, "_PROJ", IdentityProjection())
    monkeypatch.setattr(hrrr_zarr, "grid_axes", lambda: (np.arange(0.0, 10.0), np.arange(0.0, 10.0)))
    vp = hrrr_zarr.VirtualPoint("local", "local", 0, 20.0, 5.0, None)
    with pytest.raises(ValueError, match="outside the HRRR grid"):
        hrrr_zarr.point_neighbours([vp])
    vp = hrrr_zarr.VirtualPoint("local", "local", 0, np.nan, 5.0, None)
    with pytest.raises(ValueError, match="invalid HRRR coordinates"):
        hrrr_zarr.point_neighbours([vp])
