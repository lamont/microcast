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
- [Decisions](docs/decisions.md): changes since then, numbered D1–D12 (the
  "D" ids cited below). These take precedence over the design doc.
- [Phase 1 spec](docs/specs/phase-1.md): what's built and what's next.

## What exists today vs. what's planned

The [design doc](docs/design.md) describes the full system. Most of it is not
built yet. As of 2026-10-05:

**Built and running on a laptop (phase 1)**

- A registry of places, routes and truth stations, turned into ~27 virtual
  forecast points (`config/places.yaml`).
- A local Iceberg lake (PyIceberg with a SQLite catalog, Parquet files under
  `./data`) with bronze, silver and gold layers, queried through DuckDB.
- Ingest from nine working data sources (table below) into the lake's bronze layer.
- Three models behind one interface: raw HRRR, a rolling-bias correction, and
  a LightGBM residual model.
- Backtests on rolling monthly folds with CRPS skill and bootstrap CIs, logged
  to MLflow (SQLite file). **The phase 1 gate is passed:** the GBM residual
  beats raw HRRR at 0–6 h by about 31–42% for temperature and 21–25% for
  gust on hourly cycles
  ([results](docs/specs/phase-1.md#results-on-hourly-cycles-2026-10-05-evening)).
- A static status site (Status and Analytics pages) built from the lake; it
  runs locally, not hosted yet.

**Data sources that work today**

| Source | What it gives us | Stations / coverage | How it's pulled | State |
| --- | --- | --- | --- | --- |
| HRRR via Herbie (NOAA on AWS) | Forecast fields (temperature, dewpoint, wind, gust, rain, visibility, sunlight, cloud) at our points, leads 0–6 h | ~27 points around SF | `microcast ingest nwp` (live GRIB) | Works; run by hand |
| HRRR history (University of Utah hrrrzarr archive) | The same fields for past cycles | Hourly cycles 2025-04 → 2026-09, ~13,000 cycles (124 missing in the archive) | `microcast backfill hrrr` | Loaded |
| IEM ASOS | Airport observations: temperature, dewpoint, wind, gust | KSFO (SFO), KOAK (Oakland) | `microcast ingest obs` | Loaded 2025-04 → now |
| IEM HADS | Hourly temperature | SFOC1 (the Mint, downtown SF; the house's stand-in) | `microcast ingest obs` | Loaded 2025-04 → now |
| NDBC | Temperature, wind, gust | FTPC1 (Fort Point, Golden Gate) | `microcast ingest obs` | Loaded 2025-04 → now |
| Synoptic | Temperature, wind, gust, solar radiation | 604PG (PG&E, Golden Gate Park) and five CWOP home stations in the Castro | `microcast ingest synoptic` | Works; free tier keeps 7 days, so it accrues from 2026-09-28 |
| MADIS mesonet archive | History for the same Synoptic stations plus ~25 more in the SF box (CWOP, PG&E, Presidio, Marin headlands) | 2025-04 → now | `microcast backfill madis` | Loaded (18.8M rows) |
| PurpleAir, own sensor | PM2.5 on both laser channels, sensor temperature, humidity, pressure | The outdoor PA-II at the house | `microcast ingest purpleair` (home network, free) | Works; run by hand |
| PurpleAir, public transect | Hourly sensor temperature | 10 public sensors, Ocean Beach → Sunset → Twin Peaks → Castro, 2025-04 → now | `microcast backfill purpleair` (paid API points) | Loaded (~115k rows; ~237k API points) |

Only the four original truth stations (SFOC1, KSFO, KOAK, FTPC1) feed the
backtest today. The Synoptic, MADIS and PurpleAir stations
are in the lake but not yet in the models.

**Planned, not built**

- Dagster OSS as the scheduler and backfill engine, on the home k3s cluster,
  with Postgres for Dagster and the Iceberg catalog
  ([D12](docs/decisions.md#d12--dagster-on-k3s-runs-the-collectors-not-cronjobs)).
  Until then, every collector is run by hand.
- MinIO/S3 storage and the Lakekeeper REST catalog.
- HRRR at the Synoptic, MADIS and PurpleAir stations, so they can enter
  backtests; using the PurpleAir transect as model features; hourly live
  PurpleAir polling (D10).
- A backyard weather station at the house (D5).
- Hosting the status site at weather.henry.st (D8).
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

### Acronyms

| Term | Meaning |
| --- | --- |
| ASOS | Automated Surface Observing System: the airport weather stations (KSFO, KOAK) |
| AWS | Amazon Web Services: where NOAA publishes HRRR through its Open Data program |
| CI | Confidence interval (here, from a bootstrap: resampling the scores many times) |
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
| LCC | Low Cloud Cover: HRRR's low-cloud-layer fraction |
| MADIS | Meteorological Assimilation Data Ingest System: NOAA's archive of mesonet observations |
| Mesonet | A mesoscale network: dense non-federal weather stations |
| NDBC | National Data Buoy Center: buoys and coastal stations (FTPC1, Fort Point) |
| NOAA | National Oceanic and Atmospheric Administration |
| NWP | Numerical Weather Prediction: physics-based weather models such as HRRR |
| PA-II | PurpleAir's outdoor sensor model, with two laser particle counters (channels A and B) |
| PG&E | Pacific Gas and Electric: the utility, which runs its own weather stations (604PG) |
| PIT | Probability Integral Transform: a calibration check for probabilistic forecasts |
| PM2.5 | Particulate matter under 2.5 µm, what PurpleAir sensors measure |
| QC | Quality control (flags on suspicious observations) |
| REFS | Rapid Refresh Ensemble Forecast System: the ensemble version of RRFS |
| RH | Relative humidity |
| RRFS | Rapid Refresh Forecast System: NOAA's successor to HRRR |
| S3 | Amazon Simple Storage Service, and the object-storage API MinIO also speaks |
| SF | San Francisco |
| SFO | San Francisco International Airport |
| WMO | World Meteorological Organization |

### Tools and terms

| Name | Meaning |
| --- | --- |
| Backtest | Replaying history: train on past months, forecast the next month as if live, score against what happened |
| Bronze / silver / gold | The lake's layers: raw data as ingested; cleaned and aligned data; model-ready features, forecasts and scores |
| Dagster | A data orchestrator: runs jobs on schedules or triggers, and backfills partitioned data |
| DuckDB | An in-process SQL database used to query the lake's Parquet files |
| Herbie | A Python library that finds and downloads NWP output (GRIB) from NOAA's cloud archives |
| hrrrzarr | The University of Utah's archive of HRRR in Zarr format: small chunks, so a few points are cheap to read |
| Iceberg | Apache Iceberg: a table format over Parquet files with snapshots (every write is a versioned commit) |
| Lakekeeper | An Iceberg catalog server (REST); planned to replace the local SQLite catalog |
| LightGBM | Microsoft's gradient-boosting library |
| MinIO | Self-hosted object storage that speaks the S3 API |
| MLflow | An open-source experiment tracker (runs, parameters, metrics, models) |
| Parquet | Apache Parquet: a columnar file format |
| PurpleAir | A network of low-cost air-quality sensors with a public map and a paid API (points) |
| PyIceberg | The Python library for reading and writing Iceberg tables |
| Residual model | A model that predicts HRRR's error at a station, then adds it back to HRRR's forecast |
| SQLite | A single-file SQL database; holds the local Iceberg catalog and MLflow runs |
| Synoptic | Synoptic Data: a company aggregating weather stations (CWOP, PG&E, …) behind an API |
| Zarr | A chunked array format for cloud storage |

### Station ids

| Id | Meaning |
| --- | --- |
| SFOC1 | SF Downtown at the Mint (NOAA HADS), ~1 km from the house; hourly temperature |
| KSFO / KOAK | San Francisco and Oakland airport ASOS stations |
| FTPC1 | Fort Point at the Golden Gate (NDBC) |
| 604PG | PG&E station at the west end of JFK Drive, Golden Gate Park |
| C5988, F6803, E9227, F2543, F4637 | CWOP home weather stations around the Castro and Corona Heights |
| pa_<index> | A public PurpleAir sensor on the transect, by its PurpleAir sensor number |
