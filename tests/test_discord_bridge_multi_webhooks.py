"""Tests for DiscordBridgeService multi-webhook channel fan-out."""

import asyncio
from configparser import ConfigParser
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from modules.service_plugins.discord_bridge_service import DiscordBridgeService


@pytest.fixture
def multi_webhook_bot(mock_logger):
    """Mock bot for DiscordBridgeService multi-webhook tests."""
    bot = MagicMock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("DiscordBridge")
    bot.channel_manager = MagicMock()
    bot.channel_manager.get_channel_name.return_value = "Public"
    bot.channel_sent_listeners = []
    bot.meshcore = MagicMock()
    return bot


@pytest.mark.asyncio
async def test_single_webhook_backward_compatible(multi_webhook_bot):
    """Single webhook value still behaves as before."""
    url = "https://discord.com/api/webhooks/123/abc"
    multi_webhook_bot.config.set("DiscordBridge", "enabled", "true")
    multi_webhook_bot.config.set("DiscordBridge", "bridge.Public", url)

    service = DiscordBridgeService(multi_webhook_bot)

    assert service.channel_webhooks == {"Public": [url]}


@pytest.mark.asyncio
async def test_multiple_webhooks_parsed_and_queued(multi_webhook_bot):
    """Comma-separated webhooks for a channel are all used."""
    url1 = "https://discord.com/api/webhooks/123/abc"
    url2 = "https://discord.com/api/webhooks/456/def"
    multi_webhook_bot.config.set("DiscordBridge", "enabled", "true")
    multi_webhook_bot.config.set(
        "DiscordBridge",
        "bridge.Public",
        f"{url1}, {url2}",
    )

    service = DiscordBridgeService(multi_webhook_bot)

    # Both URLs should be registered for the same channel
    assert service.channel_webhooks == {"Public": [url1, url2]}

    # Patch queue_message to observe fan-out behaviour
    with patch.object(service, "_queue_message", new_callable=AsyncMock) as mock_queue:
        event = MagicMock()
        event.payload = {"channel_idx": 0, "text": "Alice: hello world"}
        await service._on_mesh_channel_message(event)

        # One call per webhook URL
        assert mock_queue.await_count == 2
        called_urls = {call.args[0] for call in mock_queue.await_args_list}
        assert called_urls == {url1, url2}
