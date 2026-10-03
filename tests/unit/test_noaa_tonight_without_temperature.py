"""A NOAA Tonight period without a temperature is left out instead of breaking the reply."""

import configparser
from unittest.mock import Mock

from modules.commands.wx_command import WxCommand


def _wx():
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": "noaa"}, "Wx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator = None
    wx = WxCommand(bot)
    wx.get_observation_data = Mock(return_value=None)
    return wx


def _period(name, temperature, short="Partly Cloudy"):
    return {
        "name": name,
        "temperature": temperature,
        "temperatureUnit": "F",
        "shortForecast": short,
        "windSpeed": "5 mph",
        "windDirection": "N",
        "detailedForecast": "",
    }


def _reply(periods):
    wx = _wx()
    points = Mock()
    points.json.return_value = {"properties": {"forecast": "https://api.weather.gov/gridpoints/SEW/1,1/forecast"}}
    forecast = Mock()
    forecast.json.return_value = {"properties": {"periods": periods}}
    wx._noaa_fetch = Mock(side_effect=[points, forecast])
    reply, _ = wx.get_noaa_weather(47.6, -122.3, max_length=200)
    return wx, reply


def test_tonight_without_temperature_does_not_fail_the_reply():
    wx, reply = _reply([
        _period("This Afternoon", 61),
        _period("Tonight", None),
        _period("Tomorrow", 63, "Sunny"),
    ])
    assert reply != wx.ERROR_FETCHING_DATA
    assert reply.startswith("This Afternoon:")
    assert "Tonight" not in reply


def test_tonight_without_temperature_does_not_repeat_today():
    _, reply = _reply([
        _period("Overnight", 48),
        _period("Today", 61, "Sunny"),
        _period("Tonight", None),
        _period("Tuesday", 63, "Cloudy"),
    ])
    assert reply.count("Today:") == 1
