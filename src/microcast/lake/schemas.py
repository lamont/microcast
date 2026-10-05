"""Iceberg schemas and partition specs for the lake.

Bronze tables are long-format (one row per variable per time) so adding a model
or variable never changes a schema. Every bronze row carries ``ingested_at`` and
``ingest_batch`` so backtests can replay exactly what was visible at a given
moment, and silver can dedupe reruns (see docs/decisions.md, D1).
"""

from __future__ import annotations

from dataclasses import dataclass

from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import DayTransform, IdentityTransform
from pyiceberg.types import (
    DoubleType,
    IntegerType,
    NestedField,
    StringType,
    TimestamptzType,
)


@dataclass(frozen=True)
class TableDef:
    namespace: str
    name: str
    schema: Schema
    spec: PartitionSpec

    @property
    def identifier(self) -> str:
        return f"{self.namespace}.{self.name}"


def _spec(schema: Schema, *fields: tuple[str, str]) -> PartitionSpec:
    out = []
    for i, (col, transform) in enumerate(fields):
        source = schema.find_field(col).field_id
        t = IdentityTransform() if transform == "identity" else DayTransform()
        name = col if transform == "identity" else f"{col}_day"
        out.append(PartitionField(source_id=source, field_id=1000 + i, transform=t, name=name))
    return PartitionSpec(*out)


NWP_POINT_SCHEMA = Schema(
    NestedField(1, "model", StringType(), required=True),
    NestedField(2, "product", StringType(), required=True),
    NestedField(3, "init_time", TimestamptzType(), required=True),
    NestedField(4, "lead_min", IntegerType(), required=True),
    NestedField(5, "valid_time", TimestamptzType(), required=True),
    NestedField(6, "point_id", StringType(), required=True),
    NestedField(7, "variable", StringType(), required=True),
    NestedField(8, "level", StringType(), required=True),
    NestedField(9, "member", IntegerType(), required=True),
    NestedField(10, "k", IntegerType(), required=True),  # neighbour rank, 0 = nearest
    NestedField(11, "value", DoubleType()),
    NestedField(12, "grid_lat", DoubleType()),  # the neighbour cell itself
    NestedField(13, "grid_lon", DoubleType()),
    NestedField(14, "grid_dist_m", DoubleType()),
    NestedField(15, "ingest_batch", StringType(), required=True),
    NestedField(16, "ingested_at", TimestamptzType(), required=True),
)

OBS_SCHEMA = Schema(
    NestedField(1, "source", StringType(), required=True),
    NestedField(2, "station_id", StringType(), required=True),
    NestedField(3, "obs_time", TimestamptzType(), required=True),
    NestedField(4, "variable", StringType(), required=True),
    NestedField(5, "value", DoubleType()),
    NestedField(6, "qc_flag", StringType()),
    NestedField(7, "ingest_batch", StringType(), required=True),
    NestedField(8, "ingested_at", TimestamptzType(), required=True),
)

BRONZE_NWP_POINT = TableDef(
    "bronze", "nwp_point", NWP_POINT_SCHEMA, _spec(NWP_POINT_SCHEMA, ("model", "identity"), ("init_time", "day"))
)
BRONZE_OBS = TableDef("bronze", "obs", OBS_SCHEMA, _spec(OBS_SCHEMA, ("source", "identity"), ("obs_time", "day")))

TABLES: list[TableDef] = [BRONZE_NWP_POINT, BRONZE_OBS]
