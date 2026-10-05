"""Copy bronze from one catalog to another (laptop lake -> cluster lake).

Iceberg metadata records absolute file locations (``file:///Users/...``), so a
lake can't be moved by copying its directory. Instead bronze is re-appended
row for row, a month at a time, into the target catalog. Every row keeps its
``ingest_batch`` and ``ingested_at``, so the as-of rule (D1) is unchanged in
the copy. Silver and gold are derived: rebuild them in the target.

What does not carry over: snapshot ids and history (MLflow runs tag source
snapshots). Rerun the backtest in the target to re-pin it.

Resumable: each append records its month in the target snapshot summary
(``microcast.copied-months``), and a rerun skips months already copied.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pyarrow.compute as pc
from pyiceberg.catalog import Catalog

from microcast.lake.catalog import BATCHES_PROPERTY, init_lake
from microcast.lake.schemas import BRONZE_NWP_POINT, BRONZE_OBS, TABLES, TableDef
from microcast.transform.silver import _range, _utc

COPIED_PROPERTY = "microcast.copied-months"
BRONZE = [(BRONZE_NWP_POINT, "init_time"), (BRONZE_OBS, "obs_time")]


def _months_with_data(catalog: Catalog, tdef: TableDef, time_col: str) -> list[date]:
    table = catalog.load_table(tdef.identifier)
    if table.current_snapshot() is None:
        return []
    parts = table.inspect.partitions().to_pylist()
    days = {p["partition"][f"{time_col}_day"] for p in parts}
    return sorted({date(d.year, d.month, 1) for d in days if d is not None})


def _copied(catalog: Catalog, identifier: str) -> set[str]:
    out: set[str] = set()
    for snap in catalog.load_table(identifier).snapshots():
        props = snap.summary.additional_properties if snap.summary else {}
        if value := props.get(COPIED_PROPERTY):
            out.update(value.split(","))
    return out


def copy_bronze(source: Catalog, target: Catalog, log: Callable[[str], None] = print) -> dict[str, int]:
    init_lake(target, TABLES)
    rows = {}
    for tdef, time_col in BRONZE:
        src, dst = source.load_table(tdef.identifier), target.load_table(tdef.identifier)
        done = _copied(target, tdef.identifier)
        rows[tdef.identifier] = 0
        for first in _months_with_data(source, tdef, time_col):
            key = f"{first:%Y-%m}"
            if key in done:
                continue
            nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
            data = src.scan(row_filter=_range(time_col, _utc(first), _utc(nxt))).to_arrow()
            batches = sorted(set(pc.unique(data["ingest_batch"]).to_pylist()))
            dst.append(data, snapshot_properties={COPIED_PROPERTY: key, BATCHES_PROPERTY: ",".join(batches)})
            rows[tdef.identifier] += data.num_rows
            log(f"  {tdef.identifier} {key}: {data.num_rows} rows")
    return rows
