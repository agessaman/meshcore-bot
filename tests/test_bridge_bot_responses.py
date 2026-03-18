"""Tests for bridge bot-responses: channel_sent_listeners registration and cleanup."""

from configparser import ConfigParser
from unittest.mock import MagicMock, patch

import pytest

from modules.service_plugins.discord_bridge_service import DiscordBridgeService
from modules.service_plugins.telegram_bridge_service import TelegramBridgeService


@pytest.fixture
def bridge_mock_bot(mock_logger):
    """Mock bot for bridge service tests."""
    bot = MagicMock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("DiscordBridge")
    bot.config.set("DiscordBridge", "bridge_bot_responses", "true")
    bot.config.add_section("TelegramBridge")
    bot.config.set("TelegramBridge", "bridge_bot_responses", "true")
    bot.config.set("TelegramBridge", "api_token", "test-token")
    bot.channel_sent_listeners = []
    bot.meshcore = MagicMock()
    return bot


class TestDiscordBridgeBotResponses:
    """Discord bridge: register/unregister channel_sent_listeners and bridge_bot_responses config."""

    @pytest.mark.asyncio
    async def test_start_registers_listener_when_bridge_bot_responses_true(self, bridge_mock_bot, mock_logger):
        bridge_mock_bot.config.set("DiscordBridge", "bridge_bot_responses", "true")
        with patch.object(DiscordBridgeService, "_load_channel_mappings", return_value=None):
            service = DiscordBridgeService(bridge_mock_bot)
        service.channel_webhooks = {"general": "https://discord.com/api/webhooks/123/abc"}
        service.bridge_bot_responses = True
        bridge_mock_bot.channel_sent_listeners = []

        with patch.object(service, "http_session", None), patch(
            "modules.service_plugins.discord_bridge_service.AIOHTTP_AVAILABLE", True
        ):
            await service.start()

        assert service._on_mesh_channel_message in bridge_mock_bot.channel_sent_listeners

    @pytest.mark.asyncio
    async def test_stop_unregisters_listener(self, bridge_mock_bot, mock_logger):
        with patch.object(DiscordBridgeService, "_load_channel_mappings", return_value=None):
            service = DiscordBridgeService(bridge_mock_bot)
        service.channel_webhooks = {"general": "https://discord.com/api/webhooks/123/abc"}
        bridge_mock_bot.channel_sent_listeners = [service._on_mesh_channel_message]

        await service.stop()

        assert service._on_mesh_channel_message not in bridge_mock_bot.channel_sent_listeners

    @pytest.mark.asyncio
    async def test_start_does_not_register_when_bridge_bot_responses_false(self, bridge_mock_bot, mock_logger):
        bridge_mock_bot.config.set("DiscordBridge", "bridge_bot_responses", "false")
        with patch.object(DiscordBridgeService, "_load_channel_mappings", return_value=None):
            service = DiscordBridgeService(bridge_mock_bot)
        service.channel_webhooks = {"general": "https://discord.com/api/webhooks/123/abc"}
        service.bridge_bot_responses = False
        bridge_mock_bot.channel_sent_listeners = []

        with patch.object(service, "http_session", None), patch(
            "modules.service_plugins.discord_bridge_service.AIOHTTP_AVAILABLE", True
        ):
            await service.start()

        assert len(bridge_mock_bot.channel_sent_listeners) == 0


class TestTelegramBridgeBotResponses:
    """Telegram bridge: register/unregister channel_sent_listeners and bridge_bot_responses config."""

    @pytest.mark.asyncio
    async def test_start_registers_listener_when_bridge_bot_responses_true(self, bridge_mock_bot, mock_logger):
        bridge_mock_bot.config.set("TelegramBridge", "bridge_bot_responses", "true")
        with patch.object(TelegramBridgeService, "_load_channel_mappings", return_value=None):
            service = TelegramBridgeService(bridge_mock_bot)
        service.channel_chat_ids = {"general": "@general"}
        service.bridge_bot_responses = True
        bridge_mock_bot.channel_sent_listeners = []
        service.api_token = "test-token"

        with patch.object(service, "http_session", None), patch(
            "modules.service_plugins.telegram_bridge_service.AIOHTTP_AVAILABLE", True
        ):
            await service.start()

        assert service._on_mesh_channel_message in bridge_mock_bot.channel_sent_listeners

    @pytest.mark.asyncio
    async def test_stop_unregisters_listener(self, bridge_mock_bot, mock_logger):
        with patch.object(TelegramBridgeService, "_load_channel_mappings", return_value=None):
            service = TelegramBridgeService(bridge_mock_bot)
        service.channel_chat_ids = {"general": "@general"}
        service.api_token = "test-token"
        bridge_mock_bot.channel_sent_listeners = [service._on_mesh_channel_message]

        await service.stop()

        assert service._on_mesh_channel_message not in bridge_mock_bot.channel_sent_listeners

    @pytest.mark.asyncio
    async def test_start_does_not_register_when_bridge_bot_responses_false(self, bridge_mock_bot, mock_logger):
        bridge_mock_bot.config.set("TelegramBridge", "bridge_bot_responses", "false")
        with patch.object(TelegramBridgeService, "_load_channel_mappings", return_value=None):
            service = TelegramBridgeService(bridge_mock_bot)
        service.channel_chat_ids = {"general": "@general"}
        service.api_token = "test-token"
        service.bridge_bot_responses = False
        bridge_mock_bot.channel_sent_listeners = []

        with patch.object(service, "http_session", None), patch(
            "modules.service_plugins.telegram_bridge_service.AIOHTTP_AVAILABLE", True
        ):
            await service.start()

        assert len(bridge_mock_bot.channel_sent_listeners) == 0
