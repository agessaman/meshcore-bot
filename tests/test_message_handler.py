"""Tests for MessageHandler pure logic (no network, no meshcore device)."""

import time
import pytest
import configparser
from unittest.mock import Mock, MagicMock

from modules.message_handler import MessageHandler
from modules.models import MeshMessage


@pytest.fixture
def bot(mock_logger):
    """Minimal bot mock for MessageHandler instantiation."""
    bot = Mock()
    bot.logger = mock_logger
    bot.config = configparser.ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "enabled", "true")
    bot.config.set("Bot", "rf_data_timeout", "15.0")
    bot.config.set("Bot", "message_correlation_timeout", "10.0")
    bot.config.set("Bot", "enable_enhanced_correlation", "true")
    bot.config.add_section("Channels")
    bot.config.set("Channels", "respond_to_dms", "true")
    bot.connection_time = None
    bot.prefix_hex_chars = 2
    bot.command_manager = Mock()
    bot.command_manager.monitor_channels = ["general", "test"]
    bot.command_manager.is_user_banned = Mock(return_value=False)
    bot.command_manager.commands = {}
    return bot


@pytest.fixture
def handler(bot):
    return MessageHandler(bot)


# ---------------------------------------------------------------------------
# _is_old_cached_message
# ---------------------------------------------------------------------------

class TestIsOldCachedMessage:
    """Tests for MessageHandler._is_old_cached_message()."""

    def test_no_connection_time_returns_false(self, handler):
        handler.bot.connection_time = None
        assert handler._is_old_cached_message(12345) is False

    def test_none_timestamp_returns_false(self, handler):
        handler.bot.connection_time = time.time()
        assert handler._is_old_cached_message(None) is False

    def test_unknown_timestamp_returns_false(self, handler):
        handler.bot.connection_time = time.time()
        assert handler._is_old_cached_message("unknown") is False

    def test_zero_timestamp_returns_false(self, handler):
        handler.bot.connection_time = time.time()
        assert handler._is_old_cached_message(0) is False

    def test_negative_timestamp_returns_false(self, handler):
        handler.bot.connection_time = time.time()
        assert handler._is_old_cached_message(-1) is False

    def test_old_timestamp_returns_true(self, handler):
        now = time.time()
        handler.bot.connection_time = now
        old = now - 100  # 100 seconds before connection
        assert handler._is_old_cached_message(old) is True

    def test_recent_timestamp_returns_false(self, handler):
        now = time.time()
        handler.bot.connection_time = now
        recent = now + 1  # after connection
        assert handler._is_old_cached_message(recent) is False

    def test_far_future_timestamp_returns_false(self, handler):
        handler.bot.connection_time = time.time()
        future = time.time() + 7200  # 2 hours in future
        assert handler._is_old_cached_message(future) is False

    def test_invalid_string_returns_false(self, handler):
        handler.bot.connection_time = time.time()
        assert handler._is_old_cached_message("not_a_number") is False


# ---------------------------------------------------------------------------
# _path_bytes_to_nodes
# ---------------------------------------------------------------------------

class TestPathBytesToNodes:
    """Tests for MessageHandler._path_bytes_to_nodes()."""

    def test_single_byte_per_hop(self, handler):
        # 3 bytes -> 3 nodes of 2 hex chars each
        path_hex, nodes = handler._path_bytes_to_nodes(bytes.fromhex("017e86"), prefix_hex_chars=2)
        assert path_hex == "017e86"
        assert nodes == ["01", "7E", "86"]

    def test_two_bytes_per_hop(self, handler):
        path_hex, nodes = handler._path_bytes_to_nodes(bytes.fromhex("01027e86"), prefix_hex_chars=4)
        assert nodes == ["0102", "7E86"]

    def test_remainder_falls_back_to_1byte(self, handler):
        # 3 bytes with prefix_hex_chars=4 → remainder, fallback to 1 byte
        path_hex, nodes = handler._path_bytes_to_nodes(bytes.fromhex("017e86"), prefix_hex_chars=4)
        assert nodes == ["01", "7E", "86"]

    def test_empty_bytes(self, handler):
        path_hex, nodes = handler._path_bytes_to_nodes(b"", prefix_hex_chars=2)
        assert path_hex == ""
        # Empty or fallback nodes — no crash expected
        assert isinstance(nodes, list)

    def test_zero_prefix_hex_chars_defaults_to_2(self, handler):
        path_hex, nodes = handler._path_bytes_to_nodes(bytes.fromhex("017e"), prefix_hex_chars=0)
        assert nodes == ["01", "7E"]


