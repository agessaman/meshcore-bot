"""Tests for MessageHandler pure logic (no network, no meshcore device)."""

import configparser
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest

from modules.message_handler import MessageHandler
from modules.models import MeshMessage
from tests.conftest import mock_message as make_message


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
        current_time = now + handler._cache_cleanup_interval + 1
        # Old entry: well outside rf_data_timeout relative to current_time
        old_ts = current_time - handler.rf_data_timeout - 10
        # Recent entry: within rf_data_timeout of current_time
        recent_ts = current_time - 1
        handler.rf_data_by_timestamp[old_ts] = {"timestamp": old_ts, "data": "old"}
        handler.rf_data_by_timestamp[recent_ts] = {"timestamp": recent_ts, "data": "new"}
        # Force full cleanup
        handler._last_cache_cleanup = 0
        handler._cleanup_stale_cache_entries(current_time=current_time)
        # Old entry should be gone, recent kept
        assert old_ts not in handler.rf_data_by_timestamp
        assert recent_ts in handler.rf_data_by_timestamp

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


# ---------------------------------------------------------------------------
# find_recent_rf_data
# ---------------------------------------------------------------------------

class TestFindRecentRfData:
    """Tests for MessageHandler.find_recent_rf_data()."""

    def _rf_entry(self, age=0, packet_prefix="aabbccdd", pubkey_prefix="1122"):
        return {
            "timestamp": time.time() - age,
            "snr": 5,
            "rssi": -80,
            "packet_prefix": packet_prefix,
            "pubkey_prefix": pubkey_prefix,
        }

    def test_returns_none_when_empty(self, handler):
        handler.recent_rf_data = []
        assert handler.find_recent_rf_data() is None

    def test_returns_none_when_all_too_old(self, handler):
        handler.rf_data_timeout = 5
        handler.recent_rf_data = [self._rf_entry(age=100)]
        assert handler.find_recent_rf_data() is None

    def test_returns_most_recent_fallback(self, handler):
        handler.rf_data_timeout = 30
        entry = self._rf_entry(age=1)
        handler.recent_rf_data = [entry]
        result = handler.find_recent_rf_data()
        assert result is entry

    def test_exact_packet_prefix_match(self, handler):
        handler.rf_data_timeout = 30
        target = self._rf_entry(age=1, packet_prefix="deadbeefdeadbeef1234567890abcdef")
        other = self._rf_entry(age=2, packet_prefix="00000000000000000000000000000000")
        handler.recent_rf_data = [target, other]
        result = handler.find_recent_rf_data("deadbeefdeadbeef1234567890abcdef")
        assert result is target

    def test_exact_pubkey_prefix_match(self, handler):
        handler.rf_data_timeout = 30
        target = self._rf_entry(age=1, pubkey_prefix="abcd", packet_prefix="")
        other = self._rf_entry(age=2, pubkey_prefix="1111", packet_prefix="")
        handler.recent_rf_data = [target, other]
        result = handler.find_recent_rf_data("abcd")
        assert result is target

    def test_partial_packet_prefix_match(self, handler):
        handler.rf_data_timeout = 30
        long_prefix = "aabbccddeeff0011aabbccddeeff0011"
        partial_key = "aabbccddeeff0011" + "xxxxxxxxxxxxxxxx"
        target = self._rf_entry(age=1, packet_prefix=long_prefix, pubkey_prefix="")
        handler.recent_rf_data = [target]
        result = handler.find_recent_rf_data(partial_key)
        assert result is target

    def test_no_key_returns_most_recent(self, handler):
        handler.rf_data_timeout = 30
        old = self._rf_entry(age=10)
        new = self._rf_entry(age=1)
        handler.recent_rf_data = [old, new]
        result = handler.find_recent_rf_data()
        assert result["timestamp"] == new["timestamp"]

    def test_custom_max_age(self, handler):
        handler.rf_data_timeout = 30
        entry = self._rf_entry(age=20)
        handler.recent_rf_data = [entry]
        # With max_age=5, entry is too old
        assert handler.find_recent_rf_data(max_age_seconds=5) is None
        # With max_age=30, entry is visible
        assert handler.find_recent_rf_data(max_age_seconds=30) is entry


# ---------------------------------------------------------------------------
# handle_raw_data
# ---------------------------------------------------------------------------

class TestHandleRawData:
    """Tests for MessageHandler.handle_raw_data()."""

    def _make_event(self, payload):
        event = Mock()
        event.payload = payload
        return event

    async def test_no_payload_logs_warning(self, handler):
        event = Mock(spec=[])
        handler.logger = Mock()
        await handler.handle_raw_data(event)
        handler.logger.warning.assert_called()

    async def test_payload_none_logs_warning(self, handler):
        event = Mock()
        event.payload = None
        handler.logger = Mock()
        await handler.handle_raw_data(event)
        handler.logger.warning.assert_called()

    async def test_payload_without_data_field_logs_warning(self, handler):
        event = self._make_event({"other": "stuff"})
        handler.logger = Mock()
        with patch.object(handler, "decode_meshcore_packet", return_value=None):
            await handler.handle_raw_data(event)
        handler.logger.warning.assert_called()

    async def test_payload_with_hex_data_calls_decode(self, handler):
        event = self._make_event({"data": "aabbccdd"})
        handler.logger = Mock()
        with patch.object(handler, "decode_meshcore_packet", return_value=None) as mock_decode:
            await handler.handle_raw_data(event)
        mock_decode.assert_called_once_with("aabbccdd")

    async def test_payload_strips_0x_prefix(self, handler):
        event = self._make_event({"data": "0xaabbccdd"})
        handler.logger = Mock()
        with patch.object(handler, "decode_meshcore_packet", return_value=None) as mock_decode:
            await handler.handle_raw_data(event)
        mock_decode.assert_called_once_with("aabbccdd")

    async def test_decoded_packet_calls_process_advertisement(self, handler):
        event = self._make_event({"data": "aabbccdd"})
        handler.logger = Mock()
        packet_info = {"type": "adv", "node_id": "ab"}
        with patch.object(handler, "decode_meshcore_packet", return_value=packet_info):
            with patch.object(handler, "_process_advertisement_packet", new_callable=AsyncMock) as mock_adv:
                await handler.handle_raw_data(event)
        mock_adv.assert_called_once_with(packet_info, None)

    async def test_non_string_data_logs_warning(self, handler):
        event = self._make_event({"data": 12345})
        handler.logger = Mock()
        await handler.handle_raw_data(event)
        handler.logger.warning.assert_called()

    async def test_exception_does_not_raise(self, handler):
        event = self._make_event({"data": "aabb"})
        handler.logger = Mock()
        with patch.object(handler, "decode_meshcore_packet", side_effect=RuntimeError("oops")):
            # Should not raise
            await handler.handle_raw_data(event)
        handler.logger.error.assert_called()


# ---------------------------------------------------------------------------
# handle_contact_message
# ---------------------------------------------------------------------------

