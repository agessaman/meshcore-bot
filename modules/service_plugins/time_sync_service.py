#!/usr/bin/env python3
"""Authenticated binary time-sync source service."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .base_service import BaseServicePlugin
from ..time_sync import (
    TIME_SYNC_DATA_TYPE,
    TIME_SYNC_MAX_TIMESTAMP,
    TIME_SYNC_MIN_TIMESTAMP,
    TimeSyncError,
    build_time_sync_canonical,
    build_time_sync_payload,
    derive_time_sync_channel_id,
    normalise_hex,
    next_sequence,
    resolve_channel_secret,
    validate_display_name,
    validate_sequence,
)


@dataclass(frozen=True)
class TimeSyncSettings:
    """Validated time-sync service settings."""

    channel: str
    identity_name: str
    public_key: bytes
    interval_seconds: int
    flood_scope: str
    full_flood_enabled: bool = False
    text_broadcast_enabled: bool = False
    text_channel: str = ""


class TimeSyncService(BaseServicePlugin):
    """Periodically emits signed binary ``Tv1`` group datagrams."""

    config_section = "Time_Sync"
    name = "timesync"
    description = "Authenticated binary MeshCore repeater time-sync source"
    settings_schema = [
        {
            "key": "channel",
            "label": "Datagram channel",
            "type": "str",
            "default": "#time",
            "required": True,
            "help": "MeshCore group/public channel used for binary Tv1 group datagrams.",
            "width": "md",
        },
        {
            "key": "interval_seconds",
            "label": "Interval",
            "type": "int",
            "default": 604800,
            "min": 1,
            "unit": "s",
            "help": "Seconds between automatic time-sync broadcasts.",
            "width": "sm",
        },
        {
            "key": "flood_scope",
            "label": "Regional flood scope",
            "type": "str",
            "default": "",
            "required": False,
            "help": "Required unless full flood is enabled. Full flood uses * and ignores regional scope.",
            "width": "md",
        },
        {
            "key": "full_flood_enabled",
            "label": "Allow full flood",
            "type": "bool",
            "default": False,
            "help": "Opt in to global/full-flood time broadcasts; flood_scope is forced to *.",
            "width": "md",
        },
        {
            "key": "sequence",
            "label": "Initial sequence",
            "type": "int",
            "default": 0,
            "min": 0,
            "max": 65535,
            "help": "Used only when no persisted time_sync.sequence metadata exists.",
            "width": "sm",
        },
        {
            "key": "text_broadcast_enabled",
            "label": "Send human-readable text",
            "type": "bool",
            "default": False,
            "help": "Also send a readable channel text announcement after the binary datagram is accepted.",
            "width": "md",
        },
        {
            "key": "text_channel",
            "label": "Text channel",
            "type": "str",
            "default": "",
            "help": "Optional text-equivalent channel. Blank sends text to the datagram channel.",
            "width": "md",
        },
    ]

    def __init__(self, bot: Any):
        super().__init__(bot)
        self.enabled = bot.config.getboolean(self.config_section, "enabled", fallback=False)
        self._task: asyncio.Task | None = None
        self._settings: TimeSyncSettings | None = None
        self._sequence: int = 0
        self._last_sequence_persist: float = 0.0
        self._sequence_metadata_key = "time_sync.sequence"

    def _load_configured_sequence(self) -> int:
        """Load initial sequence from persisted metadata, then config fallback."""
        persisted = None
        try:
            persisted = self.bot.db_manager.get_metadata(self._sequence_metadata_key)
        except Exception as exc:
            self.logger.debug("Could not read persisted time-sync sequence: %s", exc)

        if persisted not in (None, ""):
            try:
                return validate_sequence(int(str(persisted)))
            except (TypeError, ValueError, TimeSyncError):
                self.logger.warning("Ignoring invalid persisted time-sync sequence: %r", persisted)

        configured = self.bot.config.getint(self.config_section, "sequence", fallback=0)
        return validate_sequence(configured)

    def _load_settings(self) -> TimeSyncSettings:
        """Validate config and derive public key before the sender loop starts."""
        channel = self.bot.config.get(self.config_section, "channel", fallback="").strip()
        if not channel:
            raise TimeSyncError("channel is required")

        # Fail early if the configured channel is not currently known by the bot.
        if self.bot.channel_manager.get_channel_number(channel) is None:
            raise TimeSyncError(
                f"channel {channel!r} was not found in the MeshCore channel cache; "
                "add it to the radio's configured channels or set [Time_Sync] channel "
                "to an existing group/public channel"
            )

        identity_name = self._bot_identity_name()

        interval_seconds = self.bot.config.getint(self.config_section, "interval_seconds", fallback=604800)
        if interval_seconds <= 0:
            raise TimeSyncError("interval_seconds must be greater than zero")

        full_flood_enabled = self.bot.config.getboolean(
            self.config_section,
            "full_flood_enabled",
            fallback=False,
        )
        flood_scope = self._load_flood_scope(full_flood_enabled=full_flood_enabled)

        text_broadcast_enabled = self.bot.config.getboolean(
            self.config_section,
            "text_broadcast_enabled",
            fallback=False,
        )
        text_channel = self.bot.config.get(self.config_section, "text_channel", fallback="").strip()
        if (
            text_broadcast_enabled
            and text_channel
            and self.bot.channel_manager.get_channel_number(text_channel) is None
        ):
            raise TimeSyncError(
                f"text_channel {text_channel!r} was not found in the MeshCore channel cache; "
                "leave it blank to use the datagram channel or set it to an existing text channel"
            )

        public_key = self._bot_public_key()

        return TimeSyncSettings(
            channel=channel,
            identity_name=identity_name,
            public_key=public_key,
            interval_seconds=interval_seconds,
            flood_scope=flood_scope,
            full_flood_enabled=full_flood_enabled,
            text_broadcast_enabled=text_broadcast_enabled,
            text_channel=text_channel,
        )

    @staticmethod
    def _is_global_flood_scope(scope: str) -> bool:
        """Return True for scope values that ask firmware to use full flood."""
        return scope in ("", "*", "0", "None") or scope.lower() == "none"

    @staticmethod
    def _normalise_flood_scope(scope: str) -> str:
        """Return the firmware scope token used for regional or global flood."""
        if TimeSyncService._is_global_flood_scope(scope):
            return scope
        if not scope.startswith("#"):
            return f"#{scope}"
        return scope

    def _load_flood_scope(self, *, full_flood_enabled: bool) -> str:
        """Validate the time-sync flood scope policy."""
        raw_scope = (self.bot.config.get(self.config_section, "flood_scope", fallback="") or "").strip()
        if full_flood_enabled:
            return "*"

        flood_scope = self._normalise_flood_scope(raw_scope)
        if self._is_global_flood_scope(flood_scope):
            raise TimeSyncError(
                "regional flood_scope is required unless [Time_Sync] full_flood_enabled is true"
            )
        return flood_scope

    def _bot_identity_name(self) -> str:
        """Return the existing bot/radio identity name used in signed Tv1 payloads."""
        meshcore = getattr(self.bot, "meshcore", None)
        self_info = getattr(meshcore, "self_info", None) if meshcore is not None else None
        device_name: Any = None
        if isinstance(self_info, dict):
            device_name = self_info.get("name") or self_info.get("adv_name")
        elif self_info is not None:
            device_name = getattr(self_info, "name", None) or getattr(self_info, "adv_name", None)

        # The wider bot code uses [Bot] bot_name as the configured identity and
        # normally pushes it to the radio at startup.  Fall back to it if the
        # connected device has not exposed its current name yet.
        identity_name = str(device_name or self.bot.config.get("Bot", "bot_name", fallback=""))
        validate_display_name(identity_name)
        return identity_name

    @staticmethod
    def _coerce_public_key_bytes(value: Any) -> bytes:
        """Return the existing bot identity public key as 32 raw bytes."""
        if isinstance(value, bytes):
            raw = value
        elif isinstance(value, bytearray):
            raw = bytes(value)
        elif isinstance(value, str):
            raw = bytes.fromhex(normalise_hex(value, expected_bytes=32, field_name="public_key"))
        else:
            raise TimeSyncError(f"unexpected public key type: {type(value).__name__}")

        if len(raw) != 32:
            raise TimeSyncError("bot identity public key must be exactly 32 bytes")
        return raw

    def _bot_public_key(self) -> bytes:
        """Load the public key for the already-configured MeshCore bot identity."""
        meshcore = getattr(self.bot, "meshcore", None)
        self_info = getattr(meshcore, "self_info", None) if meshcore is not None else None
        public_key: Any = None
        if isinstance(self_info, dict):
            public_key = self_info.get("public_key")
        elif self_info is not None and hasattr(self_info, "public_key"):
            public_key = self_info.public_key

        if not public_key:
            raise TimeSyncError("bot identity public key is unavailable")
        return self._coerce_public_key_bytes(public_key)

    async def _sign_with_bot_identity(self, canonical: bytes) -> bytes:
        """Ask the connected MeshCore device to sign with its existing identity."""
        meshcore = getattr(self.bot, "meshcore", None)
        commands = getattr(meshcore, "commands", None) if meshcore is not None else None
        sign = getattr(commands, "sign", None)
        if sign is None:
            raise TimeSyncError("MeshCore device does not support identity signing")

        result = await sign(canonical)
        result_type = getattr(result, "type", None)
        if str(result_type).endswith("ERROR") or getattr(result_type, "name", "") == "ERROR":
            payload = getattr(result, "payload", {}) or {}
            reason = payload.get("reason", "unknown") if isinstance(payload, dict) else payload
            raise TimeSyncError(f"device signing failed: {reason}")

        payload = getattr(result, "payload", {}) or {}
        signature = payload.get("signature") if isinstance(payload, dict) else None
        if isinstance(signature, bytes):
            raw_signature = signature
        elif isinstance(signature, bytearray):
            raw_signature = bytes(signature)
        elif isinstance(signature, str):
            raw_signature = bytes.fromhex(normalise_hex(signature, expected_bytes=64, field_name="signature"))
        else:
            raise TimeSyncError("device did not return a usable Ed25519 signature")

        if len(raw_signature) != 64:
            raise TimeSyncError("device signature must be exactly 64 bytes")
        return raw_signature

    def _get_channel_secret(self, channel: str) -> bytes:
        """Resolve the raw 16-byte channel secret for canonical signature scope."""
        channel_info = self.bot.channel_manager.get_channel_by_name(channel)
        channel_key_hex = channel_info.get("channel_key_hex") if channel_info else None
        return resolve_channel_secret(channel, channel_key_hex)

    def _persist_sequence(self, *, force: bool = False) -> None:
        """Persist sequence in bot_metadata using the existing low-write store."""
        if self._settings is None:
            return
        now = time.time()
        if not force and self._settings and now - self._last_sequence_persist < self._settings.interval_seconds:
            return
        try:
            self.bot.db_manager.set_metadata(self._sequence_metadata_key, str(self._sequence))
            self._last_sequence_persist = now
        except Exception as exc:
            self.logger.warning("Could not persist time-sync sequence: %s", exc)

    def public_key_hex(self) -> str:
        """Return full public key as 64 lowercase hex characters."""
        if self._settings is not None:
            return self._settings.public_key.hex()
        return self._bot_public_key().hex()

    def status(self) -> dict[str, Any]:
        """Return redacted service status for admin/status commands."""
        fingerprint = None
        if self._settings is not None:
            fingerprint = self._settings.public_key.hex()[:12]
        else:
            try:
                fingerprint = self.public_key_hex()[:12]
            except TimeSyncError:
                fingerprint = None
        text_broadcast_enabled = (
            self._settings.text_broadcast_enabled
            if self._settings
            else self.bot.config.getboolean(self.config_section, "text_broadcast_enabled", fallback=False)
        )
        text_channel = (
            self._settings.text_channel
            if self._settings
            else self.bot.config.get(self.config_section, "text_channel", fallback="")
        )
        flood_scope = (
            self._settings.flood_scope
            if self._settings
            else self.bot.config.get(self.config_section, "flood_scope", fallback="")
        )
        full_flood_enabled = (
            self._settings.full_flood_enabled
            if self._settings
            else self.bot.config.getboolean(self.config_section, "full_flood_enabled", fallback=False)
        )

        return {
            "enabled": self.enabled,
            "running": self._running,
            "channel": (
                self._settings.channel
                if self._settings
                else self.bot.config.get(self.config_section, "channel", fallback="")
            ),
            "identity_name": self._settings.identity_name if self._settings else self._status_identity_name(),
            "sequence": self._sequence,
            "flood_scope": flood_scope,
            "full_flood_enabled": full_flood_enabled,
            "text_broadcast_enabled": text_broadcast_enabled,
            "text_channel": text_channel,
            "public_key_fingerprint": fingerprint,
        }

    def _status_identity_name(self) -> str:
        """Best-effort identity name for status without raising on invalid config."""
        try:
            return self._bot_identity_name()
        except TimeSyncError:
            return ""

    async def start(self) -> None:
        """Validate configuration and start the periodic announcement loop."""
        if not self.enabled:
            self.logger.info("Time sync service is disabled")
            return
        if not getattr(self.bot, "meshcore", None):
            self.logger.error("Time sync cannot start: meshcore not available")
            return

        try:
            self._settings = self._load_settings()
            self._sequence = self._load_configured_sequence()
        except TimeSyncError as exc:
            self.enabled = False
            self.logger.error("Time sync service not started: %s", exc)
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        self.logger.info(
            "Time sync service started: channel=%s identity_name=%r interval=%ss public_key=%s...",
            self._settings.channel,
            self._settings.identity_name,
            self._settings.interval_seconds,
            self._settings.public_key.hex()[:12],
        )

    async def stop(self) -> None:
        """Stop the sender loop and persist the next sequence."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._persist_sequence(force=True)
        self.logger.info("Time sync service stopped")

    async def on_transport_reconnected(self) -> None:
        """Restart the sender loop if the transport reconnects after a drop."""
        if not self.enabled:
            return
        if self._running and self._task and not self._task.done():
            return
        await self.start()

    async def _run_loop(self) -> None:
        """Send immediately, then repeat at the configured interval."""
        assert self._settings is not None
        while self._running:
            try:
                await self.send_once(reload_settings=False)
            except Exception as exc:
                self.logger.error("Time sync send failed: %s", exc)
            await asyncio.sleep(self._settings.interval_seconds)

    async def send_once(self, *, timestamp: int | None = None, reload_settings: bool = True) -> bool:
        """Build, sign, and enqueue one binary time-sync datagram.

        Manual sends reload settings by default so a just-saved full-flood or
        regional-scope change is used immediately. The periodic loop opts out
        because its sleep interval is tied to the settings loaded at startup.
        """
        if reload_settings or self._settings is None:
            self._settings = self._load_settings()
        settings = self._settings

        unix_seconds = int(time.time()) if timestamp is None else int(timestamp)
        if unix_seconds < TIME_SYNC_MIN_TIMESTAMP or unix_seconds > TIME_SYNC_MAX_TIMESTAMP:
            self.logger.warning("Time sync timestamp %s is outside firmware-supported range", unix_seconds)
            return False

        sequence = validate_sequence(self._sequence)

        # The signature scope is the collision-resistant channel identity, not
        # MeshCore's one-byte transport channel hash.
        channel_secret = self._get_channel_secret(settings.channel)
        channel_id = derive_time_sync_channel_id(channel_secret)
        canonical = build_time_sync_canonical(
            channel_id,
            settings.identity_name,
            unix_seconds,
            sequence,
        )

        # The wire payload carries only the display name and raw signature.  The
        # repeater already has the full public key pinned in its configuration.
        signature = await self._sign_with_bot_identity(canonical)
        tv1_payload = build_time_sync_payload(
            settings.identity_name,
            unix_seconds,
            sequence,
            signature,
        )

        sent = await self.bot.command_manager.send_group_datagram(
            settings.channel,
            TIME_SYNC_DATA_TYPE,
            tv1_payload,
            command_id=f"time_sync_{settings.channel}_{unix_seconds}_{sequence}",
            skip_user_rate_limit=True,
            scope=settings.flood_scope,
        )
        if sent:
            if settings.text_broadcast_enabled:
                await self._send_text_broadcast(settings, unix_seconds, sequence)
            self._sequence = next_sequence(sequence)
            self._persist_sequence(force=reload_settings)
        return sent

    @staticmethod
    def _format_text_broadcast(identity_name: str, unix_seconds: int, sequence: int) -> str:
        """Return the optional human-readable companion message for a Tv1 datagram."""
        iso_utc = datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"Time sync: {iso_utc} (unix {unix_seconds}, seq {sequence}, source {identity_name})"

    async def _send_text_broadcast(
        self,
        settings: TimeSyncSettings,
        unix_seconds: int,
        sequence: int,
    ) -> None:
        """Best-effort text companion to the authoritative binary datagram."""
        channel = settings.text_channel or settings.channel
        text = self._format_text_broadcast(settings.identity_name, unix_seconds, sequence)
        try:
            sent = await self.bot.command_manager.send_channel_message(
                channel,
                text,
                command_id=f"time_sync_text_{channel}_{unix_seconds}_{sequence}",
                skip_user_rate_limit=True,
                scope=settings.flood_scope,
                timestamp=datetime.fromtimestamp(unix_seconds, tz=timezone.utc),
            )
        except Exception as exc:
            self.logger.warning("Time sync text broadcast failed for channel %s: %s", channel, exc)
            return

        if not sent:
            self.logger.warning("Time sync text broadcast failed for channel %s", channel)
