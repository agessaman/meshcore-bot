#!/usr/bin/env python3
"""
Plugins command for the MeshCore Bot
Lists all active service plugins and their descriptions
"""

from typing import Any

from ..models import MeshMessage
from .base_command import BaseCommand


class PluginsCommand(BaseCommand):
    """Lists loaded service plugins with their descriptions."""

    # Plugin metadata
    name = "plugins"
    keywords = ["plugins"]
    description = "List active service plugins and their descriptions."
    category = "admin"

    # Documentation
    short_description = "List loaded service plugins"
    usage = "plugins"
    examples = ["plugins"]

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)
        self.plugins_enabled = self.get_config_value("Plugins_Command", "enabled", fallback=True, value_type="bool")

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.plugins_enabled:
            return False
        return super().can_execute(message, skip_channel_check=skip_channel_check)

    async def execute(self, message: MeshMessage) -> bool:
        try:
            response = self._build_list()
            return await self.send_response(message, response)
        except Exception as e:
            self.logger.error("Error in plugins command: %s", e)
            return False

    def _build_list(self) -> str:
        try:
            services = self.bot.services or {}
        except Exception:
            services = {}

        if not services:
            return "No service plugins loaded."

        lines = [f"Plugins ({len(services)}):"]
        for name, plugin in sorted(services.items()):
            desc = getattr(plugin, "description", "") or ""
            if desc:
                desc = desc[:60] + ("..." if len(desc) > 60 else "")
                lines.append(f"  {name}: {desc}")
            else:
                lines.append(f"  {name}")

        return "\n".join(lines)