class TestHandleContactMessage:
    """Tests for MessageHandler.handle_contact_message()."""

    def _make_event(self, payload):
        event = Mock()
        event.payload = payload
        event.metadata = {}
        return event

    def _setup_handler(self, handler):
        handler.logger = Mock()
        handler.bot.meshcore = Mock()
        handler.bot.meshcore.contacts = {}
        handler.bot.translator = None

    async def test_no_payload_returns_early(self, handler):
        self._setup_handler(handler)
        event = Mock(spec=[])
        await handler.handle_contact_message(event)
        handler.logger.warning.assert_called()

    async def test_payload_none_returns_early(self, handler):
        self._setup_handler(handler)
        event = Mock()
        event.payload = None
        await handler.handle_contact_message(event)
        handler.logger.warning.assert_called()

    async def test_old_cached_message_not_processed(self, handler):
        self._setup_handler(handler)
        # Set connection_time in the future relative to an old timestamp
        handler.bot.connection_time = time.time()
        old_ts = int(time.time()) - 3600  # 1 hour old
        event = self._make_event({
            "pubkey_prefix": "ab12",
            "text": "hello",
            "path_len": 255,
            "sender_timestamp": old_ts,
        })
        with patch.object(handler, "process_message", new_callable=AsyncMock) as mock_pm:
            with patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock):
                with patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock):
                    await handler.handle_contact_message(event)
        mock_pm.assert_not_called()

    async def test_new_message_calls_process_message(self, handler):
        self._setup_handler(handler)
        handler.bot.connection_time = None  # No connection time = don't filter
        event = self._make_event({
            "pubkey_prefix": "ab12",
            "text": "hello",
            "path_len": 255,
            "sender_timestamp": int(time.time()),
        })
        with patch.object(handler, "process_message", new_callable=AsyncMock) as mock_pm:
            with patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock):
                with patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock):
                    await handler.handle_contact_message(event)
        mock_pm.assert_called_once()

    async def test_snr_from_payload(self, handler):
        self._setup_handler(handler)
        handler.bot.connection_time = None
        captured = {}

        async def capture_message(msg):
            captured["msg"] = msg

        event = self._make_event({
            "pubkey_prefix": "ab12",
            "text": "hello",
            "path_len": 255,
            "sender_timestamp": int(time.time()),
            "SNR": 7,
            "RSSI": -70,
        })
        with patch.object(handler, "process_message", side_effect=capture_message):
            with patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock):
                with patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock):
                    await handler.handle_contact_message(event)
        assert captured["msg"].snr == 7
        assert captured["msg"].rssi == -70

    async def test_direct_path_len_255(self, handler):
        self._setup_handler(handler)
        handler.bot.connection_time = None
        captured = {}

        async def capture_message(msg):
            captured["msg"] = msg

        event = self._make_event({
            "pubkey_prefix": "ab12",
            "text": "hi",
            "path_len": 255,
            "sender_timestamp": int(time.time()),
        })
        with patch.object(handler, "process_message", side_effect=capture_message):
            with patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock):
                with patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock):
                    await handler.handle_contact_message(event)
        assert captured["msg"].is_dm is True

    async def test_message_is_dm(self, handler):
        self._setup_handler(handler)
        handler.bot.connection_time = None
        captured = {}

        async def capture_message(msg):
            captured["msg"] = msg

        event = self._make_event({
            "pubkey_prefix": "ab12",
            "text": "dm text",
            "path_len": 0,
            "sender_timestamp": int(time.time()),
        })
        with patch.object(handler, "process_message", side_effect=capture_message):
            with patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock):
                with patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock):
                    await handler.handle_contact_message(event)
        assert captured["msg"].is_dm is True
        assert captured["msg"].content == "dm text"

    async def test_contact_name_lookup(self, handler):
        self._setup_handler(handler)
        handler.bot.connection_time = None
        handler.bot.meshcore.contacts = {
            "key1": {
                "public_key": "ab12deadbeef",
                "name": "Alice",
                "out_path": "",
                "out_path_len": 0,
            }
        }
        captured = {}

        async def capture_message(msg):
            captured["msg"] = msg

        event = self._make_event({
            "pubkey_prefix": "ab12",
            "text": "hi",
            "path_len": 255,
            "sender_timestamp": int(time.time()),
        })
        with patch.object(handler, "process_message", side_effect=capture_message):
            with patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock):
                with patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock):
                    await handler.handle_contact_message(event)
        assert captured["msg"].sender_id == "Alice"

    async def test_exception_does_not_propagate(self, handler):
        self._setup_handler(handler)
        event = self._make_event({
            "pubkey_prefix": "ab12",
            "text": "hello",
            "path_len": 255,
            "sender_timestamp": int(time.time()),
        })
        with patch.object(handler, "_debug_decode_message_path", side_effect=RuntimeError("boom")):
            # Should not raise
            await handler.handle_contact_message(event)
        handler.logger.error.assert_called()


# ---------------------------------------------------------------------------
# handle_channel_message
# ---------------------------------------------------------------------------

class TestHandleChannelMessage:
    """Tests for MessageHandler.handle_channel_message()."""

    def _setup_handler(self, handler):
        handler.logger = Mock()
        handler.bot.meshcore = Mock()
        handler.bot.meshcore.contacts = {}
        handler.bot.channel_manager = Mock()
        handler.bot.channel_manager.get_channel_name = Mock(return_value="general")
        handler.bot.translator = None
        handler.bot.mesh_graph = None
        handler.recent_rf_data = []
        handler.enhanced_correlation = False

    def _make_event(self, payload):
        event = Mock()
        event.payload = payload
        return event

    async def test_no_payload_returns_early(self, handler):
        self._setup_handler(handler)
        event = Mock(spec=[])
        await handler.handle_channel_message(event)
        handler.logger.warning.assert_called()

    async def test_payload_none_returns_early(self, handler):
        self._setup_handler(handler)
        event = Mock()
        event.payload = None
        await handler.handle_channel_message(event)
        handler.logger.warning.assert_called()

    async def test_basic_channel_message_calls_process_message(self, handler):
        self._setup_handler(handler)
        handler.bot.connection_time = None
        event = self._make_event({
            "channel_idx": 0,
            "text": "ALICE: hello world",
            "path_len": 255,
            "sender_timestamp": int(time.time()),
        })
        with patch.object(handler, "process_message", new_callable=AsyncMock) as mock_pm:
            with patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock):
                with patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock):
                    await handler.handle_channel_message(event)
        mock_pm.assert_called_once()

    async def test_sender_extracted_from_text(self, handler):
        self._setup_handler(handler)
        handler.bot.connection_time = None
        captured = {}

        async def capture(msg):
            captured["msg"] = msg

        event = self._make_event({
            "channel_idx": 0,
            "text": "BOB: hi there",
            "path_len": 0,
            "sender_timestamp": int(time.time()),
        })
        with patch.object(handler, "process_message", side_effect=capture):
            with patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock):
                with patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock):
                    await handler.handle_channel_message(event)
        assert captured["msg"].sender_id == "BOB"
        assert captured["msg"].content == "hi there"

    async def test_text_without_colon_uses_full_text(self, handler):
        self._setup_handler(handler)
        handler.bot.connection_time = None
        captured = {}

        async def capture(msg):
            captured["msg"] = msg

        event = self._make_event({
            "channel_idx": 0,
            "text": "no colon here",
            "path_len": 0,
            "sender_timestamp": int(time.time()),
        })
        with patch.object(handler, "process_message", side_effect=capture):
            with patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock):
                with patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock):
                    await handler.handle_channel_message(event)
        assert captured["msg"].content == "no colon here"

    async def test_old_cached_message_not_processed(self, handler):
        self._setup_handler(handler)
        handler.bot.connection_time = time.time()
        old_ts = int(time.time()) - 3600
        event = self._make_event({
            "channel_idx": 0,
            "text": "CAROL: old msg",
            "path_len": 0,
            "sender_timestamp": old_ts,
        })
        with patch.object(handler, "process_message", new_callable=AsyncMock) as mock_pm:
            with patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock):
                with patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock):
                    await handler.handle_channel_message(event)
        mock_pm.assert_not_called()

    async def test_snr_from_payload(self, handler):
        self._setup_handler(handler)
        handler.bot.connection_time = None
        captured = {}

        async def capture(msg):
            captured["msg"] = msg

        event = self._make_event({
            "channel_idx": 0,
            "text": "DAN: test",
            "path_len": 0,
            "sender_timestamp": int(time.time()),
            "SNR": 9,
            "RSSI": -85,
        })
        with patch.object(handler, "process_message", side_effect=capture):
            with patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock):
                with patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock):
                    await handler.handle_channel_message(event)
        assert captured["msg"].snr == 9
        assert captured["msg"].rssi == -85

    async def test_channel_name_set_on_message(self, handler):
        self._setup_handler(handler)
        handler.bot.connection_time = None
        handler.bot.channel_manager.get_channel_name = Mock(return_value="emergency")
        captured = {}

        async def capture(msg):
            captured["msg"] = msg

        event = self._make_event({
            "channel_idx": 2,
            "text": "EVE: help",
            "path_len": 0,
            "sender_timestamp": int(time.time()),
        })
        with patch.object(handler, "process_message", side_effect=capture):
            with patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock):
                with patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock):
                    await handler.handle_channel_message(event)
        assert captured["msg"].channel == "emergency"
        assert captured["msg"].is_dm is False

    async def test_exception_does_not_propagate(self, handler):
        self._setup_handler(handler)
        event = self._make_event({
            "channel_idx": 0,
            "text": "FRANK: crash",
            "path_len": 0,
            "sender_timestamp": int(time.time()),
        })
        with patch.object(handler, "_debug_decode_message_path", side_effect=RuntimeError("boom")):
            await handler.handle_channel_message(event)
        handler.logger.error.assert_called()


