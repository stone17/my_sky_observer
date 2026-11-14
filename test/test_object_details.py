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

