# microcast

An experiment to use backyard sensors and public weather data to forecast SF microclimates. Mostly an excuse to play with data pipelines and weather models. 

Hyperlocal weather forecasts for a handful of named places and routes in San
Francisco. Microcast ingests hourly NWP runs (HRRR first; RRFS, REFS and GFS
later) and neighborhood sensors, learns each model's local error, and turns
the result into plain-language alerts ("the marine layer reaches the house
around 5 pm", "leave by 1:30 for a tailwind home for a bike ride to the ocean").

The forecasts are just a test case. The thing I want to build/play with is the platform: raw data →
Iceberg lake → features → competing models → backtests → live inference, all
reproducible.

- [Design doc](docs/design.md): the original design.
- [Decisions](docs/decisions.md): changes since then (append-only storage,
  cloud-cover target). These take precedence.
- [Phase 1 spec](docs/specs/phase-1.md): what's built and what's next.

## Quick start

```sh
uv sync --all-extras
cp .env.example .env          # set MICROCAST_HOME_LAT / _LON, PurpleAir index
uv run pytest
uv run microcast points       # registry virtual points
uv run microcast lake init    # local Iceberg catalog under ./data
uv run microcast ingest nwp hrrr --init latest-3 --leads 0-6
uv run microcast lake snapshots bronze.nwp_point
```

Phase 1 end to end (history → backtest → status site):

```sh
uv run microcast ingest obs --start 2025-04-01            # station truth, no keys needed
uv run microcast backfill hrrr --start 2025-04-01 --end 2026-10-01 --stride 3   # resumable
uv run microcast build silver && uv run microcast build gold
uv run microcast backtest                                 # -> gold.scores, MLflow, data/reports/
uv run microcast compare                                  # leaderboard vs raw HRRR
uv run microcast site --out site                          # static pages for weather.henry.st
python -m http.server -d site 8000
```

Collectors (run on a schedule): `microcast ingest synoptic` (daily; the free
tier keeps only 7 days), `microcast ingest purpleair` (every 2 min on the home
network). To move the lake to the cluster: `microcast lake copy --to <catalog>`,
then rebuild silver and gold there (decisions D11).

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
  ingest/nwp.py    Herbie subset → pick_points(k=4) → long Arrow (live)
  ingest/hrrr_zarr.py  HRRR history from the hrrrzarr archive (backfill)
  ingest/obs.py    station truth: IEM ASOS/HADS, NDBC
  ingest/purpleair.py  own PurpleAir sensor (LAN or API)
  ingest/synoptic.py   Synoptic/CWOP stations (last 7 days, accrues)
  lake/copy.py     copy bronze to another catalog (laptop -> cluster)
  transform/       bronze → silver → gold (DuckDB SQL)
  models/          Forecaster zoo: raw_hrrr, bias_rolling, gbm_residual
  backtest/        folds, CRPS, skill + bootstrap CI, leaderboard
  pipeline.py      backtest orchestration + MLflow
  site/            static status site (weather.henry.st)
  cli.py           `microcast …`
tests/             offline tests (synthetic pick_points output, temp catalogs)
docs/              design, decisions, specs
```

## Privacy

Home coordinates and the PurpleAir sensor index live in `.env`, and private
routes in `routes/private/` with `config/places.local.yaml`. All are
git-ignored. The status site shows station and place ids and scores only.