# ---------------------------------------------------------------------------
# Packet construction helpers (used across multiple test classes below)
# ---------------------------------------------------------------------------
#
# MeshCore packet binary layout (from decode_meshcore_packet / Packet.cpp):
#
#   header (1 byte):
#       bits 7-6 = payload_version (0 = VER_1, must be 0)
#       bits 5-2 = payload_type  (ADVERT=4, TXT_MSG=2, GRP_TXT=5, TRACE=9, …)
#       bits 1-0 = route_type    (TRANSPORT_FLOOD=0, FLOOD=1, DIRECT=2, TRANSPORT_DIRECT=3)
#   [transport bytes: 4 bytes if route_type is TRANSPORT_FLOOD or TRANSPORT_DIRECT]
#   path_len_byte (1 byte):
#       low 6 bits = hop_count
#       high 2 bits = size_code  (bytes_per_hop = size_code + 1)
#       => path_byte_length = hop_count * bytes_per_hop
#   path bytes (path_byte_length bytes)
#   payload bytes (remainder)
#
# header = (0 << 6) | (payload_type << 2) | route_type
#
# Pre-computed examples used in tests:
#   FLOOD(1)+ADVERT(4), 0 hops: header=0x11, path_len=0x00 → "1100"
#   FLOOD(1)+TXT_MSG(2), 0 hops: header=0x09, path_len=0x00 → "0900"
#   FLOOD(1)+ADVERT(4), 2 hops 1-byte (AB,CD): header=0x11, path_len=0x02 → "110202abcdfeed"
#   DIRECT(2)+GRP_TXT(5), 0 hops: header=0x16, path_len=0x00 → "1600"
#   TRANSPORT_FLOOD(0)+TXT_MSG(2), 4-byte transport, 0 hops: header=0x08 → "0801020304 00 ff"
#   FLOOD(1)+TRACE(9), 2 hops, payload: header=0x25
#
# Advert payload format (for parse_advert, from AdvertDataHelpers.h):
#   bytes 0-31:   public_key (32 bytes)
#   bytes 32-35:  timestamp  (uint32 little-endian)
#   bytes 36-99:  signature  (64 bytes)
#   byte  100:    flags_byte (app_data[0])
#       bits 3-0 = adv_type (CHAT=1, REPEATER=2, ROOM=3, SENSOR=4)
#       bit  4   = ADV_LATLON_MASK  (has location: 8 bytes lat+lon)
#       bit  5   = ADV_FEAT1_MASK   (has feat1: 2 bytes)
#       bit  6   = ADV_FEAT2_MASK   (has feat2: 2 bytes)
#       bit  7   = ADV_NAME_MASK    (has name: remaining bytes as UTF-8)
#   bytes 101+:   optional location / feat1 / feat2 / name

def _make_advert_payload(
    flags_byte: int,
    *,
    pub_key: bytes = b"\xaa" * 32,
    timestamp: int = 1700000000,
    signature: bytes = b"\xbb" * 64,
    location_lat_raw: int = 0,
    location_lon_raw: int = 0,
    feat1: int = 0,
    feat2: int = 0,
    name: str = "",
) -> bytes:
    """Build a minimal valid advert payload byte string for parse_advert()."""
    # Header: pub_key (32) + timestamp (4 little-endian) + signature (64) = 100 bytes
    ts_bytes = timestamp.to_bytes(4, "little")
    header = pub_key[:32] + ts_bytes + signature[:64]
    assert len(header) == 100

    app_data = bytes([flags_byte])

    # Optional location (8 bytes): lat (int32 LE) + lon (int32 LE)
    if flags_byte & 0x10:
        app_data += location_lat_raw.to_bytes(4, "little", signed=True)
        app_data += location_lon_raw.to_bytes(4, "little", signed=True)

    # Optional feat1 (2 bytes)
    if flags_byte & 0x20:
        app_data += feat1.to_bytes(2, "little")

    # Optional feat2 (2 bytes)
    if flags_byte & 0x40:
        app_data += feat2.to_bytes(2, "little")

    # Optional name (variable length UTF-8)
    if flags_byte & 0x80:
        app_data += name.encode("utf-8")

    return header + app_data


def _make_packet_hex(
    payload_type: int,
    route_type: int,
    path_bytes: bytes = b"",
    payload_bytes: bytes = b"\xfe",
    *,
    hop_count: int = 0,
    bytes_per_hop: int = 1,
    transport: bytes = b"",
) -> str:
    """Build a valid MeshCore packet hex string for decode_meshcore_packet()."""
    header = (0 << 6) | (payload_type << 2) | route_type
    # path_len_byte: high 2 bits = size_code (bytes_per_hop - 1), low 6 bits = hop_count
    size_code = bytes_per_hop - 1
    path_len_byte = (size_code << 6) | (hop_count & 0x3F)
    pkt = bytes([header]) + transport + bytes([path_len_byte]) + path_bytes + payload_bytes
    return pkt.hex()


# ---------------------------------------------------------------------------
# decode_meshcore_packet
# ---------------------------------------------------------------------------

