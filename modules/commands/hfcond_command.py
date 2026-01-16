#!/usr/bin/env python3
"""
HF Conditions Command - Provides HF band conditions for ham radio
"""

from .base_command import BaseCommand
from ..solar_conditions import hf_band_conditions
from ..models import MeshMessage


class HfcondCommand(BaseCommand):
    """Command to get HF band conditions.
    
    Retrieves and displays propagation conditions for High Frequency (HF) bands,
    useful for amateur radio operators.
    """
    
    # Plugin metadata
    name = "hfcond"
    keywords = ['hfcond']
    description = "Get HF band conditions for ham radio"
    category = "solar"
    requires_internet = True  # Requires internet access for hamqsl.com API
    
    # Documentation
    short_description = "Get HF band conditions for ham radio"
    usage = "hfcond"
    examples = ["hfcond"]
    
    def __init__(self, bot):
        """Initialize the hfcond command.
        
        Args:
            bot: The MeshCoreBot instance.
        """
        super().__init__(bot)
        self.hfcond_enabled = self.get_config_value('Hfcond_Command', 'enabled', fallback=True, value_type='bool')
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.hfcond_enabled:
            return False
        return super().can_execute(message)
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the hfcond command.
        
        Args:
            message: The message that triggered the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        try:
            # Get HF band conditions
            hf_info = hf_band_conditions()
            
            # Send response using unified method
            response = self.translate('commands.hfcond.header', info=hf_info)
            return await self.send_response(message, response)
            
        except Exception as e:
            error_msg = self.translate('commands.hfcond.error', error=str(e))
            return await self.send_response(message, error_msg)
    
    def get_help_text(self) -> str:
        """Get help text for this command.
        
        Returns:
            str: The help text for this command.
        """
        return self.translate('commands.hfcond.help')
