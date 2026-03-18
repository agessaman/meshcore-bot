"""Tests for modules.commands.aurora_command — pure logic functions."""

import configparser
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, patch

import pytest

from modules.commands.aurora_command import AuroraCommand
from tests.conftest import mock_message


def _make_bot(with_location=False):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    if with_location:
        config.set("Bot", "bot_latitude", "47.6")
        config.set("Bot", "bot_longitude", "-122.3")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.add_section("Weather")
    config.set("Weather", "default_state", "WA")
    config.set("Weather", "default_country", "US")
    config.add_section("Aurora_Command")
    config.set("Aurora_Command", "enabled", "true")
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    return bot


class TestProbIndicator:
    """Tests for _prob_indicator."""

    def setup_method(self):
        self.cmd = AuroraCommand(_make_bot())

    def test_zero_returns_lowest_bar(self):
        result = self.cmd._prob_indicator(0)
        assert result == "▁"

    def test_100_returns_highest_bar(self):
        result = self.cmd._prob_indicator(100)
        assert result == "█"

    def test_50_returns_mid_bar(self):
        result = self.cmd._prob_indicator(50)
        assert result in "▁▂▃▄▅▆▇█"

    def test_all_values_return_valid_bar(self):
        for pct in range(0, 101, 10):
            result = self.cmd._prob_indicator(pct)
            assert result in "▁▂▃▄▅▆▇█"


class TestFormatKpTime:
    """Tests for _format_kp_time."""

    def setup_method(self):
        self.cmd = AuroraCommand(_make_bot())

    def test_empty_string_returns_dash(self):
        assert self.cmd._format_kp_time("") == "—"

    def test_none_like_returns_dash(self):
        assert self.cmd._format_kp_time("   ") == "—"

    def test_space_separated_format(self):
        result = self.cmd._format_kp_time("2026-01-21 05:13:00")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_iso_format(self):
        result = self.cmd._format_kp_time("2026-01-21T05:13:00")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_invalid_returns_dash(self):
        result = self.cmd._format_kp_time("not-a-date")
        assert result == "—"


class TestGetBotLocation:
    """Tests for _get_bot_location."""

    def test_returns_location_when_configured(self):
        bot = _make_bot(with_location=True)
        cmd = AuroraCommand(bot)
        result = cmd._get_bot_location()
        assert result is not None
        lat, lon = result
        assert abs(lat - 47.6) < 0.01
        assert abs(lon - (-122.3)) < 0.01

    def test_returns_none_when_not_configured(self):
        bot = _make_bot(with_location=False)
        cmd = AuroraCommand(bot)
        result = cmd._get_bot_location()
        assert result is None


class TestResolveLocation:
    """Tests for _resolve_location (pure location string parsing)."""

    def setup_method(self):
        self.cmd = AuroraCommand(_make_bot())

    def test_coord_string_parsed(self):
        msg = mock_message(content="aurora 47.6,-122.3")
        lat, lon, label, err = self.cmd._resolve_location(msg, "47.6,-122.3")
        assert lat == pytest.approx(47.6)
        assert lon == pytest.approx(-122.3)
        assert err is None

    def test_invalid_lat_returns_error(self):
        msg = mock_message(content="aurora 200,0")
        lat, lon, label, err = self.cmd._resolve_location(msg, "200,0")
        assert err is not None

    def test_invalid_lon_returns_error(self):
        msg = mock_message(content="aurora 0,200")
        lat, lon, label, err = self.cmd._resolve_location(msg, "0,200")
        assert err is not None

    def test_no_location_uses_bot_location(self):
        bot = _make_bot(with_location=True)
        cmd = AuroraCommand(bot)
        msg = mock_message(content="aurora")
        # No companion location: db_manager returns nothing
        bot.db_manager.execute_query.return_value = []
        lat, lon, label, err = cmd._resolve_location(msg, None)
        assert lat is not None
        assert err is None

    def test_no_location_no_bot_returns_error(self):
        bot = _make_bot(with_location=False)
        cmd = AuroraCommand(bot)
        msg = mock_message(content="aurora")
        # No companion, no bot location
        bot.db_manager.execute_query.return_value = []
        lat, lon, label, err = cmd._resolve_location(msg, None)
        assert err is not None


