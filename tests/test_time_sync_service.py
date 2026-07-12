"""Tests for the authenticated time-sync source service."""

from __future__ import annotations

import configparser
from unittest.mock import AsyncMock, Mock

import pytest

from modules.service_plugins.time_sync_service import TimeSyncService


BOT_PUBLIC_KEY = "22" * 32


class _ChannelManager:
    def __init__(self, channel_exists: bool = True):
        self.channel_exists = channel_exists

    def get_channel_number(self, channel: str):
        return 1 if self.channel_exists and channel == "#time" else None

    def get_channel_by_name(self, channel: str):
        if self.channel_exists and channel == "#time":
            return {
                "channel_name": "#time",
                "channel_key_hex": "5d13043d9a5e61bc61aeb63208f5c64e",
            }
        return None


def _make_bot(
    *,
    enabled: bool = True,
    channel: str = "#time",
    display_name: str = "TimeBot",
):
    config = configparser.ConfigParser()
    config.add_section("Time_Sync")
    config.set("Time_Sync", "enabled", "true" if enabled else "false")
    config.set("Time_Sync", "channel", channel)
    config.set("Time_Sync", "display_name", display_name)
    config.set("Time_Sync", "sequence", "12345")

    bot = Mock()
    bot.config = config
    bot.logger = Mock()
    bot.meshcore = Mock()
    bot.meshcore.self_info = {"public_key": BOT_PUBLIC_KEY}
    bot.meshcore.commands = Mock()
    bot.meshcore.commands.sign = AsyncMock(return_value=Mock(payload={"signature": b"\x33" * 64}))
    bot.channel_manager = _ChannelManager(channel_exists=bool(channel))
    bot.db_manager = Mock()
    bot.db_manager.get_metadata = Mock(return_value=None)
    bot.db_manager.set_metadata = Mock()
    bot.command_manager = Mock()
    bot.command_manager.send_group_datagram = AsyncMock(return_value=True)
    return bot


@pytest.mark.asyncio
async def test_disabled_source_sends_nothing():
    bot = _make_bot(enabled=False)
    service = TimeSyncService(bot)

    await service.start()

    assert service._running is False
    bot.command_manager.send_group_datagram.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channel", ""),
        ("channel", "#missing"),
        ("display_name", ""),
        ("display_name", "Time\nBot"),
        ("display_name", "a" * 21),
    ],
)
def test_enabling_fails_with_incomplete_or_invalid_config(field, value):
    kwargs = {field: value}
    bot = _make_bot(**kwargs)
    service = TimeSyncService(bot)

    with pytest.raises(Exception):
        service._load_settings()


@pytest.mark.asyncio
async def test_send_once_uses_group_datagram_and_advances_sequence():
    bot = _make_bot()
    service = TimeSyncService(bot)
    service._settings = service._load_settings()
    service._sequence = 65535

    sent = await service.send_once(timestamp=1783862400)

    assert sent is True
    assert service._sequence == 0
    bot.command_manager.send_group_datagram.assert_awaited_once()
    args = bot.command_manager.send_group_datagram.await_args.args
    assert args[0] == "#time"
    assert args[1] == 0x0121
    assert args[2].startswith(b"Tv1")


def test_default_interval_is_one_week():
    bot = _make_bot()
    service = TimeSyncService(bot)

    settings = service._load_settings()

    assert settings.interval_seconds == 604800
