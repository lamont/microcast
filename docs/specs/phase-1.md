# Phase 1 spec · Laptop spike

Goal (from the roadmap in [`design.md`](../design.md#milestones)): show that a
residual model beats raw HRRR at the house at 0–6 h leads before building any
platform. Storage follows [`decisions.md`](../decisions.md) D1 (Iceberg on
local disk from day one); the cloud target follows D2.

**Gate:** LightGBM residual shows clear skill over raw HRRR at 0–6 h for
temperature and gust (CRPS skill > 0 with a bootstrap CI excluding 0, on
rolling monthly folds).

## Done (scaffold, 2026-10-04)

- [x] `uv` package, Python 3.12, ruff, pytest.
- [x] Registry: `config/places.yaml` with `${ENV}` coordinates, routes
  densified to virtual points with bearings (`microcast points`).
- [x] Lake: local PyIceberg SQL catalog, `bronze.nwp_point` and `bronze.obs`
  schemas, append-only bronze writer, partition replace for silver/gold,
  snapshot tagging, DuckDB views (`microcast lake init|snapshots`).
- [x] NWP ingest: Herbie subset → `pick_points(k=4)` → long Arrow → bronze
  (`microcast ingest nwp hrrr --init … --leads 0-6`). Verified live against
  HRRR on AWS.

## Done (pipeline, 2026-10-05)

- [x] **Registry:** truth stations as points (`stations:`), `out_and_back`
  routes, the Golden Gate Park ride (D4), a `sensors:` map with the PurpleAir
  index from `.env` (D5). 27 virtual points.
- [x] **Obs ingest, historical:** `microcast ingest obs` pulls SFOC1 (IEM
  HADS), KSFO and KOAK (IEM ASOS) and FTPC1 (NDBC) a month per batch into
  `bronze.obs`, no keys needed. 2025-04 onward loaded (~594k rows).
- [x] **PurpleAir:** `microcast ingest purpleair` (LAN `/json`, or `--history-days`
  via the API). Not yet run against the real sensor.
- [x] **HRRR backfill:** `microcast backfill hrrr` reads the hrrrzarr archive
  (D6) with a thread pool, appends 96 cycles per snapshot, and resumes from
  snapshot summaries. First pass: every third cycle (rotating hour),
  2025-04-01 → 2026-10-01, f00–f06.
- [x] **Silver:** `microcast build silver` → `silver.obs_qc` (dedupe, range
  flags) and `silver.nwp_aligned` (inverse-distance mean of the 4 cells,
  earth-relative wind, °C), rebuilt a month at a time.
- [x] **Gold:** `microcast build gold` → `gold.training_examples`, with as-of
  features (issue = init + 55 min): last obs, HRRR's error at init, 14-day
  rolling bias and spread.
- [x] **Models 0, 1, 3** behind the `Forecaster` protocol
  (`microcast.models`): `raw_hrrr`, `bias_rolling`, `gbm_residual`.
- [x] **Backtests:** `microcast backtest` runs rolling monthly folds, writes
  `gold.forecasts` and `gold.scores`, logs one MLflow run per model × target
  (`data/mlflow.db`) with the gold snapshot tagged, and writes
  `data/reports/`. `microcast compare` prints the leaderboard.
- [x] **Status site:** `microcast site` builds Status + Analytics pages (D8).

## Done (2026-10-05, later)

- [x] **Synoptic:** `microcast ingest synoptic` for 604PG (Golden Gate Park)
  and five Castro-area CWOP stations; free tier = last 7 days, so they
  accrue (D10). First pull: 43,477 obs.
- [x] **PurpleAir LAN:** the outdoor PA-II is confirmed and polled; a fouled
  B channel is range-flagged; curl fallback for macOS Local Network privacy (D5).
- [x] **Lake portability:** `microcast lake copy --to <catalog>` (D11).
- [x] **Fair comparison:** leaderboard, monthly skill and the site use only
  forecasts every model made (D11).
- [x] **MADIS ingest:** `microcast backfill madis` (free history for the CWOP
  and PG&E stations, D10); station positions in bronze.stations.
- [x] **PurpleAir:** real sensor index in `.env`; channel B cleaned and
  agreeing with A again (D5); API key added, points plan in D10.

## Handoff (2026-10-06, end of session)

Nothing is running. Every load finished and the lake is current to
2026-10-06 ~19Z (Synoptic obs, HRRR to the 18Z run). Rerunning any backfill
command resumes; HRRR for new points inside the kept window reads from disk.

Done this session (details in the Results sections below):

- **HRRR:** hourly cycles 2025-04 → now at the registry points; a
  points-only pass (`--points network --tag net1`) at 42 network stations;
  each cycle's Bay Area window kept in `data/hrrr/window/` (2.4 GB, D10).
- **MADIS:** 18.8M rows, 2025-04 → 2026-10-04. 51 hours missing, mostly the
  last day not yet archived: rerun `backfill madis` to fill.
- **PurpleAir transect:** ten public sensors, hourly temperature 2025-04 →
  now, ~237k API points (763k left). As inputs (`gbm_purpleair`) they cut
  temperature CRPS by 1.2–1.7% and do nothing for gust. No more history
  purchases (D10).
- **Gold and backtest:** 36 truth stations (the original four plus the MADIS
  network). The gate is judged on the original four; it passes (temperature
  +43% / +30%, gust +30% / +27%).
- **Site:** a model picker and hover highlighting, ready for more models.

Evening session (2026-10-06):

- **Terrain:** `microcast build static` → `silver.static_features`, 14
  features at 69 points from a cached 10 m 3DEP grid (`data/terrain/`, 93 MB).
- **Models:** `gbm_terrain`, `gbm_nwp`, `gbm_nwp_terrain`; leave-stations-out
  backtests with `backtest --holdout` (results in `data/reports/holdout/`).
  The rolling backtest was rerun with `gbm_terrain` (gold.scores and
  `data/reports/` are from it).
- **Bridge:** GGBC1's mirrored vane and stuck temperature fixed in silver;
  silver and gold rebuilt after the backtests (gold snapshot
  `4374202609169898617`, 3,318,346 rows; GGBC1 now has no temperature rows).
  The crosswind work is exploratory: its numbers are in the bridge section
  below, the scripts were not kept.

## Next

1. Synoptic: `microcast ingest synoptic` at least weekly (the free tier keeps
   7 days; the trial ends ~2026-10-19, then check what the account becomes).
2. Neighbour obs as features: for a point with no station of its own, the
   nearest stations' last obs and HRRR error at init (as-of, like the
   station's own). Leave-stations-out showed a point's own obs carry most of
   the skill and terrain alone recovers little of it; this is what the house
   (until the backyard station) and the ride need. Score it with
   `backtest --holdout` against `gbm_nwp_terrain`.
3. Bridge crosswind gust in gold: a `xgust` target at GGBC1 (gust ×
   |sin(direction − 354.7°)|, from the corrected vane), HRRR's crosswind
   gust and along-bridge wind as features, and a "crosswind gust > 9 m/s"
   probability. Then a `gg_bridge` route whose points use GGBC1's forecast.
4. Live inference, first cut: the newest HRRR run (live GRIB) through
   `gbm_terrain` for the house, the Castro stations, the ride and the bridge.
   HRRR's raw wind is far too high inside Golden Gate Park (604PG sees ~22%
   of it on afternoons), so the ride needs a corrected wind, not raw HRRR.
5. Data quality, the general version: a per-station check against
   neighbours and HRRR that would have caught GGBC1 (stuck values, mirrored
   or rotated vanes) without a hand-kept list.
6. Dagster on the laptop (`dagster dev`), when picked back up: the ingest,
   backfill and build commands as partitioned assets with schedules and
   per-table concurrency pools, declared in `defs.yaml` where possible (D12);
   then Postgres and the `dagster/dagster` Helm chart on the swarm cluster.
7. A leaner PurpleAir feature set (the two transect summaries only) against
   the 32-feature version.
8. Backyard station (D5); confirm the model first.
9. First look at `lcc` and DSWRF vs the CWOP solar sensors for D2.
10. Status site on k3s, internal at weather.henry.st (D8).

## Not in phase 1

Docker Compose (skipped, D12), MinIO/Lakekeeper, RRFS/REFS ingest, alerts.
Dagster moves into phase 1 as the scheduler (D12).

## Terrain and leave-stations-out (2026-10-06, evening)

`microcast build static` computes 14 terrain features per point into
`silver.static_features` (`transform/static.py`): USGS 3DEP elevation on a
10 m grid over the HRRR window (`ingest/dem.py`, fetched once as 10 km tiles,
cached in `data/terrain/`), HRRR's own terrain height and the gap between the
two, slope and aspect, relative elevation at 300 m and 2 km, distance to water,
overland fetch upwind at 250° and 290°, and Winstral's shelter index (250° at
1 km and 5 km, 070° at 5 km). Water is `elevation <= 0` (3DEP carries
bathymetry) plus flat ground below 2 m (open water outside the bathymetry).
69 points: registry points, MADIS stations, PurpleAir sensors.

