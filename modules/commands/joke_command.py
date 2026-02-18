#!/usr/bin/env python3
"""
Joke command for the MeshCore Bot
Provides clean, family-friendly jokes from the JokeAPI
"""

import aiohttp
import asyncio
import logging
from typing import Optional, Dict, Any, List
from .base_command import BaseCommand
from ..models import MeshMessage


class JokeCommand(BaseCommand):
    """Handles joke commands with category support"""
    
    # Plugin metadata
    name = "joke"
    keywords = ['joke', 'jokes']
    description = "Get a random joke or joke from specific category (usage: joke [category])"
    category = "entertainment"
    cooldown_seconds = 3  # 3 second cooldown per user to prevent API abuse
    requires_dm = False  # Works in both channels and DMs
    requires_internet = True  # Requires internet access for API calls
    
    # Documentation
    short_description = "Get a random joke"
    usage = "joke [category]"
    examples = ["joke", "joke programming"]
    parameters = [
        {"name": "category", "description": "programming, pun, misc, dark (optional)"}
    ]
    
    # Supported categories
    SUPPORTED_CATEGORIES = {
        'programming': 'Programming',
        'misc': 'Miscellaneous', 
        'miscellaneous': 'Miscellaneous',
        'dark': 'Dark',
        'pun': 'Pun',
        'spooky': 'Spooky',
        'christmas': 'Christmas'
    }
    
    # API configuration
    JOKE_API_BASE = "https://v2.jokeapi.dev/joke"
    BLACKLIST_FLAGS = "nsfw,religious,political,racist,sexist,explicit"
    TIMEOUT = 10  # seconds
    
    def __init__(self, bot: Any):
        """Initialize the joke command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        
        # Load configuration (enabled standard; joke_enabled legacy from [Joke_Command] or [Jokes])
        self.joke_enabled = self.get_config_value('Joke_Command', 'enabled', fallback=None, value_type='bool')
        if self.joke_enabled is None:
            self.joke_enabled = self.get_config_value('Joke_Command', 'joke_enabled', fallback=True, value_type='bool')
        self.seasonal_jokes = self.get_config_value('Joke_Command', 'seasonal_jokes', fallback=True, value_type='bool')
        self.long_jokes = self.get_config_value('Joke_Command', 'long_jokes', fallback=False, value_type='bool')
    
    def get_help_text(self, message: MeshMessage = None) -> str:
        """Get help text, excluding dark category if not in DM"""
        if message and not message.is_dm:
            # In public channel, exclude dark category
            categories = [cat for cat in self.SUPPORTED_CATEGORIES.keys() if cat != 'dark']
            categories_str = ", ".join(categories)
            return f"Usage: joke [category] - Get a random joke or from categories: {categories_str}"
        else:
            # In DM or no message context, show all categories
            categories = ", ".join(self.SUPPORTED_CATEGORIES.keys())
            return f"Usage: joke [category] - Get a random joke or from categories: {categories}"
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message starts with a joke keyword.
        
        Args:
            message: The message to check.
            
        Returns:
            bool: True if a joke keyword matches, False otherwise.
        """
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
        """Override to add custom checks (joke_enabled, dark joke) while using base class cooldown"""
        # Use base class for channel access, DM requirements, and cooldown
        if not super().can_execute(message):
            return False
        
        # Check if joke command is enabled
        if not self.joke_enabled:
            return False
        
        # Check if this is a dark joke request - require DM
        if self.is_dark_joke_request(message) and not message.is_dm:
            return False
        
        return True
    
    def is_dark_joke_request(self, message: MeshMessage) -> bool:
        """Check if the message is requesting a dark joke"""
        content = message.content.strip()
        if content.startswith('!'):
            content = content[1:].strip()
        
        # Parse the command to extract category
        parts = content.split()
        if len(parts) >= 2:
            category_input = parts[1].lower()
            return category_input == 'dark'
        
        return False
    
    def get_seasonal_default(self) -> str:
        """Get the seasonal default category based on current month"""
        if not self.seasonal_jokes:
            return None
        
        try:
            from datetime import datetime
            current_month = datetime.now().month
            
            if current_month == 10:  # October
                return "Spooky"
            elif current_month == 12:  # December
                return "Christmas"
            else:
                return None  # No seasonal default for other months
                
        except Exception as e:
            self.logger.error(f"Error getting seasonal default: {e}")
            return None
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the joke command.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        content = message.content.strip()
        
        # Parse the command to extract category
        parts = content.split()
        if len(parts) < 2:
            # No category specified, check for seasonal defaults
            category = self.get_seasonal_default()
        else:
            # Category specified
            category_input = parts[1].lower()
            category = self.SUPPORTED_CATEGORIES.get(category_input)
            
            if category is None:
                # Invalid category
                categories = ", ".join(self.SUPPORTED_CATEGORIES.keys())
                await self.send_response(message, f"Invalid category. Available categories: {categories}")
                return True
        
        try:
            # Record execution for this user
            self.record_execution(message.sender_id)
            
            # Get joke from API with length handling
            joke_data = await self.get_joke_with_length_handling(category)
            
            if joke_data is None:
                if category and category.lower() in ['dark']:
                    await self.send_response(message, f"Sorry, no {category.lower()} jokes are available right now. Try again later!")
                else:
                    await self.send_response(message, "Sorry, couldn't fetch a joke right now. Try again later!")
                return True
            
            # Format and send the joke(s)
            await self.send_joke_with_length_handling(message, joke_data)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error in joke command: {e}")
            await self.send_response(message, "Sorry, something went wrong getting a joke!")
            return True
    
    async def get_joke_from_api(self, category: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get a joke from the JokeAPI.
        
        Args:
            category: The joke category to fetch.
            
        Returns:
            Optional[Dict[str, Any]]: The joke data from the API, or None if it fails.
        """
        try:
            # Build the API URL
            # For dark jokes, don't use safe-mode since users expect dark humor
            # For other categories, use safe-mode to ensure family-friendly content
            if category and category.lower() == 'dark':
                url = f"{self.JOKE_API_BASE}/{category}?blacklistFlags={self.BLACKLIST_FLAGS}"
            else:
                url = f"{self.JOKE_API_BASE}/{category or 'Any'}?blacklistFlags={self.BLACKLIST_FLAGS}&safe-mode"
            
            self.logger.debug(f"Fetching joke from: {url}")
            
            # Make the API request
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=self.TIMEOUT)) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        # Check if the API returned an error
                        if data.get('error', False):
                            self.logger.warning(f"JokeAPI returned error: {data.get('message', 'Unknown error')}")
                            return None
                        
                        # Check flags to ensure it's clean (always check blacklist flags)
                        flags = data.get('flags', {})
                        if any(flags.get(flag, False) for flag in ['nsfw', 'religious', 'political', 'racist', 'sexist', 'explicit']):
                            self.logger.warning("JokeAPI returned flagged joke, skipping")
                            return None
                        
                        # For dark jokes, we allow safe: false since users expect dark humor
                        # For other categories, we require safe: true (when not using safe-mode)
                        if category and category.lower() == 'dark':
                            # Dark jokes can have safe: false, just check blacklist flags
                            self.logger.debug("Dark joke accepted (safe: false allowed for dark humor)")
                        else:
                            # For non-dark jokes, ensure they're safe
                            if not data.get('safe', False):
                                self.logger.warning("JokeAPI returned unsafe joke for non-dark category, skipping")
                                return None
                        
                        return data
                    elif response.status == 400:
                        # 400 error usually means no jokes available for this category
                        self.logger.info(f"No jokes available for category: {category}")
                        return None
                    else:
                        self.logger.error(f"JokeAPI returned status {response.status}")
                        return None
                        
        except asyncio.TimeoutError:
            self.logger.error("Timeout fetching joke from JokeAPI")
            return None
        except Exception as e:
            self.logger.error(f"Error fetching joke from JokeAPI: {e}")
            return None
    
    async def get_joke_with_length_handling(self, category: str = None) -> Optional[Dict[str, Any]]:
        """Get a joke from API with length handling based on configuration"""
        max_attempts = 5  # Prevent infinite loops
        
        for attempt in range(max_attempts):
            joke_data = await self.get_joke_from_api(category)
            
            if joke_data is None:
                return None
            
            # Check joke length
            joke_text = self.format_joke(joke_data)
            
            if len(joke_text) <= 130:
                # Joke is short enough, return it
                return joke_data
            elif self.long_jokes:
                # Long jokes are enabled, return it for splitting
                return joke_data
            else:
                # Long jokes are disabled, try again
                self.logger.debug(f"Joke too long ({len(joke_text)} chars), fetching another...")
                continue
        
        # If we've tried max_attempts times and still getting long jokes, return the last one
        self.logger.warning(f"Could not get short joke after {max_attempts} attempts")
        return joke_data
    
    async def send_joke_with_length_handling(self, message: MeshMessage, joke_data: Dict[str, Any]) -> None:
        """Send joke with length handling - split if necessary.
        
        Args:
            message: The original message to respond to.
            joke_data: The joke data from the API.
        """
        joke_text = self.format_joke(joke_data)
        
        if len(joke_text) <= 130:
            # Joke is short enough, send as single message
            await self.send_response(message, joke_text)
        else:
            # Joke is too long, split it
            parts = self.split_joke(joke_text)
            
            if len(parts) == 2 and len(parts[0]) <= 130 and len(parts[1]) <= 130:
                # Can be split into two messages (per-user rate limit applies only to first)
                await self.send_response(message, parts[0])
                # Use conservative delay to avoid rate limiting (same as weather command)
                await asyncio.sleep(2.0)
                await self.send_response(message, parts[1], skip_user_rate_limit=True)
            else:
                # Cannot be split properly, send as single message (user will see truncation)
                await self.send_response(message, joke_text)
    
    def split_joke(self, joke_text: str) -> List[str]:
        """Split a long joke at a logical point.
        
        Args:
            joke_text: The full text of the joke.
            
        Returns:
            List[str]: A list of joke parts.
        """
        # Remove emoji for splitting
        clean_joke = joke_text[2:] if joke_text.startswith('🎭 ') else joke_text
        
        # Try to split at common logical points
        split_points = [
            '.\n\n',  # Two-part jokes with double newline
            '.\n',    # Single newline
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
                    return [f"🎭 {parts[0]}{split_point}", f"🎭 {parts[1]}"]
        
        # If no good split point found, split at middle
        mid_point = len(clean_joke) // 2
        # Find nearest space to avoid splitting words
        for i in range(mid_point, len(clean_joke)):
            if clean_joke[i] == ' ':
                mid_point = i
                break
        
        part1 = clean_joke[:mid_point]
        part2 = clean_joke[mid_point + 1:]
        
        return [f"🎭 {part1}", f"🎭 {part2}"]
    
    def format_joke(self, joke_data: Dict[str, Any]) -> str:
        """Format the joke data into a readable string.
        
        Args:
            joke_data: The joke data from the API.
            
        Returns:
            str: The formatted joke string.
        """
        try:
            joke_type = joke_data.get('type', 'single')
            
            if joke_type == 'twopart':
                # Two-part joke (setup + delivery)
                setup = joke_data.get('setup', '')
                delivery = joke_data.get('delivery', '')
                
                if setup and delivery:
                    return f"🎭 {setup}\n\n{delivery}"
                else:
                    return f"🎭 {setup or delivery}"
            
            elif joke_type == 'single':
                # Single joke
                joke = joke_data.get('joke', '')
                
                if joke:
                    return f"🎭 {joke}"
                else:
                    return "🎭 No joke content available"
            
            else:
                # Unknown type, try to extract any text
                joke_text = joke_data.get('joke', '') or joke_data.get('setup', '') or joke_data.get('delivery', '')
                if joke_text:
                    return f"🎭 {joke_text}"
                else:
                    return "🎭 No joke content available"
                    
        except Exception as e:
            self.logger.error(f"Error formatting joke: {e}")
            return "🎭 Sorry, couldn't format the joke properly!"
