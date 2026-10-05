"""Observation parsers against small real-format samples."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from microcast.ingest import obs
from microcast.lake.schemas import OBS_SCHEMA

ASOS = """station,valid,tmpf,dwpf,sknt,drct,gust
SFO,2026-09-01 00:56,64.00,52.00,13.00,270.00,
SFO,2026-09-01 01:56,63.00,,11.00,270.00,22.00
"""

HADS = """station,utc_valid,PPQRZZZ,TAIRGZZ,TAIRZNZ
SFOC1,2026-09-01 00:00:00,0.0,,
SFOC1,2026-09-01 00:43:00,,62.88,
"""

NDBC = """#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS  TIDE
#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi    ft
2025 01 01 00 00 270  1.1  1.3 99.00 99.00 99.00 999 1022.2  11.9 999.0 999.0 99.0 99.00
2025 01 01 00 06 999 99.0  MM 99.00 99.00 99.00 999 1022.2  12.1 999.0 999.0 99.0 99.00
"""


def _value(df: pd.DataFrame, variable: str, i: int = 0) -> float:
    return df[df.variable == variable].value.iloc[i]


def test_asos_si_units_and_sparse_gust():
    df = obs.parse_iem_asos(ASOS, "KSFO")
    assert set(df.station_id) == {"KSFO"} and set(df.source) == {"iem_asos"}
    assert _value(df, "t2m") == pytest.approx(17.78, abs=0.01)
    assert _value(df, "wind10") == pytest.approx(13 * 0.514444)
    # Gust is only reported when gusting, and a missing dewpoint is dropped, not zero.
    assert len(df[df.variable == "gust"]) == 1 and _value(df, "gust") == pytest.approx(22 * 0.514444)
    assert len(df[df.variable == "d2m"]) == 1
    assert df.obs_time.iloc[0] == pd.Timestamp("2026-09-01 00:56", tz="UTC")


def test_hads_temperature_only():
    df = obs.parse_iem_hads(HADS, "SFOC1")
    assert list(df.variable) == ["t2m"]
    assert _value(df, "t2m") == pytest.approx(17.156, abs=0.01)


def test_ndbc_missing_sentinels():
    df = obs.parse_ndbc_stdmet(NDBC, "FTPC1")
    first = df[df.obs_time == pd.Timestamp("2025-01-01 00:00", tz="UTC")]
    assert dict(zip(first.variable, first.value, strict=True)) == {
        "t2m": 11.9,
        "wind10": 1.1,
        "gust": 1.3,
        "wdir10": 270.0,
    }
    second = df[df.obs_time == pd.Timestamp("2025-01-01 00:06", tz="UTC")]
    assert list(second.variable) == ["t2m"]  # 999 dir, 99.0 speed and MM gust are all missing


def test_to_arrow_schema():
    t = obs.to_arrow(obs.parse_iem_asos(ASOS, "KSFO"), "iem_asos/KSFO/2026-09")
    assert t.schema == OBS_SCHEMA.as_arrow()


def test_months_cover_range():
    assert obs.months(date(2025, 11, 15), date(2026, 2, 1)) == [
        (date(2025, 11, 15), date(2025, 12, 1)),
        (date(2025, 12, 1), date(2026, 1, 1)),
        (date(2026, 1, 1), date(2026, 2, 1)),
    ]
