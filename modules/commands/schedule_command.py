#!/usr/bin/env python3
"""
Schedule command for the MeshCore Bot
Lists upcoming scheduled messages and interval advertising settings.
"""

from typing import Any, Optional

from ..models import MeshMessage
from .base_command import BaseCommand


class ScheduleCommand(BaseCommand):
    """Show scheduled messages and advertising interval configured for the bot.

    Responds to ``schedule`` or ``schedule list``.  The output is kept compact
    so it fits within typical MeshCore message limits (~200 chars).

    Config section ``[Schedule_Command]``:
        enabled       = true        # enable/disable this command
        dm_only       = true        # restrict to DMs (default true — exposes config)
    """

    name = "schedule"
    keywords = ["schedule"]
    description = "List upcoming scheduled messages and advertising interval."
    category = "admin"

    short_description = "Show scheduled messages and advertising interval"
    usage = "schedule [list]"
    examples = ["schedule", "schedule list"]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self._enabled = self.get_config_value(
            "Schedule_Command", "enabled", fallback=True, value_type="bool"
        )
        self._dm_only = self.get_config_value(
            "Schedule_Command", "dm_only", fallback=True, value_type="bool"
        )

    # ------------------------------------------------------------------
    # BaseCommand interface
    # ------------------------------------------------------------------

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self._enabled:
            return False
        if self._dm_only and not message.is_dm:
            return False
        return super().can_execute(message)

    def get_help_text(self) -> str:
        return "schedule [list] — show scheduled messages and advert interval"

    async def execute(self, message: MeshMessage) -> bool:
        response = self._build_response()
        return await self.send_response(message, response)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_response(self) -> str:
        lines: list[str] = []

        # --- Scheduled messages ---
        scheduled = self._get_scheduled_messages()
        if scheduled:
            lines.append(f"Scheduled ({len(scheduled)}):")
            for time_str, channel, preview in scheduled:
                # Format HHMM → HH:MM
                hhmm = f"{time_str[:2]}:{time_str[2:]}"
                lines.append(f"  {hhmm} #{channel}: {preview}")
        else:
            lines.append("No scheduled messages configured.")

        # --- Interval advertising ---
        advert_info = self._get_advert_info()
        if advert_info:
            lines.append(advert_info)

        return "\n".join(lines)

    def _get_scheduled_messages(self) -> list[tuple]:
        """Return sorted list of (time_str, channel, preview) tuples."""
        scheduler = getattr(self.bot, "scheduler", None)
        if scheduler is None:
            return []

        scheduled = getattr(scheduler, "scheduled_messages", {})
        results = []
        for time_str, payload in sorted(scheduled.items()):
            channel, message = payload
            # Truncate long messages so response stays compact
            # Strip control characters that could corrupt the response
            safe_message = ''.join(c if c.isprintable() or c == ' ' else '?' for c in message)
            preview = safe_message if len(safe_message) <= 40 else safe_message[:37] + "..."
            results.append((time_str, channel, preview))
        return results

    def _get_advert_info(self) -> Optional[str]:
        """Return a one-line advert interval summary, or None if disabled."""
        try:
            interval_hours = self.bot.config.getint(
                "Bot", "advert_interval_hours", fallback=0
            )
            if interval_hours > 0:
                return f"Advert interval: every {interval_hours}h"
        except Exception:
            pass
        return None
