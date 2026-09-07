#!/usr/bin/env python3
"""Unit tests for WeatherService's localized daily-forecast pieces.

Covers the presentation helpers the localization pass rewrote: an unmapped WMO
code, a wind-speed unit the operator wrote in unexpected casing, and the
compass direction lookup. All three used to render a translation key path into
a channel broadcast when the lookup missed.
"""

import configparser
from unittest.mock import Mock

import pytest

from modules.i18n import Translator
from modules.service_plugins.weather_service import WeatherService

pytestmark = pytest.mark.unit


def build_service(*, language="en", weather_overrides=None):
    cfg = configparser.ConfigParser()
    cfg.add_section("Weather")
    cfg.add_section("Weather_Service")
    cfg.set("Weather_Service", "my_position_lat", "47.6")
    cfg.set("Weather_Service", "my_position_lon", "-122.3")
    for key, value in (weather_overrides or {}).items():
        cfg.set("Weather", key, value)

    bot = Mock()
    bot.logger = Mock()
    bot.config = cfg
    bot.db_manager = Mock()
    bot.translator = Translator(language, "translations/")

    service = WeatherService(bot)
    service.api_session = Mock()
    return service


class TestUnitConfigNormalization:
    def test_casing_is_normalized(self):
        service = build_service(weather_overrides={"wind_speed_unit": "KMH"})
        assert service.wind_speed_unit == "kmh"

    def test_unknown_unit_falls_back(self):
        service = build_service(weather_overrides={"wind_speed_unit": "furlongs/fortnight"})
        assert service.wind_speed_unit == "mph"

    def test_temperature_and_precipitation_normalized(self):
        service = build_service(weather_overrides={
            "temperature_unit": "Celsius", "precipitation_unit": "MM",
        })
        assert service.temperature_unit == "celsius"
        assert service.precipitation_unit == "mm"

    @pytest.mark.parametrize(("unit", "label"),
                             [("mph", "mph"), ("kmh", "km/h"), ("ms", "m/s"), ("kn", "kn")])
    def test_every_valid_unit_has_a_label(self, unit, label):
        service = build_service(weather_overrides={"wind_speed_unit": unit})
        from modules import alert_format as af
        rendered = af.translate_or(
            service.bot.translator,
            f"services.weather_service.wind_speed_units.{service.wind_speed_unit}",
            service.wind_speed_unit,
        )
        assert rendered == label

    def test_odd_casing_never_renders_a_key_path(self):
        service = build_service(weather_overrides={"wind_speed_unit": "KMH"})
        from modules import alert_format as af
        rendered = af.translate_or(
            service.bot.translator,
            f"services.weather_service.wind_speed_units.{service.wind_speed_unit}",
            service.wind_speed_unit,
        )
        assert "services.weather_service" not in rendered


class TestWeatherDescription:
    def test_mapped_code(self):
        assert build_service()._get_weather_description(0) == "Clear"

    def test_mapped_code_localized(self):
        assert build_service(language="ru")._get_weather_description(0) == "Ясно"

    @pytest.mark.parametrize("code", [4, 7, 123, 999])
    def test_unmapped_code_says_unknown(self, code):
        assert build_service()._get_weather_description(code) == "Unknown"

    def test_unmapped_code_localized(self):
        assert build_service(language="ru")._get_weather_description(4) == "Неизвестно"

    @pytest.mark.parametrize("code", [4, 999])
    def test_unmapped_code_never_renders_a_key_path(self, code):
        for language in ("en", "ru", "de"):
            out = build_service(language=language)._get_weather_description(code)
            assert "weather_descriptions" not in out, out


class TestWindDirection:
    @pytest.mark.parametrize(("degrees", "expected"),
                             [(0, "N"), (45, "NE"), (90, "E"), (180, "S"), (270, "W"), (359, "N")])
    def test_english_compass(self, degrees, expected):
        assert build_service()._degrees_to_direction(degrees) == expected

    def test_localized_compass(self):
        assert build_service(language="ru")._degrees_to_direction(292.5) == "ЗСЗ"

    def test_none_is_empty(self):
        assert build_service()._degrees_to_direction(None) == ""

    def test_never_renders_a_key_path(self):
        service = build_service(language="de")
        for degrees in range(0, 360, 15):
            assert "wind_directions" not in service._degrees_to_direction(degrees)


class TestAlertFormattingIsShared:
    @pytest.mark.asyncio
    async def test_matches_the_shared_helper(self):
        from modules import alert_format as af

        service = build_service()
        alert = {
            "event": "Flood Warning", "event_type": "Warning", "severity": "Severe",
            "expires": "2026-06-28T18:00:00-07:00", "office": "NWS Seattle WA",
            "area_desc": "King County", "link": "",
        }
        assert await service._format_alert_compact(alert) == af.format_alert_compact(
            alert, service.bot.translator
        )

    @pytest.mark.asyncio
    async def test_unknown_event_type_never_renders_a_key_path(self):
        service = build_service(language="ru")
        alert = {
            "event": "Hazardous", "event_type": "Unknown", "severity": "Minor",
            "expires": "", "office": "", "area_desc": "", "link": "",
        }
        assert await service._format_alert_compact(alert) == "⚪Hazardous Unknown"
