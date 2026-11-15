#!/usr/bin/env python3
"""
Matrix Bridge Service for MeshCore Bot
Posts MeshCore channel messages to Matrix rooms via the Client-Server API
(one-way, read-only)
"""

import asyncio
import contextlib
import html
import os
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote, urlsplit

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

from .channel_bridge_utils import ChannelBridgeBase

# Matrix Client-Server API
MATRIX_CLIENT_API = "/_matrix/client/v3"
MATRIX_HTTP_TIMEOUT = 10.0
# Matrix rejects events over 64 KiB, and the text goes into the event twice
# (body and HTML-escaped formatted_body). Mesh messages are a few hundred bytes
# at most, so 4000 characters leaves ample room even for escaped or multi-byte text.
MATRIX_MAX_MESSAGE_LENGTH = 4000
MATRIX_DEFAULT_RETRY_AFTER = 60.0
# How long to wait before trying an unresolved #alias again.
MATRIX_ALIAS_RETRY_INTERVAL = 60.0
# Responses to /account/whoami that mean the token itself is bad. Anything else
# (timeout, connection error, 5xx) is treated as the homeserver being unreachable.
MATRIX_TOKEN_REJECTED_STATUSES = frozenset({401, 403})
MATRIX_TOKEN_REJECTED_CODES = frozenset({"M_UNKNOWN_TOKEN", "M_MISSING_TOKEN", "M_FORBIDDEN"})
# Errors that will never succeed on retry (bad room, revoked token, no access).
MATRIX_PERMANENT_ERROR_CODES = frozenset({
    "M_FORBIDDEN",
    "M_NOT_FOUND",
    "M_UNKNOWN_TOKEN",
})
MATRIX_PERMANENT_STATUSES = frozenset({401, 403, 404})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})


@dataclass
class QueuedMessage:
    """Represents a message queued for Matrix posting."""
    room_id: str
    payload: dict[str, Any]
    channel_name: str
    txn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    retry_count: int = 0
    first_queued: float = 0.0
    next_retry_at: float = 0.0

    def __post_init__(self):
        if self.first_queued == 0.0:
            self.first_queued = time.time()
        if self.next_retry_at == 0.0:
            self.next_retry_at = time.time()


