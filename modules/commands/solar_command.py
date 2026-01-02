#!/usr/bin/env python3
"""
Solar Command - Provides solar conditions and HF band information
"""

from .base_command import BaseCommand
from ..solar_conditions import solar_conditions, hf_band_conditions
from ..models import MeshMessage


class SolarCommand(BaseCommand):
    """Command to get solar conditions.
    
    Provides information about current solar activity (SFI, sunspots, A-index, K-index)
    and improved HF band conditions.
    """
    
    # Plugin metadata
    name = "solar"
    keywords = ['solar']
    description = "Get current solar conditions and HF band info"
    category = "solar"
    requires_internet = True  # Requires internet access for hamqsl.com API
    
    def __init__(self, bot):
        """Initialize the solar command.
        
        Args:
            bot: The MeshCoreBot instance.
        """
        super().__init__(bot)
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the solar command.
        
        Retrieves solar conditions and sends a formatted response to the user.
        
        Args:
            message: The message that triggered the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        try:
            # Get solar conditions (more readable format)
            solar_info = solar_conditions()
            
            # Send response (solar only, more readable)
            response = self.translate('commands.solar.response', info=solar_info)
            
            # Use the unified send_response method
            return await self.send_response(message, response)
            
            
        except Exception as e:
            error_msg = self.translate('commands.solar.error', error=str(e))
            await self.send_response(message, error_msg)
            return False

    def get_help_text(self) -> str:
        """Get help text for this command.
        
        Returns:
            str: The help text for this command.
        """
        return self.translate('commands.solar.help')
