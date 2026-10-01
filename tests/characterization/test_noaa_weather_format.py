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
