"""Every weather translation key the code can build exists in ``en.json``.

The weather code builds keys three ways: literal ``translate('...')`` calls, a
namespace prefix taken from the provider (``commands.wx`` or ``commands.gwx``),
and a table lookup (periods, weather descriptions, wind directions, wind speed
units). A key that is missing renders as its own dotted path in a reply, so each
way is checked here against the English catalog, which is every other locale's
fallback.

The other tests cover the catalogs: the keys this branch added must exist in
every one, every weather key must resolve in every non-English locale through
its own file or its base language, and every translation must keep the
placeholders of the English string it replaces.
"""

import json
import re
from pathlib import Path

import pytest

from modules.weather_common import _COMPASS_16
from modules.weather_providers import ALERT_PROVIDERS, PROVIDERS
from modules.weather_providers.eccc import ICON_WMO, KEYWORD_WMO, EcccProvider
from modules.weather_providers.metno import (
    SYMBOL_TO_WMO,
    WMO_DESCRIPTIONS,
    MetNorwayProvider,
)
from modules.weather_providers.openmeteo import OpenMeteoProvider

ROOT = Path(__file__).resolve().parents[2]
TRANSLATIONS = ROOT / "translations"
CATALOGS = sorted(TRANSLATIONS.glob("*.json"))
NON_ENGLISH = [path for path in CATALOGS if path.stem != "en"]

WEATHER_SOURCES = [
    *sorted((ROOT / "modules" / "weather_providers").glob("*.py")),
    ROOT / "modules" / "commands" / "wx_command.py",
    ROOT / "modules" / "commands" / "alternatives" / "wx_international.py",
    ROOT / "modules" / "weather_common.py",
]

# The catalogs the weather commands can speak: one per provider.
NAMESPACES = sorted({
    cls.translation_ns
    for cls in (*PROVIDERS.values(), *ALERT_PROVIDERS.values())
})
GLOBAL_NAMESPACES = sorted({
    cls.translation_ns for cls in PROVIDERS.values() if cls.location_style == "global"
})
US_NAMESPACES = sorted({
    cls.translation_ns for cls in PROVIDERS.values() if cls.location_style == "us"
})

# A key built from the provider's namespace is only reachable in the location
# style that builds it: the global path asks for help and no_location, the US
# path for the zip code and city variants. Expanding both over every namespace
# would demand keys no code path can ask for.
GLOBAL_ONLY_SUFFIXES = {"help", "no_location"}
US_ONLY_SUFFIXES = {"no_location_city", "no_location_zipcode"}

# WeatherUnits.wind_speed, the values the unit setting accepts.
WIND_SPEED_UNITS = ("mph", "kmh", "ms", "kn")

# Keys added on this branch. They are new, so every catalog has to carry them.
BRANCH_KEYS = {
    "commands.gwx.calm",
    "commands.gwx.eccc.error_fetching_api",
    "commands.gwx.metno.error_fetching_api",
    "commands.wx.dwd.attribution",
    "commands.wx.dwd.error_fetching",
    "commands.wx.eccc.error_fetching",
    "commands.wx.meteoalarm.attribution",
    "commands.wx.meteoalarm.error_fetching",
}

TRANSLATE_CALL = re.compile(r"translate\(\s*(f?)(['\"])([^'\"\n]+)\2")


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


def literal_keys():
    """The keys the weather code passes to translate() as a literal."""
    keys = set()
    for path in WEATHER_SOURCES:
        for match in TRANSLATE_CALL.finditer(path.read_text(encoding="utf-8")):
            key = match.group(3)
            if "{ns}" in key:
                keys.update(key.replace("{ns}", ns) for ns in NAMESPACES)
            elif "{self.provider.translation_ns}" in key or "{self.translation_ns}" in key:
                suffix = key.split("}.", 1)[1]
                if suffix in GLOBAL_ONLY_SUFFIXES:
                    namespaces = GLOBAL_NAMESPACES
                elif suffix in US_ONLY_SUFFIXES:
                    namespaces = US_NAMESPACES
                else:
                    namespaces = NAMESPACES
                keys.update(f"{ns}.{suffix}" for ns in namespaces)
            elif "{" in key:
                # Expanded by the table tests below.
                continue
            else:
                keys.add(key)
    return keys


