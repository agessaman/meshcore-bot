#!/usr/bin/env python3
"""
Satellite Pass Command - Provides satellite pass information
"""

from .base_command import BaseCommand
from ..solar_conditions import get_next_satellite_pass
from ..models import MeshMessage


class SatpassCommand(BaseCommand):
    """Command to get satellite pass information"""
    
    # Plugin metadata
    name = "satpass"
    keywords = ['satpass']
    description = "Get satellite pass info: satpass <NORAD_number_or_shortcut> [visual]"
    category = "solar"
    requires_internet = True  # Requires internet access for N2YO API
    
    # Documentation
    short_description = "Get satellite pass predictions"
    usage = "satpass <NORAD_number|shortcut> [visual]"
    examples = ["satpass iss", "satpass 25544 visual"]
    parameters = [
        {"name": "satellite", "description": "NORAD ID or shortcut (iss, hst, starlink)"},
        {"name": "visual", "description": "Add 'visual' for visible passes only"}
    ]
    
    # Common satellite shortcuts
    SATELLITE_SHORTCUTS = {
    'iss': '25544',
    'hst': '20580',  # Hubble Space Telescope
    'hubble': '20580',
    'starlink': '44294',  # Example Starlink satellite
    'tiangong': '48274',  # Tiangong space station
    'goes18': '51850',  # GOES-18 weather satellite
    }
    
    def __init__(self, bot):
        """Initialize the satpass command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.satpass_enabled = self.get_config_value('Satpass_Command', 'enabled', fallback=True, value_type='bool')
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.satpass_enabled:
            return False
        return super().can_execute(message)
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the satpass command.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        try:
            # Check if user provided a satellite number
            content = message.content.strip()
            if content == 'satpass':
                # No satellite specified, show short usage (fits message length limit)
                await self.send_response(message, self.translate('commands.satpass.usage_short'))
                return True
            
            # Extract satellite identifier from command
            parts = content.split()
            if len(parts) < 2:
                error_msg = self.translate('commands.satpass.no_satellite')
                await self.send_response(message, error_msg)
                return True
            
            satellite_input = parts[1].lower()
            
            # Check for "visual" or "vis" option
            use_visual = False
            if len(parts) >= 3:
                option = parts[2].lower()
                if option in ['visual', 'vis']:
                    use_visual = True
            
            # Check if it's a shortcut first
            if satellite_input in self.SATELLITE_SHORTCUTS:
                satellite = self.SATELLITE_SHORTCUTS[satellite_input]
            else:
                # Assume it's a NORAD number
                satellite = satellite_input
            
            # Get satellite pass information
            pass_info = get_next_satellite_pass(satellite, use_visual=use_visual)
            
            # Send response
            response = self.translate('commands.satpass.header', pass_info=pass_info)
            await self.send_response(message, response)
            return True
            
        except Exception as e:
            error_msg = self.translate('commands.satpass.error', error=str(e))
            await self.send_response(message, error_msg)
            return False
    
    def get_help_text(self) -> str:
        """Get help text for this command.
        
        Returns:
            str: The help text for this command.
        """
        return self.translate('commands.satpass.description')
