"""NWP ingest: Herbie byte-range subset -> pick_points(k=4) -> long Arrow table.

``to_long`` is pure (xarray in, Arrow out) and tested offline. ``fetch_cycle``
does the network work and is what the CLI and, later, the Dagster asset call.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import xarray as xr
import yaml

from microcast import settings
from microcast.lake.schemas import NWP_POINT_SCHEMA
from microcast.registry import Registry

K_NEIGHBOURS = 4


@dataclass(frozen=True)
class ModelConfig:
    key: str
    herbie_model: str
    product: str
    search: str
    cadence_h: int
    leads: tuple[int, int, int]
    enabled: bool = True
    domain: str | None = None
    members: list[int] = field(default_factory=list)

    def lead_hours(self) -> list[int]:
        first, last, step = self.leads
        return list(range(first, last + 1, step))


def load_models(path: Path | None = None) -> dict[str, ModelConfig]:
    raw = yaml.safe_load((path or settings.config_dir() / "models.yaml").read_text())
    return {
        key: ModelConfig(
            key=key,
            herbie_model=cfg["herbie_model"],
            product=str(cfg["product"]),
            search=cfg["search"].strip(),
            cadence_h=int(cfg["cadence_h"]),
            leads=tuple(cfg["leads"]),
            enabled=bool(cfg.get("enabled", True)),
            domain=cfg.get("domain"),
            members=list(cfg.get("members", [])),
        )
        for key, cfg in raw.items()
    }


def _level(ds: xr.Dataset, da: xr.DataArray) -> str:
    level_type = da.attrs.get("GRIB_typeOfLevel", "unknown")
    coord = ds.coords.get(level_type)
    if coord is not None and coord.size == 1 and level_type != "surface":
        return f"{level_type}:{float(coord.values):g}"
    return level_type


def _utc(values: pd.Series) -> pd.Series:
    out = pd.to_datetime(values)
    return out.dt.tz_localize("UTC") if out.dt.tz is None else out.dt.tz_convert("UTC")


def to_long(
    picked: xr.Dataset | list[xr.Dataset],
    *,
    model: str,
    product: str,
    ingest_batch: str,
    member: int = 0,
    ingested_at: datetime | None = None,
) -> pa.Table:
    """Flatten ``pick_points`` output to bronze.nwp_point rows.

    Handles a single Dataset or the list cfgrib returns when messages don't fit
    one hypercube, with or without the ``k`` (neighbour) and ``step``
    (sub-hourly) dimensions.
    """
    datasets = picked if isinstance(picked, list) else [picked]
    ingested_at = ingested_at or datetime.now(UTC)
    frames = []
    for ds in datasets:
        for name, da in ds.data_vars.items():
            df = da.to_dataframe(name="value").reset_index()
            n = len(df)
            if "k" not in df:
                df["k"] = 0
            step = pd.to_timedelta(df["step"]) if "step" in df else pd.Series(pd.Timedelta(0), index=df.index)
            dist_km = df["point_grid_distance"] if "point_grid_distance" in df else np.nan
            frames.append(
                pd.DataFrame(
                    {
                        "model": model,
                        "product": product,
                        "init_time": _utc(df["time"]),
                        "lead_min": (step / pd.Timedelta(minutes=1)).astype("int32"),
                        "valid_time": _utc(df["valid_time"]),
                        "point_id": df["point_id"].astype(str),
                        "variable": str(name) if name != "unknown" else da.attrs.get("GRIB_shortName", "unknown"),
                        "level": _level(ds, da),
                        "member": np.full(n, member, dtype="int32"),
                        "k": df["k"].astype("int32"),
                        "value": df["value"].astype("float64"),
                        "grid_lat": df["latitude"].astype("float64") if "latitude" in df else np.nan,
                        "grid_lon": ((df["longitude"] + 180) % 360 - 180).astype("float64")
                        if "longitude" in df
                        else np.nan,
                        "grid_dist_m": dist_km * 1000.0,
                        "ingest_batch": ingest_batch,
                        "ingested_at": pd.Timestamp(ingested_at).tz_convert("UTC"),
                    }
                )
            )
    if not frames:
        return NWP_POINT_SCHEMA.as_arrow().empty_table()
    out = pd.concat(frames, ignore_index=True)
    return pa.Table.from_pandas(out, schema=NWP_POINT_SCHEMA.as_arrow(), preserve_index=False)


def batch_id(model: str, init: datetime, fxx: int, member: int = 0) -> str:
    return f"{model}/{init:%Y-%m-%dT%H}/f{fxx:03d}/m{member}"


def fetch_cycle(
    cfg: ModelConfig,
    init: datetime,
    registry: Registry,
    leads: list[int] | None = None,
) -> pa.Table:
    """Download and extract one cycle (all members, requested leads) for every registry point."""
    import herbie
    from herbie import Herbie

    cache = settings.data_dir() / "grib"
    # pick_points caches its BallTree under Herbie's save_dir; keep it in our data dir.
    herbie.config["default"]["save_dir"] = cache
    points = registry.points_df()
    tables = []
    for member in cfg.members or [None]:
        for fxx in leads if leads is not None else cfg.lead_hours():
            extra = {"member": member} if member is not None else {}
            if cfg.domain:
                extra["domain"] = cfg.domain
            H = Herbie(
                init.replace(tzinfo=None),
                model=cfg.herbie_model,
                product=cfg.product,
                fxx=fxx,
                save_dir=cache,
                priority=["aws", "nomads"],
                verbose=False,
                **extra,
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)  # cfgrib's xr.merge default-change notice
                ds = H.xarray(cfg.search, remove_grib=False)
            datasets = ds if isinstance(ds, list) else [ds]
            tree = f"{cfg.herbie_model}_{cfg.domain or 'conus'}"
            picked = [d.herbie.pick_points(points, method="nearest", k=K_NEIGHBOURS, tree_name=tree) for d in datasets]
            tables.append(
                to_long(
                    picked,
                    model=cfg.key,
                    product=cfg.product,
                    member=member or 0,
                    ingest_batch=batch_id(cfg.key, init, fxx, member or 0),
                )
            )
    return pa.concat_tables(tables)
