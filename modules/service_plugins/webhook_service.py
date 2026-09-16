#!/usr/bin/env python3
"""
Inbound Webhook Service for MeshCore Bot

Starts a lightweight HTTP server that accepts POST requests from external
systems and relays them as messages into MeshCore channels or DMs.

Config section ``[Webhook]``::

    enabled        = false
    host           = 127.0.0.1     # bind address (0.0.0.0 to expose externally)
    port           = 8765           # listen port
    secret_token   =               # if set, require Authorization: Bearer <token>
    allowed_channels =             # comma-separated whitelist; empty = all channels
    max_message_length = 200       # truncate messages exceeding this length

A message longer than one MeshCore frame is split across several mesh messages
rather than sent whole: the firmware's text limit is 160 bytes and a channel
body rides inside ``"<botname>: <body>"``, so an over-long payload is dropped by
the device without an acknowledgement. Each part of a multi-part send is tagged
``" (i/n)"`` so a reader can order them on a mesh that does not guarantee
delivery order; a message that fits in one part carries no suffix.
``max_message_length`` still bounds how much text is accepted (and therefore how
much airtime one request can buy); it is a character cap across all parts, not a
single-frame limit.

HTTP API
--------
POST /webhook
    Content-Type: application/json
    Authorization: Bearer <secret_token>   (if secret_token is configured)

    Body (channel message)::

        {"channel": "general", "message": "Hello from webhook!"}

    Optional regional TC_FLOOD scope (overrides [Webhook] flood_scope for this request)::

        {"channel": "general", "message": "Hello", "flood_scope": "#west"}

    Body (DM)::

        {"dm_to": "SomeUser", "message": "Private message"}

Response codes:
    200  {"ok": true, "parts": 1}   parts = mesh messages the text was split into
    400  {"error": "..."}   bad/missing fields
    401  {"error": "Unauthorized"}   wrong / missing token
    405  method not allowed
    429  {"error": "Rate limit exceeded"}
    503  {"error": "Bot not yet connected to mesh, try again shortly"}
         The listener starts before the radio connection is established, so
         callers may see this briefly after service start; retry after a
         few seconds.
"""

import json
import secrets
import time
from collections import OrderedDict
from typing import Any, Optional

from ..models import DM_BODY_LIMIT
from .base_service import BaseServicePlugin

try:
    from aiohttp import web as aio_web
    AIOHTTP_AVAILABLE = True
except ImportError:
    aio_web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False


