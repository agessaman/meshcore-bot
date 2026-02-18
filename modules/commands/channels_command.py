#!/usr/bin/env python3
"""
Channels command for the MeshCore Bot
Lists common hashtag channels for the region with multi-message support
"""

from .base_command import BaseCommand
from ..models import MeshMessage
from typing import Optional
import asyncio
import re


class ChannelsCommand(BaseCommand):
    """Handles the channels command.
    
    Lists common hashtag channels for the region with multi-message support
    and sub-category filtering.
    """
    
    # Plugin metadata
    name = "channels"
    keywords = ['channels', 'channel']
    description = "Lists hashtag channels with sub-categories. Use 'channels' for general, 'channels list' for all categories, 'channels <category>' for specific categories, 'channels #channel' for specific channel info."
    category = "basic"
    
    # Documentation
    short_description = "Lists hashtag channels with sub-categories"
    usage = "channels [list|category|#channel]"
    examples = ["channels", "channels list"]
    parameters = [
        {"name": "list", "description": "Show all channel categories"},
        {"name": "category", "description": "Filter by category name"},
        {"name": "#channel", "description": "Get info on a specific channel"}
    ]
    
    def __init__(self, bot):
        """Initialize the channels command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.channels_enabled = self.get_config_value('Channels_Command', 'enabled', fallback=True, value_type='bool')
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.channels_enabled:
            return False
        return super().can_execute(message)
    
    def get_help_text(self) -> str:
        return self.translate('commands.channels.help')
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if this command matches the message content based on keywords.
        
        Args:
            message: The message to check.
            
        Returns:
            bool: True if the message matches a command keyword.
        """
        if not self.keywords:
            return False
        
        # Strip exclamation mark if present (for command-style messages)
        content = message.content.strip()
        if content.startswith('!'):
            content = content[1:].strip()
        content_lower = content.lower()
        
        # Don't match if this looks like a subcommand of another command
        # (e.g., "stats channels" should not match "channels" command)
        if ' ' in content_lower:
            parts = content_lower.split()
            if len(parts) > 1 and parts[0] not in ['channels', 'channel']:
                return False
        
        for keyword in self.keywords:
            keyword_lower = keyword.lower()
            
            # Check for exact match first
            if keyword_lower == content_lower:
                return True
            
            # Check for word boundary matches using regex
            # Create a regex pattern that matches the keyword at word boundaries
            # Use custom word boundary that treats underscores as separators
            # (?<![a-zA-Z0-9]) = negative lookbehind for alphanumeric characters (not underscore)
            # (?![a-zA-Z0-9]) = negative lookahead for alphanumeric characters (not underscore)
            # This allows underscores to act as word boundaries
            pattern = r'(?<![a-zA-Z0-9])' + re.escape(keyword_lower) + r'(?![a-zA-Z0-9])'
            if re.search(pattern, content_lower):
                return True
        
        return False
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the channels command.
        
        Args:
            message: The input message trigger.
            
        Returns:
            bool: True if execution was successful.
        """
        try:
            # Parse the command to check for sub-commands
            content = message.content.strip()
            if content.startswith('!'):
                content = content[1:].strip()
            
            # Check for sub-command (e.g., "channels seattle", "channel seahawks", "channels list", "channels #bot")
            sub_command = None
            specific_channel = None
            if content.lower().startswith('channels ') or content.lower().startswith('channel '):
                parts = content.split(' ', 1)
                if len(parts) > 1:
                    sub_command = parts[1].strip().lower()
                    
                    # Handle special "list" command to show all categories
                    if sub_command == 'list':
                        await self._show_all_categories(message)
                        return True
                    
                    # Check if user is asking for a specific channel (starts with #)
                    if sub_command.startswith('#'):
                        specific_channel = sub_command
                        sub_command = None
                    else:
                        # First check if this is a valid category
                        if self._is_valid_category(sub_command):
                            # It's a category, keep it as sub_command
                            pass
                        else:
                            # Check if this might be a channel search (not a category)
                            # Try to find a channel that matches this name across all categories
                            found_channel = self._find_channel_by_name(sub_command)
                            if found_channel:
                                specific_channel = '#' + found_channel
                                sub_command = None
            
            # Handle specific channel request
            if specific_channel:
                await self._show_specific_channel(message, specific_channel)
                return True
            
            # Load channels from config (with sub-command support)
            channels = self._load_channels_from_config(sub_command)
            
            if not channels:
                if sub_command:
                    await self.send_response(message, self.translate('commands.channels.no_channels_for_category', category=sub_command))
                else:
                    await self.send_response(message, self.translate('commands.channels.no_channels_configured'))
                return True
            
            # Build channel list (names only, no descriptions)
            channel_list = []
            for channel_name, description in channels.items():
                channel_list.append(channel_name)  # Just the channel name
            
            # Split into multiple messages if needed (130 character limit)
            messages = self._split_into_messages(channel_list, sub_command)
            
            # Send each message with a small delay between them
            await self._send_multiple_messages(message, messages)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error in channels command: {e}")
            await self.send_response(message, self.translate('commands.channels.error_retrieving_channels', error=str(e)))
            return False
    
    def _load_channels_from_config(self, sub_command: str = None) -> dict:
        """Load channels from the Channels_List config section with optional sub-command filtering.
        
        Args:
            sub_command: Optional category filter.
            
        Returns:
            dict: Dictionary of channel names to descriptions.
        """
        channels = {}
        
        for channel_name, description in self._parse_config_channels():
            # Handle sub-command filtering
            if sub_command:
                # Special case: "general" should show general channels (no category prefix)
                if sub_command == 'general':
                    # For general channels, only show channels that don't have sub-command prefixes
                    if '.' in channel_name:
                        continue
                    display_name = channel_name
                else:
                    # Check if this channel belongs to the sub-command
                    if not channel_name.startswith(f'{sub_command}.'):
                        continue
                    # Remove the sub-command prefix for display
                    display_name = channel_name[len(sub_command) + 1:]  # Remove 'subcommand.'
            else:
                # For general channels, only show channels that don't have sub-command prefixes
                if '.' in channel_name:
                    continue
                display_name = channel_name
            
            # Add # prefix if not already present
            if not display_name.startswith('#'):
                display_name = '#' + display_name
            
            channels[display_name] = description
        
        return channels
    
    async def _show_all_categories(self, message: MeshMessage) -> None:
        """Show all available channel categories.
        
        Args:
            message: The message to reply to.
        """
        try:
            categories = self._get_all_categories()
            
            if not categories:
                await self.send_response(message, self.translate('commands.channels.no_categories_configured'))
                return
            
            # Build category list
            category_list = []
            for category, count in categories.items():
                category_list.append(self.translate('commands.channels.category_count', category=category, count=count))
            
            # Split into multiple messages if needed
            messages = self._split_into_messages(category_list, "Available categories")
            
            # Send each message with a small delay between them
            await self._send_multiple_messages(message, messages)
                
        except Exception as e:
            self.logger.error(f"Error showing categories: {e}")
            await self.send_response(message, self.translate('commands.channels.error_retrieving_categories', error=str(e)))
    
    def _get_all_categories(self) -> dict:
        """Get all available channel categories and their channel counts.
        
        Returns:
            dict: Dictionary mapping category names to channel counts.
        """
        categories = {}
        
        for channel_name, description in self._parse_config_channels():
            # Check if this is a sub-command channel (has a dot)
            if '.' in channel_name:
                category = channel_name.split('.')[0]
                if category not in categories:
                    categories[category] = 0
                categories[category] += 1
            else:
                # General channels (no category)
                if 'general' not in categories:
                    categories['general'] = 0
                categories['general'] += 1
        
        return categories
    
    def _find_channel_by_name(self, search_name: str) -> Optional[str]:
        """Find a channel by partial name match across all categories.
        
        Args:
            search_name: The channel name to search for.
            
        Returns:
            Optional[str]: The full channel name if found, None otherwise.
        """
        search_name_lower = search_name.lower()
        
        for config_name, description in self._parse_config_channels():
            # Handle sub-command channels
            if '.' in config_name:
                category, name = config_name.split('.', 1)
                # Check if the channel name matches (case insensitive)
                if name.lower() == search_name_lower:
                    return name
            else:
                # Check general channels
                if config_name.lower() == search_name_lower:
                    return config_name
        
        return None
    
    async def _show_specific_channel(self, message: MeshMessage, channel_name: str) -> None:
        """Show description for a specific channel.
        
        Args:
            message: The message to reply to.
            channel_name: The channel name to show info for.
        """
        try:
            # Search for the channel in all categories
            found_channel = None
            found_category = None
            
            for config_name, description in self._parse_config_channels():
                # Handle sub-command channels
                if '.' in config_name:
                    category, name = config_name.split('.', 1)
                    display_name = '#' + name
                else:
                    display_name = '#' + config_name
                
                # Check if this matches the requested channel
                if display_name.lower() == channel_name.lower():
                    found_channel = display_name
                    found_category = category if '.' in config_name else 'general'
                    break
            
            if found_channel:
                # Get the description
                if found_category == 'general':
                    config_key = found_channel[1:]  # Remove #
                else:
                    config_key = f"{found_category}.{found_channel[1:]}"  # Remove #
                
                description = self.bot.config.get('Channels_List', config_key, fallback=self.translate('commands.channels.no_description_available'))
                
                # Strip quotes if present
                if description.startswith('"') and description.endswith('"'):
                    description = description[1:-1]
                
                response = f"{found_channel}: {description}"
                await self.send_response(message, response)
            else:
                await self.send_response(message, self.translate('commands.channels.channel_not_found', channel=channel_name))
                
        except Exception as e:
            self.logger.error(f"Error showing specific channel: {e}")
            await self.send_response(message, self.translate('commands.channels.error_retrieving_channel_info', error=str(e)))
    
    def _split_into_messages(self, channel_list: list, sub_command: str = None) -> list:
        """Split channel list into multiple messages if they exceed 130 characters.
        
        Args:
            channel_list: List of channel string items.
            sub_command: The current sub-command/category context.
            
        Returns:
            list: List of message strings ready for sending.
        """
        messages = []
        
        # Set appropriate header based on sub-command
        current_message = self._get_header_for_subcommand(sub_command)
        current_length = len(current_message)
        
        for channel in channel_list:
            # Check if adding this channel would exceed the limit
            if current_length + len(channel) + 2 > 130:  # +2 for ", " separator
                # Start a new message
                expected_header = self._get_continuation_header_for_subcommand(sub_command)
                
                if current_message != expected_header:
                    messages.append(current_message.rstrip(", "))
                    current_message = self.translate('commands.channels.headers.channels_cont')
                    current_length = len(current_message)
                else:
                    # If even the first channel is too long, just send it alone
                    messages.append(f"{expected_header}{channel}")
                    current_message = self.translate('commands.channels.headers.channels_cont')
                    current_length = len(current_message)
                    continue
            
            # Add channel to current message
            initial_header = self._get_header_for_subcommand(sub_command)
            continuation_header = self._get_continuation_header_for_subcommand(sub_command)
            channels_cont = self.translate('commands.channels.headers.channels_cont')
            if current_message == initial_header or current_message == continuation_header or current_message == channels_cont:
                current_message += channel
            else:
                current_message += f", {channel}"
            current_length = len(current_message)
        
        # Add the last message if it has content
        header = self._get_continuation_header_for_subcommand(sub_command)
        channels_cont = self.translate('commands.channels.headers.channels_cont')
        if current_message != header and current_message != channels_cont:
            messages.append(current_message)
        
        # If no messages were created, send a default message
        if not messages:
            if sub_command:
                messages.append(self.translate('commands.channels.no_category_channels', category=sub_command))
            else:
                messages.append(self.translate('commands.channels.no_channels_configured'))
        
        return messages
    
    def _get_header_for_subcommand(self, sub_command: str = None) -> str:
        """Get the appropriate header for a sub-command.
        
        Args:
            sub_command: The sub-command/category name.
            
        Returns:
            str: Header string.
        """
        if sub_command == "Available categories":
            return self.translate('commands.channels.headers.available_categories')
        elif sub_command and sub_command != "general":
            return self.translate('commands.channels.headers.category', category=sub_command.title())
        else:
            return self.translate('commands.channels.headers.common_channels')
    
    def _get_continuation_header_for_subcommand(self, sub_command: str = None) -> str:
        """Get the appropriate header for continuation messages.
        
        Args:
            sub_command: The sub-command/category name.
            
        Returns:
            str: Continuation header string.
        """
        if sub_command == "Available categories":
            return self.translate('commands.channels.headers.available_categories')
        elif sub_command and sub_command != "general":
            return self.translate('commands.channels.headers.category_channels', category=sub_command.title())
        else:
            return self.translate('commands.channels.headers.common_channels_cont')
    
    async def _send_multiple_messages(self, message: MeshMessage, messages: list) -> None:
        """Send multiple messages with delays between them.
        
        Args:
            message: The original command message.
            messages: List of message strings to send.
        """
        for i, msg_content in enumerate(messages):
            if i > 0:
                # Small delay between messages to prevent overwhelming the network
                await asyncio.sleep(0.5)
            # Per-user rate limit applies only to first message (trigger); skip for continuations
            await self.send_response(message, msg_content, skip_user_rate_limit=(i > 0))
    
    def _parse_config_channels(self):
        """Parse all channels from config, returning a generator of (name, description) tuples.
        
        Yields:
            tuple: (channel_name, description) pairs.
        """
        if not self.bot.config.has_section('Channels_List'):
            return
        
        for channel_name, description in self.bot.config.items('Channels_List'):
            # Skip empty or commented lines
            if channel_name.strip() and not channel_name.startswith('#'):
                # Strip quotes if present
                if description.startswith('"') and description.endswith('"'):
                    description = description[1:-1]
                yield channel_name, description
    
    def _is_valid_category(self, category_name: str) -> bool:
        """Check if a category name is valid (has channels with that prefix).
        
        Args:
            category_name: The category to check.
            
        Returns:
            bool: True if the category exists.
        """
        if not category_name:
            return False
        
        # Check if there are any channels with this category prefix
        for channel_name, description in self._parse_config_channels():
            if '.' in channel_name:
                category = channel_name.split('.')[0]
                if category.lower() == category_name.lower():
                    return True
        
        return False
