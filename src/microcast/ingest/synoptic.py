"""Synoptic Data (MesoWest) stations into bronze.obs.

The free tier serves only the last ~7 days (a request further back is refused
with a 403 in the body), so these stations cannot be backfilled: they accrue.
``microcast ingest synoptic`` pulls the last ``days`` (<= 7) and must run at
least weekly, daily in practice; overlapping pulls are fine, since silver keeps
the latest ``ingested_at`` per observation.

Auth: Synoptic issues an API key, and requests need a token generated from it
(``/v2/auth?apikey=...``). ``SYNOPTIC_TOKEN`` is that token.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import requests

API = "https://api.synopticdata.com/v2"
MAX_DAYS = 7

# Synoptic variable -> our name (SI units with units=metric).
VARIABLES = {
    "air_temp": "t2m",
    "dew_point_temperature": "d2m",
    "wind_speed": "wind10",
    "wind_gust": "gust",
    "wind_direction": "wdir10",
    "solar_radiation": "solar",
}


class SynopticError(RuntimeError):
    pass


def _series(obs: dict, var: str) -> list | None:
    """Prefer the reported set (``_set_1``) over a derived one (``_set_1d``)."""
    for suffix in ("_set_1", "_set_1d"):
        if (values := obs.get(var + suffix)) is not None:
            return values
    return None


def parse_timeseries(payload: dict) -> pd.DataFrame:
    summary = payload.get("SUMMARY", {})
    if summary.get("RESPONSE_CODE") != 1:
        raise SynopticError(f"{summary.get('RESPONSE_CODE')}: {summary.get('RESPONSE_MESSAGE')}")
    frames = []
    for st in payload.get("STATION", []):
        obs = st.get("OBSERVATIONS", {})
        times = pd.to_datetime(obs.get("date_time", []), utc=True)
        for var, name in VARIABLES.items():
            values = _series(obs, var)
            if values is None:
                continue
            frames.append(
                pd.DataFrame(
                    {
                        "source": "synoptic",
                        "station_id": st["STID"],
                        "obs_time": times,
                        "variable": name,
                        "value": pd.to_numeric(pd.Series(values, dtype="object"), errors="coerce").astype("float64"),
                    }
                )
            )
    if not frames:
        return pd.DataFrame(columns=["source", "station_id", "obs_time", "variable", "value"])
    out = pd.concat(frames, ignore_index=True)
    return out[np.isfinite(out.value)].reset_index(drop=True)


def fetch(stations: Iterable[str], days: int = MAX_DAYS, token: str | None = None) -> pd.DataFrame:
    end = datetime.now(UTC)
    start = end - timedelta(days=min(days, MAX_DAYS))
    params = {
        "token": token or os.environ["SYNOPTIC_TOKEN"].split()[0],
        "stid": ",".join(stations),
        "start": f"{start:%Y%m%d%H%M}",
        "end": f"{end:%Y%m%d%H%M}",
        "vars": ",".join(VARIABLES),
        "units": "metric",
    }
    r = requests.get(f"{API}/stations/timeseries", params=params, timeout=120)
    r.raise_for_status()
    return parse_timeseries(r.json())
