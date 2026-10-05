"""Catalog access and the only write paths into the lake.

Locally the catalog is PyIceberg's SQL catalog on SQLite with a ``file://``
warehouse. Moving to Lakekeeper (REST) and S3/MinIO is configuration only: set
``PYICEBERG_CATALOG__MICROCAST__*`` variables (or ``~/.pyiceberg.yaml``) and
those win over the local defaults.

Write rules (docs/decisions.md, D1):

* bronze is append-only: ``append_bronze`` is the only way in, and every call
  adds new data files under a new snapshot. Rows are never rewritten.
* silver and gold are derived, so ``replace_partition`` rebuilds a partition
  wholesale, which is also how small files get consolidated.
"""

from __future__ import annotations

import warnings
from functools import cache

import duckdb
import pyarrow as pa
from pyiceberg.catalog import Catalog, load_catalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError
from pyiceberg.expressions import BooleanExpression
from pyiceberg.table import Table
from pyiceberg.utils.config import Config

from microcast import settings
from microcast.lake.schemas import TABLES, TableDef

CATALOG_NAME = "microcast"
NAMESPACES = ("bronze", "silver", "gold")
TABLE_PROPERTIES = {"write.parquet.compression-codec": "zstd", "format-version": "2"}


def local_catalog_properties() -> dict[str, str]:
    root = settings.data_dir()
    (root / "warehouse").mkdir(exist_ok=True)
    return {
        "type": "sql",
        "uri": f"sqlite:///{root / 'catalog.db'}",
        "warehouse": (root / "warehouse").as_uri(),
    }


@cache
def get_catalog() -> Catalog:
    if Config().get_catalog_config(CATALOG_NAME):
        return load_catalog(CATALOG_NAME)
    return load_catalog(CATALOG_NAME, **local_catalog_properties())


def init_lake(catalog: Catalog | None = None, tables: list[TableDef] = TABLES) -> list[str]:
    """Create namespaces and any missing tables. Safe to rerun."""
    catalog = catalog or get_catalog()
    for ns in NAMESPACES:
        try:
            catalog.create_namespace(ns)
        except NamespaceAlreadyExistsError:
            pass
    created = []
    for t in tables:
        try:
            catalog.load_table(t.identifier)
        except NoSuchTableError:
            catalog.create_table(t.identifier, schema=t.schema, partition_spec=t.spec, properties=TABLE_PROPERTIES)
            created.append(t.identifier)
    return created


def _conform(table: Table, data: pa.Table) -> pa.Table:
    """Cast to the table's Arrow schema so nullability and units match exactly."""
    return data.select(table.schema().column_names).cast(table.schema().as_arrow())


BATCHES_PROPERTY = "microcast.ingest-batches"


def append_bronze(
    identifier: str, data: pa.Table, catalog: Catalog | None = None, batches: list[str] | None = None
) -> int | None:
    """Append rows to a bronze table; returns the new snapshot id.

    ``batches`` (the ``ingest_batch`` ids in this append) is recorded in the
    snapshot summary so a backfill can resume without scanning the table. A
    backfill groups many batches into one append to keep snapshot count and
    metadata size down; each row still carries its own ``ingest_batch``.
    """
    if not identifier.startswith("bronze."):
        raise ValueError(f"append_bronze only writes bronze tables, got {identifier}")
    if data.num_rows == 0:
        return None
    table = (catalog or get_catalog()).load_table(identifier)
    props = {BATCHES_PROPERTY: ",".join(batches)} if batches else {}
    table.append(_conform(table, data), snapshot_properties=props)
    return table.current_snapshot().snapshot_id


def recorded_batches(identifier: str, catalog: Catalog | None = None) -> set[str]:
    """Every ``ingest_batch`` id recorded in the table's snapshot summaries."""
    table = (catalog or get_catalog()).load_table(identifier)
    out: set[str] = set()
    for snap in table.snapshots():
        props = snap.summary.additional_properties if snap.summary else {}
        if value := props.get(BATCHES_PROPERTY):
            out.update(value.split(","))
    return out


def replace_partition(identifier: str, data: pa.Table, where: BooleanExpression, catalog: Catalog | None = None) -> int:
    """Atomically replace the rows matching ``where`` in a silver/gold table."""
    if identifier.startswith("bronze."):
        raise ValueError("bronze is append-only; rebuild silver from it instead")
    table = (catalog or get_catalog()).load_table(identifier)
    with warnings.catch_warnings():
        # First build of a partition: there is nothing to delete, which PyIceberg warns about.
        warnings.filterwarnings("ignore", "Delete operation did not match any records")
        table.overwrite(_conform(table, data), overwrite_filter=where)
    return table.current_snapshot().snapshot_id


def tag_snapshot(identifier: str, tag: str, snapshot_id: int | None = None, catalog: Catalog | None = None) -> int:
    """Pin a snapshot (e.g. ``mlflow-<run_id>``) so snapshot expiry never removes it."""
    table = (catalog or get_catalog()).load_table(identifier)
    snapshot_id = snapshot_id or table.current_snapshot().snapshot_id
    table.manage_snapshots().create_tag(snapshot_id, tag).commit()
    return snapshot_id


def to_duckdb(
    identifier: str,
    con: duckdb.DuckDBPyConnection,
    view: str | None = None,
    row_filter: BooleanExpression | str = "True",
    snapshot_id: int | None = None,
    catalog: Catalog | None = None,
) -> str:
    """Register an Iceberg scan (optionally at a pinned snapshot) as a DuckDB view."""
    table = (catalog or get_catalog()).load_table(identifier)
    view = view or identifier.replace(".", "_")
    arrow = table.scan(row_filter=row_filter, snapshot_id=snapshot_id).to_arrow()
    con.register(view, arrow)
    return view
