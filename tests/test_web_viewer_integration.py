"""Tests for modules.web_viewer.integration — BotIntegration pure logic."""

import json
import queue
import time
from configparser import ConfigParser
from unittest.mock import MagicMock, Mock, patch

import pytest

# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def _make_bot():
    """Create minimal mock bot for BotIntegration tests."""
    bot = MagicMock()
    bot.logger = Mock()
    config = ConfigParser()
    config.add_section("Bot")
    config.add_section("Web_Viewer")
    config.set("Web_Viewer", "host", "127.0.0.1")
    config.set("Web_Viewer", "port", "8080")
    config.set("Web_Viewer", "enabled", "false")
    config.set("Web_Viewer", "auto_start", "false")
    config.set("Web_Viewer", "debug", "false")
    bot.config = config
    bot.bot_root = "/tmp"
    bot.db_manager = MagicMock()
    bot.db_manager.db_path = ":memory:"
    bot.transmission_tracker = None
    return bot


def _make_bot_integration(bot=None):
    """Create BotIntegration with all I/O patched out."""
    if bot is None:
        bot = _make_bot()
    from modules.web_viewer.integration import BotIntegration
    with patch.object(BotIntegration, "_init_http_session"), \
         patch.object(BotIntegration, "_init_packet_stream_table"), \
         patch.object(BotIntegration, "_start_drain_thread"):
        obj = BotIntegration(bot)
    # Give it a real write queue for testing
    obj._write_queue = queue.Queue()
    obj.http_session = None
    return obj


# ---------------------------------------------------------------------------
# reset_circuit_breaker
# ---------------------------------------------------------------------------


class TestResetCircuitBreaker:
    def test_clears_open_flag(self):
        bi = _make_bot_integration()
        bi.circuit_breaker_open = True
        bi.circuit_breaker_failures = 5
        bi.reset_circuit_breaker()
        assert bi.circuit_breaker_open is False
        assert bi.circuit_breaker_failures == 0


# ---------------------------------------------------------------------------
# _should_skip_web_viewer_send
# ---------------------------------------------------------------------------


class TestShouldSkipWebViewerSend:
    def test_not_open_returns_false(self):
        bi = _make_bot_integration()
        bi.circuit_breaker_open = False
        assert bi._should_skip_web_viewer_send() is False

    def test_open_within_cooldown_returns_true(self):
        bi = _make_bot_integration()
        bi.circuit_breaker_open = True
        bi.circuit_breaker_last_failure_time = time.time()  # just now
        assert bi._should_skip_web_viewer_send() is True

    def test_open_beyond_cooldown_resets_and_returns_false(self):
        bi = _make_bot_integration()
        bi.circuit_breaker_open = True
        bi.circuit_breaker_failures = 3
        # Set last failure time far in the past
        bi.circuit_breaker_last_failure_time = time.time() - bi.CIRCUIT_BREAKER_COOLDOWN_SEC - 1
        assert bi._should_skip_web_viewer_send() is False
        assert bi.circuit_breaker_open is False


# ---------------------------------------------------------------------------
# _record_web_viewer_result
# ---------------------------------------------------------------------------


class TestRecordWebViewerResult:
    def test_success_resets_circuit(self):
        bi = _make_bot_integration()
        bi.circuit_breaker_failures = 2
        bi.circuit_breaker_open = True
        bi._record_web_viewer_result(True)
        assert bi.circuit_breaker_failures == 0
        assert bi.circuit_breaker_open is False

    def test_failure_increments_counter(self):
        bi = _make_bot_integration()
        bi._record_web_viewer_result(False)
        assert bi.circuit_breaker_failures == 1

    def test_failure_opens_circuit_at_threshold(self):
        bi = _make_bot_integration()
        for _ in range(bi.CIRCUIT_BREAKER_THRESHOLD):
            bi._record_web_viewer_result(False)
        assert bi.circuit_breaker_open is True

    def test_failure_below_threshold_does_not_open(self):
        bi = _make_bot_integration()
        for _ in range(bi.CIRCUIT_BREAKER_THRESHOLD - 1):
            bi._record_web_viewer_result(False)
        assert bi.circuit_breaker_open is False


# ---------------------------------------------------------------------------
# _make_json_serializable
# ---------------------------------------------------------------------------


