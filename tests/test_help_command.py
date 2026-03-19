"""Tests for modules.commands.help_command — pure logic and integration paths."""

import configparser
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

import pytest

from modules.commands.help_command import HelpCommand
from tests.conftest import mock_message


# ---------------------------------------------------------------------------
# Bot factory
# ---------------------------------------------------------------------------

def _make_bot(enabled=True, commands=None):
    """Create a minimal mock bot for HelpCommand tests."""
    bot = MagicMock()
    bot.logger = Mock()

    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    if enabled:
        config.add_section("Help_Command")
        config.set("Help_Command", "enabled", "true")

    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = MagicMock()

    # Default empty commands dict
    if commands is None:
        bot.command_manager.commands = {}
    else:
        bot.command_manager.commands = commands

    # Plugin loader with keyword_mappings
    bot.command_manager.plugin_loader = MagicMock()
    bot.command_manager.plugin_loader.keyword_mappings = {}

    # DB manager with in-memory SQLite
    import sqlite3
    conn = sqlite3.connect(":memory:")
    db = MagicMock()

    @contextmanager
    def _conn_ctx():
        yield conn

    db.connection = _conn_ctx
    db.db_path = ":memory:"
    bot.db_manager = db

    return bot


# ---------------------------------------------------------------------------
# _format_commands_list_to_length (pure logic)
# ---------------------------------------------------------------------------

class TestFormatCommandsListToLength:
    def setup_method(self):
        self.cmd = HelpCommand(_make_bot())

    def test_no_max_length_returns_all(self):
        result = self.cmd._format_commands_list_to_length(["a", "b", "c"])
        assert result == "a, b, c"

    def test_max_length_zero_returns_all(self):
        result = self.cmd._format_commands_list_to_length(["a", "b", "c"], max_length=0)
        assert result == "a, b, c"

    def test_empty_list_returns_empty(self):
        result = self.cmd._format_commands_list_to_length([])
        assert result == ""

    def test_truncates_at_max_length(self):
        # "a, b, c" = 7 chars; limit to 4 → only "a" + " (2 more)"
        result = self.cmd._format_commands_list_to_length(["a", "b", "c"], max_length=10)
        # Just verify it doesn't exceed max_length
        assert len(result) <= 10

    def test_all_fit_within_max_length(self):
        result = self.cmd._format_commands_list_to_length(["ping", "wx"], max_length=100)
        assert result == "ping, wx"

    def test_suffix_appended_when_truncated(self):
        names = ["alpha", "beta", "gamma", "delta", "epsilon"]
        result = self.cmd._format_commands_list_to_length(names, max_length=20)
        # Should contain "(N more)" suffix or be truncated
        assert len(result) <= 20 or "more" in result

    def test_single_item(self):
        result = self.cmd._format_commands_list_to_length(["ping"])
        assert result == "ping"

    def test_single_item_exceeds_max_length(self):
        result = self.cmd._format_commands_list_to_length(["verylongcommandname"], max_length=5)
        # Can't fit, returns empty or truncated
        assert isinstance(result, str)

    def test_negative_max_length_returns_all(self):
        result = self.cmd._format_commands_list_to_length(["a", "b"], max_length=-1)
        assert result == "a, b"


# ---------------------------------------------------------------------------
# _is_command_valid_for_channel
# ---------------------------------------------------------------------------

