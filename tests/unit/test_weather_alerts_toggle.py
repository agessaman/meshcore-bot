#!/usr/bin/env python3
"""Unit tests for weather_alerts_enabled — the NOAA alert loop's own switch."""

import asyncio
import configparser
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.service_plugins.weather_service import WeatherService


def _weather_service(mock_logger, extra_cfg=None):
    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.add_section("Weather_Service")
    config.set("Weather_Service", "my_position_lat", "47.6062")
    config.set("Weather_Service", "my_position_lon", "-122.3321")
    for key, val in (extra_cfg or {}).items():
        config.set("Weather_Service", key, val)

    bot = Mock()
    bot.logger = mock_logger
    bot.config = config
    bot.db_manager = Mock()
    bot.command_manager = Mock()
    bot.command_manager.send_channel_message = AsyncMock()
    return WeatherService(bot)


class TestWeatherAlertsEnabledConfig:
    def test_defaults_on_so_upgrades_keep_their_alerts(self, mock_logger):
        """The loop has always run whenever the service did; silence on upgrade
        would be a regression, not a new option."""
        assert _weather_service(mock_logger).weather_alerts_enabled is True

    def test_reads_false_from_config(self, mock_logger):
        svc = _weather_service(mock_logger, {"weather_alerts_enabled": "false"})
        assert svc.weather_alerts_enabled is False

    def test_reads_true_from_config(self, mock_logger):
        svc = _weather_service(mock_logger, {"weather_alerts_enabled": "true"})
        assert svc.weather_alerts_enabled is True

    def test_exposed_in_the_settings_schema_as_a_bool(self):
        entry = next(
            item for item in WeatherService.settings_schema
            if item["key"] == "weather_alerts_enabled"
        )
        assert entry["type"] == "bool"
        assert entry["default"] is True


class TestWeatherAlertsEnabledStart:
    """start() must not spawn the poll task when the option is off."""

    @staticmethod
    async def _start_and_collect(svc):
        """Run start() with the other background work stubbed out."""
        spawned = []

        def fake_create_task(coro):
            spawned.append(getattr(coro, "__qualname__", repr(coro)))
            coro.close()  # never actually run the loop
            return Mock()

        with patch.object(
            WeatherService, "_setup_daily_forecast", Mock()
        ), patch(
            "modules.service_plugins.weather_service.asyncio.create_task",
            side_effect=fake_create_task,
        ):
            await svc.start()
        return spawned

    @pytest.mark.asyncio
    async def test_disabled_skips_the_poll_task(self, mock_logger):
        svc = _weather_service(mock_logger, {"weather_alerts_enabled": "false"})

        spawned = await self._start_and_collect(svc)

        assert svc._alerts_task is None
        assert not any("_poll_weather_alerts_loop" in name for name in spawned)

    @pytest.mark.asyncio
    async def test_enabled_starts_the_poll_task(self, mock_logger):
        svc = _weather_service(mock_logger, {"weather_alerts_enabled": "true"})

        spawned = await self._start_and_collect(svc)

        assert svc._alerts_task is not None
        assert any("_poll_weather_alerts_loop" in name for name in spawned)

    @pytest.mark.asyncio
    async def test_default_starts_the_poll_task(self, mock_logger):
        svc = _weather_service(mock_logger)

        spawned = await self._start_and_collect(svc)

        assert any("_poll_weather_alerts_loop" in name for name in spawned)

    @pytest.mark.asyncio
    async def test_disabling_alerts_leaves_the_rain_nowcast_alone(self, mock_logger):
        """The switch is for the alert loop only, not the whole service."""
        svc = _weather_service(
            mock_logger,
            {"weather_alerts_enabled": "false", "rain_nowcast_enabled": "true"},
        )

        spawned = await self._start_and_collect(svc)

        assert not any("_poll_weather_alerts_loop" in name for name in spawned)
        assert any("_poll_rain_nowcast_loop" in name for name in spawned)

    @pytest.mark.asyncio
    async def test_disabling_alerts_still_schedules_the_daily_forecast(self, mock_logger):
        svc = _weather_service(mock_logger, {"weather_alerts_enabled": "false"})

        with patch.object(
            WeatherService, "_setup_daily_forecast", Mock()
        ) as setup_forecast, patch(
            "modules.service_plugins.weather_service.asyncio.create_task",
            side_effect=lambda coro: (coro.close(), Mock())[1],
        ):
            await svc.start()

        setup_forecast.assert_called_once()

    @pytest.mark.asyncio
    async def test_service_reports_running_with_alerts_off(self, mock_logger):
        svc = _weather_service(mock_logger, {"weather_alerts_enabled": "false"})

        await self._start_and_collect(svc)

        assert svc._running is True


class TestStopWithAlertsDisabled:
    @pytest.mark.asyncio
    async def test_stop_tolerates_the_absent_task(self, mock_logger):
        """_alerts_task is None when disabled; stop() must not trip over it."""
        svc = _weather_service(mock_logger, {"weather_alerts_enabled": "false"})
        svc._running = True
        svc._alerts_task = None
        svc._forecast_task = None
        svc._lightning_task = None
        svc._rain_task = None
        svc.mqtt_task = None

        await asyncio.wait_for(svc.stop(), timeout=5)

        assert svc._running is False
