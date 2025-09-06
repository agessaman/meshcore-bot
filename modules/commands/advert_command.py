#!/usr/bin/env python3
"""
Advert command for the MeshCore Bot
Handles the 'advert' command for sending flood adverts
"""

import time
from .base_command import BaseCommand
from ..models import MeshMessage


class AdvertCommand(BaseCommand):
    """Handles the advert command"""
    
    # Plugin metadata
    name = "advert"
    keywords = ['advert']
    description = "Sends flood advert (DM only, 1hr cooldown)"
    requires_dm = True
    cooldown_seconds = 3600  # 1 hour
    category = "special"
    
    def get_help_text(self) -> str:
        return self.description
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if advert command can be executed"""
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
        """Execute the advert command"""
        try:
            # Check if enough time has passed since last advert (1 hour)
            current_time = time.time()
            if hasattr(self.bot, 'last_advert_time') and self.bot.last_advert_time and (current_time - self.bot.last_advert_time) < 3600:
                remaining_time = 3600 - (current_time - self.bot.last_advert_time)
                remaining_minutes = int(remaining_time // 60)
                response = f"Advert cooldown active. Please wait {remaining_minutes} more minutes before requesting another advert."
                await self.send_response(message, response)
                return True
            
            self.logger.info(f"User {message.sender_id} requested flood advert")
            
            # Send flood advert using meshcore.commands
            await self.bot.meshcore.commands.send_advert(flood=True)
            
            # Update last advert time
            if hasattr(self.bot, 'last_advert_time'):
                self.bot.last_advert_time = current_time
            
            response = "Flood advert sent successfully!"
            self.logger.info("Flood advert sent successfully via DM command")
            
            await self.send_response(message, response)
            return True
            
        except Exception as e:
            error_msg = f"Error sending flood advert: {e}"
            self.logger.error(error_msg)
            await self.send_response(message, error_msg)
            return False
