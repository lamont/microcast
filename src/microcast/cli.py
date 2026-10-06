"""`microcast` command line: thin wrappers over library functions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer

from microcast import registry as registry_mod

app = typer.Typer(no_args_is_help=True, help="Hyperlocal forecast post-processing.")
lake_app = typer.Typer(no_args_is_help=True, help="Iceberg lake management.")
ingest_app = typer.Typer(no_args_is_help=True, help="Ingest data into bronze.")
backfill_app = typer.Typer(no_args_is_help=True, help="Backfill history into bronze (resumable).")
build_app = typer.Typer(no_args_is_help=True, help="Rebuild silver and gold from the layer below.")
app.add_typer(lake_app, name="lake")
app.add_typer(ingest_app, name="ingest")
app.add_typer(backfill_app, name="backfill")
app.add_typer(build_app, name="build")


def _parse_leads(spec: str | None) -> list[int] | None:
    if not spec:
        return None
    if "-" in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in spec.split(",")]


def _parse_init(value: str) -> datetime:
    """ISO time or 'latest-N' (N hours ago, floored to the hour)."""
    if value.startswith("latest"):
        hours = int(value.split("-", 1)[1]) if "-" in value else 2
        t = datetime.now(UTC) - timedelta(hours=hours)
        return t.replace(minute=0, second=0, microsecond=0)
    t = datetime.fromisoformat(value)
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


@app.command()
def points() -> None:
    """List every virtual point in the registry."""
    reg = registry_mod.load()
    for vp in reg.virtual_points():
        bearing = f"{vp.bearing_deg:5.1f}°" if vp.bearing_deg is not None else "     -"
        typer.echo(f"{vp.point_id:24s} {vp.lat:9.5f} {vp.lon:10.5f} {bearing}")


@lake_app.command("init")
def lake_init() -> None:
    """Create namespaces and tables (idempotent)."""
    from microcast.lake.catalog import init_lake

    created = init_lake()
    typer.echo("created: " + (", ".join(created) if created else "nothing, lake already initialised"))


@lake_app.command("snapshots")
def lake_snapshots(table: str = typer.Argument("bronze.nwp_point")) -> None:
    """Show a table's snapshot history."""
    from microcast.lake.catalog import get_catalog

    t = get_catalog().load_table(table)
    for s in t.snapshots():
        ts = datetime.fromtimestamp(s.timestamp_ms / 1000, UTC)
        summary = s.summary.additional_properties if s.summary else {}
        typer.echo(f"{s.snapshot_id}  {ts:%Y-%m-%d %H:%M:%S}Z  +{summary.get('added-records', '?')} rows")


@lake_app.command("copy")
def lake_copy(
    to: str = typer.Option(
        ..., help="Target catalog name, configured via PYICEBERG_CATALOG__<NAME>__* or ~/.pyiceberg.yaml"
    ),
) -> None:
    """Copy bronze row for row into another catalog (e.g. the cluster's Lakekeeper). Resumable."""
    from pyiceberg.catalog import load_catalog

    from microcast.lake.catalog import get_catalog
    from microcast.lake.copy import copy_bronze

    rows = copy_bronze(get_catalog(), load_catalog(to), log=typer.echo)
    typer.echo(", ".join(f"{k} +{v}" for k, v in rows.items()) + "; now run `microcast build silver/gold` there")


@ingest_app.command("nwp")
def ingest_nwp(
    model: str = typer.Argument(..., help="Key in config/models.yaml, e.g. hrrr"),
    init: str = typer.Option("latest-2", help="Cycle time (ISO, UTC) or latest-N hours"),
    leads: str = typer.Option(None, help="'0-6' or '0,1,3'; default: all configured leads"),
) -> None:
    """Fetch one cycle and append it to bronze.nwp_point."""
    from microcast.ingest.nwp import fetch_cycle, load_models
    from microcast.lake.catalog import append_bronze, init_lake

    cfg = load_models()[model]
    init_dt = _parse_init(init)
    init_lake()
    table = fetch_cycle(cfg, init_dt, registry_mod.load(), leads=_parse_leads(leads))
    snap = append_bronze("bronze.nwp_point", table)
    typer.echo(f"{model} {init_dt:%Y-%m-%dT%HZ}: {table.num_rows} rows -> snapshot {snap}")