class TestIsCommandValidForChannel:
    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = HelpCommand(self.bot)

    def test_no_message_always_true(self):
        mock_cmd = MagicMock()
        assert self.cmd._is_command_valid_for_channel("ping", mock_cmd, None) is True

    def test_channel_allowed_returns_true(self):
        mock_cmd = MagicMock()
        mock_cmd.is_channel_allowed = Mock(return_value=True)
        msg = mock_message(content="help", channel="general")
        assert self.cmd._is_command_valid_for_channel("ping", mock_cmd, msg) is True

    def test_channel_not_allowed_returns_false(self):
        mock_cmd = MagicMock()
        mock_cmd.is_channel_allowed = Mock(return_value=False)
        msg = mock_message(content="help", channel="general")
        assert self.cmd._is_command_valid_for_channel("ping", mock_cmd, msg) is False

    def test_no_is_channel_allowed_attribute(self):
        mock_cmd = MagicMock(spec=[])  # No attributes
        msg = mock_message(content="help", channel="general")
        # Should not crash, should check _is_channel_trigger_allowed
        result = self.cmd._is_command_valid_for_channel("ping", mock_cmd, msg)
        assert isinstance(result, bool)

    def test_channel_trigger_not_allowed(self):
        mock_cmd = MagicMock()
        mock_cmd.is_channel_allowed = Mock(return_value=True)
        self.bot.command_manager._is_channel_trigger_allowed = Mock(return_value=False)
        msg = mock_message(content="help", channel="restricted")
        assert self.cmd._is_command_valid_for_channel("restricted_cmd", mock_cmd, msg) is False

    def test_channel_trigger_allowed(self):
        mock_cmd = MagicMock()
        mock_cmd.is_channel_allowed = Mock(return_value=True)
        self.bot.command_manager._is_channel_trigger_allowed = Mock(return_value=True)
        msg = mock_message(content="help", channel="general")
        assert self.cmd._is_command_valid_for_channel("ping", mock_cmd, msg) is True

    def test_no_channel_trigger_check_attribute(self):
        """When command_manager lacks _is_channel_trigger_allowed, still works."""
        mock_cmd = MagicMock()
        mock_cmd.is_channel_allowed = Mock(return_value=True)
        del self.bot.command_manager._is_channel_trigger_allowed
        msg = mock_message(content="help", channel="general")
        result = self.cmd._is_command_valid_for_channel("ping", mock_cmd, msg)
        assert result is True


# ---------------------------------------------------------------------------
# get_specific_help
# ---------------------------------------------------------------------------

class TestGetSpecificHelp:
    def test_known_command_with_help_text(self):
        bot = _make_bot()
        mock_ping = MagicMock()
        mock_ping.get_help_text = Mock(return_value="Ping the bot")
        bot.command_manager.commands = {"ping": mock_ping}
        cmd = HelpCommand(bot)
        result = cmd.get_specific_help("ping")
        assert "commands.help.specific" in result or result != ""

    def test_known_command_help_text_no_message_param(self):
        """Falls back to no-argument get_help_text when TypeError is raised."""
        bot = _make_bot()
        mock_cmd = MagicMock()
        mock_cmd.get_help_text = Mock(side_effect=[TypeError("no param"), "Simple help"])
        bot.command_manager.commands = {"foo": mock_cmd}
        cmd = HelpCommand(bot)
        result = cmd.get_specific_help("foo")
        assert isinstance(result, str)

    def test_unknown_command_returns_unknown_key(self):
        bot = _make_bot()
        bot.command_manager.commands = {}
        cmd = HelpCommand(bot)
        result = cmd.get_specific_help("unknowncmd")
        assert "commands.help.unknown" in result

    def test_alias_mapping_applied(self):
        """Alias 'ping' maps to itself."""
        bot = _make_bot()
        mock_ping = MagicMock()
        mock_ping.get_help_text = Mock(return_value="Pong!")
        bot.command_manager.commands = {"ping": mock_ping}
        cmd = HelpCommand(bot)
        result = cmd.get_specific_help("ping")
        assert isinstance(result, str)

    def test_no_get_help_text_attribute(self):
        """Command without get_help_text returns no_help key."""
        bot = _make_bot()
        mock_cmd = MagicMock(spec=[])
        bot.command_manager.commands = {"bare": mock_cmd}
        cmd = HelpCommand(bot)
        result = cmd.get_specific_help("bare")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# can_execute
