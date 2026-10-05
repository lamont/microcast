"""Static status site (weather.henry.st): a status page and an analytics page.

``build`` reads the lake (gold.scores, freshness of bronze/silver) and the
last backtest reports, writes one ``data.json``, and copies the page shells
from ``static/``. Pages render from ``data.json`` in the browser, so the site
is plain files any web server or bucket can host; nothing on it reveals home
coordinates (stations and place names only).
"""

from __future__ import annotations

import json
import math
import shutil
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd
from pyiceberg.expressions import EqualTo

from microcast import backtest, registry
from microcast.lake.catalog import get_catalog
from microcast.pipeline import reports_dir

STATIC = Path(__file__).parent / "static"
LOCAL_TZ = "America/Los_Angeles"
GATE_MODEL = "gbm_residual"


def _records(df: pd.DataFrame) -> list[dict]:
    def clean(v):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        if isinstance(v, pd.Timestamp):
            return v.isoformat()
        return v

    return [{k: clean(v) for k, v in row.items()} for row in df.to_dict("records")]


def freshness(con: duckdb.DuckDBPyConnection) -> dict:
    catalog = get_catalog()
    out: dict = {"tables": []}
    for ident in ["bronze.nwp_point", "bronze.obs", "silver.nwp_aligned", "silver.obs_qc", "gold.training_examples"]:
        t = catalog.load_table(ident)
        snap = t.current_snapshot()
        props = snap.summary.additional_properties if snap and snap.summary else {}
        out["tables"].append(
            dict(
                table=ident,
                rows=int(props.get("total-records", 0)),
                files=int(props.get("total-data-files", 0)),
                snapshots=len(t.snapshots()),
                updated=datetime.fromtimestamp(snap.timestamp_ms / 1000, UTC).isoformat() if snap else None,
            )
        )
    nwp = catalog.load_table("silver.nwp_aligned").scan(selected_fields=("init_time", "point_id")).to_arrow()
    con.register("nwp", nwp)
    out["hrrr"] = (
        con.sql("SELECT min(init_time) AS first, max(init_time) AS last, count(DISTINCT init_time) AS cycles FROM nwp")
        .df()
        .iloc[0]
        .to_dict()
    )
    obs = catalog.load_table("silver.obs_qc").scan(selected_fields=("station_id", "obs_time", "qc_flag")).to_arrow()
    con.register("obs", obs)
    out["stations"] = _records(
        con.sql("""
            SELECT station_id, min(obs_time) AS first, max(obs_time) AS last, count(*) AS rows,
                   avg((qc_flag <> 'ok')::int) AS flagged
            FROM obs GROUP BY 1 ORDER BY 1""").df()
    )
    out["hrrr"] = {k: (v.isoformat() if isinstance(v, pd.Timestamp) else int(v)) for k, v in out["hrrr"].items()}
    return out


def analytics(scores: pd.DataFrame) -> dict:
    """Breakdowns for the analytics page, all on backtest rows (leads 1-6)."""
    con = duckdb.connect()
    s = scores.assign(
        lead_h=scores.lead_min // 60,
        local_hour=scores.valid_time.dt.tz_convert(LOCAL_TZ).dt.hour,
        pit=backtest.norm.cdf((scores.obs - scores.mu) / scores.sigma),
    )
    con.register("s", s)
    base = "(SELECT variable, point_id, init_time, lead_min, crps AS crps_base FROM s WHERE model_name = 'raw_hrrr')"
    by_lead = con.sql(f"""
        SELECT variable, model_name AS model, lead_h, count(*) AS n, avg(crps) AS crps,
               1 - sum(crps) / sum(crps_base) AS skill, avg(abs(error)) AS mae, avg(in_p10_p90) AS coverage_80
        FROM s JOIN {base} b USING (variable, point_id, init_time, lead_min)
        GROUP BY ALL ORDER BY ALL""").df()
    by_hour = con.sql("""
        SELECT variable, model_name AS model, local_hour, count(*) AS n, avg(abs(error)) AS mae, avg(error) AS bias
        FROM s GROUP BY ALL ORDER BY ALL""").df()
    pit = con.sql("""
        SELECT variable, model_name AS model, least(floor(pit * 10), 9)::int AS bin, count(*) AS n
        FROM s WHERE pit IS NOT NULL GROUP BY ALL ORDER BY ALL""").df()
    # HRRR's own error (obs - forecast, so positive = HRRR too cold/too calm) by month and local hour.
    raw_bias = con.sql("""
        SELECT variable, point_id, strftime(timezone('America/Los_Angeles', valid_time), '%Y-%m') AS month,
               local_hour, -avg(error) AS obs_minus_hrrr, count(*) AS n
        FROM s WHERE model_name = 'raw_hrrr' GROUP BY ALL ORDER BY ALL""").df()
    return dict(by_lead=_records(by_lead), by_hour=_records(by_hour), pit=_records(pit), raw_bias=_records(raw_bias))


def gate(board: pd.DataFrame) -> list[dict]:
    """Phase 1 gate per target: the residual model's skill CI excludes 0 in both lead buckets."""
    out = []
    for variable, g in board[(board.model == GATE_MODEL) & (board.point == "all")].groupby("variable"):
        out.append(
            dict(
                variable=variable,
                passed=bool((g.skill_lo > 0).all()) and len(g) == len(backtest.LEAD_BUCKETS),
                buckets=_records(g[["lead", "skill", "skill_lo", "skill_hi", "n"]]),
            )
        )
    return out


def build(out_dir: Path) -> Path:
    reports = reports_dir()
    board = pd.read_csv(reports / "leaderboard.csv")
    monthly = pd.read_csv(reports / "monthly_skill.csv")
    meta = json.loads((reports / "backtest_meta.json").read_text())
    scores = get_catalog().load_table("gold.scores").scan(row_filter=EqualTo("kind", "backtest")).to_pandas()
    reg = registry.load()
    con = duckdb.connect()
    data = dict(
        generated_at=datetime.now(UTC).isoformat(),
        backtest=meta,
        gate=gate(board),
        leaderboard=_records(board),
        monthly=_records(monthly),
        freshness=freshness(con),
        analytics=analytics(scores),
        stations=[dict(id=s.id, source=s.source) for s in reg.stations],
        targets={"t2m": {"label": "Temperature", "unit": "°C"}, "gust": {"label": "Wind gust", "unit": "m/s"}},
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in STATIC.iterdir():
        shutil.copy(f, out_dir / f.name)
    (out_dir / "data.json").write_text(json.dumps(data, default=str))
    return out_dir
