"""wx with [Weather] openmeteo_fallback: places outside NWS coverage are answered from Open-Meteo."""

import configparser
from unittest.mock import AsyncMock, Mock

import pytest

from modules.commands.wx_command import WxCommand

TOKYO = (35.6812, 139.7671)
TOKYO_ADDRESS = {"city": "Tokyo", "country_code": "jp"}


def _wx(fallback=True, provider="noaa", weather=None, wx_section=None):
    config = configparser.ConfigParser()
    config.read_dict({
        "Weather": {"weather_provider": provider, "default_state": "WA", "default_country": "US",
                    "openmeteo_fallback": str(fallback).lower(), **(weather or {})},
        "Wx_Command": wx_section or {},
        "Gwx_Command": {},
        "Bot": {},
    })
    bot = Mock()
    bot.config = config
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    wx = WxCommand(bot)
    if wx._openmeteo is not None:
        wx._openmeteo.get_open_meteo_weather = Mock(return_value="Clear 20°C")
        wx._openmeteo._get_open_meteo_weather_with_conditions = Mock(return_value=("Clear 20°C", {}))
    return wx


def _nws(wx, status):
    """Every NWS request answers *status*; returns the get mock."""
    response = Mock(ok=status < 400, status_code=status, text="")
    wx.noaa_session = Mock()
    wx.noaa_session.get = Mock(return_value=response)
    return wx.noaa_session.get


def _tokyo(wx, forecast_type="default"):
    wx.city_to_lat_lon = Mock(return_value=(*TOKYO, TOKYO_ADDRESS))
    return wx._get_weather_for_location_sync("Tokyo", "city", forecast_type)


def test_off_by_default_and_unchanged():
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": "noaa"}, "Wx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    wx = WxCommand(bot)
    assert wx._openmeteo is None

    _nws(wx, 404)
    assert _tokyo(wx) == "commands.wx.error_fetching"
    assert not wx._nws_no_coverage.is_unavailable(*TOKYO)


def test_ignored_with_the_openmeteo_provider():
    wx = _wx(provider="openmeteo")
    assert wx._openmeteo is None
    assert wx.delegate_command is not None


def test_no_coverage_answers_from_open_meteo_and_remembers_the_point():
    wx = _wx()
    get = _nws(wx, 404)
    assert _tokyo(wx) == "Tokyo, JP: Clear 20°C"
    assert get.call_count == 1
    assert wx._nws_no_coverage.is_unavailable(*TOKYO)

    assert _tokyo(wx) == "Tokyo, JP: Clear 20°C"
    assert get.call_count == 1  # known outside coverage: no second NWS request


@pytest.mark.parametrize("status", [500, 503])
def test_nws_errors_do_not_fall_back(status):
    wx = _wx()
    _nws(wx, status)
    assert _tokyo(wx) == "commands.wx.error_fetching"
    assert not wx._nws_no_coverage.is_unavailable(*TOKYO)
    wx._openmeteo.get_open_meteo_weather.assert_not_called()
    wx._openmeteo._get_open_meteo_weather_with_conditions.assert_not_called()


def test_a_covered_point_is_marked_available():
    wx = _wx()
    wx._nws_no_coverage.mark_unavailable(47.6, -122.3)
    _nws(wx, 200)
    assert wx._noaa_fetch("https://api.weather.gov/points/47.6,-122.3", "weather data", point=(47.6, -122.3))
    assert not wx._nws_no_coverage.is_unavailable(47.6, -122.3)


@pytest.mark.parametrize("forecast_type", ["tomorrow", "multiday", "hourly"])
def test_forecast_options_fall_back_too(forecast_type):
    wx = _wx()
    _nws(wx, 404)
    assert _tokyo(wx, forecast_type) == "Tokyo, JP: Clear 20°C"
    assert wx._openmeteo.get_open_meteo_weather.call_args.kwargs["forecast_type"] == forecast_type


def test_extreme_conditions_go_out_as_a_second_message():
    wx = _wx()
    _nws(wx, 404)
    wx._openmeteo._check_extreme_conditions = Mock(return_value="Extreme heat")
    assert _tokyo(wx) == ("multi_message", "Tokyo, JP: Clear 20°C", "Extreme heat")


def test_fallback_uses_wx_units():
    wx = _wx(weather={"temperature_unit": "fahrenheit", "precipitation_unit": "inch"},
             wx_section={"temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm"})
    _nws(wx, 404)
    _tokyo(wx)
    om = wx._openmeteo
    assert (om.temperature_unit, om.wind_speed_unit, om.precipitation_unit) == ("celsius", "kmh", "mm")


def test_coordinates_are_labeled_with_the_country():
    wx = _wx()
    _nws(wx, 404)
    wx._coordinates_to_location_string = Mock(return_value="Tokyo")
    wx._openmeteo.geocode_location = Mock(return_value=(*TOKYO, TOKYO_ADDRESS, None))
    reply = wx._get_weather_for_location_sync("35.6812,139.7671", "coordinates")
    assert reply == "Tokyo, JP: Clear 20°C"


@pytest.mark.asyncio
@pytest.mark.parametrize(("fallback", "expected"), [
    (True, "commands.wx.source_option_not_available"),
    (False, "commands.wx.error_fetching"),
])
async def test_alerts_outside_coverage(fallback, expected):
    wx = _wx(fallback=fallback)
    _nws(wx, 404)
    wx.send_response = AsyncMock(return_value=True)
    await wx._send_full_alert_list(Mock(), *TOKYO)
    wx.send_response.assert_awaited_once()
    assert wx.send_response.await_args.args[1] == expected