class MatrixBridgeService(ChannelBridgeBase):
    """Matrix bridge service.

    Posts MeshCore channel messages to Matrix rooms via the Client-Server API.
    One-way bridge - messages only flow from MeshCore to Matrix.
    Direct messages are NEVER bridged for privacy.
    """

    config_section = 'MatrixBridge'
    description = "Posts MeshCore channel messages to Matrix rooms (one-way, read-only)"

    # Web-viewer settings schema (see modules/settings_schema.py)
    settings_schema = [
        {"key": "homeserver", "label": "Homeserver URL", "type": "str",
         "default": "",
         "help": "Base URL of the Matrix homeserver, e.g. https://matrix.org. "
                 "HTTP is accepted only for localhost."},
        {"key": "access_token", "label": "Access token", "type": "password", "default": "",
         "help": "Access token for the bot's Matrix account. Can also be set via the "
                 "MATRIX_ACCESS_TOKEN env var."},
        {"key": "max_message_length", "label": "Max message length", "type": "int",
         "min": 1, "max": MATRIX_MAX_MESSAGE_LENGTH, "default": MATRIX_MAX_MESSAGE_LENGTH,
         "help": "Longer messages are truncated. Default: 4000."},
        {"key": "filter_profanity", "label": "Profanity filter", "type": "enum",
         "options": [{"value": "drop", "label": "Drop (don't bridge)"},
                     {"value": "censor", "label": "Censor (****)"},
                     {"value": "off", "label": "Off"}],
         "default": "drop", "help": "How to handle profanity in messages and usernames."},
        {"key": "bridge_bot_responses", "label": "Bridge bot responses", "type": "bool",
         "default": True, "help": "Also bridge the bot's own command replies."},
    ]
    settings_dynamic_sections = [
        {"section": "MatrixBridge", "key_prefix": "bridge.",
         "label": "Channel mappings", "key_label": "MeshCore channel",
         "value_label": "Matrix room ID or alias",
         "help": "Map a MeshCore channel to one or more Matrix rooms (comma-separated). "
                 "Use !roomid:server or #alias:server. The bot must already be joined "
                 "to each room. DMs are never bridged.",
         "key_placeholder": "Public", "value_placeholder": "!roomid:server or #alias:server"},
    ]

    def __init__(self, bot: Any):
        super().__init__(bot)

        # State stop() relies on, set before any early return so a bridge left
        # disabled by missing config still stops cleanly at shutdown.
        self.channel_rooms: dict[str, list[str]] = {}
        self.resolved_room_ids: dict[str, str] = {}
        self._alias_retry_at: dict[str, float] = {}
        self._rate_limited_until = 0.0
        self.message_queues: dict[str, list[QueuedMessage]] = {}
        self.send_times: dict[str, deque] = {}
        self.http_session: Optional[Any] = None
        self._queue_processor_task: Optional[asyncio.Task] = None
        self._last_send_at = 0.0
        self.matrix_user_id: Optional[str] = None

        if not AIOHTTP_AVAILABLE and not REQUESTS_AVAILABLE:
            self.logger.error(
                "Neither aiohttp nor requests available. Matrix bridge requires one of these."
            )
            self.enabled = False
            return

        # Access token: config or env (env takes precedence for security)
        self.access_token = (
            os.environ.get('MATRIX_ACCESS_TOKEN') or
            self.bot.config.get('MatrixBridge', 'access_token', fallback='')
        ).strip()
        if not self.access_token:
            self.logger.error(
                "Matrix bridge: access_token not set. Set in config or MATRIX_ACCESS_TOKEN env."
            )
            self.enabled = False
            return

        # Homeserver base URL
        homeserver = self._normalize_homeserver(
            self.bot.config.get('MatrixBridge', 'homeserver', fallback='')
        )
        if not homeserver:
            self.logger.error(
                "Matrix bridge: homeserver is missing or invalid. "
                "Set e.g. homeserver = https://matrix.org in [MatrixBridge]."
            )
            self.enabled = False
            return
        self.homeserver = homeserver

        self._load_channel_mappings()

        self.max_message_length = self.bot.config.getint(
            'MatrixBridge', 'max_message_length', fallback=MATRIX_MAX_MESSAGE_LENGTH
        )
        self.max_message_length = max(1, min(self.max_message_length, MATRIX_MAX_MESSAGE_LENGTH))

        # Profanity filter: drop (default), censor, or off
        raw_filter = self.bot.config.get(
            'MatrixBridge', 'filter_profanity', fallback='drop'
        ).strip().lower()
        if raw_filter not in ('drop', 'censor', 'off'):
            raw_filter = 'drop'
        self.filter_profanity = raw_filter

        # Bridge bot's own channel responses to Matrix (default: true)
        self.bridge_bot_responses = self.bot.config.getboolean(
            'MatrixBridge', 'bridge_bot_responses', fallback=True
        )

        # Rate limiting: ~1 message per second overall (Matrix limits are per user)
        self.rate_limit_min_interval = 1.0
        self.global_min_interval = 1.0
        self.max_retries = 5
        self.retry_delay_base = 1.0
        self.max_queue_age = 300

        if not self.channel_rooms:
            self.logger.warning(
                "No Matrix channel mappings configured. "
                "Add bridge.<channelname> = !roomid:server in [MatrixBridge]"
            )

    bridge_label = "Matrix"
    payload_text_key = "body"
    old_message_log = "Dropping old message from queue [{channel}]: age {age:.1f}s > {max_age}s"
    retry_log = "Message failed, retry in {delay:.1f}s ({retry}/{max_retries}) [{channel}]"
    processor_error_log = "Error in Matrix queue processor: {error}"
    preserve_next_retry_at = True

    @property
    def _bridge_mappings(self) -> dict[str, list[str]]:
        return self.channel_rooms

    async def start(self) -> None:
        """Validate the token and resolve aliases before subscribing to events."""
        if not self.enabled:
            self.logger.info("Matrix bridge service is disabled")
            return
        if not self._bridge_mappings:
            self.logger.warning("Matrix bridge enabled but no channels configured")
            return

        self._open_http_session()
        if await self._validate_access_token() is False:
            if self.http_session is not None:
                with contextlib.suppress(Exception):
                    await self.http_session.close()
                self.http_session = None
            return

        await self._resolve_configured_aliases()

        if not any(self._resolved_target(t) for targets in self.channel_rooms.values() for t in targets):
            self.logger.warning(
                "Matrix bridge has no resolved rooms yet; unresolved aliases are "
                "retried when messages arrive"
            )

        await super().start()

    def _open_http_session(self) -> None:
        if AIOHTTP_AVAILABLE:
            if self.http_session is None:
                self.http_session = aiohttp.ClientSession()
        else:
            self.logger.debug("Using requests for HTTP (fallback)")

    def _init_queues(self) -> None:
        for targets in self.channel_rooms.values():
            for target in targets:
                room_id = self._resolved_target(target)
                if room_id:
                    self.message_queues.setdefault(room_id, [])
                    self.send_times.setdefault(room_id, deque())

    async def _deliver(self, targets: list[str], sender_name: str, message_text: str, channel_name: str) -> None:
        payload = self._build_event(sender_name, message_text)
        for target in targets:
            if target.startswith('#') and target not in self.resolved_room_ids:
                await self._try_resolve_alias(target)
            room_id = self._resolved_target(target)
            if not room_id:
                self.logger.debug(f"Skipping unresolved Matrix target {target} [{channel_name}]")
                continue
            await self._queue_message(room_id, payload, channel_name)

    def _throttled(self, key: str, queue: Any, current_time: float) -> bool:
        # Matrix rate limits are per user: a 429 in one room pauses every room.
        if current_time < self._rate_limited_until:
            return True
        # Cap sends across every room as well.
        if self._last_send_at and (current_time - self._last_send_at) < self.global_min_interval:
            return True
        # Enforce a minimum interval per room as well.
        send_times = self.send_times.get(key)
        if send_times:
            while send_times and (current_time - send_times[0]) > self.rate_limit_min_interval:
                send_times.popleft()
            if send_times and (current_time - send_times[-1]) < self.rate_limit_min_interval:
                return True
        return False

    async def _send_queued(self, queued_msg: QueuedMessage) -> bool:
        url = self._room_send_url(queued_msg.room_id, queued_msg.txn_id)
        try:
            status, data, headers = await self._request_json("PUT", url, queued_msg.payload)
        except asyncio.TimeoutError:
            self.logger.warning(f"Timeout posting to Matrix [{queued_msg.channel_name}]")
            return False
        except Exception as e:
            self.logger.warning(f"Error posting to Matrix [{queued_msg.channel_name}]: {e}")
            return False

        errcode = str(data.get('errcode') or '')

        if 200 <= status < 300:
            self.logger.debug(
                f"Posted to Matrix [{queued_msg.channel_name}]: "
                f"{queued_msg.payload.get('body', '')[:50]}..."
            )
            self._last_send_at = time.time()
            return True

        if status == 429 or errcode == 'M_LIMIT_EXCEEDED':
            retry_after = self._retry_after_seconds(data, headers)
            self.logger.warning(
                f"Matrix rate limit for [{queued_msg.channel_name}]. Retry after: {retry_after:.0f}s"
            )
            queued_msg.next_retry_at = time.time() + retry_after
            self._rate_limited_until = max(self._rate_limited_until, queued_msg.next_retry_at)
            # The base queue increments retry_count after a failure; compensate
            # for that so repeated rate-limit waits do not grow the retry count.
            queued_msg.retry_count = max(0, queued_msg.retry_count - 1)
            return False

        if status in MATRIX_PERMANENT_STATUSES or errcode in MATRIX_PERMANENT_ERROR_CODES:
            # Permanent: logging once and dropping is kinder than retrying five
            # times. Returning True tells the base queue the message is handled.
            self.logger.warning(
                f"Matrix rejected message for [{queued_msg.channel_name}] "
                f"(status {status}, {errcode or 'no errcode'}); dropping without retry"
            )
            return True

        self.logger.warning(
            f"Matrix API returned {status} for [{queued_msg.channel_name}]: "
            f"{errcode or data.get('error', '')}"
        )
        return False

    def _load_channel_mappings(self) -> None:
        """Load bridge.<channel> = <room id or alias>[, ...] from config."""
        if not self.bot.config.has_section('MatrixBridge'):
            return
        for key, value in self.bot.config.items('MatrixBridge'):
            if not key.startswith('bridge.'):
                continue
            channel_name = key[7:].strip()
            # Normalize plain names the way the Discord bridge does ("public" →
            # "Public"); keys starting with "#" keep their form.
            if channel_name and channel_name[0].isalpha():
                channel_name = channel_name[0].upper() + channel_name[1:]
            raw_value = value.strip()
            if not raw_value:
                continue

            targets: list[str] = []
            for candidate in re.split(r'[,\s]+', raw_value):
                target = candidate.strip()
                if not target:
                    continue
                if target.startswith('!') or target.startswith('#'):
                    targets.append(target)
                else:
                    self.logger.warning(
                        f"Ignoring invalid Matrix target for channel '{channel_name}': "
                        "expected a !roomid:server or #alias:server"
                    )
            if not targets:
                continue
            self.channel_rooms.setdefault(channel_name, []).extend(targets)
            self.logger.info(
                f"Configured Matrix bridge: {channel_name} → {', '.join(targets)}"
            )
        self.logger.info(
            f"Loaded {len(self.channel_rooms)} Matrix channel mapping(s)"
        )

    @staticmethod
    def _normalize_homeserver(raw: str) -> Optional[str]:
        """Return a normalized homeserver URL, or None when it is unsafe/invalid."""
        homeserver = (raw or '').strip().rstrip('/')
        if not homeserver:
            return None
        parsed = urlsplit(homeserver)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return None
        if parsed.scheme == 'http' and parsed.hostname not in _LOCAL_HOSTS:
            return None
        return homeserver

    def _mask_token(self, token: str) -> str:
        if not token or len(token) < 8:
            return "***"
        return token[:4] + "..." + token[-4:]

    def _prepare_message_text(self, message_text: str) -> str:
        """Render MeshCore @[name] mentions as plain text (no Matrix pills)."""
        return re.sub(r'@\[([^\]]+)\]', r'@\1', message_text)

    def _escape_html(self, value: str) -> str:
        return html.escape(value, quote=True)

    def _build_event(self, sender_name: str, message_text: str) -> dict[str, Any]:
        """Build the m.room.message event sent to every room."""
        prefix = f"{sender_name}: "
        available = max(1, self.max_message_length - len(prefix))
        text = message_text
        if len(text) > available:
            text = text[: available - 1].rstrip() + "…"

        return {
            "msgtype": "m.text",
            "body": f"{prefix}{text}",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<b>{self._escape_html(sender_name)}</b>: {self._escape_html(text)}",
        }

    async def _queue_message(self, room_id: str, payload: dict[str, Any], channel_name: str) -> None:
        queued = QueuedMessage(
            room_id=room_id,
            payload=payload,
            channel_name=channel_name,
            # One txnId per message per room, reused across every retry.
            txn_id=uuid.uuid4().hex,
        )
        self.message_queues.setdefault(room_id, []).append(queued)
        self.logger.debug(
            f"Queued message for Matrix [{channel_name}]: {payload.get('body', '')[:50]}..."
        )

    def _resolved_target(self, target: str) -> Optional[str]:
        """Map a configured target to a room id, or None if unresolved/invalid."""
        if target.startswith('!'):
            return target
        if target.startswith('#'):
            return self.resolved_room_ids.get(target)
        return None

    async def _resolve_configured_aliases(self) -> None:
        """Try each configured #alias once at startup."""
        for targets in self.channel_rooms.values():
            for target in targets:
                if target.startswith('#'):
                    await self._try_resolve_alias(target)

    async def _try_resolve_alias(self, alias: str) -> Optional[str]:
        """Resolve *alias* unless it is cached or failed within the retry interval.

        Successes are cached for the life of the service. A failure (homeserver
        down at boot, alias not created yet) is retried at most once per
        MATRIX_ALIAS_RETRY_INTERVAL, so one bad alias cannot stall every message.
        """
        cached = self.resolved_room_ids.get(alias)
        if cached:
            return cached
        if time.time() < self._alias_retry_at.get(alias, 0.0):
            return None
        room_id = await self._resolve_room_alias(alias)
        if room_id:
            self.resolved_room_ids[alias] = room_id
            self._alias_retry_at.pop(alias, None)
        else:
            self._alias_retry_at[alias] = time.time() + MATRIX_ALIAS_RETRY_INTERVAL
        return room_id

    async def _resolve_room_alias(self, alias: str) -> Optional[str]:
        url = f"{self.homeserver}{MATRIX_CLIENT_API}/directory/room/{quote(alias, safe='')}"
        try:
            status, data, _headers = await self._request_json("GET", url)
        except asyncio.TimeoutError:
            self.logger.warning(f"Timeout resolving Matrix room alias {alias}; will retry")
            return None
        except Exception as e:
            self.logger.warning(f"Error resolving Matrix room alias {alias}: {e}; will retry")
            return None

        room_id = data.get('room_id')
        if status == 200 and room_id:
            self.logger.info(f"Resolved Matrix room alias {alias} → {room_id}")
            return str(room_id)
        self.logger.warning(
            f"Could not resolve Matrix room alias {alias} "
            f"(status {status}, {data.get('errcode') or 'no errcode'}); will retry"
        )
        return None

    async def _validate_access_token(self) -> Optional[bool]:
        """Check the token with /account/whoami before starting delivery.

        Returns True when the token is accepted, False when the homeserver
        rejects it, and None when the homeserver could not be reached. Only a
        rejection stops the bridge; an unreachable homeserver at boot (DNS or
        network not up yet) must not leave the bridge off until the next restart.
        """
        if not self.access_token:
            self.logger.error("Matrix bridge: no access token; not starting delivery")
            return False
        url = f"{self.homeserver}{MATRIX_CLIENT_API}/account/whoami"
        unreachable = (
            "Matrix bridge: could not check the access token ({reason}); "
            "starting anyway, messages retry until the homeserver answers"
        )
        try:
            status, data, _headers = await self._request_json("GET", url)
        except asyncio.TimeoutError:
            self.logger.warning(unreachable.format(reason="timeout"))
            return None
        except Exception as e:
            self.logger.warning(unreachable.format(reason=e))
            return None

        user_id = data.get('user_id')
        if status == 200 and user_id:
            self.matrix_user_id = str(user_id)
            self.logger.info(
                f"Matrix bridge authenticated as {self.matrix_user_id} "
                f"(token {self._mask_token(self.access_token)})"
            )
            return True

        errcode = str(data.get('errcode') or '')
        if status not in MATRIX_TOKEN_REJECTED_STATUSES and errcode not in MATRIX_TOKEN_REJECTED_CODES:
            self.logger.warning(unreachable.format(reason=f"status {status}, {errcode or 'no errcode'}"))
            return None

        self.logger.error(
            f"Matrix bridge: access token rejected (status {status}, "
            f"{data.get('errcode') or 'no errcode'}); not starting delivery"
        )
        return False

    def _room_send_url(self, room_id: str, txn_id: str) -> str:
        return (
            f"{self.homeserver}{MATRIX_CLIENT_API}/rooms/{quote(room_id, safe='')}"
            f"/send/m.room.message/{quote(txn_id, safe='')}"
        )

    @staticmethod
    def _retry_after_seconds(data: dict[str, Any], headers: Any) -> float:
        """Seconds to wait from retry_after_ms, then Retry-After, then a default."""
        retry_ms = data.get('retry_after_ms')
        if retry_ms is not None:
            try:
                return max(0.0, float(retry_ms) / 1000.0)
            except (TypeError, ValueError):
                pass
        retry_header = headers.get('Retry-After') if headers is not None else None
        if retry_header is not None:
            try:
                return max(0.0, float(retry_header))
            except (TypeError, ValueError):
                pass
        return MATRIX_DEFAULT_RETRY_AFTER

    async def _request_json(
        self,
        method: str,
        url: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> tuple[int, dict[str, Any], Any]:
        """Send one Client-Server API request, returning (status, JSON body, headers)."""
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        if AIOHTTP_AVAILABLE and self.http_session is not None:
            requester = self.http_session.put if method == "PUT" else self.http_session.get
            kwargs: dict[str, Any] = {
                "headers": headers,
                "timeout": aiohttp.ClientTimeout(total=MATRIX_HTTP_TIMEOUT),
            }
            if payload is not None:
                kwargs["json"] = payload
            async with requester(url, **kwargs) as response:
                status = response.status
                response_headers = response.headers
                return status, await self._response_json(response), response_headers

        if REQUESTS_AVAILABLE:
            loop = asyncio.get_running_loop()

            def _sync_request() -> tuple[int, dict[str, Any], Any]:
                response = requests.request(
                    method, url, json=payload, headers=headers,
                    timeout=MATRIX_HTTP_TIMEOUT,
                )
                try:
                    data = response.json() if response.content else {}
                except ValueError:
                    data = {}
                return response.status_code, data if isinstance(data, dict) else {}, response.headers

            return await loop.run_in_executor(None, _sync_request)

        raise RuntimeError("No HTTP library available for Matrix")

    @staticmethod
    async def _response_json(response: Any) -> dict[str, Any]:
        try:
            data = await response.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
