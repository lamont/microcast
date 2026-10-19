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

## Current champion model

**`gbm_terrain_stack`** (since 2026-10-18), for points with a station of
their own. Replace this section when a new model beats it in the backtest.

It starts from the newest HRRR forecast and predicts HRRR's error at each
point with a LightGBM model. Its inputs:

- HRRR's own fields at the point (temperature, humidity, wind, gust, cloud,
  visibility, sunshine);
- the station's latest obs and its recent bias;
- terrain (height and how it differs from HRRR's, slope, which way it faces,
  shelter from the west wind, distance to open water);
- the marine layer's lid and the flow above it ("regime");
- the push of cool, damp air moving inland from the coast, seen at MADIS
  stations ("upwind");
- the six previous HRRR runs' forecasts for the same hour. When those runs
  disagree, the forecast's uncertainty range widens.

How it compares with raw HRRR, all 31-32 stations, July 2025 to October 2026
(16 months, each forecast by a model trained only on earlier months; ~930k
temperature and ~955k gust forecasts):

| | Temp 1-3 h | Temp 4-6 h | Gust 1-3 h | Gust 4-6 h |
| --- | --- | --- | --- | --- |
| Skill vs raw HRRR (CRPS, 95% CI) | **+50.6%** [49.8, 51.5] | **+38.4%** [37.3, 39.5] | **+56.1%** [55.4, 56.9] | **+52.8%** [51.9, 53.6] |
| Typical miss, raw HRRR | 2.5 °F | 2.6 °F | 6.0 mph | 6.2 mph |
| Typical miss, champion | 1.2 °F | 1.6 °F | 2.6 mph | 2.9 mph |
| 80% range catches the outcome | 84% | 84% | 84% | 84% |

Skill is the drop in CRPS, a score for probabilistic forecasts in the
forecast's own units (°F or mph); +50% halves HRRR's score. "Typical miss" is
the mean absolute error. Previous champion: `gbm_terrain`, +48.6% / +35.4%
temperature, +55.3% / +51.8% gust. The model history and every experiment,
kept or rejected, are in the [phase 1 spec](docs/specs/phase-1.md)
("Experiments: tried and queued").

