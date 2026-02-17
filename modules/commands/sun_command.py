#!/usr/bin/env python3
"""
Sun Command - Provides sunrise/sunset information
"""

from .base_command import BaseCommand
from ..solar_conditions import get_sun
from ..models import MeshMessage


class SunCommand(BaseCommand):
    """Command to get sun information.
    
    Calculates and displays sunrise and sunset times for the bot's configured location
    or a default location.
    """
    
    # Plugin metadata
    name = "sun"
    keywords = ['sun']
    description = "Get sunrise/sunset times"
    category = "solar"
    
    # Documentation
    short_description = "Get sunrise and sunset times"
    usage = "sun"
    examples = ["sun"]
    
    def __init__(self, bot):
        """Initialize the sun command.
        
        Args:
            bot: The MeshCoreBot instance.
        """
        super().__init__(bot)
        self.sun_enabled = self.get_config_value('Sun_Command', 'enabled', fallback=True, value_type='bool')
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.sun_enabled:
            return False
        return super().can_execute(message)
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the sun command.
        
        Calculates sun events and sends the information to the user.
        
        Args:
            message: The message that triggered the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        try:
            # Get sun information using default location
            sun_info = get_sun()
            
            # Send response using unified method
            response = self.translate('commands.sun.response', info=sun_info)
            return await self.send_response(message, response)
            
        except Exception as e:
            error_msg = self.translate('commands.sun.error', error=str(e))
            return await self.send_response(message, error_msg)
    
    def get_help_text(self) -> str:
        """Get help text for this command.
        
        Returns:
            str: The help text for this command.
        """
        return self.translate('commands.sun.help')
