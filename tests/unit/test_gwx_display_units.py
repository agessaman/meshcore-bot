#!/usr/bin/env python3
"""Unit tests for !gwx display units — driven by [Weather] config, not by locale."""

import pytest

from modules.commands.alternatives.wx_international import (
    HPA_TO_MMHG,
    MI_TO_KM,
    VISIBILITY_CAP_KM,
    VISIBILITY_CAP_MI,
    GlobalWxCommand,
)
from modules.i18n import Translator


@pytest.fixture
def gwx_bot(mock_bot):
    mock_bot.config.add_section("Weather")
    mock_bot.config.set("Weather", "weather_provider", "openmeteo")
    mock_bot.config.set("Weather", "default_country", "US")
    return mock_bot


def build(gwx_bot, *, temperature_unit="fahrenheit", language="en"):
    gwx_bot.config.set("Weather", "temperature_unit", temperature_unit)
    gwx_bot.translator = Translator(language)
    return GlobalWxCommand(gwx_bot)


@pytest.mark.unit
class TestMetricDistance:
    def test_fahrenheit_is_imperial(self, gwx_bot):
        assert build(gwx_bot, temperature_unit="fahrenheit").metric_distance is False

    def test_celsius_is_metric(self, gwx_bot):
        assert build(gwx_bot, temperature_unit="celsius").metric_distance is True

    def test_language_does_not_decide(self, gwx_bot):
        # A Fahrenheit bot answering in Russian must not print kilometers next
        # to Fahrenheit temperatures.
        cmd = build(gwx_bot, temperature_unit="fahrenheit", language="ru")
        assert cmd.metric_distance is False

    def test_en_gb_with_celsius_gets_metric(self, gwx_bot):
        cmd = build(gwx_bot, temperature_unit="celsius", language="en-GB")
        assert cmd.metric_distance is True

    def test_invalid_unit_falls_back_to_imperial(self, gwx_bot):
        cmd = build(gwx_bot, temperature_unit="kelvin")
        assert cmd.temperature_unit == "fahrenheit"
        assert cmd.metric_distance is False


@pytest.mark.unit
class TestVisibilityStrings:
    def test_imperial_string_says_miles_in_english(self):
        assert Translator("en").translate("commands.gwx.visibility", value=12) == "👁️12mi"

    def test_imperial_string_says_miles_in_russian(self):
        # This key means miles in every catalog now that the unit comes from
        # config; it used to say "км" in ru.
        out = Translator("ru").translate("commands.gwx.visibility", value=12)
        assert "км" not in out
        assert "миль" in out

    def test_metric_string_says_km(self):
        assert Translator("en").translate("commands.gwx.visibility_km", value=12) == "👁️12km"

    def test_caps_are_equivalent(self):
        assert pytest.approx(VISIBILITY_CAP_KM, abs=0.3) == VISIBILITY_CAP_MI * MI_TO_KM


@pytest.mark.unit
class TestPressureUnitConvention:
    @pytest.mark.parametrize("language", ["en", "en-GB", "de", "fr", "fr-CA",
                                          "es", "nl", "pl", "pt", "pt-BR"])
    def test_hpa_locales(self, language):
        assert Translator(language).translate("commands.gwx.pressure_unit") == "hpa"

    def test_russian_uses_mmhg(self):
        assert Translator("ru").translate("commands.gwx.pressure_unit") == "mmhg"

    @pytest.mark.parametrize("language", ["en", "de", "fr", "es", "nl", "pl", "pt"])
    def test_hpa_locales_render_no_cyrillic(self, language):
        out = Translator(language).translate("commands.gwx.pressure", value=1013)
        assert not any("Ѐ" <= ch <= "ӿ" for ch in out), out

    def test_english_mmhg_string_is_english(self):
        # The en catalog held "мм рт. ст.", which every non-ru locale inherited.
        out = Translator("en").translate("commands.gwx.pressure_mmhg", value=760)
        assert out == "📊760mmHg"

    def test_conversion_factor(self):
        assert round(1013 * HPA_TO_MMHG) == 760
