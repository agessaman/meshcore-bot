#!/usr/bin/env python3
"""
Reload Command
Allows admin users to reload the bot configuration without restarting
"""

from ..models import MeshMessage
from .base_command import BaseCommand


class ReloadCommand(BaseCommand):
    """Command for reloading bot configuration"""

    # Plugin metadata
    name = "reload"
    honors_skip_channel_check = False
    admin_only = True
    keywords = ["reload", "reloadconfig", "configreload"]
    description = "Reload bot configuration without restart (DM only, admin only)"
    requires_dm = True
    cooldown_seconds = 2
    category = "admin"

    def __init__(self, bot):
        """Initialize the reload command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)

    def get_help_text(self, message: MeshMessage | None = None) -> str:
        """Get help text for the reload command.

        Returns:
            str: The help text for this command.
        """
        return ("Reloads the bot configuration from config.ini without restarting.\n"
                "Note: Radio/connection settings cannot be changed via reload.\n"
                "If radio settings changed, restart the bot instead.\n"
                "Usage: reload")

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the reload command.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if executed successfully, False otherwise.
        """
        # Call the bot's reload_config method
        success, msg = self.bot.reload_config()

        if success:
            await self.send_response(message, f"✓ {msg}")
        else:
            await self.send_response(message, f"✗ {msg}")

        return True
