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
