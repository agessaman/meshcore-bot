#!/usr/bin/env python3
"""Unit tests for lazy NWS alert coverage handling (international / HTTP 400)."""

import configparser
from unittest.mock import AsyncMock, Mock, call

import pytest
import requests

from modules import nws_coverage
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
    assert service._nws_no_coverage.is_unavailable(51.5074, -0.1278)
    mock_logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_weather_service_us_200_does_not_cache_no_coverage(mock_logger):
    service = _weather_service(mock_logger, lat=47.6062, lon=-122.3321)
    service.api_session = Mock()
    service.api_session.get = Mock(
        return_value=_mock_response(ok=True, status_code=200, text=_MINIMAL_ATOM)
    )

    await service._check_weather_alerts()

    assert not service._nws_no_coverage._points
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
    assert not service._nws_no_coverage._points


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
    assert cmd._nws_no_coverage.is_unavailable(51.5074, -0.1278)
    mock_logger.warning.assert_called_once()


def test_wx_command_us_200_does_not_cache_no_coverage(mock_logger):
    cmd = _wx_command(mock_logger)
    cmd.noaa_session = Mock()
    cmd.noaa_session.get = Mock(
        return_value=_mock_response(ok=True, status_code=200, text=_MINIMAL_ATOM)
    )

    result = cmd.get_weather_alerts_noaa(47.6062, -122.3321, return_full_data=True)

    assert isinstance(result, tuple)
    assert not cmd._nws_no_coverage._points
    cmd.noaa_session.get.assert_called_once()


@pytest.fixture(params=["wx", "service"])
def alert_client(request, mock_logger):
    if request.param == "wx":
        client = _wx_command(mock_logger)
        client.noaa_session = Mock()
        get = client.noaa_session.get

        async def fetch(lat, lon):
            return client.get_weather_alerts_noaa(lat, lon)
    else:
        client = _weather_service(mock_logger)
        client.api_session = Mock()
        get = client.api_session.get

        async def fetch(lat, lon):
            client.my_position_lat, client.my_position_lon = lat, lon
            return await client._check_weather_alerts()

    return client, fetch, get


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404])
async def test_no_coverage_only_suppresses_same_rounded_point(alert_client, mock_logger, status):
    _client, fetch, get = alert_client
    get.side_effect = [
        _mock_response(ok=False, status_code=status),
        _mock_response(text="<feed />"),
    ]

    await fetch(51.5074, -0.1278)
    await fetch(47.6062, -122.3321)
    await fetch(51.508, -0.128)

    assert get.call_count == 2
    assert [args[0] for args, _kwargs in get.call_args_list] == [
        "https://api.weather.gov/alerts/active.atom?point=51.5074,-0.1278",
        "https://api.weather.gov/alerts/active.atom?point=47.6062,-122.3321",
    ]
    mock_logger.warning.assert_called_once_with(
        "NWS weather alerts unavailable (HTTP %s); NOAA alerts are US-only; "
        "point %s,%s is outside NWS coverage",
        status, 51.51, -0.13,
    )
    assert mock_logger.debug.call_args_list.count(call(
        "Skipping NWS weather alerts for cached point %s,%s outside NWS coverage", 51.51, -0.13,
    )) == 1


@pytest.fixture
def coverage_clock(monkeypatch):
    clock = Mock(monotonic=Mock(return_value=100.0))
    monkeypatch.setattr(nws_coverage, "time", clock)
    return clock.monotonic


@pytest.mark.asyncio
async def test_no_coverage_expires_after_24_hours(alert_client, coverage_clock, mock_logger):
    _client, fetch, get = alert_client
    get.return_value = _mock_response(ok=False, status_code=404)

    await fetch(51.5074, -0.1278)
    coverage_clock.return_value = 100.0 + 24 * 60 * 60 - 0.001
    await fetch(51.5074, -0.1278)
    assert get.call_count == 1
    mock_logger.warning.assert_called_once()

    coverage_clock.return_value = 100.0 + 24 * 60 * 60
    await fetch(51.5074, -0.1278)
    assert get.call_count == 2
    assert mock_logger.warning.call_count == 2


@pytest.mark.asyncio
async def test_success_clears_only_its_points_entry(alert_client, coverage_clock):
    client, fetch, get = alert_client
    cache = client._nws_no_coverage
    cache.mark_unavailable(47.6062, -122.3321)

    def successful_response(*_args, **_kwargs):
        # A concurrent no-coverage response may arrive while this request is in flight.
        cache.mark_unavailable(51.5074, -0.1278)
        return _mock_response(text="<feed />")

    get.side_effect = successful_response
    await fetch(51.5074, -0.1278)

    assert (51.51, -0.13) not in cache._points
    assert cache.is_unavailable(47.6062, -122.3321)
    await fetch(51.5074, -0.1278)
    assert get.call_count == 2


@pytest.mark.asyncio
async def test_no_coverage_cache_is_bounded_and_evicts_least_recently_used(alert_client, coverage_clock):
    client, fetch, get = alert_client
    get.return_value = _mock_response(ok=False, status_code=404)
    cache = client._nws_no_coverage

    for index in range(256):
        await fetch(index / 100, 0.0)
        assert len(cache._points) <= 256

    await fetch(0.0, 0.0)
    assert get.call_count == 256
    await fetch(2.56, 0.0)
    assert len(cache._points) == 256
    assert cache.is_unavailable(0.0, 0.0)
    assert not cache.is_unavailable(0.01, 0.0)

    await fetch(0.01, 0.0)
    assert get.call_count == 258
    assert len(cache._points) == 256


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_other_http_errors_do_not_suppress_requests(alert_client, status):
    client, fetch, get = alert_client
    get.return_value = _mock_response(ok=False, status_code=status)
    await fetch(51.5074, -0.1278)
    await fetch(51.5074, -0.1278)
    assert get.call_count == 2
    assert not client._nws_no_coverage._points


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
    assert not cmd._nws_no_coverage._points
