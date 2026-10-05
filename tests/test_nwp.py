"""to_long against synthetic datasets shaped like Herbie pick_points output."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from microcast.ingest.nwp import batch_id, load_models, to_long
from microcast.lake.schemas import NWP_POINT_SCHEMA

INIT = np.datetime64("2026-10-04T12:00")
POINTS = ["home", "school_walk:0", "school_walk:1"]


def _picked(k: int = 4, steps: list[int] | None = None) -> xr.Dataset:
    """Mimic pick_points(method='nearest', k=k): dims (k, point[, step])."""
    npt = len(POINTS)
    dims, shape, coords = ["k", "point"], [k, npt], {}
    if steps:
        dims.append("step")
        shape.append(len(steps))
        step = np.array(steps, dtype="timedelta64[m]")
        coords["step"] = ("step", step)
        coords["valid_time"] = ("step", INIT + step)
    else:
        coords["step"] = np.timedelta64(180, "m")
        coords["valid_time"] = INIT + np.timedelta64(180, "m")
    coords |= {
        "time": INIT,
        "heightAboveGround": 2.0,
        "point_id": ("point", POINTS),
        "point_grid_distance": (("k", "point"), np.linspace(0.5, 2.5, k * npt).reshape(k, npt)),
        # Herbie returns each neighbour cell's own lat/lon, longitude in 0-360
        "latitude": (("k", "point"), np.full((k, npt), 37.76)),
        "longitude": (("k", "point"), np.full((k, npt), 237.56)),
    }
    t2m = xr.DataArray(np.full(shape, 290.0), dims=dims, attrs={"GRIB_typeOfLevel": "heightAboveGround"})
    return xr.Dataset({"t2m": t2m}, coords=coords)


def test_hourly_k4_shape():
    out = to_long(_picked(), model="hrrr", product="sfc", ingest_batch="b")
    assert out.schema.equals(NWP_POINT_SCHEMA.as_arrow())
    assert out.num_rows == 4 * len(POINTS)
    df = out.to_pandas()
    assert set(df.k) == {0, 1, 2, 3}
    assert set(df.lead_min) == {180}
    assert set(df.level) == {"heightAboveGround:2"}
    assert df.grid_dist_m.min() == 500.0
    assert df.grid_lon.iloc[0] == pytest.approx(-122.44)
    assert (df.valid_time - df.init_time == pd.Timedelta(hours=3)).all()
    assert str(df.init_time.dt.tz) == "UTC"


def test_subhourly_steps_become_rows():
    out = to_long(_picked(k=1, steps=[15, 30, 45, 60]), model="hrrr_subh", product="subh", ingest_batch="b")
    df = out.to_pandas()
    assert sorted(set(df.lead_min)) == [15, 30, 45, 60]
    assert out.num_rows == 4 * len(POINTS)


def test_list_of_hypercubes():
    out = to_long([_picked(), _picked()], model="hrrr", product="sfc", ingest_batch="b")
    assert out.num_rows == 2 * 4 * len(POINTS)


def test_models_config_parses():
    models = load_models()
    assert models["hrrr"].enabled and models["hrrr"].lead_hours()[:3] == [0, 1, 2]
    assert models["rrfs_ens"].members == [1, 2, 3, 4, 5]
    assert batch_id("hrrr", pd.Timestamp("2026-10-04T12"), 3) == "hrrr/2026-10-04T12/f003/m0"