class WebhookService(BaseServicePlugin):
    """Accept inbound HTTP POST webhooks and relay them as MeshCore messages."""

    config_section = "Webhook"
    description = "Inbound webhook receiver — relay HTTP POST payloads to MeshCore channels"

    # Web-viewer settings schema (see modules/settings_schema.py)
    settings_schema = [
        {"key": "host", "label": "Bind host", "type": "str", "default": "127.0.0.1",
         "help": "127.0.0.1 = localhost only (recommended); 0.0.0.0 = any interface."},
        {"key": "port", "label": "Listen port", "type": "int", "min": 1, "max": 65535, "default": 8765,
         "help": "Must not conflict with the web viewer port."},
        {"key": "secret_token", "label": "Secret token", "type": "str", "default": "",
         "help": "If set, requests must include it via 'Authorization: Bearer' or 'X-Webhook-Token'. "
                 "Empty disables auth (not recommended)."},
        {"key": "max_message_length", "label": "Max message length", "type": "int", "min": 1, "default": 200,
         "help": "Total characters accepted per request; excess is silently truncated. Text longer "
                 "than one mesh frame is split across several messages tagged '(1/2)', '(2/2)', "
                 "so a large value here buys more airtime per request."},
        {"key": "allowed_channels", "label": "Allowed channels", "type": "list", "default": "",
         "help": "Comma-separated channel whitelist. Empty = any channel."},
        {"key": "flood_scope", "label": "Flood scope", "type": "str", "default": "",
         "help": "Optional regional TC_FLOOD scope for posts (e.g. #west)."},
    ]

    # Maximum body size accepted (bytes)
    MAX_BODY_SIZE = 8_192

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)

        cfg = bot.config

        if not AIOHTTP_AVAILABLE:
            self.logger.error(
                "WebhookService requires aiohttp. Install it with: pip install aiohttp"
            )
            self.enabled = False
            return

        if not cfg.has_section("Webhook"):
            self.enabled = False
            return

        self.enabled = cfg.getboolean("Webhook", "enabled", fallback=False)
        self.host = cfg.get("Webhook", "host", fallback="127.0.0.1").strip()
        self.port = cfg.getint("Webhook", "port", fallback=8765)
        self.secret_token: str = cfg.get("Webhook", "secret_token", fallback="").strip()
        self.max_message_length: int = cfg.getint(
            "Webhook", "max_message_length", fallback=200
        )

        raw_channels = cfg.get("Webhook", "allowed_channels", fallback="").strip()
        self.allowed_channels = (
            {c.strip().removeprefix("#").lower() for c in raw_channels.split(",") if c.strip()}
            if raw_channels
            else set()
        )

        self._runner: Optional[Any] = None  # aio_web.AppRunner
        self._site: Optional[Any] = None    # aio_web.TCPSite

        # Per-IP rate limiting
        self._rate_limit_per_minute: int = cfg.getint(
            "Webhook", "rate_limit_per_minute", fallback=30
        )
        self._rate_window: float = 60.0  # seconds
        self._request_log: OrderedDict[str, list[float]] = OrderedDict()  # ip -> [timestamps]
        self._max_tracked_ips: int = 1000

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("Webhook service is disabled")
            return

        app = aio_web.Application(client_max_size=self.MAX_BODY_SIZE)
        app.router.add_post("/webhook", self._handle_webhook)

        self._runner = aio_web.AppRunner(app)
        await self._runner.setup()
        self._site = aio_web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

        auth_info = "token auth enabled" if self.secret_token else "NO auth (secret_token not set)"
        self.logger.info(
            f"Webhook service listening on {self.host}:{self.port} ({auth_info})"
        )
        self._running = True

    async def stop(self) -> None:
        self._running = False
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
        self.logger.info("Webhook service stopped")

    # ------------------------------------------------------------------
    # Request handler
    # ------------------------------------------------------------------

    def _is_rate_limited(self, remote_ip: str) -> bool:
        """Return True if *remote_ip* has exceeded the per-minute request limit."""
        if self._rate_limit_per_minute <= 0:
            return False  # Rate limiting disabled

        now = time.monotonic()
        cutoff = now - self._rate_window

        timestamps = self._request_log.get(remote_ip)
        if timestamps is not None:
            # Prune expired entries
            timestamps[:] = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= self._rate_limit_per_minute:
                return True
            timestamps.append(now)
            self._request_log.move_to_end(remote_ip)
        else:
            self._request_log[remote_ip] = [now]

        # Evict oldest IPs to bound memory
        while len(self._request_log) > self._max_tracked_ips:
            self._request_log.popitem(last=False)

        return False

    async def _handle_webhook(self, request: Any) -> Any:
        """Handle a POST /webhook request."""
        # --- Rate limiting ---
        remote_ip = request.remote or "unknown"
        if self._is_rate_limited(remote_ip):
            self.logger.warning(f"Webhook: rate limited request from {remote_ip}")
            return aio_web.Response(
                status=429,
                content_type="application/json",
                text='{"error": "Rate limit exceeded"}',
            )

        # --- Readiness ---
        # The listener starts before the mesh connection is established (see
        # core.py), so reject requests with a clear, retryable error until the
        # bot is actually connected, rather than accepting them only to fail
        # message dispatch.
        if not getattr(self.bot, "connected", False):
            return aio_web.Response(
                status=503,
                content_type="application/json",
                text='{"error": "Bot not yet connected to mesh, try again shortly"}',
            )

        # --- Auth ---
        if self.secret_token and not self._verify_token(request):
            self.logger.warning(
                f"Webhook: rejected unauthenticated request from {request.remote}"
            )
            return aio_web.Response(
                status=401,
                content_type="application/json",
                text='{"error": "Unauthorized"}',
            )

        # --- Parse body ---
        try:
            body = await request.json()
        except Exception:
            return aio_web.Response(
                status=400,
                content_type="application/json",
                text='{"error": "Invalid JSON body"}',
            )

        message_text: str = str(body.get("message", "")).strip()
        if not message_text:
            return aio_web.Response(
                status=400,
                content_type="application/json",
                text='{"error": "Missing required field: message"}',
            )

        # Cap the total text accepted. This bounds how much airtime one request can
        # buy; it is not a single-frame limit — the send path splits what is left
        # into as many mesh messages as the RF budget needs.
        if len(message_text) > self.max_message_length:
            message_text = message_text[: self.max_message_length]

        channel: str = str(body.get("channel", "")).strip().removeprefix("#")
        dm_to: str = str(body.get("dm_to", "")).strip()

        if not channel and not dm_to:
            return aio_web.Response(
                status=400,
                content_type="application/json",
                text='{"error": "Missing required field: channel or dm_to"}',
            )

        # --- Channel whitelist check ---
        if channel and self.allowed_channels and channel.lower() not in self.allowed_channels:
            self.logger.warning(
                f"Webhook: channel '{channel}' not in allowed_channels whitelist"
            )
            return aio_web.Response(
                status=400,
                content_type="application/json",
                text='{"error": "Channel not allowed"}',
            )

        # --- Dispatch ---
        try:
            if channel:
                from modules.command_manager import CommandManager

                body_scope_raw = str(body.get("flood_scope") or "").strip()
                mesh_scope: str | None = (
                    CommandManager._normalize_scope_name(body_scope_raw)
                    if body_scope_raw
                    else self.get_mesh_flood_scope()
                )
                sent, parts = await self._send_channel_message(
                    channel, message_text, scope=mesh_scope
                )
                if not sent:
                    self.logger.error(
                        f"Webhook: failed to send to channel '{channel}' from {request.remote}"
                    )
                    return aio_web.Response(
                        status=500,
                        content_type="application/json",
                        text='{"error": "Failed to send message"}',
                    )
                self.logger.info(
                    f"Webhook: sent to #{channel} from {request.remote} "
                    f"in {parts} part(s): "
                    f"{message_text[:60]}{'...' if len(message_text) > 60 else ''}"
                )
            else:
                sent, parts = await self._send_dm(dm_to, message_text)
                if not sent:
                    self.logger.error(
                        f"Webhook: failed to send DM to '{dm_to}' from {request.remote}"
                    )
                    return aio_web.Response(
                        status=500,
                        content_type="application/json",
                        text='{"error": "Failed to send message"}',
                    )
                self.logger.info(
                    f"Webhook: sent DM to {dm_to} from {request.remote} "
                    f"in {parts} part(s): "
                    f"{message_text[:60]}{'...' if len(message_text) > 60 else ''}"
                )
        except Exception as exc:
            self.logger.error(f"Webhook: failed to send message: {exc}")
            return aio_web.Response(
                status=500,
                content_type="application/json",
                text='{"error": "Failed to send message"}',
            )

        return aio_web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({"ok": True, "parts": parts}),
        )

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _verify_token(self, request: Any) -> bool:
        """Return True if the request carries the correct bearer token."""
        auth_header: str = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            provided = auth_header[7:].strip()
            return secrets.compare_digest(provided, self.secret_token)
        # Also accept X-Webhook-Token header
        x_token: str = request.headers.get("X-Webhook-Token", "").strip()
        if x_token:
            return secrets.compare_digest(x_token, self.secret_token)
        return False

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    def _command_manager(self) -> Any:
        cm = getattr(self.bot, "command_manager", None)
        if cm is None:
            raise RuntimeError("command_manager not available on bot")
        return cm

    async def _send_channel_message(
        self, channel: str, message: str, *, scope: str | None = None
    ) -> tuple[bool, int]:
        """Send a message to a MeshCore channel, splitting it to fit the RF budget.

        A channel body larger than one frame is undeliverable: the device drops it
        without the confirmation event the send waits for, which costs three
        retries and then looks like a dead transport. Split it here so each part
        goes out whole. Multi-part sends are tagged ``" (i/n)"`` so a reader can
        order them; a message that fits in one part carries no suffix.

        Returns:
            (ok, parts) — whether every part was sent, and how many there were.
        """
        cm = self._command_manager()
        budget = cm.channel_body_budget(channel=channel, scope=scope)
        chunks = cm.split_text_into_numbered_utf8_chunks(message, budget)
        if len(chunks) == 1:
            ok = await cm.send_channel_message(
                channel,
                chunks[0],
                skip_user_rate_limit=True,
                rate_limit_key=None,
                scope=scope,
            )
            return bool(ok), 1
        self.logger.info(
            f"Webhook: message for #{channel} is {len(message.encode('utf-8'))} bytes; "
            f"splitting into {len(chunks)} mesh messages ({budget}-byte budget)"
        )
        for link in cm.links_split_across(message, chunks):
            self.logger.warning(
                f"Webhook: link too long for one {budget}-byte message and had to be "
                f"cut, so it will not be clickable: {link}"
            )
        ok = await cm.send_channel_messages_chunked(
            channel,
            chunks,
            skip_user_rate_limit=True,
            rate_limit_key=None,
            scope=scope,
        )
        return bool(ok), len(chunks)

    async def _send_dm(self, recipient: str, message: str) -> tuple[bool, int]:
        """Send a direct message via command_manager.

        ``send_dm`` applies its own byte guard and splits oversized bodies, so the
        whole text goes over in one call; the part count is derived with the same
        helper and budget it uses.

        Returns:
            (ok, parts) — whether the send succeeded, and how many parts it took.
        """
        cm = self._command_manager()
        parts = len(cm.split_text_into_utf8_chunks(message, DM_BODY_LIMIT))
        ok = await cm.send_dm(
            recipient,
            message,
            skip_user_rate_limit=True,
            rate_limit_key=None,
        )
        return bool(ok), parts
