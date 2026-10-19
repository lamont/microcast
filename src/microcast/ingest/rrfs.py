"""RRFS (NOAA's successor to HRRR) at our points, collected now so history accrues.

RRFS v1 runs hourly on HRRR's own 3 km grid (same Lambert projection, winds
grid-relative), so its rows land in bronze.nwp_point with HRRR's variable and
level names and silver aligns them unchanged (``model = 'rrfs'``). It is not a
model input yet: see "RRFS: collecting, not using" in docs/specs/phase-1.md for
when it gets compared.

Kept small on purpose. The hourly cycles on AWS publish only the sub-hourly
``2dfld...subh`` file (~140 MB a lead); its ``.idx`` lets Herbie byte-range just
the top-of-hour message of each field we use (``SEARCH``, ~14 MB a lead, read
and deleted). Leads 1-6 (subh has no f00), the 4 nearest cells at each point:
~13k rows, well under 1 MB of bronze, per cycle.

Source: ``noaa-rrfs-ops-pds``. The pre-operational feed has been there since
2026-08-13; operations start 2026-11-03 (SCN 26-48) under the same names.
"""

from __future__ import annotations

import shutil
import tempfile
import warnings
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa

from microcast import settings
from microcast.ingest import nwp

MODEL = "rrfs"
PRODUCT = "subh"
LEADS = list(range(1, 7))
# The top-of-hour step only ("N hour fcst"; the 15-min steps say "min fcst").
SEARCH = (
    r":(?:TMP|DPT):2 m above ground:\d+ hour fcst:"
    r"|:(?:UGRD|VGRD):10 m above ground:\d+ hour fcst:"
    r"|:(?:GUST|VIS|DSWRF):surface:\d+ hour fcst:"
    r"|:HGT:cloud ceiling:\d+ hour fcst:"
)


def batch_id(init: datetime) -> str:
    return f"{MODEL}/{init:%Y-%m-%dT%H}"


def fetch_cycle(init: datetime, points: pd.DataFrame, leads: list[int] = LEADS) -> pa.Table:
    """One RRFS cycle's leads at ``points`` (point_id, lat, lon), as bronze.nwp_point rows."""
    import herbie
    from herbie import Herbie

    # pick_points caches its BallTree under Herbie's save_dir: keep that in our data dir, and
    # the GRIB subsets in a scratch dir removed afterwards.
    herbie.config["default"]["save_dir"] = settings.data_dir() / "grib"
    pts = points.rename(columns={"point_id": "id", "lat": "latitude", "lon": "longitude"})
    scratch = Path(tempfile.mkdtemp(prefix="rrfs-"))
    try:
        tables = []
        for fxx in leads:
            H = Herbie(
                init.replace(tzinfo=None),
                model=MODEL,
                product=PRODUCT,
                fxx=fxx,
                save_dir=scratch,
                priority=["aws"],
                verbose=False,
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)
                ds = H.xarray(SEARCH, remove_grib=True)
            ds = ds if isinstance(ds, list) else [ds]
            picked = [
                d.herbie.pick_points(pts, method="nearest", k=nwp.K_NEIGHBOURS, tree_name="rrfs_conus") for d in ds
            ]
            tables.append(nwp.to_long(picked, model=MODEL, product=PRODUCT, ingest_batch=batch_id(init)))
        return pa.concat_tables(tables)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def backfill(
    points: pd.DataFrame,
    start: datetime,
    end: datetime,
    *,
    leads: list[int] = LEADS,
    stride_h: int = 1,
    workers: int = 4,
    commit_every: int = 24,
    log=print,
) -> dict[str, int]:
    """Every missing RRFS cycle in [start, end) into bronze.nwp_point; resumable like the HRRR backfill.

    Also the live path: run it over the last few days and it fills whatever
    the bucket has that bronze doesn't. Fetches run in worker processes
    (cfgrib decodes a CONUS grid per message); appends happen here, one writer.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed

    from microcast.ingest.hrrr_zarr import cycles
    from microcast.lake.catalog import append_bronze, recorded_batches

    done = recorded_batches("bronze.nwp_point")
    every = cycles(start, end, stride_h)
    todo = [c for c in every if batch_id(c) not in done]
    stats = {"skipped": len(every) - len(todo), "fetched": 0, "failed": 0, "rows": 0}
    log(f"{len(todo)} RRFS cycles to fetch, {stats['skipped']} already in bronze")
    pending: list[pa.Table] = []
    ids: list[str] = []

    def flush() -> None:
        if pending:
            table = pa.concat_tables(pending)
            append_bronze("bronze.nwp_point", table, batches=ids)
            stats["rows"] += table.num_rows
            log(f"  committed {len(ids)} cycles ({table.num_rows} rows), last {ids[-1]}")
            pending.clear()
            ids.clear()

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_cycle, c, points, leads): c for c in todo}
        for fut in as_completed(futures):
            init = futures[fut]
            try:
                pending.append(fut.result())
                ids.append(batch_id(init))
                stats["fetched"] += 1
            except Exception as e:  # not yet published, or a gap: log it, a rerun retries
                stats["failed"] += 1
                log(f"  failed {init:%Y-%m-%dT%HZ}: {type(e).__name__}: {e}")
            if len(pending) >= commit_every:
                flush()
    flush()
    return stats