Three new models: `gbm_terrain` (`gbm_residual` with terrain in place of
the station id), `gbm_nwp` (HRRR, time and lead only: no obs from the point
itself) and `gbm_nwp_terrain`. `microcast backtest --holdout` splits the 36
stations into 6 fixed groups and tests each group on models trained only on
the other 30, in quarterly rolling folds (6 × 6 fits per model and target,
~1.5 h). Reports in `data/reports/holdout/`, not the lake.

Skill vs raw HRRR, all held-out stations pooled, [95% CI]:

| Model | Temp 1–3 h | Temp 4–6 h | Gust 1–3 h | Gust 4–6 h |
| --- | --- | --- | --- | --- |
| `gbm_nwp` (no station obs, no terrain) | +1.0% [−0.3, +2.4] | +0.5% | +19.7% | +21.5% |
| `gbm_nwp_terrain` (no station obs) | **+8.0%** [+6.7, +9.4] | **+5.9%** | **+22.1%** | **+23.9%** |
| `gbm_residual` (own obs, unseen id) | +37.2% | +21.0% | +47.0% | +41.1% |
| `gbm_terrain` (own obs) | **+41.3%** [+40.3, +42.3] | **+25.6%** | **+51.0%** | **+46.3%** |

- **A point's own recent obs carry most of the skill.** Without them
  (`gbm_nwp`), a pooled GBM can't correct temperature at all at a new place;
  the gain everywhere else in this spec comes from the station's last obs,
  HRRR's error at init and the rolling bias. For the ride and the house today,
  the next step is a nearby station's obs as features, not more terrain.