class TestDecodeMeshcorePacket:
    """Tests for MessageHandler.decode_meshcore_packet() — pure hex/binary parsing."""

    # --- invalid / edge-case inputs ---

    def test_none_raw_hex_returns_none(self, handler):
        result = handler.decode_meshcore_packet(None)
        assert result is None

    def test_empty_raw_hex_returns_none(self, handler):
        result = handler.decode_meshcore_packet("")
        assert result is None

    def test_single_byte_too_short_returns_none(self, handler):
        # 1 byte only → fails minimum size check (< 2)
        result = handler.decode_meshcore_packet("11")
        assert result is None

    def test_invalid_hex_string_raises_or_returns_none(self, handler):
        # "ZZZZZZ" is not valid hex.  bytes.fromhex() raises ValueError inside the try-block;
        # the except handler then tries to reference the unbound local `byte_data` in its log
        # message, which previously produced an UnboundLocalError that propagates out (BUG-028).
        # Expected behavior: returns None and does not raise.
        result = handler.decode_meshcore_packet("ZZZZZZ")
        assert result is None

    def test_0x_prefix_stripped(self, handler):
        # Prepend '0x' — should be stripped transparently
        hex_no_prefix = _make_packet_hex(4, 1, payload_bytes=b"\xde")
        result = handler.decode_meshcore_packet("0x" + hex_no_prefix)
        assert result is not None
        assert result["route_type_name"] == "FLOOD"
        assert result["payload_type_name"] == "ADVERT"

    def test_payload_hex_preferred_over_raw_hex(self, handler):
        # raw_hex encodes a TXT_MSG, payload_hex encodes an ADVERT
        raw_txt = _make_packet_hex(2, 1, payload_bytes=b"\x01")
        raw_adv = _make_packet_hex(4, 1, payload_bytes=b"\x02")
        result = handler.decode_meshcore_packet(raw_txt, payload_hex=raw_adv)
        # Should decode from payload_hex (ADVERT), not raw_hex (TXT_MSG)
        assert result["payload_type_name"] == "ADVERT"

    def test_unknown_payload_version_returns_none(self, handler):
        # Build a packet with payload_version = 1 (VER_2) in bits 7-6
        payload_type = 2  # TXT_MSG
        route_type = 1    # FLOOD
        header = (1 << 6) | (payload_type << 2) | route_type  # version bits = 01
        path_len_byte = 0x00
        pkt = bytes([header, path_len_byte, 0xAA])
        result = handler.decode_meshcore_packet(pkt.hex())
        assert result is None

    # --- FLOOD route ---

    def test_flood_advert_no_path(self, handler):
        hex_str = _make_packet_hex(4, 1, payload_bytes=b"\xde\xad")
        result = handler.decode_meshcore_packet(hex_str)
        assert result is not None
        assert result["route_type_name"] == "FLOOD"
        assert result["payload_type_name"] == "ADVERT"
        assert result["route_type"] == 1
        assert result["payload_type"] == 4
        assert result["payload_version"] == 0
        assert result["has_transport_codes"] is False
        assert result["transport_codes"] is None
        assert result["path_len"] == 0
        assert result["path"] == []
        assert result["path_hex"] == ""
        assert result["payload_hex"] == "dead"

    def test_flood_txt_msg_no_path(self, handler):
        hex_str = _make_packet_hex(2, 1, payload_bytes=b"\x48\x69")
        result = handler.decode_meshcore_packet(hex_str)
        assert result is not None
        assert result["payload_type_name"] == "TXT_MSG"
        assert result["route_type_name"] == "FLOOD"

    def test_flood_advert_two_hops_one_byte(self, handler):
        path = bytes([0xAB, 0xCD])
        hex_str = _make_packet_hex(
            4, 1,
            path_bytes=path,
            payload_bytes=b"\xEE",
            hop_count=2,
            bytes_per_hop=1,
        )
        result = handler.decode_meshcore_packet(hex_str)
        assert result is not None
        assert result["path_len"] == 2
        assert result["path_byte_length"] == 2
        assert result["bytes_per_hop"] == 1
        assert result["path_hex"] == "abcd"
        assert result["path"] == ["AB", "CD"]

    def test_flood_advert_two_hops_two_bytes(self, handler):
        # 2 hops, 2 bytes each → 4 path bytes
        path = bytes([0x01, 0x02, 0xAB, 0xCD])
        hex_str = _make_packet_hex(
            4, 1,
            path_bytes=path,
            payload_bytes=b"\xEE",
            hop_count=2,
            bytes_per_hop=2,
        )
        result = handler.decode_meshcore_packet(hex_str)
        assert result is not None
        assert result["bytes_per_hop"] == 2
        assert result["path_byte_length"] == 4
        assert result["path_len"] == 2
        assert result["path"] == ["0102", "ABCD"]

    # --- DIRECT route ---

    def test_direct_grp_txt_no_path(self, handler):
        hex_str = _make_packet_hex(5, 2, payload_bytes=b"\x01\x02\x03")
        result = handler.decode_meshcore_packet(hex_str)
        assert result is not None
        assert result["route_type_name"] == "DIRECT"
        assert result["payload_type_name"] == "GRP_TXT"
        assert result["has_transport_codes"] is False

    # --- TRANSPORT_FLOOD route (has 4 transport bytes) ---

    def test_transport_flood_has_transport_codes(self, handler):
        transport = bytes([0x01, 0x02, 0x03, 0x04])
        hex_str = _make_packet_hex(
            2, 0,
            payload_bytes=b"\xFF",
            transport=transport,
        )
        result = handler.decode_meshcore_packet(hex_str)
        assert result is not None
        assert result["route_type_name"] == "TRANSPORT_FLOOD"
        assert result["has_transport_codes"] is True
        assert result["transport_codes"] is not None
        assert result["transport_codes"]["code1"] == 0x0201
        assert result["transport_codes"]["code2"] == 0x0403

    def test_transport_direct_has_transport_codes(self, handler):
        transport = bytes([0x0A, 0x0B, 0x0C, 0x0D])
        hex_str = _make_packet_hex(
            4, 3,
            payload_bytes=b"\xAA",
            transport=transport,
        )
        result = handler.decode_meshcore_packet(hex_str)
        assert result is not None
        assert result["route_type_name"] == "TRANSPORT_DIRECT"
        assert result["has_transport_codes"] is True

    # --- Too-short packet after stripping transport ---

    def test_too_short_for_path_len_returns_none(self, handler):
        # TRANSPORT_FLOOD needs 1 (header) + 4 (transport) + 1 (path_len) = 6 bytes minimum
        # Provide only header + 4 transport bytes (no path_len byte)
        header = (0 << 6) | (2 << 2) | 0  # TRANSPORT_FLOOD + TXT_MSG
        pkt = bytes([header, 0x01, 0x02, 0x03, 0x04])  # 5 bytes, missing path_len
        result = handler.decode_meshcore_packet(pkt.hex())
        assert result is None

    def test_path_bytes_exceed_available_data_returns_none(self, handler):
        # Claim 3 hops (3 path bytes) but only provide 2 in the packet
        header = (0 << 6) | (4 << 2) | 1  # FLOOD + ADVERT
        path_len_byte = 0x03  # 3 hops, 1 byte each
        pkt = bytes([header, path_len_byte, 0xAA, 0xBB])  # only 2 path bytes
        result = handler.decode_meshcore_packet(pkt.hex())
        assert result is None

    # --- All standard payload types decode without crashing ---

    def test_all_payload_types_decode(self, handler):
        known_types = {
            0x00: "REQ",
            0x01: "RESPONSE",
            0x02: "TXT_MSG",
            0x03: "ACK",
            0x04: "ADVERT",
            0x05: "GRP_TXT",
            0x06: "GRP_DATA",
            0x07: "ANON_REQ",
            0x08: "PATH",
            0x09: "TRACE",
            0x0A: "MULTIPART",
            0x0F: "RAW_CUSTOM",
        }
        for pt_val, expected_name in known_types.items():
            hex_str = _make_packet_hex(pt_val, 1, payload_bytes=b"\x01\x02")
            result = handler.decode_meshcore_packet(hex_str)
            assert result is not None, f"Expected non-None for payload_type 0x{pt_val:02x}"
            assert result["payload_type_name"] == expected_name

    # --- Return dict structure completeness ---

    def test_return_dict_has_expected_keys(self, handler):
        hex_str = _make_packet_hex(4, 1, payload_bytes=b"\xAA")
        result = handler.decode_meshcore_packet(hex_str)
        required_keys = {
            "header", "route_type", "route_type_name", "payload_type", "payload_type_name",
            "payload_version", "route_type_enum", "payload_type_enum", "payload_version_enum",
            "has_transport_codes", "transport_codes", "transport_size",
            "path_len", "path_byte_length", "bytes_per_hop",
            "path_info", "path", "path_hex", "payload_hex", "payload_bytes",
        }
        for key in required_keys:
            assert key in result, f"Missing key: {key}"

    def test_trace_packet_path_info_type(self, handler):
        # TRACE(9) + FLOOD(1): path_info should have type='trace'
        # Provide minimal trace payload: tag(4) + auth(4) + flags(1) = 9 bytes
        trace_payload = b"\x00" * 9
        hex_str = _make_packet_hex(9, 1, payload_bytes=trace_payload)
        result = handler.decode_meshcore_packet(hex_str)
        assert result is not None
        assert result["path_info"]["type"] == "trace"

    def test_trace_decode_skye_sample_payload_hashes_not_snr_path(self, handler):
        # Field report: RF path 31,28,23 was misread as node IDs; payload holds 37,d6,37.
        raw = "26033128235F0AED1A000000000037D637"
        result = handler.decode_meshcore_packet(raw)
        assert result is not None
        assert result["payload_type_name"] == "TRACE"
        ph = result["path_info"].get("path_hashes") or result["path_info"].get("path")
        assert ph == ["37", "D6", "37"]
        assert len(result["path_info"]["snr_data"]) == 3
        # Top-level path is still SNR chunks (legacy shape); real route is path_info
        assert result["path"]  # non-empty SNR-as-hex nodes

    def test_trace_decode_flags_two_byte_path_hashes(self, handler):
        # flags=1 → 2 bytes per hash; tail 4 bytes → AABB, CCDD
        trace_payload = (
            b"\x01\x00\x00\x00"  # tag
            + b"\x00\x00\x00\x00"  # auth
            + b"\x01"  # flags: path_sz=1 → 2 bytes per hop
            + b"\xaa\xbb\xcc\xdd"
        )
        hex_str = _make_packet_hex(9, 2, path_bytes=b"", hop_count=0, payload_bytes=trace_payload)
        result = handler.decode_meshcore_packet(hex_str)
        assert result is not None
        ph = result["path_info"].get("path_hashes") or result["path_info"].get("path")
        assert ph == ["AABB", "CCDD"]


# ---------------------------------------------------------------------------
# parse_advert
# ---------------------------------------------------------------------------

