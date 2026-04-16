"""Regression tests for graceful shutdown (idempotent stop, scheduler, MQTT logging)."""

import threading
from configparser import ConfigParser
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.core import MeshCoreBot
from modules.scheduler import MessageScheduler


def _write_minimal_config(path, db_path) -> None:
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
""",
        encoding="utf-8",
    )


@pytest.fixture
def shutdown_test_bot(tmp_path):
    config_file = tmp_path / "config.ini"
    db_path = tmp_path / "bot.db"
    _write_minimal_config(config_file, db_path)
    return MeshCoreBot(config_file=str(config_file))


@pytest.mark.asyncio
async def test_stop_is_idempotent(shutdown_test_bot):
    """Second stop() must not re-run teardown (entrypoint owns single shutdown)."""
    bot = shutdown_test_bot
    bot.feed_manager = MagicMock()
    bot.feed_manager.stop = AsyncMock()
    bot.mesh_graph = None
    bot.services = {}
    bot.web_viewer_integration = None
    bot.scheduler = MagicMock()
    bot.scheduler.scheduler_thread = None
    bot.meshcore = None

    await bot.stop()
    await bot.stop()

    bot.feed_manager.stop.assert_called_once()


@pytest.mark.asyncio
async def test_second_stop_skips_when_complete(shutdown_test_bot):
    bot = shutdown_test_bot
    bot.feed_manager = MagicMock()
    bot.feed_manager.stop = AsyncMock()
    bot.mesh_graph = None
    bot.services = {}
    bot.web_viewer_integration = None
    bot.scheduler = MagicMock()
    bot.scheduler.scheduler_thread = None
    bot.meshcore = None

    await bot.stop()
    bot.feed_manager.stop.reset_mock()
    await bot.stop()
    bot.feed_manager.stop.assert_not_called()


class TestWebViewerRestartGating:
    """restart_viewer must not run during bot shutdown."""

    def test_restart_viewer_noop_when_shutdown_event_set(self):
        from modules.web_viewer.integration import WebViewerIntegration

        bot = MagicMock()
        bot.logger = MagicMock()
        bot._shutdown_event = threading.Event()
        bot._shutdown_event.set()
        bot.connected = False

        cfg = ConfigParser()
        cfg.add_section("Web_Viewer")
        cfg.set("Web_Viewer", "enabled", "false")
        cfg.set("Web_Viewer", "auto_start", "false")
        cfg.set("Web_Viewer", "host", "127.0.0.1")
        cfg.set("Web_Viewer", "port", "8080")
        cfg.set("Web_Viewer", "debug", "false")
        bot.config = cfg

        with patch.object(WebViewerIntegration, "_validate_config"), patch(
            "modules.web_viewer.integration.BotIntegration", MagicMock()
        ):
            wvi = WebViewerIntegration(bot)

        with patch.object(wvi, "stop_viewer") as mock_stop:
            wvi.restart_viewer()
        mock_stop.assert_not_called()


class TestSchedulerShutdownSafe:
    """join() / setup must not spuriously log APScheduler 'not running' errors."""

    def test_join_twice_after_shutdown_no_not_running_debug(self, mock_logger):
        bot = MagicMock()
        bot.logger = mock_logger
        bot.config = ConfigParser()
        bot.config.add_section("Bot")
        scheduler = MessageScheduler(bot)
        scheduler.setup_scheduled_messages()
        scheduler.join(timeout=1)
        mock_logger.debug.reset_mock()
        scheduler.join(timeout=0.1)
        joined = " ".join(str(c) for c in mock_logger.debug.call_args_list).lower()
        assert "not running" not in joined


def test_mqtt_disconnect_logs_use_matched_client_config():
    """Mirrors PacketCaptureService on_disconnect: each client logs its own broker host."""
    log_hosts: list[str] = []

    class Svc:
        mqtt_clients: list
        mqtt_connected = False

    svc = Svc()

    def on_disconnect(client, userdata, rc, properties=None):
        for mqtt_info in svc.mqtt_clients:
            if mqtt_info["client"] == client:
                mqtt_info["connected"] = False
                cfg = mqtt_info["config"]
                host = cfg["host"]
                log_hosts.append(host)
                break
        svc.mqtt_connected = any(m.get("connected", False) for m in svc.mqtt_clients)

    c1, c2 = object(), object()
    svc.mqtt_clients = [
        {"client": c1, "config": {"host": "broker-a.example", "port": 1883}, "connected": True},
        {"client": c2, "config": {"host": "broker-b.example", "port": 1883}, "connected": True},
    ]
    on_disconnect(c1, None, 0)
    on_disconnect(c2, None, 0)
    assert log_hosts == ["broker-a.example", "broker-b.example"]
