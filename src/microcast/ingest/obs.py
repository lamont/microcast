"""Station observations into bronze.obs, from sources that need no API key.

* ``iem_asos``: ASOS METARs (routine + specials) from the Iowa Environmental
  Mesonet archive. Temperature, dewpoint, wind, direction, gust.
* ``iem_hads``: NWS HADS/DCP reports via IEM. Used for SFOC1 (SF Downtown,
  hourly temperature only).
* ``ndbc``: NOAA NDBC standard meteorological files (yearly archive, then
  monthly, then the 45-day realtime file). Used for FTPC1 at Fort Point.

Parsers are pure (text in, long DataFrame out) and tested offline. Every row is
converted to SI and the names silver uses: ``t2m``, ``d2m`` (degC), ``wind10``,
``gust`` (m/s), ``wdir10`` (degrees from). Values are as reported; range checks
happen in silver.
"""

from __future__ import annotations

import gzip
import io
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pyarrow as pa
import requests

from microcast.lake.schemas import OBS_SCHEMA
from microcast.registry import Station

IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request"
NDBC = "https://www.ndbc.noaa.gov"
KT_TO_MS = 0.514444
_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def f_to_c(f: pd.Series) -> pd.Series:
    return (f - 32.0) * 5.0 / 9.0


def _long(df: pd.DataFrame, source: str, station: str, columns: dict[str, str]) -> pd.DataFrame:
    """Wide (obs_time + value columns) to long rows with OBS_SCHEMA's leading columns."""
    out = (
        df.rename(columns=columns)[["obs_time", *columns.values()]]
        .melt(id_vars="obs_time", var_name="variable", value_name="value")
        .dropna(subset=["value"])
    )
    out.insert(0, "station_id", station)
    out.insert(0, "source", source)
    return out.reset_index(drop=True)


def parse_iem_asos(text: str, station: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text), na_values=["", "M"])
    if df.empty:
        return _long(pd.DataFrame(columns=["obs_time", "t2m"]), "iem_asos", station, {"t2m": "t2m"})
    df["obs_time"] = pd.to_datetime(df["valid"], utc=True)
    df["t2m"] = f_to_c(df["tmpf"])
    df["d2m"] = f_to_c(df["dwpf"])
    df["wind10"] = df["sknt"] * KT_TO_MS
    df["gust"] = df["gust"] * KT_TO_MS  # only reported when gusting; silver fills from wind10
    df["wdir10"] = df["drct"]
    return _long(df, "iem_asos", station, {c: c for c in ["t2m", "d2m", "wind10", "gust", "wdir10"]})


def parse_iem_hads(text: str, station: str) -> pd.DataFrame:
    """HADS SHEF codes: TAIRGZZ is the instantaneous air temperature in degF."""
    df = pd.read_csv(io.StringIO(text))
    if df.empty or "TAIRGZZ" not in df:
        return _long(pd.DataFrame(columns=["obs_time", "t2m"]), "iem_hads", station, {"t2m": "t2m"})
    df["obs_time"] = pd.to_datetime(df["utc_valid"], utc=True)
    df["t2m"] = f_to_c(pd.to_numeric(df["TAIRGZZ"], errors="coerce"))
    return _long(df, "iem_hads", station, {"t2m": "t2m"})


