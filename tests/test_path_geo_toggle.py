"""Tests for PathCommand geographic_scoring_enabled config toggle."""

import configparser
from pathlib import Path
from unittest.mock import Mock

import pytest

from modules.commands.path_command import PathCommand


@pytest.fixture
def bot_with_location():
    """Bot mock with a valid Bot section and lat/lon configured."""
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_latitude", "37.7749")
    config.set("Bot", "bot_longitude", "-122.4194")
    config.add_section("Path_Command")
    config.add_section("Logging")

    bot = Mock()
    bot.logger = Mock()
    bot.config = config
    bot.db_manager = Mock()
    bot.bot_root = Path("/tmp")
    bot._local_root = None
    bot.prefix_hex_chars = 2
    bot.key_prefix = lambda pk: (pk or "")[:2]
    bot.repeater_manager = Mock()
    bot.repeater_manager.get_repeater_devices = Mock(return_value=[])
    bot.web_viewer_integration = None
    bot.mesh_graph = None
    return bot


class TestPathGeoScoringToggle:
    """geographic_scoring_enabled config option gates self.geographic_guessing_enabled."""

    def test_geo_scoring_enabled_by_default_with_valid_coords(self, bot_with_location):
        """When geographic_scoring_enabled is unset (defaults True) and coords valid → enabled."""
        cmd = PathCommand(bot_with_location)
        assert cmd.geographic_guessing_enabled is True
        assert cmd.geographic_scoring_config_enabled is True

    def test_geo_scoring_disabled_via_config(self, bot_with_location):
        """When geographic_scoring_enabled = false, guessing stays disabled even with valid coords."""
        bot_with_location.config.set("Path_Command", "geographic_scoring_enabled", "false")
        cmd = PathCommand(bot_with_location)
        assert cmd.geographic_scoring_config_enabled is False
        assert cmd.geographic_guessing_enabled is False

    def test_geo_scoring_enabled_explicit_true(self, bot_with_location):
        """Explicit geographic_scoring_enabled = true behaves the same as default."""
        bot_with_location.config.set("Path_Command", "geographic_scoring_enabled", "true")
        cmd = PathCommand(bot_with_location)
        assert cmd.geographic_scoring_config_enabled is True
        assert cmd.geographic_guessing_enabled is True

    def test_geo_guessing_disabled_without_coords_regardless_of_toggle(self, bot_with_location):
        """Without configured coordinates, guessing is disabled even when toggle is True."""
        bot_with_location.config.remove_option("Bot", "bot_latitude")
        bot_with_location.config.remove_option("Bot", "bot_longitude")
        cmd = PathCommand(bot_with_location)
        assert cmd.geographic_guessing_enabled is False

    def test_coords_stored_when_scoring_enabled(self, bot_with_location):
        """bot_latitude/bot_longitude are stored when geographic scoring is on."""
        cmd = PathCommand(bot_with_location)
        assert cmd.bot_latitude == pytest.approx(37.7749)
        assert cmd.bot_longitude == pytest.approx(-122.4194)

    def test_coords_stored_but_guessing_off_when_toggle_disabled(self, bot_with_location):
        """Coordinates are stored even when toggle disables guessing (for potential future use)."""
        bot_with_location.config.set("Path_Command", "geographic_scoring_enabled", "false")
        cmd = PathCommand(bot_with_location)
        assert cmd.bot_latitude == pytest.approx(37.7749)
        assert cmd.geographic_guessing_enabled is False