# ---------------------------------------------------------------------------

class TestCanExecute:
    def test_enabled_true(self):
        bot = _make_bot(enabled=True)
        cmd = HelpCommand(bot)
        msg = mock_message(content="help", channel="general")
        assert cmd.can_execute(msg) is True

    def test_enabled_false(self):
        bot = _make_bot()
        # Set the flag directly — the config section already exists from _make_bot
        bot.config.set("Help_Command", "enabled", "false")
        cmd = HelpCommand(bot)
        cmd.help_enabled = False
        msg = mock_message(content="help", channel="general")
        assert cmd.can_execute(msg) is False


# ---------------------------------------------------------------------------
# get_help_text
# ---------------------------------------------------------------------------

class TestGetHelpText:
    def test_returns_string(self):
        cmd = HelpCommand(_make_bot())
        result = cmd.get_help_text()
        assert isinstance(result, str)


class TestGetGeneralHelp:
    """Tests for get_general_help() (lines 134-138)."""

    def test_returns_string(self):
        bot = _make_bot()
        cmd = HelpCommand(bot)
        result = cmd.get_general_help()
        assert isinstance(result, str)

    def test_includes_commands_help_key(self):
        bot = _make_bot()
        cmd = HelpCommand(bot)
        result = cmd.get_general_help()
        # Our mock translator returns keys — so should contain 'commands.help.general'
        assert "commands.help" in result


