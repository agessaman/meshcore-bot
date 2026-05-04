#!/usr/bin/env python3
"""
Minimal Discord webhook and Telegram Bot API posting for service plugins.

Independent of DiscordBridgeService / TelegramBridgeService queues and lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    import requests

    REQUESTS_AVAILABLE = True
except ImportError:
    requests = None  # type: ignore[assignment]
    REQUESTS_AVAILABLE = False

DISCORD_WEBHOOK_PREFIX = "https://discord.com/api/webhooks/"
DISCORD_CONTENT_MAX = 2000
TELEGRAM_TEXT_MAX = 4096
TELEGRAM_TRUNCATE_AT = 4000
HTTP_TIMEOUT_SECONDS = 10.0


def is_valid_discord_webhook_url(url: str) -> bool:
    u = (url or "").strip()
    return bool(u.startswith(DISCORD_WEBHOOK_PREFIX))


def _truncate_discord_content(content: str) -> str:
    if len(content) <= DISCORD_CONTENT_MAX:
        return content
    return content[: DISCORD_CONTENT_MAX - 1].rstrip() + "…"


def _truncate_telegram_text(text: str) -> str:
    if len(text) <= TELEGRAM_TRUNCATE_AT:
        return text
    return text[: TELEGRAM_TRUNCATE_AT - 1].rstrip() + "…"


async def post_discord_webhook(
    url: str,
    content: str,
    *,
    username: str = "MeshCore",
    session: Any = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """POST plain text to a Discord webhook. Returns True on HTTP 204/200."""
    log = logger or logging.getLogger(__name__)
    if not is_valid_discord_webhook_url(url):
        log.warning("Invalid Discord webhook URL (skipped)")
        return False

    payload = {
        "content": _truncate_discord_content(content),
        "username": username[:80] if username else "MeshCore",
    }

    if AIOHTTP_AVAILABLE:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        try:
            if session is not None:
                async with session.post(url, json=payload, timeout=timeout) as response:
                    return await _discord_response_ok(response, log)
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload, timeout=timeout) as response:
                    return await _discord_response_ok(response, log)
        except asyncio.TimeoutError:
            log.error("Timeout posting to Discord webhook")
            return False
        except Exception as e:
            log.error("Error posting to Discord webhook: %s", e, exc_info=True)
            return False

    if REQUESTS_AVAILABLE:
        loop = asyncio.get_event_loop()

        def _sync_post() -> bool:
            try:
                r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT_SECONDS)
                if r.status_code in (200, 204):
                    return True
                log.warning(
                    "Discord webhook returned %s: %s",
                    r.status_code,
                    (r.text or "")[:200],
                )
                return False
            except Exception as ex:
                log.error("requests Discord webhook error: %s", ex, exc_info=True)
                return False

        return await loop.run_in_executor(None, _sync_post)

    log.error("No aiohttp or requests; cannot post to Discord webhook")
    return False


async def _discord_response_ok(response: Any, log: logging.Logger) -> bool:
    if response.status in (200, 204):
        return True
    body = await response.text()
    log.warning("Discord webhook returned %s: %s", response.status, body[:200])
    return False


async def post_telegram_message(
    api_token: str,
    chat_id: str,
    text: str,
    *,
    session: Any = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """POST sendMessage (plain text, no parse_mode)."""
    log = logger or logging.getLogger(__name__)
    token = (api_token or "").strip()
    cid = (chat_id or "").strip()
    if not token or not cid:
        log.warning("Telegram post skipped: missing token or chat_id")
        return False

    safe_text = _truncate_telegram_text(text)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, Any] = {"chat_id": cid, "text": safe_text}

    if AIOHTTP_AVAILABLE:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        try:
            if session is not None:
                async with session.post(url, json=payload, timeout=timeout) as response:
                    return await _telegram_response_ok(response, log)
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload, timeout=timeout) as response:
                    return await _telegram_response_ok(response, log)
        except asyncio.TimeoutError:
            log.error("Timeout posting to Telegram")
            return False
        except Exception as e:
            log.error("Error posting to Telegram: %s", e, exc_info=True)
            return False

    if REQUESTS_AVAILABLE:
        loop = asyncio.get_event_loop()

        def _sync_post() -> bool:
            try:
                r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT_SECONDS)
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except Exception:
                        data = {}
                    if data.get("ok"):
                        return True
                    log.warning("Telegram API not ok: %s", data.get("description", r.text[:200]))
                    return False
                log.warning("Telegram returned %s: %s", r.status_code, (r.text or "")[:200])
                return False
            except Exception as ex:
                log.error("requests Telegram error: %s", ex, exc_info=True)
                return False

        return await loop.run_in_executor(None, _sync_post)

    log.error("No aiohttp or requests; cannot post to Telegram")
    return False


async def _telegram_response_ok(response: Any, log: logging.Logger) -> bool:
    try:
        data = await response.json() if response.content else {}
    except Exception:
        data = {}
    if response.status == 200 and data.get("ok"):
        return True
    log.warning(
        "Telegram API returned %s: %s",
        response.status,
        (data.get("description") or "")[:200],
    )
    return False
