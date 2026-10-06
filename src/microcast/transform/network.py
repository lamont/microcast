"""Silver -> gold.network_features: PurpleAir transect features per HRRR cycle.

The transect's sensors read hot inside their housings, so levels are never
used, only changes (D10): each sensor's last-hour and 3-hour change, and its
anomaly against its own trailing 14-day mean at the same hour of day (which
also removes most of the daytime solar heating). Two transect summaries: the
mean anomaly, and west minus east (Ocean Beach side vs Castro side, split at
the median longitude), which is the marine-layer push in one number.

As-of rule: PurpleAir hourly averages are stamped with the start of the hour,
so the last hour complete at a cycle's init is the one starting an hour
earlier. That is visible well before the issue time (init + 55 min).
"""

from __future__ import annotations

import duckdb
import pandas as pd
import pyarrow as pa
from pyiceberg.expressions import AlwaysTrue, EqualTo

from microcast.lake.catalog import get_catalog, replace_partition
from microcast.lake.schemas import NETWORK_FEATURES_SCHEMA

FEATURES_SQL = """
WITH t AS (
    SELECT station_id AS s, time_bucket(INTERVAL 1 HOUR, obs_time) AS hour, avg(value) AS v
    FROM obs
    WHERE variable = 't_sensor' AND qc_flag = 'ok' AND station_id IN (SELECT s FROM sensors)
    GROUP BY ALL
),
lagged AS (
    -- One row per sensor and complete hour; joins rather than LAG so missing hours stay missing.
    SELECT t.s, t.hour, t.v,
        t.v - h1.v AS d1h,
        t.v - h3.v AS d3h,
        t.v - avg(t.v) OVER (
            PARTITION BY t.s, hour(t.hour) ORDER BY t.hour
            RANGE BETWEEN INTERVAL 14 DAY PRECEDING AND INTERVAL 1 HOUR PRECEDING) AS anom
    FROM t
    LEFT JOIN t h1 ON h1.s = t.s AND h1.hour = t.hour - INTERVAL 1 HOUR
    LEFT JOIN t h3 ON h3.s = t.s AND h3.hour = t.hour - INTERVAL 3 HOUR
),
per_cycle AS (
    -- The hour starting at init - 1 h is the last one complete at init.
    SELECT l.hour + INTERVAL 1 HOUR AS init_time, l.s, l.d1h, l.d3h, l.anom, sensors.west
    FROM lagged l JOIN sensors USING (s)
),
long AS (
    SELECT init_time, 'pa_' || replace(s, 'pa_', '') || '_d1h' AS feature, d1h AS value FROM per_cycle
    UNION ALL SELECT init_time, 'pa_' || replace(s, 'pa_', '') || '_d3h', d3h FROM per_cycle
    UNION ALL SELECT init_time, 'pa_' || replace(s, 'pa_', '') || '_anom', anom FROM per_cycle
    UNION ALL SELECT init_time, 'pa_mean_anom', avg(anom) FROM per_cycle GROUP BY init_time
    UNION ALL SELECT init_time, 'pa_west_east_anom',
        avg(anom) FILTER (WHERE west) - avg(anom) FILTER (WHERE NOT west)
        FROM per_cycle GROUP BY init_time
)
SELECT init_time, feature, value FROM long WHERE value IS NOT NULL ORDER BY init_time, feature
"""


def build_features(obs: pa.Table, sensors: dict[str, float]) -> pa.Table:
    """``sensors``: station id -> longitude, for the west/east split."""
    lons = sorted(sensors.values())
    median = lons[len(lons) // 2]
    con = duckdb.connect()
    con.register("obs", obs)
    con.register("sensors", pa.table({"s": list(sensors), "west": [lon < median for lon in sensors.values()]}))
    return con.sql(FEATURES_SQL).to_arrow_table().cast(NETWORK_FEATURES_SCHEMA.as_arrow())


def rebuild(sensors: dict[str, float]) -> int:
    """Rebuild gold.network_features in full from silver; returns the row count."""
    obs = get_catalog().load_table("silver.obs_qc").scan(row_filter=EqualTo("source", "purpleair")).to_arrow()
    out = build_features(obs, sensors)
    replace_partition("gold.network_features", out, AlwaysTrue())
    return out.num_rows


def wide(long: pa.Table) -> pd.DataFrame:
    """One row per init_time, one column per feature."""
    return long.to_pandas().pivot(index="init_time", columns="feature", values="value").reset_index()
