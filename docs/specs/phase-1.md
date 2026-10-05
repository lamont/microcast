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

## Next

1. **Obs ingest, historical:** KSFO + nearby ASOS/CWOP via SynopticPy (or
   Iowa Environmental Mesonet ASOS CSV, which needs no key) → `bronze.obs`.
   Pick 3–5 stations near the home and route points.
2. **HRRR backfill:** `microcast backfill hrrr --start 2025-04-01 --end
   2026-10-01 --leads 0-6`. That's ~13k cycles × 7 leads, so run it with a
   thread pool and resume support; skip batches whose `ingest_batch` already
   exists in bronze.
3. **Silver:** `obs_qc` (SI units, range checks) and `nwp_aligned` (k=0..3
   pivoted wide, distance-weighted mean, wind speed/direction from u/v), each
   rebuilt per day partition.
4. **Gold:** `training_examples` with as-of features (`available_at = init + 55
   min` for HRRR history), target = station obs at valid time.
5. **Models 0, 1, 3:** `raw_hrrr`, `bias_rolling`, `gbm_residual` behind the
   `Forecaster` protocol; local MLflow (`mlruns/`) with snapshot tags.
6. **Notebook:** leaderboard by lead bucket, plus a first look at `lcc` vs
   GOES low cloud for D2.

## Not in phase 1

Dagster, Docker Compose, MinIO/Lakekeeper, RRFS/REFS ingest, alerts.
