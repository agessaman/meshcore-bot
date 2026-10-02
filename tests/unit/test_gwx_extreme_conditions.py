"""gwx warnings use Open-Meteo values, independently of reply units and wording."""

import configparser
from unittest.mock import Mock, patch

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.i18n import Translator

MODULE = "modules.commands.alternatives.wx_international"


def _gwx(temperature_unit="fahrenheit", wind_unit="mph", language="en"):
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {
        "temperature_unit": temperature_unit,
        "wind_speed_unit": wind_unit,
    }, "Gwx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator = Translator(language)
    cmd = GlobalWxCommand(bot)
    cmd.geocode_location = Mock(return_value=(1.0, 2.0, {"city": "Test City", "country_code": "fr"}, None))
    cmd._format_location_display = Mock(return_value="Test City")
    cmd.get_max_message_length = Mock(return_value=158)
    return cmd


def _data(cmd, **current):
    temp = 25.0 if cmd.temperature_unit == "celsius" else 77.0
    return {
        "current": {
            "temperature_2m": temp,
            "apparent_temperature": temp,
            "relative_humidity_2m": 50,
            "wind_direction_10m": 0,
            "wind_speed_10m": 0,
            "wind_gusts_10m": 0,
            "weather_code": 0,
            **current,
        },
        "daily": {
            "temperature_2m_max": [temp, temp],
            "temperature_2m_min": [temp, temp],
            "weather_code": [0, 0],
        },
    }


def _run(cmd, data, forecast_type="default", ok=True):
    response = Mock(ok=ok, status_code=503 if not ok else 200)
    response.json.return_value = data
    with patch(f"{MODULE}.requests.get", return_value=response) as get:
        result = cmd._get_weather_for_location_sync(
            "Test City", forecast_type=forecast_type, num_days=2, message=Mock()
        )
    get.assert_called_once()
    assert get.call_args.kwargs["params"]["temperature_unit"] == cmd.temperature_unit
    assert get.call_args.kwargs["params"]["wind_speed_unit"] == cmd.wind_speed_unit
    cmd.logger.error.assert_not_called()
    return result


def _assert_warning(result, expected):
    if expected is None:
        assert isinstance(result, str)
        assert result.startswith("Test City: ")
    else:
        assert isinstance(result, tuple)
        assert len(result) == 3
        assert result[0] == "multi_message"
        assert result[1].startswith("Test City: ")
        assert result[2] == expected


@pytest.mark.parametrize("temperature_unit", ["fahrenheit", "celsius"])
@pytest.mark.parametrize("fahrenheit, expected", [
    (94.9, None), (95, "⚠️ Extreme heat"), (95.1, "⚠️ Extreme heat"),
    (20.1, None), (20, "⚠️ Extreme cold"), (19.9, "⚠️ Extreme cold"),
    (-1, "⚠️ Extreme cold"), (-100, "⚠️ Extreme cold"),
])
def test_temperature_thresholds(temperature_unit, fahrenheit, expected):
    cmd = _gwx(temperature_unit=temperature_unit)
    temp = fahrenheit if temperature_unit == "fahrenheit" else (fahrenheit - 32) * 5 / 9
    result = _run(cmd, _data(cmd, temperature_2m=temp, apparent_temperature=temp))
    _assert_warning(result, expected)


@pytest.mark.parametrize("wind_unit, factor, label", [
    ("mph", 1, "mph"), ("kmh", 1.609344, "km/h"), ("ms", 0.44704, "m/s"), ("kn", 0.868976, "kn"),
])
@pytest.mark.parametrize("mph, warns", [(29.9, False), (30, True), (30.1, True), (40, True)])
def test_wind_thresholds(wind_unit, factor, label, mph, warns):
    # The threshold is 30 mph in any unit; the warning shows the speed in the reply's own unit.
    cmd = _gwx(wind_unit=wind_unit)
    result = _run(cmd, _data(cmd, wind_speed_10m=mph * factor))
    _assert_warning(result, f"⚠️ High winds ({int(mph * factor)} {label})" if warns else None)


def test_polish_warning_shows_the_replys_own_speed():
    cmd = _gwx(temperature_unit="celsius", wind_unit="kmh", language="pl")
    result = _run(cmd, _data(cmd, wind_speed_10m=80))
    assert "80" in result[1] and result[2].endswith("(80 km/h)")


def test_40_kmh_is_below_the_high_wind_threshold():
    cmd = _gwx(wind_unit="kmh")
    _assert_warning(_run(cmd, _data(cmd, wind_speed_10m=40)), None)


def test_sustained_wind_warning_keeps_its_speed_even_with_stronger_gusts():
    cmd = _gwx()
    _assert_warning(_run(cmd, _data(cmd, wind_speed_10m=30, wind_gusts_10m=45)),
                    "⚠️ High winds (30 mph)")