class TestMakeJsonSerializable:
    def setup_method(self):
        self.bi = _make_bot_integration()

    def test_none_passes_through(self):
        assert self.bi._make_json_serializable(None) is None

    def test_string_passes_through(self):
        assert self.bi._make_json_serializable("hello") == "hello"

    def test_int_passes_through(self):
        assert self.bi._make_json_serializable(42) == 42

    def test_float_passes_through(self):
        assert self.bi._make_json_serializable(3.14) == 3.14

    def test_bool_passes_through(self):
        assert self.bi._make_json_serializable(True) is True

    def test_list_recurses(self):
        result = self.bi._make_json_serializable([1, "two", None])
        assert result == [1, "two", None]

    def test_tuple_becomes_list(self):
        result = self.bi._make_json_serializable((1, 2))
        assert result == [1, 2]

    def test_dict_recurses(self):
        result = self.bi._make_json_serializable({"a": 1, "b": [2, 3]})
        assert result == {"a": 1, "b": [2, 3]}

    def test_enum_like_uses_name(self):
        obj = Mock(spec=["name"])
        obj.name = "MY_ENUM"
        result = self.bi._make_json_serializable(obj)
        assert result == "MY_ENUM"

    def test_value_attr_used_when_no_name(self):
        obj = Mock(spec=["value"])
        obj.value = 99
        result = self.bi._make_json_serializable(obj)
        assert result == 99

    def test_object_with_dict_converted(self):
        class Dummy:
            def __init__(self):
                self.x = 1
                self.y = "a"
        result = self.bi._make_json_serializable(Dummy())
        assert result == {"x": 1, "y": "a"}

    def test_unknown_object_stringified(self):
        # An object with no __dict__, no name, no value
        result = self.bi._make_json_serializable(object())
        assert isinstance(result, str)

    def test_max_depth_stringifies(self):
        # At max_depth, return str
        result = self.bi._make_json_serializable({"nested": [1]}, depth=4, max_depth=3)
        assert isinstance(result, str)

    def test_deeply_nested_dict(self):
        d = {"a": {"b": {"c": "deep"}}}
        result = self.bi._make_json_serializable(d)
        assert result["a"]["b"]["c"] == "deep"


# ---------------------------------------------------------------------------
# _insert_packet_stream_row
# ---------------------------------------------------------------------------


class TestInsertPacketStreamRow:
    def test_enqueues_tuple(self):
        bi = _make_bot_integration()
        bi._insert_packet_stream_row('{"x": 1}', "packet")
        assert not bi._write_queue.empty()
        ts, data, row_type = bi._write_queue.get_nowait()
        assert data == '{"x": 1}'
        assert row_type == "packet"

    def test_queue_exception_logged(self):
        bi = _make_bot_integration()
        bi._write_queue = Mock()
        bi._write_queue.put_nowait.side_effect = Exception("full")
        # Should not raise
        bi._insert_packet_stream_row("{}", "packet")
        bi.bot.logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# capture_full_packet_data
# ---------------------------------------------------------------------------


class TestCaptureFullPacketData:
    def test_dict_packet_data_queued(self):
        bi = _make_bot_integration()
        bi.capture_full_packet_data({"snr": -5.0, "path_len": 2})
        assert not bi._write_queue.empty()
        ts, data, row_type = bi._write_queue.get_nowait()
        parsed = json.loads(data)
        assert row_type == "packet"
        assert parsed["hops"] == 2

    def test_no_path_len_defaults_hops_to_0(self):
        bi = _make_bot_integration()
        bi.capture_full_packet_data({"snr": -5.0})
        ts, data, row_type = bi._write_queue.get_nowait()
        parsed = json.loads(data)
        assert parsed["hops"] == 0

    def test_existing_hops_not_overwritten(self):
        bi = _make_bot_integration()
        bi.capture_full_packet_data({"hops": 3, "path_len": 2})
        ts, data, row_type = bi._write_queue.get_nowait()
        parsed = json.loads(data)
        assert parsed["hops"] == 3

    def test_datetime_added(self):
        bi = _make_bot_integration()
        bi.capture_full_packet_data({"snr": 0})
        ts, data, _ = bi._write_queue.get_nowait()
        parsed = json.loads(data)
        assert "datetime" in parsed

    def test_non_dict_wrapped(self):
        bi = _make_bot_integration()
        # Pass a non-dict (e.g. a Mock with __dict__)
        bi.capture_full_packet_data("not a dict")
        # Should not raise; may enqueue something
        assert True  # reached here without exception


# ---------------------------------------------------------------------------
# capture_command
# ---------------------------------------------------------------------------


