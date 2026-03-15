"""Tests for local check-in service plugin.

Skipped when the checkin_service local plugin is not present (it does not ship with the bot).
"""

import configparser

# Import from local plugin (repo root is on path when running tests)
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

try:
    from local.service_plugins.checkin_service import CheckInService
except ImportError:
    CheckInService = None
    pytestmark = pytest.mark.skip(reason="local checkin_service plugin not installed")


def _make_bot(config_overrides=None):
    """Build a mock bot with [CheckIn] and channel_manager."""
    bot = MagicMock()
    bot.logger = Mock()
    bot.logger.info = Mock()
    bot.logger.warning = Mock()
    bot.logger.error = Mock()
    bot.logger.debug = Mock()
    bot.config = configparser.ConfigParser()
    bot.config.add_section("Connection")
    bot.config.add_section("Bot")
    bot.config.add_section("Channels")
    bot.config.add_section("CheckIn")
    bot.config.set("CheckIn", "enabled", "true")
    bot.config.set("CheckIn", "channel", "#meshmonday")
    bot.config.set("CheckIn", "check_in_days", "daily")
    bot.config.set("CheckIn", "require_phrase", "check in")
    bot.config.set("CheckIn", "any_message_counts", "false")
    bot.config.set("CheckIn", "flush_time", "23:59")
    bot.config.set("CheckIn", "api_url", "")
    bot.config.set("CheckIn", "api_key", "")
    if config_overrides:
        for k, v in config_overrides.items():
            bot.config.set("CheckIn", k, str(v))
    channel_manager = MagicMock()
    channel_manager.get_channel_name = Mock(return_value="#meshmonday")
    bot.channel_manager = channel_manager
    return bot


def _make_event(channel_idx=0, text="HOWL: check in", raw_hex=None):
    event = MagicMock()
    event.payload = {
        "channel_idx": channel_idx,
        "text": text,
    }
    if raw_hex is not None:
        event.payload["raw_hex"] = raw_hex
    return event


@pytest.mark.asyncio
async def test_message_wrong_channel_not_stored():
    """Message in a different channel is not stored."""
    bot = _make_bot()
    bot.channel_manager.get_channel_name.return_value = "#other"
    service = CheckInService(bot)
    with patch("local.service_plugins.checkin_service.get_config_timezone") as gtz:
        gtz.return_value = (MagicMock(), "America/Los_Angeles")
        await service._on_channel_message(_make_event(), None)
    assert not service._buckets


@pytest.mark.asyncio
async def test_message_correct_channel_with_phrase_stored():
    """Message in configured channel containing the phrase is stored."""
    bot = _make_bot()
    service = CheckInService(bot)
    with patch("local.service_plugins.checkin_service.get_config_timezone") as gtz:
        tz = MagicMock()
        gtz.return_value = (tz, "America/Los_Angeles")
        with patch("local.service_plugins.checkin_service.datetime") as dt:
            dt.now.return_value = datetime(2025, 3, 3, 12, 0, 0)  # Monday
            await service._on_channel_message(_make_event(text="HOWL: check in"), None)
    assert len(service._buckets) == 1
    date_str = "2025-03-03"
    assert date_str in service._buckets
    records = service._buckets[date_str]
    assert len(records) == 1
    rec = next(iter(records.values()))
    assert rec["username"] == "HOWL"
    assert rec["message"] == "check in"
    assert "packet_hash" in rec


@pytest.mark.asyncio
async def test_message_without_phrase_when_required_not_stored():
    """When require_phrase is set and any_message_counts is false, message without phrase is not stored."""
    bot = _make_bot({"require_phrase": "check in", "any_message_counts": "false"})
    service = CheckInService(bot)
    with patch("local.service_plugins.checkin_service.get_config_timezone") as gtz:
        gtz.return_value = (MagicMock(), "America/Los_Angeles")
        with patch("local.service_plugins.checkin_service.datetime") as dt:
            dt.now.return_value = datetime(2025, 3, 3, 12, 0, 0)
            await service._on_channel_message(_make_event(text="HOWL: hello world"), None)
    assert not service._buckets


@pytest.mark.asyncio
async def test_any_message_counts_stored():
    """When any_message_counts is true, any message in the channel is stored."""
    bot = _make_bot({"any_message_counts": "true", "require_phrase": ""})
    service = CheckInService(bot)
    with patch("local.service_plugins.checkin_service.get_config_timezone") as gtz:
        gtz.return_value = (MagicMock(), "America/Los_Angeles")
        with patch("local.service_plugins.checkin_service.datetime") as dt:
            dt.now.return_value = datetime(2025, 3, 4, 14, 0, 0)
            await service._on_channel_message(_make_event(text="ALICE: random message"), None)
    assert len(service._buckets) == 1
    assert "2025-03-04" in service._buckets
    records = service._buckets["2025-03-04"]
    assert len(records) == 1
    rec = next(iter(records.values()))
    assert rec["username"] == "ALICE"
    assert rec["message"] == "random message"


@pytest.mark.asyncio
async def test_monday_only_skips_tuesday():
    """When check_in_days is monday, message on Tuesday is not stored."""
    bot = _make_bot({"check_in_days": "monday"})
    service = CheckInService(bot)
    with patch("local.service_plugins.checkin_service.get_config_timezone") as gtz:
        gtz.return_value = (MagicMock(), "America/Los_Angeles")
        with patch("local.service_plugins.checkin_service.datetime") as dt:
            # Tuesday 2025-03-04
            dt.now.return_value = datetime(2025, 3, 4, 12, 0, 0)
            await service._on_channel_message(_make_event(text="HOWL: check in"), None)
    assert not service._buckets
