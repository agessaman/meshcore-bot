"""Issue #364: one incoming channel command must be handled once.

Overlapping radio relinks used to subscribe CHANNEL_MSG_RECV twice on the
live MeshCore instance, so every channel command ran twice in one process.
"""

import asyncio
import configparser
import contextvars
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from meshcore import EventType
from meshcore.events import Event, EventDispatcher

from modules.core import MeshCoreBot
from modules.message_handler import MessageHandler

# Default window. Kept literal so this file imports on the unfixed code.
_DUPLICATE_WINDOW_SECONDS = 5.0

_ORIG_SLEEP = asyncio.sleep
# Set only around the automatic transport reconnect, so a test can pause that
# connect without also pausing the manual reboot/reconnect that overlaps it.
_in_transport_reconnect: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "in_transport_reconnect", default=False
)


def _write_config(path: Path, db_path: Path) -> None:
    path.write_text(
        f"""[Connection]
connection_type = ble
ble_device_name = TestRadio
reconnect_max_retries = 1
reconnect_delay_seconds = 0.01
reconnect_max_delay_seconds = 0.01

[Bot]
db_path = {db_path.as_posix()}
prefix_bytes = 1

[Channels]
monitor_channels = #general
""",
        encoding="utf-8",
    )


async def _fast_sleep(delay: float = 0, result: object = None) -> object:
    """Drop the multi-second reconnect and handler-setup pauses.

    Still yield once, so a reconnect task scheduled just before the pause can
    reach its first real wait. A no-yield stub would hide that overlap.
    """
    if delay >= 0.05:
        await _ORIG_SLEEP(0)
        return result
    return await _ORIG_SLEEP(delay, result)