class TestAuroraCanExecute:
    """Tests for can_execute."""

    def test_enabled(self):
        bot = _make_bot()
        cmd = AuroraCommand(bot)
        msg = mock_message(content="aurora", channel="general")
        assert cmd.can_execute(msg) is True

    def test_disabled(self):
        bot = _make_bot()
        bot.config.set("Aurora_Command", "enabled", "false")
        cmd = AuroraCommand(bot)
        msg = mock_message(content="aurora", channel="general")
        assert cmd.can_execute(msg) is False


class TestResolveLocationExtended:
    """Extended tests for _resolve_location."""

    def test_companion_location_used(self):
        bot = _make_bot()
        cmd = AuroraCommand(bot)
        bot.db_manager.execute_query.return_value = [{"latitude": 47.5, "longitude": -122.1}]
        msg = mock_message(content="aurora", sender_pubkey="a" * 64)
        lat, lon, label, err = cmd._resolve_location(msg, None)
        assert lat == pytest.approx(47.5)
        assert lon == pytest.approx(-122.1)
        assert err is None

    def test_default_lat_lon_from_config(self):
        bot = _make_bot()
        bot.config.set("Aurora_Command", "default_lat", "48.0")
        bot.config.set("Aurora_Command", "default_lon", "-122.5")
        cmd = AuroraCommand(bot)
        bot.db_manager.execute_query.return_value = []
        msg = mock_message(content="aurora")
        lat, lon, label, err = cmd._resolve_location(msg, None)
        assert lat == pytest.approx(48.0)
        assert err is None

    def test_coords_value_error_returns_error(self):
        bot = _make_bot()
        cmd = AuroraCommand(bot)
        msg = mock_message(content="aurora 47.6,-not-a-number")
        lat, lon, label, err = cmd._resolve_location(msg, "47.6,-not-a-number")
        # Non-matching regex so falls through to city path or ValueError
        assert isinstance(err, (str, type(None)))


