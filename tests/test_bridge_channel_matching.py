"""Tests for bridge channel-name matching and logging of unmapped channels."""

from configparser import ConfigParser
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.service_plugins.discord_bridge_service import DiscordBridgeService
from modules.service_plugins.telegram_bridge_service import TelegramBridgeService

URL_HASH = "https://discord.com/api/webhooks/1/hash"
URL_PLAIN = "https://discord.com/api/webhooks/2/plain"


@pytest.fixture
def bridge_bot(mock_logger):
    bot = MagicMock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("DiscordBridge")
    bot.config.set("DiscordBridge", "enabled", "true")
    bot.config.add_section("TelegramBridge")
    bot.config.set("TelegramBridge", "enabled", "true")
    bot.config.set("TelegramBridge", "api_token", "test-token")
    bot.channel_manager = MagicMock()
    bot.channel_sent_listeners = []
    bot.meshcore = MagicMock()
    return bot


@pytest.mark.parametrize("key", ["bridge.#test", "bridge.test", "bridge.Test", "bridge.#TEST"])
def test_discord_hashtag_channel_matches_with_or_without_hash(bridge_bot, key):
    bridge_bot.config.set("DiscordBridge", key, URL_HASH)
    service = DiscordBridgeService(bridge_bot)
    assert service._targets_for("#test") == [URL_HASH]


def test_discord_mapping_with_hash_matches_plain_channel(bridge_bot):
    bridge_bot.config.set("DiscordBridge", "bridge.#public", URL_HASH)
    service = DiscordBridgeService(bridge_bot)
    assert service._targets_for("Public") == [URL_HASH]


def test_exact_match_wins_over_hash_insensitive_match(bridge_bot):
    """Separate mappings for Public and #public each keep their own target."""
    bridge_bot.config.set("DiscordBridge", "bridge.public", URL_PLAIN)
    bridge_bot.config.set("DiscordBridge", "bridge.#public", URL_HASH)
    service = DiscordBridgeService(bridge_bot)
    assert service._targets_for("Public") == [URL_PLAIN]
    assert service._targets_for("#public") == [URL_HASH]


def test_unrelated_channel_has_no_target(bridge_bot):
    bridge_bot.config.set("DiscordBridge", "bridge.#test", URL_HASH)
    service = DiscordBridgeService(bridge_bot)
    assert service._targets_for("#other") is None


def test_telegram_matching_ignores_hash_and_case(bridge_bot):
    bridge_bot.config.set("TelegramBridge", "bridge.HowlTest", "@howl")
    service = TelegramBridgeService(bridge_bot)
    assert service._targets_for("#howltest") == "@howl"


@pytest.mark.asyncio
async def test_unmapped_channel_logged_at_info_once(bridge_bot, mock_logger):
    bridge_bot.config.set("DiscordBridge", "bridge.Bridge.#test", URL_HASH)
    bridge_bot.channel_manager.get_channel_name.return_value = "#test"
    service = DiscordBridgeService(bridge_bot)
    mock_logger.info.reset_mock()

    event = MagicMock()
    event.payload = {"channel_idx": 1, "text": "Alice: hi"}
    with patch.object(service, "_queue_message", new_callable=AsyncMock) as mock_queue:
        await service._on_mesh_channel_message(event)
        await service._on_mesh_channel_message(event)

    mock_queue.assert_not_awaited()
    unmapped = [c for c in mock_logger.info.call_args_list if "not bridging channel '#test'" in c.args[0]]
    assert len(unmapped) == 1
    assert "Bridge.#test" in unmapped[0].args[0]
