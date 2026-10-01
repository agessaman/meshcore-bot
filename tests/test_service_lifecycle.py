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
    service._meshcore_subscriptions = [broken]
    service._unsubscribe_all()
    assert service._meshcore_subscriptions == []


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


def test_a_service_that_declines_to_start_is_not_restarted(tmp_path):
    bot = _core_bot(tmp_path)
    service = _Declining()
    assert asyncio.run(bot._restart_service("declining", service)) is False
    assert "declining" in bot._services_inactive
    assert "declining" not in bot._service_restart_failures


def test_a_restart_that_leaves_the_service_unhealthy_backs_off(tmp_path):
    bot = _core_bot(tmp_path)
    assert asyncio.run(bot._restart_service("flaky", _StillUnhealthy())) is False
    assert "flaky" in bot._service_restart_failures
    assert "flaky" not in bot._services_inactive