@ingest_app.command("obs")
def ingest_obs(
    start: str = typer.Option("2025-04-01", help="First day (ISO date)"),
    end: str = typer.Option(None, help="Stop before this day (ISO date); default tomorrow"),
    station: Annotated[list[str] | None, typer.Option(help="Station id(s) from the registry; default all")] = None,
) -> None:
    """Station observations (IEM ASOS/HADS, NDBC) into bronze.obs, a month per batch."""
    from datetime import date

    from microcast.ingest.obs import backfill
    from microcast.lake.catalog import init_lake

    init_lake()
    archive_sources = {"iem_asos", "iem_hads", "ndbc"}  # Synoptic has its own command
    stations = [
        s for s in registry_mod.load().stations if s.source in archive_sources and (not station or s.id in station)
    ]
    last = date.fromisoformat(end) if end else date.today() + timedelta(days=1)
    stats = backfill(stations, date.fromisoformat(start), last, log=typer.echo)
    typer.echo(", ".join(f"{k} {v}" for k, v in stats.items()))


@ingest_app.command("purpleair")
def ingest_purpleair(
    history_days: int = typer.Option(0, help="Backfill N days via the PurpleAir API (needs PURPLEAIR_API_KEY)"),
) -> None:
    """Own PurpleAir sensor: one LAN reading (default), or API history."""
    from microcast.ingest import purpleair
    from microcast.ingest.obs import to_arrow
    from microcast.lake.catalog import append_bronze, init_lake

    init_lake()
    now = datetime.now(UTC)
    if history_days:
        index = int(registry_mod.load().sensors["purpleair_home"]["sensor_index"])
        for d in range(history_days, 0, -2):  # the API returns ~2 days of 10-min averages per call
            start, end = now - timedelta(days=d), now - timedelta(days=max(d - 2, 0))
            df = purpleair.fetch_history(index, start, end)
            bid = f"purpleair/history/{start:%Y-%m-%dT%H}"
            append_bronze("bronze.obs", to_arrow(df, bid), batches=[bid])
            typer.echo(f"  {bid}: {len(df)} rows")
    else:
        df = purpleair.fetch_local()
        bid = f"purpleair/local/{now:%Y-%m-%dT%H:%M}"
        append_bronze("bronze.obs", to_arrow(df, bid), batches=[bid])
        typer.echo(f"{bid}: {len(df)} rows")


