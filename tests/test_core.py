"""Tests for MeshCoreBot logic (config loading, radio settings, helpers)."""

import asyncio
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modules.core import MeshCoreBot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(path: Path, db_path: Path, extra: str = "") -> None:
    path.write_text(
        f"""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0
timeout = 30

[Bot]
db_path = {db_path.as_posix()}
prefix_bytes = 1

[Channels]
monitor_channels = #general
{extra}
""",
        encoding="utf-8",
    )


@pytest.fixture
def bot(tmp_path):
    """Minimal MeshCoreBot from a temporary config file."""
    config_file = tmp_path / "config.ini"
    db_path = tmp_path / "bot.db"
    _write_config(config_file, db_path)
    return MeshCoreBot(config_file=str(config_file))


# ---------------------------------------------------------------------------
# bot_root property
# ---------------------------------------------------------------------------

class TestBotRoot:
    def test_returns_config_directory(self, tmp_path):
        config_file = tmp_path / "config.ini"
        db_path = tmp_path / "bot.db"
        _write_config(config_file, db_path)
        bot = MeshCoreBot(config_file=str(config_file))
        assert bot.bot_root == tmp_path.resolve()


# ---------------------------------------------------------------------------
# _get_radio_settings
# ---------------------------------------------------------------------------

class TestGetRadioSettings:
    def test_returns_dict_with_expected_keys(self, bot):
        settings = bot._get_radio_settings()
        assert "connection_type" in settings
        assert "serial_port" in settings
        assert "ble_device_name" in settings
        assert "hostname" in settings
        assert "tcp_port" in settings
        assert "timeout" in settings

    def test_reads_connection_type(self, bot):
        assert bot._get_radio_settings()["connection_type"] == "serial"

    def test_reads_serial_port(self, bot):
        assert bot._get_radio_settings()["serial_port"] == "/dev/ttyUSB0"

    def test_reads_timeout(self, bot):
        assert bot._get_radio_settings()["timeout"] == 30

    def test_defaults_when_missing(self, tmp_path):
        config_file = tmp_path / "config.ini"
        db_path = tmp_path / "bot.db"
        # Write config without optional fields
        config_file.write_text(
            f"""[Connection]
connection_type = ble

[Bot]
db_path = {db_path.as_posix()}

[Channels]
monitor_channels = #general
""",
            encoding="utf-8",
        )
        b = MeshCoreBot(config_file=str(config_file))
        settings = b._get_radio_settings()
        assert settings["ble_device_name"] == ""
        assert settings["hostname"] == ""
        assert settings["tcp_port"] == 5000


# ---------------------------------------------------------------------------
# reload_config
# ---------------------------------------------------------------------------

class TestReloadConfig:
    def test_reload_succeeds_with_same_radio_settings(self, tmp_path):
        config_file = tmp_path / "config.ini"
        db_path = tmp_path / "bot.db"
        _write_config(config_file, db_path)
        b = MeshCoreBot(config_file=str(config_file))
        success, msg = b.reload_config()
        assert success is True

    def test_reload_fails_when_radio_settings_changed(self, tmp_path):
        config_file = tmp_path / "config.ini"
        db_path = tmp_path / "bot.db"
        _write_config(config_file, db_path)
        b = MeshCoreBot(config_file=str(config_file))
        # Change serial port in config file
        _write_config(config_file, db_path, extra="")
        config_file.write_text(
            f"""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB1
timeout = 30

[Bot]
db_path = {db_path.as_posix()}

[Channels]
monitor_channels = #general
""",
            encoding="utf-8",
        )
        success, msg = b.reload_config()
        assert success is False
        assert "serial_port" in msg.lower() or "restart" in msg.lower()

    def test_reload_config_not_found_returns_false(self, tmp_path):
        config_file = tmp_path / "config.ini"
        db_path = tmp_path / "bot.db"
        _write_config(config_file, db_path)
        b = MeshCoreBot(config_file=str(config_file))
        # Delete the config file
        config_file.unlink()
        success, msg = b.reload_config()
        assert success is False


# ---------------------------------------------------------------------------
# key_prefix / is_valid_prefix
# ---------------------------------------------------------------------------

class TestKeyPrefixHelpers:
    def test_key_prefix_returns_first_n_chars(self, bot):
        assert bot.key_prefix("deadbeef1234") == "de"

    def test_key_prefix_uses_prefix_hex_chars(self, bot):
        bot.prefix_hex_chars = 4
        assert bot.key_prefix("deadbeef1234") == "dead"

    def test_is_valid_prefix_correct_length(self, bot):
        assert bot.is_valid_prefix("de") is True

    def test_is_valid_prefix_wrong_length(self, bot):
        assert bot.is_valid_prefix("d") is False
        assert bot.is_valid_prefix("dead") is False

    def test_prefix_hex_chars_from_config(self, tmp_path):
        config_file = tmp_path / "config2.ini"
        db_path = tmp_path / "bot2.db"
        config_file.write_text(
            f"""[Connection]
connection_type = ble

[Bot]
db_path = {db_path.as_posix()}
prefix_bytes = 2

[Channels]
monitor_channels = #general
""",
            encoding="utf-8",
        )
        b = MeshCoreBot(config_file=str(config_file))
        assert b.prefix_hex_chars == 4
        assert b.is_valid_prefix("dead") is True
        assert b.is_valid_prefix("de") is False


