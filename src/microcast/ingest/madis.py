"""NOAA MADIS public mesonet archive into bronze.obs (free history for CWOP etc.).

Each hour is one national netCDF-3 file, gzipped (~30 MB): stream it, keep
the observations inside the network box, discard the rest. gzip and the
record-interleaved netCDF-3 layout rule out range reads, so the whole file is
read; what is kept is a few hundred KB. The free Synoptic tier has no
history (D10); MADIS carries the same CWOP and PG&E stations.

Rows: ``source = 'madis'``, ``station_id`` = the MADIS station id (the CWOP
ids match Synoptic's, e.g. C5988), SI units and our variable names.
``qc_flag`` holds MADIS's per-variable QC summary (V verified, S screened,
C coarse pass, Q questionable, B bad, X rejected, Z none); silver drops B and X.
Station positions go to bronze.stations, since network features need them.
"""

from __future__ import annotations

import gzip
import io
import threading
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pyarrow as pa
import requests
import xarray as xr

from microcast.lake.schemas import OBS_SCHEMA, STATIONS_SCHEMA

BASE_URL = "https://madis-data.ncep.noaa.gov/madisPublic1/data/archive"

# MADIS variable -> (our name, converter to SI as we store it)
VARIABLES: dict[str, tuple[str, Callable[[np.ndarray], np.ndarray]]] = {
    "temperature": ("t2m", lambda k: k - 273.15),
    "dewpoint": ("d2m", lambda k: k - 273.15),
    "relHumidity": ("rh2m", lambda x: x),
    "windSpeed": ("wind10", lambda x: x),
    "windGust": ("gust", lambda x: x),
    "windDir": ("wdir10", lambda x: x),
    "stationPressure": ("pressure", lambda pa_: pa_ / 100.0),
    "altimeter": ("altimeter", lambda pa_: pa_ / 100.0),
    "solarRadiation": ("solar", lambda x: x),
}


@dataclass(frozen=True)
class Box:
    """Lat/lon bounds of the observation network we keep."""

    lat_min: float = 37.60
    lat_max: float = 37.90
    lon_min: float = -122.62
    lon_max: float = -122.33


SF_BAY_BOX = Box()


def hour_url(t: datetime) -> str:
    return f"{BASE_URL}/{t:%Y/%m/%d}/LDAD/mesonet/netCDF/{t:%Y%m%d_%H}00.gz"


def batch_id(t: datetime) -> str:
    return f"madis/{t:%Y-%m-%dT%H}"


def _text(a: xr.DataArray) -> np.ndarray:
    v = a.values
    if v.ndim == 2:  # char arrays: (recNum, len)
        return np.array([b"".join(r).decode(errors="ignore").strip("\x00 ") for r in v])
    return np.array([x.decode(errors="ignore").strip("\x00 ") if isinstance(x, bytes) else str(x) for x in v])


def parse_hour(raw_gz: bytes, box: Box, batch: str, ingested_at: datetime | None = None) -> tuple[pa.Table, pa.Table]:
    """One hourly file -> (bronze.obs rows, bronze.stations rows) inside ``box``."""
    ingested_at = pd.Timestamp(ingested_at or datetime.now(UTC))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # MADIS declares two fill values per variable
        ds = xr.open_dataset(io.BytesIO(gzip.decompress(raw_gz)), engine="scipy", decode_times=False)
    lat, lon = ds["latitude"].values, ds["longitude"].values
    keep = np.where((lat >= box.lat_min) & (lat <= box.lat_max) & (lon >= box.lon_min) & (lon <= box.lon_max))[0]
    sub = ds.isel(recNum=keep)
    ids, providers = _text(sub["stationId"]), _text(sub["dataProvider"])
    times = pd.to_datetime(sub["observationTime"].values, unit="s", utc=True)
    frames = []
    for var, (name, convert) in VARIABLES.items():
        if var not in sub:
            continue
        values = convert(sub[var].values.astype("float64"))
        flags = _text(sub[f"{var}DD"]) if f"{var}DD" in sub else np.full(len(keep), None)
        frames.append(
            pd.DataFrame(
                {
                    "station_id": ids,
                    "obs_time": times,
                    "variable": name,
                    "value": values,
                    "qc_flag": flags,
                }
            )
        )
    obs = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=OBS_SCHEMA.column_names)
    obs = obs[np.isfinite(obs.value.astype("float64")) & obs.obs_time.notna()]
    obs = obs.drop_duplicates(["station_id", "obs_time", "variable"], keep="last")
    obs = obs.assign(source="madis", ingest_batch=batch, ingested_at=ingested_at)
    stations = (
        pd.DataFrame(
            {
                "station_id": ids,
                "provider": providers,
                "lat": lat[keep].astype("float64"),
                "lon": lon[keep].astype("float64"),
                "elevation_m": sub["elevation"].values.astype("float64"),
            }
        )
        .drop_duplicates("station_id", keep="last")
        .assign(source="madis", ingest_batch=batch, ingested_at=ingested_at)
    )
    return (
        pa.Table.from_pandas(obs[OBS_SCHEMA.column_names], schema=OBS_SCHEMA.as_arrow(), preserve_index=False),
        pa.Table.from_pandas(
            stations[STATIONS_SCHEMA.column_names], schema=STATIONS_SCHEMA.as_arrow(), preserve_index=False
        ),
    )


_local = threading.local()


def fetch_hour(t: datetime) -> bytes | None:
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
    r = _local.session.get(hour_url(t), timeout=300)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


def hours(start: datetime, end: datetime) -> list[datetime]:
    t = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    out = []
    while t < end:
        out.append(t)
        t += timedelta(hours=1)
    return out


def backfill(
    start: datetime,
    end: datetime,
    *,
    box: Box = SF_BAY_BOX,
    workers: int = 4,
    commit_every: int = 48,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    """Every hour in [start, end) not yet in bronze.obs; resumable like the HRRR backfill."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from microcast.lake.catalog import append_bronze, recorded_batches

    done = recorded_batches("bronze.obs")
    todo = [t for t in hours(start, end) if batch_id(t) not in done]
    stats = {"skipped": len(hours(start, end)) - len(todo), "fetched": 0, "missing": 0, "rows": 0}
    log(f"{len(todo)} hours to fetch, {stats['skipped']} already in bronze")
    obs_parts: list[pa.Table] = []
    station_parts: list[pa.Table] = []
    ids: list[str] = []

    def flush() -> None:
        if ids:
            obs = pa.concat_tables(obs_parts)
            append_bronze("bronze.obs", obs, batches=ids)
            append_bronze("bronze.stations", pa.concat_tables(station_parts), batches=ids)
            stats["rows"] += obs.num_rows
            log(f"  committed {len(ids)} hours ({obs.num_rows} rows), last {ids[-1]}")
            obs_parts.clear()
            station_parts.clear()
            ids.clear()

    def work(t: datetime) -> tuple[pa.Table, pa.Table] | None:
        raw = fetch_hour(t)
        return None if raw is None else parse_hour(raw, box, batch_id(t))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, t): t for t in todo}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                result = fut.result()
            except Exception as e:  # a corrupt or truncated file: log and leave for a rerun
                stats["missing"] += 1
                log(f"  failed {batch_id(t)}: {e}")
                continue
            if result is None:
                stats["missing"] += 1
                log(f"  missing {batch_id(t)}")
                continue
            obs_parts.append(result[0])
            station_parts.append(result[1])
            ids.append(batch_id(t))
            stats["fetched"] += 1
            if len(ids) >= commit_every:
                flush()
    flush()
    return stats
