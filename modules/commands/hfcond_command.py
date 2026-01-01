#!/usr/bin/env python3
"""
HF Conditions Command - Provides HF band conditions for ham radio
"""

from .base_command import BaseCommand
from ..solar_conditions import hf_band_conditions
from ..models import MeshMessage


class HfcondCommand(BaseCommand):
    """Command to get HF band conditions"""
    
    # Plugin metadata
    name = "hfcond"
    keywords = ['hfcond']
    description = "Get HF band conditions for ham radio"
    category = "solar"
    
    def __init__(self, bot):
        super().__init__(bot)
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the hfcond command"""
        try:
            # Get HF band conditions
            hf_info = hf_band_conditions()
            
            # Send response using unified method
            response = self.translate('commands.hfcond.header', info=hf_info)
            return await self.send_response(message, response)
            
        except Exception as e:
            error_msg = self.translate('commands.hfcond.error', error=str(e))
            return await self.send_response(message, error_msg)
    
    def get_help_text(self):
        """Get help text for this command"""
        return self.translate('commands.hfcond.help')
