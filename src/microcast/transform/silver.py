"""Bronze -> silver: dedupe, clean, align. Each build replaces whole partitions.

* ``silver.obs_qc``: latest ``ingested_at`` per natural key, range checks.
* ``silver.nwp_aligned``: one wide row per (model, cycle, lead, point). The 4
  neighbour cells are combined by inverse-distance weighting; u/v are rotated
  from HRRR's grid-relative frame to earth-relative per cell first.

Both are rebuilt a month at a time (``overwrite`` with a time-range filter),
which is also what compacts bronze's many small files into a few large ones.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

import duckdb
import pyarrow as pa
from pyiceberg.expressions import And, GreaterThanOrEqual, LessThan

from microcast.ingest.obs import months
from microcast.lake.catalog import get_catalog, replace_partition
from microcast.lake.schemas import NWP_ALIGNED_SCHEMA, OBS_QC_SCHEMA

# HRRR Lambert conformal: true latitude 38.5N, orientation longitude 97.5W.
# Earth-relative wind = grid-relative rotated by sin(38.5) * (lon - lov).
HRRR_ROTCON = 0.6225146366376195
HRRR_LOV = -97.5

# Physical sanity bounds, SI. Values outside are kept but flagged.
RANGES = {
    "t2m": (-15, 50),
    "d2m": (-40, 35),
    "wind10": (0, 60),
    "gust": (0, 80),
    "wdir10": (0, 360),
    # PurpleAir PMS5003 channels: the counter tops out near 1000 ug/m3. A fouled channel
    # reads in the thousands (the outdoor sensor's B did, 2026-10-05) and is flagged, leaving A alone.
    "pm25_a": (0, 1000),
    "pm25_b": (0, 1000),
    "rh_sensor": (0, 100),
    "solar": (0, 1400),  # W/m2; clear-sky noon at SF peaks near 1000
}

_BRONZE_NWP_COLS = (
    "model",
    "init_time",
    "lead_min",
    "valid_time",
    "point_id",
    "variable",
    "level",
    "member",
    "k",
    "value",
    "grid_lon",
    "grid_dist_m",
    "ingested_at",
    "product",
)


def _range(col: str, lo: datetime, hi: datetime):
    return And(GreaterThanOrEqual(col, lo.isoformat()), LessThan(col, hi.isoformat()))


def _utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


NWP_ALIGNED_SQL = f"""
WITH latest AS (
    SELECT * FROM bronze
    WHERE member = 0
    QUALIFY row_number() OVER (
        PARTITION BY model, product, init_time, lead_min, point_id, variable, level, member, k
        ORDER BY ingested_at DESC) = 1
),
named AS (
    SELECT model, init_time, lead_min, valid_time, point_id, k, grid_lon, grid_dist_m, ingested_at, value,
        CASE
            WHEN variable = 'gh' AND level = 'cloudBase:0' THEN 'cloud_base'
            WHEN variable = 'gh' AND level = 'cloudCeiling:0' THEN 'ceiling'
            WHEN variable = 'r2' THEN 'rh2m'
            WHEN variable = 'sdswrf' THEN 'dswrf'
            ELSE variable
        END AS field
    FROM latest
),
cell AS (
    PIVOT named ON field IN ('t2m', 'd2m', 'rh2m', 'u10', 'v10', 'gust', 'prate', 'vis', 'lcc', 'tcc',
                             'cloud_base', 'ceiling', 'dswrf')
    USING first(value)
    GROUP BY model, init_time, lead_min, valid_time, point_id, k, grid_lon, grid_dist_m
),
earth AS (
    SELECT *,
        radians({HRRR_ROTCON} * (grid_lon - ({HRRR_LOV}))) AS ang,
        cos(ang) * u10 + sin(ang) * v10 AS ue,
        -sin(ang) * u10 + cos(ang) * v10 AS ve,
        1.0 / greatest(grid_dist_m, 100.0) AS w
    FROM cell
),
idw AS (
    SELECT model, init_time, lead_min, valid_time, point_id,
        sum(w * (t2m - 273.15)) / sum(w) FILTER (WHERE t2m IS NOT NULL) AS t2m,
        sum(w * (d2m - 273.15)) / sum(w) FILTER (WHERE d2m IS NOT NULL) AS d2m,
        sum(w * rh2m) / sum(w) FILTER (WHERE rh2m IS NOT NULL) AS rh2m,
        sum(w * ue) / sum(w) FILTER (WHERE ue IS NOT NULL) AS u10,
        sum(w * ve) / sum(w) FILTER (WHERE ve IS NOT NULL) AS v10,
        sum(w * sqrt(ue * ue + ve * ve)) / sum(w) FILTER (WHERE ue IS NOT NULL) AS wind10,
        sum(w * gust) / sum(w) FILTER (WHERE gust IS NOT NULL) AS gust,
        sum(w * prate) / sum(w) FILTER (WHERE prate IS NOT NULL) AS prate,
        sum(w * vis) / sum(w) FILTER (WHERE vis IS NOT NULL) AS vis,
        sum(w * lcc) / sum(w) FILTER (WHERE lcc IS NOT NULL) AS lcc,
        sum(w * tcc) / sum(w) FILTER (WHERE tcc IS NOT NULL) AS tcc,
        sum(w * cloud_base) / sum(w) FILTER (WHERE cloud_base IS NOT NULL) AS cloud_base,
        sum(w * ceiling) / sum(w) FILTER (WHERE ceiling IS NOT NULL) AS ceiling,
        sum(w * dswrf) / sum(w) FILTER (WHERE dswrf IS NOT NULL) AS dswrf,
        max(t2m) - min(t2m) AS t2m_spread,
        max(gust) - min(gust) AS gust_spread
    FROM earth
    GROUP BY ALL
),
ingest AS (
    SELECT model, init_time, lead_min, point_id, max(ingested_at) AS ingested_at
    FROM latest GROUP BY ALL
)
SELECT idw.*,
    (degrees(atan2(-idw.u10, -idw.v10)) + 360) % 360 AS wdir10,  -- direction the wind blows from
    ingest.ingested_at
