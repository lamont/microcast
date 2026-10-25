# Dagster on Kubernetes · spec

*2026-10-19. Implements D12 (Dagster runs the collectors) on top of D1 (Iceberg,
PyIceberg writes) and D11 (moving the lake). Replaces phase-1 Next #6.*

This is the generic half: what microcast ships so that any Kubernetes cluster
running Dagster OSS, Postgres and an S3-compatible object store can run it.
Cluster-specific choices (which node, which secrets store, hostnames, sizes of
volumes) belong to whoever deploys it and are not in this repo.

## Done means

1. `uv run dagster dev -m microcast.orchestrate` on a laptop shows every
   ingest, build, backtest and site step as an asset, with schedules and
   per-table pools, against the local SQLite catalog.
2. The same image, as a Dagster code location on a cluster, materializes those
   assets against a Postgres SQL catalog and an S3 warehouse. Hourly ingests
   run by schedule; a month of HRRR backfill is one Dagster backfill.
3. The laptop's bronze is in the cluster lake (`microcast lake copy`), silver
   and gold were rebuilt there, and a backtest there agrees with the laptop's
   leaderboard within noise.
4. Nothing in this repo names a node, hostname or secret value.

## Runtime contract

What a pod running the image needs. Every item is an environment variable or a
mount; none is baked into the image.

| What | How | Notes |
|---|---|---|
| Iceberg catalog | `PYICEBERG_CATALOG__MICROCAST__TYPE=sql`, `__URI=postgresql+psycopg2://…`, `__WAREHOUSE=s3://<bucket>/`, `__S3__ENDPOINT`, `__S3__REGION`, `__S3__PATH_STYLE_ACCESS`, `__S3__ACCESS_KEY_ID`, `__S3__SECRET_ACCESS_KEY` | Already honoured by `lake.catalog.get_catalog` (D1). The catalog stores `s3://` paths, not the endpoint, so a laptop (public endpoint) and pods (in-cluster endpoint) can share one catalog. |
| Data directory | a volume at `/data` (`MICROCAST_DATA_DIR`, the image default) | Persists caches and reports between runs; see below. |
| MLflow | `MLFLOW_TRACKING_URI=http://…` | Already honoured by `pipeline._mlflow`. Unset falls back to `/data/mlflow.db`. |
| Private registry | `MICROCAST_PLACES=/config/places.local.yaml`, mounted from a Secret | Home coordinates and private places never go in the image. |
| Location and keys | `MICROCAST_HOME_LAT/LON`, `MICROCAST_PURPLEAIR_SENSOR_INDEX`, `MICROCAST_PURPLEAIR_HOST`, `PURPLEAIR_API_KEY` | As in `.env.example`. |
| Placement and size | `MICROCAST_K8S_CONFIG_HEAVY`, `MICROCAST_K8S_CONFIG_LIGHT` (JSON) | Optional; see "Run size". |

### What lives in `/data`

With the catalog on Postgres + S3, `/data` holds only things that are cheaper to
keep than to rebuild, plus outputs that later steps read:

| Path | Size today | Why it persists |
|---|---|---|
| `hrrr/window/` | 3.3 GB | HRRR window tiles from the archive; upwind and lagged-ensemble features reread up to 6 earlier cycles. Refetchable but slow. |
| `grib/` | 0.7 GB | Herbie downloads and its BallTree cache. |
| `terrain/` | 89 MB | DEM for `build static`. |
| `reports/` | 2.7 GB | Backtest output; `microcast site` reads it. |
| `site/` | small | The built static site, for whatever serves it. |

Cache files are written whole and renamed (`hrrr_zarr`, `dem`), so two runs
sharing the volume can't see a torn file. **All run pods must mount the same
volume.** With a node-local (ReadWriteOnce) volume, that means every microcast
run lands on one node; that is the deployer's call.

## Dependencies

- PyIceberg gains the `sql-postgres` extra (psycopg2).
- A new `dagster` extra: `dagster`, `dagster-k8s`, `dagster-postgres`,
  `dagster-aws`. The image already installs every extra.
- **The `dagster` version must equal the cluster's Dagster version** (the Helm
  chart's). It is pinned exactly in `pyproject.toml`; a chart bump needs a
  matching microcast release. `dagster-aws` is there because a cluster using
  S3 compute logs needs it in every code location.

## Definitions: `microcast.orchestrate`

Plain Python `Definitions` (D12's `defs.yaml` components can come later, once
the assets settle). The package is `orchestrate`, not `dagster`, to keep imports
unambiguous. Asset bodies call the same functions the CLI calls; the CLI stays
the way to run anything without Dagster.

