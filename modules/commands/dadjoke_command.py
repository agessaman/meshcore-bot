#!/usr/bin/env python3
"""
Dad Joke Command for MeshCore Bot
Fetches dad jokes from icanhazdadjoke.com API
"""

import asyncio
import aiohttp
import logging
from typing import Optional, Dict, Any
from .base_command import BaseCommand
from ..models import MeshMessage

logger = logging.getLogger("MeshCoreBot")

class DadJokeCommand(BaseCommand):
    """Handles dad joke commands using icanhazdadjoke.com API"""
    
    # Plugin metadata
    name = "dadjoke"
    keywords = ['dadjoke', 'dad joke', 'dadjokes', 'dad jokes']
    description = "Get a random dad joke from icanhazdadjoke.com"
    category = "fun"
    cooldown_seconds = 3
    
    # API configuration
    DAD_JOKE_API_URL = "https://icanhazdadjoke.com/"
    TIMEOUT = 10  # seconds
    
    def __init__(self, bot):
        super().__init__(bot)
        
        # Per-user cooldown tracking
        self.user_cooldowns = {}  # user_id -> last_execution_time
        
        # Load configuration
        self.dadjoke_enabled = bot.config.getboolean('Jokes', 'dadjoke_enabled', fallback=True)
        self.long_jokes = bot.config.getboolean('Jokes', 'long_jokes', fallback=False)
    
    def get_help_text(self) -> str:
        return "Usage: dadjoke - Get a random dad joke"
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message starts with a dad joke keyword"""
        content = message.content.strip()
        if content.startswith('!'):
            content = content[1:].strip()
        content_lower = content.lower()
        for keyword in self.keywords:
            # Match if keyword is at start followed by space or end of message
            if content_lower == keyword or content_lower.startswith(keyword + ' '):
                return True
        return False
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Override cooldown check to be per-user instead of per-command-instance"""
        # Check if dadjoke command is enabled
        if not self.dadjoke_enabled:
            return False
        
        # Check if command requires DM and message is not DM
        if self.requires_dm and not message.is_dm:
            return False
        
        # Check per-user cooldown
        if self.cooldown_seconds > 0:
            import time
            current_time = time.time()
            user_id = message.sender_id
            
            if user_id in self.user_cooldowns:
                last_execution = self.user_cooldowns[user_id]
                if (current_time - last_execution) < self.cooldown_seconds:
                    return False
        
        return True
    
    def get_remaining_cooldown(self, user_id: str) -> int:
        """Get remaining cooldown time for a specific user"""
        if self.cooldown_seconds <= 0:
            return 0
        
        import time
        current_time = time.time()
        if user_id in self.user_cooldowns:
            last_execution = self.user_cooldowns[user_id]
            elapsed = current_time - last_execution
            if elapsed < self.cooldown_seconds:
                remaining = self.cooldown_seconds - elapsed
                return max(0, int(remaining))
        
        return 0
    
    def _record_execution(self, user_id: str):
        """Record the execution time for a specific user"""
        import time
        self.user_cooldowns[user_id] = time.time()
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the dad joke command"""
        try:
            # Record execution for this user
            self._record_execution(message.sender_id)
            
            # Get dad joke from API with length handling
            joke_data = await self.get_dad_joke_with_length_handling()
            
            if joke_data is None:
                await self.send_response(message, "Sorry, couldn't fetch a dad joke right now. Try again later!")
                return True
            
            # Format and send the joke(s)
            await self.send_dad_joke_with_length_handling(message, joke_data)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error in dad joke command: {e}")
            await self.send_response(message, "Sorry, something went wrong getting a dad joke!")
            return True
    
    async def get_dad_joke_from_api(self) -> Optional[Dict[str, Any]]:
        """Get a dad joke from icanhazdadjoke.com API"""
        try:
            headers = {
                'Accept': 'application/json',
                'User-Agent': 'MeshCoreBot (https://github.com/adam/meshcore-bot)'
            }
            
            self.logger.debug(f"Fetching dad joke from: {self.DAD_JOKE_API_URL}")
            
            # Make the API request
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.DAD_JOKE_API_URL, 
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.TIMEOUT)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        # Check if the API returned an error
                        if data.get('status') != 200:
                            self.logger.warning(f"Dad joke API returned error status: {data.get('status')}")
                            return None
                        
                        # Validate required fields
                        if not data.get('joke'):
                            self.logger.warning("Dad joke API returned joke without content")
                            return None
                        
                        return data
                    else:
                        self.logger.error(f"Dad joke API returned status {response.status}")
                        return None
                        
        except asyncio.TimeoutError:
            self.logger.error("Timeout fetching dad joke from API")
            return None
        except Exception as e:
            self.logger.error(f"Error fetching dad joke from API: {e}")
            return None
    
    async def get_dad_joke_with_length_handling(self) -> Optional[Dict[str, Any]]:
        """Get a dad joke from API with length handling based on configuration"""
        max_attempts = 5  # Prevent infinite loops
        
        for attempt in range(max_attempts):
            joke_data = await self.get_dad_joke_from_api()
            
            if joke_data is None:
                return None
            
            # Check joke length
            joke_text = self.format_dad_joke(joke_data)
            
            if len(joke_text) <= 130:
                # Joke is short enough, return it
                return joke_data
            elif self.long_jokes:
                # Long jokes are enabled, return it for splitting
                return joke_data
            else:
                # Long jokes are disabled, try again
                self.logger.debug(f"Dad joke too long ({len(joke_text)} chars), fetching another...")
                continue
        
        # If we've tried max_attempts times and still getting long jokes, return the last one
        self.logger.warning(f"Could not get short dad joke after {max_attempts} attempts")
        return joke_data
    
    async def send_dad_joke_with_length_handling(self, message: MeshMessage, joke_data: Dict[str, Any]):
        """Send dad joke with length handling - split if necessary"""
        joke_text = self.format_dad_joke(joke_data)
        
        if len(joke_text) <= 130:
            # Joke is short enough, send as single message
            await self.send_response(message, joke_text)
        else:
            # Joke is too long, split it
            parts = self.split_dad_joke(joke_text)
            
            if len(parts) == 2 and len(parts[0]) <= 130 and len(parts[1]) <= 130:
                # Can be split into two messages
                await self.send_response(message, parts[0])
                # Use conservative delay to avoid rate limiting (same as weather command)
                await asyncio.sleep(2.0)
                await self.send_response(message, parts[1])
            else:
                # Cannot be split properly, send as single message (user will see truncation)
                await self.send_response(message, joke_text)
    
    def split_dad_joke(self, joke_text: str) -> list:
        """Split a long dad joke at a logical point"""
        # Remove emoji for splitting
        clean_joke = joke_text[2:] if joke_text.startswith('🥸 ') else joke_text
        
        # Try to split at common logical points
        split_points = [
            '. ',     # Period followed by space
            '? ',     # Question mark followed by space
            '! ',     # Exclamation mark followed by space
            ', ',     # Comma followed by space
        ]
        
        for split_point in split_points:
            if split_point in clean_joke:
                parts = clean_joke.split(split_point, 1)
                if len(parts) == 2:
                    # Add emoji back to both parts
                    return [f"🥸 {parts[0]}{split_point}", f"🥸 {parts[1]}"]
        
        # If no good split point found, split at middle
        mid_point = len(clean_joke) // 2
        # Find nearest space to avoid splitting words
        for i in range(mid_point, len(clean_joke)):
            if clean_joke[i] == ' ':
                mid_point = i
                break
        
        part1 = clean_joke[:mid_point]
        part2 = clean_joke[mid_point + 1:]
        
        return [f"🥸 {part1}", f"🥸 {part2}"]
    
    def format_dad_joke(self, joke_data: Dict[str, Any]) -> str:
        """Format the dad joke data into a readable string"""
        try:
            joke = joke_data.get('joke', '')
            
            if joke:
                return f"🥸 {joke}"
            else:
                return "🥸 No dad joke content available"
                    
        except Exception as e:
            self.logger.error(f"Error formatting dad joke: {e}")
            return "🥸 Sorry, couldn't format the dad joke properly!"
