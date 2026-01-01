#!/usr/bin/env python3
"""
Sun Command - Provides sunrise/sunset information
"""

from .base_command import BaseCommand
from ..solar_conditions import get_sun
from ..models import MeshMessage


class SunCommand(BaseCommand):
    """Command to get sun information"""
    
    # Plugin metadata
    name = "sun"
    keywords = ['sun']
    description = "Get sunrise/sunset times"
    category = "solar"
    
    def __init__(self, bot):
        super().__init__(bot)
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the sun command"""
        try:
            # Get sun information using default location
            sun_info = get_sun()
            
            # Send response using unified method
            response = self.translate('commands.sun.response', info=sun_info)
            return await self.send_response(message, response)
            
        except Exception as e:
            error_msg = self.translate('commands.sun.error', error=str(e))
            return await self.send_response(message, error_msg)
    
    def get_help_text(self):
        """Get help text for this command"""
        return self.translate('commands.sun.help')
