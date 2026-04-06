#!/usr/bin/env python3
"""
Version command for the MeshCore Bot.
Returns the currently running bot version string.
"""

from typing import Any

from ..models import MeshMessage
from ..version_info import resolve_runtime_version
from .base_command import BaseCommand


class VersionCommand(BaseCommand):
    """Handles the version/ver command."""

    name = "version"
    keywords = ["version", "ver"]
    description = "Show the running bot version."
    category = "basic"

    short_description = "Show running bot version"
    usage = "version"
    examples = ["version", "ver"]

    def __init__(self, bot: Any):
        super().__init__(bot)
        self.version_enabled = self.get_config_value(
            "Version_Command",
            "enabled",
            fallback=True,
            value_type="bool",
        )

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        if not self.version_enabled:
            return False
        return super().can_execute(message, skip_channel_check=skip_channel_check)

    def get_help_text(self) -> str:
        return self.description

    async def execute(self, message: MeshMessage) -> bool:
        version_value = getattr(self.bot, "bot_version", None)
        if not version_value:
            bot_root = getattr(self.bot, "bot_root", ".")
            version_value = resolve_runtime_version(bot_root).get("display", "unknown")
        sender = message.sender_id or "Unknown"
        response = f"@[{sender}] Bot version: {version_value}"
        return await self.send_response(message, response)

