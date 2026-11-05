"""Daily weather forecast retry behavior."""

import asyncio
import configparser
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from modules.service_plugins.weather_service import (
    FORECAST_RETRY_JOB_ID,
    ForecastFetchResult,
    WeatherService,
)


def _build_service(mock_logger) -> WeatherService:
    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.add_section("Weather_Service")
    config.set("Weather_Service", "my_position_lat", "47.6")
    config.set("Weather_Service", "my_position_lon", "-122.3")
    config.set("Weather_Service", "weather_alarm", "06:00")

    bot = Mock()
    bot.logger = mock_logger
    bot.config = config
    bot.db_manager = Mock()
    bot.translator.translate.side_effect = lambda key, **_kwargs: key
    bot.command_manager.send_channel_message = AsyncMock(return_value=True)

    service = WeatherService(bot)
    service._running = True
    return service


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_http_status_is_retryable(mock_logger, status):
    service = _build_service(mock_logger)
    response = Mock(ok=False, status_code=status)
    service.api_session.get = Mock(return_value=response)

    result = asyncio.run(service._get_weather_forecast_result())

    assert result.retryable is True
    assert result.text == "services.weather_service.error_fetching"


def test_permanent_http_status_is_not_retryable(mock_logger):
    service = _build_service(mock_logger)
    response = Mock(ok=False, status_code=400)
    service.api_session.get = Mock(return_value=response)

    result = asyncio.run(service._get_weather_forecast_result())

    assert result.retryable is False
    assert result.text == "services.weather_service.error_fetching"


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.Timeout("timed out"),
        requests.exceptions.ConnectionError("connection reset"),
    ],
)
def test_transport_failure_is_retryable(mock_logger, error):
    service = _build_service(mock_logger)
    service.api_session.get = Mock(side_effect=error)

    result = asyncio.run(service._get_weather_forecast_result())

    assert result.retryable is True


def test_retry_jobs_use_absolute_backoff_offsets(mock_logger):
    service = _build_service(mock_logger)
    scheduler = Mock()
    service._forecast_scheduler = scheduler
    cycle_id, started_at = service._begin_forecast_cycle()
    scheduler.reset_mock()
    service._get_weather_forecast_result = AsyncMock(
        return_value=ForecastFetchResult(
            "services.weather_service.error_fetching",
            retryable=True,
        )
    )

    asyncio.run(service._send_daily_forecast_async(cycle_id, started_at, 0))
    first_job = scheduler.add_job.call_args.kwargs
    assert first_job["id"] == FORECAST_RETRY_JOB_ID
    assert first_job["args"] == [cycle_id, started_at, 1]
    assert first_job["trigger"].run_date == started_at + timedelta(minutes=5)

    asyncio.run(service._send_daily_forecast_async(cycle_id, started_at, 1))
    second_job = scheduler.add_job.call_args.kwargs
    assert second_job["args"] == [cycle_id, started_at, 2]
    assert second_job["trigger"].run_date == started_at + timedelta(minutes=15)

    asyncio.run(service._send_daily_forecast_async(cycle_id, started_at, 2))
    third_job = scheduler.add_job.call_args.kwargs
    assert third_job["args"] == [cycle_id, started_at, 3]
    assert third_job["trigger"].run_date == started_at + timedelta(minutes=30)


def test_successful_retry_sends_once_and_stale_attempt_is_ignored(mock_logger):
    service = _build_service(mock_logger)
    scheduler = Mock()
    service._forecast_scheduler = scheduler
    cycle_id, started_at = service._begin_forecast_cycle()
    service._get_weather_forecast_result = AsyncMock(
        return_value=ForecastFetchResult("Seattle: Clear")
    )

    first_result = asyncio.run(
        service._send_daily_forecast_async(cycle_id, started_at, 1)
    )
    stale_result = asyncio.run(
        service._send_daily_forecast_async(cycle_id, started_at, 2)
    )

    assert first_result is True
    assert stale_result is False
    service.bot.command_manager.send_channel_message.assert_awaited_once()
    service._get_weather_forecast_result.assert_awaited_once()


def test_permanent_failure_does_not_schedule_retry(mock_logger):
    service = _build_service(mock_logger)
    scheduler = Mock()
    service._forecast_scheduler = scheduler
    cycle_id, started_at = service._begin_forecast_cycle()
    scheduler.reset_mock()
    service._get_weather_forecast_result = AsyncMock(
        return_value=ForecastFetchResult("services.weather_service.error_fetching")
    )

    result = asyncio.run(
        service._send_daily_forecast_async(cycle_id, started_at, 0)
    )

    assert result is False
    scheduler.add_job.assert_not_called()


def test_retry_is_not_scheduled_after_service_stops(mock_logger):
    service = _build_service(mock_logger)
    scheduler = Mock()
    service._forecast_scheduler = scheduler
    cycle_id, started_at = service._begin_forecast_cycle()
    scheduler.reset_mock()
    service._running = False
    service._get_weather_forecast_result = AsyncMock(
        return_value=ForecastFetchResult(
            "services.weather_service.error_fetching",
            retryable=True,
        )
    )

    asyncio.run(service._send_daily_forecast_async(cycle_id, started_at, 0))

    scheduler.add_job.assert_not_called()


def test_expired_absolute_deadline_runs_promptly(mock_logger):
    service = _build_service(mock_logger)
    scheduler = Mock()
    service._forecast_scheduler = scheduler
    cycle_id, _ = service._begin_forecast_cycle()
    scheduler.reset_mock()
    old_start = datetime.now(timezone.utc) - timedelta(hours=1)

    before = datetime.now(timezone.utc)
    service._schedule_forecast_retry(cycle_id, old_start, retry_attempt=0)
    after = datetime.now(timezone.utc)

    run_date = scheduler.add_job.call_args.kwargs["trigger"].run_date
    assert before + timedelta(seconds=1) <= run_date <= after + timedelta(seconds=1)