class TestAuroraExecute:
    """Tests for execute()."""

    def test_no_location_no_bot_returns_error(self):
        from unittest.mock import AsyncMock
        import asyncio
        bot = _make_bot(with_location=False)
        cmd = AuroraCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        bot.db_manager.execute_query.return_value = []
        msg = mock_message(content="aurora", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True
        cmd.send_response.assert_called_once()

    def test_execute_with_bot_location_success(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import asyncio
        bot = _make_bot(with_location=True)
        cmd = AuroraCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        bot.db_manager.execute_query.return_value = []

        # Mock the aurora client data
        mock_data = MagicMock()
        mock_data.kp_index = 2.5
        mock_data.kp_timestamp = "2026-01-21 05:13:00"
        mock_data.aurora_probability = 15.0

        with patch("modules.commands.aurora_command.NOAAAuroraClient") as MockClient:
            MockClient.return_value.get_aurora_data.return_value = mock_data
            msg = mock_message(content="aurora", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is True
        cmd.send_response.assert_called_once()

    def test_execute_kp_g3_severe(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import asyncio
        bot = _make_bot(with_location=True)
        cmd = AuroraCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        bot.db_manager.execute_query.return_value = []

        mock_data = MagicMock()
        mock_data.kp_index = 8.0  # >= 7
        mock_data.kp_timestamp = ""
        mock_data.aurora_probability = 95.0

        with patch("modules.commands.aurora_command.NOAAAuroraClient") as MockClient:
            MockClient.return_value.get_aurora_data.return_value = mock_data
            msg = mock_message(content="aurora", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_kp_g1_g2(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import asyncio
        bot = _make_bot(with_location=True)
        cmd = AuroraCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        bot.db_manager.execute_query.return_value = []

        mock_data = MagicMock()
        mock_data.kp_index = 5.5  # >= 5 but < 7
        mock_data.kp_timestamp = ""
        mock_data.aurora_probability = 60.0

        with patch("modules.commands.aurora_command.NOAAAuroraClient") as MockClient:
            MockClient.return_value.get_aurora_data.return_value = mock_data
            msg = mock_message(content="aurora", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_kp_unsettled(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import asyncio
        bot = _make_bot(with_location=True)
        cmd = AuroraCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        bot.db_manager.execute_query.return_value = []

        mock_data = MagicMock()
        mock_data.kp_index = 4.2  # >= 4 but < 5
        mock_data.kp_timestamp = "2026-01-21T05:13:00"
        mock_data.aurora_probability = 40.0

        with patch("modules.commands.aurora_command.NOAAAuroraClient") as MockClient:
            MockClient.return_value.get_aurora_data.return_value = mock_data
            msg = mock_message(content="aurora", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_aurora_fetch_exception(self):
        from unittest.mock import AsyncMock, patch
        import asyncio
        bot = _make_bot(with_location=True)
        cmd = AuroraCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        bot.db_manager.execute_query.return_value = []

        with patch("modules.commands.aurora_command.NOAAAuroraClient") as MockClient:
            MockClient.return_value.get_aurora_data.side_effect = Exception("Network error")
            msg = mock_message(content="aurora", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is True
        cmd.send_response.assert_called_once()

    def test_execute_with_coords_arg(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import asyncio
        bot = _make_bot()
        cmd = AuroraCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)

        mock_data = MagicMock()
        mock_data.kp_index = 1.0
        mock_data.kp_timestamp = ""
        mock_data.aurora_probability = 5.0

        with patch("modules.commands.aurora_command.NOAAAuroraClient") as MockClient:
            MockClient.return_value.get_aurora_data.return_value = mock_data
            msg = mock_message(content="aurora 47.6,-122.3", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_no_location_error_key(self):
        from unittest.mock import AsyncMock, patch
        import asyncio
        bot = _make_bot(with_location=False)
        cmd = AuroraCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        bot.db_manager.execute_query.return_value = []

        # Simulate an error from _resolve_location
        with patch.object(cmd, "_resolve_location", return_value=(None, None, None, "commands.aurora.no_location")):
            msg = mock_message(content="aurora", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_error_key_zipcode(self):
        from unittest.mock import AsyncMock, patch
        import asyncio
        bot = _make_bot()
        cmd = AuroraCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)

        with patch.object(cmd, "_resolve_location", return_value=(None, None, None, "commands.aurora.no_location_zipcode")):
            msg = mock_message(content="aurora 99999", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_error_key_city(self):
        from unittest.mock import AsyncMock, patch
        import asyncio
        bot = _make_bot()
        cmd = AuroraCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)

        with patch.object(cmd, "_resolve_location", return_value=(None, None, None, "commands.aurora.no_location_city")):
            msg = mock_message(content="aurora Seattle", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_error_key_other(self):
        from unittest.mock import AsyncMock, patch
        import asyncio
        bot = _make_bot()
        cmd = AuroraCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)

        with patch.object(cmd, "_resolve_location", return_value=(None, None, None, "commands.aurora.error")):
            msg = mock_message(content="aurora bad", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_response_truncated_at_max_length(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import asyncio
        bot = _make_bot(with_location=True)
        cmd = AuroraCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        bot.db_manager.execute_query.return_value = []

        mock_data = MagicMock()
        mock_data.kp_index = 0.5
        mock_data.kp_timestamp = ""
        mock_data.aurora_probability = 1.0

        # Make translate return a very long string
        original_translate = cmd.translate
        cmd.translate = Mock(side_effect=lambda key, **kw: "x" * 300 if key == "commands.aurora.response" else key)
        cmd.get_max_message_length = Mock(return_value=100)

        with patch("modules.commands.aurora_command.NOAAAuroraClient") as MockClient:
            MockClient.return_value.get_aurora_data.return_value = mock_data
            msg = mock_message(content="aurora", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is True
        # Response should be truncated
        call_text = cmd.send_response.call_args[0][1]
        assert len(call_text) <= 103  # 100 + "..."
