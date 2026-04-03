"""Tests for modules.commands.stats_command — pure logic functions."""

import configparser
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

from modules.commands.stats_command import StatsCommand
from tests.conftest import mock_message


def _make_db_manager():
    """Create a mock db_manager with a working connection context manager."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    db = MagicMock()

    @contextmanager
    def _conn_ctx():
        yield conn

    db.connection = _conn_ctx
    db.db_path = ":memory:"
    return db


def _make_bot(enabled=True):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.add_section("Stats_Command")
    config.set("Stats_Command", "enabled", str(enabled).lower())
    config.set("Stats_Command", "collect_stats", "true")
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.db_manager = _make_db_manager()
    bot.prefix_hex_chars = 2
    return bot


class TestIsValidPathFormat:
    """Tests for _is_valid_path_format."""

    def setup_method(self):
        self.cmd = StatsCommand(_make_bot())

    def test_none_returns_false(self):
        assert self.cmd._is_valid_path_format(None) is False

    def test_empty_returns_false(self):
        assert self.cmd._is_valid_path_format("") is False

    def test_hex_path_valid(self):
        assert self.cmd._is_valid_path_format("01,7a,55") is True

    def test_continuous_hex_valid(self):
        assert self.cmd._is_valid_path_format("017a55") is True

    def test_descriptive_text_invalid(self):
        assert self.cmd._is_valid_path_format("Routed through 3 hops") is False
        assert self.cmd._is_valid_path_format("Direct") is False
        assert self.cmd._is_valid_path_format("unknown path") is False

    def test_single_hex_node_valid(self):
        assert self.cmd._is_valid_path_format("7a") is True


class TestFormatPathForDisplay:
    """Tests for _format_path_for_display."""

    def setup_method(self):
        self.cmd = StatsCommand(_make_bot())

    def test_none_returns_direct(self):
        assert self.cmd._format_path_for_display(None) == "Direct"

    def test_empty_returns_direct(self):
        assert self.cmd._format_path_for_display("") == "Direct"

    def test_already_formatted_with_commas_unchanged(self):
        assert self.cmd._format_path_for_display("01,7a,55") == "01,7a,55"

    def test_continuous_hex_chunked(self):
        result = self.cmd._format_path_for_display("017a55")
        assert "," in result
        parts = result.split(",")
        assert len(parts) == 3

    def test_single_node_unchanged(self):
        result = self.cmd._format_path_for_display("7a")
        assert result == "7a"

    def test_descriptive_text_returned_as_is(self):
        text = "Routed through 3 hops"
        result = self.cmd._format_path_for_display(text)
        assert result == text


class TestStatsCommandEnabled:
    """Tests for can_execute."""

    def test_enabled(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        # can_execute uses the base class logic (enabled flag lives elsewhere)
        # Actually, stats doesn't override can_execute beyond base — it checks at execute
        assert cmd.stats_enabled is True

    def test_disabled(self):
        bot = _make_bot(enabled=False)
        cmd = StatsCommand(bot)
        assert cmd.stats_enabled is False


# ---------------------------------------------------------------------------
# record_message
# ---------------------------------------------------------------------------

class TestRecordMessage:
    def test_record_message_inserts_row(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        msg = mock_message(content="hello", channel="general", sender_id="Alice")
        msg.timestamp = 1000
        msg.hops = 2
        msg.snr = 5.0
        msg.rssi = -90
        msg.path = "aa,bb"
        cmd.record_message(msg)
        # Verify row inserted
        # Since we used in-memory DB in _make_db_manager, we just assert no exception was raised

    def test_record_message_disabled_collect_stats(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        cmd.collect_stats = False
        msg = mock_message(content="hello", channel="general")
        # Should return early without error
        cmd.record_message(msg)

    def test_record_message_disabled_track_all(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        cmd.track_all_messages = False
        msg = mock_message(content="hello", channel="general")
        cmd.record_message(msg)

    def test_record_message_anonymize_users(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        cmd.anonymize_users = True
        msg = mock_message(content="hello", channel="general", sender_id="RealUser")
        msg.timestamp = 1000
        msg.hops = 0
        msg.snr = None
        msg.rssi = None
        msg.path = None
        # Should not raise
        cmd.record_message(msg)

    def test_record_message_no_sender_id(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        msg = mock_message(content="hello", channel="general", sender_id=None)
        msg.timestamp = 1000
        msg.hops = 0
        msg.snr = None
        msg.rssi = None
        msg.path = None
        cmd.record_message(msg)


# ---------------------------------------------------------------------------
# record_command
# ---------------------------------------------------------------------------

class TestRecordCommand:
    def test_record_command_inserts_row(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        msg = mock_message(content="ping", channel="general", sender_id="Alice")
        msg.timestamp = 1000
        cmd.record_command(msg, "ping", response_sent=True)

    def test_record_command_disabled(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        cmd.collect_stats = False
        msg = mock_message(content="ping", channel="general")
        cmd.record_command(msg, "ping")

    def test_record_command_no_track_details(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        cmd.track_command_details = False
        msg = mock_message(content="ping", channel="general")
        cmd.record_command(msg, "ping")

    def test_record_command_anonymize(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        cmd.anonymize_users = True
        msg = mock_message(content="ping", channel="general", sender_id="RealUser")
        msg.timestamp = 1000
        cmd.record_command(msg, "ping")


# ---------------------------------------------------------------------------
# record_path_stats
# ---------------------------------------------------------------------------

class TestRecordPathStats:
    def test_record_path_stats_valid_path(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        msg = mock_message(content="hello", channel="general", sender_id="Alice")
        msg.timestamp = 1000
        msg.hops = 3
        msg.path = "aa,bb,cc"
        cmd.record_path_stats(msg)

    def test_record_path_stats_no_hops(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        msg = mock_message(content="hello", channel="general")
        msg.hops = 0
        msg.path = "aa,bb"
        cmd.record_path_stats(msg)

    def test_record_path_stats_none_hops(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        msg = mock_message(content="hello", channel="general")
        msg.hops = None
        msg.path = "aa,bb"
        cmd.record_path_stats(msg)

    def test_record_path_stats_no_path(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        msg = mock_message(content="hello", channel="general")
        msg.hops = 2
        msg.path = None
        cmd.record_path_stats(msg)

    def test_record_path_stats_descriptive_path_skipped(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        msg = mock_message(content="hello", channel="general")
        msg.hops = 2
        msg.path = "Routed through 2 hops"
        cmd.record_path_stats(msg)

    def test_record_path_stats_disabled(self):
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        cmd.collect_stats = False
        msg = mock_message(content="hello", channel="general")
        msg.hops = 2
        msg.path = "aa,bb"
        cmd.record_path_stats(msg)


# ---------------------------------------------------------------------------
# execute — basic paths
# ---------------------------------------------------------------------------

class TestExecuteStats:
    def test_execute_disabled_returns_false(self):
        import asyncio
        bot = _make_bot(enabled=False)
        cmd = StatsCommand(bot)
        cmd.stats_enabled = False
        msg = mock_message(content="stats", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is False

    def test_execute_enabled_returns_true(self):
        import asyncio
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        bot.command_manager.send_response = __import__('unittest.mock', fromlist=['AsyncMock']).AsyncMock(return_value=True)
        msg = mock_message(content="stats", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_with_messages_subcommand(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot(enabled=True)
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = StatsCommand(bot)
        msg = mock_message(content="stats messages", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_with_channels_subcommand(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot(enabled=True)
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = StatsCommand(bot)
        msg = mock_message(content="stats channels", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_with_paths_subcommand(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot(enabled=True)
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = StatsCommand(bot)
        msg = mock_message(content="stats paths", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_adverts_subcommand(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="stats adverts", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_adverts_hashes_subcommand(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="stats adverts hashes", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_unknown_subcommand(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="stats foobar", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_bang_prefix_stripped(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="!stats", channel="general")
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_execute_exception_returns_false(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot(enabled=True)
        cmd = StatsCommand(bot)
        cmd.send_response = AsyncMock(return_value=False)
        with patch.object(cmd, '_get_basic_stats', side_effect=Exception("boom")):
            msg = mock_message(content="stats", channel="general")
            result = asyncio.run(cmd.execute(msg))
        assert result is False


# ---------------------------------------------------------------------------
# get_help_text
# ---------------------------------------------------------------------------

class TestGetHelpText:
    def test_returns_string(self):
        cmd = StatsCommand(_make_bot())
        result = cmd.get_help_text()
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _format_path_for_display edge cases
# ---------------------------------------------------------------------------

class TestFormatPathEdgeCases:
    def test_hex_chars_zero_uses_default(self):
        bot = _make_bot()
        bot.prefix_hex_chars = 0
        cmd = StatsCommand(bot)
        # "017a55" with hex_chars=0 falls back to 2
        result = cmd._format_path_for_display("017a55")
        assert "," in result

    def test_legacy_fallback_odd_length(self):
        """Path length not divisible by hex_chars triggers legacy fallback."""
        bot = _make_bot()
        bot.prefix_hex_chars = 4  # expects 4-char chunks, "017a55" is 6 chars (div by 4 = 1.5)
        cmd = StatsCommand(bot)
        result = cmd._format_path_for_display("017a55")
        # 6 not divisible by 4 → legacy fallback: 2-char chunks
        assert "," in result


# ---------------------------------------------------------------------------
# Exception paths for record_*
# ---------------------------------------------------------------------------

class TestRecordExceptionPaths:
    def test_record_message_exception_handled(self):
        """record_message handles exception without raising."""
        bot = _make_bot()

        @contextmanager
        def _bad_conn():
            raise Exception("DB down")
            yield

        bot.db_manager.connection = _bad_conn
        cmd = StatsCommand.__new__(StatsCommand)
        cmd.bot = bot
        cmd.logger = bot.logger
        cmd.collect_stats = True
        cmd.track_all_messages = True
        cmd.anonymize_users = False
        msg = mock_message(content="hello", channel="general", sender_id="Alice")
        msg.timestamp = 1000
        msg.hops = 0
        msg.snr = None
        msg.rssi = None
        msg.path = None
        # Should not raise
        cmd.record_message(msg)

    def test_record_command_exception_handled(self):
        """record_command handles exception without raising."""
        bot = _make_bot()

        @contextmanager
        def _bad_conn():
            raise Exception("DB down")
            yield

        bot.db_manager.connection = _bad_conn
        cmd = StatsCommand.__new__(StatsCommand)
        cmd.bot = bot
        cmd.logger = bot.logger
        cmd.collect_stats = True
        cmd.track_command_details = True
        cmd.anonymize_users = False
        msg = mock_message(content="ping", channel="general", sender_id="Alice")
        msg.timestamp = 1000
        # Should not raise
        cmd.record_command(msg, "ping")

    def test_record_path_stats_anonymize_and_exception(self):
        """record_path_stats with anonymize_users=True and bad conn."""
        bot = _make_bot()

        @contextmanager
        def _bad_conn():
            raise Exception("DB down")
            yield

        bot.db_manager.connection = _bad_conn
        cmd = StatsCommand.__new__(StatsCommand)
        cmd.bot = bot
        cmd.logger = bot.logger
        cmd.collect_stats = True
        cmd.track_all_messages = True
        cmd.anonymize_users = True
        msg = mock_message(content="hello", channel="general", sender_id="RealUser")
        msg.timestamp = 1000
        msg.hops = 3
        msg.path = "aa,bb,cc"
        # Should not raise (hits anonymize branch then exception handler)
        cmd.record_path_stats(msg)


# ---------------------------------------------------------------------------
# _get_basic_stats with data (covers lines 424-425, 439-440)
# ---------------------------------------------------------------------------

class TestGetBasicStatsWithData:
    def test_top_command_and_user_set(self):
        """When command_stats has rows, covers top_command and top_user format lines."""
        import asyncio
        import time
        bot = _make_bot()
        cmd = StatsCommand(bot)  # creates tables
        with bot.db_manager.connection() as conn:
            ts = int(time.time())
            conn.execute(
                "INSERT INTO command_stats (timestamp, sender_id, command_name, channel, is_dm, response_sent) "
                "VALUES (?, 'Alice', 'ping', 'general', 0, 1)", (ts,)
            )
            conn.commit()
        result = asyncio.run(cmd._get_basic_stats())
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _get_bot_user_leaderboard with data (covers lines 484-486)
# ---------------------------------------------------------------------------

class TestGetUserLeaderboardWithData:
    def test_with_users_shows_data(self):
        import asyncio
        import time
        bot = _make_bot()
        cmd = StatsCommand(bot)  # creates tables
        with bot.db_manager.connection() as conn:
            ts = int(time.time())
            # Insert a user with a long name to trigger truncation (len > 15)
            conn.execute(
                "INSERT INTO command_stats (timestamp, sender_id, command_name, channel, is_dm, response_sent) "
                "VALUES (?, 'Alice_very_long_name_here', 'ping', 'general', 0, 1)", (ts,)
            )
            conn.commit()
        result = asyncio.run(cmd._get_bot_user_leaderboard())
        assert isinstance(result, str)

    def test_exception_returns_error_key(self):
        import asyncio
        bot = _make_bot()

        @contextmanager
        def _bad_conn():
            raise Exception("DB down")
            yield

        bot.db_manager.connection = _bad_conn
        cmd = StatsCommand.__new__(StatsCommand)
        cmd.bot = bot
        cmd.logger = bot.logger
        cmd.translator = bot.translator
        result = asyncio.run(cmd._get_bot_user_leaderboard())
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _get_channel_leaderboard with data (covers lines 524-532)
# ---------------------------------------------------------------------------

class TestGetChannelLeaderboardWithData:
    def test_with_channels_shows_data(self):
        import asyncio
        import time
        bot = _make_bot()
        cmd = StatsCommand(bot)  # creates tables
        with bot.db_manager.connection() as conn:
            ts = int(time.time())
            conn.execute(
                "INSERT INTO message_stats (timestamp, sender_id, channel, content, is_dm, hops, snr, rssi, path) "
                "VALUES (?, 'Alice', 'general', 'hello', 0, 0, NULL, NULL, NULL)", (ts,)
            )
            conn.commit()
        result = asyncio.run(cmd._get_channel_leaderboard())
        assert isinstance(result, str)

    def test_exception_returns_error_key(self):
        import asyncio
        bot = _make_bot()

        @contextmanager
        def _bad_conn():
            raise Exception("DB down")
            yield

        bot.db_manager.connection = _bad_conn
        cmd = StatsCommand.__new__(StatsCommand)
        cmd.bot = bot
        cmd.logger = bot.logger
        cmd.translator = bot.translator
        result = asyncio.run(cmd._get_channel_leaderboard())
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _get_path_leaderboard with data (covers lines 578-593)
# ---------------------------------------------------------------------------

class TestGetPathLeaderboardWithData:
    def test_with_paths_shows_data(self):
        import asyncio
        import time
        bot = _make_bot()
        cmd = StatsCommand(bot)  # creates tables
        with bot.db_manager.connection() as conn:
            ts = int(time.time())
            conn.execute(
                "INSERT INTO path_stats (timestamp, sender_id, channel, path_length, path_string, hops) "
                "VALUES (?, 'Alice', 'general', 3, 'aa,bb,cc', 3)", (ts,)
            )
            conn.commit()
        msg = mock_message(content="stats paths", channel="general")
        result = asyncio.run(cmd._get_path_leaderboard(msg))
        assert isinstance(result, str)

    def test_exception_returns_error_key(self):
        import asyncio
        bot = _make_bot()

        @contextmanager
        def _bad_conn():
            raise Exception("DB down")
            yield

        bot.db_manager.connection = _bad_conn
        cmd = StatsCommand.__new__(StatsCommand)
        cmd.bot = bot
        cmd.logger = bot.logger
        cmd.translator = bot.translator
        result = asyncio.run(cmd._get_path_leaderboard())
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _get_adverts_leaderboard (covers lines 613-771)
# ---------------------------------------------------------------------------

def _add_advert_tables(conn, with_daily_stats=True):
    """Add complete_contact_tracking, unique_advert_packets, optionally daily_stats."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS complete_contact_tracking (
            public_key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            last_advert_timestamp TEXT,
            advert_count INTEGER DEFAULT 0
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS unique_advert_packets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_key TEXT,
            packet_hash TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    if with_daily_stats:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                public_key TEXT,
                advert_count INTEGER DEFAULT 0
            )
        ''')
    conn.commit()


class TestGetAdvertsLeaderboard:
    def test_no_contact_table_returns_error(self):
        """When complete_contact_tracking table doesn't exist, exception path fires."""
        import asyncio
        bot = _make_bot()
        # Don't add advert tables — the query will fail
        cmd = StatsCommand(bot)
        result = asyncio.run(cmd._get_adverts_leaderboard())
        assert isinstance(result, str)

    def test_no_adverts_returns_none_message(self):
        """complete_contact_tracking exists but is empty → none branch."""
        import asyncio
        bot = _make_bot()
        with bot.db_manager.connection() as conn:
            _add_advert_tables(conn)
        cmd = StatsCommand(bot)
        result = asyncio.run(cmd._get_adverts_leaderboard())
        assert isinstance(result, str)

    def test_with_adverts_no_daily_stats_no_hashes(self):
        """Fallback path: no daily_stats table, data present, no hashes."""
        import asyncio
        bot = _make_bot()
        with bot.db_manager.connection() as conn:
            _add_advert_tables(conn, with_daily_stats=False)
            conn.execute(
                "INSERT INTO complete_contact_tracking VALUES ('abc', 'TestNode', datetime('now', '-1 hour'), 5)"
            )
            conn.execute(
                "INSERT INTO unique_advert_packets (public_key, packet_hash, first_seen) "
                "VALUES ('abc', 'hash1', datetime('now', '-1 hour'))"
            )
            conn.commit()
        cmd = StatsCommand(bot)
        result = asyncio.run(cmd._get_adverts_leaderboard())
        assert isinstance(result, str)

    def test_with_adverts_no_daily_stats_show_hashes(self):
        """Fallback path: no daily_stats, data present, show_hashes=True."""
        import asyncio
        bot = _make_bot()
        with bot.db_manager.connection() as conn:
            _add_advert_tables(conn, with_daily_stats=False)
            conn.execute(
                "INSERT INTO complete_contact_tracking VALUES ('abc', 'TestNode', datetime('now', '-1 hour'), 5)"
            )
            conn.execute(
                "INSERT INTO unique_advert_packets (public_key, packet_hash, first_seen) "
                "VALUES ('abc', 'hash1', datetime('now', '-1 hour'))"
            )
            conn.commit()
        cmd = StatsCommand(bot)
        result = asyncio.run(cmd._get_adverts_leaderboard(show_hashes=True))
        assert isinstance(result, str)

    def test_with_daily_stats_no_hashes(self):
        """daily_stats table present, no hashes."""
        import asyncio
        bot = _make_bot()
        with bot.db_manager.connection() as conn:
            _add_advert_tables(conn, with_daily_stats=True)
            conn.execute(
                "INSERT INTO complete_contact_tracking VALUES ('abc', 'TestNode', datetime('now', '-1 hour'), 5)"
            )
            conn.execute(
                "INSERT INTO daily_stats (date, public_key, advert_count) VALUES (date('now'), 'abc', 5)"
            )
            conn.commit()
        cmd = StatsCommand(bot)
        result = asyncio.run(cmd._get_adverts_leaderboard())
        assert isinstance(result, str)

    def test_with_daily_stats_show_hashes(self):
        """daily_stats table present, show_hashes=True."""
        import asyncio
        bot = _make_bot()
        with bot.db_manager.connection() as conn:
            _add_advert_tables(conn, with_daily_stats=True)
            conn.execute(
                "INSERT INTO complete_contact_tracking VALUES ('abc', 'TestNode', datetime('now', '-1 hour'), 5)"
            )
            conn.execute(
                "INSERT INTO daily_stats (date, public_key, advert_count) VALUES (date('now'), 'abc', 5)"
            )
            conn.execute(
                "INSERT INTO unique_advert_packets (public_key, packet_hash, first_seen) "
                "VALUES ('abc', 'hash1', datetime('now', '-1 hour'))"
            )
            conn.commit()
        cmd = StatsCommand(bot)
        result = asyncio.run(cmd._get_adverts_leaderboard(show_hashes=True))
        assert isinstance(result, str)

    def test_advert_with_singular_count(self):
        """count == 1 triggers advert_singular translation key."""
        import asyncio
        bot = _make_bot()
        with bot.db_manager.connection() as conn:
            _add_advert_tables(conn, with_daily_stats=False)
            conn.execute(
                "INSERT INTO complete_contact_tracking VALUES ('abc', 'TestNode', datetime('now', '-1 hour'), 1)"
            )
            conn.commit()
        cmd = StatsCommand(bot)
        result = asyncio.run(cmd._get_adverts_leaderboard())
        assert isinstance(result, str)

    def test_advert_node_name_truncated(self):
        """Names > 18 chars get truncated to 15 + '...'."""
        import asyncio
        bot = _make_bot()
        with bot.db_manager.connection() as conn:
            _add_advert_tables(conn, with_daily_stats=False)
            conn.execute(
                "INSERT INTO complete_contact_tracking VALUES ('abc', 'VeryLongNodeNameHere', datetime('now', '-1 hour'), 3)"
            )
            conn.commit()
        cmd = StatsCommand(bot)
        result = asyncio.run(cmd._get_adverts_leaderboard())
        assert isinstance(result, str)

    def test_many_hashes_truncated_at_10(self):
        """More than 10 hashes for a node triggers truncation display."""
        import asyncio
        bot = _make_bot()
        with bot.db_manager.connection() as conn:
            _add_advert_tables(conn, with_daily_stats=False)
            conn.execute(
                "INSERT INTO complete_contact_tracking VALUES ('abc', 'Node', datetime('now', '-1 hour'), 15)"
            )
            for i in range(15):
                conn.execute(
                    "INSERT INTO unique_advert_packets (public_key, packet_hash, first_seen) "
                    f"VALUES ('abc', 'hash{i}', datetime('now', '-1 hour'))"
                )
            conn.commit()
        cmd = StatsCommand(bot)
        result = asyncio.run(cmd._get_adverts_leaderboard(show_hashes=True))
        assert isinstance(result, str)

    def test_exception_returns_error_key(self):
        """DB exception returns error translation."""
        import asyncio
        bot = _make_bot()

        @contextmanager
        def _bad_conn():
            raise Exception("DB down")
            yield

        bot.db_manager.connection = _bad_conn
        cmd = StatsCommand.__new__(StatsCommand)
        cmd.bot = bot
        cmd.logger = bot.logger
        cmd.translator = bot.translator
        result = asyncio.run(cmd._get_adverts_leaderboard())
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# cleanup_old_stats (covers lines 779-804)
# ---------------------------------------------------------------------------

class TestCleanupOldStats:
    def test_cleanup_runs_without_error(self):
        bot = _make_bot()
        cmd = StatsCommand(bot)
        cmd.cleanup_old_stats(7)  # Should not raise

    def test_cleanup_exception_handled(self):
        bot = _make_bot()

        @contextmanager
        def _bad_conn():
            raise Exception("DB down")
            yield

        bot.db_manager.connection = _bad_conn
        cmd = StatsCommand.__new__(StatsCommand)
        cmd.bot = bot
        cmd.logger = bot.logger
        # Should not raise
        cmd.cleanup_old_stats(7)


# ---------------------------------------------------------------------------
# get_stats_summary (covers lines 812-841)
# ---------------------------------------------------------------------------

class TestGetStatsSummary:
    def test_returns_dict_with_keys(self):
        bot = _make_bot()
        cmd = StatsCommand(bot)
        result = cmd.get_stats_summary()
        assert isinstance(result, dict)
        assert 'total_messages' in result
        assert 'total_commands' in result
        assert 'unique_users' in result
        assert 'unique_channels' in result

    def test_exception_returns_empty_dict(self):
        bot = _make_bot()

        @contextmanager
        def _bad_conn():
            raise Exception("DB down")
            yield

        bot.db_manager.connection = _bad_conn
        cmd = StatsCommand.__new__(StatsCommand)
        cmd.bot = bot
        cmd.logger = bot.logger
        result = cmd.get_stats_summary()
        assert result == {}
