"""Characterization: wx's NOAA forecast formatting.

Replays real api.weather.gov responses captured on 2026-10-01 (Seattle, Denver,
Miami, Anchorage; tests/fixtures/noaa) with the clock frozen at capture time.
Variants drop leading forecast periods (other times of day), remove the
station observation, and change the length budget.
"""

import configparser
import copy
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tests.characterization.golden_util import assert_golden

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "noaa"
LOCATIONS = ["seattle", "denver", "miami", "anchorage"]
FROZEN_NOW = datetime(2026, 10, 1, 15, 30, 0)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


def _wx():
    from modules.commands.wx_command import WxCommand

    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": "noaa"}, "Wx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.logger = Mock()
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))
    bot.command_manager.send_channel_message = AsyncMock()
    bot.translator.translate.side_effect = lambda key, **kwargs: f"<{key}{sorted(kwargs.items()) or ''}>"
    return WxCommand(bot)


def _response(payload, ok=True):
    response = Mock()
    response.ok = ok
    response.status_code = 200 if ok else 503
    response.json.return_value = payload
    return response


def _load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _session(data, *, drop_periods=0, with_observation=True):
    forecast = copy.deepcopy(data["forecast"])
    forecast["properties"]["periods"] = forecast["properties"]["periods"][drop_periods:]
    routes = {
        data["points_url"]: _response(data["points"]),
        data["forecast_url"]: _response(forecast),
        data["stations_url"]: _response(data["stations"]),
        data["observation_url"]: _response(data["observation"], ok=with_observation),
    }
    session = Mock()
    session.get = Mock(side_effect=lambda url, timeout=None, **kw: routes.get(url, _response({}, ok=False)))
    return session, forecast["properties"]["periods"]


def _latlon(data):
    url = data["points_url"].rsplit("/", 1)[1]
    lat, lon = url.split(",")
    return float(lat), float(lon)


@pytest.mark.parametrize("name", LOCATIONS)
def test_noaa_forecast_text(name):
    data = _load(name)
    lat, lon = _latlon(data)
    results = {}
    with patch("modules.commands.wx_command.datetime", _FrozenDateTime):
        for drop in (0, 1, 2, 3):
            for with_obs in (True, False):
                for max_length in (130, 200):
                    cmd = _wx()
                    cmd.noaa_session, periods = _session(data, drop_periods=drop, with_observation=with_obs)
                    weather, _points = cmd.get_noaa_weather(lat, lon, max_length=max_length)
                    key = f"drop{drop}-obs{int(with_obs)}-len{max_length}"
                    results[key] = weather
                    if with_obs and max_length == 130:
                        results[f"tomorrow-drop{drop}"] = cmd.format_tomorrow_forecast(periods)
                        for days in (3, 5, 7):
                            results[f"multiday{days}-drop{drop}"] = cmd.format_multiday_forecast(periods, num_days=days)
                        results[f"details-drop{drop}"] = [
                            cmd._add_period_details("Now:", p.get("detailedForecast", ""), length, max_length=130)
                            for p in periods[:4]
                            for length in (0, 60, 110)
                        ]
    assert_golden(f"noaa_wx_{name}", results)


@pytest.mark.parametrize("name", ["seattle", "miami"])
def test_noaa_hourly_text(name):
    data = _load(name)
    periods = data["hourly"]["properties"]["periods"]
    with patch("modules.commands.wx_command.datetime", _FrozenDateTime):
        cmd = _wx()
        results = {f"len{n}": cmd.format_hourly_forecast(periods, max_length=n) for n in (130, 200)}
    assert_golden(f"noaa_wx_hourly_{name}", results)


LONG_FORECASTS = [
    "Chance Showers And Thunderstorms then Mostly Sunny",
    "Slight Chance Rain Showers And Patchy Fog then Partly Sunny With Breezy Winds",
    "Rain And Snow Showers Likely Becoming Mostly Cloudy",
    "Areas Of Fog then Sunny",
    "Patchy Smoke Followed By Hazy Sunshine And Light Winds",
]


@pytest.mark.parametrize("name", ["miami", "seattle"])
def test_noaa_forecast_text_with_long_forecasts(name):
    data = copy.deepcopy(_load(name))
    for i, period in enumerate(data["forecast"]["properties"]["periods"][1:]):
        period["shortForecast"] = LONG_FORECASTS[i % len(LONG_FORECASTS)]
    lat, lon = _latlon(data)
    results = {}
    with patch("modules.commands.wx_command.datetime", _FrozenDateTime):
        for drop in (0, 1, 2, 3):
            for max_length in (130, 160, 200):
                cmd = _wx()
                cmd.noaa_session, _ = _session(data, drop_periods=drop)
                results[f"drop{drop}-len{max_length}"] = cmd.get_noaa_weather(lat, lon, max_length=max_length)[0]
    assert_golden(f"noaa_wx_long_{name}", results)


DETAIL_TEXTS = [
    "Mostly sunny, with a high near 70. Humidity 45%. Dew point 52. Visibility 10 miles.",
    "Showers likely. Chance of precipitation is 60%. Wind gusts as high as 25 mph. Pressure 1012 mb.",
    "Rain. Chance of precipitation is 90%. Humidity around 88 percent. Gusts up to 40 mph.",
    "Clear. Low around 40.",
    "Cloudy with a 40% chance of rain. Humidity 70%. Wind gusts as high as 30 mph.",
]
OBSERVATIONS = [
    None,
    {"humidity": 52, "dew_point": 48, "visibility": 9, "wind_gusts": "18mph", "pressure": 1016},
    {"humidity": 0, "dew_point": None, "visibility": 10},
]


def test_add_period_details_matrix():
    cmd = _wx()
    results = []
    for text in DETAIL_TEXTS:
        for observation in OBSERVATIONS:
            for length in (0, 40, 80, 100, 115):
                for budget in (130, 200):
                    results.append(
                        cmd._add_period_details(
                            " | Today: ☀️Sunny 75°", text, length, max_length=budget, observation_data=observation
                        )
                    )
    assert_golden("noaa_wx_period_details", results)


def test_a_tomorrow_period_without_forecast_text_is_skipped_not_fatal():
    # A null shortForecast must not break the reply (it used to be skipped by the guard).
    data = copy.deepcopy(_load("miami"))  # starts with Tonight
    periods = data["forecast"]["properties"]["periods"]
    for period in periods[1:]:
        period["temperature"] = None
        period["shortForecast"] = None
    lat, lon = _latlon(data)
    with patch("modules.commands.wx_command.datetime", _FrozenDateTime):
        cmd = _wx()
        cmd.noaa_session, _ = _session(data)
        weather, points = cmd.get_noaa_weather(lat, lon)
    assert weather.startswith("Tonight:")
    assert points is not None
