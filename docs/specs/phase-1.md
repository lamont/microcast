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

## Handoff (2026-10-07)

Nothing is running. Lake current to 2026-10-07: HRRR to the 15Z run
(registry and `net1` network points), Synoptic to ~18:35Z, IEM/NDBC to
~19Z, MADIS to 10-05 ~17Z (the archive lags; rerun `backfill madis`).
Silver rebuilt for Sep–Oct, gold rebuilt (snapshot `7225361806880371065`,
3,323,820 rows). Neighbour features and the leave-stations-out result are
below; the previous holdout reports are in `data/reports/holdout-2026-10-06/`.

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
  purchases (D10). **The ingest was removed 2026-10-16;** the history stays
  (see "PurpleAir transect: ingest removed").
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

## Handoff (2026-10-18)

Done this session, details in the sections below:

- **Regime, lagged, upwind:** all adopted; together as `gbm_terrain_stack`
  (+2-3 points of temperature skill, +1 of gust). HRRR backfilled 2025-04 →
  2026-10-17 17Z with the regime fields and leads 7-12 of t2m/gust (tags
  `regime`, `net1-regime`, `lag`, `net1-lag`); window cache now 3.3 GB. Silver
  and gold rebuilt (gold `400097479855311500`, 3,383,856 rows; upwind
  features 269,527 rows).
- **RRFS:** `backfill rrfs` backfilled 2026-08-13 → 2026-10-18 23Z (D13):
  1,604 of 1,608 cycles, 18.2M rows. Four hours have no `.idx` on S3
  (2026-08-13 17Z, 09-14 17Z and 21Z, 09-15 04Z) and stay missing; dropped
  connections are retried by a rerun.
- **Storage:** bronze metadata pruned (last 10 versions per table, properties
  set by `init_lake`); 2,087 orphaned metadata.json files
  (2.6 GB) deleted. Old silver/gold snapshots not yet expired.
- **Catch-up:** `data/logs/catchup-2026-10-18.sh` replaces the 10-17 script:
  default HRRR pass (regime included), the two lag passes, then RRFS.
- Not committed.

