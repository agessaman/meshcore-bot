"""Tests for MessageHandler pure logic (no network, no meshcore device)."""

import configparser
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest

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
