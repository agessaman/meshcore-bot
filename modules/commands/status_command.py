#!/usr/bin/env python3
"""
Status command for the MeshCore Bot
Admin DM-only command showing live bot health snapshot
"""

import os
import time
from typing import Any

from ..models import MeshMessage
from .base_command import BaseCommand


class StatusCommand(BaseCommand):
    """Admin DM command returning a live health snapshot of the bot."""

    name = "status"
    keywords = ["status"]
    description = "Show live bot status: uptime, radio state, DB size, loaded services."
    requires_dm = True
    category = "admin"

    short_description = "Show live bot health snapshot (admin DM only)"
    usage = "status"
    examples = ["status"]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.enabled = self.get_config_value("Status_Command", "enabled", fallback=True, value_type="bool")

    async def execute(self, message: MeshMessage) -> bool:
        if not self.enabled:
            return False
        try:
            response = self._build_status()
            return await self.send_response(message, response)
        except Exception as e:
            self.logger.error("Error in status command: %s", e)
            return False

    def _build_status(self) -> str:
        lines = []

        # Uptime
        start = getattr(self.bot, "start_time", None)
        if start:
            elapsed = int(time.time() - start)
            d, rem = divmod(elapsed, 86400)
            h, rem = divmod(rem, 3600)
            m = rem // 60
            if d:
                uptime = f"{d}d {h}h {m}m"
            elif h:
                uptime = f"{h}h {m}m"
            else:
                uptime = f"{m}m"
        else:
            uptime = "unknown"
        lines.append(f"Up: {uptime}")

        # Radio state
        zombie = getattr(self.bot, "is_radio_zombie", False)
        if callable(zombie):
            zombie = zombie()
        offline = getattr(self.bot, "is_radio_offline", False)
        if callable(offline):
            offline = offline()
        if zombie:
            lines.append("Radio: ZOMBIE")
        elif offline:
            lines.append("Radio: OFFLINE")
        else:
            lines.append("Radio: ok")

        # DB size
        try:
            db_path = str(self.bot.db_manager.db_path)
            size = os.path.getsize(db_path)
            if size >= 1_048_576:
                db_str = f"{size / 1_048_576:.1f} MB"
            elif size >= 1024:
                db_str = f"{size / 1024:.0f} KB"
            else:
                db_str = f"{size} B"
        except Exception:
            db_str = "unknown"
        lines.append(f"DB: {db_str}")

        # Channels
        try:
            channels = self.bot.command_manager.monitor_channels or []
            lines.append(f"Channels: {len(channels)}")
        except Exception:
            pass

        # Loaded services
        try:
            services = self.bot.services or {}
            if services:
                lines.append(f"Services: {len(services)} ({', '.join(services.keys())})")
            else:
                lines.append("Services: none")
        except Exception:
            pass

        return "\n".join(lines)
