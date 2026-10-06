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
    IcebergType,
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


def _wide(*cols: tuple[str, IcebergType, bool]) -> Schema:
    return Schema(*(NestedField(i, name, t, required=req) for i, (name, t, req) in enumerate(cols, start=1)))


_D, _S, _I, _T = DoubleType(), StringType(), IntegerType(), TimestamptzType()

# Deduped, range-checked obs in SI units, still long. qc_flag is "ok" or the failed check.
OBS_QC_SCHEMA = _wide(
    ("source", _S, True),
    ("station_id", _S, True),
    ("obs_time", _T, True),
    ("variable", _S, True),
    ("value", _D, False),
    ("qc_flag", _S, True),
    ("ingested_at", _T, True),
)

# NWP pivoted wide per (model, cycle, lead, point): distance-weighted mean of the
# k=4 cells, winds rotated to earth-relative, temperatures in degC.
NWP_FIELDS = [
    "t2m",
    "d2m",
    "rh2m",
    "u10",
    "v10",
    "wind10",
    "wdir10",
    "gust",
    "prate",
    "vis",
    "lcc",
    "tcc",
    "cloud_base",
    "ceiling",
    "dswrf",
    "t2m_spread",  # max - min over the 4 cells: a gradient/edge signal
    "gust_spread",
]
NWP_ALIGNED_SCHEMA = _wide(
    ("model", _S, True),
    ("init_time", _T, True),
    ("lead_min", _I, True),
    ("valid_time", _T, True),
    ("point_id", _S, True),
    *((f, _D, False) for f in NWP_FIELDS),
    ("ingested_at", _T, True),
)

TARGETS = ["t2m", "gust"]
# One row per (station point, cycle, lead). issue_time is when the forecast could
# first be made (HRRR history: init + 55 min). Every obs-derived feature is as of
# issue_time; targets are obs at valid_time.
TRAINING_SCHEMA = _wide(
    ("point_id", _S, True),
    ("init_time", _T, True),
    ("issue_time", _T, True),
    ("lead_min", _I, True),
    ("valid_time", _T, True),
    *((f"nwp_{f}", _D, False) for f in NWP_FIELDS),
    ("hour_sin", _D, True),
    ("hour_cos", _D, True),
    ("doy_sin", _D, True),
    ("doy_cos", _D, True),
    *(
        (f"{prefix}_{t}", _D, False)
        for t in TARGETS
        for prefix in ("obs_last", "err_at_init", "bias14", "std14", "obs")
    ),
)

# Every prediction a model made (backtest now, live later), as a Gaussian plus quantiles.
FORECASTS_SCHEMA = _wide(
    ("model_name", _S, True),
    ("run_id", _S, True),
    ("kind", _S, True),  # backtest | live | shadow
    ("fold", _S, False),
    ("variable", _S, True),
    ("point_id", _S, True),
    ("init_time", _T, True),
    ("issue_time", _T, True),
    ("lead_min", _I, True),
    ("valid_time", _T, True),
    ("mu", _D, True),
    ("sigma", _D, True),
    ("p10", _D, True),
    ("p50", _D, True),
    ("p90", _D, True),
)

# Forecasts joined to the truth once it exists.
SCORES_SCHEMA = _wide(
    ("model_name", _S, True),
    ("run_id", _S, True),
    ("kind", _S, True),
    ("fold", _S, False),
    ("variable", _S, True),
    ("point_id", _S, True),
    ("init_time", _T, True),
    ("lead_min", _I, True),
    ("valid_time", _T, True),
    ("obs", _D, True),
    ("mu", _D, True),
    ("sigma", _D, True),
    ("error", _D, True),  # mu - obs
    ("crps", _D, True),
    ("in_p10_p90", IntegerType(), True),
)

# Where each observing station was, as reported with its data (MADIS); one row per
# station per batch. Network features (upwind stations) join on this.
STATIONS_SCHEMA = _wide(
    ("source", _S, True),
    ("station_id", _S, True),
    ("provider", _S, False),
    ("lat", _D, True),
    ("lon", _D, True),
    ("elevation_m", _D, False),
    ("ingest_batch", _S, True),
    ("ingested_at", _T, True),
)
BRONZE_STATIONS = TableDef(
    "bronze", "stations", STATIONS_SCHEMA, _spec(STATIONS_SCHEMA, ("source", "identity"), ("ingested_at", "day"))
)

SILVER_OBS_QC = TableDef(
    "silver", "obs_qc", OBS_QC_SCHEMA, _spec(OBS_QC_SCHEMA, ("source", "identity"), ("obs_time", "day"))
)
SILVER_NWP_ALIGNED = TableDef(
    "silver",
    "nwp_aligned",
    NWP_ALIGNED_SCHEMA,
    _spec(NWP_ALIGNED_SCHEMA, ("model", "identity"), ("init_time", "day")),
)
GOLD_TRAINING = TableDef("gold", "training_examples", TRAINING_SCHEMA, _spec(TRAINING_SCHEMA, ("init_time", "day")))
GOLD_FORECASTS = TableDef(
    "gold", "forecasts", FORECASTS_SCHEMA, _spec(FORECASTS_SCHEMA, ("model_name", "identity"), ("valid_time", "day"))
)
GOLD_SCORES = TableDef(
    "gold", "scores", SCORES_SCHEMA, _spec(SCORES_SCHEMA, ("model_name", "identity"), ("valid_time", "day"))
)

# Network features, long: one row per (cycle, feature), the same for every point.
# Long keeps the schema fixed as sensors come and go; the pipeline pivots it wide.
NETWORK_FEATURES_SCHEMA = _wide(
    ("init_time", _T, True),
    ("feature", _S, True),
    ("value", _D, False),
)
GOLD_NETWORK_FEATURES = TableDef("gold", "network_features", NETWORK_FEATURES_SCHEMA, _spec(NETWORK_FEATURES_SCHEMA))

TABLES: list[TableDef] = [
    BRONZE_NWP_POINT,
    BRONZE_OBS,
    BRONZE_STATIONS,
    SILVER_OBS_QC,
    SILVER_NWP_ALIGNED,
    GOLD_TRAINING,
    GOLD_NETWORK_FEATURES,
    GOLD_FORECASTS,
    GOLD_SCORES,
]