class TestCaptureCommand:
    def test_basic_capture_queued(self):
        bi = _make_bot_integration()
        msg = Mock()
        msg.sender_id = "aa:bb"
        msg.channel = "general"
        msg.content = "ping"
        bi.capture_command(msg, "ping", "Pong!", True)
        assert not bi._write_queue.empty()
        ts, data, row_type = bi._write_queue.get_nowait()
        assert row_type == "command"
        parsed = json.loads(data)
        assert parsed["command"] == "ping"
        assert parsed["success"] is True

    def test_no_transmission_tracker(self):
        bi = _make_bot_integration()
        bi.bot.transmission_tracker = None
        msg = Mock()
        msg.sender_id = "u1"
        msg.channel = "ch"
        msg.content = "cmd"
        bi.capture_command(msg, "cmd", "resp", True)
        ts, data, _ = bi._write_queue.get_nowait()
        parsed = json.loads(data)
        assert parsed["repeat_count"] == 0


# ---------------------------------------------------------------------------
# capture_channel_message
# ---------------------------------------------------------------------------


class TestCaptureChannelMessage:
    def test_message_queued_with_type_message(self):
        bi = _make_bot_integration()
        msg = Mock()
        msg.sender_id = "aa"
        msg.channel = "general"
        msg.content = "hello"
        msg.snr = -5.0
        msg.hops = 1
        msg.path = "bb,cc"
        msg.is_dm = False
        bi.capture_channel_message(msg)
        assert not bi._write_queue.empty()
        ts, data, row_type = bi._write_queue.get_nowait()
        assert row_type == "message"
        parsed = json.loads(data)
        assert parsed["type"] == "message"
        assert parsed["content"] == "hello"

    def test_dm_message_captured(self):
        bi = _make_bot_integration()
        msg = Mock()
        msg.sender_id = "aa"
        msg.channel = ""
        msg.content = "private"
        msg.snr = -3.0
        msg.hops = 0
        msg.path = ""
        msg.is_dm = True
        bi.capture_channel_message(msg)
        ts, data, _ = bi._write_queue.get_nowait()
        parsed = json.loads(data)
        assert parsed["is_dm"] is True


# ---------------------------------------------------------------------------
# capture_packet_routing
# ---------------------------------------------------------------------------


class TestCapturePacketRouting:
    def test_routing_data_queued(self):
        bi = _make_bot_integration()
        bi.capture_packet_routing({"path_nodes": ["aa", "bb"]})
        assert not bi._write_queue.empty()
        ts, data, row_type = bi._write_queue.get_nowait()
        assert row_type == "routing"


# ---------------------------------------------------------------------------
# _get_web_viewer_db_path
# ---------------------------------------------------------------------------


class TestGetWebViewerDbPath:
    def test_uses_bot_db_path_when_no_section(self):
        bi = _make_bot_integration()
        bi.bot.config.remove_section("Web_Viewer")
        bi.bot.db_manager.db_path = "/tmp/test.db"
        result = bi._get_web_viewer_db_path()
        assert "test.db" in result

    def test_uses_web_viewer_db_path_when_set(self):
        bi = _make_bot_integration()
        bi.bot.config.set("Web_Viewer", "db_path", "/tmp/viewer.db")
        result = bi._get_web_viewer_db_path()
        assert "viewer.db" in result

    def test_falls_back_when_web_viewer_db_path_empty(self):
        bi = _make_bot_integration()
        bi.bot.config.set("Web_Viewer", "db_path", "")
        bi.bot.db_manager.db_path = "/tmp/bot.db"
        result = bi._get_web_viewer_db_path()
        assert "bot.db" in result


# ---------------------------------------------------------------------------
# WebViewerIntegration validation
# ---------------------------------------------------------------------------


