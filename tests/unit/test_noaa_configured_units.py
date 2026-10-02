"""wx's NOAA replies follow [Weather] temperature_unit / wind_speed_unit."""

import configparser
import copy
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest

from modules.commands.wx_command import WxCommand

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "noaa"
US = json.loads((FIXTURES / "seattle.json").read_text())
SI = json.loads((FIXTURES / "seattle_si.json").read_text())
NOW = datetime(2026, 10, 1, 15, 30, tzinfo=ZoneInfo("America/Los_Angeles"))


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.replace(tzinfo=None) if tz is None else NOW.astimezone(tz)


def _wx(weather=None, wx=None):
    config = configparser.ConfigParser()
    config.read_dict({
        "Weather": {"weather_provider": "noaa", **(weather or {})},
        "Wx_Command": dict(wx or {}),
        "Bot": {},
    })
    bot = Mock()
    bot.config = config
    bot.translator.translate.side_effect = lambda key, **kw: f"<{key}{sorted(kw.items()) or ''}>"
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))
    bot.command_manager.send_channel_message = AsyncMock()
    return WxCommand(bot)


def _response(payload, ok=True):
    r = Mock()
    r.ok = ok
    r.status_code = 200 if ok else 503
    r.json.return_value = payload
    return r


def _session(si: bool, observation=None):
    """Serve the captured US or SI forecast, recording which URLs were asked for."""
    asked = []
    data = SI if si else US

    def get(url, timeout=None, **kw):
        asked.append(url)
        base = url.split("?")[0]
        if base == US["points_url"]:
            return _response(US["points"])
        if base == US["forecast_url"]:
            return _response(copy.deepcopy(data["forecast"]))
        if base == US["points"]["properties"]["forecastHourly"]:
            return _response(copy.deepcopy(data["hourly"]))
        if base == US["stations_url"]:
            return _response(US["stations"])
        if base == US["observation_url"]:
            return _response(observation or US["observation"])
        return _response({}, ok=False)

    session = Mock()
    session.get = Mock(side_effect=get)
    return session, asked


def _latlon():
    lat, lon = US["points_url"].rsplit("/", 1)[1].split(",")
    return float(lat), float(lon)


def _weather(cmd, si):
    cmd.noaa_session, asked = _session(si)
    with patch("modules.commands.wx_command.datetime", _Clock):
        text, _ = cmd.get_noaa_weather(*_latlon(), max_length=400)
    return text, asked


def test_default_units_are_unchanged():
    text, asked = _weather(_wx(), si=False)
    assert all("units=si" not in u for u in asked)
    assert "°F" in text


def test_celsius_asks_noaa_for_si_units_and_shows_celsius():
    text, asked = _weather(_wx({"temperature_unit": "celsius", "wind_speed_unit": "kmh"}), si=True)
    assert any(u.startswith(US["forecast_url"]) and "units=si" in u for u in asked)
    assert "°C" in text and "°F" not in text


def test_celsius_shows_visibility_in_km_and_dew_point_in_celsius():
    cmd = _wx({"temperature_unit": "celsius", "wind_speed_unit": "kmh"})
    observed = copy.deepcopy(US["observation"])
    observed["properties"]["visibility"] = {"value": 16090}
    observed["properties"]["dewpoint"] = {"value": 8.6}
    observed["properties"]["windGust"] = {"value": 10.0}
    cmd.noaa_session, _ = _session(si=True, observation=observed)
    data = cmd.get_observation_data(US["points"])
    assert data["visibility"] == "16"  # km
    assert data["dew_point"] == "9"  # °C
    assert data["wind_gusts"] == "36"  # km/h


def test_fahrenheit_observations_are_as_before():
    cmd = _wx()
    observed = copy.deepcopy(US["observation"])
    observed["properties"]["visibility"] = {"value": 16090}
    observed["properties"]["dewpoint"] = {"value": 8.6}
    observed["properties"]["windGust"] = {"value": 10.0}
    cmd.noaa_session, _ = _session(si=False, observation=observed)
    data = cmd.get_observation_data(US["points"])
    assert (data["visibility"], data["dew_point"], data["wind_gusts"]) == ("9", "47", "22")


@pytest.mark.parametrize(
    ("units", "si", "noaa", "shown"),
    [
        ({"wind_speed_unit": "kmh"}, False, "10 mph", "16"),
        ({"wind_speed_unit": "ms"}, False, "10 mph", "4"),
        ({"temperature_unit": "celsius", "wind_speed_unit": "mph"}, True, "16 km/h", "10"),
        ({"temperature_unit": "celsius", "wind_speed_unit": "kmh"}, True, "16 km/h", "16"),
        ({}, False, "10 mph", "10"),
    ],
)
def test_wind_is_converted_to_the_configured_unit(units, si, noaa, shown):
    cmd = _wx(units)
    assert cmd._noaa_wind_convert(noaa.split()[0], noaa) == shown


def test_wx_command_section_overrides_weather_section():
    cmd = _wx({"temperature_unit": "celsius"}, {"temperature_unit": "fahrenheit"})
    assert cmd._noaa_units()[0] == "fahrenheit"
    assert _wx({"temperature_unit": "celsius"})._noaa_units()[0] == "celsius"


def test_invalid_units_fall_back_to_imperial():
    assert _wx({"temperature_unit": "kelvin", "wind_speed_unit": "knots"})._noaa_units() == ("fahrenheit", "mph")


def test_celsius_hourly_uses_si_and_converts_nothing_else():
    cmd = _wx({"temperature_unit": "celsius", "wind_speed_unit": "kmh"})
    cmd.noaa_session, asked = _session(si=True)
    with patch("modules.commands.wx_command.datetime", _Clock):
        text, _ = cmd.get_noaa_hourly_weather(*_latlon())
    assert any("forecast/hourly" in u and "units=si" in u for u in asked)
    assert "km/h" not in text