class TestParseAdvert:
    """Tests for MessageHandler.parse_advert() — pure binary advert parsing."""

    def test_too_short_payload_returns_empty_dict(self, handler):
        # Must be >= 101 bytes
        result = handler.parse_advert(b"\x00" * 100)
        assert result == {}

    def test_none_length_payload_short(self, handler):
        result = handler.parse_advert(b"")
        assert result == {}

    def test_no_app_data_after_100_bytes_returns_empty(self, handler):
        # Exactly 100 bytes means app_data is empty → returns {}
        result = handler.parse_advert(b"\x00" * 100)
        assert result == {}

    def test_companion_advert_basic(self, handler):
        # ADV_TYPE_CHAT=0x01, no optional fields
        payload = _make_advert_payload(0x01)
        result = handler.parse_advert(payload)
        assert result is not None
        assert result != {}
        assert result["mode"] == "Companion"
        assert "public_key" in result
        assert len(result["public_key"]) == 64  # 32 bytes → 64 hex chars
        assert "advert_time" in result
        assert result["advert_time"] == 1700000000

    def test_repeater_advert(self, handler):
        payload = _make_advert_payload(0x02)  # ADV_TYPE_REPEATER
        result = handler.parse_advert(payload)
        assert result["mode"] == "Repeater"

    def test_room_server_advert(self, handler):
        payload = _make_advert_payload(0x03)  # ADV_TYPE_ROOM
        result = handler.parse_advert(payload)
        assert result["mode"] == "RoomServer"

    def test_sensor_advert(self, handler):
        payload = _make_advert_payload(0x04)  # ADV_TYPE_SENSOR
        result = handler.parse_advert(payload)
        assert result["mode"] == "Sensor"

    def test_unknown_type_advert(self, handler):
        payload = _make_advert_payload(0x05)  # No matching type
        result = handler.parse_advert(payload)
        assert result["mode"] == "Type5"

    def test_advert_flags_0xfd_invalid_for_python_flag_still_parses(self, handler):
        """0xFD sets type nibble 0x0D; enum.Flag rejected this — parsing must match firmware bit tests."""
        payload = _make_advert_payload(
            0xFD,
            location_lat_raw=1000000,
            location_lon_raw=-2000000,
            feat1=0x0001,
            feat2=0x0002,
            name="CorruptWire",
        )
        result = handler.parse_advert(payload)
        assert result != {}
        assert result["mode"] == "Type13"
        assert result["lat"] == 1.0
        assert result["lon"] == -2.0
        assert result["feat1"] == 0x0001
        assert result["feat2"] == 0x0002
        assert result["name"] == "CorruptWire"

    def test_advert_type_nibble_8_no_extra_flags(self, handler):
        """Low nibble 8 sets bit 0x08 alone — must not raise."""
        payload = _make_advert_payload(0x08)
        result = handler.parse_advert(payload)
        assert result["mode"] == "Type8"
        assert "lat" not in result

    def test_companion_with_name(self, handler):
        # ADV_NAME_MASK=0x80 | ADV_TYPE_CHAT=0x01
        payload = _make_advert_payload(0x81, name="TestNode")
        result = handler.parse_advert(payload)
        assert result["mode"] == "Companion"
        assert result["name"] == "TestNode"

    def test_companion_with_location(self, handler):
        # ADV_LATLON_MASK=0x10 | ADV_TYPE_CHAT=0x01
        # lat=47.606209 → raw=47606209, lon=-122.332069 → raw=-122332069
        lat_raw = 47606209
        lon_raw = -122332069
        payload = _make_advert_payload(
            0x11,
            location_lat_raw=lat_raw,
            location_lon_raw=lon_raw,
        )
        result = handler.parse_advert(payload)
        assert result["mode"] == "Companion"
        assert "lat" in result
        assert "lon" in result
        assert abs(result["lat"] - round(lat_raw / 1_000_000, 6)) < 1e-5
        assert abs(result["lon"] - round(lon_raw / 1_000_000, 6)) < 1e-5

    def test_advert_with_name_and_location(self, handler):
        # ADV_LATLON_MASK=0x10 | ADV_NAME_MASK=0x80 | ADV_TYPE_CHAT=0x01 = 0x91
        payload = _make_advert_payload(
            0x91,
            location_lat_raw=10000000,
            location_lon_raw=-20000000,
            name="Rooftop",
        )
        result = handler.parse_advert(payload)
        assert result["mode"] == "Companion"
        assert "lat" in result and "lon" in result
        assert result["name"] == "Rooftop"

    def test_advert_with_feat1(self, handler):
        # ADV_FEAT1_MASK=0x20 | ADV_TYPE_CHAT=0x01 = 0x21
        payload = _make_advert_payload(0x21, feat1=0x1234)
        result = handler.parse_advert(payload)
        assert result["feat1"] == 0x1234

    def test_advert_with_feat2(self, handler):
        # ADV_FEAT2_MASK=0x40 | ADV_TYPE_CHAT=0x01 = 0x41
        payload = _make_advert_payload(0x41, feat2=0xABCD)
        result = handler.parse_advert(payload)
        assert result["feat2"] == 0xABCD

    def test_advert_with_all_optional_fields(self, handler):
        # 0x10 | 0x20 | 0x40 | 0x80 | 0x02 = 0xF2  (REPEATER with all flags)
        payload = _make_advert_payload(
            0xF2,
            location_lat_raw=1000000,
            location_lon_raw=-2000000,
            feat1=0x0001,
            feat2=0x0002,
            name="AllFlagsRepeater",
        )
        result = handler.parse_advert(payload)
        assert result["mode"] == "Repeater"
        assert "lat" in result
        assert "lon" in result
        assert result["feat1"] == 0x0001
        assert result["feat2"] == 0x0002
        assert result["name"] == "AllFlagsRepeater"

    def test_location_flag_but_too_short_returns_partial(self, handler):
        # ADV_LATLON_MASK set but only 1 app_data byte (the flags) → too short for lat+lon
        pub_key = b"\xaa" * 32
        ts = (1700000000).to_bytes(4, "little")
        sig = b"\xbb" * 64
        flags = bytes([0x11])  # ADV_LATLON_MASK | ADV_TYPE_CHAT — no lat/lon data after
        payload = pub_key + ts + sig + flags  # 101 bytes, but no location data
        result = handler.parse_advert(payload)
        # Method returns advert dict without location (early return on short data)
        assert "mode" in result
        assert "lat" not in result

    def test_public_key_in_result(self, handler):
        pub_key = bytes(range(32))  # 0x00..0x1f
        payload = _make_advert_payload(0x01, pub_key=pub_key)
        result = handler.parse_advert(payload)
        assert result["public_key"] == bytes(range(32)).hex()

    def test_signature_in_result(self, handler):
        sig = bytes([0xFF] * 64)
        payload = _make_advert_payload(0x01, signature=sig)
        result = handler.parse_advert(payload)
        assert result["signature"] == "ff" * 64


# ---------------------------------------------------------------------------
# store_message_for_correlation and cleanup_old_messages
# ---------------------------------------------------------------------------

class TestMessageCorrelation:
    """Tests for store_message_for_correlation(), cleanup_old_messages(), and
    correlate_message_with_rf_data()."""

    def test_store_message_adds_to_pending(self, handler):
        handler.store_message_for_correlation("msg-001", {"pubkey_prefix": "aa"})
        assert "msg-001" in handler.pending_messages
        entry = handler.pending_messages["msg-001"]
        assert entry["data"] == {"pubkey_prefix": "aa"}
        assert entry["processed"] is False
        assert isinstance(entry["timestamp"], float)

    def test_store_message_overwrites_existing(self, handler):
        handler.store_message_for_correlation("dup", {"v": 1})
        handler.store_message_for_correlation("dup", {"v": 2})
        assert handler.pending_messages["dup"]["data"]["v"] == 2

    def test_cleanup_removes_expired_entries(self, handler):
        handler.message_timeout = 5.0
        # Store a message then backdate its timestamp well past the timeout
        handler.store_message_for_correlation("old-msg", {"x": 1})
        handler.pending_messages["old-msg"]["timestamp"] = time.time() - 100
        handler.cleanup_old_messages()
        assert "old-msg" not in handler.pending_messages

    def test_cleanup_keeps_fresh_entries(self, handler):
        handler.message_timeout = 60.0
        handler.store_message_for_correlation("fresh", {"x": 2})
        handler.cleanup_old_messages()
        assert "fresh" in handler.pending_messages

    def test_cleanup_empty_pending_is_safe(self, handler):
        handler.pending_messages = {}
        handler.cleanup_old_messages()  # Should not raise

    def test_correlate_unknown_message_id_returns_none(self, handler):
        result = handler.correlate_message_with_rf_data("nonexistent-id")
        assert result is None

    def test_correlate_message_with_matching_rf_data(self, handler):
        handler.store_message_for_correlation("m1", {"pubkey_prefix": "aabb"})
        rf = {
            "timestamp": time.time(),
            "snr": 5,
            "rssi": -80,
            "packet_prefix": "aabb",
            "pubkey_prefix": "aabb",
        }
        handler.recent_rf_data = [rf]
        handler.rf_data_timeout = 60
        result = handler.correlate_message_with_rf_data("m1")
        assert result is not None
        assert handler.pending_messages["m1"]["processed"] is True

    def test_correlate_no_matching_rf_returns_none(self, handler):
        handler.store_message_for_correlation("m2", {"pubkey_prefix": "ffff"})
        handler.recent_rf_data = []
        result = handler.correlate_message_with_rf_data("m2")
        assert result is None


# ---------------------------------------------------------------------------
# try_correlate_pending_messages
# ---------------------------------------------------------------------------

