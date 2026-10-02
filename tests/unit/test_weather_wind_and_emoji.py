"""gwx names the nearest compass point; NOAA conditions get the emoji for their strongest feature."""

import configparser
from unittest.mock import Mock

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.commands.wx_command import WxCommand


def _bot():
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": "noaa"}, "Wx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator.translate.side_effect = lambda key, **kw: key.rsplit(".", 1)[-1]
    return bot


@pytest.mark.parametrize(("degrees", "shown"), [
    (0, "⬆️N"), (10, "⬆️N"), (11.3, "↗️NNE"), (22.5, "↗️NNE"), (45, "↗️NE"),
    (90, "➡️E"), (202, "↙️SSW"), (241, "⬅️WSW"), (349, "⬆️N"), (350, "⬆️N"),
    (360, "⬆️N"), (-10, "⬆️N"), (720 + 90, "➡️E"),
])
def test_gwx_wind_direction_is_the_nearest_compass_point(degrees, shown):
    assert GlobalWxCommand(_bot())._degrees_to_direction(degrees) == shown


def test_gwx_and_wx_agree_on_arrows_for_the_same_compass_point():
    gwx = GlobalWxCommand(_bot())
    wx = WxCommand(_bot())
    for i, label in enumerate(["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                               "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]):
        assert gwx._degrees_to_direction(i * 22.5) == wx.abbreviate_wind_direction(label)


@pytest.mark.parametrize(("forecast", "emoji"), [
    ("Partly Cloudy", "⛅"),
    ("Mostly Cloudy", "⛅"),
    ("Cloudy", "☁️"),
    ("Chance Showers And Thunderstorms", "⛈️"),
    ("Showers And Thunderstorms Likely", "⛈️"),
    ("Mostly Cloudy then Chance T-storms", "⛈️"),
    ("Chance Rain Showers", "🌦️"),
    ("Heavy Rain", "🌧️"),
    ("Sunny", "☀️"),
    ("Sunny then Slight Chance Showers And Thunderstorms", "☀️"),
])
def test_noaa_emoji_matches_the_strongest_condition(forecast, emoji):
    assert WxCommand(_bot()).get_weather_emoji(forecast) == emoji
