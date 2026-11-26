"""gwx retries an Open-Meteo request that stalls instead of failing the command."""

import configparser
import io
import json
from unittest.mock import Mock, patch

from urllib3.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse

from modules.commands.alternatives.wx_international import OPEN_METEO_TIMEOUT, GlobalWxCommand


def _command(mock_logger):
    config = configparser.ConfigParser()
    config.add_section("Weather")
    bot = Mock()
    bot.logger = mock_logger
    bot.config = config
    bot.db_manager = Mock()
    return GlobalWxCommand(bot)


def _payload():
    return {
        "current": {
            "time": "2026-11-23T15:00",
            "temperature_2m": 44,
            "weather_code": 3,
            "wind_speed_10m": 5,
            "wind_direction_10m": 180,
            "apparent_temperature": 40,
            "relative_humidity_2m": 70,
            "is_day": 1,
        },
        "daily": {
            "time": ["2026-11-23", "2026-11-24"],
            "weather_code": [3, 61],
            "temperature_2m_max": [47, 49],
            "temperature_2m_min": [41, 42],
        },
    }


def _ok_response():
    return HTTPResponse(
        body=io.BytesIO(json.dumps(_payload()).encode()),
        headers={"Content-Type": "application/json"},
        status=200,
        preload_content=False,
    )


def test_read_timeout_is_retried(mock_logger):
    """A stalled first attempt is retried; the reply is the forecast, not the error."""
    cmd = _command(mock_logger)
    calls = []

    def make_request(pool, conn, method, url, *args, **kwargs):
        calls.append(kwargs.get("timeout"))
        if len(calls) == 1:
            raise ReadTimeoutError(pool, url, "Read timed out.")
        return _ok_response()

    with patch("urllib3.connectionpool.HTTPConnectionPool._make_request", autospec=True,
               side_effect=make_request), patch("urllib3.util.retry.time.sleep"):
        reply = cmd.get_open_meteo_weather(48.42, -122.33)

    assert len(calls) == 2
    assert reply != cmd.translate("commands.gwx.error_fetching")
    assert "44" in reply


def test_gives_up_after_three_attempts(mock_logger):
    cmd = _command(mock_logger)
    calls = []

    def make_request(pool, conn, method, url, *args, **kwargs):
        calls.append(url)
        raise ReadTimeoutError(pool, url, "Read timed out.")

    with patch("urllib3.connectionpool.HTTPConnectionPool._make_request", autospec=True,
               side_effect=make_request), patch("urllib3.util.retry.time.sleep"):
        reply = cmd.get_open_meteo_weather(48.42, -122.33)

    assert len(calls) == 3
    assert reply == cmd.translate("commands.gwx.error_fetching")


def test_uses_short_per_attempt_timeout(mock_logger):
    cmd = _command(mock_logger)
    response = Mock(ok=True)
    response.json.return_value = _payload()
    with patch("requests.Session.get", return_value=response) as get:
        cmd.get_open_meteo_weather(48.42, -122.33)
    assert get.call_args.kwargs["timeout"] == OPEN_METEO_TIMEOUT
