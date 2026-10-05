# Decisions

Changes to [`design.md`](design.md) agreed after it was written. Where the two
disagree, this file wins. Newest first within each section; each decision says
what changed, why, and what it means for code.

## D1 · Storage: append-only bronze, consolidating silver/gold, PyIceberg writes

*2026-10-04*

**Decision.**

- **One table format end to end:** Iceberg v2 tables of zstd Parquet, from the
  phase 1 laptop spike onward (the roadmap said "local Parquet" for phase 1;
  using Iceberg from day one costs nothing and removes a migration).
- **Catalog:** locally, PyIceberg's SQL catalog on SQLite with a `file://`
  warehouse under `data/`. Later, Lakekeeper (Iceberg REST) with MinIO or S3.
  The switch is configuration only (`PYICEBERG_CATALOG__MICROCAST__*` env vars),
  no code change.
- **Writes:** PyIceberg + Arrow for every write, in every layer. DuckDB is a
  reader and transform engine (Arrow in, Arrow out) and never writes Iceberg
  directly. This also removes the "DuckDB can only write through a REST
  catalog" constraint from phase 1.
- **Bronze rows are append-only.** Each ingest batch (one model cycle and lead,
  or one obs poll) is one `append`: new data files plus a new snapshot. A
  rerun or late correction appends again. Bronze rows are never overwritten or
  deleted. `lake.catalog.append_bronze` is the only write path, and
  `replace_partition` refuses bronze tables.
- **Every bronze row carries `ingest_batch` and `ingested_at`.** Silver dedupes
  on the natural key, keeping the latest `ingested_at` for "current truth".
  Backtests can filter `ingested_at <= issue_time` to replay exactly what live
  inference would have seen.
- **Silver and gold are derived and consolidating.** Transforms rebuild a whole
  partition (typically one day) with `overwrite(..., overwrite_filter=...)`.
  That writes a few large files in place of many small hourly ones, which is
  the compaction step for the layers that are read most.
- **Bronze compaction is optional and row-preserving.** If small-file count
  ever matters (roughly 24 files a day per model is fine for years), rewrite a
  partition with the same rows, `ingested_at` included. PyIceberg 0.12 has no
  `rewrite_data_files`, so this is read-then-overwrite in a maintenance job.
- **Reproducibility:** training tags the `gold.training_examples` snapshot it
  read (`mlflow-<run_id>`) via `lake.catalog.tag_snapshot`, and logs the
  snapshot id to MLflow. Iceberg never expires a snapshot a tag points to, so
  nightly `expire_snapshots` can't break an old run.

**Why not overwrite a bronze partition on rerun?** Physically you're right that
an Iceberg overwrite writes new files and a new snapshot and leaves the old
files in place. But the old rows are then only reachable by time travel to an
older snapshot, and that history disappears once snapshot expiry runs. The
as-of rule reads the *current* snapshot and filters on `ingested_at`, so the
first-seen values and times of corrected rows have to stay in the current
snapshot. Append-only plus dedupe in silver keeps that history permanently, at
the cost of a few extra rows.

**Verified.** `tests/test_lake.py` covers append-only snapshots, the refused
bronze overwrite, tag survival through `expire_snapshots`, and
latest-ingest dedupe in DuckDB. A live HRRR ingest made one Parquet file
(~10 KB) and one snapshot per batch.

## D2 · Marine-layer cloud cover replaces ground fog as the cloud target

*2026-10-04*

**Decision.** The target is **low-cloud onset at a place**: the time the marine
layer arrives overhead (at home, the stratus spilling over Corona Heights in the
late afternoon or evening), and when it clears. Ground fog and visibility are
dropped as a target. The `fog_event` MLflow experiment becomes `low_cloud`, and
`silver.events` labels `low_cloud_onset` and `low_cloud_clear` in place of fog
arrival.

**Forecast inputs (already ingested for HRRR):** low cloud cover `lcc`
(LCDC, low cloud layer), total cloud cover `tcc`, cloud base and ceiling height
(`gh` at `cloudBase` / `cloudCeiling`), 2 m RH and dewpoint depression, 10 m wind
direction (onshore flow). Later: the neighbour-cell gradient of `lcc`, which
shows where the cloud edge is, and Ocean Beach / Sunset sensors upstream.

**Ground truth at the house, in order of preference:**

