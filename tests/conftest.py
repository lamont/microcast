from __future__ import annotations

import pytest
from pyiceberg.catalog import load_catalog


@pytest.fixture(autouse=True)
def _home_coords(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MICROCAST_HOME_LAT", "37.7609")
    monkeypatch.setenv("MICROCAST_HOME_LON", "-122.4350")
    monkeypatch.setenv("MICROCAST_PURPLEAIR_SENSOR_INDEX", "12345")


@pytest.fixture
def catalog(tmp_path):
    (tmp_path / "warehouse").mkdir()
    return load_catalog(
        "test",
        type="sql",
        uri=f"sqlite:///{tmp_path / 'catalog.db'}",
        warehouse=(tmp_path / "warehouse").as_uri(),
    )
