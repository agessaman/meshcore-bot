#!/usr/bin/env python3
"""
Help command for the MeshCore Bot
Provides help information for commands and general usage
"""

import sqlite3
from collections import defaultdict
from typing import Any, Optional
from .base_command import BaseCommand
from ..models import MeshMessage


class HelpCommand(BaseCommand):
    """Handles the help command.
    
    Provides assistance to users by listing available commands or displaying
    detailed help for specific commands. It dynamically aggregates command
    information from all loaded plugins.
    """
    
    # Plugin metadata
    name = "help"
    keywords = ['help']
    description = "Shows commands. Use 'help <command>' for details."
    category = "basic"
    
    # Documentation
    short_description = "Get help on available commands"
    usage = "help [command]"
    examples = ["help", "help wx"]
    parameters = [
        {"name": "command", "description": "Command name for detailed help (optional)"}
    ]
    
    def __init__(self, bot):
        """Initialize the help command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.help_enabled = self.get_config_value('Help_Command', 'enabled', fallback=True, value_type='bool')
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.help_enabled:
            return False
        return super().can_execute(message)
    
    def get_help_text(self) -> str:
        """Get help text for the help command.
        
        Returns:
            str: The help text for this command.
        """
        return self.translate('commands.help.description')
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the help command.
        
        Note: The help command logic is primarily handled by the CommandManager's
        keyword matching system. This method serves as a placeholder or fallback.
        
        Args:
            message: The message that triggered the command.
            
        Returns:
            bool: True (always, as actual processing happens elsewhere).
        """
        # The help command is now handled by keyword matching in the command manager
        # This is just a placeholder for future functionality
        self.logger.debug("Help command executed (handled by keyword matching)")
        return True
    
    def get_specific_help(self, command_name: str, message: MeshMessage = None) -> str:
        """Get help text for a specific command.
        
        Resolves aliases, finds the corresponding command plugin, and retrieves
        its help text.
        
        Args:
            command_name: The name or alias of the command.
            message: Optional message object for context-aware help.
            
        Returns:
            str: The formatted help text for the specific command.
        """
        # Map command aliases to their actual command names
        command_aliases = {
            't': 't_phrase',
            'advert': 'advert',
            'test': 'test',
            'ping': 'ping',
            'help': 'help'
        }
        
        # Normalize the command name
        normalized_name = command_aliases.get(command_name, command_name)
        
        # Get the command instance
        command = self.bot.command_manager.commands.get(normalized_name)
        
        if command:
            # Pass message context to get_help_text if the method supports it
            if hasattr(command, 'get_help_text') and callable(getattr(command, 'get_help_text')):
                try:
                    help_text = command.get_help_text(message)
                except TypeError:
                    # Fallback for commands that don't accept message parameter
                    help_text = command.get_help_text()
            else:
                help_text = self.translate('commands.help.no_help')
            return self.translate('commands.help.specific', command=command_name, help_text=help_text)
        else:
            available = self.get_available_commands_list(message)
            return self.translate('commands.help.unknown', command=command_name, available=available)
    
    def get_general_help(self) -> str:
        """Get general help text.
        
        Compiles a list of available commands and usage examples.
        
        Returns:
            str: The general help message to display to users.
        """
        commands_list = self.get_available_commands_list()
        help_text = self.translate('commands.help.general', commands_list=commands_list)
        help_text += self.translate('commands.help.usage_examples')
        help_text += self.translate('commands.help.custom_syntax')
        return help_text
    
    def _is_command_valid_for_channel(self, cmd_name: str, cmd_instance: Any, message: Optional[MeshMessage]) -> bool:
        """Return True if this command is valid in the message's channel context."""
        if message is None:
            return True
        if hasattr(cmd_instance, 'is_channel_allowed') and callable(cmd_instance.is_channel_allowed):
            if not cmd_instance.is_channel_allowed(message):
                return False
        if hasattr(self.bot.command_manager, '_is_channel_trigger_allowed'):
            if not self.bot.command_manager._is_channel_trigger_allowed(cmd_name, message):
                return False
        return True

    def get_available_commands_list(self, message: Optional[MeshMessage] = None) -> str:
        """Get a list of most popular commands in descending order.
        
        Queries usage statistics to order commands by popularity. Ensures each
        command is listed only once using its primary name. When message is
        provided, only returns commands valid for the message's channel (respects
        per-command channel overrides and channel_keywords).
        
        Args:
            message: Optional message for context filtering. When provided, only
                commands that can execute in this channel are included.
        
        Returns:
            str: Comma-separated list of command names.
        """
        try:
            # Use the plugin loader's keyword mappings to map keywords/aliases to primary command names
            plugin_loader = self.bot.command_manager.plugin_loader
            keyword_mappings = plugin_loader.keyword_mappings.copy() if hasattr(plugin_loader, 'keyword_mappings') else {}
            
            # Build a set of all primary command names and ensure they map to themselves
            # Filter by channel when message is provided
            primary_names = set()
            for cmd_name, cmd_instance in self.bot.command_manager.commands.items():
                if not self._is_command_valid_for_channel(cmd_name, cmd_instance, message):
                    continue
                primary_name = cmd_instance.name if hasattr(cmd_instance, 'name') else cmd_name
                primary_names.add(primary_name)
                # Ensure primary name maps to itself in keyword_mappings
                keyword_mappings[primary_name.lower()] = primary_name
            
            # Query the database for command usage statistics
            command_counts = defaultdict(int)
            try:
                with sqlite3.connect(self.bot.db_manager.db_path) as conn:
                    cursor = conn.cursor()
                    # Check if command_stats table exists
                    cursor.execute("""
                        SELECT name FROM sqlite_master 
                        WHERE type='table' AND name='command_stats'
                    """)
                    if cursor.fetchone():
                        # Query command usage
                        cursor.execute("""
                            SELECT command_name, COUNT(*) as count 
                            FROM command_stats 
                            GROUP BY command_name
                        """)
                        for row in cursor.fetchall():
                            command_name = row[0]
                            count = row[1]
                            
                            # Map keyword/alias to primary command name
                            # First try the plugin_loader's keyword_mappings
                            primary_name = keyword_mappings.get(command_name.lower())
                            
                            # If not found in mappings, check if it's already a primary name
                            if primary_name is None:
                                if command_name in primary_names:
                                    primary_name = command_name
                                else:
                                    # Try to find which command this belongs to by checking all commands
                                    for cmd_name, cmd_instance in self.bot.command_manager.commands.items():
                                        # Check if command_name matches the command's name
                                        cmd_primary = cmd_instance.name if hasattr(cmd_instance, 'name') else cmd_name
                                        if cmd_primary == command_name:
                                            primary_name = cmd_primary
                                            break
                                        # Check if it's a keyword of this command
                                        if hasattr(cmd_instance, 'keywords'):
                                            if command_name.lower() in [k.lower() for k in cmd_instance.keywords]:
                                                primary_name = cmd_primary
                                                break
                                    # If still not found, use the command_name as-is
                                    if primary_name is None:
                                        primary_name = command_name
                            
                            if primary_name in primary_names:
                                command_counts[primary_name] += count
            except Exception as e:
                self.logger.debug(f"Error querying command stats: {e}")
                # If stats table doesn't exist or query fails, fall back to all commands
                for cmd_name in self.bot.command_manager.commands.keys():
                    primary_name = self.bot.command_manager.commands[cmd_name].name if hasattr(self.bot.command_manager.commands[cmd_name], 'name') else cmd_name
                    command_counts[primary_name] = 0
            
            # If we have stats, sort by count descending, otherwise use all commands
            if command_counts:
                # Sort by count descending, then by name for consistency
                sorted_commands = sorted(
                    command_counts.items(),
                    key=lambda x: (-x[1], x[0])
                )
                # Extract just the command names (only primary names, no aliases)
                command_names = [name for name, _ in sorted_commands]
            else:
                # Fallback: use all primary command names (filtered by channel)
                command_names = sorted([
                    cmd.name if hasattr(cmd, 'name') else name
                    for name, cmd in self.bot.command_manager.commands.items()
                    if self._is_command_valid_for_channel(name, cmd, message)
                ])
            
            # Return comma-separated list
            return ', '.join(command_names)
            
        except Exception as e:
            self.logger.error(f"Error getting available commands list: {e}")
            # Fallback to simple list of all command names (filtered by channel)
            command_names = sorted([
                cmd.name if hasattr(cmd, 'name') else name
                for name, cmd in self.bot.command_manager.commands.items()
                if self._is_command_valid_for_channel(name, cmd, message)
            ])
            return ', '.join(command_names)
    

