#!/usr/bin/env python3
"""Unit tests for reading the [Weather_Service] blitz_area_* lightning box."""

import configparser
from unittest.mock import AsyncMock, Mock

from modules.service_plugins.weather_service import WeatherService

BOX = {
    "blitz_area_min_lat": "47.0",
    "blitz_area_min_lon": "-123.0",
    "blitz_area_max_lat": "48.5",
    "blitz_area_max_lon": "-121.5",
}


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


class TestBlitzArea:
    def test_unset_means_off(self, mock_logger):
        assert _weather_service(mock_logger).blitz_area is None

    def test_all_four_corners(self, mock_logger):
        svc = _weather_service(mock_logger, BOX)
        assert svc.blitz_area == {
            "min_lat": 47.0, "min_lon": -123.0, "max_lat": 48.5, "max_lon": -121.5,
        }

    def test_partial_box_is_off_instead_of_failing_init(self, mock_logger):
        """The web viewer saves one corner at a time; a half-filled box used to
        raise NoOptionError and take the whole service down."""
        svc = _weather_service(mock_logger, {"blitz_area_min_lat": "47.0"})
        assert svc.blitz_area is None
        mock_logger.warning.assert_any_call(
            "Lightning detection off: blitz_area_min_lat, blitz_area_min_lon, "
            "blitz_area_max_lat and blitz_area_max_lon must all be numbers"
        )

    def test_blank_corners_are_unset(self, mock_logger):
        svc = _weather_service(mock_logger, dict.fromkeys(BOX, ""))
        assert svc.blitz_area is None
