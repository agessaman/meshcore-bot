#!/usr/bin/env python3
"""
Telegram Bridge Service for MeshCore Bot
Posts MeshCore channel messages to Telegram via the Bot API (one-way, read-only)
"""

import asyncio
import copy
import html
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from meshcore import EventType

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

import contextlib

from ..profanity_filter import censor, contains_profanity
from .base_service import BaseServicePlugin

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


class TelegramBridgeService(BaseServicePlugin):
    """Telegram bridge service.

    Posts MeshCore channel messages to Telegram channels/groups via the Bot API.
    One-way bridge - messages only flow from MeshCore to Telegram.
    Direct messages are NEVER bridged for privacy.
    """

    config_section = 'TelegramBridge'
    description = "Posts MeshCore channel messages to Telegram (one-way, read-only)"

    def __init__(self, bot: Any):
        super().__init__(bot)

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

        self.channel_chat_ids: dict[str, str] = {}
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
        self.message_queues: dict[str, list[QueuedMessage]] = {}
        self.send_times: dict[str, deque] = {}
        self.rate_limit_min_interval = 1.0
        self.max_retries = 5
        self.retry_delay_base = 1.0
        self.max_queue_age = 300

        self.http_session: aiohttp.ClientSession | None = None
        self._queue_processor_task: asyncio.Task | None = None

        if not self.channel_chat_ids:
            self.logger.warning(
                "No Telegram channel mappings configured. "
                "Add bridge.<channelname> = <chat_id> in [TelegramBridge]"
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

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("Telegram bridge service is disabled")
            return
        if not self.channel_chat_ids:
            self.logger.warning("Telegram bridge enabled but no channels configured")
            return

        self.logger.info("Starting Telegram bridge service...")
        if AIOHTTP_AVAILABLE:
            self.http_session = aiohttp.ClientSession()
        else:
            self.logger.debug("Using requests for HTTP (fallback)")

        if hasattr(self.bot, 'meshcore') and self.bot.meshcore:
            self.bot.meshcore.subscribe(EventType.CHANNEL_MSG_RECV, self._on_mesh_channel_message)
            self.logger.info("Subscribed to CHANNEL_MSG_RECV events")
        else:
            self.logger.error("Cannot subscribe to events - meshcore not available")
            return

        # Register for bot-sent channel messages so bot responses are bridged too
        if self.bridge_bot_responses and getattr(self.bot, 'channel_sent_listeners', None) is not None:
            self.bot.channel_sent_listeners.append(self._on_mesh_channel_message)
            self.logger.info("Registered for bot channel-sent events (bridge_bot_responses=true)")

        for chat_id in self.channel_chat_ids.values():
            self.message_queues[chat_id] = []
            self.send_times[chat_id] = deque()

        self._queue_processor_task = asyncio.create_task(self._process_message_queues())
        self._running = True
        self.logger.info(
            f"Telegram bridge service started (bridging {len(self.channel_chat_ids)} channels)"
        )

    async def stop(self) -> None:
        self.logger.info("Stopping Telegram bridge service...")
        self._running = False

        # Unregister bot channel-sent listener
        if getattr(self.bot, 'channel_sent_listeners', None) is not None:
            with contextlib.suppress(ValueError):
                self.bot.channel_sent_listeners.remove(self._on_mesh_channel_message)

        if self._queue_processor_task:
            self._queue_processor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._queue_processor_task
        if self.http_session:
            await self.http_session.close()
            self.http_session = None
        self.logger.info("Telegram bridge service stopped")

    async def _on_mesh_channel_message(self, event, metadata=None) -> None:
        try:
            payload = copy.deepcopy(event.payload) if hasattr(event, 'payload') else None
            if payload is None:
                self.logger.warning("Channel message event has no payload")
                return

            channel_idx = payload.get('channel_idx', 0)
            channel_name = self.bot.channel_manager.get_channel_name(channel_idx)
            text = payload.get('text', '')
            sender = 'Unknown'

            if ':' in text and not text.startswith('http'):
                parts = text.split(':', 1)
                sender = parts[0].strip()

            if not channel_name or channel_name.lower() in ('dm', 'direct', 'private'):
                self.logger.debug("Ignoring DM (DMs are never bridged)")
                return

            chat_id = None
            # Normalize: strip leading # and compare case-insensitively so bridge.HowlTest matches #howltest
            channel_key = channel_name.lstrip('#').lower()
            for config_channel, cid in self.channel_chat_ids.items():
                if config_channel.lstrip('#').lower() == channel_key:
                    chat_id = cid
                    break
            if not chat_id:
                self.logger.debug(f"Channel '{channel_name}' not configured for Telegram bridge")
                return

            if ':' in text and not text.startswith('http'):
                parts = text.split(':', 1)
                sender_name = parts[0].strip()
                message_text = parts[1].strip() if len(parts) > 1 else text
            else:
                sender_name = sender
                message_text = text

            # Profanity filter: drop (don't bridge), censor (replace with ****), or off
            if self.filter_profanity == 'drop':
                if contains_profanity(sender_name, self.logger) or contains_profanity(message_text, self.logger):
                    self.logger.debug(f"Telegram bridge: dropping message with profanity from [{channel_name}]")
                    return
            elif self.filter_profanity == 'censor':
                sender_name = censor(sender_name, self.logger)
                message_text = censor(message_text, self.logger)

            full_text = self._build_message_text(sender_name, message_text, channel_name)
            full_text = self._truncate_text(full_text)

            await self._queue_message(chat_id, full_text, channel_name)
        except Exception as e:
            self.logger.error(f"Error handling mesh channel message: {e}", exc_info=True)

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

    async def _process_message_queues(self) -> None:
        while self._running:
            try:
                current_time = time.time()
                for chat_id, queue in list(self.message_queues.items()):
                    if not queue:
                        continue
                    # Enforce min interval per chat
                    if chat_id in self.send_times:
                        st = self.send_times[chat_id]
                        while st and (current_time - st[0]) > self.rate_limit_min_interval:
                            st.popleft()
                        if st and (current_time - st[-1]) < self.rate_limit_min_interval:
                            continue

                    queued_msg = None
                    for msg in queue:
                        if current_time >= msg.next_retry_at:
                            queued_msg = msg
                            break
                    if queued_msg is None:
                        continue

                    age = current_time - queued_msg.first_queued
                    if age > self.max_queue_age:
                        queue.remove(queued_msg)
                        self.logger.warning(
                            f"Dropping old message from queue [{queued_msg.channel_name}]: "
                            f"age {age:.1f}s > {self.max_queue_age}s"
                        )
                        continue

                    success = await self._send_to_telegram(
                        queued_msg.chat_id,
                        queued_msg.payload,
                        queued_msg.channel_name,
                        queued_msg,
                    )
                    if success:
                        queue.remove(queued_msg)
                        if chat_id not in self.send_times:
                            self.send_times[chat_id] = deque()
                        self.send_times[chat_id].append(current_time)
                    else:
                        queued_msg.retry_count += 1
                        if queued_msg.retry_count > self.max_retries:
                            queue.remove(queued_msg)
                            self.logger.error(
                                f"Dropping message after {self.max_retries} retries "
                                f"[{queued_msg.channel_name}]: {queued_msg.payload['text'][:50]}..."
                            )
                        else:
                            delay = self.retry_delay_base * (2 ** (queued_msg.retry_count - 1))
                            queued_msg.next_retry_at = current_time + delay
                            self.logger.debug(
                                f"Message failed, retry in {delay:.1f}s "
                                f"({queued_msg.retry_count}/{self.max_retries}) [{queued_msg.channel_name}]"
                            )

                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in Telegram queue processor: {e}", exc_info=True)
                await asyncio.sleep(1.0)

    async def _send_to_telegram(
        self,
        chat_id: str,
        payload: dict[str, Any],
        channel_name: str,
        queued_msg: QueuedMessage | None = None,
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
        queued_msg: QueuedMessage | None = None,
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
        except TimeoutError:
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
        queued_msg: QueuedMessage | None = None,
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