- **Terrain helps temperature where HRRR's ground is wrong.** Without station
  obs, the biggest gains are on ridges and slopes HRRR flattens: 180PG +28
  points, F6803 +27, PG521 +26, AW038 +20. It hurts at a few (AU915 −19, a
  170 m Sausalito hill where HRRR's terrain is 26 m).
- **Terrain doesn't fix gust at a new place.** Pooled +2 points, per station
  anywhere from −38 to +35. CWOP gust is the sensor's own shelter (backyard
  fences and roofs), which a 10 m DEM can't see. 604PG, the open-park station
  that matters for the ride, gains +16.
- **With the station's obs, terrain beats the station id** by 4–5 points on
  every target and lead bucket, so `gbm_terrain` loses nothing by dropping
  the id, and it can be used at points it never trained on.
- Calibration: the no-obs models' 80% intervals cover only 71–75% of
  outcomes. Their sigma comes from training stations' residuals, which are
  smaller than a new station's.

**Rolling monthly folds** (every station in training, 16 folds, gold snapshot
`3857895018820912251`), skill vs raw HRRR, all 36 / original 4:

| Target | Lead | `gbm_residual` | `gbm_terrain` | `gbm_purpleair` |
| --- | --- | --- | --- | --- |
| Temperature | 1–3 h | +48.7% / +42.7% | +48.5% / +42.6% | +49.0% / +43.1% |
| Temperature | 4–6 h | +35.7% / +29.9% | +35.3% / +29.6% | +36.3% / +30.7% |
| Gust | 1–3 h | +55.3% / +30.5% | +55.3% / +30.7% | +55.1% / +30.3% |
| Gust | 4–6 h | +51.9% / +26.6% | +51.8% / +26.7% | +51.6% / +26.4% |

On known stations terrain ties the station id (CIs ±0.7–1.0 points). So
`gbm_terrain` is the better default for live inference: as good where there
is history, better where there isn't, and it accepts points the registry adds
later.

## Golden Gate Bridge crosswind (2026-10-06, evening)

**GGBC1 is on the bridge**: 37.8198, −122.4790, 82 m, mid-span between the
towers at deck height, reporting wind, gust and direction every 15 minutes
since 2025-04 (via MADIS). Fort Point (FTPC1) sits under the south end at the
waterline.

- **Its vane is mirrored.** It reports (c − true direction): as HRRR's
  direction turns clockwise, GGBC1's turns anticlockwise. Fitting c gives 12°
  against HRRR at the point and 359° against Fort Point, steady month to month
  (December and January, with few strong winds, wander), and the mirror fits
  far better than any rotation (mean resultant length 0.88 vs 0.77). Silver
  now corrects it with c = 6° (`MIRRORED_VANES`); ±6° moves a crosswind by
  under 1%. Its temperature and dewpoint are stuck and now flagged
  `station_fault` (`STATION_FAULTS`).