| Asset | Body (today's CLI) | Partitions | Trigger | Pool | Size |
|---|---|---|---|---|---|
| `hrrr_point` | `backfill hrrr` | hourly by cycle | schedule, hourly, partition ~3 h back (archive lag) | `bronze.nwp_point` | light |
| `rrfs_point` | `backfill rrfs` | hourly by cycle | schedule, hourly, partition ~2 h back | `bronze.nwp_point` | light |
| `madis_obs` | `backfill madis` | hourly | schedule, hourly | `bronze.obs` | light |
| `madis_live` | `ingest madis` | none (last 2 real-time files) | schedule, hourly, before init + 55 min | `bronze.obs` | light |
| `purpleair_obs` | `ingest purpleair` | none | schedule, hourly | `bronze.obs` | light |
| `silver` | `build silver` | daily | eager, after bronze | `silver` | light |
| `gold` | `build gold` | none | daily, after silver | `gold` | **heavy** |
| `static` | `build static` | none | manual | `gold` | light |
| `backtest` | `backtest` | none | daily, after gold | `gold` | **heavy** |
| `site` | `site --out /data/site` | none | after backtest | none | light |

- **Backfills run as one run per range** (`BackfillPolicy.single_run()`), so a
  month of HRRR is one pod and, as in the CLI, one Iceberg append per batch
  group rather than per hour.
- **Pools are per table** (Dagster's `pool=` on each asset). The deployment
  must give pools a default limit of 1: that is what makes "one writer per
  table" (D12) true without anyone checking `ps`. Bronze tables have several
  writers (HRRR, RRFS and NBM all append to `bronze.nwp_point`), so the pool is
  named for the table, not the source.
- **Failures fail the run.** Asset bodies raise; a CLI command that today prints
  and returns on an empty fetch keeps doing that (a missing cycle is not a
  failure until the archive should have it).
- NBM, holdout and ablation runs stay CLI-only for now. Live inference (phase-1
  Next #4) becomes an asset when it is built.

### Run size

Measured on the laptop, 2026-10-19 (peak RSS):

| Step | Peak |
|---|---|
| `build gold`, in memory | 11.7 GB |
| `backtest`, loading gold for six models (before any fit) | 5.4 GB |
| ingests | well under 1 GB |

Gold grows with history, so expect these to climb. Jobs carry a tag
`microcast/size: light|heavy`. If `MICROCAST_K8S_CONFIG_<SIZE>` is set, its JSON
becomes the job's `dagster-k8s/config` tag (resources, node selector,
`merge_behavior`); if not, there is no tag and the instance default applies.
Locally neither is set. This keeps node names and memory limits in the
deployer's config.

## In this repo, under `deploy/`

- `deploy/k8s/madis-job.yaml`: one plain Kubernetes Job running
  `microcast ingest madis`, with placeholder Secret and PVC names. It proves
  the image, secrets, catalog and volume before Dagster is involved.
- `deploy/dagster/values-example.yaml`: a `dagster-user-deployments` entry for
  the code location (`dagsterApiGrpcArgs: ["-m", "microcast.orchestrate"]`,
  `includeConfigInLaunchedRuns`, `envSecrets`, the `/data` volume), and the
  instance settings microcast relies on (pool default limit 1).
- `deploy/dagster/dagster.yaml`: the same pool setting for `dagster dev`.

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
5. Stop the laptop catch-up script; Dagster owns the schedules from here.

The laptop lake keeps its 20 GB on disk but only ~5.4 GB is live; the rest is
unexpired snapshots. The cluster lake starts clean. Expiring old snapshots
there is a follow-up (a weekly maintenance asset).

MLflow history does not move: snapshot ids differ, so the cluster's first
backtest re-pins the runs (D11).

## Order of work

1. Dependencies (`sql-postgres`, the `dagster` extra), image rebuilt by CI.
2. `deploy/k8s/madis-job.yaml` run once on the cluster against the empty
   cluster catalog.
3. `lake copy`, then silver/gold/backtest by CLI in a one-off pod, to check
   memory before Dagster schedules anything.
4. `microcast.orchestrate` with `dagster dev` on the laptop, local catalog.
5. Code location on the cluster; schedules on; the laptop catch-up stops.
6. Docs: D12 consequences, `design.md` "Deployment", the README's working vs
   planned, phase-1 Next #6.

## Open

- Laptop experiments after the move read a remote lake. Gold is ~0.6 GB of
  Parquet, fine to pull; silver is not. Ablations may be better run as ad hoc
  Dagster jobs (or `kubectl exec` in a run pod) than from the laptop.
- `build gold` holds all of silver in memory. If it outgrows the node, build it
  by month.
