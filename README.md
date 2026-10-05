# microcast

Hyperlocal weather forecasts for a handful of named places and routes in San
Francisco. Microcast ingests hourly NWP runs (HRRR first; RRFS, REFS and GFS
later) and neighbourhood sensors, learns each model's local error, and turns
the result into plain-language alerts ("the marine layer reaches the house
around 6 pm", "leave by 1:30 for a tailwind home").

The forecasts are the test case. The product is the platform: raw data →
Iceberg lake → features → competing models → backtests → live inference, all
reproducible.

- [Design doc](docs/design.md): the original design.
- [Decisions](docs/decisions.md): changes since then (append-only storage,
  cloud-cover target). These take precedence.
- [Phase 1 spec](docs/specs/phase-1.md): what's built and what's next.

## Quick start

```sh
uv sync --all-extras
cp .env.example .env          # set MICROCAST_HOME_LAT / _LON
uv run pytest
uv run microcast points       # registry virtual points
uv run microcast lake init    # local Iceberg catalog under ./data
uv run microcast ingest nwp hrrr --init latest-3 --leads 0-6
uv run microcast lake snapshots bronze.nwp_point
```

Each ingest batch appends new Parquet files and a new Iceberg snapshot to
`bronze`, which is append-only. Silver and gold rebuild whole partitions into
fewer, larger files. To move from local disk to Lakekeeper and S3/MinIO, set
the `PYICEBERG_CATALOG__MICROCAST__*` variables; no code changes.

## Layout

```
config/            places.yaml (registry), models.yaml (NWP products + GRIB search)
routes/examples/   example route GeoJSON; real routes go in routes/private/ (ignored)
src/microcast/
  registry.py      places/routes → virtual points with bearings
  lake/            Iceberg schemas, catalog, append-only bronze writer
  ingest/nwp.py    Herbie subset → pick_points(k=4) → long Arrow
  cli.py           `microcast …`
tests/             offline tests (synthetic pick_points output, temp catalogs)
docs/              design, decisions, specs
```

## Privacy

Home coordinates live in `.env`, and private routes in `routes/private/` with
`config/places.local.yaml`. All three are git-ignored.
