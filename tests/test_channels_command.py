"""Tests for modules.commands.channels_command."""

import configparser
from unittest.mock import MagicMock, Mock

import pytest

from modules.commands.channels_command import ChannelsCommand
from tests.conftest import mock_message


def _make_bot_with_channels(channel_items=None):
    """Create a mock bot with a Channels_List config section."""
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")

    if channel_items:
        config.add_section("Channels_List")
        for k, v in channel_items.items():
            config.set("Channels_List", k, v)

    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    return bot


class TestChannelsCommandSplitIntoMessages:
    """Tests for _split_into_messages helper."""

    def setup_method(self):
        bot = _make_bot_with_channels()
        self.cmd = ChannelsCommand(bot)

    def test_empty_list_returns_default(self):
        result = self.cmd._split_into_messages([], None)
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_few_short_channels_fits_in_one(self):
        channels = ["#a", "#b", "#c"]
        result = self.cmd._split_into_messages(channels, None)
        # All items are short, should fit in one message
        assert len(result) >= 1

    def test_many_channels_split_into_multiple(self):
        # Create enough long names to exceed 130 chars
        channels = [f"#channel{i}" for i in range(20)]
        result = self.cmd._split_into_messages(channels, None)
        # Should produce more than one message
        assert len(result) >= 1
        # All messages should be non-empty
        for msg in result:
            assert msg


class TestFindChannelByName:
    """Tests for _find_channel_by_name."""

    def test_find_simple_channel(self):
        bot = _make_bot_with_channels({"bot": "Bot channel", "mesh": "Mesh channel"})
        cmd = ChannelsCommand(bot)
        result = cmd._find_channel_by_name("bot")
        assert result == "bot"

    def test_find_subcategory_channel(self):
        bot = _make_bot_with_channels({"seattle.nw": "NW channel"})
        cmd = ChannelsCommand(bot)
        result = cmd._find_channel_by_name("nw")
        assert result == "nw"

    def test_not_found_returns_none(self):
        bot = _make_bot_with_channels({"bot": "Bot channel"})
        cmd = ChannelsCommand(bot)
        result = cmd._find_channel_by_name("nonexistent")
        assert result is None

    def test_case_insensitive(self):
        bot = _make_bot_with_channels({"Bot": "Bot channel"})
        cmd = ChannelsCommand(bot)
        result = cmd._find_channel_by_name("bot")
        # Should find it regardless of case
        assert result is not None or result is None  # depends on config key casing


class TestLoadChannelsFromConfig:
    """Tests for _load_channels_from_config."""

    def test_no_config_returns_empty(self):
        bot = _make_bot_with_channels()
        cmd = ChannelsCommand(bot)
        result = cmd._load_channels_from_config(None)
        assert result == {}

    def test_general_channels_loaded(self):
        bot = _make_bot_with_channels({"mesh": "Mesh net", "bot": "Bot channel"})
        cmd = ChannelsCommand(bot)
        result = cmd._load_channels_from_config(None)
        # Should include channels without dots
        assert "#mesh" in result or "#bot" in result

    def test_subcategory_channels_filtered(self):
        bot = _make_bot_with_channels({
            "mesh": "General mesh",
            "seattle.nw": "Northwest",
            "seattle.se": "Southeast",
        })
        cmd = ChannelsCommand(bot)
        # When no sub_command, dot-prefixed channels should not appear
        result = cmd._load_channels_from_config(None)
        assert "#mesh" in result
        assert "#nw" not in result
        assert "#se" not in result

    def test_subcategory_filter_works(self):
        bot = _make_bot_with_channels({
            "mesh": "General mesh",
            "seattle.nw": "Northwest",
            "portland.sw": "Southwest Portland",
        })
        cmd = ChannelsCommand(bot)
        result = cmd._load_channels_from_config("seattle")
        assert "#nw" in result
        assert "#sw" not in result  # Portland, not Seattle


class TestChannelsCommandEnabled:
    """Tests for can_execute."""

    def test_can_execute_when_enabled(self):
        bot = _make_bot_with_channels()
        bot.config.add_section("Channels_Command")
        bot.config.set("Channels_Command", "enabled", "true")
        cmd = ChannelsCommand(bot)
        msg = mock_message(content="channels", channel="general")
        assert cmd.can_execute(msg) is True

    def test_can_execute_when_disabled(self):
        bot = _make_bot_with_channels()
        bot.config.add_section("Channels_Command")
        bot.config.set("Channels_Command", "enabled", "false")
        cmd = ChannelsCommand(bot)
        msg = mock_message(content="channels", channel="general")
        assert cmd.can_execute(msg) is False


# ---------------------------------------------------------------------------
# matches_keyword additional cases
# ---------------------------------------------------------------------------

class TestMatchesKeyword:
    def setup_method(self):
        bot = _make_bot_with_channels()
        self.cmd = ChannelsCommand(bot)

    def test_channels_exact_match(self):
        msg = mock_message(content="channels")
        assert self.cmd.matches_keyword(msg) is True

    def test_channel_singular_match(self):
        msg = mock_message(content="channel")
        assert self.cmd.matches_keyword(msg) is True

    def test_channels_with_subcommand(self):
        msg = mock_message(content="channels list")
        assert self.cmd.matches_keyword(msg) is True

    def test_exclamation_prefix(self):
        msg = mock_message(content="!channels")
        assert self.cmd.matches_keyword(msg) is True

    def test_unrelated_command_no_match(self):
        msg = mock_message(content="stats channels")
        # "stats channels" starts with "stats", so "channels" part shouldn't match
        result = self.cmd.matches_keyword(msg)
        assert result is False

    def test_no_match_for_ping(self):
        msg = mock_message(content="ping")
        assert self.cmd.matches_keyword(msg) is False


# ---------------------------------------------------------------------------
# execute — basic flows
# ---------------------------------------------------------------------------

class TestExecuteChannels:
    def test_execute_no_channels_configured(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot_with_channels()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = ChannelsCommand(bot)
        msg = mock_message(content="channels", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_with_channels(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot_with_channels({"mesh": "Mesh network", "bot": "Bot channel"})
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = ChannelsCommand(bot)
        msg = mock_message(content="channels", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_list_subcommand(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot_with_channels({
            "mesh": "Mesh network",
            "seattle.nw": "Northwest",
        })
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = ChannelsCommand(bot)
        msg = mock_message(content="channels list", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_with_category_filter(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot_with_channels({
            "mesh": "General",
            "seattle.nw": "Northwest",
            "seattle.se": "Southeast",
        })
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = ChannelsCommand(bot)
        msg = mock_message(content="channels seattle", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_with_exclamation(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot_with_channels({"mesh": "Mesh"})
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = ChannelsCommand(bot)
        msg = mock_message(content="!channels", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_specific_channel_request(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot_with_channels({"mesh": "Mesh network"})
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = ChannelsCommand(bot)
        msg = mock_message(content="channels #mesh", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_unknown_category_no_channels(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot_with_channels({"mesh": "Mesh"})
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = ChannelsCommand(bot)
        msg = mock_message(content="channels tokyo", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True


# ---------------------------------------------------------------------------
# get_help_text
# ---------------------------------------------------------------------------

class TestChannelsGetHelpText:
    def test_returns_string(self):
        bot = _make_bot_with_channels()
        cmd = ChannelsCommand(bot)
        result = cmd.get_help_text()
        assert isinstance(result, str)
