"""Places and routes registry: load YAML, densify routes into virtual points.

Every downstream table joins on ``point_id``. A point place has one virtual
point (``<place_id>``); a route has one per sample (``<route_id>:<seq>``), each
carrying the route bearing at that spot so wind can be split into headwind and
crosswind components.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from pyproj import Geod, Transformer
from shapely.geometry import LineString, shape

from microcast import settings

_GEOD = Geod(ellps="WGS84")
# UTM 10N covers San Francisco; metre-accurate distances along the route.
_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32610", always_xy=True)
_FROM_UTM = Transformer.from_crs("EPSG:32610", "EPSG:4326", always_xy=True)

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class VirtualPoint:
    point_id: str
    target_id: str
    seq: int
    lat: float
    lon: float
    bearing_deg: float | None  # direction of travel; None for point places


@dataclass
class Place:
    id: str
    lat: float
    lon: float
    sensors: list[str] = field(default_factory=list)
    variables: list[str] = field(default_factory=list)

    def virtual_points(self) -> list[VirtualPoint]:
        return [VirtualPoint(self.id, self.id, 0, self.lat, self.lon, None)]


@dataclass
class Route:
    id: str
    geometry: LineString  # lon/lat
    sample_every_m: float
    mode: str
    schedule: list[dict[str, Any]] = field(default_factory=list)
    flexible_departure: dict[str, Any] | None = None
    audience: list[str] = field(default_factory=list)
    out_and_back: bool = False

    def virtual_points(self) -> list[VirtualPoint]:
        return densify(self.id, self.geometry, self.sample_every_m)


@dataclass
class Station:
    id: str
    source: str  # which obs ingest serves it: iem_asos, iem_hads, ndbc
    lat: float
    lon: float

    def virtual_points(self) -> list[VirtualPoint]:
        return [VirtualPoint(self.id, self.id, 0, self.lat, self.lon, None)]


@dataclass
class Registry:
    places: list[Place]
    routes: list[Route]
    stations: list[Station] = field(default_factory=list)
    sensors: dict[str, dict[str, Any]] = field(default_factory=dict)

    def virtual_points(self) -> list[VirtualPoint]:
        points = [vp for p in self.places for vp in p.virtual_points()]
        points += [vp for r in self.routes for vp in r.virtual_points()]
        points += [vp for s in self.stations for vp in s.virtual_points()]
        return points

    def points_df(self) -> pd.DataFrame:
        """Points in the shape Herbie's ``pick_points`` expects.

        The ``id`` column comes back from Herbie as the ``point_id`` coordinate.
        """
        vps = self.virtual_points()
        return pd.DataFrame(
            {
                "latitude": [vp.lat for vp in vps],
                "longitude": [vp.lon for vp in vps],
                "id": [vp.point_id for vp in vps],
            }
        )


def densify(target_id: str, line: LineString, every_m: float) -> list[VirtualPoint]:
    """Sample a lon/lat LineString every ``every_m`` metres, endpoints included."""
    utm = LineString([_TO_UTM.transform(x, y) for x, y in line.coords])
    length = utm.length
    n = max(1, round(length / every_m))
    step = length / n
    points = []
    for seq in range(n + 1):
        d = seq * step
        lon, lat = _FROM_UTM.transform(*utm.interpolate(d).coords[0])
        # Bearing from a short chord around the sample, clamped to the line.
        a = _FROM_UTM.transform(*utm.interpolate(max(0.0, d - 5)).coords[0])
        b = _FROM_UTM.transform(*utm.interpolate(min(length, d + 5)).coords[0])
        fwd, _, _ = _GEOD.inv(a[0], a[1], b[0], b[1])
        points.append(VirtualPoint(f"{target_id}:{seq}", target_id, seq, lat, lon, fwd % 360))
    return points


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise KeyError(f"registry references ${{{name}}} but it is not set (see .env.example)")
            return os.environ[name]

        return _ENV_REF.sub(sub, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _load_line(path: Path) -> LineString:
    data = json.loads(path.read_text())
    if data.get("type") == "FeatureCollection":
        data = data["features"][0]
    geom = shape(data["geometry"] if data.get("type") == "Feature" else data)
    if not isinstance(geom, LineString):
        raise ValueError(f"{path}: expected a LineString, got {geom.geom_type}")
    return geom


def _coord(value: Any, what: str) -> float:
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{what} must be a single number, got {value!r} (one value per variable in .env)") from None


def load(path: Path | None = None) -> Registry:
    path = path or settings.places_file()
    raw = _expand_env(yaml.safe_load(path.read_text()))
    base = path.parent.parent  # geometry paths are relative to the repo root

    places = [
        Place(
            id=p["id"],
            lat=_coord(p["lat"], f"{p['id']}.lat"),
            lon=_coord(p["lon"], f"{p['id']}.lon"),
            sensors=p.get("sensors", []),
            variables=p.get("variables", []),
        )
        for p in raw.get("places", [])
    ]
    routes = [
        Route(
            id=r["id"],
            geometry=_load_line(base / r["geometry"]),
            sample_every_m=float(r["sample_every_m"]),
            mode=r["mode"],
            schedule=r.get("schedule", []),
            flexible_departure=r.get("flexible_departure"),
            audience=r.get("audience", []),
            out_and_back=bool(r.get("out_and_back", False)),
        )
        for r in raw.get("routes", [])
    ]
    stations = [
        Station(id=s["id"], source=s["source"], lat=_coord(s["lat"], s["id"]), lon=_coord(s["lon"], s["id"]))
        for s in raw.get("stations", [])
    ]
    sensors = {s["id"]: s for s in raw.get("sensors", [])}
    ids = [t.id for t in places] + [t.id for t in routes] + [t.id for t in stations]
    if dupes := {i for i in ids if ids.count(i) > 1}:
        raise ValueError(f"duplicate registry ids: {sorted(dupes)}")
    return Registry(places, routes, stations, sensors)