def period_keys():
    """Every period label the three providers' period functions can return."""
    keys = {"tomorrow"}  # the literal commands.gwx.periods.tomorrow calls
    eccc = object.__new__(EcccProvider)
    metno = object.__new__(MetNorwayProvider)
    openmeteo = object.__new__(OpenMeteoProvider)
    for hour in range(24):
        for has_high in (True, False):
            keys.add(eccc._period_key(hour, has_high))
        keys.add(metno._period_key(hour))
        keys.add(openmeteo._open_meteo_period_key({"time": f"2026-11-27T{hour:02d}:00"}))
    return keys


def wmo_codes():
    """Every WMO code the three providers' description tables can ask for."""
    codes = set(ICON_WMO.values()) | {wmo for _keyword, wmo in KEYWORD_WMO}
    codes |= set(SYMBOL_TO_WMO.values()) | set(WMO_DESCRIPTIONS)
    source = (ROOT / "modules" / "weather_providers" / "openmeteo.py").read_text(encoding="utf-8")
    table = source.split("weather_codes = {", 1)[1].split("}", 1)[0]
    codes |= {int(code) for code in re.findall(r"^\s*(\d+):", table, re.MULTILINE)}
    return codes


@pytest.mark.unit
def test_every_weather_key_the_code_builds_exists_in_english(english):
    keys = set(literal_keys())
    keys |= {f"commands.gwx.periods.{key}" for key in period_keys()}
    keys |= {f"commands.gwx.weather_descriptions.{code}" for code in wmo_codes()}
    keys |= {f"common.wind_directions.{key}" for key in _COMPASS_16}
    keys |= {f"services.weather_service.wind_speed_units.{unit}" for unit in WIND_SPEED_UNITS}

    missing = sorted(key for key in keys if key not in english)

    assert missing == [], f"weather keys missing from en.json: {missing}"


@pytest.mark.unit
def test_the_branch_keys_are_in_every_catalog(english):
    assert set(english) >= BRANCH_KEYS, sorted(BRANCH_KEYS - set(english))

    missing = {
        path.stem: sorted(BRANCH_KEYS - set(load(path)))
        for path in NON_ENGLISH
        if BRANCH_KEYS - set(load(path))
    }

    assert missing == {}, f"branch keys missing from catalogs: {missing}"


# The translator loads the base language first and lets the regional file
# override it (modules/i18n.py), so a regional catalog may leave a key to its
# base. en-GB is exempt: its base is English, which is the fallback anyway.
BASE_LANGUAGE = {"fr-CA": "fr", "pt-BR": "pt"}
ENGLISH_BASE = {"en-GB"}

PLACEHOLDER = re.compile(r"\{[^{}]+\}")


def placeholders(value):
    """Every {placeholder} in a catalog value, lists and dicts included."""
    if isinstance(value, str):
        return set(PLACEHOLDER.findall(value))
    if isinstance(value, list):
        return set().union(*(placeholders(item) for item in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(placeholders(item) for item in value.values())) if value else set()
    return set()


def weather_keys(english):
    """The keys of the weather namespaces, which every locale has to speak."""
    return {
        key for key in english
        if key.startswith((
            "commands.gwx", "commands.wx", "services.weather_service",
            "common.wind_directions", "common.alerts", "common.temp_",
        ))
    }


@pytest.mark.unit
def test_every_weather_key_resolves_without_falling_back_to_english(english):
    keys = weather_keys(english)
    catalogs = {path.stem: load(path) for path in NON_ENGLISH}
    unresolved = {}
    for language, catalog in catalogs.items():
        if language in ENGLISH_BASE:
            continue
        base = catalogs.get(BASE_LANGUAGE.get(language, ""), {})
        missing = sorted(key for key in keys if key not in catalog and key not in base)
        if missing:
            unresolved[language] = missing

    assert unresolved == {}, f"weather keys falling back to English: {unresolved}"


@pytest.mark.unit
def test_every_translation_keeps_the_english_placeholders(english):
    keys = weather_keys(english)
    catalogs = {path.stem: load(path) for path in NON_ENGLISH}
    catalogs["en"] = english
    mismatched = {}
    for language, catalog in catalogs.items():
        for key in keys:
            if key not in catalog:
                continue
            expected = placeholders(english[key])
            found = placeholders(catalog[key])
            if found != expected:
                mismatched[f"{language}:{key}"] = (sorted(expected), sorted(found))

    assert mismatched == {}, f"placeholders changed in translation: {mismatched}"
