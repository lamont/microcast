from __future__ import annotations

import itertools

import pytest
from pyproj import Geod
from shapely.geometry import LineString

from microcast import registry, settings

GEOD = Geod(ellps="WGS84")


def test_example_registry_loads():
    reg = registry.load(settings.REPO_ROOT / "config" / "places.yaml")
    ids = [vp.point_id for vp in reg.virtual_points()]
    assert ids[0] == "home"
    assert "school_walk:0" in ids and "gg_park_ride:0" in ids
    assert {"SFOC1", "KSFO", "KOAK", "FTPC1"} <= set(ids)
    assert len(ids) == len(set(ids))
    ride = next(r for r in reg.routes if r.id == "gg_park_ride")
    assert ride.out_and_back
    assert reg.sensors["purpleair_home"]["sensor_index"] == "12345"
    df = reg.points_df()
    assert list(df.columns) == ["latitude", "longitude", "id"]


def test_missing_env_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("MICROCAST_HOME_LAT")
    with pytest.raises(KeyError, match="MICROCAST_HOME_LAT"):
        registry.load(settings.REPO_ROOT / "config" / "places.yaml")


def test_lat_lon_pair_in_one_variable_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("MICROCAST_HOME_LAT", "37.76, -122.43")
    with pytest.raises(ValueError, match="single number"):
        registry.load(settings.REPO_ROOT / "config" / "places.yaml")


def test_densify_spacing_and_endpoints():
    # ~1.1 km due north along a meridian
    line = LineString([(-122.44, 37.77), (-122.44, 37.78)])
    pts = registry.densify("r", line, 200)
    assert (pts[0].lat, pts[0].lon) == pytest.approx((37.77, -122.44), abs=1e-6)
    assert (pts[-1].lat, pts[-1].lon) == pytest.approx((37.78, -122.44), abs=1e-6)
    gaps = [GEOD.inv(a.lon, a.lat, b.lon, b.lat)[2] for a, b in itertools.pairwise(pts)]
    assert all(150 < g < 250 for g in gaps)
    assert [p.seq for p in pts] == list(range(len(pts)))


def test_bearing_follows_direction_of_travel():
    north = registry.densify("n", LineString([(-122.44, 37.77), (-122.44, 37.78)]), 500)
    west = registry.densify("w", LineString([(-122.44, 37.77), (-122.46, 37.77)]), 500)
    assert all(min(p.bearing_deg, 360 - p.bearing_deg) < 1 for p in north)
    assert all(abs(p.bearing_deg - 270) < 1 for p in west)
