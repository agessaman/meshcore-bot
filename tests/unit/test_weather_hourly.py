"""Hourly forecasts use the provider's local time and stay within one radio reply."""

import configparser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.commands.wx_command import WxCommand
from modules.i18n import Translator

MODULE = "modules.commands.alternatives.wx_international"


def _command(command_class=GlobalWxCommand, *, provider="noaa", language="en", **weather):
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": provider, **weather}, "Bot": {}, "Wx_Command": {}})
    bot = Mock()
    bot.config = config
    bot.translator = Translator(language)
    cmd = command_class(bot)
    cmd.send_response = AsyncMock()
    return cmd


def _data(current="2035-06-01T09:45"):
    return {
        "current": {"time": current},
        "hourly": {
            "time": [f"2035-06-01T{hour:02d}:00" for hour in range(9, 15)],
            "temperature_2m": [49.8] * 6,
            "weather_code": [61] * 6,
            "precipitation_probability": [26] * 6,
            "wind_speed_10m": [5.9] * 6,
            "wind_direction_10m": [202.5] * 6,
            "wind_gusts_10m": [12] * 6,
        },
    }


def _response(data):
    response = Mock(ok=True)
    response.json.return_value = data
    return response


@pytest.mark.parametrize("current,first", [("2035-06-01T09:45", "10AM:"), ("2035-06-01T10:00", "11AM:")])
def test_open_meteo_hourly_uses_current_time_and_packs_whole_lines(current, first):
    cmd = _command()
    cmd.get_max_message_length = Mock(return_value=158)
    with patch(f"{MODULE}.requests.get", return_value=_response(_data(current))) as get:
        text = cmd.get_open_meteo_weather(47.6, -122.3, "hourly", message=Mock(), location_prefix_len=18)
    lines = text.splitlines()
    assert lines[0].startswith(first)
    assert "🌧️ 26% Light Rain 49° SW5" in lines[0]
    assert len(lines) >= 2
    assert len(text.encode()) + 18 <= 158
    next_line = lines[-1].replace(lines[-1].split(":")[0], "2PM", 1)
    assert len((text + "\n" + next_line).encode()) + 18 > 158
    assert "precipitation_probability" in get.call_args.kwargs["params"]["hourly"].split(",")
    assert get.call_args.kwargs["params"]["timezone"] == "auto"


def test_open_meteo_hourly_rolls_over_midnight():
    data = _data("2035-05-31T23:45")
    data["hourly"]["time"] = ["2035-05-31T23:00", "2035-06-01T00:00", "2035-06-01T01:00"]
    cmd = _command()
    with patch(f"{MODULE}.requests.get", return_value=_response(data)):
        text = cmd.get_open_meteo_weather(0, 0, "hourly")
    assert text.startswith("12AM:")
    assert "1AM:" in text
    assert "11PM:" not in text


def test_hourly_preserves_zero_temperature_and_omits_null_optional_values():
    data = _data()
    data["hourly"]["temperature_2m"] = [0] * 6
    data["hourly"]["precipitation_probability"] = [None] * 6
    data["hourly"]["wind_speed_10m"] = [None] * 6
    cmd = _command(temperature_unit="celsius")
    with patch(f"{MODULE}.requests.get", return_value=_response(data)):
        text = cmd.get_open_meteo_weather(0, 0, "hourly")
    assert "0°" in text
    assert "%" not in text
    assert "SW" not in text


@pytest.mark.parametrize("mutation", ["no_current", "bad_current", "no_future", "null_temperature", "missing_codes"])
def test_hourly_without_usable_data_is_not_available(mutation):
    data = _data()
    if mutation == "no_current":
        data.pop("current")
    elif mutation == "bad_current":
        data["current"]["time"] = "invalid"
    elif mutation == "no_future":
        data["current"]["time"] = "2035-06-02T00:00"
    elif mutation == "null_temperature":
        data["hourly"]["temperature_2m"] = [None] * 6
    else:
        data["hourly"].pop("weather_code")
    cmd = _command()
    with patch(f"{MODULE}.requests.get", return_value=_response(data)):
        assert cmd.get_open_meteo_weather(0, 0, "hourly") == "Hourly forecast not available"


@pytest.mark.parametrize("temperature,wind", [("celsius", "kmh"), ("fahrenheit", "mph")])
def test_hourly_requests_configured_units_and_translates_wind(temperature, wind):
    cmd = _command(language="ru", temperature_unit=temperature, wind_speed_unit=wind)
    with patch(f"{MODULE}.requests.get", return_value=_response(_data())) as get:
        text = cmd.get_open_meteo_weather(0, 0, "hourly")
    assert "ЮЗ5" in text
    assert get.call_args.kwargs["params"]["temperature_unit"] == temperature
    assert get.call_args.kwargs["params"]["wind_speed_unit"] == wind


