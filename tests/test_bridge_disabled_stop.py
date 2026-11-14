"""A bridge that disabled itself in __init__ still starts and stops without errors."""

from configparser import ConfigParser
from unittest.mock import MagicMock, patch

import pytest

from modules.service_plugins.discord_bridge_service import DiscordBridgeService
from modules.service_plugins.telegram_bridge_service import TelegramBridgeService


@pytest.fixture
def bot(mock_logger):
    bot = MagicMock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("DiscordBridge")
    bot.config.add_section("TelegramBridge")
    bot.channel_sent_listeners = []
    return bot


async def _start_and_stop(service, bot):
    await service.start()
    await service.stop()
    bot.logger.error.assert_called_once()  # only the reason it disabled itself
    assert bot.channel_sent_listeners == []


@pytest.mark.asyncio
class TestDisabledBridgesStop:
    async def test_telegram_without_api_token(self, bot, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        service = TelegramBridgeService(bot)
        assert service.enabled is False
        await _start_and_stop(service, bot)
        bot.logger.info.assert_any_call("Telegram bridge service stopped")

    async def test_telegram_without_http_library(self, bot):
        with patch("modules.service_plugins.telegram_bridge_service.AIOHTTP_AVAILABLE", False), \
                patch("modules.service_plugins.telegram_bridge_service.REQUESTS_AVAILABLE", False):
            service = TelegramBridgeService(bot)
        assert service.enabled is False
        await _start_and_stop(service, bot)
        bot.logger.info.assert_any_call("Telegram bridge service stopped")

    async def test_discord_without_http_library(self, bot):
        with patch("modules.service_plugins.discord_bridge_service.AIOHTTP_AVAILABLE", False), \
                patch("modules.service_plugins.discord_bridge_service.REQUESTS_AVAILABLE", False):
            service = DiscordBridgeService(bot)
        assert service.enabled is False
        await _start_and_stop(service, bot)
        bot.logger.info.assert_any_call("Discord bridge service stopped")
