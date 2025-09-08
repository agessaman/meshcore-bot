#!/usr/bin/env python3
"""
Command management functionality for the MeshCore Bot
Handles all bot commands, keyword matching, and response generation
"""

import re
import time
import asyncio
from typing import List, Dict, Tuple, Optional, Any
from meshcore import EventType

from .models import MeshMessage
from .plugin_loader import PluginLoader
from .commands.base_command import BaseCommand


class CommandManager:
    """Manages all bot commands and responses using dynamic plugin loading"""
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        
        # Load configuration
        self.keywords = self.load_keywords()
        self.custom_syntax = self.load_custom_syntax()
        self.banned_users = self.load_banned_users()
        self.monitor_channels = self.load_monitor_channels()
        
        # Initialize plugin loader and load all plugins
        self.plugin_loader = PluginLoader(bot)
        self.commands = self.plugin_loader.load_all_plugins()
        
        self.logger.info(f"CommandManager initialized with {len(self.commands)} plugins")
    
    async def _apply_tx_delay(self):
        """Apply transmission delay to prevent message collisions"""
        if self.bot.tx_delay_ms > 0:
            self.logger.debug(f"Applying {self.bot.tx_delay_ms}ms transmission delay")
            await asyncio.sleep(self.bot.tx_delay_ms / 1000.0)
    
    def load_keywords(self) -> Dict[str, str]:
        """Load keywords from config"""
        keywords = {}
        if self.bot.config.has_section('Keywords'):
            for keyword, response in self.bot.config.items('Keywords'):
                # Strip quotes from the response if present
                if response.startswith('"') and response.endswith('"'):
                    response = response[1:-1]
                keywords[keyword.lower()] = response
        return keywords
    
    def load_custom_syntax(self) -> Dict[str, str]:
        """Load custom syntax patterns from config"""
        syntax_patterns = {}
        if self.bot.config.has_section('Custom_Syntax'):
            for pattern, response_format in self.bot.config.items('Custom_Syntax'):
                # Strip quotes from the response format if present
                if response_format.startswith('"') and response_format.endswith('"'):
                    response_format = response_format[1:-1]
                syntax_patterns[pattern] = response_format
        return syntax_patterns
    
    def load_banned_users(self) -> List[str]:
        """Load banned users from config"""
        banned = self.bot.config.get('Banned_Users', 'banned_users', fallback='')
        return [user.strip() for user in banned.split(',') if user.strip()]
    
    def load_monitor_channels(self) -> List[str]:
        """Load monitored channels from config"""
        channels = self.bot.config.get('Channels', 'monitor_channels', fallback='')
        return [channel.strip() for channel in channels.split(',') if channel.strip()]
    
    def build_enhanced_connection_info(self, message: MeshMessage) -> str:
        """Build enhanced connection info with SNR, RSSI, and parsed route information"""
        # Extract just the hops and path info without the route type
        routing_info = message.path or "Unknown routing"
        
        # Clean up the routing info to remove the "via ROUTE_TYPE_*" part
        if "via ROUTE_TYPE_" in routing_info:
            # Extract just the hops and path part
            parts = routing_info.split(" via ROUTE_TYPE_")
            if len(parts) > 0:
                routing_info = parts[0]
        
        # Add SNR and RSSI
        snr_info = f"SNR: {message.snr or 'Unknown'} dB"
        rssi_info = f"RSSI: {message.rssi or 'Unknown'} dBm"
        
        # Build enhanced connection info
        connection_info = f"{routing_info} | {snr_info} | {rssi_info}"
        
        return connection_info
    
    def check_keywords(self, message: MeshMessage) -> List[tuple]:
        """Check message content for keywords and return matching responses"""
        matches = []
        # Strip exclamation mark if present (for command-style messages)
        content = message.content.strip()
        if content.startswith('!'):
            content = content[1:].strip()
        content_lower = content.lower()
        
        # Check for help requests first (special handling)
        if content_lower.startswith('help '):
            command_name = content_lower[5:].strip()  # Remove "help " prefix
            help_text = self.get_help_for_command(command_name)
            matches.append(('help', help_text))
            return matches
        elif content_lower == 'help':
            help_text = self.get_general_help()
            matches.append(('help', help_text))
            return matches
        
        # Check all loaded plugins for matches
        for command_name, command in self.commands.items():
            if command.should_execute(message):
                # Get response format and generate response
                response_format = command.get_response_format()
                if response_format:
                    response = command.format_response(message, response_format)
                    matches.append((command_name, response))
                else:
                    # For commands without response format, they handle their own response
                    # We'll mark them as matched but let execute_commands handle the actual execution
                    matches.append((command_name, None))
        
        # Check remaining keywords that don't have plugins
        for keyword, response_format in self.keywords.items():
            # Skip if we already have a plugin handling this keyword
            if any(cmd.matches_keyword(message) for cmd in self.commands.values()):
                continue
                
            if keyword.lower() in content_lower:
                try:
                    # Build enhanced connection info with parsed route information
                    connection_info = self.build_enhanced_connection_info(message)
                    
                    # Format timestamp
                    if message.timestamp and message.timestamp != 'unknown':
                        try:
                            from datetime import datetime
                            dt = datetime.fromtimestamp(message.timestamp)
                            time_str = dt.strftime("%H:%M:%S")
                        except:
                            time_str = str(message.timestamp)
                    else:
                        time_str = "Unknown"
                    
                    # Format the response with available message data
                    response = response_format.format(
                        sender=message.sender_id or "Unknown",
                        connection_info=connection_info,
                        path=message.path or "Unknown",
                        timestamp=time_str,
                        snr=message.snr or "Unknown",
                        rssi=message.rssi or "Unknown"
                    )
                    matches.append((keyword, response))
                except (KeyError, ValueError) as e:
                    # Fallback to simple response if formatting fails
                    self.logger.warning(f"Error formatting response for '{keyword}': {e}")
                    matches.append((keyword, response_format))
        
        return matches
    
    async def handle_advert_command(self, message: MeshMessage):
        """Handle the advert command from DM"""
        await self.commands['advert'].execute(message)
    
    async def send_dm(self, recipient_id: str, content: str) -> bool:
        """Send a direct message using meshcore-cli command"""
        if not self.bot.connected or not self.bot.meshcore:
            return False
        
        # Check user rate limiter (prevents spam from users)
        if not self.bot.rate_limiter.can_send():
            wait_time = self.bot.rate_limiter.time_until_next()
            self.logger.warning(f"Rate limited. Wait {wait_time:.1f} seconds")
            return False
        
        # Wait for bot TX rate limiter (prevents network overload)
        await self.bot.bot_tx_rate_limiter.wait_for_tx()
        
        # Apply transmission delay to prevent message collisions
        await self._apply_tx_delay()
        
        try:
            # Find the contact by name (since recipient_id is the contact name)
            contact = self.bot.meshcore.get_contact_by_name(recipient_id)
            if not contact:
                self.logger.error(f"Contact not found for name: {recipient_id}")
                return False
            
            # Use the contact name for logging
            contact_name = contact.get('name', contact.get('adv_name', recipient_id))
            self.logger.info(f"Sending DM to {contact_name}: {content}")
            
            # Try to use send_msg_with_retry if available (meshcore-2.1.6+)
            try:
                # Use the meshcore commands interface for send_msg_with_retry
                if hasattr(self.bot.meshcore, 'commands') and hasattr(self.bot.meshcore.commands, 'send_msg_with_retry'):
                    self.logger.debug("Using send_msg_with_retry for improved reliability")
                    
                    # Use send_msg_with_retry with configurable retry parameters
                    max_attempts = self.bot.config.getint('Bot', 'dm_max_retries', fallback=3)
                    max_flood_attempts = self.bot.config.getint('Bot', 'dm_max_flood_attempts', fallback=2)
                    flood_after = self.bot.config.getint('Bot', 'dm_flood_after', fallback=2)
                    timeout = 0  # Use suggested timeout from meshcore
                    
                    self.logger.debug(f"Attempting DM send with {max_attempts} max attempts")
                    result = await self.bot.meshcore.commands.send_msg_with_retry(
                        contact, 
                        content,
                        max_attempts=max_attempts,
                        max_flood_attempts=max_flood_attempts,
                        flood_after=flood_after,
                        timeout=timeout
                    )
                else:
                    # Fallback to regular send_msg for older meshcore versions
                    self.logger.debug("send_msg_with_retry not available, using send_msg")
                    result = await self.bot.meshcore.commands.send_msg(contact, content)
                    
            except AttributeError:
                # Fallback to regular send_msg for older meshcore versions
                self.logger.debug("send_msg_with_retry not available, using send_msg")
                result = await self.bot.meshcore.commands.send_msg(contact, content)
            
            # Check if the result indicates success
            if result:
                if hasattr(result, 'type') and result.type == EventType.ERROR:
                    self.logger.error(f"❌ DM failed to {contact_name}: {result.payload}")
                    return False
                elif hasattr(result, 'type') and result.type == EventType.MSG_SENT:
                    # For send_msg_with_retry, check if we got an ACK (result is not None means ACK received)
                    if hasattr(self.bot.meshcore, 'commands') and hasattr(self.bot.meshcore.commands, 'send_msg_with_retry'):
                        # We used send_msg_with_retry, so result being returned means ACK was received
                        self.logger.info(f"✅ DM sent and ACK received from {contact_name}")
                    else:
                        # We used regular send_msg, so just log the send
                        self.logger.info(f"✅ DM sent to {contact_name}")
                    self.bot.rate_limiter.record_send()
                    self.bot.bot_tx_rate_limiter.record_tx()
                    return True
                else:
                    # If result is not None but doesn't have expected attributes, assume success
                    self.logger.info(f"✅ DM sent to {contact_name} (result: {result})")
                    self.bot.rate_limiter.record_send()
                    self.bot.bot_tx_rate_limiter.record_tx()
                    return True
            else:
                # This means send_msg_with_retry failed to get an ACK after all retries
                if hasattr(self.bot.meshcore, 'commands') and hasattr(self.bot.meshcore.commands, 'send_msg_with_retry'):
                    self.logger.error(f"❌ DM to {contact_name} failed - no ACK received after retries")
                else:
                    self.logger.error(f"❌ DM to {contact_name} failed - no result returned")
                return False
                
        except Exception as e:
            self.logger.error(f"Failed to send DM: {e}")
            return False
    
    async def send_channel_message(self, channel: str, content: str) -> bool:
        """Send a channel message using meshcore-cli command"""
        if not self.bot.connected or not self.bot.meshcore:
            return False
        
        # Check user rate limiter (prevents spam from users)
        if not self.bot.rate_limiter.can_send():
            wait_time = self.bot.rate_limiter.time_until_next()
            self.logger.warning(f"Rate limited. Wait {wait_time:.1f} seconds")
            return False
        
        # Wait for bot TX rate limiter (prevents network overload)
        await self.bot.bot_tx_rate_limiter.wait_for_tx()
        
        # Apply transmission delay to prevent message collisions
        await self._apply_tx_delay()
        
        try:
            # Get channel number from channel name
            channel_num = self.bot.channel_manager.get_channel_number(channel)
            
            self.logger.info(f"Sending channel message to {channel} (channel {channel_num}): {content}")
            
            # Use meshcore-cli send_chan_msg function
            from meshcore_cli.meshcore_cli import send_chan_msg
            result = await send_chan_msg(self.bot.meshcore, channel_num, content)
            
            if result and result.type != EventType.ERROR:
                self.logger.info(f"Successfully sent channel message to {channel} (channel {channel_num})")
                self.bot.rate_limiter.record_send()
                self.bot.bot_tx_rate_limiter.record_tx()
                return True
            else:
                self.logger.error(f"Failed to send channel message: {result.payload if result else 'No result'}")
                return False
                
        except Exception as e:
            self.logger.error(f"Failed to send channel message: {e}")
            return False
    
    def get_help_for_command(self, command_name: str) -> str:
        """Get help text for a specific command (LoRa-friendly compact format)"""
        # First, try to find a command by exact name
        command = self.commands.get(command_name)
        if command:
            return f"Help {command_name}: {command.get_help_text()}"
        
        # If not found, search through all commands and their keywords
        for cmd_name, cmd_instance in self.commands.items():
            # Check if the requested command name matches any of this command's keywords
            if hasattr(cmd_instance, 'keywords') and command_name in cmd_instance.keywords:
                return f"Help {command_name}: {cmd_instance.get_help_text()}"
        
        # If still not found, return unknown command message
        available_commands = []
        for cmd_name, cmd_instance in self.commands.items():
            available_commands.append(cmd_name)
            if hasattr(cmd_instance, 'keywords'):
                available_commands.extend(cmd_instance.keywords)
        
        return f"Unknown: {command_name}. Available: {', '.join(sorted(set(available_commands)))}"
    
    def get_general_help(self) -> str:
        """Get general help text from config (LoRa-friendly compact format)"""
        # Get the help response from the keywords config
        return self.keywords.get('help', 'Help not configured')
    
    def get_available_commands_list(self) -> str:
        """Get a formatted list of available commands"""
        commands_list = ""
        
        # Group commands by category
        basic_commands = ['test', 'ping', 'help', 'cmd']
        custom_syntax = ['t_phrase', 'at_phrase']  # Use the actual command key
        special_commands = ['advert']
        weather_commands = ['wx', 'aqi']
        solar_commands = ['sun', 'moon', 'solar', 'hfcond', 'satpass']
        
        commands_list += "**Basic Commands:**\n"
        for cmd in basic_commands:
            if cmd in self.commands:
                help_text = self.commands[cmd].get_help_text()
                commands_list += f"• `{cmd}` - {help_text}\n"
        
        commands_list += "\n**Custom Syntax:**\n"
        for cmd in custom_syntax:
            if cmd in self.commands:
                help_text = self.commands[cmd].get_help_text()
                # Add user-friendly aliases
                if cmd == 't_phrase':
                    commands_list += f"• `t phrase` - {help_text}\n"
                elif cmd == 'at_phrase':
                    commands_list += f"• `@{{string}}` - {help_text}\n"
                else:
                    commands_list += f"• `{cmd}` - {help_text}\n"
        
        commands_list += "\n**Special Commands:**\n"
        for cmd in special_commands:
            if cmd in self.commands:
                help_text = self.commands[cmd].get_help_text()
                commands_list += f"• `{cmd}` - {help_text}\n"
        
        commands_list += "\n**Weather Commands:**\n"
        for cmd in weather_commands:
            if cmd in self.commands:
                help_text = self.commands[cmd].get_help_text()
                commands_list += f"• `{cmd}` - {help_text}\n"
        
        commands_list += "\n**Solar Commands:**\n"
        for cmd in solar_commands:
            if cmd in self.commands:
                help_text = self.commands[cmd].get_help_text()
                commands_list += f"• `{cmd}` - {help_text}\n"
        
        return commands_list
    
    async def send_response(self, message: MeshMessage, content: str) -> bool:
        """Unified method for sending responses to users"""
        try:
            if message.is_dm:
                return await self.send_dm(message.sender_id, content)
            else:
                return await self.send_channel_message(message.channel, content)
        except Exception as e:
            self.logger.error(f"Failed to send response: {e}")
            return False
    
    async def execute_commands(self, message):
        """Execute command objects that handle their own responses"""
        # Strip exclamation mark if present (for command-style messages)
        content = message.content.strip()
        if content.startswith('!'):
            content = content[1:].strip()
        content_lower = content.lower()
        
        # Check each command to see if it should execute
        for command_name, command in self.commands.items():
            if command.should_execute(message):
                # Only execute commands that don't have a response format (they handle their own responses)
                response_format = command.get_response_format()
                if response_format is not None:
                    # This command was already handled by keyword matching
                    continue
                
                self.logger.info(f"Command '{command_name}' matched, executing")
                
                # Check if command can execute (cooldown, DM requirements, etc.)
                if not command.can_execute(message):
                    if command.requires_dm and not message.is_dm:
                        await self.send_response(message, f"Command '{command_name}' can only be used in DMs")
                    elif hasattr(command, 'get_remaining_cooldown') and callable(command.get_remaining_cooldown):
                        # Check if it's the per-user version (takes user_id parameter)
                        import inspect
                        sig = inspect.signature(command.get_remaining_cooldown)
                        if len(sig.parameters) > 0:
                            remaining = command.get_remaining_cooldown(message.sender_id)
                        else:
                            remaining = command.get_remaining_cooldown()
                        
                        if remaining > 0:
                            await self.send_response(message, f"Command '{command_name}' is on cooldown. Wait {remaining} seconds.")
                    return
                
                try:
                    # Record execution time for cooldown tracking
                    if hasattr(command, '_record_execution') and callable(command._record_execution):
                        import inspect
                        sig = inspect.signature(command._record_execution)
                        if len(sig.parameters) > 0:
                            command._record_execution(message.sender_id)
                        else:
                            command._record_execution()
                    await command.execute(message)
                except Exception as e:
                    self.logger.error(f"Error executing command '{command_name}': {e}")
                    # Send error message to user
                    await self.send_response(message, f"Error executing {command_name}: {e}")
                return
    
    def get_plugin_by_keyword(self, keyword: str) -> Optional[BaseCommand]:
        """Get a plugin by keyword"""
        return self.plugin_loader.get_plugin_by_keyword(keyword)
    
    def get_plugin_by_name(self, name: str) -> Optional[BaseCommand]:
        """Get a plugin by name"""
        return self.plugin_loader.get_plugin_by_name(name)
    
    def reload_plugin(self, plugin_name: str) -> bool:
        """Reload a specific plugin"""
        return self.plugin_loader.reload_plugin(plugin_name)
    
    def get_plugin_metadata(self, plugin_name: str = None) -> Dict[str, Any]:
        """Get plugin metadata"""
        return self.plugin_loader.get_plugin_metadata(plugin_name)