# ---------------------------------------------------------------------------
# _path_hex_to_nodes
# ---------------------------------------------------------------------------

class TestPathHexToNodes:
    """Tests for MessageHandler._path_hex_to_nodes()."""

    def test_splits_into_2char_nodes(self, handler):
        handler.bot.prefix_hex_chars = 2
        nodes = handler._path_hex_to_nodes("017e86")
        assert nodes == ["01", "7e", "86"]

    def test_empty_string_returns_empty(self, handler):
        nodes = handler._path_hex_to_nodes("")
        assert nodes == []

    def test_short_string_returns_empty(self, handler):
        nodes = handler._path_hex_to_nodes("0")
        assert nodes == []

    def test_4char_prefix_hex_chars(self, handler):
        handler.bot.prefix_hex_chars = 4
        nodes = handler._path_hex_to_nodes("01027e86")
        assert nodes == ["0102", "7e86"]

    def test_remainder_falls_back_to_2chars(self, handler):
        handler.bot.prefix_hex_chars = 4
        # 6 hex chars (3 bytes) with 4-char chunks → remainder → fallback to 2-char
        nodes = handler._path_hex_to_nodes("017e86")
        assert nodes == ["01", "7e", "86"]


# ---------------------------------------------------------------------------
# _format_path_string
# ---------------------------------------------------------------------------

class TestFormatPathString:
    """Tests for MessageHandler._format_path_string()."""

    def test_empty_path_returns_direct(self, handler):
        assert handler._format_path_string("") == "Direct"

    def test_legacy_single_byte_per_hop(self, handler):
        result = handler._format_path_string("017e86")
        assert result == "01,7e,86"

    def test_with_bytes_per_hop_1(self, handler):
        result = handler._format_path_string("017e86", bytes_per_hop=1)
        assert result == "01,7e,86"

    def test_with_bytes_per_hop_2(self, handler):
        result = handler._format_path_string("01027e86", bytes_per_hop=2)
        assert result == "0102,7e86"

    def test_remainder_with_bytes_per_hop_falls_back(self, handler):
        # 3 bytes (6 hex) with bytes_per_hop=2 → remainder → fallback to 1 byte
        result = handler._format_path_string("017e86", bytes_per_hop=2)
        assert result == "01,7e,86"

    def test_none_path_returns_direct(self, handler):
        assert handler._format_path_string(None) == "Direct"

    def test_invalid_hex_returns_raw(self, handler):
        result = handler._format_path_string("ZZZZ", bytes_per_hop=None)
        # Should not crash; returns "Raw: ..." fallback
        assert "Raw" in result or "ZZ" in result.upper() or result == "Direct"


# ---------------------------------------------------------------------------
# _get_route_type_name
# ---------------------------------------------------------------------------

class TestGetRouteTypeName:
    """Tests for MessageHandler._get_route_type_name()."""

    def test_known_types(self, handler):
        assert handler._get_route_type_name(0x00) == "ROUTE_TYPE_TRANSPORT_FLOOD"
        assert handler._get_route_type_name(0x01) == "ROUTE_TYPE_FLOOD"
        assert handler._get_route_type_name(0x02) == "ROUTE_TYPE_DIRECT"
        assert handler._get_route_type_name(0x03) == "ROUTE_TYPE_TRANSPORT_DIRECT"

    def test_unknown_type(self, handler):
        result = handler._get_route_type_name(0xFF)
        assert "UNKNOWN" in result
        assert "ff" in result


# ---------------------------------------------------------------------------
# get_payload_type_name
# ---------------------------------------------------------------------------

class TestGetPayloadTypeName:
    """Tests for MessageHandler.get_payload_type_name()."""

    def test_known_types(self, handler):
        assert handler.get_payload_type_name(0x00) == "REQ"
        assert handler.get_payload_type_name(0x02) == "TXT_MSG"
        assert handler.get_payload_type_name(0x04) == "ADVERT"
        assert handler.get_payload_type_name(0x05) == "GRP_TXT"
        assert handler.get_payload_type_name(0x08) == "PATH"
        assert handler.get_payload_type_name(0x0F) == "RAW_CUSTOM"

    def test_unknown_type(self, handler):
        result = handler.get_payload_type_name(0xAB)
        assert "UNKNOWN" in result


