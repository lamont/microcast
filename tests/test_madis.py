"""MADIS hourly file parsing on a small synthetic netCDF-3 file."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime

import numpy as np
import xarray as xr

from microcast.ingest import madis
from microcast.lake.schemas import OBS_SCHEMA, STATIONS_SCHEMA

T = datetime(2025, 6, 1, 20, tzinfo=UTC).timestamp()


def _file() -> bytes:
    n = 3
    ds = xr.Dataset(
        {
            "stationId": ("recNum", np.array([b"C5988", b"C5988", b"KLAX"], dtype="S6")),
            "dataProvider": ("recNum", np.array([b"APRSWXNET", b"APRSWXNET", b"ASOS"], dtype="S11")),
            "latitude": ("recNum", np.array([37.76, 37.76, 33.94], dtype="f4")),
            "longitude": ("recNum", np.array([-122.43, -122.43, -118.4], dtype="f4")),
            "elevation": ("recNum", np.array([54.0, 54.0, 38.0], dtype="f4")),
            "observationTime": ("recNum", np.array([T - 600, T, T], dtype="f8")),
            "temperature": ("recNum", np.array([288.15, np.nan, 300.0], dtype="f4")),
            "temperatureDD": ("recNum", np.array([b"V", b"Z", b"V"], dtype="S1")),
            "altimeter": ("recNum", np.array([101300.0, 101290.0, 101000.0], dtype="f4")),
            "altimeterDD": ("recNum", np.array([b"V", b"B", b"V"], dtype="S1")),
        },
    )
    assert ds.sizes["recNum"] == n
    return gzip.compress(ds.to_netcdf(engine="scipy"))


def test_parse_keeps_box_converts_units_and_flags():
    obs, stations = madis.parse_hour(_file(), madis.Box(), "madis/2025-06-01T20")
    assert obs.schema == OBS_SCHEMA.as_arrow() and stations.schema == STATIONS_SCHEMA.as_arrow()
    df = obs.to_pandas()
    assert set(df.station_id) == {"C5988"}  # KLAX is outside the box
    t = df[df.variable == "t2m"]
    assert len(t) == 1 and abs(t.value.iloc[0] - 15.0) < 1e-4  # NaN second report dropped
    alt = df[df.variable == "altimeter"].sort_values("obs_time")
    assert alt.value.round(1).tolist() == [1013.0, 1012.9] and alt.qc_flag.tolist() == ["V", "B"]
    st = stations.to_pylist()
    assert len(st) == 1 and st[0]["provider"] == "APRSWXNET"


def test_hour_url():
    assert madis.hour_url(datetime(2025, 6, 1, 20, tzinfo=UTC)).endswith(
        "/2025/06/01/LDAD/mesonet/netCDF/20250601_2000.gz"
    )
