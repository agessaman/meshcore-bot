"""wx's default reply keeps a Tomorrow period forecast at exactly 0°."""

import configparser
from unittest.mock import Mock

from modules.commands.wx_command import WxCommand


def _reply(periods):
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": "noaa"}, "Wx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator = None
    wx = WxCommand(bot)
    wx.get_observation_data = Mock(return_value=None)
    points = Mock()
    points.json.return_value = {"properties": {"forecast": "https://api.weather.gov/gridpoints/AFC/1,1/forecast"}}
    forecast = Mock()
    forecast.json.return_value = {"properties": {"periods": periods}}
    wx._noaa_fetch = Mock(side_effect=[points, forecast])
    return wx.get_noaa_weather(61.2, -149.9, max_length=200)[0]


def _period(name, temperature, short):
    return {"name": name, "temperature": temperature, "temperatureUnit": "F", "shortForecast": short,
            "windSpeed": "", "windDirection": "", "detailedForecast": ""}


def test_tomorrow_at_zero_degrees_is_shown():
    reply = _reply([
        _period("Tonight", -8, "Clear"),
        _period("Tomorrow", 0, "Sunny"),
    ])
    assert "Tomorrow" in reply
    assert "0°" in reply.split("Tomorrow", 1)[1]
