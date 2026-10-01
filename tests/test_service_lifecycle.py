"""Service restarts must not double-subscribe, and services that decline to start are not restarted forever."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from meshcore.events import EventDispatcher, EventType

from modules.service_plugins.repeater_prefix_collision_service import RepeaterPrefixCollisionService
from tests.test_repeater_prefix_collision_service import _make_bot


def _subscriptions(dispatcher, event_type):
    return [s for s in dispatcher.subscriptions if s.event_type == event_type]


def test_restart_leaves_exactly_one_subscription():
    bot = _make_bot()
    bot.meshcore = EventDispatcher()
    service = RepeaterPrefixCollisionService(bot)

    async def cycle():
        await service.start()
        await service.stop()
        await service.start()

    asyncio.run(cycle())
    assert len(_subscriptions(bot.meshcore, EventType.NEW_CONTACT)) == 1
    asyncio.run(service.stop())
    assert _subscriptions(bot.meshcore, EventType.NEW_CONTACT) == []


def test_reconnect_drops_the_old_instance_subscription():
    bot = _make_bot()
    old = bot.meshcore = EventDispatcher()
    service = RepeaterPrefixCollisionService(bot)
    asyncio.run(service.start())
    new = bot.meshcore = EventDispatcher()
    asyncio.run(service.on_transport_reconnected())
    assert _subscriptions(old, EventType.NEW_CONTACT) == []
    assert len(_subscriptions(new, EventType.NEW_CONTACT)) == 1


def test_base_unsubscribe_tolerates_errors():
    bot = _make_bot()
    service = RepeaterPrefixCollisionService(bot)
    broken = MagicMock()
    broken.unsubscribe.side_effect = RuntimeError("gone")
    meshcore = MagicMock()
    meshcore.subscribe.return_value = broken
    service._subscribe(meshcore, EventType.NEW_CONTACT, service._on_new_contact)
    service._unsubscribe_all()
    broken.unsubscribe.assert_called_once()
    assert service._meshcore_subscriptions == []


def test_packet_capture_restart_clears_should_exit():
    import configparser

    from modules.service_plugins.packet_capture_service import PacketCaptureService

    bot = MagicMock()
    bot.config = configparser.ConfigParser()
    bot.config.read_dict({"PacketCapture": {"enabled": "true"}})
    bot.connected = False
    service = PacketCaptureService(bot)
    service.enabled = True
    service.should_exit = True  # as stop() leaves it

    async def no_wait(*args, **kwargs):
        return None

    from unittest.mock import patch

    with patch("asyncio.sleep", no_wait):
        asyncio.run(service.start())  # gives up waiting for the radio, then returns
    assert service.should_exit is False


def _core_bot(tmp_path: Path):
    from modules.core import MeshCoreBot
    from tests.test_core import _write_config

    config_file = tmp_path / "config.ini"
    _write_config(config_file, tmp_path / "bot.db")
    return MeshCoreBot(config_file=str(config_file))


class _Declining:
    """A service whose start() returns without running, like Discord with no channels."""

    enabled = True

    def __init__(self):
        self.starts = 0

    async def start(self):
        self.starts += 1

    async def stop(self):
        pass

    def is_running(self):
        return False

    def is_healthy(self):
        return False


class _StillUnhealthy(_Declining):
    def is_running(self):
        return True


class _Recovers(_Declining):
    """Declines while the radio is down, then starts once it is back."""

    def __init__(self):
        super().__init__()
        self.radio_up = False
        self._running = False

    async def start(self):
        self.starts += 1
        self._running = self.radio_up

    def is_running(self):
        return self._running

    def is_healthy(self):
        return self._running


def test_a_service_that_declines_to_start_waits_out_the_backoff(tmp_path):
    bot = _core_bot(tmp_path)
    service = _Declining()
    assert asyncio.run(bot._restart_service("declining", service)) is False
    failed_at = bot._service_restart_failures["declining"]
    backoff = 300
    assert bot._service_restart_due("declining", service, failed_at + 5, backoff) is False
    assert bot._service_restart_due("declining", service, failed_at + backoff, backoff) is True


def test_a_transient_start_failure_recovers_after_the_backoff(tmp_path):
    bot = _core_bot(tmp_path)
    service = _Recovers()
    assert asyncio.run(bot._restart_service("bridge", service)) is False  # radio still down
    service.radio_up = True
    later = bot._service_restart_failures["bridge"] + 300
    assert bot._service_restart_due("bridge", service, later, 300) is True
    assert asyncio.run(bot._restart_service("bridge", service)) is True
    assert "bridge" not in bot._service_restart_failures
    assert bot._service_restart_due("bridge", service, later + 5, 300) is False  # healthy now


def test_restart_due_skips_disabled_healthy_and_in_progress_services(tmp_path):
    bot = _core_bot(tmp_path)
    unhealthy = _Declining()
    assert bot._service_restart_due("a", unhealthy, 0.0, 300) is True
    disabled = _Declining()
    disabled.enabled = False
    assert bot._service_restart_due("b", disabled, 0.0, 300) is False
    healthy = _Recovers()
    healthy._running = True
    assert bot._service_restart_due("c", healthy, 0.0, 300) is False
    bot._service_restarting.add("a")
    assert bot._service_restart_due("a", unhealthy, 0.0, 300) is False


def test_a_restart_that_leaves_the_service_unhealthy_backs_off(tmp_path):
    bot = _core_bot(tmp_path)
    assert asyncio.run(bot._restart_service("flaky", _StillUnhealthy())) is False
    assert "flaky" in bot._service_restart_failures


def _plain_bot(sections):
    import configparser

    bot = MagicMock()
    bot.config = configparser.ConfigParser()
    bot.config.read_dict(sections)
    bot.bot_root = Path("/tmp")
    return bot


def test_bridges_close_their_http_session_when_meshcore_is_missing():
    from modules.service_plugins.discord_bridge_service import DiscordBridgeService
    from modules.service_plugins.telegram_bridge_service import TelegramBridgeService

    for cls, section, mapping in (
        (DiscordBridgeService, "DiscordBridge", "channel_webhooks"),
        (TelegramBridgeService, "TelegramBridge", "channel_chat_ids"),
    ):
        bot = _plain_bot({section: {"enabled": "true"}})
        service = cls(bot)
        service.enabled = True
        setattr(service, mapping, {"general": ["target"]})
        bot.meshcore = None

        async def run(service=service):
            await service.start()
            return service.http_session

        assert asyncio.run(run()) is None, cls.__name__
        assert service.is_running() is False


def test_map_uploader_restart_restores_its_file_log_and_clears_should_exit(tmp_path):
    import logging
    import logging as _logging

    from modules.service_plugins.map_uploader_service import MapUploaderService

    bot = _plain_bot({"MapUploader": {"enabled": "true"}, "Logging": {"log_file": str(tmp_path / "bot.log")}})
    bot.logger = _logging.getLogger("test-bot")
    bot.connected = False
    service = MapUploaderService(bot)
    service.enabled = True

    def file_handlers():
        return [h for h in service.logger.handlers if isinstance(h, logging.FileHandler)]

    assert len(file_handlers()) == 1
    asyncio.run(service.stop())
    assert file_handlers() == [] and service.should_exit is True

    async def no_wait(*args, **kwargs):
        return None

    from unittest.mock import patch

    with patch("asyncio.sleep", no_wait):
        asyncio.run(service.start())  # gives up waiting for the radio
    assert len(file_handlers()) == 1
    assert service.should_exit is False
    for handler in file_handlers():
        handler.close()
        service.logger.removeHandler(handler)


def test_health_step_restarts_only_services_that_are_due(tmp_path):
    from unittest.mock import AsyncMock

    bot = _core_bot(tmp_path)
    due, backing_off = _Declining(), _Declining()
    bot.services = {"due": due, "backing_off": backing_off}
    bot._service_restart_failures["backing_off"] = 1000.0
    bot._restart_service = AsyncMock(return_value=False)

    async def step():
        bot._restart_unhealthy_services(now=1010.0, backoff=300)
        await asyncio.sleep(0)

    asyncio.run(step())
    bot._restart_service.assert_awaited_once_with("due", due)


def test_boot_start_failures_and_disabled_services(tmp_path):
    bot = _core_bot(tmp_path)

    class Raises(_Declining):
        async def start(self):
            raise OSError("port in use")

    asyncio.run(bot._start_service_at_boot("raises", Raises(), started="ok", failed="failed"))
    assert "raises" in bot._service_restart_failures

    disabled = _Declining()
    disabled.enabled = False
    asyncio.run(bot._start_service_at_boot("off", disabled, started="ok", failed="failed"))
    assert "off" not in bot._service_restart_failures


def test_darc_mowas_is_not_running_when_its_port_is_taken():
    import socket

    from modules.service_plugins.darc_mowas_service import DARC_MoWaS_Service

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        bot = _plain_bot({"DARC_MoWaS_Service": {"enabled": "true", "host": "127.0.0.1", "port": str(port)}})
        service = DARC_MoWaS_Service(bot)
        service.host, service.port = "127.0.0.1", port
        service._ensure_channels = lambda: asyncio.sleep(0)
        import pytest

        with pytest.raises(OSError):  # not SystemExit: a taken port must not stop the bot
            asyncio.run(service.start())
        assert service.is_healthy() is False
    finally:
        holder.close()
