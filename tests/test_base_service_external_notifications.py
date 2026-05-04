"""Tests for BaseServicePlugin outbound helpers."""

import configparser
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.service_plugins.base_service import BaseServicePlugin


class _StubPlugin(BaseServicePlugin):
    config_section = "StubExternal_Service"

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False


def _bot_with_section(**section_kv):
    bot = MagicMock()
    bot.logger = MagicMock()
    bot.logger.warning = MagicMock()
    cfg = configparser.ConfigParser()
    cfg.add_section("StubExternal_Service")
    for k, v in section_kv.items():
        cfg.set("StubExternal_Service", k, str(v))
    bot.config = cfg
    return bot


def test_parse_external_notify_filters_invalid_discord_urls():
    bot = _bot_with_section(
        discord_webhook_urls="https://discord.com/api/webhooks/1/abc, https://evil.com/x",
        telegram_chat_ids="",
    )
    svc = _StubPlugin(bot)
    s = svc._parse_external_notify_settings()
    assert len(s.discord_urls) == 1
    assert s.discord_urls[0].startswith("https://discord.com/api/webhooks/1/")


def test_has_external_notification_targets_discord_only():
    bot = _bot_with_section(
        discord_webhook_urls="https://discord.com/api/webhooks/1/abc",
    )
    svc = _StubPlugin(bot)
    assert svc.has_external_notification_targets() is True


def test_has_external_notification_targets_telegram_requires_token():
    bot = _bot_with_section(
        telegram_chat_ids="-1001",
    )
    svc = _StubPlugin(bot)
    assert svc.has_external_notification_targets() is False

    bot2 = _bot_with_section(
        telegram_chat_ids="-1001",
        telegram_bot_token="secret",
    )
    svc2 = _StubPlugin(bot2)
    assert svc2.has_external_notification_targets() is True


@pytest.mark.asyncio
async def test_send_external_notifications_noop_when_empty():
    bot = _bot_with_section()
    svc = _StubPlugin(bot)
    await svc.send_external_notifications("hello")
    # no crash


@pytest.mark.asyncio
async def test_send_external_notifications_calls_discord_and_telegram():
    bot = _bot_with_section(
        discord_webhook_urls="https://discord.com/api/webhooks/1/tok",
        telegram_chat_ids="-99",
        telegram_bot_token="secret",
    )
    svc = _StubPlugin(bot)

    with (
        patch("modules.bridge_outbound.post_discord_webhook", new_callable=AsyncMock, return_value=True) as pd,
        patch("modules.bridge_outbound.post_telegram_message", new_callable=AsyncMock, return_value=True) as pt,
        patch("modules.bridge_outbound.AIOHTTP_AVAILABLE", True),
    ):
        await svc.send_external_notifications("ping")

    assert pd.await_count == 1
    assert pt.await_count == 1
