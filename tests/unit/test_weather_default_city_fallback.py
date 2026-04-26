#!/usr/bin/env python3
"""Unit tests for default_city fallback ordering in wx and gwx commands."""

import asyncio
import configparser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.commands.wx_command import WxCommand


def _build_config(default_city: str = "", use_bot: bool = False) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.set("Weather", "weather_provider", "noaa")
    config.set("Weather", "default_city", default_city)
    config.set("Weather", "default_state", "WA")
    config.set("Weather", "default_country", "US")
    config.add_section("Wx_Command")
    config.set(
        "Wx_Command",
        "use_bot_location_when_no_location",
        "true" if use_bot else "false",
    )
    config.add_section("Bot")
    config.set("Bot", "bot_tx_rate_limit_seconds", "1.0")
    return config


def _build_bot(config: configparser.ConfigParser, mock_logger):
    bot = Mock()
    bot.logger = mock_logger
    bot.config = config
    bot.db_manager = Mock()
    bot.translator = Mock()
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    bot.command_manager = Mock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    return bot


def _mock_message(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        sender_id="user1",
        sender_pubkey="pubkey1",
        channel="general",
        is_dm=False,
    )


def _stub_shared_paths(cmd):
    cmd._get_custom_mqtt_weather_topic = Mock(return_value=None)
    cmd._get_custom_wxsim_source = Mock(return_value=None)
    cmd.send_response = AsyncMock(return_value=True)
    cmd.record_execution = Mock()
    cmd.translate = Mock(side_effect=lambda key, **kwargs: key)


def test_gwx_empty_prefers_companion_over_default_city(mock_logger):
    cmd = GlobalWxCommand(_build_bot(_build_config(default_city="Seattle"), mock_logger))
    _stub_shared_paths(cmd)
    cmd._get_companion_location = Mock(return_value=(1, 2))
    cmd._coordinates_to_location_string = Mock(return_value=None)
    cmd.get_weather_for_location = AsyncMock(return_value="ok")

    asyncio.run(cmd.execute(_mock_message("gwx")))

    assert cmd.get_weather_for_location.await_count == 1
    assert cmd.get_weather_for_location.await_args.args[0] == "1,2"


def test_gwx_empty_uses_default_city_when_no_companion(mock_logger):
    cmd = GlobalWxCommand(_build_bot(_build_config(default_city="Seattle"), mock_logger))
    _stub_shared_paths(cmd)
    cmd._get_companion_location = Mock(return_value=None)
    cmd.get_weather_for_location = AsyncMock(return_value="ok")

    asyncio.run(cmd.execute(_mock_message("gwx")))

    assert cmd.get_weather_for_location.await_count == 1
    assert cmd.get_weather_for_location.await_args.args[0] == "Seattle, WA, US"


def test_gwx_empty_falls_back_to_bot_location_when_default_city_missing(mock_logger):
    cmd = GlobalWxCommand(_build_bot(_build_config(default_city="", use_bot=True), mock_logger))
    _stub_shared_paths(cmd)
    cmd._get_companion_location = Mock(return_value=None)
    cmd._get_bot_location = Mock(return_value=(47, -122))
    cmd._coordinates_to_location_string = Mock(return_value=None)
    cmd.get_weather_for_location = AsyncMock(return_value="ok")

    asyncio.run(cmd.execute(_mock_message("gwx")))

    assert cmd.get_weather_for_location.await_count == 1
    assert cmd.get_weather_for_location.await_args.args[0] == "47,-122"


def test_gwx_empty_shows_usage_without_any_fallback(mock_logger):
    cmd = GlobalWxCommand(_build_bot(_build_config(default_city="", use_bot=False), mock_logger))
    _stub_shared_paths(cmd)
    cmd._get_companion_location = Mock(return_value=None)
    cmd.get_weather_for_location = AsyncMock(return_value="ok")

    asyncio.run(cmd.execute(_mock_message("gwx")))

    assert cmd.get_weather_for_location.await_count == 0
    assert cmd.send_response.await_count == 1
    assert cmd.send_response.await_args.args[1] == "commands.gwx.usage"


def test_wx_empty_prefers_companion_over_default_city(mock_logger):
    cmd = WxCommand(_build_bot(_build_config(default_city="Seattle"), mock_logger))
    _stub_shared_paths(cmd)
    cmd._get_companion_location = Mock(return_value=(1, 2))
    cmd._coordinates_to_location_string = Mock(return_value=None)
    cmd.get_weather_for_location = AsyncMock(return_value="ok")

    asyncio.run(cmd.execute(_mock_message("wx")))

    assert cmd.get_weather_for_location.await_count == 1
    assert cmd.get_weather_for_location.await_args.args[0] == "1,2"
    assert cmd.get_weather_for_location.await_args.kwargs["using_companion_location"] is True


def test_wx_empty_uses_default_city_when_no_companion(mock_logger):
    cmd = WxCommand(_build_bot(_build_config(default_city="Seattle"), mock_logger))
    _stub_shared_paths(cmd)
    cmd._get_companion_location = Mock(return_value=None)
    cmd.get_weather_for_location = AsyncMock(return_value="ok")

    asyncio.run(cmd.execute(_mock_message("wx")))

    assert cmd.get_weather_for_location.await_count == 1
    assert cmd.get_weather_for_location.await_args.args[0] == "Seattle, WA, US"
    assert cmd.get_weather_for_location.await_args.kwargs["using_companion_location"] is False


def test_wx_empty_falls_back_to_bot_location_when_default_city_missing(mock_logger):
    cmd = WxCommand(_build_bot(_build_config(default_city="", use_bot=True), mock_logger))
    _stub_shared_paths(cmd)
    cmd._get_companion_location = Mock(return_value=None)
    cmd._get_bot_location = Mock(return_value=(47, -122))
    cmd._coordinates_to_location_string = Mock(return_value=None)
    cmd.get_weather_for_location = AsyncMock(return_value="ok")

    asyncio.run(cmd.execute(_mock_message("wx")))

    assert cmd.get_weather_for_location.await_count == 1
    assert cmd.get_weather_for_location.await_args.args[0] == "47,-122"
    assert cmd.get_weather_for_location.await_args.kwargs["using_companion_location"] is False


def test_wx_empty_shows_usage_without_any_fallback(mock_logger):
    cmd = WxCommand(_build_bot(_build_config(default_city="", use_bot=False), mock_logger))
    _stub_shared_paths(cmd)
    cmd._get_companion_location = Mock(return_value=None)
    cmd.get_weather_for_location = AsyncMock(return_value="ok")

    asyncio.run(cmd.execute(_mock_message("wx")))

    assert cmd.get_weather_for_location.await_count == 0
    assert cmd.send_response.await_count == 1
    assert cmd.send_response.await_args.args[1] == "commands.wx.usage"