Next up: make `gbm_terrain_stack` the default in live inference and the
site (Next #4); the lagged ensemble held out; then queue #5 (`xgust`).

## Experiments: tried and queued

Every forecasting experiment gets a row here when it starts, and its verdict
when it ends. Failed ones keep their row: the code goes, and the row says
which commit restores it. Details live in the dated sections further down.

**Tried.** Skill is CRPS skill vs raw HRRR at 1-3 h / 4-6 h unless noted.

| Date | Experiment | Result | Verdict |
| --- | --- | --- | --- |
| 10-05 | GBM residual vs raw HRRR (`gbm_residual`), 4 stations | temp +41% / +30%, gust +25% / +21% | Adopted: the phase 1 gate |
| 10-05 | GBM fitted on top of `bias_rolling` | ~+1 point, inside the CI | Not adopted |
| 10-05 | Hourly cycles instead of every third | under 1 point, narrower CIs | Kept hourly |
| 10-06 | Train on the 36-station MADIS network | gust +5 points at the original four | Adopted |
| 10-06 | PurpleAir transect changes (`gbm_purpleair`) | temp CRPS -1.2 to -1.7%, gust 0 | Model kept frozen; ingest removed 10-16 (cost), restorable from e28a5e2 |
| 10-06 | Terrain in place of the station id (`gbm_terrain`) | ties on known stations, +4-5 points unseen | Adopted as the default |
| 10-06 | HRRR 80 m wind for bridge crosswind | r 0.68 vs 0.69 for 10 m; no gain | Rejected, never backfilled |
| 10-06 | Bridge crosswind gust GBM at GGBC1 | +19% / +16%, Brier 0.051 vs 0.063 | Promising; `xgust` in gold is Next #3 |
| 10-07 | Neighbour obs (`gbm_nwp_neighbours`) | no-own-obs temp +3.5 points, gust -2 | Temperature only (Next #2) |
| 10-11 | HRRR's 4 cells kept apart | no change beyond the CIs | Removed; restorable from be498f1 |
| 10-12 | NBM as a benchmark | NBM +2-6% vs raw; ours 31-54% lower CRPS than NBM | Benchmark only; rerun after NBM takes RRFS (11-03) |
| 10-18 | Regime features: inversion, 925 mb flow, PBL height (`gbm_*_regime`) | own obs: temp -0.9% / -2.5%, gust -0.9% / -1.1%; held out: temp -4.6% / -4.8%, gust 0 (split by station) | Adopted; not for gust without own obs |
| 10-18 | Time-lagged HRRR ensemble, as features and as sigma (`gbm_terrain_lagged*`) | features: -0.4 to -0.8%; spread as sigma a further -0.3 to -0.9%, coverage unchanged | Adopted |
| 10-18 | Does the coast's push travel inland? (MADIS, event odds) | ridge x6 at 75-90 min, east side x2-3 at 1.5-3.5 h; loose timing past the ridge | Real signal; built as `up_*` features |
| 10-18 | Same, as continuous 30-min changes (lagged correlation) | r 0.1-0.2, peak at 0-20 min | No wave to track at 10-min scale; use events and group changes |
| 10-18 | Upwind features in the GBM (`gbm_*_upwind`) | own obs: temp -1.4% / -1.7%, gust 0; held out: temp -3.0% / -2.5%, gust -1.2% / -1.0%; SFOC1 held out -5.6% / -6.4% | Adopted for temperature |
| 10-18 | All three together (`gbm_terrain_stack`) | they add up: skill temp +48.6/+35.4% → +50.6/+38.4%, gust +55.3/+51.8% → +56.1/+52.8% | Adopted: candidate default for points with own obs |

**Queued**, in order. Status: *running*, *next*, *later*, *waiting* (on data or a date).

| # | Experiment | Why | Status |
| --- | --- | --- | --- |
| 1 | Regime features (marine layer lid, onshore flow, PBL depth) | Errors learnt per regime, not per place; aimed at no-own-obs temperature and 4-6 h | done 10-18: adopted (see "Regime features") |
| 2 | Time-lagged HRRR ensemble (6 earlier cycles) | A weather-dependent spread for sigma, and run-to-run trend; no downloads | done 10-18: adopted; held-out test still to do |
| 3 | RRFS: collect at our points | HRRR's successor; history only accrues | collecting since 10-18; compare from 2027-04 (see "RRFS: collecting, not using") |
| 4 | Upwind changes from MADIS/Synoptic stations, and how they travel across the city | What the PurpleAir transect gave, from stations we already pull for free; does the fog edge follow Twin Peaks and Corona Heights? | done 10-18: adopted (see "Upwind changes") |
| 4a | Upwind, per point: weight the push by each point's own path (stations to its west, its shelter) instead of three city-wide groups | Timing past the ridge varies 95-220 min within a neighbourhood; city-wide groups can't say which pocket gets it when | later |
| 5 | Bridge crosswind gust target (`xgust`) and P(> 9 m/s) | The ride's main question | next (Next #3) |
| 6 | RTMA-RU (2.5 km, 15 min) as a stand-in for a point's own obs | Own obs carry most of the skill; RTMA gives every point an analysed "now". Leaks in holdouts (it assimilates MADIS), so judge on the backyard station | later |
| 7 | Synoptic pressure gradient (SFO minus Sacramento, HRRR) | The classic sea-breeze and fog-push predictor; needs a point outside the kept window | later |
| 8 | Analog ensemble over HRRR + obs | A calibrated challenger to the Gaussian sigma | later |
| 9 | REFS ceiling probabilities (< 152-3050 m) for the marine-layer target (D2) | Ensemble odds of low ceiling, every 6 h | waiting: RRFS history, and the cloud target |
| 10 | NBM benchmark rerun | NBM swaps NAM/HiresW for RRFS/REFS on 11-03 (SCN 26-89) | waiting: 2026-12 |
| 11 | GOES-18 low-cloud / clear-sky mask | The best fog-edge signal there is | later (phase 3) |
| 12 | Weather Underground stations as truth stations (`wu` source: current obs + history by date), starting with a friend's station near Corona Heights (id and position in `.env`, alias only in git and on the site) | More stations with their own obs in the city; it isn't on CWOP/MADIS | waiting: our own WU API key, once the backyard station (D5) is on WU |

## Next

1. Synoptic: `microcast ingest synoptic` at least weekly (the free tier keeps
   7 days; the trial ends ~2026-10-19, then check what the account becomes).
   GGBC1 joined the Synoptic stations 2026-10-09. Doing without Synoptic:
   D10 "Real time without Synoptic" (MADIS real-time covers everything;
   GGBC1 arrives ~1 h late there, costing ~4 points of bridge gust skill).
2. Neighbour obs, follow-ups (first pass done 2026-10-07, see below):
   neighbour features for temperature only (they cost gust 2 points), and a
   no-own-obs sigma that matches its 70–74% coverage. Bridge gust must keep
   GGBC1's own obs: held out, every model is 40%+ worse than raw HRRR there.
   Ranked research directions for places without a station: see "HRRR
   cells kept apart" below (abandoned approaches are recorded there too).
3. Bridge crosswind gust in gold: a `xgust` target at GGBC1 (gust ×
   |sin(direction − 354.7°)|, from the corrected vane), HRRR's crosswind
   gust and along-bridge wind as features, and a "crosswind gust > 9 m/s"
   probability. Then a `gg_bridge` route whose points use GGBC1's forecast.
4. Live inference, first cut: the newest HRRR run (live GRIB) through
   `gbm_terrain` for the house, the Castro stations, the ride and the bridge.
   Tried by hand on 2026-10-07 (scratch script, not kept): live HRRR at
   GGBC1, FTPC1, 604PG and F4637 through `nwp.fetch_cycle`, fresh Synoptic
   obs, `silver.build_*` and `gold.build_training` in memory, `gbm_terrain`
   fit on all of gold (~1 min per target). It works; make it a command.
   HRRR's raw wind is far too high inside Golden Gate Park (604PG sees ~22%
   of it on afternoons), so the ride needs a corrected wind, not raw HRRR.
5. Data quality, the general version: a per-station check against
   neighbours and HRRR that would have caught GGBC1 (stuck values, mirrored
   or rotated vanes) without a hand-kept list.
6. Dagster on k3s (D12), picked back up 2026-10-16. Superseded by
   [`dagster-k8s.md`](dagster-k8s.md) (2026-10-19); the history below stays. Done: one multi-arch
   (amd64 + arm64) image, `deploy/docker/Dockerfile`, built by CI and pushed to
   `ghcr.io/lamont/microcast` on `main` (`deploy/docker/publish.sh` from the
   laptop); mirroring it to the Gitea registry at gitea.henry.st comes later.
   Then, in order:
   1. Postgres on k3s as the Iceberg catalog (PyIceberg's SQL catalog), a
      volume for the warehouse, and the lake moved with `lake copy` (D11): the
      laptop's Iceberg metadata holds absolute laptop paths, so its `data/`
      can't simply be mounted.
   2. One plain k8s Job, `microcast ingest synoptic`, to prove the image,
      secrets, catalog and storage before Dagster (and to take the weekly
      Synoptic pull off the laptop).
   3. Dagster: `dagster dev` on the laptop with the ingest, backfill and build
      commands as partitioned assets, schedules and per-table concurrency
      pools, declared in `defs.yaml` where possible; then the
      `dagster/dagster` Helm chart with this image as the user-code
      deployment.
7. ~~A leaner PurpleAir feature set (the two transect summaries only) against
   the 32-feature version.~~ Moot: the transect ingest was removed 2026-10-16.
8. Backyard station (D5); confirm the model first.
9. First look at `lcc` and DSWRF vs the CWOP solar sensors for D2.
10. Status site on k3s, internal at weather.henry.st (D8).

## Not in phase 1

Docker Compose (skipped, D12), MinIO/Lakekeeper, REFS ingest, RRFS as a model
input (it is collected from 2026-10-18, D13), alerts.
Dagster moves into phase 1 as the scheduler (D12).

## Upwind changes: how the marine push crosses the city (2026-10-18)

Question: the PurpleAir transect's small gain came from temperature drops
and RH jumps moving inland from Ocean Beach. Do the MADIS stations we pull
for free show the same push, and does it follow the terrain (Twin Peaks,
Corona Heights)?

Data: 10-minute t2m and RH at the MADIS/Synoptic stations, May-Oct 2025 and
2026, 11:00-22:59 local. A **push** at a station: temperature down 2 C and RH
up 8 points within an hour; its onset is the first such 10 minutes of the
day. Coast = 604PG, D5422 (west end of Golden Gate Park), AV684, F8022
(Pacifica); 36 afternoons had a coastal push. Scripts (not in git):
`data/logs/upwind-explore/`.

**Odds of a push inland after one at the coast**, against afternoons with
none at the coast, and the median delay:

| Station | Where | km east | Elev. | P(push), no coast push | P(push), after one | Lift | Median delay |
| --- | --- | --- | --- | --- | --- | --- | --- |
| PG800 | Daly City hills | 3.0 | 86 m | 0.10 | 0.61 | x6.4 | 75 min |
| 070PG | Twin Peaks south ridge | 5.8 | 164 m | 0.10 | 0.61 | x5.9 | 90 min |
| C5988 | Castro | 6.7 | 46 m | 0.15 | 0.34 | x2.3 | 95 min |
| F4637 | Hayes Valley / Duboce | 7.5 | 17 m | 0.18 | 0.51 | x2.8 | 155 min |
| G3010 | Bernal / Mission | 8.6 | 46 m | 0.20 | 0.56 | x2.8 | 190 min |
| E5830 | Glen Park | 6.8 | 79 m | 0.15 | 0.42 | x2.8 | 220 min |
| E9227 | Corona Heights east | 6.3 | 110 m | 0.18 | 0.33 | x1.9 | 205 min |
| 438PG | Bayview | 11.0 | 5 m | 0.18 | 0.61 | x3.3 | 130 min |
| PG733, PG521, AU915 | Marin | | | | | x0.4-1.3 | |

* **Yes, it travels, and the first ridge is the gate.** The Daly City gap and
  Twin Peaks' south ridge see it 75-90 min after the coast, with six times
  the usual odds. The east side follows 1.5-3.5 h after the coast at two to
  three times the odds. Marin, on another path, gets no lift.
* **Timing past the ridge is loose.** Delay vs distance east r = 0.28 among
  the stations with a lift; a fit gives ~53 min + 11 min/km east + 18 min
  per 100 m of height, but the scatter within a neighbourhood (95 to 220 min
  across the Castro, Glen Park and Corona Heights) is as big as the trend.
  Sheltered pockets and the push's depth on the day matter more than
  distance.
* **Not a crisp front at 10-minute scale.** A lagged correlation of
  de-seasoned 30-minute temperature changes, coast to each station, is weak
  (r 0.1-0.2) and peaks at 0-20 min. Day-to-day changes are mostly
  city-wide; the push shows up as an event, not a wave you can track minute
  by minute with ~25 stations.
* Corona Heights' F2543 reports no RH, so the detector never fires there;
  AW038 (Hunters Point) fires on 69% of afternoons with or without the coast,
  so it carries no signal.

**As features** (`transform/upwind.py`, gold.upwind_features, the `up_*`
columns): for the coast, ridge and east groups, the mean of each station's
1 h and 3 h change and its anomaly against its own trailing 14 days at the
same time of day, for temperature and RH; coast minus east; and minutes
since today's coastal push. As-of: the 10-minute bucket ending at init.
Models `gbm_terrain_upwind` (with own obs) and `gbm_nwp_terrain_upwind`
(without).

**Results** (`backtest/ablation.py`, in memory, gold snapshot
`1140949246963792470`; reports in `data/reports/experiments/upwind-*`). CRPS
change vs the same model without `up_*`, paired on identical rows, 95%
day-block bootstrap CI; negative = upwind better:

| | Temp 1-3 h | Temp 4-6 h | Gust 1-3 h | Gust 4-6 h |
| --- | --- | --- | --- | --- |
| With own obs, rolling folds (`gbm_terrain_upwind`) | **-1.4%** [-0.9, -2.1] | **-1.7%** [-1.1, -2.3] | -0.1% [-0.3, +0.1] | 0.0% [-0.2, +0.2] |
| No own obs, held out (`gbm_nwp_terrain_upwind`) | **-3.0%** [-2.2, -3.8] | **-2.5%** [-1.6, -3.4] | **-1.2%** [-1.0, -1.5] | **-1.0%** [-0.8, -1.3] |

* **The PurpleAir transect's gain, for free.** With own obs it is the same
  size as `gbm_purpleair`'s (-1.2 to -1.7%), larger at 4-6 h than 1-3 h, as a
  push taking hours to cross would be. Gust: nothing, as before.
* **More where there's no station**, the house and ride case: SFOC1 (the
  house's stand-in) held out **-5.6% / -6.4%**, F4637 -8.1% / -5.3%, 604PG
  -7.8% / -4.5%, KSFO -4.0% / -2.2%. Better at 18 of 31 stations (1-3 h),
  worse at 5 (F6803 and D5422 among the near-zero ones).
* **Not a leak.** A held-out station that is in a group still feeds its own
  obs into that group's means. Stations outside the groups gain as much
  (held out, temperature -2.6% / -2.5% vs -3.2% / -2.4% for members;
  rolling, better at 10 of 14 and worse at none).
* Gust without own obs gains a point: GGBC1 -2.2%, F4637 -3%; FTPC1 +2.7%
  worse (the Fort Point anemometer sits under the bridge).

Verdict: **adopted as an input for temperature.** It costs nothing (MADIS
stations we already pull, in real time from MADIS or Synoptic) and replaces
what the transect gave. Live inference should use it with `gbm_terrain` for
temperature; gust stays on `gbm_terrain` for points with their own obs.

## Regime features and the time-lagged HRRR ensemble (2026-10-18)

Two ideas that need no new source, only more of HRRR.

**Regime ("the fog").** The marine layer is a lid: cool, moist air under a
warm inversion, pushed in by onshore flow. HRRR knows the lid even where it
gets the surface wrong, so the GBM gets, per point and cycle: 925 and 850 mb
temperature, 925 mb wind, PBL height (`nwp_t925`, `nwp_t850`, `nwp_u925`,
`nwp_v925`, `nwp_hpbl`, added to the default HRRR pass and backfilled
2025-04 →), and derived from them the inversion (`inv925` = T925 - T2m,
`inv850`), the 925 mb wind speed and its onshore component through the Gate
(`onshore925`, from 270°). Models `gbm_terrain_regime` and
`gbm_nwp_terrain_regime`.

**Lagged ensemble.** For each forecast, the six earlier hourly HRRR cycles'
forecasts for the same valid hour (so leads up to 12 h, backfilled from the
kept window with `--variables t2m,gust --leads 7-12`). As features: how many
there are, their mean, their spread and the newest run's change
(`lag_n/mean/sd/d1_*`; `gbm_terrain_lagged`). As sigma: sigma² = a + b ·
spread² per lead, fitted on the model's held-out 15% (`SpreadSigma`;
`gbm_terrain_lagged_sigma`).

**Results** (`backtest/ablation.py`, gold snapshot `400097479855311500`,
reports in `data/reports/experiments/regime-*`; scripts
`data/logs/regime-lag-ablation-2026-10-18.py`). CRPS change vs the model
without, paired rows, 95% day-block CI; negative = better:

| | Temp 1-3 h | Temp 4-6 h | Gust 1-3 h | Gust 4-6 h |
| --- | --- | --- | --- | --- |
| Regime, own obs, rolling (`gbm_terrain_regime`) | **-0.9%** [-0.3, -1.4] | **-2.5%** [-1.8, -3.1] | **-0.9%** [-0.7, -1.1] | **-1.1%** [-0.9, -1.3] |
| Regime, no own obs, held out (`gbm_nwp_terrain_regime`) | **-4.6%** [-3.9, -5.4] | **-4.8%** [-4.0, -5.6] | -0.1% [-0.3, +0.1] | -0.2% [-0.4, +0.0] |
| Lagged as features, rolling (`gbm_terrain_lagged`) | **-0.6%** [-0.4, -0.9] | **-0.4%** [-0.2, -0.6] | **-0.8%** [-0.7, -1.0] | **-0.7%** [-0.6, -0.9] |
| Lagged spread as sigma, vs lagged (`gbm_terrain_lagged_sigma`) | **-0.9%** [-0.8, -1.0] | **-0.7%** [-0.6, -0.8] | **-0.3%** [-0.2, -0.4] | **-0.3%** [-0.2, -0.4] |

* **Regime is the bigger of the two, and where it was aimed.** Temperature
  at 4-6 h gains 2.5% with own obs (skill +35.4% → +37.0%), and without own
  obs, the house and ride case, nearly 5% (skill +5.4% → +9.9% at 4-6 h;
  +7.5% → +11.7% at 1-3 h). Better at every one of 31 stations with own obs
  (4-6 h; most at PG521, G6226, 180PG, AV684, 3-4%). Held out, 21-23 of 31
  stations are better beyond the CI (AU915, G5625 and F4637 by 9-13%) and two
  worse: G6226 (+8-10%) and PG733 or F6992 (+1-2%).
* **Gust: a point with own obs, a split without.** Rolling, better at 28 of
  32 stations. Held out it nets to zero, but that hides 11-12 stations
  better and 11-12 worse beyond their CIs, GGBC1 (+1.5%), 604PG (+3.5%) and
  PXOC1 (+5-6%) among the worse. Without own obs, keep gust off regime.
* **Lagged: small but everywhere.** Under 1% each, CIs clear of 0, better at
  28-30 stations of 31-32. The one worse beyond its CI is F6992 (gust, +2-5%),
  which reports gust rarely (1,011 scored rows, vs ~33k at most stations). The run-to-run trend is mostly
  information the latest run already has.
* **Spread as sigma helps CRPS without a coverage cost:** better at 29-30
  stations; 80% intervals catch 83.5-84.3%, vs 83.7-84.4% with the plain
  per-lead sigma.
* F6992 is worse with regime gust too (+19-22% with own obs), on the same
  thin sample; not enough to act on.

**Do they stack?** (`data/logs/stack-ablation-2026-10-18.py`, reports in
`data/reports/experiments/stack-rolling/`.) Yes. Each step against the one
before it, rolling folds with own obs:

| | Temp 1-3 h | Temp 4-6 h | Gust 1-3 h | Gust 4-6 h |
| --- | --- | --- | --- | --- |
| + regime, on top of upwind | **-0.8%** [-0.3, -1.4] | **-1.8%** [-1.3, -2.4] | **-0.6%** [-0.5, -0.8] | **-0.9%** [-0.8, -1.1] |
| + lagged features and spread sigma, on top of both | **-1.9%** [-1.5, -2.2] | **-1.4%** [-1.1, -1.8] | **-1.0%** [-0.8, -1.2] | **-1.0%** [-0.8, -1.2] |

CRPS skill vs raw HRRR, all stations:

| | Temp 1-3 h | Temp 4-6 h | Gust 1-3 h | Gust 4-6 h |
| --- | --- | --- | --- | --- |
| `gbm_terrain` | +48.6% | +35.4% | +55.3% | +51.8% |
| + upwind | +49.3% | +36.4% | +55.4% | +51.8% |
| + regime | +49.7% | +37.5% | +55.7% | +52.3% |
| + lagged, spread sigma (`gbm_terrain_stack`) | **+50.6%** | **+38.4%** | **+56.1%** | **+52.8%** |

Coverage of the 80% interval stays 83.6-84.3%. Better at 29-30 of 31-32
stations at every step; the only station worse beyond its CI is F6992 (thin
gust sample, above) and KOAK at temp 1-3 h with regime (+0.8%).

Verdict: **adopted.** `gbm_terrain_stack` (terrain + regime + upwind + lagged
features, sigma from the lagged spread) is the candidate for points with their
own obs, temperature and gust. For points without obs: regime and upwind for
temperature (`gbm_nwp_terrain` + both), and for gust upwind only (regime
splits stations there). The lagged ensemble wasn't run held out. Swapping the
live and site default from `gbm_terrain` to the stack is Next #4's job.


## RRFS: collecting, not using (2026-10-18)

RRFS v1 replaces NAM, HREF, SREF and HiresW on **2026-11-03 at 12Z** (SCN
26-48, revised 10-15; the date moved from 08-31, 10-14 and 10-06). HRRR
stays: it is two of REFS's members, and NOAA has announced no HRRR changes
beyond a guess-file swap (PNS 26-70). The pre-operational feed has been on
AWS (`noaa-rrfs-ops-pds`) since 2026-08-13.

`microcast backfill rrfs` (`ingest/rrfs.py`, D13) collects RRFS at the registry
points and MADIS stations: hourly cycles, leads 1-6, the 4 nearest cells, eight
fields (T2m, dewpoint, 10 m wind, gust, visibility, ceiling, DSWRF) from the
top-of-hour step of the `2dfld.3km.subh` file. That is ~14 MB read per lead and
under 1 MB of bronze per cycle (~0.3 GB a year). RRFS runs on HRRR's grid
(1799 x 1059 Lambert, LoV 262.5, grid-relative winds), so silver aligns it
with HRRR's code (`model = 'rrfs'`). Gold ignores it.

**As-of rule for when it is used:** RRFS publishes later than HRRR. S3
Last-Modified on 2026-10-17: f06 of an hourly cycle at +86 to +97 min, of the
12Z cycle at +124 min (HRRR f06: +62 min). So at our issue time (HRRR init +
55 min) the newest RRFS is the cycle 1-2 hours older. Use `available_at =
init + 130 min` and take the latest RRFS cycle available by issue time.

**When to compare:**
* **2027-01, a first look:** RRFS vs HRRR raw skill at our stations
  (Aug-Dec), and whether the RRFS - HRRR difference correlates with HRRR's
  error. Diagnostic only.
* **2027-04, the real test:** with a winter's rain season in hand (Nov-Mar),
  add RRFS (and RRFS - HRRR) as features to `gbm_terrain` in the rolling
  backtest, scored only on months that have RRFS. Adopt if the CI excludes 0.
* Flag the 11-03 switch from the parallel to the operational feed; same
  code (v1.0), but check for a step in RRFS's bias around it.
* REFS (`prob`: ceiling, visibility and 10 m wind exceedances; `mean`/`sprd`:
  T2m, wind, low cloud, 925/850 mb) is not collected. Revisit with the cloud
  target (queue #9).

Running it: in the catch-up script (`data/logs/catchup-2026-10-18.sh`, after
the HRRR passes, one writer at a time) until Dagster takes it over; every day
or two is enough (the default window is the last 3 days, and it resumes). The bucket's retention isn't stated; the prototype bucket
(`noaa-rrfs-pds`) now holds only retrospectives, so don't count on it keeping
everything.

## NBM benchmark (2026-10-12)

The operational forecast a researcher would compare against: NWS's National
Blend of Models (`core` product, 2.5 km, blend mean plus its ensemble
spread). One run a day, rotating hour, 2025-07-01 to 2026-10-05 (462 runs,
`microcast backfill nbm`), at the 33 backtest stations, 4 nearest cells
inverse-distance weighted. Scored on the rolling backtest's own rows from
gold.scores (`microcast backtest --nbm`, data/reports/nbm/). The NBM run of
the same hour as our HRRR cycle: published 42-62 min after its hour, so it
was in hand at our issue time (init + 55 min) on most days, slightly
generous to NBM. `nbm` uses NBM's own spread as sigma; `nbm_cal` uses
NBM's RMS error per lead from the other months (as raw_hrrr does; also
generous, it sees later months).

CRPS skill vs raw HRRR, same ~38-39k rows per cell:

| | temp 1-3 h | temp 4-6 h | gust 1-3 h | gust 4-6 h |
|---|---|---|---|---|
| nbm (own spread) | +4.3% | +3.4% | -9.3% | -5.6% |
| nbm_cal | +5.6% | +5.6% | +1.6% | +4.8% |
| bias_rolling | +19.3% | +17.7% | +31.9% | +33.8% |
| gbm_terrain | +47.9% | +34.9% | +54.9% | +51.7% |

MAE, all stations, 1-3 h: temp NBM 1.27 C, ours 0.69 C; gust NBM 2.68 m/s,
ours 1.19 m/s. Our CRPS is 45% lower than nbm_cal for temperature at 1-3 h
(31% at 4-6 h) and 54% lower for gust (49%).

* At these lead times NBM is barely better than raw HRRR. Even at the
  airports, where its URMA calibration is anchored, temp 1-3 h is +16%
  (KSFO) and +2% (KOAK) against ours at +37% and +41%. NBM's strength is
  days ahead; for the first hours it is mostly a blend of the same
  short-range models without the latest station obs.
* NBM's own spread is too narrow at these stations: 80% intervals catch 54%
  (temp) and 35% (gust) of outcomes. Hence the two variants.
* NBM gust runs high, like HRRR: +1.5 m/s on average, +3.3 m/s at 604PG
  (sheltered park). At the bridge it is the one place NBM gust is worse
  than raw HRRR (-27%).
* Caveats: NBM is calibrated for representative (airport-like) exposure, and
  most of these stations are CWOP / PG&E mesonet sites; it is a point
  comparison NBM was not designed for, which is also exactly our use. One run
  a day, not every hour.

## HRRR cells kept apart (2026-10-11): tried, abandoned

Hypothesis: silver blends the 4 nearest HRRR cells by distance alone, so a
coastal point gets sea and land mixed into a value that may be true nowhere,
and the model can't tell which cell resembled it. HRRR calls the nearest cell
water at KSFO, FTPC1, 604PG and GGBC1. Giving the model each cell (its value
minus the blend for t2m, gust and wind, HRRR terrain height, land mask,
distance, real ground above the cell) and a land-only blend should help, most
of all where there is no station of its own.

Test: `silver.nwp_cells` and `transform.cells`, models
`gbm_nwp_terrain_cells` / `gbm_terrain_cells`, leave-stations-out with the
same gold snapshot and groups as 2026-10-07. CRPS skill vs raw HRRR:

| | temp 1-3 h | temp 4-6 h | gust 1-3 h | gust 4-6 h |
|---|---|---|---|---|
| gbm_nwp_terrain (no own obs) | +8.2% | +6.1% | +22.3% | +24.1% |
| + cells | +9.0% | +6.3% | +22.4% | +24.3% |
| gbm_terrain (own obs) | +41.3% | +25.6% | +51.2% | +46.4% |
| + cells | +40.9% | +25.7% | +50.9% | +46.2% |

Result: no gain. Every difference is inside the bootstrap CIs. Per station
(1-3 h, no own obs) the change is noise both ways, median 0, most within
+-6-10%; the water-cell stations show no pattern (KSFO temp +8%, 604PG gust
-9%, FTPC1 ~0). With own obs, median -0.3%. Most likely the terrain features
already recover what the blend loses (`elev_minus_hrrr_m`, distance to
water, fetch), and anything left is too weak to learn from ~30 stations.

Abandoned: code and table removed. The experiment is commit be498f1
(`git show be498f1`, or `git checkout be498f1 -- src/microcast/transform/cells.py`
and the model/pipeline/schema hunks) if it's worth another look.

Don't redo: a terrain-weighted blend (by land mask or cell height) on this
evidence; it uses the same information the cells test gave the model.

Worth revisiting only if:
* the truth network gets much denser along the coast and hills (more
  stations is what would let a weak cell signal be learnt), or
* a point lies outside the terrain the stations cover (Twin Peaks summit, a
  hilltop route). Trees can't extrapolate `elev_minus_hrrr_m`; a physical
  lapse-rate adjustment before the model would. The holdout can't test this
  (no station out there), so it needs a station or a careful argument first.

Where the no-own-obs gap is instead: obs. Own obs take temperature 1-3 h
from +8% to +41%; nothing done to the HRRR input has moved it more than a
point. Directions, most promising first:
1. Own obs at the places that matter: the backyard WS-2000 for the house
   (Next #8); for the ride, any station along JFK and the Panhandle.
2. Neighbour obs, temperature only, with a calibrated sigma (Next #2).
3. Regime features: marine layer depth and inversion strength (HRRR 850 mb
   vs surface temperature, low cloud, onshore wind), so errors are learnt
   per regime rather than per place.
4. PurpleAir temperature as changes along the transect (they carry the fog
   edge, not levels).

## Neighbour obs (2026-10-07)

`transform/neighbours.py` adds, for each point, the 3 nearest other truth
stations' HRRR error at init and 14-day bias (same cycle and lead, so as-of),
their distances, and inverse-distance means over the 5 nearest that report
(`nbw_*`, ~99% filled). Never the point's own station; the closest pair is
604PG and D5422, 230 m apart. Joined in `pipeline.load_gold` (need
`"neighbours"`). Models: `gbm_nwp_neighbours` (no own obs) and
`gbm_terrain_neighbours` (with). A neighbour's error at init correlates ~0.4
with the station's own.

Leave-stations-out (`backtest --holdout`, gold snapshot
`7225361806880371065`, same 6 groups), skill vs raw HRRR, all held-out
stations pooled [95% CI]:

| Model | Temp 1–3 h | Temp 4–6 h | Gust 1–3 h | Gust 4–6 h |
| --- | --- | --- | --- | --- |
| `gbm_nwp_terrain` | +8.2% [+6.9, +9.4] | +6.1% | **+22.3%** [+21.0, +23.6] | **+24.1%** |
| `gbm_nwp_neighbours` | **+11.7%** [+10.3, +13.0] | **+7.8%** | +20.5% [+19.1, +22.0] | +22.3% |
| `gbm_terrain` (own obs) | +41.3% | +25.6% | +51.2% | +46.4% |
| `gbm_terrain_neighbours` | +41.2% | +25.8% | +51.4% | +46.8% |

- **Neighbours help temperature at a point with no station**, by 3.5 points
  at 1–3 h (better at 23 of 31 stations). Most for the city-side stations:
  SFOC1 (the house's stand-in) +22.7% vs +15.9%, F4637 +28.5% vs +17.3%,
  D5422 +18.5% vs +5.6%. Still a quarter of what a point's own obs give.
- **They cost gust 2 points** (better at 14 of 32). A neighbour's gust error
  is its own sensor's shelter, not the wind at the point.
- **With the point's own obs they add nothing** (within 0.4 points).
- **The bridge can't borrow:** GGBC1 held out scores −45% / −41% on gust
  with either model. Trained on sheltered stations where HRRR's gust runs
  high, they cut the bridge's gust, where HRRR is right. With its own obs
  (rolling folds) it is +31% / +23%.
- Calibration is unchanged: the no-obs models' 80% intervals cover 70–74%.

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

## PurpleAir transect: ingest removed (2026-10-16)

Decision: stop pulling the ten public transect sensors. They bought a 1.2–1.7%
temperature CRPS gain and nothing for gust ("PurpleAir with vs without",
below), for ~237k API points (~$24) of history. Keeping them live would cost
~1.2–1.5k points a day for hourly polling (D10), plus a sensor list to look
after as sensors go offline. Not worth it. The own sensor stays:
`microcast ingest purpleair` (the LAN reading, free) and `--history-days`
(its API history) are unchanged.

What went:

- `microcast backfill purpleair` (the paid `/history` pull with a points
  floor, and `--load` from the response cache into bronze).
- `backfill_network`, `load_cached`, `windows`, `cache_path` and
  `remaining_points` in `ingest/purpleair.py`, and their tests.
- Hourly live transect polling, which was planned (D10) but never built.

What stays, frozen at 2026-10-05:

- The transect's rows in `bronze.obs` (`source = purpleair`, `pa_<index>`)
  and its positions in `bronze.stations`; bronze is append-only.
- The ten `purpleair_network` sensors in `config/places.yaml`, which
  `build gold` still needs for the west/east split.
- `gold.network_features` (`transform/network.py`), rebuilt by `build gold`
  from that history, and `gbm_purpleair` with its backtest results.

So `gbm_purpleair` can't run live: for cycles after 2026-10-05 its `pa_*`
features are missing. A backtest that covers those months gets missing
values there (LightGBM accepts them), so any skill the transect shows will
shrink as new months come in. The 2026-10-06 numbers below are the ones to
quote.

To restore the ingest: the code is at commit e28a5e2 (`git show
e28a5e2:src/microcast/cli.py`, the `backfill_purpleair` command, and `git
show e28a5e2:src/microcast/ingest/purpleair.py`). The response cache under
`data/purpleair/history/` is still on disk, so `--load` needs no API calls.
Revisit only if the transect gets cheaper, for example free sensors of our
own along it, or if a leaner feature set (the two summaries only) does
noticeably better.

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
