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

## Handoff (2026-10-05, end of session)

Two loads were running when this was written; both resume where they stop.

- **HRRR hourly fill** (`backfill hrrr --stride 1`): **finished**, +8,482
  cycles (≈13,000 of 13,176 hourly cycles 2025-04 → 2026-09; 124 missing
  from the archive, mostly 23Z; fillable from GRIB).
- **MADIS** (`backfill madis --start 2025-04-01 --end 2026-10-05T21`): about
  13–14 h total at 3 workers, under `caffeinate`. Log
  `data/logs/backfill-madis.log`.

Then, in order:

1. ~~Rebuild on hourly cycles~~ **done 2026-10-05 21:00**: see "Results on
   hourly cycles" below. MADIS finished (18.8M rows; 51 hours missing, mostly
   the last day not yet archived: rerun `backfill madis` to fill), and the
   PurpleAir transect is in bronze (115,110 rows).
2. HRRR at the Synoptic/MADIS stations: **done 2026-10-06 00:35**,
   `backfill hrrr --points network --tag net1`: 42 points, 13,141 cycles
   (125 missing from the archive), 200.9M rows (bronze.nwp_point 184 →
   705 MB). The Bay Area window is kept for every cycle: 2.4 GB under
   `data/hrrr/window/` (D10). **The stations are in gold and the backtest**
   (2026-10-06 01:44): see "Results with the network stations" below.
3. PurpleAir: discovery done, budget agreed (250k points). Nine transect
   sensors, now ten (Ocean Beach → Castro, `kind: purpleair_network` in
   `config/places.yaml`) are pulled as hourly temperature, 2025-04 → now, by
   `microcast backfill purpleair` into `data/purpleair/history/` (stops at
   750k points left). **Pulled 2026-10-05:** 117,709 rows (ten sensors) for about 237k points
   (763,003 left, about $24 at $10 per 100k). pa_110410 (Noriega, history only
   from 2026-09-15) was swapped for pa_105692, and pa_117947 (States St) added
   because pa_284234 starts 2025-07-22 with about half its hours missing;
   pa_295103 starts 2025-12-23. The dropped sensor's responses stay in the
   cache and aren't loaded. Once MADIS is done: `microcast backfill purpleair --load`
   appends them to bronze.obs and bronze.stations. Live polling will be hourly,
   not every 10 min. **Judged 2026-10-06**: a small, real temperature gain
   (see "PurpleAir with vs without"); no more history purchases.
4. Scheduled collectors as Dagster jobs on the swarm k3s cluster (D12), not
   CronJobs: Synoptic daily (the free tier keeps only 7 days, so pull from the
   laptop at least weekly until then), PurpleAir LAN every 2 min, HRRR live
   hourly; status site internal at weather.henry.st (D8).

## Next

1. Finish the hourly HRRR fill, rebuild, rerun, refresh the site.
2. Dagster on the laptop (`dagster dev`): wrap the ingest, backfill and
   build commands as partitioned assets with schedules and per-table
   concurrency pools, declared in `defs.yaml` where possible (D12). Then
   Postgres and the `dagster/dagster` Helm chart on the swarm cluster.
3. HRRR at the Synoptic stations: a points-only pass for the weeks they have
   obs (D10), then they enter backtests once ~4 months have accrued.
4. Backyard station (D5); confirm the model first.
5. First look at `lcc` and DSWRF vs the CWOP solar sensors for D2.
6. Status site on k3s, internal at weather.henry.st (D8).

## Not in phase 1

Docker Compose (skipped, D12), MinIO/Lakekeeper, RRFS/REFS ingest, alerts.
Dagster moves into phase 1 as the scheduler (D12).

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
  with 26 °C errors: bad observations that pass the range check. To do: a
  per-station sanity check in silver.

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