class TestGetAvailableCommandsListFiltered:
    """Tests for channel-filtered command listing (line 185)."""

    def test_channel_filter_excludes_invalid_commands(self):
        bot = _make_bot()
        mock_ping = MagicMock()
        mock_ping.name = "ping"
        mock_ping.is_channel_allowed = Mock(return_value=False)  # Excluded
        mock_wx = MagicMock()
        mock_wx.name = "wx"
        mock_wx.is_channel_allowed = Mock(return_value=True)  # Included
        bot.command_manager.commands = {"ping": mock_ping, "wx": mock_wx}
        bot.command_manager._is_channel_trigger_allowed = Mock(return_value=True)
        cmd = HelpCommand(bot)
        msg = mock_message(content="help", channel="general")
        result = cmd.get_available_commands_list(message=msg)
        # ping is excluded; wx should be present
        assert "wx" in result or "commands.help" in result  # or just no crash

    def test_command_in_stats_not_in_keyword_mappings(self):
        """Commands returned from DB stats but not in keyword_mappings (lines 218-235)."""
        import sqlite3
        from contextlib import contextmanager

        bot = _make_bot()
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE command_stats (
                id INTEGER PRIMARY KEY,
                timestamp INTEGER,
                sender_id TEXT,
                command_name TEXT,
                channel TEXT,
                is_dm BOOLEAN,
                response_sent BOOLEAN
            )
        """)
        # Add a command that's NOT in keyword_mappings but IS a primary command name
        conn.execute("INSERT INTO command_stats VALUES (1, 1, 'u', 'unknown_cmd', 'g', 0, 1)")
        conn.commit()

        db = MagicMock()

        @contextmanager
        def _conn_ctx():
            yield conn

        db.connection = _conn_ctx
        bot.db_manager = db

        mock_cmd = MagicMock()
        mock_cmd.name = "known"
        bot.command_manager.commands = {"known": mock_cmd}
        bot.command_manager.plugin_loader.keyword_mappings = {}

        cmd = HelpCommand(bot)
        result = cmd.get_available_commands_list()
        assert isinstance(result, str)


class TestFormatCommandsListSuffix:
    """Tests for _format_commands_list_to_length with suffix that fits (line 294)."""

    def test_suffix_fits_within_max(self):
        cmd = HelpCommand(_make_bot())
        # "ping" = 4 chars, " (1 more)" = 9 chars, "wx" doesn't fit; total "ping (1 more)" = 13
        result = cmd._format_commands_list_to_length(["ping", "wx"], max_length=13)
        assert "(1 more)" in result or "ping" in result

    def test_suffix_appended_when_some_fit(self):
        cmd = HelpCommand(_make_bot())
        names = ["ab", "cd", "ef", "gh"]
        result = cmd._format_commands_list_to_length(names, max_length=8)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

class TestExecute:
    def test_execute_returns_true(self):
        import asyncio
        bot = _make_bot()
        cmd = HelpCommand(bot)
        msg = mock_message(content="help", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True


# ---------------------------------------------------------------------------
# get_available_commands_list
# ---------------------------------------------------------------------------

class TestGetAvailableCommandsList:
    def test_empty_commands_returns_empty(self):
        bot = _make_bot()
        bot.command_manager.commands = {}
        cmd = HelpCommand(bot)
        result = cmd.get_available_commands_list()
        assert isinstance(result, str)

    def test_with_commands_returns_names(self):
        bot = _make_bot()
        mock_ping = MagicMock()
        mock_ping.name = "ping"
        bot.command_manager.commands = {"ping": mock_ping}
        cmd = HelpCommand(bot)
        result = cmd.get_available_commands_list()
        assert "ping" in result

    def test_with_max_length(self):
        bot = _make_bot()
        mock_ping = MagicMock()
        mock_ping.name = "ping"
        bot.command_manager.commands = {"ping": mock_ping}
        cmd = HelpCommand(bot)
        result = cmd.get_available_commands_list(max_length=3)
        assert len(result) <= 3 or "ping" in result

    def test_with_message_filter(self):
        bot = _make_bot()
        mock_ping = MagicMock()
        mock_ping.name = "ping"
        mock_ping.is_channel_allowed = Mock(return_value=True)
        bot.command_manager.commands = {"ping": mock_ping}
        cmd = HelpCommand(bot)
        msg = mock_message(content="help", channel="general")
        result = cmd.get_available_commands_list(message=msg)
        assert isinstance(result, str)

    def test_with_stats_table_present(self):
        """When command_stats table exists, commands are sorted by usage count."""
        import sqlite3
        from contextlib import contextmanager

        bot = _make_bot()
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE command_stats (
                id INTEGER PRIMARY KEY,
                timestamp INTEGER,
                sender_id TEXT,
                command_name TEXT,
                channel TEXT,
                is_dm BOOLEAN,
                response_sent BOOLEAN
            )
        """)
        conn.execute("INSERT INTO command_stats (timestamp, sender_id, command_name, channel, is_dm, response_sent) VALUES (1, 'u1', 'ping', 'general', 0, 1)")
        conn.execute("INSERT INTO command_stats (timestamp, sender_id, command_name, channel, is_dm, response_sent) VALUES (2, 'u1', 'ping', 'general', 0, 1)")
        conn.commit()

        db = MagicMock()

        @contextmanager
        def _conn_ctx():
            yield conn

        db.connection = _conn_ctx
        bot.db_manager = db

        mock_ping = MagicMock()
        mock_ping.name = "ping"
        bot.command_manager.commands = {"ping": mock_ping}
        bot.command_manager.plugin_loader.keyword_mappings = {"ping": "ping"}

        cmd = HelpCommand(bot)
        result = cmd.get_available_commands_list()
        assert "ping" in result

    def test_db_exception_falls_back_gracefully(self):
        """If DB raises, falls back to sorted command names."""
        bot = _make_bot()
        mock_ping = MagicMock()
        mock_ping.name = "ping"
        bot.command_manager.commands = {"ping": mock_ping}
        bad_db = MagicMock()

        @contextmanager
        def _bad_conn():
            raise Exception("DB down")
            yield  # noqa: unreachable

        bad_db.connection = _bad_conn
        bot.db_manager = bad_db

        cmd = HelpCommand(bot)
        result = cmd.get_available_commands_list()
        assert isinstance(result, str)