1. **Station solar radiation vs clear-sky irradiance (daylight).** Compute the
   clear-sky index = measured GHI / modelled clear-sky GHI (`pvlib`, Ineichen
   model). Onset is when the 15-min median falls below ~0.5 and stays there for
   30+ min. This is free with a Tempest or Ecowitt station. Evening marine-layer
   arrival is mostly a summer phenomenon, when sunset is after 8 pm, so most
   onsets fall in daylight.
2. **GOES-18 low-cloud product (day and night).** Use `goes2go` for the ABI
   cloud-top height and the night-time fog/low-stratus product, sampled at the
   registry points. This covers onsets after sunset and gives history from
   before the station exists. Move it from phase 5 to phase 1–2.
3. **Optional: a sky camera aimed at Corona Heights.** A cheap IP camera plus a
   simple brightness/edge classifier gives a direct "cloud over the ridge"
   label, and it matches what you actually see.
4. **Regional proxy only:** KSFO/KOAK ceiling. Useful as a feature, not as the
   label for the Castro.

**Why.** It's what's actually observed and wanted ("clear blue skies, then the
cloud comes in"). It's also measurable at the house with the station already
planned, unlike visibility.

## D3 · Review findings carried into the plan

*2026-10-04*

- **Promotion gate:** challenger replaces champion only after 14 days of
  shadow **and** a minimum count of events (e.g. ≥ 10 rain hours or ≥ 10
  low-cloud onsets) for event targets. Rare events can't be judged on calendar
  days alone. For rain, fall back to backtest skill until the count is met.
- **Sub-hourly GRIB:** `subh` files hold several 15-min steps per lead.
  `ingest.nwp.to_long` explodes `step` into `lead_min` and `valid_time`
  (tested).
- **Grid cells are identified by coordinates, not indices.** Herbie's
  `pick_points` returns each neighbour cell's own lat/lon, not `(i, j)`, so
  `bronze.nwp_point` stores `grid_lat` / `grid_lon` in place of
  `grid_i` / `grid_j`. Longitudes are normalised to −180…180.
- **Level column:** GRIB messages that share a short name (`gh` at cloud base
  and at ceiling) are kept apart by a `level` column (`cloudBase:0`,
  `heightAboveGround:2`), part of the bronze natural key.
- **Routes in git are examples.** Real routes reveal home and school, so they
  live in `routes/private/` with a `config/places.local.yaml`, both git-ignored.
  Home coordinates come from `.env`.

## D4 · Bike route: Golden Gate Park out and back, replacing the Ocean Beach loop

*2026-10-04*

**Decision.** The bike use case is now Divisadero & Fell → the Panhandle → JFK
Drive → the Great Highway at Ocean Beach, and back the same way (~6.3 km each
way). Registry id `gg_park_ride`, geometry `routes/examples/gg_park_jfk.geojson`,
`duration_min` 75. The old Great Highway / Sunset Blvd loop is gone.

**For code.** Routes gain `out_and_back: true`: the outbound line is sampled
once (14 points at 500 m); the return leg reuses those points with the bearing
reversed, so the westerly that is a headwind on the way out is scored as a
tailwind home. Ride-window scoring (phase 4) evaluates both legs: the
question is still "leave by when, for a tailwind home".

## D5 · Ground truth before the home station; own sensors

*2026-10-04*

**Decision.**

- **Truth stations are registry points.** `places.yaml` gains a `stations:`
  list; each station is a virtual point (`point_id` = station id), so NWP is
  extracted at the station and residuals are learned there. Phase 1 uses four
  free, keyless sources:

  | Station | Where | Source | Variables |
  | --- | --- | --- | --- |
  | SFOC1 | SF Downtown (the Mint), ~1 km from home | IEM HADS | hourly temp |
  | KSFO | SFO | IEM ASOS | temp, dewpoint, wind, gust |
  | KOAK | Oakland | IEM ASOS | temp, dewpoint, wind, gust |
  | FTPC1 | Fort Point, Golden Gate | NDBC | temp, wind, gust |

  SFOC1 is the house's stand-in for temperature until the backyard station
  exists. There is no wind truth inside the city yet, so the gust target is
  learned at SFO, Oakland and Fort Point. Synoptic (CWOP/mesonet in the
  Castro and the Sunset) needs a free token and is the next source to add.
- **Own PurpleAir sensor.** Listed on the public map with its location
  snapped to a nearby corner; its map name is kept out of git too. The sensor index lives in `.env`
  (`MICROCAST_PURPLEAIR_SENSOR_INDEX`), never in git, since index + "home"
  locates the house. Rows use `station_id = purpleair_home`. Polled on the LAN
  (`MICROCAST_PURPLEAIR_HOST`, free) or backfilled through the API
  (`PURPLEAIR_API_KEY`). Its temperature reads hot and is a trend feature only.
  The index (in `.env`) comes from the widget embed code and has not been
  checked against the API yet. **LAN, confirmed 2026-10-05:** the outdoor sensor reports
  `place: outside` and two laser counters (PMSX003 A + B). A second, indoor
  PurpleAir is single-channel and is not ingested. Channel B outdoors
  was fouled (3,334 µg/m³ vs 4.6 on A, 47× the 0.3 µm count), so silver
  range-flags PM channels above 1,000 µg/m³ and PM2.5 comes from A alone until
  B is cleaned. With one good channel there is no A/B agreement check, so PM
  data from this period is single-sensor quality. Give the sensor a DHCP
  reservation so the LAN host stays put. On macOS, Local Network privacy
  blocks uv's and Homebrew's Pythons (ad-hoc signed, no stable identity, so
  there is no prompt and no way to grant it), while Apple's curl is allowed.
  `fetch_local` falls back to `/usr/bin/curl` on that error; a poller on the
  NAS or cluster takes the normal path. It also refuses a sensor that doesn't
  report `place: outside`.
  **Hardware:** a PurpleAir PA-II (hardware 2.0, firmware 7.02: ESP8266,
  BME280, two PMS5003 counters). It reported only 51% of its uploads to
  PurpleAir succeeding (3,484 of 6,853, WiFi −64 dBm), so API history has
  gaps and the LAN poll is the primary path, not a convenience.
- **Backyard station (planned).** A wifi station away from the house, which
  gives better temperature than anything near a wall, plus wind, plus solar
  radiation for the clear-sky index in D2. Requirements: solar radiation (W/m²
  ideally; lux works with a rough conversion), wind speed and gust, and data
  readable locally or by push to our own endpoint rather than only through a
  vendor cloud. Model still to be confirmed (see Open).

## D6 · HRRR history from the hrrrzarr archive

*2026-10-05*

**Decision.** Backfill HRRR from the University of Utah Zarr archive
(`s3://hrrrzarr`, anonymous HTTPS) instead of GRIB through Herbie.
`ingest.hrrr_zarr` writes the same rows as `ingest.nwp` (same `model`,
`product`, `variable`, `level`, neighbour cells and ranks); only
`ingest_batch` (`hrrrzarr/<cycle>`) tells them apart. Live ingest stays on
Herbie/GRIB, since the archive lags by about a day.

**Why.** Each Zarr chunk holds every forecast hour of a cycle for a 150×150
tile: a cycle at our points is 26 small GETs (~16 MB), against ~20 MB of GRIB
per forecast hour. Measured: 7.5 s per cycle for f00–f06 serially vs 66 s, and
~1 cycle/s with 24 workers (bandwidth-bound). The archive also has DSWRF
(downward solar) for D2, now in the GRIB search too.

**Verified.** For 2025-07-15 21Z, f00–f06, all 12 fields: identical values,
cells and neighbour ranks to the Herbie path (`max |diff| = 0`).

**Amends D1, for backfill only.** One bronze append covers 96 cycles (one
snapshot each), not one per batch, to keep snapshot count and metadata small.
Rows still carry their own `ingest_batch`. The batch ids of each append are
recorded in the snapshot summary (`microcast.ingest-batches`), which is how a
rerun resumes without scanning bronze.

**Backfill order.** First every third cycle with the kept hour rotating daily
(`hrrr_zarr.cycles(stride_h=3)`), so every lead sees every hour of the day,
then `--stride 1` fills the rest. The archive has occasional missing cycles
(40 of 4,287 in the first pass, mostly the 23Z forecast since February 2026);
they are logged, and a rerun or the GRIB path can fill them.

## D7 · Evaluation details for phase 1

*2026-10-05*

- **Issue time and lead 0.** HRRR history is "issued" at init + 55 min. Lead 0
  verifies before it is issued, so it is a hindcast: it is a feature source
  (HRRR's own error at the cycle time) but never scored. The gate is judged on
  leads 1–6, which are ~5 min to ~5 h ahead of issue.
- **Every model outputs a Gaussian**, including raw HRRR, whose sigma is its
  historical RMS error by lead. A point forecast's CRPS is its MAE, which would
  flatter any probabilistic challenger.
- **`gold.training_examples` is wide**: one row per (point, cycle, lead) with
  a nullable target column per variable (`obs_t2m`, `obs_gust`), not one row
  per target variable as the design sketched. Same information, half the
  joins.
- **Gust truth** is the reported gust, else the sustained wind (ASOS reports a
  gust only when it is gusting). Observations are snapped to the report nearest
  the top of the hour within ±20 min.
- **Rolling bias** (model 1) is the trailing 14-day mean error at the same
  point and lead, over residuals verified before issue time. It is also a
  feature for the GBM.
- **Skill CI** is a day-block bootstrap (whole days resampled, 1000 draws).

## D8 · Status site at weather.henry.st

*2026-10-05*

**Decision.** The status page is a static site at `weather.henry.st` with two
pages: **Status** (phase gate, leaderboard, monthly skill vs raw HRRR, data
freshness) and **Analytics** (skill by lead and station, error by hour of day,
PIT calibration, raw HRRR's error by month × hour). `microcast site --out
site/` builds it from the lake and the last backtest into plain files
(`index.html`, `analytics.html`, `data.json`, CSS/JS, no dependencies), so any
static host works. Once live inference exists (phase 4) the same pages gain
the next-12-hour forecast panel the design described, read from
`serving.latest`.

**Hosting:** k3s on the home "swarm" cluster (the design's stage 3, early
for this one service): the built files in an nginx container behind the
cluster's Traefik ingress for `weather.henry.st`, rebuilt by a job after each
backtest (later, each live scoring run).

**Changes the design.** The design kept everything behind Tailscale. The
status site is meant to be reachable at a public name, so it carries a
privacy rule: it shows station ids, place ids and aggregate scores only,
never coordinates, the PurpleAir index or route geometry. Dagster and MLflow
stay private.

## D9 · Alerts go to Slack

*2026-10-04*

Alerts are delivered to individuals or a family channel on Slack, in place of
the Home Assistant `notify` service and ntfy fallback (edited in
`design.md`, Delivery). The phase 4 alert engine targets Slack first.

## D10 · Synoptic stations accrue; they can't be backfilled

*2026-10-05*

**Decision.** Six Synoptic stations join the registry (`source: synoptic`):
**604PG** (PG&E, Golden Gate Park at the west end of JFK Drive) for the ride,
and five CWOP stations around the Castro and Corona Heights (**C5988, F6803,
E9227, F2543, F4637**), four of them with solar radiation, which is the D2
low-cloud truth we lacked before the backyard station. `microcast ingest
synoptic` pulls the last 7 days; schedule it daily (a CronJob on the swarm
cluster once that exists, D8).

**Constraint.** The free tier serves only the last ~7 days (older requests are
refused in the response body), so there is no history to train on: these
stations accrue from 2026-09-28 on. A paid tier would unlock the archive
(C5988 goes back to 2006), which would let them into backtests immediately.

**Caveats.** CWOP stations are backyard installs: temperature is usually fine,
wind is often sheltered by buildings and trees. Treat Castro wind as local
exposure, not open-terrain truth; 604PG and Fort Point are the wind truth for
the ride. Auth: Synoptic issues an API key, and requests need a token
generated from it (`/v2/auth`); `.env` holds both.

**For code.** Stations added after a cycle was backfilled have no HRRR rows
for it, and a rerun skips the cycle because its batch id is already recorded.
Before these stations enter a backtest, backfill HRRR for just the new points
over the weeks they have obs (a points-only pass with its own batch ids).

## Open

- RRFS/REFS operational date (Oct 6 vs Oct 14, 2026) is still unverified. Herbie
  2026.9.2 ships `rrfs` and `refs` templates on `noaa-rrfs-ops-pds`.
- Backyard station model (D5). It was described as an "AccuWeather" wifi
  station; if that's an AcuRite (e.g. Atlas), it reports light in lux and UV,
  not W/m², and local capture needs rtl_433 or the Access hub. Tempest and
  Ecowitt report W/m² and push locally. Confirm before building the ingest.
- Confirm the PurpleAir index in `.env` is the outdoor sensor (one API call
  with a read key; the LAN `/json` doesn't report the index). Clean channel B.
- How weather.henry.st reaches the internet from the swarm cluster (port
  forward, or a tunnel), and where the lake lives there.
- REFS products available through Herbie: `mean, sprd, pmmn, lpmm, avrg, prob,
  eas, ffri`. Check whether `prob` covers the rain and wind exceedances we need.
