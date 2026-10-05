"""`microcast` command line: thin wrappers over library functions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import typer

from microcast import registry as registry_mod

app = typer.Typer(no_args_is_help=True, help="Hyperlocal forecast post-processing.")
lake_app = typer.Typer(no_args_is_help=True, help="Iceberg lake management.")
ingest_app = typer.Typer(no_args_is_help=True, help="Ingest data into bronze.")
app.add_typer(lake_app, name="lake")
app.add_typer(ingest_app, name="ingest")


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


if __name__ == "__main__":
    app()
