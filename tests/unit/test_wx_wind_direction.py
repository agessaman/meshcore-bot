"""wx renders NOAA's 16-point wind abbreviations with an arrow instead of truncating them."""

import configparser
from unittest.mock import Mock

import pytest

from modules.commands.wx_command import WxCommand


def _wx():
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": "noaa"}, "Wx_Command": {}})
    bot = Mock()
    bot.config = config
    return WxCommand(bot)


@pytest.mark.parametrize(
    ("noaa", "shown"),
    [
        ("N", "⬆️N"), ("NNE", "↗️NNE"), ("NE", "↗️NE"), ("ENE", "➡️ENE"),
        ("E", "➡️E"), ("ESE", "↘️ESE"), ("SE", "↘️SE"), ("SSE", "⬇️SSE"),
        ("S", "⬇️S"), ("SSW", "↙️SSW"), ("SW", "↙️SW"), ("WSW", "⬅️WSW"),
        ("W", "⬅️W"), ("WNW", "↖️WNW"), ("NW", "↖️NW"), ("NNW", "⬆️NNW"),
        ("wnw", "↖️WNW"),
    ],
)
def test_noaa_abbreviations_keep_their_letters_and_gain_an_arrow(noaa, shown):
    assert _wx().abbreviate_wind_direction(noaa) == shown


def test_full_words_and_unknown_values_are_unchanged():
    wx = _wx()
    assert wx.abbreviate_wind_direction("Northwest") == "↖️NW"
    assert wx.abbreviate_wind_direction("") == ""
    assert wx.abbreviate_wind_direction("calm") == "💨CA"
