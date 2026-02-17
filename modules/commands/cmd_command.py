#!/usr/bin/env python3
"""
Cmd command for the MeshCore Bot
Lists available commands in a compact, comma-separated format for LoRa
"""

from typing import Optional
from .base_command import BaseCommand
from ..models import MeshMessage


class CmdCommand(BaseCommand):
    """Handles the cmd command"""
    
    # Plugin metadata
    name = "cmd"
    keywords = ['cmd', 'commands']
    description = "Lists available commands in compact format"
    category = "basic"
    
    # Documentation
    short_description = "Lists available commands"
    usage = "cmd"
    examples = ["cmd"]
    
    def __init__(self, bot):
        """Initialize the cmd command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.cmd_enabled = self.get_config_value('Cmd_Command', 'enabled', fallback=True, value_type='bool')
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.cmd_enabled:
            return False
        return super().can_execute(message)
    
    def get_help_text(self) -> str:
        """Get help text for the cmd command.
        
        Returns:
            str: The help text for this command.
        """
        return "Lists commands in compact format."
    
    def _get_commands_list(self, max_length: Optional[int] = None) -> str:
        """Get a compact list of available commands, prioritizing important ones.
        
        Args:
            max_length: Maximum length for the command list (None = no limit).
        
        Returns:
            str: Comma-separated list of commands, truncated if necessary.
        """
        # Define priority order - most important/commonly used commands first
        priority_commands = [
            'test', 'ping', 'help', 'hello', 'cmd', 'advert',
            'wx', 'aqi', 'sun', 'moon', 'solar', 'hfcond', 'satpass',
            'prefix', 'path', 'sports', 'dice', 'roll', 'stats'
        ]
        
        # Get all command names
        all_commands = []
        
        # Include plugin commands
        for cmd_name, cmd_instance in self.bot.command_manager.commands.items():
            # Skip system commands without keywords (like greeter)
            if hasattr(cmd_instance, 'keywords') and cmd_instance.keywords:
                all_commands.append(cmd_name)
        
        # Include config keywords that aren't handled by plugins
        for keyword in self.bot.command_manager.keywords.keys():
            # Check if this keyword is already handled by a plugin
            is_plugin_keyword = any(
                keyword.lower() in [k.lower() for k in cmd.keywords] 
                for cmd in self.bot.command_manager.commands.values()
            )
            if not is_plugin_keyword:
                all_commands.append(keyword)
        
        # Remove duplicates and sort
        all_commands = sorted(set(all_commands))
        
        # Prioritize: put priority commands first, then others
        prioritized = []
        remaining = []
        
        for cmd in all_commands:
            if cmd in priority_commands:
                prioritized.append(cmd)
            else:
                remaining.append(cmd)
        
        # Sort priority commands by their order in priority_commands list
        prioritized = sorted(prioritized, key=lambda x: priority_commands.index(x) if x in priority_commands else 999)
        
        # Combine: priority first, then others
        command_names = prioritized + sorted(remaining)
        
        # Build the list, respecting max_length if provided
        if max_length is None:
            return ', '.join(command_names)
        
        # Build list within length limit
        result = []
        prefix = "Available commands: "
        current_length = len(prefix)
        
        for cmd in command_names:
            # Calculate length if we add this command: ", cmd" or "cmd" (first one)
            if result:
                test_length = current_length + len(', ') + len(cmd)
            else:
                test_length = current_length + len(cmd)
            
            if test_length <= max_length:
                if result:
                    result.append(cmd)
                    current_length += len(', ') + len(cmd)
                else:
                    result.append(cmd)
                    current_length += len(cmd)
            else:
                # Can't fit this command, add count of remaining
                remaining_count = len(command_names) - len(result)
                if remaining_count > 0:
                    suffix = f" ({remaining_count} more)"
                    if current_length + len(suffix) <= max_length:
                        result.append(suffix)
                break
        
        return prefix + ', '.join(result)
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the cmd command.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        try:
            # Check if user has defined a custom cmd keyword response in config
            # Use the already-loaded keywords dict (quotes are already stripped)
            cmd_keyword = self.bot.command_manager.keywords.get('cmd')
            if cmd_keyword:
                # User has defined a custom response, use it with formatting
                response = self.bot.command_manager.format_keyword_response(cmd_keyword, message)
                return await self.send_response(message, response)
            
            # Fallback to dynamic command list if no custom keyword is defined
            # Get max message length to ensure we fit within limits
            max_length = self.get_max_message_length(message)
            # _get_commands_list handles the prefix internally
            response = self._get_commands_list(max_length=max_length)
            return await self.send_response(message, response)
        except Exception as e:
            self.logger.error(f"Error executing cmd command: {e}")
            error_msg = self.translate('errors.execution_error', command='cmd', error=str(e))
            return await self.send_response(message, error_msg)
