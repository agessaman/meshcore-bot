#!/usr/bin/env python3
"""
Telegram Bridge Service for MeshCore Bot
Posts MeshCore channel messages to Telegram via the Bot API (one-way, read-only)
"""

import asyncio
import html
import os
import re
import time
from collections import deque
from dataclasses import dataclass
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


from ..profanity_filter import censor, contains_profanity  # noqa: F401  re-exported
from .base_service import BaseServicePlugin  # noqa: F401  re-exported
from .channel_bridge_utils import ChannelBridgeBase

# Telegram API
TELEGRAM_API_BASE = "https://api.telegram.org/bot"
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_TRUNCATE_AT = 4000


@dataclass
class QueuedMessage:
    """Represents a message queued for Telegram posting."""
    chat_id: str
    payload: dict[str, Any]
    channel_name: str
    retry_count: int = 0
    first_queued: float = 0.0
    next_retry_at: float = 0.0

    def __post_init__(self):
        if self.first_queued == 0.0:
            self.first_queued = time.time()
        if self.next_retry_at == 0.0:
            self.next_retry_at = time.time()


class TelegramBridgeService(ChannelBridgeBase):
    """Telegram bridge service.

    Posts MeshCore channel messages to Telegram channels/groups via the Bot API.
    One-way bridge - messages only flow from MeshCore to Telegram.
    Direct messages are NEVER bridged for privacy.
    """

    config_section = 'TelegramBridge'
    description = "Posts MeshCore channel messages to Telegram (one-way, read-only)"

    # Web-viewer settings schema (see modules/settings_schema.py)
    settings_schema = [
        {"key": "api_token", "label": "Bot API token", "type": "str", "default": "",
         "help": "Token from @BotFather. Can also be set via the TELEGRAM_BOT_TOKEN env var."},
        {"key": "parse_mode", "label": "Parse mode", "type": "enum",
         "options": [{"value": "HTML", "label": "HTML"},
                     {"value": "Markdown", "label": "Markdown"},
                     {"value": "MarkdownV2", "label": "MarkdownV2"}],
         "default": "HTML", "help": "Message formatting mode."},
        {"key": "disable_web_page_preview", "label": "Disable link previews", "type": "bool",
         "default": False, "help": "Disable link previews in bridged messages."},
        {"key": "max_message_length", "label": "Max message length", "type": "int",
         "min": 1, "max": 4096, "default": 4096, "help": "Telegram's hard limit is 4096."},
        {"key": "filter_profanity", "label": "Profanity filter", "type": "enum",
         "options": [{"value": "drop", "label": "Drop (don't bridge)"},
                     {"value": "censor", "label": "Censor (****)"},
                     {"value": "off", "label": "Off"}],
         "default": "drop", "help": "How to handle profanity in messages and usernames."},
        {"key": "bridge_bot_responses", "label": "Bridge bot responses", "type": "bool",
         "default": True, "help": "Also bridge the bot's own command replies."},
    ]
    settings_dynamic_sections = [
        {"section": "TelegramBridge", "key_prefix": "bridge.",
         "label": "Channel mappings", "key_label": "MeshCore channel", "value_label": "Telegram chat ID",
         "help": "Map a MeshCore channel to a Telegram chat. Use @channelusername (public) or a "
                 "numeric -100... ID (private). DMs are never bridged.",
         "key_placeholder": "Public", "value_placeholder": "@YourChannel or -1001234567890"},
    ]

    def __init__(self, bot: Any):
        super().__init__(bot)

        # State stop() relies on, set before any early return so a bridge left
        # disabled by missing config still stops cleanly at shutdown.
        self.channel_chat_ids: dict[str, str] = {}
        self.message_queues: dict[str, list[QueuedMessage]] = {}
        self.send_times: dict[str, deque] = {}
        self.http_session: Optional[aiohttp.ClientSession] = None
        self._queue_processor_task: Optional[asyncio.Task] = None

        if not AIOHTTP_AVAILABLE and not REQUESTS_AVAILABLE:
            self.logger.error(
                "Neither aiohttp nor requests available. Telegram bridge requires one of these."
            )
            self.enabled = False
            return

        # API token: config or env (env takes precedence for security)
        self.api_token = (
            os.environ.get('TELEGRAM_BOT_TOKEN') or
            self.bot.config.get('TelegramBridge', 'api_token', fallback='').strip()
        )
        if not self.api_token:
            self.logger.error("Telegram bridge: api_token not set. Set in config or TELEGRAM_BOT_TOKEN env.")
            self.enabled = False
            return

        self._load_channel_mappings()

        # Optional settings
        self.parse_mode = self.bot.config.get('TelegramBridge', 'parse_mode', fallback='HTML')
        if self.parse_mode and self.parse_mode.upper() not in ('HTML', 'MARKDOWN', 'MARKDOWNV2'):
            self.parse_mode = 'HTML'
        self.disable_web_page_preview = self.bot.config.getboolean(
            'TelegramBridge', 'disable_web_page_preview', fallback=False
        )
        self.max_message_length = self.bot.config.getint(
            'TelegramBridge', 'max_message_length', fallback=TELEGRAM_MAX_MESSAGE_LENGTH
        )
        self.max_message_length = min(self.max_message_length, TELEGRAM_MAX_MESSAGE_LENGTH)

        # Profanity filter: drop (default), censor, or off
        raw_filter = self.bot.config.get('TelegramBridge', 'filter_profanity', fallback='drop').strip().lower()
        if raw_filter not in ('drop', 'censor', 'off'):
            raw_filter = 'drop'
        self.filter_profanity = raw_filter

        # Bridge bot's own channel responses to Telegram (default: true)
        self.bridge_bot_responses = self.bot.config.getboolean(
            'TelegramBridge', 'bridge_bot_responses', fallback=True
        )

        # Rate limiting: ~1 message per second per chat
        self.rate_limit_min_interval = 1.0
        self.max_retries = 5
        self.retry_delay_base = 1.0
        self.max_queue_age = 300

        if not self.channel_chat_ids:
            self.logger.warning(
                "No Telegram channel mappings configured. "
                "Add bridge.<channelname> = <chat_id> in [TelegramBridge]"
            )

    bridge_label = "Telegram"
    payload_text_key = "text"
    old_message_log = "Dropping old message from queue [{channel}]: age {age:.1f}s > {max_age}s"
    retry_log = "Message failed, retry in {delay:.1f}s ({retry}/{max_retries}) [{channel}]"
    processor_error_log = "Error in Telegram queue processor: {error}"

    @property
    def _bridge_mappings(self) -> dict[str, str]:
        return self.channel_chat_ids

    def _open_http_session(self) -> None:
        if AIOHTTP_AVAILABLE:
            self.http_session = aiohttp.ClientSession()
        else:
            self.logger.debug("Using requests for HTTP (fallback)")

    def _init_queues(self) -> None:
        for chat_id in self.channel_chat_ids.values():
            self.message_queues[chat_id] = []
            self.send_times[chat_id] = deque()

    def _targets_for(self, channel_name: str) -> Optional[str]:
        # Normalize: strip leading # and compare case-insensitively so bridge.HowlTest matches #howltest
        channel_key = channel_name.lstrip('#').lower()
        for config_channel, cid in self.channel_chat_ids.items():
            if config_channel.lstrip('#').lower() == channel_key:
                return cid
        return None

    async def _deliver(self, targets: str, sender_name: str, message_text: str, channel_name: str) -> None:
        full_text = self._build_message_text(sender_name, message_text, channel_name)
        full_text = self._truncate_text(full_text)
        await self._queue_message(targets, full_text, channel_name)

    def _throttled(self, key: str, queue: Any, current_time: float) -> bool:
        # Enforce min interval per chat
        if key in self.send_times:
            st = self.send_times[key]
            while st and (current_time - st[0]) > self.rate_limit_min_interval:
                st.popleft()
            if st and (current_time - st[-1]) < self.rate_limit_min_interval:
                return True
        return False

    async def _send_queued(self, queued_msg: QueuedMessage) -> bool:
        return await self._send_to_telegram(
            queued_msg.chat_id,
            queued_msg.payload,
            queued_msg.channel_name,
            queued_msg,
        )

    def _load_channel_mappings(self) -> None:
        """Load bridge.<channel> = chat_id from config."""
        if not self.bot.config.has_section('TelegramBridge'):
            return
        for key, value in self.bot.config.items('TelegramBridge'):
            if key.startswith('bridge.'):
                channel_name = key[7:].strip()
                chat_id = value.strip()
                if not chat_id:
                    continue
                self.channel_chat_ids[channel_name] = chat_id
                # Log chat_id safely (mask numeric IDs partially)
                if chat_id.startswith('@'):
                    safe_id = chat_id
                else:
                    safe_id = chat_id[:4] + '...' + chat_id[-4:] if len(chat_id) > 10 else '***'
                self.logger.info(f"Configured Telegram bridge: {channel_name} → {safe_id}")
        self.logger.info(f"Loaded {len(self.channel_chat_ids)} Telegram channel mapping(s)")

    def _mask_token(self, token: str) -> str:
        if not token or len(token) < 8:
            return "***"
        return token[:4] + "..." + token[-4:]

    def _format_mentions_html(self, text: str) -> str:
        """Convert @[username] to <code>@username</code> for Telegram HTML."""
        pattern = r'@\[([^\]]+)\]'
        return re.sub(pattern, r'<code>@\1</code>', text)

    def _escape_html(self, s: str) -> str:
        return html.escape(s, quote=True)

    def _build_message_text(
        self,
        sender_name: str,
        message_text: str,
        channel_name: str,
        use_channel_tag: bool = True,
    ) -> str:
        """Build HTML message: [Channel] **Sender**: text (with escaping)."""
        safe_sender = self._escape_html(sender_name)
        formatted_body = self._format_mentions_html(message_text)
        # Escape HTML in the rest of the body (mentions already wrapped in <code>)
        parts = re.split(r'(<code>[^<]*</code>)', formatted_body)
        escaped_parts = [
            self._escape_html(p) if not p.startswith('<code>') else p
            for p in parts
        ]
        safe_body = ''.join(escaped_parts)

        prefix = f"<i>[{self._escape_html(channel_name)}]</i> " if use_channel_tag else ""
        return f"{prefix}<b>{safe_sender}</b>: {safe_body}"

    def _truncate_text(self, text: str) -> str:
        if len(text) <= self.max_message_length:
            return text
        self.logger.debug(f"Truncating message from {len(text)} to {self.max_message_length} chars")
        return text[: self.max_message_length - 1].rstrip() + "…"

    async def _queue_message(self, chat_id: str, text: str, channel_name: str) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if self.parse_mode:
            payload["parse_mode"] = self.parse_mode
        if self.disable_web_page_preview:
            payload["disable_web_page_preview"] = True

        queued = QueuedMessage(
            chat_id=chat_id,
            payload=payload,
            channel_name=channel_name,
        )
        if chat_id not in self.message_queues:
            self.message_queues[chat_id] = []
        self.message_queues[chat_id].append(queued)
        self.logger.debug(f"Queued message for Telegram [{channel_name}]: {text[:50]}...")

    async def _send_to_telegram(
        self,
        chat_id: str,
        payload: dict[str, Any],
        channel_name: str,
        queued_msg: Optional[QueuedMessage] = None,
    ) -> bool:
        url = f"{TELEGRAM_API_BASE}{self.api_token}/sendMessage"
        if AIOHTTP_AVAILABLE and self.http_session:
            return await self._send_async(url, payload, channel_name, queued_msg)
        elif REQUESTS_AVAILABLE:
            return await self._send_sync(url, payload, channel_name, queued_msg)
        self.logger.error("No HTTP library available for Telegram")
        return False

    async def _send_async(
        self,
        url: str,
        payload: dict[str, Any],
        channel_name: str,
        queued_msg: Optional[QueuedMessage] = None,
    ) -> bool:
        try:
            assert self.http_session is not None
            async with self.http_session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                data = await response.json() if response.content else {}
                if response.status == 200 and data.get('ok'):
                    self.logger.debug(f"Posted to Telegram [{channel_name}]: {payload['text'][:50]}...")
                    return True
                if response.status == 429:
                    retry_after = (data.get('parameters') or {}).get('retry_after', 60)
                    self.logger.warning(
                        f"Telegram rate limit for [{channel_name}]. Retry after: {retry_after}s"
                    )
                    if queued_msg:
                        queued_msg.next_retry_at = time.time() + retry_after
                        queued_msg.retry_count = max(0, queued_msg.retry_count - 1)
                    return False
                self.logger.warning(
                    f"Telegram API returned {response.status} for [{channel_name}]: {data.get('description', '')}"
                )
                return False
        except asyncio.TimeoutError:
            self.logger.error(f"Timeout posting to Telegram [{channel_name}]")
            return False
        except Exception as e:
            self.logger.error(f"Error posting to Telegram [{channel_name}]: {e}")
            return False

    async def _send_sync(
        self,
        url: str,
        payload: dict[str, Any],
        channel_name: str,
        queued_msg: Optional[QueuedMessage] = None,
    ) -> bool:
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.post(url, json=payload, timeout=10),
            )
            data = response.json() if response.content else {}
            if response.status_code == 200 and data.get('ok'):
                self.logger.debug(f"Posted to Telegram [{channel_name}]: {payload['text'][:50]}...")
                return True
            if response.status_code == 429:
                retry_after = (data.get('parameters') or {}).get('retry_after', 60)
                self.logger.warning(
                    f"Telegram rate limit for [{channel_name}]. Retry after: {retry_after}s"
                )
                if queued_msg:
                    queued_msg.next_retry_at = time.time() + retry_after
                    queued_msg.retry_count = max(0, queued_msg.retry_count - 1)
                return False
            self.logger.warning(
                f"Telegram API returned {response.status_code} for [{channel_name}]: "
                f"{data.get('description', '')}"
            )
            return False
        except Exception as e:
            self.logger.error(f"Error posting to Telegram [{channel_name}]: {e}")
            return False
