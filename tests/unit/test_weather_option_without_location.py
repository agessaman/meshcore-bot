"""An option given alone ("wx hourly", "gwx 5d") applies to the location the command falls back to."""

import asyncio
import configparser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.commands.wx_command import WxCommand

CLASSES = [WxCommand, GlobalWxCommand]


def _cmd(cls, default_city=""):
    config = configparser.ConfigParser()
    config.read_dict({
        "Weather": {"weather_provider": "noaa", "default_city": default_city, "default_state": "", "default_country": ""},
        "Wx_Command": {},
        "Bot": {},
    })
    bot = Mock()
    bot.config = config
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    cmd = cls(bot)
    cmd._get_custom_mqtt_weather_topic = Mock(return_value=None)
    cmd._get_custom_wxsim_source = Mock(return_value=None)
    cmd._get_companion_location = Mock(return_value=None)
    cmd._coordinates_to_location_string_async = AsyncMock(return_value=None)
    cmd.send_response = AsyncMock(return_value=True)
    cmd.record_execution = Mock()
    cmd.get_weather_for_location = AsyncMock(return_value="ok")
    cmd._send_multiday_forecast = AsyncMock(return_value=True)
    return cmd


def _say(cmd, text):
    message = SimpleNamespace(content=text, sender_id="u", sender_pubkey="pk", channel="general", is_dm=False)
    return asyncio.run(cmd.execute(message))


@pytest.mark.parametrize("cls", CLASSES)
@pytest.mark.parametrize(("option", "forecast_type", "days"), [
    ("hourly", "hourly", 7), ("tomorrow", "tomorrow", 7), ("5d", "multiday", 5), ("3", "multiday", 3),
])
def test_an_option_alone_uses_the_senders_position(cls, option, forecast_type, days):
    cmd = _cmd(cls)
    cmd._get_companion_location = Mock(return_value=(47.6062, -122.3321))
    assert _say(cmd, f"{cmd.keywords[0]} {option}")
    args = cmd.get_weather_for_location.await_args.args
    assert args[0] == "47.60620,-122.33210"
    assert forecast_type in args and (forecast_type != "multiday" or days in args)


@pytest.mark.parametrize("cls", CLASSES)
def test_an_option_alone_uses_the_default_city(cls):
    cmd = _cmd(cls, default_city="Seattle")
    assert _say(cmd, f"{cmd.keywords[0]} tomorrow")
    args = cmd.get_weather_for_location.await_args.args
    assert args[0] == "Seattle" and "tomorrow" in args


@pytest.mark.parametrize("cls", CLASSES)
def test_a_zip_code_is_still_a_location(cls):
    cmd = _cmd(cls)
    _say(cmd, f"{cmd.keywords[0]} 98101")
    assert cmd.get_weather_for_location.await_args.args[0] == "98101"


@pytest.mark.parametrize("cls", CLASSES)
def test_the_default_wxsim_source_answers_the_option(cls):
    cmd = _cmd(cls)
    cmd._get_custom_wxsim_source = Mock(side_effect=lambda name=None: None if name else "https://example.test/wxsim")
    cmd._get_wxsim_weather = Mock(return_value="Tomorrow: sunny")
    cmd._get_wxsim_weather_async = AsyncMock(return_value="Tomorrow: sunny")
    assert _say(cmd, f"{cmd.keywords[0]} tomorrow")
    fetch = cmd._get_wxsim_weather_async if cls is WxCommand else cmd._get_wxsim_weather
    call = fetch.await_args if cls is WxCommand else fetch.call_args
    assert call.args[1:3] == ("tomorrow", 7)
    cmd.send_response.assert_awaited_once()


@pytest.mark.parametrize("cls", CLASSES)
@pytest.mark.parametrize("source", ["mqtt", "wxsim"])
def test_a_refused_send_from_a_default_source_is_reported(cls, source):
    cmd = _cmd(cls)
    if source == "mqtt":
        cmd._get_custom_mqtt_weather_topic = Mock(return_value="weather/station")
        cmd._mqtt_weather_line = Mock(return_value="12°C")
    else:
        cmd._get_custom_wxsim_source = Mock(side_effect=lambda name=None: None if name else "https://example.test/wxsim")
        cmd._get_wxsim_weather = Mock(return_value="12°C")
        cmd._get_wxsim_weather_async = AsyncMock(return_value="12°C")
    cmd.send_response = AsyncMock(return_value=False)
    assert _say(cmd, cmd.keywords[0]) is False


@pytest.mark.parametrize("cls", CLASSES)
def test_alerts_alone_uses_the_fallback_location(cls):
    cmd = _cmd(cls, default_city="Seattle")
    cmd._send_full_alert_list = AsyncMock(return_value=True)
    cmd.city_to_lat_lon = Mock(return_value=(47.6, -122.3, {}))
    _say(cmd, f"{cmd.keywords[0]} alerts")
    if cls is WxCommand:
        cmd._send_full_alert_list.assert_awaited_once()
        cmd.city_to_lat_lon.assert_called_once_with("Seattle")
    else:  # Open-Meteo has no alerts
        cmd.send_response.assert_awaited_once()
        assert cmd.send_response.await_args.args[1] == "commands.gwx.source_option_not_available"
    cmd.get_weather_for_location.assert_not_awaited()


@pytest.mark.parametrize("cls", CLASSES)
def test_a_custom_source_named_like_an_option_is_that_source(cls):
    cmd = _cmd(cls)
    cmd._get_custom_mqtt_weather_topic = Mock(side_effect=lambda name=None: {"hourly": "weather/hourly", None: "weather/default"}.get(name))
    cmd._mqtt_weather_line = Mock(return_value="12°C")
    _say(cmd, f"{cmd.keywords[0]} hourly")
    assert cmd._mqtt_weather_line.call_args.args[0] == "weather/hourly"


def test_a_refused_first_alert_message_ends_the_list():
    cmd = _cmd(WxCommand)
    cmd._get_weather_alerts_noaa_async = AsyncMock(return_value=([{"x": 1}] * 3, 3))
    cmd._format_alert_full = Mock(side_effect=lambda alert, index: "x" * 100)
    cmd.get_max_message_length = Mock(return_value=130)
    cmd.send_response = AsyncMock(return_value=False)
    message = SimpleNamespace(content="wx", sender_id="u", sender_pubkey="pk", channel="general", is_dm=False)
    assert asyncio.run(cmd._send_full_alert_list(message, 47.6, -122.3)) is False
    assert cmd.send_response.await_count == 1
