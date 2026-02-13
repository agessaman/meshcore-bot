"""Tests for MessageScheduler pure logic (no threading, no asyncio)."""

import pytest
from configparser import ConfigParser
from unittest.mock import Mock

from modules.scheduler import MessageScheduler


@pytest.fixture
def scheduler(mock_logger):
    """MessageScheduler with mock bot for pure logic tests."""
    bot = Mock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("Bot")
    return MessageScheduler(bot)


class TestIsValidTimeFormat:
    """Tests for _is_valid_time_format()."""

    def test_valid_time_0000(self, scheduler):
        assert scheduler._is_valid_time_format("0000") is True

    def test_valid_time_2359(self, scheduler):
        assert scheduler._is_valid_time_format("2359") is True

    def test_valid_time_1200(self, scheduler):
        assert scheduler._is_valid_time_format("1200") is True

    def test_invalid_time_2400(self, scheduler):
        assert scheduler._is_valid_time_format("2400") is False

    def test_invalid_time_0060(self, scheduler):
        assert scheduler._is_valid_time_format("0060") is False

    def test_invalid_time_short(self, scheduler):
        assert scheduler._is_valid_time_format("123") is False

    def test_invalid_time_letters(self, scheduler):
        assert scheduler._is_valid_time_format("abcd") is False

    def test_invalid_time_empty(self, scheduler):
        assert scheduler._is_valid_time_format("") is False


class TestGetCurrentTime:
    """Tests for timezone-aware time retrieval."""

    def test_valid_timezone(self, scheduler):
        scheduler.bot.config.set("Bot", "timezone", "US/Pacific")
        result = scheduler.get_current_time()
        assert result.tzinfo is not None

    def test_invalid_timezone_falls_back(self, scheduler):
        scheduler.bot.config.set("Bot", "timezone", "Invalid/Zone")
        result = scheduler.get_current_time()
        # Should still return a datetime (system time fallback)
        assert result is not None
        scheduler.bot.logger.warning.assert_called()

    def test_empty_timezone_uses_system(self, scheduler):
        scheduler.bot.config.set("Bot", "timezone", "")
        result = scheduler.get_current_time()
        assert result is not None


class TestHasMeshInfoPlaceholders:
    """Tests for _has_mesh_info_placeholders()."""

    def test_detects_placeholder(self, scheduler):
        assert scheduler._has_mesh_info_placeholders("Contacts: {total_contacts}") is True

    def test_no_placeholder_returns_false(self, scheduler):
        assert scheduler._has_mesh_info_placeholders("Hello world") is False

    def test_detects_legacy_placeholder(self, scheduler):
        assert scheduler._has_mesh_info_placeholders("Repeaters: {repeaters}") is True
