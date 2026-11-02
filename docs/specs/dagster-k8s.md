# Dagster on Kubernetes · spec

*2026-10-19, revised 2026-11-01. Implements D12 (Dagster runs the collectors)
on top of D1 (Iceberg, PyIceberg writes) and D11 (moving the lake). Replaces
phase-1 Next #6. The 11-01 revision adds the run window, one hourly chain,
gap-fill, per-table pools with a `heavy` pool, the live forecast and site
publish assets, and memory cuts before anything runs in the cluster.*

This is the generic half: what microcast ships so that any Kubernetes cluster
running Dagster OSS, Postgres and an S3-compatible object store can run it.
Cluster-specific choices (which node, which secrets store, hostnames, sizes of
volumes) belong to whoever deploys it and are not in this repo.

## Done means

1. `uv run dagster dev -m microcast.orchestrate` on a laptop shows every
   ingest, build, backtest, forecast and site step as an asset, with the one
   hourly schedule and per-table pools, against the local SQLite catalog.
2. The same image, as a Dagster code location on a cluster, materializes those
   assets against a Postgres SQL catalog and an S3 warehouse. Nothing runs
   outside the run window; the first tick of the day fills the night's gaps;
   a month of HRRR backfill is one Dagster backfill.
3. The laptop's bronze is in the cluster lake (`microcast lake copy`), silver
   and gold were rebuilt there, and a backtest there agrees with the laptop's
   leaderboard within noise.
4. Gold build and backtest each peak under 8 GB, measured in-cluster (see
   "Memory").
5. Nothing in this repo names a node, hostname or secret value.

## Runtime contract

What a pod running the image needs. Every item is an environment variable or a
mount; none is baked into the image.

| What | How | Notes |
|---|---|---|
| Iceberg catalog | `PYICEBERG_CATALOG__MICROCAST__TYPE=sql`, `__URI=postgresql+psycopg2://…`, `__WAREHOUSE=s3://<bucket>/`, `__S3__ENDPOINT`, `__S3__REGION`, `__S3__PATH_STYLE_ACCESS`, `__S3__ACCESS_KEY_ID`, `__S3__SECRET_ACCESS_KEY` | Already honoured by `lake.catalog.get_catalog` (D1). The catalog stores `s3://` paths, not the endpoint, so a laptop (public endpoint) and pods (in-cluster endpoint) can share one catalog. |
| Data directory | a volume at `/data` (`MICROCAST_DATA_DIR`, the image default) | Persists caches, models and reports between runs; see below. |
| MLflow | `MLFLOW_TRACKING_URI=http://…` | Already honoured by `pipeline._mlflow`. Unset falls back to `/data/mlflow.db`. |
| Private registry | `MICROCAST_PLACES=/config/places.local.yaml`, mounted from a Secret | Home coordinates and private places never go in the image. |
| Location and keys | `MICROCAST_HOME_LAT/LON`, `MICROCAST_PURPLEAIR_SENSOR_INDEX`, `MICROCAST_PURPLEAIR_HOST`, `PURPLEAIR_API_KEY` | As in `.env.example`. `MICROCAST_PURPLEAIR_HOST` is whatever answers `/json` for the house sensor and is reachable from the run pods (see "PurpleAir"). |
| Site publish | `MICROCAST_PUBLISH_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` | `site_publish` only (`static-hosting.md`). A key that can write that one bucket and nothing else. |
| Placement and size | `MICROCAST_K8S_CONFIG_HEAVY`, `MICROCAST_K8S_CONFIG_LIGHT` (JSON) | Optional; see "Run size". |

### What lives in `/data`

With the catalog on Postgres + S3, `/data` holds only things that are cheaper to
keep than to rebuild, plus outputs that later steps read:

| Path | Size today | Why it persists |
|---|---|---|
| `hrrr/window/` | 3.3 GB | HRRR window tiles from the archive; upwind and lagged-ensemble features reread up to 6 earlier cycles. Refetchable but slow. |
| `grib/` | 0.7 GB | Herbie downloads and its BallTree cache. |
| `terrain/` | 89 MB | DEM for `build static`. |
| `models/` | small | The forecast's daily fit (`forecast_model`), keyed by the gold snapshot it was fitted on. |
| `reports/` | 2.7 GB | Backtest output; `microcast site` reads it. |
| `site/` | small | The built static site, which `site_publish` uploads. |
| `tmp/` | transient | DuckDB spill for the gold build. |

