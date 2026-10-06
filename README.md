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
- [Decisions](docs/decisions.md): changes since then (D1–D12). These take
  precedence over the design doc.
- [Phase 1 spec](docs/specs/phase-1.md): what's built and what's next.

## What exists today vs. what's planned

The [design doc](docs/design.md) describes the full system. Most of it is not
built yet. As of 2026-10-05:

**Built and running on a laptop (phase 1)**

- A registry of places, routes and truth stations, turned into ~27 virtual
  forecast points (`config/places.yaml`).
- A local Iceberg lake (PyIceberg with a SQLite catalog, Parquet files under
  `./data`) with bronze, silver and gold layers, queried through DuckDB.
- Ingest: HRRR (live via Herbie, history via the hrrrzarr archive, hourly
  cycles 2025-04 → 2026-09), station observations (IEM ASOS/HADS, NDBC), Synoptic
  stations (last 7 days only), MADIS mesonet history, and the home PurpleAir
  sensor (LAN or API).
- Three models behind one interface: raw HRRR, a rolling-bias correction, and
  a LightGBM residual model.
- Backtests on rolling monthly folds with CRPS skill and bootstrap CIs, logged
  to MLflow (SQLite file). **The phase 1 gate is passed:** the GBM residual
  beats raw HRRR at 0–6 h by about 20–40% for temperature and gust
  ([results](docs/specs/phase-1.md#results-gate-passed-2026-10-05)).
- A static status site (Status and Analytics pages) built from the lake.

**Planned, not built**

- Dagster OSS as the scheduler and backfill engine, on the home k3s cluster,
  with Postgres for Dagster and the Iceberg catalog
  ([D12](docs/decisions.md#d12--dagster-on-k3s-runs-the-collectors-not-cronjobs)).
  Until then, every collector is run by hand.
- MinIO/S3 storage and the Lakekeeper REST catalog.
- PurpleAir neighbourhood network: nine transect sensors' history is being
  pulled (D10); live polling and using them as model features are not built.
  Also a backyard weather station, and HRRR at the newer stations.
- Live inference, the forecast panel on the site, and plain-language alerts
  to Slack.
- More NWP models (RRFS, REFS, GFS), and cloud-cover forecasts.

The [phase 1 spec](docs/specs/phase-1.md) has the running checklist and the
ordered next steps.

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

Collectors, run by hand until Dagster schedules them (D12): `microcast ingest
synoptic` (at least weekly; the free tier keeps only 7 days), `microcast ingest
purpleair` (every 2 min on the home network). MADIS history: `microcast
backfill madis --start 2025-04-01` (resumable; streams ~30 MB per hour of data
and keeps the SF box). PurpleAir transect history: `microcast backfill
purpleair` (spends API points, stops at `--floor`), then `--load` into bronze. To move the lake to the cluster: `microcast lake copy --to <catalog>`,
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
  ingest/madis.py  MADIS mesonet history (CWOP, PG&E stations), streamed hourly
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

## Glossary

| Term | Meaning |
| --- | --- |
| API | Application Programming Interface |
| ASOS | Automated Surface Observing System: the airport weather stations (KSFO, KOAK) |
| CI | Confidence interval (here, from a bootstrap: resampling the scores many times) |
| CLI | Command-line interface (`microcast …`) |
| CRPS | Continuous Ranked Probability Score: error of a probabilistic forecast, in the variable's units; lower is better |
| CWOP | Citizen Weather Observer Program: volunteer home weather stations |
| DSWRF | Downward Short-Wave Radiation Flux: sunlight reaching the ground, in W/m² |
| GBM | Gradient-Boosted Machine: an ensemble of decision trees (here LightGBM) |
| GFS | Global Forecast System: NOAA's global weather model |
| GRIB | GRIdded Binary: the WMO file format NWP output is published in |
| HADS | Hydrometeorological Automated Data System: NOAA's feed for stations like SFOC1 |
| HRRR | High-Resolution Rapid Refresh: NOAA's 3 km, hourly-updated US weather model |
| IEM | Iowa Environmental Mesonet: Iowa State's archive of ASOS and HADS observations |
| k3s | A lightweight Kubernetes distribution (the "k8s" numeronym, smaller) |
| LAN | Local Area Network: the home network |
| LCC | Low Cloud Cover: HRRR's low-cloud-layer fraction |
| MADIS | Meteorological Assimilation Data Ingest System: NOAA's archive of mesonet observations |
| Mesonet | A mesoscale network: dense non-federal weather stations |
| MLflow | An open-source experiment tracker (runs, parameters, metrics, models) |
| NDBC | National Data Buoy Center: buoys and coastal stations (FTPC1, Fort Point) |
| NOAA | National Oceanic and Atmospheric Administration |
| NWP | Numerical Weather Prediction: physics-based weather models such as HRRR |
| PG&E | Pacific Gas and Electric: the utility, which runs its own weather stations (604PG) |
| PIT | Probability Integral Transform: a calibration check for probabilistic forecasts |
| PM2.5 | Particulate matter under 2.5 µm, what PurpleAir sensors measure |
| QC | Quality control (flags on suspicious observations) |
| REFS | Rapid Refresh Ensemble Forecast System: the ensemble version of RRFS |
| RH | Relative humidity |
| RRFS | Rapid Refresh Forecast System: NOAA's successor to HRRR |
| S3 | Amazon Simple Storage Service, and the object-storage API MinIO also speaks |
| SF | San Francisco |
| SQL | Structured Query Language |

Bronze, silver and gold are the lake's layers: raw data as ingested, cleaned
and aligned data, and model-ready features, forecasts and scores.
