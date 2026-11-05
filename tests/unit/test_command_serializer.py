"""Tests for the host->radio command serializer (_serialize_command_frames).

These validate the core mitigation for the firmware corruption / parser-desync
failure mode: every frame written to the radio is serialized to one in-flight
companion frame at a time and paced by a minimum inter-command interval, so the
firmware's single-threaded serial loop cannot be overrun. The lock covers a
frame and its immediate reply only, so commands that wait for ACKs or remote
responses don't block other senders.
"""

import asyncio
import time
from pathlib import Path

import pytest
from meshcore.commands import CommandHandler
from meshcore.events import Event, EventDispatcher, EventType

from modules.core import MeshCoreBot, _serialize_command_frames


def _make_bot(tmp_path: Path, min_interval_ms: int = 30) -> MeshCoreBot:
    config_file = tmp_path / "config.ini"
    db_path = tmp_path / "bot.db"
    config_file.write_text(
        f"""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0
command_min_interval_ms = {min_interval_ms}

[Bot]
db_path = {db_path.as_posix()}
prefix_bytes = 1

[Channels]
monitor_channels = #general
""",
        encoding="utf-8",
    )
    return MeshCoreBot(config_file=str(config_file))


class FakeCommands:
    """Stand-in for meshcore.commands: every command writes through send()."""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.frames = []

    async def send(self, data, expected_events=None, timeout=None, delay=0.01):
        self.frames.append(data)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(delay)
            return "ok"
        finally:
            self.active -= 1

    async def send_and_wait_for_reply(self, data, wait: float):
        """Like send_msg_with_retry: write one frame, then wait for a remote reply."""
        await self.send(data)
        await asyncio.sleep(wait)
        return "replied"


class FakeMeshcore:
    def __init__(self):
        self.commands = FakeCommands()


def test_min_interval_parsed_from_config(tmp_path):
    bot = _make_bot(tmp_path, min_interval_ms=45)
    assert bot._radio_cmd_min_interval == pytest.approx(0.045)


def test_min_interval_negative_clamped_to_zero(tmp_path):
    bot = _make_bot(tmp_path, min_interval_ms=-100)
    assert bot._radio_cmd_min_interval == 0.0


async def test_serializes_concurrent_frames(tmp_path):
    """Only one frame may be in flight at a time."""
    bot = _make_bot(tmp_path, min_interval_ms=0)  # isolate mutex from pacing
    fake = FakeCommands()
    _serialize_command_frames(bot, fake)

    await asyncio.gather(*(fake.send(b"\x01") for _ in range(10)))

    assert len(fake.frames) == 10
    assert fake.max_active == 1  # never more than one in-flight frame


async def test_pacing_enforces_minimum_gap(tmp_path):
    bot = _make_bot(tmp_path, min_interval_ms=50)
    fake = FakeCommands()
    _serialize_command_frames(bot, fake)

    start = time.monotonic()
    for _ in range(5):
        await fake.send(b"\x01", delay=0.0)
    elapsed = time.monotonic() - start

    # 5 frames => 4 enforced gaps of ~50ms (first frame is not delayed).
    assert elapsed >= 0.18


async def test_wrapped_send_returns_value(tmp_path):
    bot = _make_bot(tmp_path, min_interval_ms=0)
    fake = FakeCommands()
    _serialize_command_frames(bot, fake)

    assert await fake.send(b"\x01", delay=0.0) == "ok"


async def test_reply_wait_does_not_hold_the_lock(tmp_path):
    """A command waiting for a remote reply must not block other frames."""
    bot = _make_bot(tmp_path, min_interval_ms=0)
    fake = FakeCommands()
    _serialize_command_frames(bot, fake)

    waiting = asyncio.create_task(fake.send_and_wait_for_reply(b"\x02", wait=1.0))
    await asyncio.sleep(0.05)  # first frame written, now waiting for the reply

    start = time.monotonic()
    assert await fake.send(b"\x03") == "ok"
    assert time.monotonic() - start < 0.5
    assert not waiting.done()

    waiting.cancel()