Without a station at the point (the house, the ride), skill is much lower,
around +10-12% for temperature and +22-24% for gust. For those points the regime
and upwind inputs help temperature. For gust, upwind helps there but regime
doesn't. The status site shows it (rolling backtest of 2026-10-18); the live
forecast command, still to be built (spec Next #4), will start on it.

## What exists today vs. what's planned

The [design doc](docs/design.md) describes the full system. Most of it is not
built yet. As of 2026-10-06:

**Built and running on a laptop (phase 1)**

- A registry of places, routes and truth stations, turned into ~27 virtual
  forecast points (`config/places.yaml`).
- A local Iceberg lake (PyIceberg with a SQLite catalog, Parquet files under
  `./data`) with bronze, silver and gold layers, queried through DuckDB.
- Ingest from ten working data sources (table below) into the lake's bronze layer.
- Terrain features for every point (USGS 3DEP 10 m elevation, HRRR's own
  terrain height, slope, shelter, fetch from the water) in
  `silver.static_features`.
- Seven models behind one interface: raw HRRR, a rolling-bias correction, a
  LightGBM residual model, the same LightGBM with the PurpleAir transect
  (1–2% better on temperature, no help on gust), with terrain in place of the
  station id (as good on known stations, 4–5 points better on unseen ones),
  and two HRRR-only variants for places with no station of their own.
- Backtests on rolling monthly folds with CRPS skill and bootstrap CIs, logged
  to MLflow (SQLite file). **The phase 1 gate is passed:** at the four original stations
  the GBM residual beats raw HRRR at 0–6 h by about 30–43% for temperature
  and 27–30% for gust, and it now trains and scores at 36 stations including
  the Castro and Golden Gate Park
  ([results](docs/specs/phase-1.md#results-with-the-network-stations-2026-10-06)).
- Leave-stations-out backtests (`microcast backtest --holdout`): how the
  models do at a station they never trained on
  ([results](docs/specs/phase-1.md#terrain-and-leave-stations-out-2026-10-06-evening)).
- A benchmark against NWS's National Blend of Models (NBM) on the same rows
  (`microcast backtest --nbm`): at 1–6 h NBM is only 2–6% better than raw
  HRRR at these stations; ours is 35–55% better
  ([results](docs/specs/phase-1.md#nbm-benchmark-2026-10-12)).
- A first look at crosswind gust on the Golden Gate Bridge from GGBC1, the
  station mid-span (its mirrored wind vane is corrected in silver)
  ([results](docs/specs/phase-1.md#golden-gate-bridge-crosswind-2026-10-06-evening)).
- A static status site (Status and Analytics pages) built from the lake; it
  runs locally, not hosted yet.

**Data sources that work today**

| Source | What it gives us | Stations / coverage | How it's pulled | State |
| --- | --- | --- | --- | --- |
| HRRR via Herbie (NOAA on AWS) | Forecast fields (temperature, dewpoint, wind, gust, rain, visibility, sunlight, cloud) at our points | ~27 registry points around SF | `microcast ingest nwp` (live GRIB; the newest run, ~1 h after its start) | Works; run by hand |
| HRRR history (University of Utah hrrrzarr archive) | The same fields for past cycles, up to 18 h ahead | Hourly cycles 2025-04 → now (~13,000; 124 missing in the archive), at the registry points and 42 network stations. Each cycle's Bay Area window is kept on disk (2.4 GB), so a new point inside it needs no download | `microcast backfill hrrr` (`--points network` for the stations) | Loaded; the archive runs ~3 h behind real time |
| RRFS via Herbie (NOAA on AWS) | HRRR's successor (operational 2026-11-03): temperature, dewpoint, wind, gust, visibility, ceiling, sunlight, leads 1–6 | Registry points and MADIS stations; hourly cycles from 2026-08-13 (the pre-operational feed) | `microcast backfill rrfs` (default: the last 3 days; run every day or two) | Collecting, not yet a model input; compared from 2027 ([D13](docs/decisions.md#d13--rrfs-collect-at-our-points-now-compare-in-spring-2027)) |
| NBM via Herbie (NOAA on AWS) | National Blend of Models: temperature and gust, mean and ensemble spread, as a benchmark only | The backtest stations; one run a day (rotating hour) 2025-07 → 2026-10-05 | `microcast backfill nbm` | Loaded (462 runs) |
| IEM ASOS | Airport observations: temperature, dewpoint, wind, gust | KSFO (SFO), KOAK (Oakland) | `microcast ingest obs` | Loaded 2025-04 → now |
| IEM HADS | Hourly temperature | SFOC1 (the Mint, downtown SF; the house's stand-in) | `microcast ingest obs` | Loaded 2025-04 → now |
| NDBC | Temperature, wind, gust | FTPC1 (Fort Point, Golden Gate) | `microcast ingest obs` | Loaded 2025-04 → now |
| Synoptic | Temperature, wind, gust, solar radiation | 604PG (PG&E, Golden Gate Park) and five CWOP home stations in the Castro | `microcast ingest synoptic` | Works; free tier keeps 7 days, so it accrues from 2026-09-28 |
| MADIS mesonet archive | History for the same Synoptic stations plus ~25 more in the SF box (CWOP, PG&E, Presidio, Marin headlands) | 2025-04 → now | `microcast backfill madis` | Loaded (18.8M rows) |
| PurpleAir, own sensor | PM2.5 on both laser channels, sensor temperature, humidity, pressure | The outdoor PA-II at the house | `microcast ingest purpleair` (home network, free) | Works; run by hand |
| ~~PurpleAir, public transect~~ | ~~Hourly sensor temperature~~ | ~~10 public sensors, Ocean Beach → Sunset → Twin Peaks → Castro~~ | ~~`microcast backfill purpleair` (paid API points)~~ | **Removed 2026-10-16:** a 1–2% temperature gain wasn't worth the API cost or the upkeep. The history already loaded (2025-04 → 2026-10-05) stays in the lake, so `gbm_purpleair` and its results remain ([spec](docs/specs/phase-1.md#purpleair-transect-ingest-removed-2026-10-16)) |

The backtest trains and scores at 36 stations: the four original truth
stations (SFOC1, KSFO, KOAK, FTPC1) plus the MADIS network, which includes
the Synoptic stations. The PurpleAir transect sensors are inputs to one model, never truth
(their temperature reads hot), and their history stops at 2026-10-05.

**Planned, not built**

- Dagster OSS as the scheduler and backfill engine, on the home k3s cluster,
  with Postgres for Dagster and the Iceberg catalog
  ([D12](docs/decisions.md#d12--dagster-on-k3s-runs-the-collectors-not-cronjobs)).
  Until then, every collector is run by hand.
- MinIO/S3 storage and the Lakekeeper REST catalog.
- ~~Hourly live PurpleAir polling (D10).~~ Dropped with the transect ingest.
- A backyard weather station at the house (D5).
- Hosting the status site at weather.henry.st (D8).
- Live inference, the forecast panel on the site, and plain-language alerts
  to Slack.
- RRFS as a model input (collected from 2026-10-18; compared in 2027), REFS and
  GFS, and cloud-cover forecasts.

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
uv run microcast backfill hrrr --start 2025-04-01 --end 2026-10-01   # hourly cycles, resumable
uv run microcast backfill madis --start 2025-04-01 --end 2026-10-01  # network stations' history
uv run microcast backfill hrrr --start 2025-04-01 --end 2026-10-01 --points network --tag net1
uv run microcast build silver && uv run microcast build gold        # gold: 36 truth stations
uv run microcast build static                             # terrain per point (fetches 3DEP once)
uv run microcast backtest                                 # -> gold.scores, MLflow, data/reports/
uv run microcast backtest --holdout --models gbm_residual,gbm_terrain,gbm_nwp,gbm_nwp_terrain  # leave stations out
uv run microcast backfill nbm --end 2026-10-06            # NBM benchmark runs, one a day
uv run microcast backtest --nbm --models bias_rolling,gbm_terrain  # NBM on the backtest's rows
uv run microcast compare                                  # leaderboard vs raw HRRR
uv run microcast site --out site                          # static pages for weather.henry.st
python -m http.server -d site 8000
```

For an explicit station holdout, add `--holdout-stations ID1,ID2` to
`backtest --holdout`. All listed stations are withheld together; the remaining
stations supply training data. Without this option, the existing grouped
station holdouts are unchanged.

Both backtest modes accept `--start` and `--end` to select forecast cycles in
the half-open UTC interval `[start, end)`. This filters the data available for
both training and evaluation, so include at least three training months plus
a later test month. It does not select a test-only window. For example:

```sh
uv run microcast backtest --holdout --holdout-stations KSFO,KOAK \
  --models raw_hrrr,gbm_nwp --start 2025-04-01 --end 2025-08-01
```

Holdout reports record the window and training/held-out station groups under
`data/reports/holdout/`; they leave rolling backtest lake tables and reports
unchanged. A rolling `backtest` still replaces its usual forecast/score tables
and reports, including when a window is specified.

The status site can also be built immediately after `microcast lake init`.
It shows ingestion status and an empty backtest state until results exist.
A partially written report set is rejected rather than displayed as complete.

Collectors, run by hand until Dagster schedules them (D12): `microcast ingest
synoptic` (at least weekly; the free tier keeps only 7 days), `microcast ingest
purpleair` (every 2 min on the home network). MADIS history: `microcast
backfill madis --start 2025-04-01` (resumable; streams ~30 MB per hour of data
and keeps the SF box). To move the lake to the cluster: `microcast lake copy --to <catalog>`,
then rebuild silver and gold there (decisions D11).

Container image: CI tests every push and PR, and on `main` publishes a
multi-arch (amd64 + arm64) image to `ghcr.io/lamont/microcast` tagged with
the short commit and `latest`. `deploy/docker/publish.sh [tag…]` does the same
from the laptop. The image's entrypoint is `microcast`; the lake lives in the
`/data` volume, and keys and coordinates come from the environment:

```sh
docker run --rm --env-file .env ghcr.io/lamont/microcast:latest points
```

The laptop's `data/` can't simply be mounted there: its Iceberg metadata
holds absolute laptop paths, so a lake for the image is made with `lake copy`.

Each ingest batch appends new Parquet files and a new Iceberg snapshot to
`bronze`, which is append-only. Silver and gold rebuild whole partitions into
fewer, larger files. To move from local disk to Lakekeeper and S3/MinIO, set
the `PYICEBERG_CATALOG__MICROCAST__*` variables; no code changes.

## Trying another station network

Use a private `config/places.local.yaml` (or `MICROCAST_PLACES`) for station
IDs and places, and set the optional `.env` collection bounds as
`lat_min,lat_max,lon_min,lon_max`:

- `MICROCAST_HRRR_BOUNDS` selects the historical HRRR cache window. Include
  every registry and network point; the bounds plus interpolation margin must
  fit within the supported HRRR grid.
- `MICROCAST_MADIS_BOUNDS` selects the observations retained from each national
  MADIS archive. It does not reduce the downloaded archive size.

Both retain the existing San Francisco defaults when unset. Use a separate
`MICROCAST_DATA_DIR` for each region (and a separate catalog if configuring a
remote catalog): completed ingestion batches are tracked by time, so changing
bounds or station IDs does not automatically backfill previously completed
hours for the new configuration.

**Terrain remains limited to the built-in San Francisco DEM.**
`microcast build static` rejects points outside that coverage before fetching
terrain, and sampling a cached DEM rejects out-of-bounds points instead of
silently returning its edge values. For another region, use the existing
models that do not require static features, such as `raw_hrrr` and `gbm_nwp`.
An empty PurpleAir sensor registry produces an empty network-feature table.
Dashboard times remain Pacific time.

## Layout

```
config/            places.yaml (registry), models.yaml (NWP products + GRIB search)
routes/examples/   example route GeoJSON; real routes go in routes/private/ (ignored)
src/microcast/
  registry.py      places/routes → virtual points with bearings
  lake/            Iceberg schemas, catalog, append-only bronze writer
  ingest/nwp.py    Herbie subset → pick_points(k=4) → long Arrow (live)
  ingest/hrrr_zarr.py  HRRR history from the hrrrzarr archive (backfill; `--variables` adds fields later)
  ingest/rrfs.py   RRFS at our points from AWS, top-of-hour fields only (collected, not yet used)
  ingest/obs.py    station truth: IEM ASOS/HADS, NDBC
  ingest/purpleair.py  own PurpleAir sensor (LAN or API)
  ingest/synoptic.py   Synoptic/CWOP stations (last 7 days, accrues)
  ingest/madis.py  MADIS mesonet history (CWOP, PG&E stations), streamed hourly
  ingest/dem.py    USGS 3DEP elevation over the HRRR window, 10 m, cached
  lake/copy.py     copy bronze to another catalog (laptop -> cluster)
  transform/       bronze → silver → gold (DuckDB SQL; gold.py has the lagged HRRR ensemble);
                   network.py: PurpleAir transect features; upwind.py: the marine push at the
                   MADIS stations; static.py: terrain features per point
  models/          Forecaster zoo: raw_hrrr, bias_rolling, gbm_residual, gbm_purpleair,
                   gbm_terrain, gbm_nwp, gbm_nwp_terrain, gbm_terrain_stack (terrain +
                   regime + upwind + lagged HRRR, adopted 2026-10-18), and the
                   *_neighbours, *_regime, *_lagged and *_upwind experiments
  backtest/ablation.py  paired model-vs-twin comparisons -> data/reports/experiments/
  backtest/        folds, CRPS, skill + bootstrap CI, leaderboard
  pipeline.py      backtest orchestration + MLflow
  site/            static status site (weather.henry.st)
  cli.py           `microcast …`
tests/             offline tests (synthetic pick_points output, temp catalogs)
deploy/docker/     Dockerfile (multi-arch image) and publish.sh (laptop -> GHCR)
.github/workflows/ ci.yml: lint + tests, then build and push the image on main
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
| DEM | Digital Elevation Model: a grid of ground heights (here USGS 3DEP) |
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
| Shelter index | Winstral's Sx: the steepest horizon angle upwind of a point; positive means sheltered from that wind |
| Synoptic | Synoptic Data: a company aggregating weather stations (CWOP, PG&E, …) behind an API |
| Zarr | A chunked array format for cloud storage |

### Station ids

| Id | Meaning |
| --- | --- |
| SFOC1 | SF Downtown at the Mint (NOAA HADS), ~1 km from the house; hourly temperature |
| KSFO / KOAK | San Francisco and Oakland airport ASOS stations |
| FTPC1 | Fort Point at the Golden Gate (NDBC) |
| GGBC1 | Golden Gate Bridge, mid-span at deck height (MesoWest via MADIS); wind only, its vane reads mirrored |
| 604PG | PG&E station at the west end of JFK Drive, Golden Gate Park |
| C5988, F6803, E9227, F2543, F4637 | CWOP home weather stations around the Castro and Corona Heights |
| pa_<index> | A public PurpleAir sensor on the transect, by its PurpleAir sensor number (history only; no longer ingested) |
