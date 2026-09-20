#!/usr/bin/env python3
"""Checks across the shipped translation catalogs.

The English catalog is every other locale's fallback, so a wrong string there
leaks into nine other languages. These are the two mistakes that has actually
produced: a translated string committed to en.json, and a key defined only in
a non-English catalog (which then falls back to its own key path).
"""

import json
import re
from pathlib import Path

import pytest

TRANSLATIONS = Path(__file__).resolve().parents[2] / "translations"
CATALOGS = sorted(p for p in TRANSLATIONS.glob("*.json"))
NON_ENGLISH = [p for p in CATALOGS if p.stem not in ("en", "en-GB")]


def flatten(node, prefix=""):
    """Flatten a catalog into {dotted.key: value}."""
    flat = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten(value, path))
        else:
            flat[path] = value
    return flat


def load(path):
    with open(path, encoding="utf-8") as handle:
        return flatten(json.load(handle))


@pytest.fixture(scope="module")
def english():
    return load(TRANSLATIONS / "en.json")


@pytest.mark.unit
def test_catalogs_exist():
    assert len(CATALOGS) >= 10, [p.name for p in CATALOGS]


@pytest.mark.unit
def test_english_catalog_holds_no_cyrillic(english):
    offenders = {k: v for k, v in english.items()
                 if isinstance(v, str) and any("Ѐ" <= ch <= "ӿ" for ch in v)}
    assert offenders == {}, f"non-English text in en.json: {offenders}"


@pytest.mark.unit
@pytest.mark.parametrize("path", NON_ENGLISH, ids=lambda p: p.stem)
def test_no_key_is_defined_only_outside_english(english, path):
    """A key missing from en.json has no fallback and renders as its key path."""
    orphans = sorted(set(load(path)) - set(english))
    assert orphans == [], f"{path.name} defines keys absent from en.json: {orphans}"


# Placeholder drift predates this suite across most command namespaces; these
# are the namespaces the weather/alert localization owns.
WEATHER_PREFIXES = (
    "commands.gwx.", "commands.wx.", "commands.rain.",
    "services.weather_service.", "common.alerts.", "common.wind_directions.",
    "common.date_time.", "common.temp_",
)


@pytest.mark.unit
@pytest.mark.parametrize("path", CATALOGS, ids=lambda p: p.stem)
def test_weather_placeholders_match_english(english, path):
    """A locale that drops or renames a {placeholder} raises at format() time."""
    mismatches = {}
    for key, value in load(path).items():
        if not key.startswith(WEATHER_PREFIXES):
            continue
        expected = english.get(key)
        if not isinstance(value, str) or not isinstance(expected, str):
            continue
        if set(re.findall(r"\{(\w+)\}", value)) != set(re.findall(r"\{(\w+)\}", expected)):
            mismatches[key] = (expected, value)
    assert mismatches == {}, f"{path.name} placeholder drift: {mismatches}"


@pytest.mark.unit
def test_alert_strings_live_under_common(english):
    """Shared by !wx alerts and WeatherService, so not under a service namespace."""
    for key in ("common.alerts.til", "common.alerts.by", "common.alerts.from",
                "common.alerts.am", "common.alerts.pm",
                "common.alerts.time_12h", "common.alerts.date_12h"):
        assert key in english, key
    for event_type in ("Warning", "Watch", "Advisory", "Statement"):
        assert f"common.alerts.event_types.{event_type}" in english


@pytest.mark.unit
def test_months_are_not_duplicated(english):
    """services.weather_service.months duplicated common.date_time.*."""
    assert not [k for k in english if k.startswith("services.weather_service.months.")]
    assert "common.date_time.month_abbreviations.Jan" in english
    assert "common.date_time.months.January" in english


@pytest.mark.unit
def test_wind_directions_live_under_common(english):
    """Read from a command module as well as the service."""
    assert "common.wind_directions.N" in english
    assert not [k for k in english if k.startswith("services.weather_service.wind_directions.")]