Cache files are written whole and renamed (`hrrr_zarr`, `dem`), so two runs
sharing the volume can't see a torn file. Models are written the same way.
**All run pods must mount the same volume.** With a node-local
(ReadWriteOnce) volume, that means every microcast run lands on one node; that
is the deployer's call.

## Dependencies

- PyIceberg gains the `sql-postgres` extra (psycopg2).
- A new `dagster` extra: `dagster`, `dagster-k8s`, `dagster-postgres`,
  `dagster-aws`, and `boto3` named explicitly for `microcast publish`. The image
  already installs every extra.
- **The `dagster` version must equal the cluster's Dagster version** (the Helm
  chart's). It is pinned exactly in `pyproject.toml`; a chart bump needs a
  matching microcast release. `dagster-aws` is there because a cluster using
  S3 compute logs needs it in every code location.

## Definitions: `microcast.orchestrate`

Plain Python `Definitions` (D12's `defs.yaml` components can come later, once
the assets settle). The package is `orchestrate`, not `dagster`, to keep imports
unambiguous. Asset bodies call the same functions the CLI calls; the CLI stays
the way to run anything without Dagster.

### Run window

Nothing runs at night. Nobody reads the page then, and 1–6 h forecasts made at
3 AM are stale by breakfast. One constant in `microcast.orchestrate` holds the
window: **08:00–21:00 America/Los_Angeles**, the same hours `static-hosting.md`
settled for publishing. Ingests and backfills are inside it too, not just the
site: the night's cycles stay in the archives (hrrrzarr, RRFS on S3, MADIS),
and the first tick of the day fetches them as one ranged run per source.

The only exception is a manual backfill launched from the UI.

### Assets