FROM idw JOIN ingest USING (model, init_time, lead_min, point_id)
"""

OBS_QC_SQL = """
WITH latest AS (
    SELECT * FROM bronze
    QUALIFY row_number() OVER (
        PARTITION BY source, station_id, obs_time, variable ORDER BY ingested_at DESC) = 1
)
SELECT source, station_id, obs_time, variable, value,
    CASE
        WHEN value IS NULL OR isnan(value) THEN 'missing'
        WHEN qc_flag IN ('B', 'X') THEN 'source_rejected'  -- MADIS QC: bad / rejected
        WHEN value < r.lo OR value > r.hi THEN 'range'
        ELSE 'ok'
    END AS qc_flag,
    ingested_at
FROM latest LEFT JOIN ranges r USING (variable)
"""


def _conform(con: duckdb.DuckDBPyConnection, sql: str, schema) -> pa.Table:
    return con.sql(sql).to_arrow_table().select(schema.column_names)


def build_nwp_aligned(bronze: pa.Table) -> pa.Table:
    con = duckdb.connect()
    con.register("bronze", bronze)
    return _conform(con, NWP_ALIGNED_SQL, NWP_ALIGNED_SCHEMA)


def build_obs_qc(bronze: pa.Table) -> pa.Table:
    con = duckdb.connect()
    con.register("bronze", bronze)
    ranges = pa.table(
        {"variable": list(RANGES), "lo": [lo for lo, _ in RANGES.values()], "hi": [hi for _, hi in RANGES.values()]}
    )
    con.register("ranges", ranges)
    return _conform(con, OBS_QC_SQL, OBS_QC_SCHEMA)


def rebuild(start: date, end: date, log: Callable[[str], None] = print) -> None:
    """Rebuild silver.nwp_aligned and silver.obs_qc for every month in [start, end)."""
    catalog = get_catalog()
    nwp, obs = catalog.load_table("bronze.nwp_point"), catalog.load_table("bronze.obs")
    for first, last in months(start, end):
        lo, hi = _utc(first), _utc(last)
        b = nwp.scan(row_filter=_range("init_time", lo, hi), selected_fields=_BRONZE_NWP_COLS).to_arrow()
        if b.num_rows:
            out = build_nwp_aligned(b)
            replace_partition("silver.nwp_aligned", out, _range("init_time", lo, hi))
            log(f"  silver.nwp_aligned {first:%Y-%m}: {b.num_rows} bronze -> {out.num_rows} rows")
        o = obs.scan(row_filter=_range("obs_time", lo, hi)).to_arrow()
        if o.num_rows:
            out = build_obs_qc(o)
            replace_partition("silver.obs_qc", out, _range("obs_time", lo, hi))
            log(f"  silver.obs_qc {first:%Y-%m}: {o.num_rows} bronze -> {out.num_rows} rows")
