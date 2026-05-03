"""Tests for modules.bridge_outbound."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules import bridge_outbound


def test_is_valid_discord_webhook_url():
    assert bridge_outbound.is_valid_discord_webhook_url(
        "https://discord.com/api/webhooks/123/abc-token"
    )
    assert not bridge_outbound.is_valid_discord_webhook_url("http://example.com/hook")
    assert not bridge_outbound.is_valid_discord_webhook_url("")


@pytest.mark.asyncio
async def test_post_discord_webhook_async_success():
    mock_resp = AsyncMock()
    mock_resp.status = 204

    mock_session = MagicMock()
    mock_session.post = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    mock_session.post.return_value = cm

    ok = await bridge_outbound.post_discord_webhook(
        "https://discord.com/api/webhooks/1/tok",
        "hello world",
        username="Bot",
        session=mock_session,
        logger=MagicMock(),
    )
    assert ok is True


@pytest.mark.asyncio
async def test_post_telegram_message_async_ok_json():
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"ok": True})

    mock_session = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    mock_session.post.return_value = cm

    ok = await bridge_outbound.post_telegram_message(
        "tok123",
        "-100123",
        "alert text",
        session=mock_session,
        logger=MagicMock(),
    )
    assert ok is True


@pytest.mark.asyncio
async def test_post_discord_invalid_url_returns_false():
    log = MagicMock()
    ok = await bridge_outbound.post_discord_webhook(
        "https://example.com/nope",
        "x",
        logger=log,
    )
    assert ok is False


@pytest.mark.asyncio
@patch.object(bridge_outbound, "AIOHTTP_AVAILABLE", False)
@patch.object(bridge_outbound, "REQUESTS_AVAILABLE", True)
async def test_post_discord_requests_fallback():
    mock_resp = MagicMock()
    mock_resp.status_code = 204

    with patch("modules.bridge_outbound.requests.post", return_value=mock_resp) as p:
        ok = await bridge_outbound.post_discord_webhook(
            "https://discord.com/api/webhooks/9/x",
            "hi",
            logger=MagicMock(),
        )
    assert ok is True
    p.assert_called_once()