class TestWebViewerIntegrationValidation:
    def test_invalid_host_raises(self):
        from modules.web_viewer.integration import WebViewerIntegration
        bot = _make_bot()
        bot.config.set("Web_Viewer", "host", "evil.host")
        bot.config.set("Web_Viewer", "enabled", "false")
        with patch.object(WebViewerIntegration, "start_viewer"):
            with patch("modules.web_viewer.integration.BotIntegration._init_http_session"), \
                 patch("modules.web_viewer.integration.BotIntegration._init_packet_stream_table"), \
                 patch("modules.web_viewer.integration.BotIntegration._start_drain_thread"):
                with pytest.raises(ValueError, match="Invalid host"):
                    WebViewerIntegration(bot)

    def test_invalid_port_raises(self):
        from modules.web_viewer.integration import WebViewerIntegration
        bot = _make_bot()
        bot.config.set("Web_Viewer", "port", "80")  # privileged port
        with patch("modules.web_viewer.integration.BotIntegration._init_http_session"), \
             patch("modules.web_viewer.integration.BotIntegration._init_packet_stream_table"), \
             patch("modules.web_viewer.integration.BotIntegration._start_drain_thread"):
            with pytest.raises(ValueError, match="Port must be"):
                WebViewerIntegration(bot)

    def test_valid_config_no_error(self):
        from modules.web_viewer.integration import WebViewerIntegration
        bot = _make_bot()
        with patch("modules.web_viewer.integration.BotIntegration._init_http_session"), \
             patch("modules.web_viewer.integration.BotIntegration._init_packet_stream_table"), \
             patch("modules.web_viewer.integration.BotIntegration._start_drain_thread"):
            wvi = WebViewerIntegration(bot)
        assert wvi.host == "127.0.0.1"
        assert wvi.port == 8080

    def test_zero_host_without_password_logs_error_does_not_raise(self):
        from modules.web_viewer.integration import WebViewerIntegration
        bot = _make_bot()
        bot.config.set("Web_Viewer", "enabled", "true")
        bot.config.set("Web_Viewer", "host", "0.0.0.0")
        bot.config.set("Web_Viewer", "web_viewer_password", "")
        with patch.object(WebViewerIntegration, "start_viewer"):
            with patch("modules.web_viewer.integration.BotIntegration._init_http_session"), \
                 patch("modules.web_viewer.integration.BotIntegration._init_packet_stream_table"), \
                 patch("modules.web_viewer.integration.BotIntegration._start_drain_thread"):
                WebViewerIntegration(bot)
        bot.logger.error.assert_called()
        msg = bot.logger.error.call_args[0][0]
        assert "0.0.0.0" in msg
        assert "web_viewer_password" in msg

    def test_zero_host_without_password_does_not_log_when_disabled(self):
        from modules.web_viewer.integration import WebViewerIntegration
        bot = _make_bot()
        bot.config.set("Web_Viewer", "enabled", "false")
        bot.config.set("Web_Viewer", "host", "0.0.0.0")
        bot.config.set("Web_Viewer", "web_viewer_password", "")
        with patch.object(WebViewerIntegration, "start_viewer"):
            with patch("modules.web_viewer.integration.BotIntegration._init_http_session"), \
                 patch("modules.web_viewer.integration.BotIntegration._init_packet_stream_table"), \
                 patch("modules.web_viewer.integration.BotIntegration._start_drain_thread"):
                WebViewerIntegration(bot)
        bot.logger.error.assert_not_called()


class TestNormalizedWebViewerPassword:
    def test_blank_and_null_placeholders(self):
        from modules.web_viewer.integration import normalized_web_viewer_password

        c = ConfigParser()
        c.add_section("Web_Viewer")
        assert normalized_web_viewer_password(c) == ""
        c.set("Web_Viewer", "web_viewer_password", "")
        assert normalized_web_viewer_password(c) == ""
        c.set("Web_Viewer", "web_viewer_password", "  ")
        assert normalized_web_viewer_password(c) == ""
        c.set("Web_Viewer", "web_viewer_password", '""')
        assert normalized_web_viewer_password(c) == ""
        c.set("Web_Viewer", "web_viewer_password", "null")
        assert normalized_web_viewer_password(c) == ""
        c.set("Web_Viewer", "web_viewer_password", "NONE")
        assert normalized_web_viewer_password(c) == ""

    def test_real_password_preserved(self):
        from modules.web_viewer.integration import normalized_web_viewer_password

        c = ConfigParser()
        c.add_section("Web_Viewer")
        c.set("Web_Viewer", "web_viewer_password", "secret")
        assert normalized_web_viewer_password(c) == "secret"
        c.set("Web_Viewer", "web_viewer_password", '"quoted"')
        assert normalized_web_viewer_password(c) == "quoted"


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_shutdown_sets_flag(self):
        bi = _make_bot_integration()
        bi._drain_thread = Mock()
        bi._drain_thread.is_alive.return_value = False
        bi.shutdown()
        assert bi.is_shutting_down is True

    def test_shutdown_stops_drain_thread(self):
        bi = _make_bot_integration()
        bi._drain_thread = Mock()
        bi._drain_thread.is_alive.return_value = True
        bi.shutdown()
        bi._drain_thread.join.assert_called_once()
