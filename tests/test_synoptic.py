"""Synoptic timeseries parsing, including the free tier's history refusal."""

from __future__ import annotations

import pandas as pd
import pytest

from microcast.ingest import synoptic

PAYLOAD = {
    "SUMMARY": {"RESPONSE_CODE": 1, "RESPONSE_MESSAGE": "OK"},
    "STATION": [
        {
            "STID": "604PG",
            "OBSERVATIONS": {
                "date_time": ["2026-10-05T19:40:00Z", "2026-10-05T19:50:00Z"],
                "air_temp_set_1": [15.2, None],
                "wind_speed_set_1": [6.1, 6.7],
                "wind_gust_set_1": [9.3, 10.0],
                "dew_point_temperature_set_1d": [11.0, 11.1],
            },
        }
    ],
}


def test_parse_long_rows():
    df = synoptic.parse_timeseries(PAYLOAD)
    assert set(df.source) == {"synoptic"} and set(df.station_id) == {"604PG"}
    assert len(df[df.variable == "t2m"]) == 1  # the null temperature is dropped
    assert df[df.variable == "d2m"].value.tolist() == [11.0, 11.1]  # derived set is used when no reported set
    assert df.obs_time.iloc[0] == pd.Timestamp("2026-10-05 19:40", tz="UTC")


def test_history_refusal_is_an_error():
    refused = {"SUMMARY": {"RESPONSE_CODE": 403, "RESPONSE_MESSAGE": "does not have access to the requested history"}}
    with pytest.raises(synoptic.SynopticError, match="history"):
        synoptic.parse_timeseries(refused)