async def test_send_msg_with_retry_ack_wait_does_not_block_other_commands(tmp_path):
    """meshcore's own retry loop sends through the wrapped send(), outside the lock."""
    bot = _make_bot(tmp_path, min_interval_ms=0)
    dispatcher = EventDispatcher()
    await dispatcher.start()
    try:
        handler = CommandHandler()
        handler.set_dispatcher(dispatcher)
        contact = {"public_key": "ab" * 32, "out_path_len": 0, "out_path": ""}
        handler._get_contact_by_prefix = lambda prefix: contact
        frames = []

        async def radio(data):
            data = bytes(data)
            frames.append(data[0])
            if data[0] == 0x02:  # CMD_SEND_TXT_MSG: accepted, but never ACKed
                await dispatcher.dispatch(Event(
                    EventType.MSG_SENT,
                    {"type": 0, "expected_ack": b"\x01\x02\x03\x04", "suggested_timeout": 1000},
                ))
            else:
                await dispatcher.dispatch(Event(EventType.OK, {}))

        handler._sender_func = radio
        _serialize_command_frames(bot, handler)

        dm = asyncio.create_task(handler.send_msg_with_retry(contact, "hi", max_attempts=1))
        await asyncio.sleep(0.1)  # DM sent, waiting for its ACK

        start = time.monotonic()
        result = await asyncio.wait_for(handler.send(b"\x14", [EventType.OK]), timeout=0.5)
        assert result.type == EventType.OK
        assert time.monotonic() - start < 0.5
        assert not dm.done()

        assert await dm is None  # no ACK arrived
        assert frames == [0x02, 0x14]
    finally:
        await dispatcher.stop()


async def test_radio_session_keeps_a_sequence_together(tmp_path):
    """Frames from another task wait until the session ends; frames inside it don't deadlock."""
    bot = _make_bot(tmp_path, min_interval_ms=0)
    fake = FakeCommands()
    _serialize_command_frames(bot, fake)

    async def other_sender():
        await asyncio.sleep(0.01)
        await fake.send(b"other", delay=0.0)

    other = asyncio.create_task(other_sender())
    async with bot.radio_session():
        await fake.send(b"set-scope")
        await asyncio.sleep(0.05)  # the other task tries to send here
        await fake.send(b"send")
        await fake.send(b"restore")
    await other

    assert fake.frames == [b"set-scope", b"send", b"restore", b"other"]


async def test_radio_session_is_reentrant(tmp_path):
    bot = _make_bot(tmp_path, min_interval_ms=0)
    fake = FakeCommands()
    _serialize_command_frames(bot, fake)

    async def nested():
        async with bot.radio_session():
            async with bot.radio_session():
                await fake.send(b"\x01")

    await asyncio.wait_for(nested(), timeout=1.0)
    assert fake.frames == [b"\x01"]


async def test_install_command_serializer_wraps_and_is_idempotent(tmp_path):
    bot = _make_bot(tmp_path, min_interval_ms=0)
    bot.meshcore = FakeMeshcore()
    cmds = bot.meshcore.commands

    bot._install_command_serializer()
    wrapped = cmds.send
    assert getattr(wrapped, "_radio_serialized", False)
    assert bot.meshcore.commands is cmds  # wrapped in place

    # Re-installing must not double-wrap (a nested non-reentrant lock would deadlock).
    bot._install_command_serializer()
    assert cmds.send is wrapped
    assert await asyncio.wait_for(cmds.send(b"\x01"), timeout=1.0) == "ok"


def test_install_command_serializer_noop_without_meshcore(tmp_path):
    bot = _make_bot(tmp_path)
    bot.meshcore = None
    bot._install_command_serializer()  # must not raise
    assert bot.meshcore is None