| Asset | Body (today's CLI) | Partitions | Runs in | Pool | Size |
|---|---|---|---|---|---|
| `hrrr_live` | `ingest nwp` (newest cycle, leads 0–6) | none | hourly chain | `bronze.nwp_point` | light |
| `madis_live` | `ingest madis` | none (last 2 real-time files) | hourly chain | `bronze.obs` | light |
| `purpleair_obs` | `ingest purpleair` | none | hourly chain | `bronze.obs` | light |
| `forecast` | `forecast` (`live-forecast.md`), predict only | none | hourly chain | `gold.live_forecasts` | light |
| `site` | `site --out /data/site` | none | hourly chain, and after `backtest` | none | light |
| `site_publish` | `publish --from /data/site` (`static-hosting.md`) | none | after every `site` | none | light |
| `hrrr_point` | `backfill hrrr` | hourly by cycle | gap-fill | `bronze.nwp_point` | light |
| `rrfs_point` | `backfill rrfs` | hourly by cycle | gap-fill | `bronze.nwp_point` | light |
| `madis_obs` | `backfill madis` | hourly | gap-fill | `bronze.obs` | light |
| `silver` | `build silver` | daily | daily chain | `silver` | light |
| `gold` | `build gold` | none | daily chain | `heavy` | **heavy** |
| `forecast_model` | fit `gbm_terrain_stack` for gust and t2m on gold, save to `/data/models` | none | daily chain | `heavy` | **heavy** |
| `backtest` | `backtest` | none | daily chain | `heavy` | **heavy** |
| `static` | `build static` | none | manual | `silver.static_features` | light |

- **Backfills run as one run per range** (`BackfillPolicy.single_run()`), so a
  month of HRRR is one pod and, as in the CLI, one Iceberg append per batch
  group rather than per hour. Bronze bodies already skip batches the table has
  recorded (`recorded_batches`), so a range that overlaps loaded hours costs
  only the check.
- **Failures fail the run.** Asset bodies raise. A partitioned ingest whose
  source has nothing yet *does not materialize* (`output_required=False`), so
  gap-fill asks for it again next hour; once it is older than the source's lag
  plus a margin (HRRR archive 3 h, RRFS 2 h, MADIS archive 2 days, each + 6 h),
  it raises, because the archive should have it by then.
- NBM, holdout and ablation runs stay CLI-only for now.

### One schedule: the hourly chain

The only clock is one schedule, `hourly`, at minute :45 of every hour in the
run window (08:45 … 20:45, 13 ticks). Everything else follows from it. :45 is a
starting point: it is the first minute that HRRR f06 of the cycle two hours
back is reliably out (+86 to +97 min, measured 2026-10-17), and GGBC1's
real-time MADIS row (~75–80 min late) for the previous hour is in. Retune it
from the run history once it has a week.

Each tick yields up to three kinds of run request:

1. **The hourly chain, one run:** `hrrr_live`, `madis_live`, `purpleair_obs`
   → `forecast` → `site` → `site_publish`. One run means one pod and the order
   comes from the asset graph, not from separate schedules racing each other.
   It is submitted first, so the pools serve it ahead of the gap-fill runs.
2. **Gap-fill, one ranged run per partitioned ingest** (`hrrr_point`,
   `rrfs_point`, `madis_obs`): the schedule asks the instance which partitions
   in the last 7 days, older than the source's lag, are not materialized, and
   requests the range from the oldest to the newest of them
   (`dagster/asset_partition_range_start|end` tags). No request if nothing is
   missing, or if a run for that asset is already queued or in progress. On
   the 08:45 tick this is the night's ~11 cycles; through the day it is
   usually one partition, or none.
3. **The daily chain, at the 09:45 tick only:** `silver` (yesterday's and
   today's partitions) → `gold` → `forecast_model` and `backtest` → `site` →
   `site_publish`. 09:45 gives the night's gap-fill an hour to land. The site
   republishes after the backtest, so the leaderboard and the forecast panel
   both reflect the new gold by mid-morning.

Until `forecast_model` has run once, `forecast` fails with a clear message
rather than fitting on its own; the daily chain can be launched by hand to
start it.

**Why a schedule and not declarative automation.** `AutomationCondition.on_missing()`
looks only at the latest time partition by default, and the automation sensor
evaluates every 30 s around the clock, which means a night-time guard and a
steady trickle of ticks in run storage. One schedule tick an hour that does
one partition-status query costs less, keeps the window in one place, and
makes "what runs when" readable in a single function.

**Retries.** The instance enables run retries (`run_retries`, max 2) for
transient failures: a slow archive, a dropped connection. Heavy runs set
`dagster/max_retries: 0`: an OOM retried is another 8 GB for nothing, and
tomorrow's daily chain picks it up. Anything a retry doesn't fix, gap-fill
asks for again next hour. Together these replace the laptop's catch-up
scripts.

### Pools: one per written table

A pool guards a table's writers; readers never take one. Snapshot isolation
means a backtest reading gold doesn't block, and isn't blocked by, anything.
The deployment gives pools a default limit of 1, which is what makes "one
writer per table" (D12) true without anyone checking `ps`.

- Pools are named for the table, not the source. HRRR, RRFS and NBM all
  append to `bronze.nwp_point`, so they share its pool.
- **`heavy` is the one pool that isn't a table.** Dagster gives an asset one
  pool, so the heavy assets (`gold`, `forecast_model`, `backtest`) take `heavy`
  (limit 1) instead of their table's. That keeps one heavy run in memory at a
  time, and it still keeps one writer per table, because each heavy asset is
  the only writer of what it writes: `gold` of `gold.training_examples`,
  `backtest` of the `kind = backtest` rows of `gold.forecasts` and
  `gold.scores`, and `forecast_model` of nothing in the lake. A light asset
  that starts writing one of those tables needs this revisited.
- The hourly `forecast` takes `gold.live_forecasts`, not anything a heavy run
  holds, so the daily backtest (about an hour) never delays it.

**To check when there is something to run:** whether the pools are enforced
per op (Dagster's default) or per run (`concurrency.pools.granularity: run`).
Per op, a run blocked on `heavy` still launches its pod, which then holds its
memory request while it waits. Per run, it waits in the queue instead, but a
multi-asset run like the hourly chain claims all of its pools before it
starts. Try both with `dagster dev` and on the cluster with a deliberately
long `backtest`, and write down which one the deployment uses and why. Until
then, assume the run queue serializes heavy runs.

### Memory

Gold build peaked at 11.7 GB and the backtest's load at 5.4 GB (laptop,
2026-10-19), and both grow with history. Cut them before the cluster runs
anything, so the run node needs less RAM, not more:

| Step | Today | Change | Target |
|---|---|---|---|
| `build gold` | reads all of `silver.nwp_aligned` (every column) and `silver.obs_qc` into Arrow, filters stations afterwards, joins in DuckDB in memory, rewrites all of gold | Scan only the columns `training_sql` uses, with the station filter in the scan (`In("point_id", …)`). Give DuckDB a `memory_limit` and `temp_directory=/data/tmp` so the join spills. Rebuild only the months whose silver changed, plus the feature lookback (rolling bias, 6 earlier cycles), with `replace_partition` on that window. A full rebuild stays a flag. | under 4 GB, flat with history |
| `backtest` load | `to_pandas()` of all of gold, then wide joins | Load only the columns the requested models use; float32 features; categorical ids; free each model's frame before the next | under 4 GB |
| `forecast` | `live-forecast.md` left fit-per-run open (~5 GB each hour) | **Fit once a day** in `forecast_model`; the hourly run loads the booster and predicts | under 1 GB |

Each change must leave the backtest leaderboard unchanged (same folds, same
rows, scores within float noise), checked against the current laptop run
before the lake moves. Measure again in the one-off cluster pod (order of
work, step 4), and only then set the heavy run's limits.

### Run size

Jobs carry a tag `microcast/size: light|heavy`. If `MICROCAST_K8S_CONFIG_<SIZE>`
is set, its JSON becomes the job's `dagster-k8s/config` tag (resources, node
selector, `merge_behavior`); if not, there is no tag and the instance default
applies. Locally neither is set. This keeps node names and memory limits in
the deployer's config.

### PurpleAir

`purpleair_obs` reads the house sensor's `/json` from `MICROCAST_PURPLEAIR_HOST`.
The sensor is on the home LAN and the cluster's run node may not be, so one of
these comes before it is scheduled:

- a small proxy at the house that the run pods can reach, which forwards
  `/json` (the asset is unchanged; only the host changes); or
- if the house already scrapes the sensor into Prometheus, a backfill from
  Prometheus instead of live polling. That is a new ingest path and gets its
  own few lines in the spec when chosen.

Until one exists, `purpleair_obs` is left out of the hourly chain rather than
failing every hour.

## In this repo, under `deploy/`

- `deploy/k8s/madis-job.yaml`: one plain Kubernetes Job running
  `microcast ingest madis`, with placeholder Secret and PVC names. It proves
  the image, secrets, catalog and volume before Dagster is involved.
- `deploy/dagster/values-example.yaml`: a `dagster-user-deployments` entry for
  the code location (`dagsterApiGrpcArgs: ["-m", "microcast.orchestrate"]`,
  `includeConfigInLaunchedRuns`, `envSecrets`, the `/data` volume), and the
  instance settings microcast relies on: pool default limit 1, run retries.
- `deploy/dagster/dagster.yaml`: the same settings for `dagster dev`.

## Moving the lake (D11)

1. `microcast lake init` against the cluster catalog (env vars as above, with a
   reachable Postgres, e.g. through a port-forward, and the store's external
   endpoint).
2. `microcast lake copy --to <name>` from the laptop: bronze is ~1 GB live
   today (`nwp_point` 917 MB, `obs` 74 MB), copied a month per snapshot,
   resumable.
3. In the cluster: `build silver`, `build static`, `build gold`, `backtest`
   (as Dagster materializations). Compare the leaderboard with the laptop's.
4. Copy `hrrr/window/` and `terrain/` into the cluster's `/data` so the first
   runs don't refetch them.
5. Stop the laptop catch-up script; Dagster owns the schedule from here.

The laptop lake keeps its 20 GB on disk but only ~5.4 GB is live; the rest is
unexpired snapshots. The cluster lake starts clean. Expiring old snapshots
there, and compacting `gold.live_forecasts` monthly, is a follow-up (a weekly
maintenance asset in the daily chain).

MLflow history does not move: snapshot ids differ, so the cluster's first
backtest re-pins the runs (D11).

## Order of work

1. Memory cuts (above) on the laptop, with the leaderboard check.
2. Dependencies (`sql-postgres`, the `dagster` extra), image rebuilt by CI.
3. `deploy/k8s/madis-job.yaml` run once on the cluster against the empty
   cluster catalog.
4. `lake copy`, then silver/gold/backtest by CLI in a one-off pod, to measure
   memory before Dagster schedules anything.
5. `microcast.orchestrate` with `dagster dev` on the laptop, local catalog:
   the schedule's three kinds of request, gap-fill after a simulated night,
   and the pool granularity check.
6. Code location on the cluster; schedule on; the laptop catch-up stops.
7. Docs: D12 consequences, `design.md` "Deployment", the README's working vs
   planned, phase-1 Next #6.

`live-forecast.md` and `static-hosting.md` supply `forecast`, `forecast_model`
and `site_publish`; until they land, the hourly chain is the two live ingests
→ `site`, and the daily chain stops at `backtest` → `site`.

## Open

- Laptop experiments after the move read a remote lake. Gold is ~0.6 GB of
  Parquet, fine to pull; silver is not. Ablations may be better run as ad hoc
  Dagster jobs (or `kubectl exec` in a run pod) than from the laptop.
- If 08:00 turns out to be too late for an early ride, widen the window; the
  constant is the only place it lives.