def test_gusts_alone_do_not_warn():
    # Gusts of 30+ mph are common on ordinary windy days; the warning is about sustained wind.
    cmd = _gwx()
    _assert_warning(_run(cmd, _data(cmd, wind_speed_10m=10, wind_gusts_10m=45)), None)


@pytest.mark.parametrize("code, expected", [
    (65, "⚠️ Heavy rain"), (82, "⚠️ Heavy rain"),
    (95, "⚠️ Thunderstorms"), (96, "⚠️ Thunderstorms"), (97, "⚠️ Thunderstorms"), (99, "⚠️ Thunderstorms"),
    (75, "⚠️ Heavy snow"), (85, "⚠️ Heavy snow"), (86, "⚠️ Heavy snow"),
    *[(code, None) for code in (0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 66, 67, 71, 73, 77, 80, 81, 999)],
])
def test_wmo_warning_categories(code, expected):
    cmd = _gwx()
    _assert_warning(_run(cmd, _data(cmd, weather_code=code)), expected)


@pytest.mark.parametrize("code, warning", [
    (65, "heavy_rain"), (82, "heavy_rain"),
    (95, "thunderstorms"), (96, "thunderstorms"), (97, "thunderstorms"), (99, "thunderstorms"),
    (75, "heavy_snow"), (85, "heavy_snow"), (86, "heavy_snow"),
])
def test_russian_directions_and_conditions(code, warning):
    cmd = _gwx(temperature_unit="celsius", wind_unit="kmh", language="ru")
    result = _run(cmd, _data(cmd, weather_code=code, wind_speed_10m=48.28032))
    expected = " | ".join([
        cmd.translate(f"commands.gwx.warnings.{warning}"),
        cmd.translate("commands.gwx.warnings.high_winds", wind_speed=48, unit="км/ч"),
    ])
    _assert_warning(result, expected)
    assert "С48" in result[1]


def test_warning_words_in_a_description_do_not_create_warnings():
    cmd = _gwx()
    cmd.bot.translator.translations["commands"]["gwx"]["weather_descriptions"]["0"] = (
        "Heavy Rain Thunderstorm Heavy Snow"
    )
    _assert_warning(_run(cmd, _data(cmd)), None)


def test_warning_does_not_depend_on_a_rendered_description():
    cmd = _gwx()
    cmd._get_weather_description = Mock(return_value="Wet")
    _assert_warning(_run(cmd, _data(cmd, weather_code=65)), "⚠️ Heavy rain")


def test_daily_extremes_do_not_replace_current_conditions():
    cmd = _gwx()
    data = _data(cmd)
    data["daily"].update(
        temperature_2m_max=[110, 110], temperature_2m_min=[-100, -100],
        weather_code=[99, 99], wind_speed_10m_max=[100, 100], wind_gusts_10m_max=[120, 120],
    )
    _assert_warning(_run(cmd, data), None)


def test_combined_warnings_keep_the_existing_order_and_wording():
    cmd = _gwx()
    result = _run(cmd, _data(cmd, temperature_2m=100, apparent_temperature=100,
                            weather_code=95, wind_speed_10m=40))
    _assert_warning(result, "⚠️ Extreme heat | ⚠️ Thunderstorms | ⚠️ High winds (40 mph)")


@pytest.mark.parametrize("forecast_type", ["tomorrow", "multiday"])
def test_forecast_only_replies_do_not_add_current_warnings(forecast_type):
    cmd = _gwx()
    result = _run(cmd, _data(cmd, temperature_2m=100, weather_code=99, wind_speed_10m=50),
                  forecast_type=forecast_type)
    _assert_warning(result, None)


def test_fetch_error_still_returns_the_api_error():
    cmd = _gwx()
    assert _run(cmd, {}, ok=False) == cmd.translate("commands.gwx.error_fetching_api")


def test_public_weather_getter_still_returns_text():
    cmd = _gwx()
    response = Mock(ok=True)
    response.json.return_value = _data(cmd, temperature_2m=100, weather_code=99, wind_speed_10m=50)
    with patch(f"{MODULE}.requests.get", return_value=response) as get:
        text = cmd.get_open_meteo_weather(1.0, 2.0)
    assert isinstance(text, str)
    assert "100°F" in text
    get.assert_called_once()


def test_heavy_thunderstorm_code_has_its_own_description_and_emoji():
    # Open-Meteo documents WMO 97 as a heavy thunderstorm.
    cmd = _gwx()
    assert cmd._get_weather_description(97) == "Heavy T-Storm"
    assert cmd._get_weather_emoji(97) == "⛈️"