@pytest.mark.asyncio
@pytest.mark.parametrize("command_class,keyword", [(GlobalWxCommand, "gwx"), (WxCommand, "wx")])
async def test_hourly_execution_geocodes_only_the_location(command_class, keyword):
    cmd = _command(command_class, provider="openmeteo")
    actual = cmd.delegate_command if command_class is WxCommand else cmd
    actual.send_response = cmd.send_response
    actual.geocode_location = Mock(return_value=(47.6, -122.3, {}, None))
    actual._format_location_display = Mock(return_value="München, DE")
    actual.get_max_message_length = Mock(return_value=158)
    message = SimpleNamespace(content=f"{keyword} Seattle hourly", sender_id="u")
    with patch(f"{MODULE}.requests.get", return_value=_response(_data())):
        assert await cmd.execute(message)
    actual.geocode_location.assert_called_once_with("Seattle")
    cmd.send_response.assert_awaited_once()
    text = cmd.send_response.call_args.args[1]
    assert text.startswith("München, DE: 10AM:")
    assert len(text.encode()) <= 158


@pytest.mark.asyncio
@pytest.mark.parametrize("command_class,keyword", [(GlobalWxCommand, "gwx"), (WxCommand, "wx")])
@pytest.mark.parametrize("option", ["hourly", "alerts"])
@pytest.mark.parametrize("source", ["wxsim", "mqtt_weather"])
async def test_custom_sources_reject_unsupported_options(command_class, keyword, option, source):
    cmd = _command(command_class, **{f"custom.{source}.patio": "https://example.test/weather" if source == "wxsim" else "weather/patio"})
    cmd.wxsim_parser = Mock()
    cmd.get_weather_for_location = AsyncMock()
    cmd._city_to_lat_lon_async = AsyncMock()
    message = SimpleNamespace(content=f"{keyword} patio {option}", sender_id="u")
    assert await cmd.execute(message)
    expected = ("Not available for this weather source" if source == "wxsim"
                else "Extended forecast is not available for MQTT weather sources")
    cmd.send_response.assert_awaited_once_with(message, expected)
    cmd.wxsim_parser.fetch_from_url.assert_not_called()
    cmd.get_weather_for_location.assert_not_awaited()
    cmd._city_to_lat_lon_async.assert_not_awaited()


@pytest.mark.parametrize("command_class", [GlobalWxCommand, WxCommand])
@pytest.mark.parametrize("option", ["hourly", "alerts"])
def test_wxsim_direct_call_rejects_before_fetch(command_class, option):
    cmd = _command(command_class, language="de")
    cmd.wxsim_parser = Mock()
    assert cmd._get_wxsim_weather("https://example.test/weather", option) == "Not available for this weather source"
    cmd.wxsim_parser.fetch_from_url.assert_not_called()


@pytest.mark.parametrize("direction,expected", [("NNE", "NNE5"), ("SSW", "SSW5")])
def test_noaa_hourly_keeps_16_point_wind(direction, expected):
    cmd = _command(WxCommand)
    line = cmd._hourly_line({"startTime": "2035-06-01T10:00:00-07:00", "temperature": 49,
                             "shortForecast": "Light Rain", "windSpeed": "5 mph", "windDirection": direction})
    assert line.endswith(expected)


def _wxsim_period(high, low):
    from modules.clients.wxsim_parser import ForecastPeriod, PeriodType

    return ForecastPeriod(
        day_name="Fri", date="Oct 2", period_type=list(PeriodType)[0], conditions="Clear", high_temp=high, low_temp=low
    )


@pytest.mark.parametrize("cls_path", ["modules.commands.wx_command.WxCommand", "modules.commands.alternatives.wx_international.GlobalWxCommand"])
def test_wxsim_keeps_zero_degree_highs_and_lows(cls_path):
    import configparser
    import importlib
    from unittest.mock import Mock

    module_name, cls_name = cls_path.rsplit(".", 1)
    cls = getattr(importlib.import_module(module_name), cls_name)
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": "noaa", "temperature_unit": "celsius"}, "Wx_Command": {"temperature_unit": "celsius"}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator.translate.side_effect = lambda key, **kw: f"<{key}{sorted(kw.items()) or ''}>"
    cmd = cls(bot)
    forecast = Mock(periods=[_wxsim_period(5.0, -3.0), _wxsim_period(0.0, 0.0)])
    cmd.wxsim_parser.fetch_from_url = Mock(return_value="text")
    cmd.wxsim_parser.parse = Mock(return_value=forecast)
    cmd.wxsim_parser.is_forecast_stale = Mock(return_value=(False, None))
    text = cmd._get_wxsim_weather("http://example/plaintext.txt", "tomorrow")
    assert "0°C" in text


def test_gwx_wxsim_warns_about_a_stale_forecast():
    import configparser
    from unittest.mock import Mock

    from modules.commands.alternatives.wx_international import GlobalWxCommand

    config = configparser.ConfigParser()
    config.read_dict({"Weather": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator.translate.side_effect = lambda key, **kw: key
    cmd = GlobalWxCommand(bot)
    cmd.logger = Mock()
    cmd.wxsim_parser.fetch_from_url = Mock(return_value="text")
    cmd.wxsim_parser.parse = Mock(return_value=Mock(periods=[_wxsim_period(5.0, -3.0), _wxsim_period(6.0, 1.0)]))
    cmd.wxsim_parser.is_forecast_stale = Mock(return_value=(True, "issued 3 days ago"))
    cmd._get_wxsim_weather("http://example/plaintext.txt", "tomorrow")
    cmd.logger.warning.assert_called_with("WXSIM forecast appears stale: issued 3 days ago")
