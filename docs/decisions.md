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

## Open

- RRFS/REFS operational date (Oct 6 vs Oct 14, 2026) is still unverified. Herbie
  2026.9.2 ships `rrfs` and `refs` templates on `noaa-rrfs-ops-pds`.
- Tempest vs Ecowitt: both report solar radiation, which D2 depends on.
  Tempest's solar sensor and 1-min cadence suit the clear-sky index.
- REFS products available through Herbie: `mean, sprd, pmmn, lpmm, avrg, prob,
  eas, ffri`. Check whether `prob` covers the rain and wind exceedances we need.