# ---------------------------------------------------------------------------
# Loop exception handler (TASK-00 / BUG-022)
# ---------------------------------------------------------------------------

class TestLoopExceptionHandler:
    """Verify the custom asyncio exception handler installed by start()."""

    def _make_bot(self, tmp_path: Path) -> MeshCoreBot:
        config_file = tmp_path / "config.ini"
        db_path = tmp_path / "bot.db"
        _write_config(config_file, db_path)
        return MeshCoreBot(config_file=str(config_file))

    def _extract_handler(self, bot: MeshCoreBot) -> object:
        """Run a fake start() up to the set_exception_handler call and return the handler."""
        mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
        mock_loop.get_exception_handler.return_value = None

        captured: list = []

        def capture_handler(h):
            captured.append(h)

        mock_loop.set_exception_handler.side_effect = capture_handler

        with patch.object(bot, "connect", return_value=False):
            with patch("asyncio.get_running_loop", return_value=mock_loop):
                asyncio.run(bot.start())

        assert captured, "set_exception_handler was never called"
        return captured[0], mock_loop

    def test_handler_is_installed_on_start(self, tmp_path):
        bot = self._make_bot(tmp_path)
        handler, mock_loop = self._extract_handler(bot)
        mock_loop.set_exception_handler.assert_called_once()
        assert callable(handler)

    def test_index_error_logged_at_debug_not_propagated(self, tmp_path):
        bot = self._make_bot(tmp_path)
        handler, mock_loop = self._extract_handler(bot)

        with patch.object(bot.logger, "debug") as mock_debug:
            handler(mock_loop, {"exception": IndexError("index out of range")})

        mock_debug.assert_called_once()
        assert "IndexError" in mock_debug.call_args[0][1]
        # default handler must NOT be invoked for IndexError
        mock_loop.default_exception_handler.assert_not_called()

    def test_struct_error_logged_at_debug_not_propagated(self, tmp_path):
        bot = self._make_bot(tmp_path)
        handler, mock_loop = self._extract_handler(bot)

        with patch.object(bot.logger, "debug") as mock_debug:
            handler(mock_loop, {"exception": struct.error("unpack requires")})

        mock_debug.assert_called_once()
        mock_loop.default_exception_handler.assert_not_called()

    def test_other_exception_passes_to_default_handler(self, tmp_path):
        bot = self._make_bot(tmp_path)

        # Use a real previous handler to verify passthrough
        previous_handler = MagicMock()
        mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)
        mock_loop.get_exception_handler.return_value = previous_handler

        captured: list = []
        mock_loop.set_exception_handler.side_effect = lambda h: captured.append(h)

        with patch.object(bot, "connect", return_value=False):
            with patch("asyncio.get_running_loop", return_value=mock_loop):
                asyncio.run(bot.start())

        handler = captured[0]
        ctx = {"exception": RuntimeError("something else")}
        handler(mock_loop, ctx)

        previous_handler.assert_called_once_with(mock_loop, ctx)
        mock_loop.default_exception_handler.assert_not_called()

    def test_no_exception_key_passes_to_default_handler(self, tmp_path):
        bot = self._make_bot(tmp_path)
        handler, mock_loop = self._extract_handler(bot)

        ctx = {"message": "Task destroyed but pending"}
        handler(mock_loop, ctx)

        mock_loop.default_exception_handler.assert_called_once_with(ctx)


# ---------------------------------------------------------------------------
# _probe_radio_health (PR4 — zombie-connection detection)
# ---------------------------------------------------------------------------


