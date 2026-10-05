"""Own PurpleAir sensor into bronze.obs.

Two paths, same rows:

* **LAN** (``MICROCAST_PURPLEAIR_HOST``): ``GET http://<host>/json`` on the home
  network. Free, real time (2-minute averages); poll it every 2 minutes.
* **API history** (``PURPLEAIR_API_KEY``): ``/v1/sensors/<index>/history`` for
  backfill. Costs API points, so it asks for few fields and 10-minute averages.

Rows use ``source`` ``purpleair`` and ``station_id`` ``purpleair_home`` (the
registry sensor id, never the public sensor index, which would locate the house).
Variables: ``pm25_a`` / ``pm25_b`` (ug/m3, ATM, both laser channels; silver
checks they agree), ``t_sensor`` (degC, reads several degrees hot inside the
housing, so it is a trend feature only, never a target), ``rh_sensor`` (%),
``pressure`` (hPa).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime

import pandas as pd
import requests

from microcast.ingest.obs import f_to_c

SOURCE = "purpleair"
STATION = "purpleair_home"
API = "https://api.purpleair.com/v1"
HISTORY_FIELDS = ["pm2.5_atm_a", "pm2.5_atm_b", "temperature", "humidity", "pressure"]


def _rows(obs_time: pd.Series | pd.Timestamp, values: dict[str, object]) -> pd.DataFrame:
    df = pd.DataFrame({"obs_time": obs_time, **values})
    out = df.melt(id_vars="obs_time", var_name="variable", value_name="value")
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out = out.dropna(subset=["value"])
    out.insert(0, "station_id", STATION)
    out.insert(0, "source", SOURCE)
    return out.reset_index(drop=True)


def parse_local(doc: dict) -> pd.DataFrame:
    """The sensor's own ``/json`` document (one reading, 2-minute averages)."""
    t = pd.Timestamp(datetime.strptime(doc["DateTime"], "%Y/%m/%dT%H:%M:%Sz").replace(tzinfo=UTC))
    return _rows(
        [t],
        {
            "pm25_a": [doc.get("pm2_5_atm")],
            "pm25_b": [doc.get("pm2_5_atm_b")],
            "t_sensor": [f_to_c(pd.Series([doc.get("current_temp_f")], dtype="float64"))[0]],
            "rh_sensor": [doc.get("current_humidity")],
            "pressure": [doc.get("pressure")],
        },
    )


def parse_history(payload: dict) -> pd.DataFrame:
    """``/sensors/<index>/history`` JSON: ``fields`` plus ``data`` rows."""
    df = pd.DataFrame(payload.get("data", []), columns=payload.get("fields", []))
    if df.empty:
        return _rows(pd.Series([], dtype="datetime64[ns, UTC]"), {"pm25_a": []})
    return _rows(
        pd.to_datetime(df["time_stamp"], unit="s", utc=True),
        {
            "pm25_a": df.get("pm2.5_atm_a"),
            "pm25_b": df.get("pm2.5_atm_b"),
            "t_sensor": f_to_c(df["temperature"].astype("float64")) if "temperature" in df else None,
            "rh_sensor": df.get("humidity"),
            "pressure": df.get("pressure"),
        },
    )


def _get_lan_json(url: str) -> dict:
    """GET a LAN URL, falling back to Apple's curl when macOS blocks this Python.

    macOS Local Network privacy denies LAN connections to binaries without a
    stable code identity (uv's and Homebrew's ad-hoc signed Pythons) and never
    prompts, so the connect fails with EHOSTUNREACH. Apple-signed /usr/bin/curl
    is allowed. On Linux the first attempt simply works.
    """
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.ConnectionError as e:
        if sys.platform != "darwin" or "No route to host" not in str(e):
            raise
    out = subprocess.run(["/usr/bin/curl", "-sf", "-m", "10", url], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def fetch_local(host: str | None = None) -> pd.DataFrame:
    host = host or os.environ["MICROCAST_PURPLEAIR_HOST"]
    doc = _get_lan_json(f"http://{host}/json")
    if doc.get("place") not in (None, "", "outside"):
        raise ValueError(f"{host} reports place={doc.get('place')!r}; expected the outdoor sensor")
    return parse_local(doc)


def fetch_history(sensor_index: int, start: datetime, end: datetime, api_key: str | None = None) -> pd.DataFrame:
    """10-minute averages for [start, end). The API caps one call at ~2 days at this average."""
    params = {
        "start_timestamp": int(start.timestamp()),
        "end_timestamp": int(end.timestamp()),
        "average": 10,
        "fields": ",".join(HISTORY_FIELDS),
    }
    r = requests.get(
        f"{API}/sensors/{sensor_index}/history",
        params=params,
        headers={"X-API-Key": api_key or os.environ["PURPLEAIR_API_KEY"]},
        timeout=60,
    )
    r.raise_for_status()
    return parse_history(r.json())