class FakeMeshCore:
    """Enough of a MeshCore connection for connect() and handler setup."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.is_connected = True
        self.self_info = {"name": name}
        self.channels: dict[str, object] = {}
        self.contacts: dict[str, object] = {}
        self.commands = None
        self.dispatcher = EventDispatcher()
        self.fetch_loops = 0

    def subscribe(self, event_type, callback, attribute_filters=None):
        return self.dispatcher.subscribe(event_type, callback, attribute_filters)

    async def start_auto_message_fetching(self) -> None:
        self.fetch_loops += 1

    async def disconnect(self) -> None:
        self.is_connected = False


class CountingHandler:
    def __init__(self) -> None:
        self.channel_calls = 0

    async def handle_channel_message(self, event, metadata=None) -> None:
        self.channel_calls += 1

    async def handle_contact_message(self, event, metadata=None) -> None:
        return None

    async def handle_rf_log_data(self, event, metadata=None) -> None:
        return None

    async def handle_raw_data(self, event, metadata=None) -> None:
        return None

    async def handle_new_contact(self, event, metadata=None) -> None:
        return None


def _make_bot(tmp_path: Path) -> MeshCoreBot:
    config_file = tmp_path / "config.ini"
    _write_config(config_file, tmp_path / "bot.db")
    bot = MeshCoreBot(config_file=str(config_file))
    bot.transmission_tracker = None
    bot.message_handler = CountingHandler()

    async def _fetch_channels(*_args, **_kwargs) -> bool:
        return True

    async def _ok(*_args, **_kwargs) -> bool:
        return True

    bot.channel_manager.fetch_channels = _fetch_channels
    bot.set_radio_clock = _ok
    bot.set_device_name = _ok
    bot._notify_services_transport_reconnected = _ok
    return bot


def _channel_subs(meshcore: FakeMeshCore) -> list:
    return [
        sub
        for sub in meshcore.dispatcher.subscriptions
        if sub.event_type == EventType.CHANNEL_MSG_RECV
    ]


async def _deliver_one_channel_event(meshcore: FakeMeshCore) -> None:
    event = Event(
        EventType.CHANNEL_MSG_RECV,
        {"channel_idx": 2, "text": "Adrian: !ping", "sender_timestamp": 1},
    )
    for sub in _channel_subs(meshcore):
        await sub.callback(event)


def _assert_single_live_subscription(bot: MeshCoreBot) -> None:
    live = bot.meshcore
    assert isinstance(live, FakeMeshCore)
    subs = _channel_subs(live)
    assert len(subs) == 1, f"{live.name} has {len(subs)} CHANNEL_MSG_RECV subscriptions"
    assert live.fetch_loops == 1


class _ConnectFactory:
    def __init__(self) -> None:
        self.created: list[FakeMeshCore] = []

    async def __call__(self, *_args, **_kwargs) -> FakeMeshCore:
        mc = FakeMeshCore(f"M{len(self.created) + 1}")
        self.created.append(mc)
        return mc


# ---------------------------------------------------------------------------
# Overlapping relinks
# ---------------------------------------------------------------------------


class TestOverlappingRelinks:
    async def test_overlapping_connect_and_attempt_reconnect_subscribes_once(self, tmp_path):
        """The race_sim case: a second connect runs while the first is still in setup."""
        bot = _make_bot(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()
        waits = {"n": 0}

        async def wait_for_contacts() -> None:
            waits["n"] += 1
            if waits["n"] == 1:
                entered.set()
                await release.wait()

        bot.wait_for_contacts = wait_for_contacts
        factory = _ConnectFactory()

        with (
            patch("meshcore.MeshCore.create_ble", new=factory),
            patch("asyncio.sleep", new=_fast_sleep),
        ):
            first = asyncio.create_task(bot.connect())
            await asyncio.wait_for(entered.wait(), timeout=2)
            second = asyncio.create_task(bot._attempt_reconnect())
            try:
                for _ in range(50):
                    await asyncio.sleep(0)
                    if second.done():
                        break
                release.set()
                await asyncio.wait_for(first, timeout=2)
                await asyncio.wait_for(second, timeout=2)
                await _deliver_one_channel_event(bot.meshcore)
            finally:
                release.set()

        _assert_single_live_subscription(bot)
        assert bot.message_handler.channel_calls == 1

    async def test_reboot_versus_scheduled_reconnect_subscribes_once(self, tmp_path):
        await _manual_relink_versus_scheduled_reconnect(tmp_path, kind="reboot")

    async def test_reconnect_versus_poll_detected_subscribes_once(self, tmp_path):
        await _manual_relink_versus_scheduled_reconnect(tmp_path, kind="reconnect")

    def test_schedule_skipped_while_manual_relink_in_progress(self, tmp_path):
        bot = _make_bot(tmp_path)
        bot.connected = True
        bot._radio_relinks_in_progress = 1

        created: list[object] = []

        def swallow(coro, *_args, **_kwargs):
            created.append(coro)
            coro.close()
            return Mock()

        async def run() -> None:
            with patch("asyncio.create_task", side_effect=swallow):
                await bot._schedule_transport_reconnect("poll_detected")

        asyncio.run(run())
        assert created == []
        assert bot._transport_reconnect_in_progress is False

    async def test_queued_reconnect_skips_a_replaced_connection(self, tmp_path):
        """A reconnect queued for the old link must not tear down the new one."""
        bot = _make_bot(tmp_path)
        old = FakeMeshCore("old")
        new = FakeMeshCore("new")
        bot.meshcore = new
        bot.connected = True
        called: list[bool] = []

        async def attempt() -> bool:
            called.append(True)
            return True

        bot._attempt_reconnect = attempt
        await bot._run_transport_reconnect(old)
        assert called == []
        assert bot.meshcore is new
        assert new.is_connected is True
        assert bot.connected is True
        assert bot._transport_reconnect_in_progress is False

    async def test_queued_reconnect_still_runs_for_the_link_that_dropped(self, tmp_path):
        bot = _make_bot(tmp_path)
        old = FakeMeshCore("old")
        old.is_connected = False
        bot.meshcore = old
        bot.connected = True
        called: list[bool] = []

        async def attempt() -> bool:
            called.append(True)
            bot.connected = True
            return True

        bot._attempt_reconnect = attempt
        await bot._run_transport_reconnect(old)
        assert called == [True]

    async def test_queued_reconnect_does_not_undo_stop_bot(self, tmp_path):
        """A reconnect queued before Stop Bot must not connect once Stop Bot finishes.

        The reconnect coroutine is held back until disconnect_radio returns, which
        is when it would acquire the relink lock in production.
        """
        bot = _make_bot(tmp_path)
        old = FakeMeshCore("old")
        bot.meshcore = old
        bot.connected = True
        scheduled: list[object] = []
        real_create_task = asyncio.create_task

        def capture(coro, *_args, **_kwargs):
            scheduled.append(coro)
            return real_create_task(asyncio.sleep(0))

        with patch("asyncio.create_task", side_effect=capture):
            await bot._schedule_transport_reconnect("disconnected")
        assert len(scheduled) == 1
        assert await bot.disconnect_radio() is True

        await scheduled[0]
        assert bot.connected is False
        assert bot.meshcore is old
        assert old.is_connected is False

    async def test_reconnect_after_disconnect_restores_auto_reconnect(self, tmp_path):
        await _disconnect_then_manual_relink_restores_auto_reconnect(tmp_path, kind="reconnect")

    async def test_reboot_after_disconnect_restores_auto_reconnect(self, tmp_path):
        await _disconnect_then_manual_relink_restores_auto_reconnect(tmp_path, kind="reboot")

    async def test_stop_returns_while_connect_holds_the_relink_lock(self, tmp_path):
        """stop() must not wait on a connect that is blocked in setup."""
        bot = _make_bot(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def wait_for_contacts() -> None:
            entered.set()
            await release.wait()

        bot.wait_for_contacts = wait_for_contacts
        factory = _ConnectFactory()

        with (
            patch("meshcore.MeshCore.create_ble", new=factory),
            patch("asyncio.sleep", new=_fast_sleep),
        ):
            connect_task = asyncio.create_task(bot.connect())
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                await asyncio.wait_for(bot.stop(), timeout=2)
            finally:
                release.set()
                await asyncio.wait_for(connect_task, timeout=2)


async def _disconnect_then_manual_relink_restores_auto_reconnect(tmp_path: Path, kind: str) -> None:
    """Disconnect, then Connect or Reboot, then a later drop must auto-reconnect.

    The 8s reboot wait is the patched ``_fast_sleep``, not a real sleep.
    """
    bot = _make_bot(tmp_path)
    old = FakeMeshCore("old")
    old.commands = SimpleNamespace(reboot=AsyncMock())
    bot.meshcore = old
    bot.connected = True
    factory = _ConnectFactory()
    relink = bot.reboot_radio if kind == "reboot" else bot.reconnect_radio

    with (
        patch("meshcore.MeshCore.create_ble", new=factory),
        patch("asyncio.sleep", new=_fast_sleep),
    ):
        assert await bot.disconnect_radio() is True
        assert await relink() is True

    live = bot.meshcore
    assert isinstance(live, FakeMeshCore)
    assert live is not old
    assert bot.connected is True
    assert bot._manual_radio_disconnect is False

    called: list[bool] = []

    async def attempt() -> bool:
        called.append(True)
        return True

    bot._attempt_reconnect = attempt
    subs = [
        sub
        for sub in live.dispatcher.subscriptions
        if sub.event_type == EventType.DISCONNECTED
    ]
    assert len(subs) == 1
    await subs[0].callback(Event(EventType.DISCONNECTED, {"reason": "disconnected"}))
    for _ in range(20):
        if called and not bot._transport_reconnect_in_progress:
            break
        await asyncio.sleep(0)

    assert called == [True]
    assert bot._transport_reconnect_in_progress is False


async def _manual_relink_versus_scheduled_reconnect(tmp_path: Path, kind: str) -> None:
    """Firmware drop schedules a reconnect while reboot/reconnect is mid-teardown."""
    bot = _make_bot(tmp_path)
    current = FakeMeshCore("old")
    bot.meshcore = current
    bot.connected = True
    release = asyncio.Event()
    blocked = asyncio.Event()
    original_attempt = bot._attempt_reconnect

    async def attempt_reconnect() -> bool:
        token = _in_transport_reconnect.set(True)
        try:
            return await original_attempt()
        finally:
            _in_transport_reconnect.reset(token)

    bot._attempt_reconnect = attempt_reconnect

    async def wait_for_contacts() -> None:
        # Only the automatic reconnect pauses, and it pauses after it has stored
        # its own MeshCore instance. The manual relink then finishes on another.
        if _in_transport_reconnect.get():
            blocked.set()
            await release.wait()

    async def disconnect() -> None:
        current.is_connected = False
        # _attempt_reconnect disconnects this same object. That call is the
        # reconnect already in progress; scheduling again would deadlock it.
        if bot._transport_reconnect_in_progress:
            return
        reason = "disconnected" if kind == "reboot" else "poll_detected"
        # connected is still True here: reboot/reconnect clear it after this returns.
        await bot._schedule_transport_reconnect(reason)
        for _ in range(30):
            await asyncio.sleep(0)
            if blocked.is_set() or not bot._transport_reconnect_in_progress:
                break

    current.disconnect = disconnect
    if kind == "reboot":
        current.commands = SimpleNamespace(reboot=AsyncMock())
    bot.wait_for_contacts = wait_for_contacts
    factory = _ConnectFactory()
    relink = bot.reboot_radio if kind == "reboot" else bot.reconnect_radio

    with (
        patch("meshcore.MeshCore.create_ble", new=factory),
        patch("asyncio.sleep", new=_fast_sleep),
    ):
        relink_task = asyncio.create_task(relink())
        try:
            for _ in range(200):
                await asyncio.sleep(0)
                if relink_task.done():
                    break
            else:
                pytest.fail(f"{kind} did not finish")
            release.set()
            await relink_task
            for _ in range(200):
                await asyncio.sleep(0)
                if not bot._transport_reconnect_in_progress:
                    break
            await _deliver_one_channel_event(bot.meshcore)
        finally:
            release.set()

    _assert_single_live_subscription(bot)
    assert bot.message_handler.channel_calls == 1
    assert bot._radio_relinks_in_progress == 0


# ---------------------------------------------------------------------------
# setup_message_handlers
# ---------------------------------------------------------------------------


class TestSetupMessageHandlersIdempotent:
    async def test_second_call_on_same_instance_subscribes_once(self, tmp_path):
        bot = _make_bot(tmp_path)
        mc = FakeMeshCore("only")
        bot.meshcore = mc
        debugs: list[str] = []
        infos: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno == logging.DEBUG:
                    debugs.append(record.getMessage())
                elif record.levelno == logging.INFO:
                    infos.append(record.getMessage())

        bot.logger.addHandler(_Capture())
        bot.logger.setLevel(logging.DEBUG)
        with patch("asyncio.sleep", new=_fast_sleep):
            await bot.setup_message_handlers()
            await bot.setup_message_handlers()

        assert len(_channel_subs(mc)) == 1
        assert mc.fetch_loops == 1
        assert infos.count("Message handlers setup complete") == 1
        assert infos.count("NEW_CONTACT subscription active - ready to receive new contact events") == 1
        assert any("already installed" in message for message in debugs)

    async def test_second_instance_still_subscribes(self, tmp_path):
        bot = _make_bot(tmp_path)
        first = FakeMeshCore("A")
        second = FakeMeshCore("B")
        with patch("asyncio.sleep", new=_fast_sleep):
            bot.meshcore = first
            await bot.setup_message_handlers()
            bot.meshcore = second
            await bot.setup_message_handlers()
        assert len(_channel_subs(first)) == 1
        assert len(_channel_subs(second)) == 1
        assert first.fetch_loops == 1
        assert second.fetch_loops == 1


# ---------------------------------------------------------------------------
# Duplicate message backstop
# ---------------------------------------------------------------------------


def _channel_handler() -> MessageHandler:
    bot = Mock()
    bot.logger = Mock()
    bot.config = configparser.ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "rf_data_timeout", "15.0")
    bot.config.set("Bot", "message_correlation_timeout", "10.0")
    bot.config.set("Bot", "enable_enhanced_correlation", "false")
    bot.connection_time = None
    bot.prefix_hex_chars = 2
    bot.command_manager = Mock()
    bot.command_manager.flood_scope_keys = {}
    bot.command_manager.flood_scope_allow_global = False
    bot.command_manager.commands = {}
    bot.meshcore = Mock()
    bot.meshcore.contacts = {}
    bot.channel_manager = Mock()
    bot.channel_manager.get_channel_name = Mock(return_value="general")
    bot.translator = None
    bot.mesh_graph = None
    bot.region_warning_monitor = None
    handler = MessageHandler(bot)
    handler.logger = Mock()
    handler.enhanced_correlation = False
    return handler


def _channel_event(timestamp: int = 1_700_000_000, text: str = "Ada: !ping", txt_type: int | None = 0):
    event = Mock()
    payload = {
        "channel_idx": 0,
        "text": text,
        "sender_timestamp": timestamp,
    }
    if txt_type is not None:
        payload["txt_type"] = txt_type
    event.payload = payload
    return event


async def _handle_channel(handler: MessageHandler, event, process: AsyncMock) -> None:
    with (
        patch.object(handler, "process_message", process),
        patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock),
        patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock),
    ):
        await handler.handle_channel_message(event)


class TestDuplicateChannelMessage:
    async def test_identical_payload_is_processed_once(self):
        handler = _channel_handler()
        process = AsyncMock()
        clock = {"t": 1_000.0}

        def monotonic() -> float:
            return clock["t"]

        event = _channel_event()
        with patch("modules.message_handler.time.monotonic", monotonic):
            await _handle_channel(handler, event, process)
            clock["t"] = 1_004.9
            await _handle_channel(handler, _channel_event(), process)

        assert process.await_count == 1
        drop_logs = [str(call) for call in handler.logger.info.call_args_list]
        assert any("Dropping duplicate channel message" in message for message in drop_logs)

    async def test_same_text_with_a_new_timestamp_is_processed_twice(self):
        handler = _channel_handler()
        process = AsyncMock()
        clock = {"t": 1_000.0}

        def monotonic() -> float:
            return clock["t"]

        with patch("modules.message_handler.time.monotonic", monotonic):
            await _handle_channel(handler, _channel_event(timestamp=1_700_000_000), process)
            await _handle_channel(handler, _channel_event(timestamp=1_700_000_001), process)

        assert process.await_count == 2

    async def test_identical_payload_after_the_window_is_processed_twice(self):
        handler = _channel_handler()
        process = AsyncMock()
        clock = {"t": 50.0}

        def monotonic() -> float:
            return clock["t"]

        with patch("modules.message_handler.time.monotonic", monotonic):
            await _handle_channel(handler, _channel_event(), process)
            clock["t"] = 50.0 + _DUPLICATE_WINDOW_SECONDS
            await _handle_channel(handler, _channel_event(), process)

        assert process.await_count == 2

    async def test_missing_identity_is_not_filtered(self):
        handler = _channel_handler()
        process = AsyncMock()
        await _handle_channel(handler, _channel_event(txt_type=None), process)
        await _handle_channel(handler, _channel_event(txt_type=None), process)
        assert process.await_count == 2

    async def test_duplicate_is_decided_before_the_first_await(self):
        """Two handlers can run interleaved; the second must lose before either awaits."""
        handler = _channel_handler()
        process = AsyncMock()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = {"n": 0}

        async def correlate(*_args, **_kwargs):
            calls["n"] += 1
            # Block only the first lookup. The handler looks twice (route, then
            # scope); a duplicate must not reach either of them.
            if calls["n"] == 1:
                entered.set()
                await release.wait()
            return None

        event = _channel_event()
        with (
            patch.object(handler, "process_message", process),
            patch.object(handler, "_correlate_channel_message_rf_data", side_effect=correlate),
            patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock),
            patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock),
        ):
            first = asyncio.create_task(handler.handle_channel_message(event))
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                try:
                    await asyncio.wait_for(
                        handler.handle_channel_message(_channel_event()), timeout=0.05
                    )
                except TimeoutError:
                    pytest.fail("duplicate check did not happen before the first await")
                assert calls["n"] == 1
            finally:
                release.set()
                await first

        assert process.await_count == 1

    def test_recent_identity_cache_is_bounded(self):
        handler = _channel_handler()
        cache = handler._recent_channel_ids
        for i in range(600):
            assert handler._claim_recent_message(cache, f"id-{i}") is False
        assert len(cache) == 512
        assert "id-0" not in cache
        assert "id-599" in cache


class TestDuplicateContactMessage:
    async def test_identical_dm_is_processed_once_and_a_new_timestamp_twice(self):
        bot = Mock()
        bot.logger = Mock()
        bot.config = configparser.ConfigParser()
        bot.config.add_section("Bot")
        bot.config.set("Bot", "rf_data_timeout", "15.0")
        bot.config.set("Bot", "message_correlation_timeout", "10.0")
        bot.config.set("Bot", "enable_enhanced_correlation", "false")
        bot.connection_time = None
        bot.prefix_hex_chars = 2
        bot.meshcore = Mock()
        bot.meshcore.contacts = {}
        bot.translator = None
        handler = MessageHandler(bot)
        handler.logger = Mock()
        process = AsyncMock()
        clock = {"t": 10.0}

        def monotonic() -> float:
            return clock["t"]

        def event(timestamp: int):
            ev = Mock()
            ev.payload = {
                "pubkey_prefix": "ab12",
                "text": "hello",
                "sender_timestamp": timestamp,
                "txt_type": 0,
                "path_len": 255,
            }
            ev.metadata = {}
            return ev

        with (
            patch("modules.message_handler.time.monotonic", monotonic),
            patch.object(handler, "process_message", process),
            patch.object(handler, "_debug_decode_message_path", new_callable=AsyncMock),
            patch.object(handler, "_debug_decode_packet_for_message", new_callable=AsyncMock),
        ):
            await handler.handle_contact_message(event(100))
            await handler.handle_contact_message(event(100))
            await handler.handle_contact_message(event(101))

        assert process.await_count == 2
        drop_logs = [str(call) for call in handler.logger.info.call_args_list]
        assert any("Dropping duplicate contact message" in message for message in drop_logs)