class TestProbeRadioHealth:
    """Tests for MeshCoreBot._probe_radio_health()."""

    def _make_bot(self, tmp_path: Path) -> MeshCoreBot:
        config_file = tmp_path / "config.ini"
        db_path = tmp_path / "bot.db"
        _write_config(config_file, db_path)
        return MeshCoreBot(config_file=str(config_file))

    def test_returns_false_when_meshcore_is_none(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.meshcore = None
        result = asyncio.run(bot._probe_radio_health())
        assert result is False

    def test_returns_false_when_not_connected(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.meshcore = MagicMock()
        bot.meshcore.is_connected = False
        result = asyncio.run(bot._probe_radio_health())
        assert result is False

    def test_error_event_increments_fail_count(self, tmp_path):
        from meshcore.events import EventType
        bot = self._make_bot(tmp_path)
        bot._radio_fail_count = 0

        error_event = MagicMock()
        error_event.type = EventType.ERROR
        error_event.payload = {"reason": "no_event_received"}

        bot.meshcore = MagicMock()
        bot.meshcore.is_connected = True
        bot.meshcore.commands.get_time = MagicMock(
            return_value=_make_coro(error_event)
        )

        result = asyncio.run(bot._probe_radio_health())
        assert result is False
        assert bot._radio_fail_count == 1

    def test_error_event_below_threshold_does_not_reconnect(self, tmp_path):
        from meshcore.events import EventType
        bot = self._make_bot(tmp_path)
        bot._radio_fail_count = 0
        bot.config.set("Bot", "radio_probe_fail_threshold", "3")

        error_event = MagicMock()
        error_event.type = EventType.ERROR
        error_event.payload = {"reason": "no_event_received"}

        bot.meshcore = MagicMock()
        bot.meshcore.is_connected = True
        bot.meshcore.commands.get_time = MagicMock(
            return_value=_make_coro(error_event)
        )

        reconnect_called = []

        async def fake_reconnect():
            reconnect_called.append(True)
            return True

        bot.reconnect_radio = fake_reconnect

        asyncio.run(bot._probe_radio_health())
        assert not reconnect_called

    def test_error_event_at_threshold_triggers_reconnect(self, tmp_path):
        from meshcore.events import EventType
        bot = self._make_bot(tmp_path)
        bot._radio_fail_count = 2  # this probe makes it 3
        bot.config.set("Bot", "radio_probe_fail_threshold", "3")

        error_event = MagicMock()
        error_event.type = EventType.ERROR
        error_event.payload = {"reason": "no_event_received"}

        bot.meshcore = MagicMock()
        bot.meshcore.is_connected = True
        bot.meshcore.commands.get_time = MagicMock(
            return_value=_make_coro(error_event)
        )

        reconnect_called = []

        async def fake_reconnect():
            reconnect_called.append(True)
            return True

        bot.reconnect_radio = fake_reconnect

        result = asyncio.run(bot._probe_radio_health())
        assert result is False
        assert bot._radio_fail_count == 0  # reset after trigger
        assert reconnect_called

    def test_success_resets_fail_count(self, tmp_path):
        from meshcore.events import EventType
        bot = self._make_bot(tmp_path)
        bot._radio_fail_count = 2

        ok_event = MagicMock()
        ok_event.type = EventType.OK

        bot.meshcore = MagicMock()
        bot.meshcore.is_connected = True
        bot.meshcore.commands.get_time = MagicMock(
            return_value=_make_coro(ok_event)
        )

        result = asyncio.run(bot._probe_radio_health())
        assert result is True
        assert bot._radio_fail_count == 0

    def test_success_logs_recovery_when_previously_failed(self, tmp_path):
        from meshcore.events import EventType
        bot = self._make_bot(tmp_path)
        bot._radio_fail_count = 1

        ok_event = MagicMock()
        ok_event.type = EventType.OK

        bot.meshcore = MagicMock()
        bot.meshcore.is_connected = True
        bot.meshcore.commands.get_time = MagicMock(
            return_value=_make_coro(ok_event)
        )

        with patch.object(bot.logger, "info") as mock_info:
            asyncio.run(bot._probe_radio_health())

        logged_messages = [str(c) for c in mock_info.call_args_list]
        assert any("recovered" in m for m in logged_messages)

    def test_success_no_recovery_log_when_no_prior_failure(self, tmp_path):
        from meshcore.events import EventType
        bot = self._make_bot(tmp_path)
        bot._radio_fail_count = 0

        ok_event = MagicMock()
        ok_event.type = EventType.OK

        bot.meshcore = MagicMock()
        bot.meshcore.is_connected = True
        bot.meshcore.commands.get_time = MagicMock(
            return_value=_make_coro(ok_event)
        )

        with patch.object(bot.logger, "info") as mock_info:
            asyncio.run(bot._probe_radio_health())

        logged_messages = [str(c) for c in mock_info.call_args_list]
        assert not any("recovered" in m for m in logged_messages)

    def test_asyncio_timeout_returns_false_and_warns(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.meshcore = MagicMock()
        bot.meshcore.is_connected = True

        async def slow_get_time():
            raise asyncio.TimeoutError()

        bot.meshcore.commands.get_time = MagicMock(return_value=slow_get_time())

        async def run():
            async def fake_wait_for(coro, timeout):
                raise asyncio.TimeoutError()

            with patch("asyncio.wait_for", side_effect=fake_wait_for):
                return await bot._probe_radio_health()

        result = asyncio.run(run())
        assert result is False

    def test_generic_exception_returns_false_and_warns(self, tmp_path):
        bot = self._make_bot(tmp_path)
        bot.meshcore = MagicMock()
        bot.meshcore.is_connected = True

        async def failing_get_time():
            raise RuntimeError("device exploded")

        bot.meshcore.commands.get_time = MagicMock(
            return_value=failing_get_time()
        )

        with patch.object(bot.logger, "warning") as mock_warn:
            result = asyncio.run(bot._probe_radio_health())

        assert result is False
        assert mock_warn.called


# ---------------------------------------------------------------------------
# Helper: create a coroutine that returns a fixed value
# ---------------------------------------------------------------------------


async def _make_coro_async(value):
    return value


def _make_coro(value):
    """Return a coroutine that immediately resolves to *value*."""
    return _make_coro_async(value)