# ---------------------------------------------------------------------------
# should_process_message
# ---------------------------------------------------------------------------

class TestShouldProcessMessage:
    """Tests for MessageHandler.should_process_message()."""

    def _make_msg(self, channel=None, is_dm=False, sender_id="Alice"):
        return MeshMessage(
            content="hello",
            channel=channel,
            is_dm=is_dm,
            sender_id=sender_id,
        )

    def test_bot_disabled_returns_false(self, handler):
        handler.bot.config.set("Bot", "enabled", "false")
        msg = self._make_msg(channel="general")
        assert handler.should_process_message(msg) is False

    def test_banned_user_returns_false(self, handler):
        handler.bot.command_manager.is_user_banned.return_value = True
        msg = self._make_msg(channel="general")
        assert handler.should_process_message(msg) is False

    def test_monitored_channel_returns_true(self, handler):
        msg = self._make_msg(channel="general")
        assert handler.should_process_message(msg) is True

    def test_unmonitored_channel_returns_false(self, handler):
        msg = self._make_msg(channel="unmonitored")
        assert handler.should_process_message(msg) is False

    def test_dm_enabled_returns_true(self, handler):
        handler.bot.config.set("Channels", "respond_to_dms", "true")
        msg = self._make_msg(is_dm=True)
        assert handler.should_process_message(msg) is True

    def test_dm_disabled_returns_false(self, handler):
        handler.bot.config.set("Channels", "respond_to_dms", "false")
        msg = self._make_msg(is_dm=True)
        assert handler.should_process_message(msg) is False

    def test_command_override_allows_unmonitored_channel(self, handler):
        cmd = Mock()
        cmd.is_channel_allowed = Mock(return_value=True)
        handler.bot.command_manager.commands = {"special": cmd}
        msg = self._make_msg(channel="unmonitored")
        assert handler.should_process_message(msg) is True


# ---------------------------------------------------------------------------
# _cleanup_stale_cache_entries
# ---------------------------------------------------------------------------

class TestCleanupStaleCacheEntries:
    """Tests for MessageHandler._cleanup_stale_cache_entries()."""

    def test_removes_old_timestamp_cache_entries(self, handler):
        now = time.time()
        # Old entry: older than rf_data_timeout
        handler.rf_data_by_timestamp[now - 100] = {"timestamp": now - 100, "data": "old"}
        # Recent entry
        handler.rf_data_by_timestamp[now] = {"timestamp": now, "data": "new"}
        # Force full cleanup
        handler._last_cache_cleanup = 0
        handler._cleanup_stale_cache_entries(current_time=now + handler._cache_cleanup_interval + 1)
        # Old entry should be gone, recent kept
        assert (now - 100) not in handler.rf_data_by_timestamp
        assert now in handler.rf_data_by_timestamp

    def test_removes_stale_pubkey_cache_entries(self, handler):
        now = time.time()
        handler.rf_data_by_pubkey["deadbeef"] = [
            {"timestamp": now - 100, "data": "old"},  # stale
            {"timestamp": now, "data": "new"},         # fresh
        ]
        handler._last_cache_cleanup = 0
        handler._cleanup_stale_cache_entries(current_time=now + handler._cache_cleanup_interval + 1)
        entries = handler.rf_data_by_pubkey.get("deadbeef", [])
        assert all(now - e["timestamp"] < handler.rf_data_timeout for e in entries)

    def test_removes_stale_recent_rf_data(self, handler):
        now = time.time()
        handler.recent_rf_data = [
            {"timestamp": now - 100},
            {"timestamp": now},
        ]
        handler._last_cache_cleanup = 0
        handler._cleanup_stale_cache_entries(current_time=now + handler._cache_cleanup_interval + 1)
        assert all(now - e["timestamp"] < handler.rf_data_timeout for e in handler.recent_rf_data)

    def test_skips_full_cleanup_within_interval(self, handler):
        now = time.time()
        handler._last_cache_cleanup = now  # just cleaned
        # Stale entry in timestamp cache
        stale_ts = now - 100
        handler.rf_data_by_timestamp[stale_ts] = {"timestamp": stale_ts}
        # Call with time just slightly after (within cleanup interval)
        handler._cleanup_stale_cache_entries(current_time=now + 1)
        # Still cleaned (timeout-only cleanup still runs)
        assert stale_ts not in handler.rf_data_by_timestamp
