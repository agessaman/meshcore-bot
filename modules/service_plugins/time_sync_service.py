#!/usr/bin/env python3
"""Authenticated binary time-sync source service."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
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
    display_name: str
    public_key: bytes
    interval_seconds: int


class TimeSyncService(BaseServicePlugin):
    """Periodically emits signed binary ``Tv1`` group datagrams."""

    config_section = "Time_Sync"
    name = "timesync"
    description = "Authenticated binary MeshCore repeater time-sync source"

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
            raise TimeSyncError(f"channel {channel!r} was not found in the MeshCore channel cache")

        display_name = self.bot.config.get(self.config_section, "display_name", fallback="")
        validate_display_name(display_name)

        interval_seconds = self.bot.config.getint(self.config_section, "interval_seconds", fallback=604800)
        if interval_seconds <= 0:
            raise TimeSyncError("interval_seconds must be greater than zero")

        public_key = self._bot_public_key()

        return TimeSyncSettings(
            channel=channel,
            display_name=display_name,
            public_key=public_key,
            interval_seconds=interval_seconds,
        )

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
        return {
            "enabled": self.enabled,
            "running": self._running,
            "channel": self._settings.channel if self._settings else self.bot.config.get(self.config_section, "channel", fallback=""),
            "display_name": self._settings.display_name if self._settings else self.bot.config.get(self.config_section, "display_name", fallback=""),
            "sequence": self._sequence,
            "public_key_fingerprint": fingerprint,
        }

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
            "Time sync service started: channel=%s display_name=%r interval=%ss public_key=%s...",
            self._settings.channel,
            self._settings.display_name,
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
                await self.send_once()
            except Exception as exc:
                self.logger.error("Time sync send failed: %s", exc)
            await asyncio.sleep(self._settings.interval_seconds)

    async def send_once(self, *, timestamp: int | None = None) -> bool:
        """Build, sign, and enqueue one binary time-sync datagram."""
        if self._settings is None:
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
            settings.display_name,
            unix_seconds,
            sequence,
        )

        # The wire payload carries only the display name and raw signature.  The
        # repeater already has the full public key pinned in its configuration.
        signature = await self._sign_with_bot_identity(canonical)
        tv1_payload = build_time_sync_payload(
            settings.display_name,
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
        )
        if sent:
            self._sequence = next_sequence(sequence)
            self._persist_sequence()
        return sent
