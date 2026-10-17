import pandas as pd
import pytest

from microcast import pipeline


def test_window_bounds_are_utc_and_end_exclusive():
    start, end = pipeline._window_bounds("2026-06-01", "2026-10-01")
    assert start == pd.Timestamp("2026-06-01", tz="UTC")
    assert end == pd.Timestamp("2026-10-01", tz="UTC")


def test_window_bounds_reject_empty_range():
    with pytest.raises(ValueError, match="start must be before end"):
        pipeline._window_bounds("2026-10-01", "2026-10-01")


def test_window_bounds_default_is_unbounded():
    assert pipeline._window_bounds(None, None) == (None, None)


@pytest.mark.parametrize("value", [pd.NaT, "NaT"])
def test_window_bounds_reject_missing_endpoints(value):
    with pytest.raises(ValueError, match="finite timestamps"):
        pipeline._window_bounds(value, None)


def test_filter_window_excludes_october_smoke_rows():
    gold = pd.DataFrame(
        {
            "init_time": pd.to_datetime(
                ["2026-05-31T23:00Z", "2026-06-01T00:00Z", "2026-09-30T23:00Z", "2026-10-01T00:00Z"]
            )
        }
    )
    start, end = pipeline._window_bounds("2026-06-01", "2026-10-01")
    filtered = pipeline._filter_window(gold, start, end)
    assert filtered.init_time.tolist() == list(pd.to_datetime(["2026-06-01T00:00Z", "2026-09-30T23:00Z"]))