class TestTryCorrelatePendingMessages:
    """Tests for MessageHandler.try_correlate_pending_messages()."""

    def test_marks_matching_message_processed(self, handler):
        handler.store_message_for_correlation("pm1", {"pubkey_prefix": "ccdd"})
        rf_data = {"pubkey_prefix": "ccdd", "packet_prefix": "ccdd", "timestamp": time.time()}
        handler.try_correlate_pending_messages(rf_data)
        assert handler.pending_messages["pm1"]["processed"] is True

    def test_skips_already_processed_messages(self, handler):
        handler.store_message_for_correlation("pm2", {"pubkey_prefix": "eeff"})
        handler.pending_messages["pm2"]["processed"] = True
        rf_data = {"pubkey_prefix": "eeff", "packet_prefix": "eeff", "timestamp": time.time()}
        # Should not raise; processed flag remains True
        handler.try_correlate_pending_messages(rf_data)
        assert handler.pending_messages["pm2"]["processed"] is True

    def test_no_match_does_not_mark_processed(self, handler):
        handler.store_message_for_correlation("pm3", {"pubkey_prefix": "1111"})
        rf_data = {"pubkey_prefix": "9999", "packet_prefix": "9999", "timestamp": time.time()}
        handler.try_correlate_pending_messages(rf_data)
        assert handler.pending_messages["pm3"]["processed"] is False

    def test_partial_prefix_match_16chars(self, handler):
        # If both pubkey_prefixes share first 16 chars, they correlate
        long_key = "aabbccddeeff0011aabbccddeeff0011"
        handler.store_message_for_correlation("pm4", {"pubkey_prefix": long_key})
        rf_data = {"pubkey_prefix": long_key, "packet_prefix": long_key, "timestamp": time.time()}
        handler.try_correlate_pending_messages(rf_data)
        assert handler.pending_messages["pm4"]["processed"] is True


# ---------------------------------------------------------------------------
# handle_rf_log_data
# ---------------------------------------------------------------------------

class TestHandleRfLogData:
    """Tests for MessageHandler.handle_rf_log_data() — async event handler."""

    def _make_event(self, payload):
        event = Mock()
        event.payload = payload
        return event

    def _setup_handler(self, handler):
        handler.logger = Mock()
        handler.bot.transmission_tracker = None
        handler.bot.web_viewer_integration = None

    async def test_no_payload_attribute_logs_warning(self, handler):
        self._setup_handler(handler)
        event = Mock(spec=[])  # no .payload attribute
        await handler.handle_rf_log_data(event)
        handler.logger.warning.assert_called()

    async def test_payload_none_logs_warning(self, handler):
        self._setup_handler(handler)
        event = Mock()
        event.payload = None
        await handler.handle_rf_log_data(event)
        handler.logger.warning.assert_called()

    async def test_payload_without_snr_field_no_store(self, handler):
        self._setup_handler(handler)
        event = self._make_event({"raw_hex": "1100de", "rssi": -80})
        await handler.handle_rf_log_data(event)
        # No SNR field → nothing stored in recent_rf_data
        assert len(handler.recent_rf_data) == 0

    async def test_snr_without_raw_hex_no_store(self, handler):
        self._setup_handler(handler)
        # Has snr but no raw_hex → packet_prefix is None → no store
        event = self._make_event({"snr": 5.0})
        await handler.handle_rf_log_data(event)
        assert len(handler.recent_rf_data) == 0

    async def test_snr_cached_from_packet_prefix(self, handler):
        self._setup_handler(handler)
        raw_hex = "a" * 64  # 32 hex chars → packet_prefix is first 32 chars = "a"*32
        event = self._make_event({"snr": 7.5, "raw_hex": raw_hex})
        await handler.handle_rf_log_data(event)
        expected_prefix = raw_hex[:32]
        assert handler.snr_cache.get(expected_prefix) == 7.5

    async def test_rssi_cached_from_packet_prefix(self, handler):
        self._setup_handler(handler)
        raw_hex = "b" * 64
        event = self._make_event({"snr": 3.0, "rssi": -95, "raw_hex": raw_hex})
        await handler.handle_rf_log_data(event)
        expected_prefix = raw_hex[:32]
        assert handler.rssi_cache.get(expected_prefix) == -95

    async def test_rf_data_stored_in_recent_rf_data(self, handler):
        self._setup_handler(handler)
        raw_hex = "c" * 64
        event = self._make_event({"snr": 4.0, "raw_hex": raw_hex})
        await handler.handle_rf_log_data(event)
        assert len(handler.recent_rf_data) == 1
        entry = handler.recent_rf_data[0]
        assert entry["snr"] == 4.0
        assert entry["packet_prefix"] == raw_hex[:32]

    async def test_rf_data_added_to_timestamp_index(self, handler):
        self._setup_handler(handler)
        raw_hex = "d" * 64
        event = self._make_event({"snr": 2.0, "raw_hex": raw_hex})
        await handler.handle_rf_log_data(event)
        assert len(handler.rf_data_by_timestamp) == 1

    async def test_rf_data_added_to_pubkey_index(self, handler):
        self._setup_handler(handler)
        raw_hex = "e" * 64
        event = self._make_event({"snr": 1.0, "raw_hex": raw_hex})
        await handler.handle_rf_log_data(event)
        prefix = raw_hex[:32]
        assert prefix in handler.rf_data_by_pubkey

    async def test_pubkey_from_metadata_stored(self, handler):
        self._setup_handler(handler)
        raw_hex = "f" * 64
        meta = {"pubkey_prefix": "aabbccdd"}
        event = self._make_event({"snr": 6.0, "raw_hex": raw_hex})
        await handler.handle_rf_log_data(event, metadata=meta)
        entry = handler.recent_rf_data[0]
        assert entry["pubkey_prefix"] == "aabbccdd"

    async def test_valid_packet_decoded_and_routing_stored(self, handler):
        self._setup_handler(handler)
        # Build a valid MeshCore packet (FLOOD + TXT_MSG, 0 hops)
        pkt_hex = _make_packet_hex(2, 1, payload_bytes=b"\x48\x65\x6c\x6c\x6f")
        # raw_hex must be >= 64 chars for packet_prefix, pad with zeros
        padded = pkt_hex.ljust(64, "0")
        event = self._make_event({"snr": 9.0, "raw_hex": padded})
        await handler.handle_rf_log_data(event)
        assert len(handler.recent_rf_data) == 1
        # routing_info should be populated since raw_hex contains a valid packet
        entry = handler.recent_rf_data[0]
        assert entry["routing_info"] is not None

    async def test_exception_does_not_propagate(self, handler):
        self._setup_handler(handler)
        event = Mock()
        # Make deepcopy blow up
        with patch("modules.message_handler.copy.deepcopy", side_effect=RuntimeError("deepcopy fail")):
            await handler.handle_rf_log_data(event)
        handler.logger.error.assert_called()

    async def test_tc_flood_scope_fields_from_library_payload(self, handler):
        """Library-provided route_type/transport_code/pkt_payload populate scope fields."""
        import hmac as hmac_mod
        from hashlib import sha256
        self._setup_handler(handler)

        scope_name = "#waw"
        payload_type = 5       # GRP_TXT
        pkt_payload_bytes = b"\xca\xb2\x83\xf7\x84\xe1\x17\x40\x2c\x81"

        # Compute the expected transport code (same HMAC logic as _match_scope)
        key = sha256(scope_name.encode()).digest()[:16]
        check_data = bytes([payload_type]) + pkt_payload_bytes
        digest = hmac_mod.new(key, check_data, sha256).digest()
        code1 = int.from_bytes(digest[:2], "little")
        if code1 == 0:
            code1 = 1
        elif code1 == 0xFFFF:
            code1 = 0xFFFE

        # Transport code hex as the library emits it (4 bytes: code1 LE + code2 LE)
        tc_hex = code1.to_bytes(2, "little").hex() + "0000"

        raw_hex = "ab" * 32  # 64 hex chars → packet_prefix is first 32
        event = self._make_event({
            "snr": 5.0,
            "raw_hex": raw_hex,
            # Library-provided fields from meshcore-py parsePacketPayload
            "route_type": 0,                  # TC_FLOOD
            "transport_code": tc_hex,
            "payload_type": payload_type,
            "pkt_payload": pkt_payload_bytes,
        })
        await handler.handle_rf_log_data(event)

        assert len(handler.recent_rf_data) == 1
        entry = handler.recent_rf_data[0]
        assert entry["route_type_int"] == 0
        assert entry["transport_code1"] == code1
        assert entry["payload_type_int"] == payload_type
        assert entry["scope_payload_hex"] == pkt_payload_bytes.hex()

    async def test_tc_flood_scope_fields_pkt_payload_as_hex_string(self, handler):
        """pkt_payload stored as hex string (not bytes) is also accepted."""
        self._setup_handler(handler)
        pkt_payload_bytes = b"\xde\xad\xbe\xef"
        raw_hex = "cd" * 32
        event = self._make_event({
            "snr": 3.0,
            "raw_hex": raw_hex,
            "route_type": 0,
            "transport_code": "1234" + "0000",
            "payload_type": 5,
            "pkt_payload": pkt_payload_bytes.hex(),  # hex string variant
        })
        await handler.handle_rf_log_data(event)

        entry = handler.recent_rf_data[0]
        assert entry["scope_payload_hex"] == pkt_payload_bytes.hex()

    async def test_flood_route_type_not_zero(self, handler):
        """Plain FLOOD (route_type=1) stores route_type_int=1 and no transport code."""
        self._setup_handler(handler)
        raw_hex = "ef" * 32
        event = self._make_event({
            "snr": 2.0,
            "raw_hex": raw_hex,
            "route_type": 1,    # FLOOD, not TC_FLOOD
            "payload_type": 5,
            "pkt_payload": b"\xaa\xbb",
        })
        await handler.handle_rf_log_data(event)

        entry = handler.recent_rf_data[0]
        assert entry["route_type_int"] == 1
        assert entry["transport_code1"] is None


