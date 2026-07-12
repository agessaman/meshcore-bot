#!/usr/bin/env python3
"""Admin command for authenticated time-sync source operations."""

from __future__ import annotations

from ..models import MeshMessage
from ..service_plugins.time_sync_service import TimeSyncService
from ..time_sync import TimeSyncError
from .base_command import BaseCommand


class TimeSyncCommand(BaseCommand):
    """Expose local/admin-only time-sync status and public key export."""

    name = "timesync"
    keywords = ["timesync", "time-sync", "time_sync"]
    description = "Show authenticated time-sync source status and public key (DM only, admin only)"
    short_description = "Manage authenticated time-sync source"
    usage = "timesync <status|publickey>"
    examples = ["timesync status", "timesync publickey"]
    requires_dm = True
    cooldown_seconds = 2
    category = "admin"

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Require this command to be explicitly listed in the admin command allow-list."""
        if not super().requires_admin_access():
            return False
        return super().can_execute(message, skip_channel_check=skip_channel_check)

    def get_help_text(self) -> str:
        """Return usage text for command help."""
        return (
            "Usage: timesync <status|publickey>\n"
            "status: show redacted time-sync source state.\n"
            "publickey: show the full 32-byte Ed25519 public key as 64 lowercase hex characters."
        )

    def _get_service(self) -> TimeSyncService:
        """Return loaded service or a temporary config reader when disabled."""
        service = getattr(self.bot, "services", {}).get("timesync")
        if isinstance(service, TimeSyncService):
            return service
        return TimeSyncService(self.bot)

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the requested time-sync subcommand."""
        content = self.cleanup_message_for_matching(message)
        parts = content.split()
        subcommand = parts[1].lower() if len(parts) > 1 else "status"

        if subcommand in ("status", "st"):
            await self._handle_status(message)
        elif subcommand in ("publickey", "pubkey", "key"):
            await self._handle_public_key(message)
        else:
            await self.send_response(message, "Usage: timesync <status|publickey>")
        return True

    async def _handle_status(self, message: MeshMessage) -> None:
        """Send a redacted status response."""
        service = self._get_service()
        status = service.status()
        fingerprint = status.get("public_key_fingerprint") or "unavailable"
        text = (
            "Time Sync Status:\n"
            f"enabled: {status.get('enabled')}\n"
            f"running: {status.get('running')}\n"
            f"channel: {status.get('channel') or 'not configured'}\n"
            f"display_name: {status.get('display_name') or 'not configured'}\n"
            f"sequence: {status.get('sequence')}\n"
            f"public_key_fingerprint: {fingerprint}"
        )
        await self.send_response(message, text)

    async def _handle_public_key(self, message: MeshMessage) -> None:
        """Send the full Ed25519 public key for repeater pinning."""
        try:
            public_key = self._get_service().public_key_hex()
        except TimeSyncError as exc:
            await self.send_response(message, f"Time sync public key unavailable: {exc}")
            return
        await self.send_response(message, public_key)
