"""gwx keeps working when Open-Meteo pads the days past a model's horizon with nulls."""

import configparser
from unittest.mock import Mock, patch

from modules.commands.alternatives.wx_international import GlobalWxCommand


def _gwx():
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"temperature_unit": "celsius"}, "Gwx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator.translate.side_effect = lambda key, **kwargs: f"<{key}{sorted(kwargs.items()) or ''}>"
    cmd = GlobalWxCommand(bot)
    cmd.logger = Mock()
    return cmd


def _daily(days, null_from):
    def series(value):
        return [value if i < null_from else None for i in range(days)]

    return {
        "daily": {
            "weather_code": series(3),
            "temperature_2m_max": series(20.4),
            "temperature_2m_min": series(11.6),
            "precipitation_sum": series(0.0),
            "precipitation_probability_max": series(10),
            "wind_speed_10m_max": series(12.0),
            "wind_gusts_10m_max": series(30.0),
        }
    }


def test_multiday_stops_at_the_first_null_day():
    cmd = _gwx()
    text = cmd.format_multiday_forecast(_daily(16, null_from=7), num_days=15)
    assert len(text.split("\n")) == 6  # days 1-6; today (day 0) is never listed
    assert "multiday_error" not in text
    cmd.logger.error.assert_not_called()


def test_multiday_with_no_usable_day_is_not_available():
    cmd = _gwx()
    text = cmd.format_multiday_forecast(_daily(16, null_from=1), num_days=7)
    assert text.startswith("<commands.gwx.multiday_not_available")
    cmd.logger.error.assert_not_called()


def test_tomorrow_with_null_temperatures_is_not_available():
    cmd = _gwx()
    assert cmd.format_tomorrow_forecast(_daily(16, null_from=1)) == "<commands.gwx.tomorrow_not_available>"
    cmd.logger.error.assert_not_called()


def test_null_precipitation_probability_is_left_out():
    cmd = _gwx()
    data = _daily(16, null_from=16)
    data["daily"]["precipitation_probability_max"] = [None] * 16
    text = cmd.format_tomorrow_forecast(data)
    assert "tomorrow_error" not in text and "🌦️" not in text
    cmd.logger.error.assert_not_called()


def test_default_reply_with_null_precipitation_probability_still_shows_tomorrow():
    cmd = _gwx()
    cmd.get_max_message_length = Mock(return_value=600)
    data = _daily(16, null_from=16)
    data["daily"]["precipitation_probability_max"] = [None] * 16
    data["current"] = {"temperature_2m": 18.0, "relative_humidity_2m": 70, "weather_code": 3}
    response = Mock(ok=True)
    response.json.return_value = data
    with patch("modules.commands.alternatives.wx_international.requests.get", return_value=response):
        text = cmd.get_open_meteo_weather(51.5, -0.13, message=Mock())
    assert "<commands.gwx.periods.tomorrow>" in text
    cmd.logger.error.assert_not_called()