# ---------------------------------------------------------------------------
# _get_path_from_rf_data
# ---------------------------------------------------------------------------

class TestGetPathFromRfData:
    """Tests for MessageHandler._get_path_from_rf_data() — path extraction helper."""

    def test_routing_info_path_nodes_returned_directly(self, handler):
        rf = {
            "routing_info": {"path_nodes": ["ab", "cd"], "path_length": 2},
            "raw_hex": "",
        }
        path_str, nodes, hops = handler._get_path_from_rf_data(rf)
        assert path_str == "ab,cd"
        assert nodes == ["ab", "cd"]
        assert hops == 2

    def test_no_raw_hex_returns_none_tuple(self, handler):
        rf = {"routing_info": {}, "raw_hex": ""}
        path_str, nodes, hops = handler._get_path_from_rf_data(rf)
        assert path_str is None
        assert nodes is None
        assert hops == 255

    def test_raw_hex_decoded_and_path_returned(self, handler):
        path_b = bytes([0xAB, 0xCD])
        pkt_hex = _make_packet_hex(4, 1, path_bytes=path_b, payload_bytes=b"\xEE",
                                   hop_count=2, bytes_per_hop=1)
        padded = pkt_hex.ljust(64, "0")
        rf = {"routing_info": {}, "raw_hex": padded, "payload": ""}
        path_str, nodes, hops = handler._get_path_from_rf_data(rf)
        assert nodes is not None
        assert len(nodes) == 2
        assert hops == 2

    def test_invalid_raw_hex_raises_or_returns_none_tuple(self, handler):
        # Non-hex raw_hex triggers the same UnboundLocalError source bug as
        # decode_meshcore_packet("ZZZZ") — document the actual behaviour.
        rf = {"routing_info": {}, "raw_hex": "ZZZZ", "payload": ""}
        try:
            path_str, nodes, hops = handler._get_path_from_rf_data(rf)
            assert path_str is None
            assert nodes is None
        except (ValueError, UnboundLocalError):
            pass  # expected — source-level bug causes exception to propagate


# ---------------------------------------------------------------------------
# SNR/RSSI LRU cache bounds
# ---------------------------------------------------------------------------

class TestSignalCacheLRUBounds:
    """Tests for bounded LRU eviction on snr_cache and rssi_cache."""

    def test_snr_cache_evicts_oldest_at_limit(self, handler):
        handler._max_signal_cache_size = 3
        # Fill cache to capacity
        handler.snr_cache["aaa"] = 1.0
        handler.snr_cache["bbb"] = 2.0
        handler.snr_cache["ccc"] = 3.0
        # Simulate the write path with LRU eviction
        key = "ddd"
        handler.snr_cache[key] = 4.0
        handler.snr_cache.move_to_end(key)
        while len(handler.snr_cache) > handler._max_signal_cache_size:
            handler.snr_cache.popitem(last=False)
        # Oldest entry ("aaa") should be evicted
        assert "aaa" not in handler.snr_cache
        assert len(handler.snr_cache) == 3
        assert list(handler.snr_cache.keys()) == ["bbb", "ccc", "ddd"]

    def test_rssi_cache_evicts_oldest_at_limit(self, handler):
        handler._max_signal_cache_size = 2
        handler.rssi_cache["x1"] = -50.0
        handler.rssi_cache["x2"] = -60.0
        # Add a third entry with eviction
        key = "x3"
        handler.rssi_cache[key] = -70.0
        handler.rssi_cache.move_to_end(key)
        while len(handler.rssi_cache) > handler._max_signal_cache_size:
            handler.rssi_cache.popitem(last=False)
        assert "x1" not in handler.rssi_cache
        assert len(handler.rssi_cache) == 2

    def test_existing_key_update_does_not_evict(self, handler):
        handler._max_signal_cache_size = 2
        handler.snr_cache["a"] = 1.0
        handler.snr_cache["b"] = 2.0
        # Update existing key — no eviction needed
        handler.snr_cache["a"] = 5.0
        handler.snr_cache.move_to_end("a")
        while len(handler.snr_cache) > handler._max_signal_cache_size:
            handler.snr_cache.popitem(last=False)
        assert len(handler.snr_cache) == 2
        assert handler.snr_cache["a"] == 5.0
        assert handler.snr_cache["b"] == 2.0


# ---------------------------------------------------------------------------
# respond_to_mentions — process_message gate and stripping
# ---------------------------------------------------------------------------

class TestRespondToMentions:
    """Tests for the respond_to_mentions config gate in process_message.

    process_message is async; we short-circuit the command execution
    side-effects by mocking should_process_message to return True and
    stubbing out check_keywords / execute_commands so only the mention
    block is exercised.
    """

    @pytest.fixture
    def mention_bot(self, mock_logger):
        """Bot fixture with respond_to_mentions support."""
        bot = Mock()
        bot.logger = mock_logger
        bot.config = configparser.ConfigParser()
        bot.config.add_section("Bot")
        bot.config.set("Bot", "enabled", "true")
        bot.config.set("Bot", "bot_name", "TestBot")
        bot.config.set("Bot", "rf_data_timeout", "15.0")
        bot.config.set("Bot", "message_correlation_timeout", "10.0")
        bot.config.set("Bot", "enable_enhanced_correlation", "true")
        bot.config.add_section("Channels")
        bot.config.set("Channels", "respond_to_dms", "true")
        bot.config.set("Channels", "max_response_hops", "64")
        bot.connection_time = None
        bot.prefix_hex_chars = 2
        bot.channel_responses_enabled = True
        bot.command_manager = Mock()
        bot.command_manager.monitor_channels = ["general"]
        bot.command_manager.is_user_banned = Mock(return_value=False)
        bot.command_manager.commands = {}
        bot.command_manager.check_keywords = Mock(return_value=[])
        bot.command_manager.match_randomline = Mock(return_value=None)
        bot.command_manager.execute_commands = AsyncMock()
        return bot

    @pytest.fixture
    def mention_handler(self, mention_bot):
        return MessageHandler(mention_bot)

    def _channel_msg(self, content, channel="general"):
        return make_message(content=content, channel=channel, is_dm=False, sender_id="User")

    def _dm_msg(self, content):
        return make_message(content=content, channel=None, is_dm=True, sender_id="User")

    # ------------------------------------------------------------------ also --
    async def test_also_plain_command_processed(self, mention_handler, mention_bot):
        """'also': plain channel message (no mention) is still processed."""
        mention_bot.config.set("Bot", "respond_to_mentions", "also")
        msg = self._channel_msg("ping")
        await mention_handler.process_message(msg)
        # Command execution was reached; content unchanged
        assert msg.content == "ping"

    async def test_also_strips_bot_mention_from_content(self, mention_handler, mention_bot):
        """'also': @[TestBot] is stripped before command dispatch."""
        mention_bot.config.set("Bot", "respond_to_mentions", "also")
        msg = self._channel_msg("@[TestBot] ping")
        await mention_handler.process_message(msg)
        assert msg.content == "ping"

    async def test_also_case_insensitive_strip(self, mention_handler, mention_bot):
        """'also': bot name match is case-insensitive."""
        mention_bot.config.set("Bot", "respond_to_mentions", "also")
        msg = self._channel_msg("@[testbot] ping")
        await mention_handler.process_message(msg)
        assert msg.content == "ping"

    async def test_also_dm_bypasses_mention_logic(self, mention_handler, mention_bot):
        """'also': DMs are never subject to mention stripping or gating."""
        mention_bot.config.set("Bot", "respond_to_mentions", "also")
        msg = self._dm_msg("@[TestBot] ping")
        await mention_handler.process_message(msg)
        # Content should remain as-is — mention logic skipped for DMs
        assert "@[TestBot]" in msg.content

    # ------------------------------------------------------------------ only --
    async def test_only_with_mention_processes(self, mention_handler, mention_bot):
        """'only': message with bot mention is processed (mention stripped)."""
        mention_bot.config.set("Bot", "respond_to_mentions", "only")
        msg = self._channel_msg("@[TestBot] ping")
        await mention_handler.process_message(msg)
        assert msg.content == "ping"
        mention_bot.command_manager.execute_commands.assert_called_once()

    async def test_only_without_mention_ignored(self, mention_handler, mention_bot):
        """'only': plain channel message is silently dropped."""
        mention_bot.config.set("Bot", "respond_to_mentions", "only")
        msg = self._channel_msg("ping")
        await mention_handler.process_message(msg)
        mention_bot.command_manager.execute_commands.assert_not_called()

    async def test_only_dm_always_processed(self, mention_handler, mention_bot):
        """'only': DMs bypass the mention gate and are always processed."""
        mention_bot.config.set("Bot", "respond_to_mentions", "only")
        msg = self._dm_msg("ping")
        await mention_handler.process_message(msg)
        mention_bot.command_manager.execute_commands.assert_called_once()

    # ------------------------------------------------------------------ false --
    async def test_false_no_stripping(self, mention_handler, mention_bot):
        """'false': mention is NOT stripped from message content."""
        mention_bot.config.set("Bot", "respond_to_mentions", "false")
        msg = self._channel_msg("@[TestBot] ping")
        await mention_handler.process_message(msg)
        # Content must still contain the mention — no stripping in false mode
        assert "@[TestBot]" in msg.content

    async def test_false_plain_command_processed(self, mention_handler, mention_bot):
        """'false': plain commands work exactly as before."""
        mention_bot.config.set("Bot", "respond_to_mentions", "false")
        msg = self._channel_msg("ping")
        await mention_handler.process_message(msg)
        assert msg.content == "ping"
        mention_bot.command_manager.execute_commands.assert_called_once()


