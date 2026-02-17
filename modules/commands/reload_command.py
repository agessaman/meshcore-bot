#!/usr/bin/env python3
"""
Reload Command
Allows admin users to reload the bot configuration without restarting
"""

from .base_command import BaseCommand
from ..models import MeshMessage


class ReloadCommand(BaseCommand):
    """Command for reloading bot configuration"""
    
    # Plugin metadata
    name = "reload"
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
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed (admin only)"""
        if not self.requires_admin_access():
            return False
        return super().can_execute(message)
    
    def requires_admin_access(self) -> bool:
        """Reload command requires admin access"""
        return True
    
    def get_help_text(self) -> str:
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
