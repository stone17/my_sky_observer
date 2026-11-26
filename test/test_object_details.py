import asyncio
import os
import sys
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.main import app
from backend.stream_service import build_single_object_details


class _StubCatalogManager:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def get_all_objects(self, _names):
        return self._df


def _make_sample_catalog_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": "M 42",
                "name": "Orion Nebula",
                "type": "HII",
                "constellation": "Ori",
                "other_id": "NGC 1976",
                "aliases": "NGC 1976, LBN 974",
                "sub_objects": [],
                "ra": 83.8221,
                "dec": -5.3911,
                "mag": 4.0,
                "maj_ax": 85.0,
                "min_ax": 60.0,
            },
            {
                "id": "Cl Trumpler 14/16",
                "name": "Carina Cluster Pair",
                "type": "OCl",
                "constellation": "Car",
                "other_id": "",
                "aliases": "Cr 228/232",
                "sub_objects": [],
                "ra": 160.98,
                "dec": -59.55,
                "mag": 5.5,
                "maj_ax": 15.0,
                "min_ax": 12.0,
            },
        ]
    )


def test_build_single_object_details_exact_and_alias():
    stub_cm = _StubCatalogManager(_make_sample_catalog_df())
    settings = {
        "location": {"latitude": 40.7128, "longitude": -74.0060},
        "telescope": {"focal_length": 400.0},
        "camera": {"sensor_width": 23.5, "sensor_height": 15.6},
        "min_altitude": 25.0,
        "catalogs": ["messier"],
    }

    # Normalized lookup ("m42" -> "M 42")
    detail = asyncio.run(
        build_single_object_details("m42", settings, catalog_manager=stub_cm)
    )
    assert detail["name"] == "M 42"
    assert detail["common_name"] == "Orion Nebula"
    assert detail["constellation"] == "Ori"
    assert isinstance(detail["altitude_graph"], list)
    assert len(detail["altitude_graph"]) == 60
    assert isinstance(detail["moon_graph"], list)
    assert len(detail["moon_graph"]) == 60
    assert isinstance(detail["sun_graph"], list)
    assert len(detail["sun_graph"]) == 60
    assert "hours_above_min" in detail
    assert "hours_visible" in detail
    assert detail["size"] == "85.0' x 60.0'"

    # Alias lookup ("NGC1976" -> "M 42")
    alias_detail = asyncio.run(
        build_single_object_details("NGC1976", settings, catalog_manager=stub_cm)
    )
    assert alias_detail["name"] == "M 42"
    assert len(alias_detail["altitude_graph"]) == 60


def test_api_object_details_slash_id_and_location_overrides():
    stub_cm = _StubCatalogManager(_make_sample_catalog_df())
    mock_settings = {
        "location": {"latitude": 50.0, "longitude": 8.0},
        "telescope": {"focal_length": 500.0},
        "camera": {"sensor_width": 23.5, "sensor_height": 15.6},
        "min_altitude": 30.0,
    }

    with (
        patch("backend.main.load_settings", return_value=dict(mock_settings)),
        patch("backend.main.catalogs", stub_cm),
    ):
        client = TestClient(app)
        resp = client.get(
            "/api/object-details/Cl%20Trumpler%2014%2F16",
            params={
                "latitude": -33.8688,
                "longitude": 151.2093,
                "tz_offset": -600,
                "min_hours_altitude": 20.0,
            },
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["name"] == "Cl Trumpler 14/16"
        assert len(payload["altitude_graph"]) == 60
        assert payload["max_altitude"] > 50.0  # High in southern sky from Sydney

        missing = client.get("/api/object-details/DOES_NOT_EXIST_99999")
        assert missing.status_code == 404


def test_real_catalog_single_object_details():
    settings = {
        "location": {"latitude": 48.1351, "longitude": 11.5820},
        "telescope": {"focal_length": 400.0},
        "camera": {"sensor_width": 23.5, "sensor_height": 15.6},
        "min_altitude": 30.0,
        "catalogs": ["messier"],
    }
    detail = asyncio.run(build_single_object_details("M 31", settings))
    assert detail["name"] == "M 31"
    assert len(detail["altitude_graph"]) == 60
    assert len(detail["moon_graph"]) == 60
    assert len(detail["sun_graph"]) == 60
    assert "night_times" in detail


def test_api_object_details_obs_date_updates_graph_and_night_times():
    stub_cm = _StubCatalogManager(_make_sample_catalog_df())
    mock_settings = {
        "location": {"latitude": 40.7128, "longitude": -74.0060},
        "telescope": {"focal_length": 500.0},
        "camera": {"sensor_width": 23.5, "sensor_height": 15.6},
        "min_altitude": 25.0,
    }

    with (
        patch("backend.main.load_settings", return_value=dict(mock_settings)),
        patch("backend.main.catalogs", stub_cm),
    ):
        client = TestClient(app)
        winter_resp = client.get(
            "/api/object-details/M%2042",
            params={"obs_date": "2026-01-15"},
        )
        summer_resp = client.get(
            "/api/object-details/M%2042",
            params={"obs_date": "2026-06-15"},
        )
        assert winter_resp.status_code == 200
        assert summer_resp.status_code == 200

        winter = winter_resp.json()
        summer = summer_resp.json()

        assert winter["altitude_graph"][0]["time"].startswith("2026-01-15T")
        assert summer["altitude_graph"][0]["time"].startswith("2026-06-15T")
        assert "night_times" in winter and "night" in winter["night_times"]
        assert "night_times" in summer and "night" in summer["night_times"]
        # M 42 (Orion) is prominent on January nights and low/daytime in June
        assert winter["hours_visible"] > summer["hours_visible"] + 4.0


def test_solar_system_object_details():
    mock_settings = {
        "location": {"latitude": 48.1351, "longitude": 11.5820},
        "telescope": {"focal_length": 1000.0},
        "camera": {"sensor_width": 23.5, "sensor_height": 15.6},
        "min_altitude": 20.0,
    }
    with patch("backend.main.load_settings", return_value=dict(mock_settings)):
        client = TestClient(app)

        jup_resp = client.get(
            "/api/object-details/Jupiter", params={"obs_date": "2026-11-26"}
        )
        assert jup_resp.status_code == 200
        jup = jup_resp.json()
        assert jup["name"] == "Jupiter"
        assert jup["is_solar_system"] is True
        assert jup["solar_id"] == "jupiter"
        assert jup["type"] == "Planet"
        assert jup["status"] == "live"
        assert len(jup["altitude_graph"]) == 60
        assert '"' in jup["size"]  # Planet size formatted in arcseconds

        moon_resp = client.get(
            "/api/object-details/Moon", params={"obs_date": "2026-11-26"}
        )
        assert moon_resp.status_code == 200
        moon = moon_resp.json()
        assert moon["name"] == "Moon"
        assert moon["is_solar_system"] is True
        assert moon["solar_id"] == "moon"
        assert "'" in moon["size"]  # Moon size formatted in arcminutes

        sun_resp = client.get(
            "/api/object-details/Sun", params={"obs_date": "2026-06-21"}
        )
        assert sun_resp.status_code == 200
        sun = sun_resp.json()
        assert sun["name"] == "Sun"
        assert sun["is_solar_system"] is True
        assert sun["solar_id"] == "sun"
        assert max(pt["altitude"] for pt in sun["altitude_graph"]) > 55.0
        assert sun["hours_visible"] > 6.0