def parse_ndbc_stdmet(text: str, station: str) -> pd.DataFrame:
    """NDBC stdmet text: two header lines, whitespace columns, 99/999 or MM for missing."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("#"):
        return _long(pd.DataFrame(columns=["obs_time", "t2m"]), "ndbc", station, {"t2m": "t2m"})
    names = lines[0].lstrip("#").split()
    rows = [ln.split() for ln in lines[2:] if not ln.startswith("#")]
    df = pd.DataFrame(rows, columns=names).replace("MM", np.nan).apply(pd.to_numeric, errors="coerce")
    df["obs_time"] = pd.to_datetime(
        dict(year=df["YY"], month=df["MM"], day=df["DD"], hour=df["hh"], minute=df["mm"]), utc=True
    )
    for col, missing in [("ATMP", 999.0), ("DEWP", 999.0), ("WSPD", 99.0), ("GST", 99.0), ("WDIR", 999.0)]:
        df.loc[df[col] >= missing, col] = np.nan
    df = df.rename(columns={"ATMP": "t2m", "DEWP": "d2m", "WSPD": "wind10", "GST": "gust", "WDIR": "wdir10"})
    return _long(df, "ndbc", station, {c: c for c in ["t2m", "d2m", "wind10", "gust", "wdir10"]})


def to_arrow(df: pd.DataFrame, ingest_batch: str, ingested_at: datetime | None = None) -> pa.Table:
    df = df.copy()
    df["qc_flag"] = None
    df["ingest_batch"] = ingest_batch
    df["ingested_at"] = pd.Timestamp(ingested_at or datetime.now(UTC))
    return pa.Table.from_pandas(df[OBS_SCHEMA.column_names], schema=OBS_SCHEMA.as_arrow(), preserve_index=False)


# --- fetchers -----------------------------------------------------------------


def _get_text(url: str, params: dict | list | None = None) -> str | None:
    r = requests.get(url, params=params, timeout=120)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    if url.endswith(".gz") and r.content[:2] == b"\x1f\x8b":
        return gzip.decompress(r.content).decode()
    return r.text


def fetch_iem_asos(station: str, start: date, end: date) -> str:
    sid = station[1:] if len(station) == 4 and station.startswith("K") else station
    params = [("station", sid), ("tz", "Etc/UTC"), ("format", "onlycomma"), ("latlon", "no")]
    params += [("missing", "empty"), ("trace", "empty"), ("report_type", "3"), ("report_type", "4")]
    params += [("data", d) for d in ["tmpf", "dwpf", "sknt", "drct", "gust"]]
    params += [("year1", start.year), ("month1", start.month), ("day1", start.day)]
    params += [("year2", end.year), ("month2", end.month), ("day2", end.day)]
    return _get_text(f"{IEM}/asos.py", params) or ""


def fetch_iem_hads(station: str, start: date, end: date) -> str:
    params = {"network": "CA_DCP", "stations": station, "what": "txt", "delim": "comma"}
    params |= {"sts": f"{start:%Y-%m-%d}T00:00Z", "ets": f"{end:%Y-%m-%d}T00:00Z"}
    return _get_text(f"{IEM}/hads.py", params) or ""


def fetch_ndbc_month(station: str, year: int, month: int) -> str | None:
    """One month of stdmet: from the yearly archive if published, else the monthly file."""
    s = station.lower()
    if text := _ndbc_year(s, year):
        return text
    code = f"{month:x}"  # NDBC names Oct-Dec a, b, c
    url = f"{NDBC}/view_text_file.php?filename={s}{code}{year}.txt.gz&dir=data/stdmet/{_MONTH_ABBR[month - 1]}/"
    text = _get_text(url)
    return text if text and text.startswith("#") else None


_year_cache: dict[tuple[str, int], str | None] = {}


def _ndbc_year(station: str, year: int) -> str | None:
    if (station, year) not in _year_cache:
        url = f"{NDBC}/view_text_file.php?filename={station}h{year}.txt.gz&dir=data/historical/stdmet/"
        text = _get_text(url)
        _year_cache[station, year] = text if text and text.startswith("#") else None
    return _year_cache[station, year]


def fetch_ndbc_realtime(station: str) -> str | None:
    return _get_text(f"{NDBC}/data/realtime2/{station.upper()}.txt")


# --- driver -------------------------------------------------------------------


def months(start: date, end: date) -> list[tuple[date, date]]:
    """[first, next-first) month windows covering [start, end)."""
    out, d = [], date(start.year, start.month, 1)
    while d < end:
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        out.append((max(d, start), min(nxt, end)))
        d = nxt
    return out


def batch_id(source: str, station: str, first: date) -> str:
    return f"{source}/{station}/{first:%Y-%m}"


def fetch_station_month(station: Station, first: date, last: date) -> pd.DataFrame:
    """Rows for [first, last) from the station's source; empty if the source has nothing."""
    if station.source == "iem_asos":
        df = parse_iem_asos(fetch_iem_asos(station.id, first, last), station.id)
    elif station.source == "iem_hads":
        df = parse_iem_hads(fetch_iem_hads(station.id, first, last), station.id)
    elif station.source == "ndbc":
        text = fetch_ndbc_month(station.id, first.year, first.month)
        df = parse_ndbc_stdmet(text, station.id) if text else parse_ndbc_stdmet("", station.id)
        if df.empty and (rt := fetch_ndbc_realtime(station.id)):
            df = parse_ndbc_stdmet(rt, station.id)
    else:
        raise ValueError(f"{station.id}: unknown source {station.source!r}")
    lo, hi = pd.Timestamp(first, tz="UTC"), pd.Timestamp(last, tz="UTC")
    return df[(df.obs_time >= lo) & (df.obs_time < hi)]


def backfill(
    stations: list[Station],
    start: date,
    end: date,
    *,
    refetch_recent_days: int = 45,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    """Append each station-month not yet in bronze.obs.

    Months that end within ``refetch_recent_days`` of today are always fetched
    again, since recent data gets filled in and corrected (the append is new
    rows with a later ``ingested_at``; silver keeps the latest).
    """
    from microcast.lake.catalog import append_bronze, recorded_batches

    done = recorded_batches("bronze.obs")
    recent = date.today() - timedelta(days=refetch_recent_days)
    stats = {"appended": 0, "skipped": 0, "empty": 0, "rows": 0}
    for st in stations:
        for first, last in months(start, end):
            bid = batch_id(st.source, st.id, first)
            if bid in done and last <= recent:
                stats["skipped"] += 1
                continue
            df = fetch_station_month(st, first, last)
            if df.empty:
                stats["empty"] += 1
                log(f"  {bid}: no data")
                continue
            append_bronze("bronze.obs", to_arrow(df, bid), batches=[bid])
            stats["appended"] += 1
            stats["rows"] += len(df)
            log(f"  {bid}: {len(df)} rows")
    return stats
