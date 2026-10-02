"""wx reads highs, lows, gusts and precipitation chances from NOAA's own forecast wording."""

import configparser
from unittest.mock import Mock

import pytest

from modules.commands.wx_command import WxCommand


def _wx(weather=None):
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": "noaa", **(weather or {})}, "Wx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator = None
    return WxCommand(bot)


@pytest.mark.parametrize(("text", "units", "low"), [
    ("Mostly clear, with a low around -5. North wind around 5 mph.", "°F", "-5"),
    ("Clear and cold, with a low around 8.", "°F", "8"),
    ("Partly cloudy, with a low around 0.", "°F", "0"),
    ("Clear, with a low around -12. Wind chill values as low as -25.", "°C", "-12"),
])
def test_cold_lows_are_read(text, units, low):
    assert f"{low}{units}" in _wx().extract_high_low(text, units)


@pytest.mark.parametrize(("text", "high"), [
    ("Sunny and cold, with a high near -2.", "-2"),
    ("Mostly sunny, with a high near 14.", "14"),
])
def test_cold_highs_are_read(text, high):
    assert f"{high}°F" in _wx().extract_high_low(text, "°F")


def test_an_implausible_value_is_still_rejected():
    assert _wx().extract_high_low("with a low around -140.", "°F") == ""
    assert _wx().extract_high_low("with a high near 70.", "°C") == ""


def test_noaa_precipitation_sentence_is_read():
    text = "Showers likely, mainly after 11am. Cloudy, with a high near 58. Chance of precipitation is 60%."
    assert _wx().extract_precip_probability(text) == "60"


@pytest.mark.parametrize(("text", "wind_unit", "shown"), [
    ("Southwest wind 15 to 20 mph, with gusts as high as 35 mph.", "mph", "35"),
    ("Southwest wind 24 to 32 km/h, with gusts as high as 56 km/h.", "kmh", "56"),
])
def test_noaa_gusts_as_high_as_are_read(text, wind_unit, shown):
    assert _wx({"wind_speed_unit": wind_unit})._forecast_text_gusts(text) == shown