- **The bridge runs 354.7°** (OSM, the sidewalks), so a crosswind is wind from
  ~85° or ~265°. The sea breeze through the Gate (250–270°) is nearly square
  across the deck; 83% of hours have the wind on the west side.
- **Crosswind gust** = gust × |sin(direction − 354.7°)|. June–July, 3–6 pm
  local: mean 9.4–10.0 m/s, and P(> 9 m/s, ~20 mph) is 60–80%. Winter
  mornings: 2–4 m/s, ~3%.
- **Raw HRRR is already close at the bridge.** Its 10 m crosswind gust
  correlates 0.81 with GGBC1's, bias +0.12 m/s; its plain gust bias at GGBC1
  is −0.1 m/s (vs +3.5 inside Golden Gate Park).
- **A GBM on GGBC1 alone** (gold features plus HRRR's crosswind gust, wind,
  along-bridge wind and GGBC1's last crosswind gust), same monthly folds:
  CRPS skill +19.2% [+17.8, +20.5] at 1–3 h, +16.1% [+14.7, +17.6] at
  4–6 h, MAE 1.13 m/s (~2.5 mph). Brier score for "crosswind gust > 9 m/s"
  0.051 vs raw HRRR 0.063 (base rate 11%). Exploratory: not yet in gold.
- **HRRR's 80 m wind doesn't help.** Sampled at GGBC1 for 150 summer
  afternoons (f01–f06 from 20Z): its crosswind correlates 0.68 with GGBC1's
  crosswind gust vs 0.69 for the 10 m crosswind gust, and adding it to a
  linear fit doesn't lower the error. Not worth a backfill.
- One sensor can't say which sidewalk is worse, or what the towers do to
  gusts locally.

## PurpleAir with vs without (2026-10-06)

`gbm_purpleair` is `gbm_residual` plus 32 transect features from
`gold.network_features`: each of the 10 sensors' 1 h and 3 h change and its
anomaly against the same hour over the last 14 days, the transect mean
anomaly, and west minus east. Only hours complete at init are used. Same
folds, same rows, 36 stations. CRPS change vs `gbm_residual`, paired, with a
95% day-block bootstrap interval (negative = PurpleAir model better):

| Target | Stations | 1–3 h | 4–6 h |
| --- | --- | --- | --- |
| Temperature | all 36 | **−1.2%** [−0.6, −1.8] | **−1.3%** [−0.7, −2.0] |
| Temperature | original 4 | **−1.5%** [−0.8, −2.1] | **−1.7%** [−1.1, −2.4] |
| Gust | all 36 | +0.1% [−0.1, +0.3] | +0.3% [+0.1, +0.5] |
| Gust | original 4 | −0.3% [−0.5, −0.1] | −0.3% [−0.5, 0.0] |

- **Temperature: a small, real gain.** It is largest on the west side and in
  the afternoon: D5422 (west end of the park) −3.1%, FTPC1 −2.2%, 604PG
  −2.0%; afternoons −1.75% vs mornings −0.9%. May–Sep and Oct–Apr are about
  the same (−1.4% vs −1.2%).
- **Gust: nothing.** Expected: PurpleAir has no wind sensor.
- **Verdict on the spend** (~237k points, ~$24, D10): the transect earns a
  place as a temperature input, but it is a percent or two, not a step change.
  Don't buy more history. Hourly live polling (~$1.50/month) is worth running
  for the live model; revisit if a leaner feature set (the two summaries
  only) or the backyard station does as well.

## Results with the network stations (2026-10-06)

Gold now trains and scores at 36 truth stations: the four original ones plus
the MADIS network (Castro and Corona Heights CWOP, 604PG in Golden Gate Park,
PG&E, the Presidio and Marin headlands, harbor stations). PurpleAir stays out
as truth. 3,308,368 training examples, 16 monthly folds, ~0.9M scored
forecasts per target and lead bucket. Gold snapshot `4331126739293301442`.
The rebuild took 14 minutes end to end (silver 78 s, gold 11 s after the
rolling-bias window rewrite, backtest 12 min).

**The original four, now trained alongside the network** (the gate's group):

| Target | Lead | GBM skill, 4 stations only | **GBM skill, trained on 36** [95% CI] |
| --- | --- | --- | --- |
| Temperature | 1–3 h | +42.0% | **+42.5%** [+41.5, +43.7] |
| Temperature | 4–6 h | +30.7% | **+29.8%** [+28.5, +31.1] |
| Gust | 1–3 h | +24.8% | **+30.4%** [+29.0, +31.7] |
| Gust | 4–6 h | +21.2% | **+26.5%** [+24.9, +27.9] |

Training on the network made the original stations' **gust** forecasts about
5 points better; temperature is unchanged within the CIs.

**All 36 stations pooled:** temperature +48.5% / +35.5%, gust +55.3% /
+51.9% (1–3 h / 4–6 h). The GBM beats raw HRRR in every month (weakest +31%).

- Castro and park stations, temperature 1–3 h: F2543 +41%, 604PG +43%, E9227
  +45%, SFOC1 +46%, F4637 +48%, C5988 +54%, F6803 +58%.
- **Read the pooled gust number with care.** Raw HRRR's gust is too high by a
  median 1.5 m/s across the stations and by up to 6.3 m/s at sheltered
  backyard CWOP sites (D10: CWOP wind is local exposure). Much of the GBM's
  gust skill there is learning each site's shelter, which is real for that
  sensor but isn't weather skill. 604PG (open park, +72%) and the original four
  are the honest gust numbers.
- GGBC1 (Golden Gate Bridge) shows −9% temperature skill on only 3 scored rows
  with 26 °C errors: bad observations that pass the range check. Fixed
  2026-10-06: flagged `station_fault` in silver (see the bridge section).

## Results on hourly cycles (2026-10-05, evening)

Same folds and models on the hourly HRRR backfill (~3× the cycles): 364,784
training examples, 15 monthly test folds, leads 1–6 scored. Gold snapshot
`5451605693666611693`.

| Target | Lead | raw HRRR CRPS | Rolling bias skill | **GBM residual skill** [95% CI] | n |
| --- | --- | --- | --- | --- | --- |
| Temperature | 1–3 h | 0.78 °C | +20.5% | **+42.0%** [+40.7, +43.3] | 130,014 |
| Temperature | 4–6 h | 0.82 °C | +17.2% | **+30.7%** [+29.3, +32.2] | 130,014 |
| Gust | 1–3 h | 1.33 m/s | +6.0% | **+24.8%** [+23.1, +26.3] | 97,524 |
| Gust | 4–6 h | 1.39 m/s | +6.6% | **+21.2%** [+19.2, +23.0] | 97,524 |

- Three times the cycles changed skill by under a point: the stride-3 sample
  was already representative, and the extra data mostly narrowed the CIs.
- The GBM beats raw HRRR in all 30 month × target cells. Weakest: gust in
  November (+11%) and December 2025 (+13%).
- The GBM's gust forecasts run slightly high (+0.16 to +0.19 m/s bias), well
  under raw HRRR's +0.60 to +0.76.

## Results: gate passed (2026-10-05, stride-3)

First backtest on the stride-3 backfill: 4,247 HRRR cycles (2025-04-01 →
2026-09-30, f00–f06), four truth stations, 15 monthly test folds
(2025-07 → 2026-09), leads 1–6 scored. Gold snapshot `3316482051644290483`, tagged per MLflow run.

| Target | Lead | raw HRRR CRPS | Rolling bias skill | **GBM residual skill** [95% CI] |
| --- | --- | --- | --- | --- |
| Temperature | 1–3 h | 0.78 °C | +20.5% | **+41.0%** [+39.6, +42.5] |
| Temperature | 4–6 h | 0.83 °C | +17.3% | **+30.1%** [+28.6, +31.7] |
| Gust | 1–3 h | 1.34 m/s | +5.8% | **+24.8%** [+23.0, +26.4] |
| Gust | 4–6 h | 1.39 m/s | +6.4% | **+20.9%** [+18.9, +22.9] |

- The GBM beats raw HRRR at **every station and in every test month**.
  Weakest: December 2025 gust (+8%). SFOC1, the house's stand-in: +43% / +33%
  on temperature.
- Raw HRRR runs ~0.7 °C cold on average. At SFOC1 it is too warm from late
  morning to mid-afternoon in summer (the marine layer it misses) and too cold
  at night and all winter (Analytics → "Where HRRR goes wrong").
- Calibration: 80% intervals cover 81–83%. The GBM's gust PIT is slightly
  humped (sigma a little wide).
- On six months of data the GBM kept a −0.35 °C bias; with a full year of
  training it is −0.05 °C. Fitting the GBM on top of the rolling-bias forecast
  gained ~1 point on the six-month data (within the CI) and is not adopted.
- Archive gaps: 40 of 4,287 cycles (0.9%), mostly the 23Z forecast, recurring
  since February 2026.

**Caveat.** Truth is at stations, not the house: SFOC1 is ~1 km away and
there is no in-city wind truth yet. The gate says the method works on this
data; it does not yet measure the house or the ride.
