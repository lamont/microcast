"""HRRR history from the University of Utah Zarr archive (``s3://hrrrzarr``).

The archive holds each HRRR surface field as 150x150 Zarr chunks with every
forecast hour of a cycle in one chunk, so a cycle at our ~30 points is a dozen
small GETs instead of ~20 MB of GRIB per forecast hour. That makes a year of
backfill practical on a laptop (docs/decisions.md D6).

Rows match what the GRIB path (``ingest.nwp``) writes: same ``model``,
``product``, ``variable`` and ``level`` values, so silver can't tell the two
sources apart and dedupes across them on the natural key. Only
``ingest_batch`` (``hrrrzarr/...``) records where a row came from.

Layout, per cycle ``YYYYMMDD_HHz``:

* ``sfc/<day>/<cycle>_anl.zarr/<level>/<VAR>/<level>/<VAR>/<cy>.<cx>``: lead 0
* ``sfc/<day>/<cycle>_fcst.zarr/<level>/<VAR>/<level>/<VAR>/0.<cy>.<cx>``: leads
  1-18 (1-48 at 00/06/12/18Z), time first.

Each cycle is cut to a fixed Bay Area window (``WINDOW_BOUNDS``: every field,
leads 0-18) and kept under ``data/hrrr/window/``. Points anywhere inside it are
then read from disk, so adding stations later costs no downloads. A cycle's
chunks are ~20 MB on the wire; its window is a few hundred KB.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import requests
from numcodecs import Blosc
from pyproj import Proj

from microcast import settings
from microcast.lake.schemas import NWP_POINT_SCHEMA
from microcast.registry import Registry, VirtualPoint

BASE_URL = "https://hrrrzarr.s3.amazonaws.com"
CHUNK = 150
K_NEIGHBOURS = 4
FILL = -9999.0
# Kept window (lat_min, lat_max, lon_min, lon_max): the MADIS network box plus SFO, Oakland and Marin.
WINDOW_BOUNDS = (37.55, 37.95, -122.70, -122.15)
WINDOW_MARGIN = 3  # cells beyond the bounds, so every point's 4 nearest cells are inside
CACHE_LEADS = 18  # every cycle has f01-f18; the 48-hour cycles' later leads aren't kept

# (zarr level, zarr variable) -> (cfgrib short name, level label used by ingest.nwp)
VARIABLES: dict[tuple[str, str], tuple[str, str]] = {
    ("2m_above_ground", "TMP"): ("t2m", "heightAboveGround:2"),
    ("2m_above_ground", "DPT"): ("d2m", "heightAboveGround:2"),
    ("2m_above_ground", "RH"): ("r2", "heightAboveGround:2"),
    ("10m_above_ground", "UGRD"): ("u10", "heightAboveGround:10"),
    ("10m_above_ground", "VGRD"): ("v10", "heightAboveGround:10"),
    ("surface", "GUST"): ("gust", "surface"),
    ("surface", "PRATE"): ("prate", "surface"),
    ("surface", "VIS"): ("vis", "surface"),
    ("surface", "DSWRF"): ("sdswrf", "surface"),
    ("low_cloud_layer", "LCDC"): ("lcc", "lowCloudLayer:0"),
    ("entire_atmosphere", "TCDC"): ("tcc", "atmosphere:0"),
    ("cloud_base", "HGT"): ("gh", "cloudBase:0"),
    ("cloud_ceiling", "HGT"): ("gh", "cloudCeiling:0"),
}

# HRRR's Lambert conformal grid on a sphere, as published in grid/projparams.json.
_PROJ = Proj(proj="lcc", a=6371229, b=6371229, lon_0=262.5, lat_0=38.5, lat_1=38.5, lat_2=38.5)
_BLOSC = Blosc()
_local = threading.local()


class CycleMissing(Exception):
    """The archive has no data for this cycle (a gap in the archive, or not yet published)."""


def _session() -> requests.Session:
    if not hasattr(_local, "session"):
        s = requests.Session()
        s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=3))
        _local.session = s
    return _local.session


def _get(url: str) -> bytes | None:
    r = _session().get(url, timeout=60)
    if r.status_code in (403, 404):
        return None
    r.raise_for_status()
    return r.content


@cache
def grid_axes() -> tuple[np.ndarray, np.ndarray]:
    """Projection x and y coordinates of the HRRR grid (metres), from any cycle."""
    base = f"{BASE_URL}/sfc/20250715/20250715_00z_anl.zarr/2m_above_ground/TMP/projection_{{}}_coordinate"
    out = []
    for axis in ("x", "y"):
        meta = json.loads(_get(base.format(axis) + "/.zarray"))
        raw = _BLOSC.decode(_get(base.format(axis) + "/0"))
        out.append(np.frombuffer(raw, dtype=meta["dtype"]))
    return out[0], out[1]


@dataclass(frozen=True)
class Neighbour:
    point_id: str
    k: int
    row: int
    col: int
    grid_lat: float
    grid_lon: float
    dist_m: float


def neighbours(registry: Registry, k: int = K_NEIGHBOURS) -> list[Neighbour]:
    """The k nearest grid cells to every registry point, by projected distance."""
    return point_neighbours(registry.virtual_points(), k)


def point_neighbours(points: list[VirtualPoint], k: int = K_NEIGHBOURS) -> list[Neighbour]:
    """The k nearest grid cells to each point, by projected distance."""
    xs, ys = grid_axes()
    dx = float(xs[1] - xs[0])
    out = []
    for vp in points:
        px, py = _PROJ(vp.lon, vp.lat)
        c0, r0 = int(round((px - xs[0]) / dx)), int(round((py - ys[0]) / dx))
        cands = [
            (float(np.hypot(xs[c] - px, ys[r] - py)), r, c)
            for r in range(r0 - 2, r0 + 3)
            for c in range(c0 - 2, c0 + 3)
            if 0 <= r < len(ys) and 0 <= c < len(xs)
        ]
        for rank, (dist, r, c) in enumerate(sorted(cands)[:k]):
            lon, lat = _PROJ(xs[c], ys[r], inverse=True)
            out.append(Neighbour(vp.point_id, rank, r, c, lat, (lon + 180) % 360 - 180, dist))
    return out


def _cycle_url(init: datetime, kind: str) -> str:
    return f"{BASE_URL}/sfc/{init:%Y%m%d}/{init:%Y%m%d_%H}z_{kind}.zarr"


@cache
def window() -> tuple[int, int, int, int]:
    """Grid rows [r0, r1) and columns [c0, c1) covering ``WINDOW_BOUNDS`` plus the margin."""
    xs, ys = grid_axes()
    dx = float(xs[1] - xs[0])
    lat0, lat1, lon0, lon1 = WINDOW_BOUNDS
    rows, cols = [], []
    for lat in (lat0, lat1):
        for lon in (lon0, lon1):
            px, py = _PROJ(lon, lat)
            cols.append(int(round((px - xs[0]) / dx)))
            rows.append(int(round((py - ys[0]) / dx)))
    m = WINDOW_MARGIN
    return min(rows) - m, max(rows) + m + 1, min(cols) - m, max(cols) + m + 1


def _read_window(url: str, with_time: bool) -> np.ndarray | None:
    """One field over the window: shape (n_time, rows, cols), float32, NaN for fill."""
    r0, r1, c0, c1 = window()
    out = None
    for cy in range(r0 // CHUNK, (r1 - 1) // CHUNK + 1):
        for cx in range(c0 // CHUNK, (c1 - 1) // CHUNK + 1):
            key = f"0.{cy}.{cx}" if with_time else f"{cy}.{cx}"
            raw = _get(f"{url}/{key}")
            if raw is None:
                return None
            # Every field is <f4 with the whole time axis in one chunk (18 or 48 leads), so the
            # decoded size gives the time length and the .zarray round trip can be skipped.
            block = np.frombuffer(_BLOSC.decode(raw), dtype="<f4").reshape(-1, CHUNK, CHUNK)
            if out is None:
                out = np.full((block.shape[0], r1 - r0, c1 - c0), np.nan, dtype="float32")
            ra, rb = max(r0, cy * CHUNK), min(r1, (cy + 1) * CHUNK)
            ca, cb = max(c0, cx * CHUNK), min(c1, (cx + 1) * CHUNK)
            out[:, ra - r0 : rb - r0, ca - c0 : cb - c0] = block[
                :, ra - cy * CHUNK : rb - cy * CHUNK, ca - cx * CHUNK : cb - cx * CHUNK
            ]
    out[out == FILL] = np.nan
    return out


def _key(level: str, var: str) -> str:
    return f"{level}__{var}"


def tile_path(init: datetime) -> Path:
    return settings.data_dir() / "hrrr" / "window" / f"{init:%Y/%m/%d}" / f"{init:%Y%m%d%H}.npz"


def cycle_window(init: datetime) -> dict[str, np.ndarray]:
    """Every field over the window for one cycle, (CACHE_LEADS + 1, rows, cols); index 0 is the analysis.

    Read from the local copy when there is one, else fetched from the archive
    and kept. Raises ``CycleMissing`` if the archive lacks any field.
    """
    path = tile_path(init)
    if path.exists():
        with np.load(path) as z:
            if tuple(z["window"]) == window():
                return {k: z[k] for k in z.files if k != "window"}
    r0, r1, c0, c1 = window()
    out = {}
    for level, var in VARIABLES:
        anl = _read_window(f"{_cycle_url(init, 'anl')}/{level}/{var}/{level}/{var}", with_time=False)
        if anl is None:
            raise CycleMissing(f"{init:%Y-%m-%dT%HZ} anl {level}/{var}")
        fcst = _read_window(f"{_cycle_url(init, 'fcst')}/{level}/{var}/{level}/{var}", with_time=True)
        if fcst is None:
            raise CycleMissing(f"{init:%Y-%m-%dT%HZ} fcst {level}/{var}")
        arr = np.full((CACHE_LEADS + 1, r1 - r0, c1 - c0), np.nan, dtype="float32")
        arr[0] = anl[0]
        n = min(CACHE_LEADS, fcst.shape[0])
        arr[1 : n + 1] = fcst[:n]  # fcst time index 0 is f01
        out[_key(level, var)] = arr
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.npz")  # written whole, then renamed: no torn files
    np.savez_compressed(tmp, window=np.array(window()), **out)
    tmp.replace(path)
    return out


def fetch_cycle(
    init: datetime,
    cells: list[Neighbour],
    leads: list[int],
    *,
    model: str = "hrrr",
    product: str = "sfc",
    tag: str | None = None,
    ingested_at: datetime | None = None,
) -> pa.Table:
    """One cycle's requested leads at every neighbour cell, as bronze.nwp_point rows.

    Raises ``CycleMissing`` if the archive lacks any field for the cycle, so a
    partial cycle is never written and a rerun retries it.
    """
    init = init.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    ingested_at = ingested_at or datetime.now(UTC)
    if any(h > CACHE_LEADS for h in leads):
        raise ValueError(f"leads beyond f{CACHE_LEADS:02d} aren't kept in the window")
    r0, r1, c0, c1 = window()
    if outside := [c.point_id for c in cells if not (r0 <= c.row < r1 and c0 <= c.col < c1)]:
        raise ValueError(f"points outside the kept window {WINDOW_BOUNDS}: {sorted(set(outside))}")
    rows = np.array([c.row - r0 for c in cells])
    cols = np.array([c.col - c0 for c in cells])
    win = cycle_window(init)
    frames = []
    for (level, var), (name, level_label) in VARIABLES.items():
        arr = win[_key(level, var)]
        per_lead = {h: arr[h, rows, cols].astype("float64") for h in leads}
        for h, v in per_lead.items():
            frames.append(
                pd.DataFrame(
                    {
                        "lead_min": np.int32(h * 60),
                        "valid_time": pd.Timestamp(init + timedelta(hours=h)),
                        "point_id": [c.point_id for c in cells],
                        "variable": name,
                        "level": level_label,
                        "k": np.array([c.k for c in cells], dtype="int32"),
                        "value": v,
                        "grid_lat": [c.grid_lat for c in cells],
                        "grid_lon": [c.grid_lon for c in cells],
                        "grid_dist_m": [c.dist_m for c in cells],
                    }
                )
            )
    if not frames:
        return NWP_POINT_SCHEMA.as_arrow().empty_table()
    df = pd.concat(frames, ignore_index=True)
    df["model"] = model
    df["product"] = product
    df["init_time"] = pd.Timestamp(init)
    df["member"] = np.int32(0)
    df["ingest_batch"] = batch_id(model, init, tag)
    df["ingested_at"] = pd.Timestamp(ingested_at)
    return pa.Table.from_pandas(
        df[NWP_POINT_SCHEMA.column_names], schema=NWP_POINT_SCHEMA.as_arrow(), preserve_index=False
    )


def batch_id(model: str, init: datetime, tag: str | None = None) -> str:
    """``hrrrzarr/<cycle>`` for the registry pass; ``hrrrzarr/<tag>/<cycle>`` for a points-only pass."""
    return f"{model}zarr/{tag}/{init:%Y-%m-%dT%H}" if tag else f"{model}zarr/{init:%Y-%m-%dT%H}"


def cycles(start: datetime, end: datetime, stride_h: int = 1) -> list[datetime]:
    """Hourly cycles in [start, end), thinned to one in ``stride_h``.

    The kept hour rotates by one each day (``(day + hour) % stride == 0``), so
    every lead still sees every hour of the day over ``stride_h`` days. A fixed
    00/03/06Z subset would only ever verify lead L at 8 valid hours.
    """
    t = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    out = []
    while t < end:
        day = (t - datetime(1970, 1, 1, tzinfo=UTC)).days
        if (day + t.hour) % stride_h == 0:
            out.append(t)
        t += timedelta(hours=1)
    return out


def backfill(
    registry: Registry,
    start: datetime,
    end: datetime,
    leads: list[int],
    *,
    stride_h: int = 1,
    workers: int = 16,
    commit_every: int = 48,
    model: str = "hrrr",
    points: list[VirtualPoint] | None = None,
    tag: str | None = None,
    log=print,
) -> dict[str, int]:
    """Fetch every missing cycle in [start, end) and append to bronze.nwp_point.

    By default the points are the registry's. A points-only pass (``points``
    plus a ``tag`` naming that set) records its own batch ids, so it covers
    cycles the registry pass already did; with the window kept on disk it
    reads no network for those cycles.

    Cycles already recorded in bronze snapshot summaries are skipped, so this
    is safe to interrupt and rerun. Archive gaps are logged and left for a
    later rerun. Fetches run in a thread pool; appends happen on this thread,
    one snapshot per ``commit_every`` cycles.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from microcast.lake.catalog import append_bronze, recorded_batches

    done = recorded_batches("bronze.nwp_point")
    todo = [c for c in cycles(start, end, stride_h) if batch_id(model, c, tag) not in done]
    stats = {"skipped": 0, "fetched": 0, "missing": 0, "rows": 0}
    stats["skipped"] = len(cycles(start, end, stride_h)) - len(todo)
    log(f"{len(todo)} cycles to fetch, {stats['skipped']} already in bronze")
    if not todo:
        return stats
    cells = neighbours(registry) if points is None else point_neighbours(points)
    pending: list[pa.Table] = []
    pending_ids: list[str] = []

    def flush() -> None:
        if pending:
            table = pa.concat_tables(pending)
            append_bronze("bronze.nwp_point", table, batches=pending_ids)
            stats["rows"] += table.num_rows
            log(f"  committed {len(pending_ids)} cycles ({table.num_rows} rows), last {pending_ids[-1]}")
            pending.clear()
            pending_ids.clear()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_cycle, c, cells, leads, model=model, tag=tag): c for c in todo}
        for fut in as_completed(futures):
            init = futures[fut]
            try:
                pending.append(fut.result())
                pending_ids.append(batch_id(model, init, tag))
                stats["fetched"] += 1
            except CycleMissing as e:
                stats["missing"] += 1
                log(f"  missing: {e}")
            if len(pending) >= commit_every:
                flush()
    flush()
    return stats
