"""Silver -> gold.training_examples, with as-of features.

One row per (station point, cycle, lead). The as-of rule (design doc,
Backtesting): a feature may only use data visible at ``issue_time``. For HRRR
history ``issue_time = init_time + 55 min`` (when f00-f06 are usually out); an
observation is visible 5 minutes after it was taken.

Observations are first snapped to the hour: the report nearest the top of the
hour within +-20 minutes (ASOS routine reports are at :56, SFOC1 at :43, NDBC
every 6-10 minutes). ``gust`` is the reported gust, else the sustained wind:
ASOS only reports a gust when it is gusting, and a calm hour's gust is its wind.
"""

from __future__ import annotations

import duckdb
import pyarrow as pa
from pyiceberg.expressions import AlwaysTrue, EqualTo

from microcast.lake.catalog import get_catalog, replace_partition
from microcast.lake.schemas import NWP_FIELDS, TRAINING_SCHEMA

HRRR_ISSUE_DELAY_MIN = 55
OBS_DELAY_MIN = 5
BIAS_WINDOW_DAYS = 14

_nwp_cols = ",\n    ".join(f"n.{f} AS nwp_{f}" for f in NWP_FIELDS)

_TRAINING_SQL = f"""
WITH obs_snap AS (
    SELECT station_id, variable, value, obs_time,
        time_bucket(INTERVAL 1 HOUR, obs_time + INTERVAL 30 MINUTE) AS hour
    FROM obs WHERE qc_flag = 'ok'
),
obs_near AS (
    SELECT * FROM obs_snap
    WHERE abs(epoch(obs_time) - epoch(hour)) <= 20 * 60
    QUALIFY row_number() OVER (
        PARTITION BY station_id, variable, hour ORDER BY abs(epoch(obs_time) - epoch(hour)), obs_time) = 1
),
obs_h AS (
    SELECT station_id AS point_id, hour,
        first(value) FILTER (WHERE variable = 't2m') AS t2m,
        first(value) FILTER (WHERE variable = 'wind10') AS wind10,
        first(value) FILTER (WHERE variable = 'gust') AS gust_reported
    FROM obs_near GROUP BY ALL
),
obs_hour AS (
    SELECT point_id, hour, t2m,
        CASE WHEN wind10 IS NULL THEN NULL ELSE greatest(coalesce(gust_reported, wind10), wind10) END AS gust
    FROM obs_h
),
base AS (
    SELECT n.point_id, n.init_time,
        n.init_time + INTERVAL {HRRR_ISSUE_DELAY_MIN} MINUTE AS issue_time,
        n.lead_min, n.valid_time,
        {_nwp_cols},
        sin(2 * pi() * hour(n.valid_time) / 24) AS hour_sin,
        cos(2 * pi() * hour(n.valid_time) / 24) AS hour_cos,
        sin(2 * pi() * dayofyear(n.valid_time) / 365.25) AS doy_sin,
        cos(2 * pi() * dayofyear(n.valid_time) / 365.25) AS doy_cos,
        tgt.t2m AS obs_t2m, tgt.gust AS obs_gust,
        -- Latest hourly obs at or before the cycle time: reported by init + 20 min at the
        -- latest, so visible well before issue_time.
        last_.t2m AS obs_last_t2m, last_.gust AS obs_last_gust
    FROM nwp n
    LEFT JOIN obs_hour tgt ON tgt.point_id = n.point_id AND tgt.hour = n.valid_time
    LEFT JOIN obs_hour last_ ON last_.point_id = n.point_id AND last_.hour = n.init_time
    WHERE n.model = 'hrrr'
),
with_init AS (
    -- HRRR's own error at the cycle time: obs at init minus this cycle's f00.
    SELECT b.*,
        b.obs_last_t2m - f0.nwp_t2m AS err_at_init_t2m,
        b.obs_last_gust - f0.nwp_gust AS err_at_init_gust
    FROM base b
    LEFT JOIN base f0 ON f0.point_id = b.point_id AND f0.init_time = b.init_time AND f0.lead_min = 0
),
resid AS (
    SELECT point_id, init_time, lead_min, valid_time,
        obs_t2m - nwp_t2m AS r_t2m, obs_gust - nwp_gust AS r_gust
    FROM base
),
rolling AS (
    -- Trailing error at the same point and lead, over residuals whose truth was
    -- visible before issue_time: one window per lead (see _rolling_sql).
    {{rolling}}
)
SELECT w.*, rolling.* EXCLUDE (point_id, init_time, lead_min)
FROM with_init w
LEFT JOIN rolling USING (point_id, init_time, lead_min)
ORDER BY point_id, init_time, lead_min
"""


def _rolling_sql(lead_min: int) -> str:
    """Rolling bias for one lead as a range window over valid_time.

    Within a lead, issue_time = valid_time - lead + 55 min, so the as-of window
    (truth visible by issue_time, within the last 14 days) is a fixed range
    relative to each row's valid_time. A window instead of a self-join keeps
    this linear in rows: the join produced ~336 pairs per row.
    """
    # visible: r.valid + 5 min <= issue  <=>  r.valid <= valid - (lead - 50 min)
    hi = lead_min - (HRRR_ISSUE_DELAY_MIN - OBS_DELAY_MIN)
    hi_sql = f"INTERVAL {hi} MINUTE PRECEDING" if hi >= 0 else f"INTERVAL {-hi} MINUTE FOLLOWING"
    # recent: r.valid > issue - 14 days  <=>  r.valid >= valid - (14 days + lead - 55 min) + 1 s
    lo_s = (BIAS_WINDOW_DAYS * 1440 + lead_min - HRRR_ISSUE_DELAY_MIN) * 60 - 1
    w = f"(PARTITION BY point_id ORDER BY valid_time RANGE BETWEEN INTERVAL {lo_s} SECOND PRECEDING AND {hi_sql})"
    return f"""SELECT point_id, init_time, lead_min,
        avg(r_t2m) OVER {w} AS bias14_t2m, stddev_samp(r_t2m) OVER {w} AS std14_t2m,
        avg(r_gust) OVER {w} AS bias14_gust, stddev_samp(r_gust) OVER {w} AS std14_gust
    FROM resid WHERE lead_min = {lead_min}"""


def training_sql(leads: list[int]) -> str:
    return _TRAINING_SQL.replace("{rolling}", "\n    UNION ALL\n    ".join(_rolling_sql(m) for m in sorted(leads)))


def build_training(nwp: pa.Table, obs: pa.Table) -> pa.Table:
    con = duckdb.connect()
    con.register("nwp", nwp)
    con.register("obs", obs)
    leads = pa.compute.unique(nwp["lead_min"]).to_pylist() or [0]
    return (
        con.sql(training_sql(leads))
        .to_arrow_table()
        .select(TRAINING_SCHEMA.column_names)
        .cast(TRAINING_SCHEMA.as_arrow())
    )


def rebuild(station_ids: list[str]) -> int:
    """Rebuild gold.training_examples in full from silver; returns the row count."""
    catalog = get_catalog()
    nwp = catalog.load_table("silver.nwp_aligned").scan(row_filter=EqualTo("model", "hrrr")).to_arrow()
    nwp = nwp.filter(pa.compute.is_in(nwp["point_id"], pa.array(station_ids)))
    obs = catalog.load_table("silver.obs_qc").scan().to_arrow()
    out = build_training(nwp, obs)
    replace_partition("gold.training_examples", out, AlwaysTrue())
    return out.num_rows
