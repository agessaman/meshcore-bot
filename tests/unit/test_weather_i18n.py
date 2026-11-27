"""Every weather translation key the code can build exists in ``en.json``.

The weather code builds keys three ways: literal ``translate('...')`` calls, a
namespace prefix taken from the provider (``commands.wx`` or ``commands.gwx``),
and a table lookup (periods, weather descriptions, wind directions, wind speed
units). A key that is missing renders as its own dotted path in a reply, so each
way is checked here against the English catalog, which is every other locale's
fallback.

The second test freezes the keys this branch added: they must exist in every
catalog. The older weather keys that were already missing from the other
catalogs on dev are listed as a known gap, so a new gap fails and an old one
does not.
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

# Weather keys that were already missing from at least one catalog on dev, when
# this branch started. They are a known gap, not a regression: the test below
# fails on a key outside this set, so new gaps are caught and these are not.
KNOWN_GAPS = {
    "commands.gwx.dew_point",
    "commands.gwx.gust",
    "commands.gwx.humidity",
    "commands.gwx.pressure",
    "commands.gwx.pressure_mmhg",
    "commands.gwx.pressure_unit",
    "commands.gwx.visibility",
    "commands.gwx.visibility_km",
    "commands.wx.subcommands",
    "common.alerts.am",
    "common.alerts.by",
    "common.alerts.date_12h",
    "common.alerts.event_types.Advisory",
    "common.alerts.event_types.Statement",
    "common.alerts.event_types.Warning",
    "common.alerts.event_types.Watch",
    "common.alerts.from",
    "common.alerts.pm",
    "common.alerts.til",
    "common.alerts.time_12h",
    "common.temp_high_label",
    "common.temp_low_label",
    "common.wind_directions.E",
    "common.wind_directions.ENE",
    "common.wind_directions.ESE",
    "common.wind_directions.N",
    "common.wind_directions.NE",
    "common.wind_directions.NNE",
    "common.wind_directions.NNW",
    "common.wind_directions.NW",
    "common.wind_directions.S",
    "common.wind_directions.SE",
    "common.wind_directions.SSE",
    "common.wind_directions.SSW",
    "common.wind_directions.SW",
    "common.wind_directions.W",
    "common.wind_directions.WNW",
    "common.wind_directions.WSW",
    "services.weather_service.daily_weather",
    "services.weather_service.error_fetching",
    "services.weather_service.est",
    "services.weather_service.near",
    "services.weather_service.no_data",
    "services.weather_service.rain_ending",
    "services.weather_service.rain_starting",
    "services.weather_service.steady",
    "services.weather_service.tomorrow",
    "services.weather_service.weather_descriptions.0",
    "services.weather_service.weather_descriptions.1",
    "services.weather_service.weather_descriptions.2",
    "services.weather_service.weather_descriptions.3",
    "services.weather_service.weather_descriptions.45",
    "services.weather_service.weather_descriptions.48",
    "services.weather_service.weather_descriptions.51",
    "services.weather_service.weather_descriptions.53",
    "services.weather_service.weather_descriptions.55",
    "services.weather_service.weather_descriptions.56",
    "services.weather_service.weather_descriptions.57",
    "services.weather_service.weather_descriptions.61",
    "services.weather_service.weather_descriptions.63",
    "services.weather_service.weather_descriptions.65",
    "services.weather_service.weather_descriptions.66",
    "services.weather_service.weather_descriptions.67",
    "services.weather_service.weather_descriptions.71",
    "services.weather_service.weather_descriptions.73",
    "services.weather_service.weather_descriptions.75",
    "services.weather_service.weather_descriptions.77",
    "services.weather_service.weather_descriptions.80",
    "services.weather_service.weather_descriptions.81",
    "services.weather_service.weather_descriptions.82",
    "services.weather_service.weather_descriptions.85",
    "services.weather_service.weather_descriptions.86",
    "services.weather_service.weather_descriptions.95",
    "services.weather_service.weather_descriptions.96",
    "services.weather_service.weather_descriptions.97",
    "services.weather_service.weather_descriptions.99",
    "services.weather_service.wind_speed_units.kmh",
    "services.weather_service.wind_speed_units.kn",
    "services.weather_service.wind_speed_units.mph",
    "services.weather_service.wind_speed_units.ms",
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


@pytest.mark.unit
def test_no_new_weather_key_is_missing_from_a_catalog(english):
    weather = {
        key for key in english
        if key.startswith((
            "commands.gwx", "commands.wx", "services.weather_service",
            "common.wind_directions", "common.alerts", "common.temp_",
        ))
    }
    gaps = set()
    for path in NON_ENGLISH:
        catalog = load(path)
        gaps |= {key for key in weather if key not in catalog}

    assert gaps - KNOWN_GAPS == set(), (
        f"new weather keys missing from a catalog: {sorted(gaps - KNOWN_GAPS)}"
    )
