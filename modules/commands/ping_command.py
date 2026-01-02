#!/usr/bin/env python3
"""
Ping command for the MeshCore Bot
Handles the 'ping' keyword response
"""

from typing import Optional
from .base_command import BaseCommand
from ..models import MeshMessage


class PingCommand(BaseCommand):
    """Handles the ping command.
    
    A simple diagnostic command that responds with 'Pong!' or a custom configured response
    to verify bot connectivity and responsiveness.
    """
    
    # Plugin metadata
    name = "ping"
    keywords = ['ping']
    description = "Responds to 'ping' with 'Pong!'"
    category = "basic"
    
    def get_help_text(self) -> str:
        """Get help text for the ping command.
        
        Returns:
            str: The help text for this command.
        """
        return self.translate('commands.ping.description')
    
    def get_response_format(self) -> Optional[str]:
        """Get the response format from config.
        
        Returns:
            Optional[str]: The format string for the response, or None if not configured.
        """
        if self.bot.config.has_section('Keywords'):
            format_str = self.bot.config.get('Keywords', 'ping', fallback=None)
            return self._strip_quotes_from_config(format_str) if format_str else None
        return None
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the ping command.
        
        Args:
            message: The message that triggered the command.
            
        Returns:
            bool: True if the response was sent successfully, False otherwise.
        """
        return await self.handle_keyword_match(message)
