#!/usr/bin/env python3
"""Unit tests for lazy NWS alert coverage handling (international / HTTP 400)."""

import asyncio
import configparser
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from modules.commands.wx_command import WxCommand
from modules.service_plugins.weather_service import WeatherService

_MINIMAL_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:oid:2.49.test.alert</id>
    <title>Test Warning issued June 27 at 10:00AM until June 28 at 6:00AM by NWS Seattle WA</title>
    <updated>2026-06-27T10:00:00Z</updated>
  </entry>
</feed>"""


def _build_bot(mock_logger, config):
    bot = Mock()
    bot.logger = mock_logger
    bot.config = config
    bot.db_manager = Mock()
    bot.command_manager = Mock()
    bot.command_manager.send_channel_message = AsyncMock()
    return bot


def _weather_service(mock_logger, lat=51.5074, lon=-0.1278):
    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.add_section("Weather_Service")
    config.set("Weather_Service", "my_position_lat", str(lat))
    config.set("Weather_Service", "my_position_lon", str(lon))
    service = WeatherService(_build_bot(mock_logger, config))
    return service


def _wx_command(mock_logger):
    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.set("Weather", "weather_provider", "noaa")
    config.add_section("Wx_Command")
    bot = _build_bot(mock_logger, config)
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))
    bot.db_manager.cache_geocoding = Mock()
    return WxCommand(bot)


def _mock_response(*, ok=True, status_code=200, text=""):
    response = Mock()
    response.ok = ok
    response.status_code = status_code
    response.text = text
    return response


@pytest.mark.asyncio
async def test_weather_service_intl_400_skips_after_first_poll(mock_logger):
    service = _weather_service(mock_logger)
    call_count = 0

    def _fake_get(_url, timeout=0):
        nonlocal call_count
        call_count += 1
        return _mock_response(ok=False, status_code=400)

    service.api_session = Mock()
    service.api_session.get = _fake_get

    await service._check_weather_alerts()
    await service._check_weather_alerts()

    assert call_count == 1
    assert service._nws_alerts_available is False
    mock_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_weather_service_us_200_sets_available(mock_logger):
    service = _weather_service(mock_logger, lat=47.6062, lon=-122.3321)
    service.api_session = Mock()
    service.api_session.get = Mock(
        return_value=_mock_response(ok=True, status_code=200, text=_MINIMAL_ATOM)
    )

    await service._check_weather_alerts()

    assert service._nws_alerts_available is True
    service.api_session.get.assert_called_once()


@pytest.mark.asyncio
async def test_weather_service_timeout_retries_next_poll(mock_logger):
    service = _weather_service(mock_logger)
    call_count = 0

    def _fake_get(_url, timeout=0):
        nonlocal call_count
        call_count += 1
        raise requests.exceptions.Timeout("timed out")

    service.api_session = Mock()
    service.api_session.get = _fake_get

    await service._check_weather_alerts()
    await service._check_weather_alerts()

    assert call_count == 2
    assert service._nws_alerts_available is None


def test_wx_command_intl_400_skips_after_first_request(mock_logger):
    cmd = _wx_command(mock_logger)
    call_count = 0

    def _fake_get(_url, timeout=0):
        nonlocal call_count
        call_count += 1
        return _mock_response(ok=False, status_code=400)

    cmd.noaa_session = Mock()
    cmd.noaa_session.get = _fake_get

    assert cmd.get_weather_alerts_noaa(51.5074, -0.1278) == cmd.ERROR_FETCHING_DATA
    assert cmd.get_weather_alerts_noaa(51.5074, -0.1278) == cmd.ERROR_FETCHING_DATA

    assert call_count == 1
    assert cmd._nws_alerts_available is False
    mock_logger.warning.assert_called_once()


def test_wx_command_us_200_sets_available(mock_logger):
    cmd = _wx_command(mock_logger)
    cmd.noaa_session = Mock()
    cmd.noaa_session.get = Mock(
        return_value=_mock_response(ok=True, status_code=200, text=_MINIMAL_ATOM)
    )

    result = cmd.get_weather_alerts_noaa(47.6062, -122.3321, return_full_data=True)

    assert isinstance(result, tuple)
    assert cmd._nws_alerts_available is True
    cmd.noaa_session.get.assert_called_once()


def test_wx_command_timeout_retries_next_request(mock_logger):
    cmd = _wx_command(mock_logger)
    call_count = 0

    def _fake_get(_url, timeout=0):
        nonlocal call_count
        call_count += 1
        raise requests.exceptions.ConnectionError("connection reset")

    cmd.noaa_session = Mock()
    cmd.noaa_session.get = _fake_get

    assert cmd.get_weather_alerts_noaa(51.5074, -0.1278) == cmd.ERROR_FETCHING_DATA
    assert cmd.get_weather_alerts_noaa(51.5074, -0.1278) == cmd.ERROR_FETCHING_DATA

    assert call_count == 2
    assert cmd._nws_alerts_available is None
