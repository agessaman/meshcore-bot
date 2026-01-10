#!/usr/bin/env python3
"""
Advert command for the MeshCore Bot
Handles the 'advert' command for sending flood adverts
"""

import time
from typing import Any
from .base_command import BaseCommand
from ..models import MeshMessage


class AdvertCommand(BaseCommand):
    """Handles the advert command.
    
    This command allows users to manually trigger a flood advertisement
    to help propagate their node information across the mesh network.
    It enforces a strict cooldown to prevent network congestion.
    """
    
    # Plugin metadata
    name = "advert"
    keywords = ['advert']
    description = "Sends flood advert (DM only, 1hr cooldown)"
    requires_dm = True
    cooldown_seconds = 3600  # 1 hour
    category = "special"
    
    def __init__(self, bot: Any):
        """Initialize the advert command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.advert_enabled = self.get_config_value('Advert_Command', 'enabled', fallback=True, value_type='bool')
        
    def get_help_text(self) -> str:
        """Get help text for the advert command.
        
        Returns:
            str: The help text for this command.
        """
        return self.translate('commands.advert.description')
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if advert command can be executed.
        
        Verifies both the standard command cooldowns and checks against the
        bot's global last advertisement time.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if the command can be executed, False otherwise.
        """
        # Check if advert command is enabled
        if not self.advert_enabled:
            return False
        
        # Use the base class cooldown check
        if not super().can_execute(message):
            return False
        
        # Additional check for bot's last advert time (legacy support)
        if hasattr(self.bot, 'last_advert_time') and self.bot.last_advert_time:
            current_time = time.time()
            if (current_time - self.bot.last_advert_time) < 3600:  # 1 hour
                return False
        
        return True
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the advert command.
        
        Sends a flood advertisement if the cooldown has passed. If on cooldown,
        informs the user of the remaining time.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if executed successfully (including cooldown notice), False otherwise.
        """
        try:
            # Check if enough time has passed since last advert (1 hour)
            current_time = time.time()
            if hasattr(self.bot, 'last_advert_time') and self.bot.last_advert_time and (current_time - self.bot.last_advert_time) < 3600:
                remaining_time = 3600 - (current_time - self.bot.last_advert_time)
                remaining_minutes = int(remaining_time // 60)
                response = self.translate('commands.advert.cooldown_active', minutes=remaining_minutes)
                await self.send_response(message, response)
                return True
            
            self.logger.info(f"User {message.sender_id} requested flood advert")
            
            # Send flood advert using meshcore.commands
            await self.bot.meshcore.commands.send_advert(flood=True)
            
            # Update last advert time
            if hasattr(self.bot, 'last_advert_time'):
                self.bot.last_advert_time = current_time
            
            response = self.translate('commands.advert.success')
            self.logger.info("Flood advert sent successfully via DM command")
            
            await self.send_response(message, response)
            return True
            
        except Exception as e:
            error_msg = self.translate('commands.advert.error', error=str(e))
            self.logger.error(f"Error sending flood advert: {e}")
            await self.send_response(message, error_msg)
            return False
