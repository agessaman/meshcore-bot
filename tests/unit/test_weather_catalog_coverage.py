"""Every bundled language has the weather replies' own words, instead of falling back to English."""

import json
from pathlib import Path

import pytest

TRANSLATIONS = Path(__file__).resolve().parents[2] / "translations"
CATALOGS = sorted(TRANSLATIONS.glob("*.json"))
ENGLISH = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
# Replies a user sees when a forecast is missing or fails.
REPLIES = [
    f"commands.{ns}.{key}"
    for ns in ("wx", "gwx")
    for key in (
        "hourly_not_available", "source_option_not_available", "tomorrow_not_available", "tomorrow_error",
        "multiday_not_available", "multiday_error", "mqtt_forecast_not_supported", "mqtt_weather_no_data",
        "mqtt_weather_no_subscriber", "mqtt_weather_payload_error", "mqtt_weather_stale",
    )
]
LABELS = (
    [f"commands.{ns}.day_abbrev.{day}" for ns in ("wx", "gwx") for day in DAYS]
    + ["commands.gwx.feels_like", "commands.gwx.warnings.high_winds"]
)
# Catalogs that use the English catalog's wind letters on purpose.
INTERNATIONAL_WIND_LETTERS = {"en", "en-GB", "pl"}


def _get(catalog, key):
    node = catalog
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", CATALOGS, ids=lambda p: p.stem)
def test_weather_keys_are_present(path):
    catalog = _load(path)
    assert [key for key in REPLIES + LABELS if _get(catalog, key) is None] == []


@pytest.mark.parametrize("path", [p for p in CATALOGS if not p.stem.startswith("en")], ids=lambda p: p.stem)
def test_weather_replies_are_not_left_in_english(path):
    catalog = _load(path)
    assert [key for key in REPLIES + ["commands.gwx.feels_like"] if _get(catalog, key) == _get(ENGLISH, key)] == []


@pytest.mark.parametrize("path", CATALOGS, ids=lambda p: p.stem)
def test_wind_directions_are_complete(path):
    directions = _get(_load(path), "common.wind_directions")
    if path.stem in INTERNATIONAL_WIND_LETTERS and path.stem != "en":
        assert directions is None or set(directions) == set(POINTS)
        return
    assert directions is not None and set(directions) == set(POINTS)
    assert len(set(directions.values())) == 16


@pytest.mark.parametrize("path", CATALOGS, ids=lambda p: p.stem)
def test_the_high_wind_warning_names_its_unit(path):
    assert "{unit}" in _get(_load(path), "commands.gwx.warnings.high_winds")
