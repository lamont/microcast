"""PurpleAir parsers against the documented LAN and API payload shapes."""

from __future__ import annotations

import pandas as pd
import pytest

from microcast.ingest import purpleair

LOCAL = {
    "SensorId": "84:f3:eb:00:00:00",
    "DateTime": "2026/10/05T06:40:00z",
    "current_temp_f": 72,
    "current_humidity": 41,
    "pressure": 1012.4,
    "pm2_5_atm": 3.1,
    "pm2_5_atm_b": 2.9,
}

HISTORY = {
    "fields": ["time_stamp", "humidity", "temperature", "pressure", "pm2.5_atm_a", "pm2.5_atm_b"],
    "data": [[1791181200, 55.0, 68.0, 1013.0, 4.0, 4.4], [1791181800, None, 67.0, 1013.1, 5.0, 5.2]],
}


def test_local_json():
    df = purpleair.parse_local(LOCAL)
    got = dict(zip(df.variable, df.value, strict=True))
    assert got["pm25_a"] == 3.1 and got["pm25_b"] == 2.9
    assert got["t_sensor"] == pytest.approx(22.22, abs=0.01)
    assert set(df.station_id) == {"purpleair_home"}  # never the public sensor index
    assert df.obs_time.iloc[0] == pd.Timestamp("2026-10-05 06:40", tz="UTC")


def test_history_rows_and_missing_values():
    df = purpleair.parse_history(HISTORY)
    assert len(df[df.variable == "pm25_a"]) == 2
    assert len(df[df.variable == "rh_sensor"]) == 1  # the null humidity is dropped
    assert purpleair.parse_history({"fields": [], "data": []}).empty


def test_lan_fetch_falls_back_to_curl_when_macos_blocks_python(monkeypatch):
    import json
    import subprocess

    import requests

    def blocked(*a, **k):
        raise requests.ConnectionError("Failed to establish a new connection: [Errno 65] No route to host")

    calls = []

    def fake_curl(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(LOCAL | {"place": "outside"}))

    monkeypatch.setattr(purpleair.requests, "get", blocked)
    monkeypatch.setattr(purpleair.subprocess, "run", fake_curl)
    monkeypatch.setattr(purpleair.sys, "platform", "darwin")
    df = purpleair.fetch_local("192.0.2.1")
    assert calls[0][0] == "/usr/bin/curl" and len(df) == 5


def test_lan_fetch_refuses_the_indoor_sensor(monkeypatch):
    monkeypatch.setattr(purpleair, "_get_lan_json", lambda url: LOCAL | {"place": "inside"})
    with pytest.raises(ValueError, match="outdoor"):
        purpleair.fetch_local("192.0.2.1")


def test_history_rows_take_the_network_station_id():
    df = purpleair.parse_history({"fields": ["time_stamp", "temperature"], "data": [[1791181200, 68.0]]}, "pa_92183")
    assert list(df.station_id) == ["pa_92183"] and list(df.variable) == ["t_sensor"]


def test_windows_cover_the_range_without_overlap():
    from datetime import UTC, datetime, timedelta

    a, b = datetime(2025, 4, 1, tzinfo=UTC), datetime(2025, 5, 1, tzinfo=UTC)
    w = purpleair.windows(a, b)
    assert w[0][0] == a and w[-1][1] == b and len(w) == 3
    assert all(x[1] == y[0] for x, y in zip(w, w[1:], strict=False))
    assert all(e - s <= timedelta(days=14) for s, e in w)


def test_backfill_network_resumes_from_cache_and_respects_the_floor(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    calls = []
    balance = iter([800_000, 700_000])
    monkeypatch.setattr(purpleair, "remaining_points", lambda: next(balance))
    monkeypatch.setattr(
        purpleair,
        "history_payload",
        lambda idx, a, b, fields, avg: calls.append(a) or {"fields": ["time_stamp", "temperature"], "data": [[0, 1]]},
    )
    a, b = datetime(2025, 4, 1, tzinfo=UTC), datetime(2025, 5, 1, tzinfo=UTC)
    purpleair.cache_path(tmp_path, "pa_1", a).parent.mkdir(parents=True)
    purpleair.cache_path(tmp_path, "pa_1", a).write_text("{}")  # first window already paid for
    stats = purpleair.backfill_network({"pa_1": 1}, a, b, tmp_path, floor=750_000, log=lambda _: None)
    assert stats == {"cached": 1, "fetched": 1, "rows": 1}  # second balance check is under the floor
    assert len(calls) == 1
    loaded = purpleair.load_cached(tmp_path)
    assert [bid for bid, _ in loaded][1] == "purpleair/history/pa_1/2025-04-15T00"