@ingest_app.command("synoptic")
def ingest_synoptic(days: int = typer.Option(7, help="Days back to pull (free tier: at most 7)")) -> None:
    """Synoptic stations from the registry: the last N days into bronze.obs. Run daily."""
    from microcast.ingest import synoptic
    from microcast.ingest.obs import to_arrow
    from microcast.lake.catalog import append_bronze, init_lake

    init_lake()
    ids = [s.id for s in registry_mod.load().stations if s.source == "synoptic"]
    df = synoptic.fetch(ids, days)
    bid = f"synoptic/{datetime.now(UTC):%Y-%m-%dT%H:%M}"
    append_bronze("bronze.obs", to_arrow(df, bid), batches=[bid])
    counts = df.groupby("station_id").size().to_dict()
    typer.echo(f"{bid}: {len(df)} rows " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


@backfill_app.command("madis")
def backfill_madis(
    start: str = typer.Option(..., help="First hour (ISO, UTC)"),
    end: str = typer.Option(..., help="Stop before this hour (ISO, UTC)"),
    workers: int = typer.Option(4, help="Parallel hourly downloads (~30 MB each)"),
) -> None:
    """MADIS mesonet history (CWOP, PG&E, ...) inside the network box into bronze.obs."""
    from microcast.ingest.madis import backfill
    from microcast.lake.catalog import init_lake

    init_lake()
    stats = backfill(_parse_init(start), _parse_init(end), workers=workers, log=typer.echo)
    typer.echo(", ".join(f"{k} {v}" for k, v in stats.items()))


@backfill_app.command("purpleair")
def backfill_purpleair(
    start: str = typer.Option("2025-04-01", help="First hour (ISO, UTC)"),
    end: str = typer.Option("latest-0", help="Stop before this hour (ISO, UTC)"),
    floor: int = typer.Option(750_000, help="Stop once the key has this many points left"),
    load: bool = typer.Option(False, help="Append the cached responses to bronze (no API calls)"),
) -> None:
    """Public PurpleAir sensors on the transect: hourly temperature, cached under data/purpleair/history/."""
    from microcast import settings
    from microcast.ingest import purpleair

    cache = settings.data_dir() / "purpleair" / "history"
    network = {k: v for k, v in registry_mod.load().sensors.items() if v.get("kind") == "purpleair_network"}
    if not load:
        sensors = {k: int(v["sensor_index"]) for k, v in network.items()}
        stats = purpleair.backfill_network(
            sensors, _parse_init(start), _parse_init(end), cache, floor=floor, log=typer.echo
        )
        typer.echo(", ".join(f"{k} {v}" for k, v in stats.items()))
        return

    import pandas as pd
    import pyarrow as pa

    from microcast.ingest.obs import to_arrow
    from microcast.lake.catalog import append_bronze, init_lake, recorded_batches
    from microcast.lake.schemas import STATIONS_SCHEMA

    init_lake()
    done = recorded_batches("bronze.obs")
    # Only sensors still in the registry; a dropped sensor's cached responses stay on disk.
    todo = [(bid, df) for bid, df in purpleair.load_cached(cache) if bid not in done and bid.split("/")[2] in network]
    if not todo:
        typer.echo("nothing new to load")
        return
    obs = pa.concat_tables([to_arrow(df, bid) for bid, df in todo])
    append_bronze("bronze.obs", obs, batches=[bid for bid, _ in todo])
    now = pd.Timestamp(datetime.now(UTC))
    bid = f"purpleair/stations/{now:%Y-%m-%dT%H:%M}"
    stations = pd.DataFrame(
        [
            {
                "source": purpleair.SOURCE,
                "station_id": k,
                "provider": "PurpleAir",
                "lat": float(v["lat"]),
                "lon": float(v["lon"]),
                "elevation_m": None,
                "ingest_batch": bid,
                "ingested_at": now,
            }
            for k, v in network.items()
        ]
    )
    append_bronze(
        "bronze.stations",
        pa.Table.from_pandas(stations, schema=STATIONS_SCHEMA.as_arrow(), preserve_index=False),
        batches=[bid],
    )
    typer.echo(f"loaded {len(todo)} responses, {obs.num_rows} rows, {len(stations)} station positions")


def _network_points(registry: registry_mod.Registry) -> list[registry_mod.VirtualPoint]:
    """Every station in bronze.stations (MADIS, PurpleAir) the registry HRRR pass doesn't cover.

    The Synoptic stations count as uncovered: they joined the registry after
    the hourly fill started, so they have no HRRR rows (D10, "For code").
    """
    from microcast.lake.catalog import get_catalog

    covered = {vp.point_id for vp in registry.virtual_points()} - {
        s.id for s in registry.stations if s.source == "synoptic"
    }
    df = get_catalog().load_table("bronze.stations").scan().to_pandas()
    latest = df.sort_values("ingested_at").groupby("station_id").last()
    return [
        registry_mod.VirtualPoint(sid, sid, 0, float(r.lat), float(r.lon), None)
        for sid, r in latest.iterrows()
        if sid not in covered
    ]


@backfill_app.command("hrrr")
def backfill_hrrr(
    start: str = typer.Option(..., help="First cycle (ISO date/time, UTC)"),
    end: str = typer.Option(..., help="Stop before this cycle (ISO date/time, UTC)"),
    leads: str = typer.Option("0-6", help="'0-6' or '0,1,3'"),
    stride: int = typer.Option(1, help="Keep one cycle in N (rotating hour, see hrrr_zarr.cycles)"),
    workers: int = typer.Option(16, help="Parallel cycle fetches"),
    commit_every: int = typer.Option(48, help="Cycles per bronze append (one snapshot each)"),
    points: str = typer.Option("registry", help="'registry', or 'network': stations in bronze.stations"),
    tag: str = typer.Option("net1", help="Batch tag for a network pass; use a new one when stations are added"),
) -> None:
    """HRRR history from the hrrrzarr archive into bronze.nwp_point (kept window under data/hrrr/window/)."""
    from microcast.ingest.hrrr_zarr import backfill
    from microcast.lake.catalog import init_lake

    init_lake()
    registry = registry_mod.load()
    network = _network_points(registry) if points == "network" else None
    if network is not None:
        typer.echo(f"{len(network)} network points: {' '.join(vp.point_id for vp in network)}")
    stats = backfill(
        registry,
        _parse_init(start),
        _parse_init(end),
        _parse_leads(leads),
        stride_h=stride,
        workers=workers,
        commit_every=commit_every,
        points=network,
        tag=tag if network is not None else None,
        log=typer.echo,
    )
    typer.echo(", ".join(f"{k} {v}" for k, v in stats.items()))


@build_app.command("silver")
def build_silver(
    start: str = typer.Option("2025-04-01", help="First month (ISO date)"),
    end: str = typer.Option(None, help="Stop before this day; default tomorrow"),
) -> None:
    """Rebuild silver.nwp_aligned and silver.obs_qc month by month."""
    from datetime import date

    from microcast.lake.catalog import init_lake
    from microcast.transform import silver

    init_lake()
    last = date.fromisoformat(end) if end else date.today() + timedelta(days=1)
    silver.rebuild(date.fromisoformat(start), last, log=typer.echo)


@build_app.command("gold")
def build_gold(
    stations: str = typer.Option("network", help="'registry', or 'network': registry plus every MADIS station"),
) -> None:
    """Rebuild gold.training_examples at the truth stations.

    PurpleAir sensors are never truth: their temperature reads hot inside the
    housing (D10), so they can only be inputs.
    """
    from microcast.lake.catalog import get_catalog, init_lake
    from microcast.transform import gold

    init_lake()  # creates gold.network_features on first run
    ids = {s.id for s in registry_mod.load().stations}
    if stations == "network":
        st = get_catalog().load_table("bronze.stations").scan(selected_fields=("source", "station_id")).to_arrow()
        ids |= {r["station_id"] for r in st.to_pylist() if r["source"] == "madis"}
    typer.echo(f"{len(ids)} truth stations")
    typer.echo(f"gold.training_examples: {gold.rebuild(sorted(ids))} rows")
    from microcast.transform import network

    transect = {
        k: float(v["lon"]) for k, v in registry_mod.load().sensors.items() if v.get("kind") == "purpleair_network"
    }
    typer.echo(f"gold.network_features: {network.rebuild(transect)} rows from {len(transect)} PurpleAir sensors")


@app.command("backtest")
def backtest_cmd(
    models: str = typer.Option("raw_hrrr,bias_rolling,gbm_residual,gbm_purpleair", help="Comma-separated model names"),
    targets: str = typer.Option("t2m,gust", help="Comma-separated targets"),
) -> None:
    """Rolling monthly backtests -> gold.forecasts, gold.scores, MLflow, data/reports/."""
    from microcast.pipeline import run_backtests

    board, _ = run_backtests(models.split(","), targets.split(","), log=typer.echo)
    _print_board(board)


@app.command("compare")
def compare(point: str = typer.Option("all", help="Station id, or 'all'")) -> None:
    """Print the last backtest leaderboard (skill vs raw HRRR, 95% CI)."""
    import pandas as pd

    from microcast.pipeline import reports_dir

    _print_board(pd.read_csv(reports_dir() / "leaderboard.csv"), point)


@app.command("site")
def site_cmd(out: str = typer.Option("site", help="Output directory")) -> None:
    """Build the static status site (index + analytics) from the lake and last backtest."""
    from pathlib import Path

    from microcast.site import build

    typer.echo(f"site written to {build(Path(out))}/")


def _print_board(board, point: str = "all") -> None:
    rows = board[board.point == point].sort_values(["variable", "lead", "skill"], ascending=[True, True, False])
    typer.echo(
        f"{'target':6} {'lead':6} {'model':14} {'n':>7} {'CRPS':>6} {'MAE':>6} {'bias':>6} {'cov80':>5}  skill [95% CI]"
    )
    for r in rows.itertuples():
        typer.echo(
            f"{r.variable:6} {r.lead:6} {r.model:14} {r.n:7d} {r.crps:6.3f} {r.mae:6.3f} {r.bias:+6.2f} "
            f"{r.coverage_80:5.2f}  {r.skill:+.3f} [{r.skill_lo:+.3f}, {r.skill_hi:+.3f}]"
        )


if __name__ == "__main__":
    app()