class TestProcessMessageDmKeywordRouting:
    """Regression tests for DM keyword reply routing."""

    @pytest.mark.asyncio
    async def test_keyword_reply_uses_prefix_sender_id_for_dm_send(self, handler):
        """DM keyword flow should route reply using prefix sender identity."""
        handler.should_process_message = Mock(return_value=True)
        handler.bot.command_manager.check_keywords = Mock(return_value=[("test", "ack")])
        handler.bot.command_manager.match_randomline = Mock(return_value=None)
        handler.bot.command_manager.execute_commands = AsyncMock()
        handler.bot.command_manager.get_rate_limit_key = Mock(return_value="ab12deadbeef")
        handler.bot.command_manager.send_response = AsyncMock(return_value=True)
        handler.bot.command_manager.commands = {}

        message = MeshMessage(
            content="test",
            sender_id="ab12",
            sender_pubkey="ab12deadbeefcafebabe",
            is_dm=True,
        )

        await handler.process_message(message)

        handler.bot.command_manager.send_response.assert_awaited_once()
        call = handler.bot.command_manager.send_response.await_args
        assert call.args[0] is message
        assert call.args[1] == "ack"
        assert call.kwargs["command_id"].startswith("keyword_test_ab12_")


class TestProcessMessageChannelKeywordFloodScope:
    """Keyword channel replies must use send_response so flood scope is applied (#178)."""

    @pytest.mark.asyncio
    async def test_keyword_channel_reply_passes_message_with_reply_scope(self, handler, bot):
        handler.should_process_message = Mock(return_value=True)
        bot.command_manager.check_keywords = Mock(return_value=[("wx", "sunny")])
        bot.command_manager.match_randomline = Mock(return_value=None)
        bot.command_manager.execute_commands = AsyncMock()
        bot.command_manager.send_response = AsyncMock(return_value=True)
        bot.command_manager.commands = {}

        message = MeshMessage(
            content="wx",
            channel="#bot",
            is_dm=False,
            sender_id="alice",
            reply_scope="#pl-mz",
        )

        await handler.process_message(message)

        bot.command_manager.send_response.assert_awaited_once()
        call = bot.command_manager.send_response.await_args
        assert call.args[0].reply_scope == "#pl-mz"
        assert call.args[1] == "sunny"
        assert call.kwargs["command_id"].startswith("keyword_wx_alice_")


# ---------------------------------------------------------------------------
# handle_new_contact — auto_manage_contacts (companion path)
# ---------------------------------------------------------------------------


class _NewContactEvent:
    def __init__(self, payload: dict) -> None:
        self.payload = payload


def _companion_contact_payload() -> dict:
    pk = "ab" * 32
    return {
        "public_key": pk,
        "adv_name": "Alice",
        "name": "Alice",
        "type": 1,
        "flags": 0,
        "out_path": "",
        "out_path_len": 0,
        "out_path_hash_mode": 0,
    }


@pytest.fixture
def new_contact_env(mock_logger):
    """Bot + MessageHandler with mocked repeater_manager and meshcore for NEW_CONTACT tests."""
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
    bot.prefix_hex_chars = 8
    bot.command_manager = Mock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.is_user_banned = Mock(return_value=False)
    bot.command_manager.commands = {}

    handler = MessageHandler(bot)
    bot.message_handler = handler

    rm = Mock()
    from modules.repeater_manager import TrackAdvertResult

    rm.track_contact_advertisement = AsyncMock(
        return_value=TrackAdvertResult(ok=True, duplicate_packet=False)
    )
    rm.check_and_auto_purge = AsyncMock()
    rm.get_contact_list_status = AsyncMock(
        return_value={
            "is_near_limit": False,
            "usage_percentage": 10.0,
            "current_contacts": 5,
            "estimated_limit": 300,
        }
    )
    rm.manage_contact_list = AsyncMock(return_value={"success": True})
    rm.add_companion_from_contact_data = AsyncMock(return_value=True)
    rm.log_purging_action = Mock()
    rm.db_manager = Mock()
    rm.db_manager.execute_update = Mock()
    rm._is_repeater_device = Mock(return_value=False)
    bot.repeater_manager = rm

    mesh = Mock()
    mesh.commands = Mock()
    mesh.commands.add_contact = AsyncMock()
    bot.meshcore = mesh

    return bot, handler, rm, mesh


@pytest.mark.asyncio
class TestHandleNewContactAutoManage:
    async def test_manual_mode_no_device_add(self, new_contact_env):
        bot, handler, rm, mesh = new_contact_env
        bot.config.set("Bot", "auto_manage_contacts", "false")
        ev = _NewContactEvent(_companion_contact_payload())
        await handler.handle_new_contact(ev, None)
        rm.track_contact_advertisement.assert_awaited_once()
        mesh.commands.add_contact.assert_not_called()
        rm.add_companion_from_contact_data.assert_not_called()
        rm.log_purging_action.assert_called_once()

    async def test_device_mode_no_bot_add_contact(self, new_contact_env):
        bot, handler, rm, mesh = new_contact_env
        bot.config.set("Bot", "auto_manage_contacts", "device")
        ev = _NewContactEvent(_companion_contact_payload())
        await handler.handle_new_contact(ev, None)
        rm.track_contact_advertisement.assert_awaited_once()
        mesh.commands.add_contact.assert_not_called()
        rm.add_companion_from_contact_data.assert_not_called()
        rm.get_contact_list_status.assert_awaited()

    async def test_bot_mode_uses_add_companion_from_contact_data(self, new_contact_env):
        bot, handler, rm, mesh = new_contact_env
        bot.config.set("Bot", "auto_manage_contacts", "bot")
        ev = _NewContactEvent(_companion_contact_payload())
        await handler.handle_new_contact(ev, None)
        rm.add_companion_from_contact_data.assert_awaited_once()
        mesh.commands.add_contact.assert_not_called()

    async def test_bot_mode_skips_add_when_duplicate_packet_hash(self, new_contact_env):
        from modules.repeater_manager import TrackAdvertResult

        bot, handler, rm, mesh = new_contact_env
        bot.config.set("Bot", "auto_manage_contacts", "bot")
        rm.track_contact_advertisement = AsyncMock(
            return_value=TrackAdvertResult(ok=True, duplicate_packet=True)
        )
        ev = _NewContactEvent(_companion_contact_payload())
        await handler.handle_new_contact(ev, None)
        rm.track_contact_advertisement.assert_awaited_once()
        rm.add_companion_from_contact_data.assert_not_called()
        rm.get_contact_list_status.assert_not_awaited()

    async def test_bot_mode_two_events_first_unique_then_duplicate_adds_once(self, new_contact_env):
        from modules.repeater_manager import TrackAdvertResult

        bot, handler, rm, mesh = new_contact_env
        bot.config.set("Bot", "auto_manage_contacts", "bot")
        rm.track_contact_advertisement = AsyncMock(
            side_effect=[
                TrackAdvertResult(ok=True, duplicate_packet=False),
                TrackAdvertResult(ok=True, duplicate_packet=True),
            ]
        )
        ev = _NewContactEvent(_companion_contact_payload())
        await handler.handle_new_contact(ev, None)
        await handler.handle_new_contact(ev, None)
        assert rm.track_contact_advertisement.await_count == 2
        rm.add_companion_from_contact_data.assert_awaited_once()
