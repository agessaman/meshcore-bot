#!/usr/bin/env python3
"""
Base command class for all MeshCore Bot commands
Provides common functionality and interface for command implementations
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
import pytz
import re
from ..models import MeshMessage
from ..security_utils import validate_pubkey_format
from ..utils import format_elapsed_display


class BaseCommand(ABC):
    """Base class for all bot commands - Plugin Interface.
    
    This class defines the interface that all commands must implement. It provides
    common functionality for configuration loading, localization, permission checking,
    rate limiting, and message response handling.
    """
    
    # Plugin metadata - to be overridden by subclasses
    name: str = ""
    keywords: List[str] = []  # All trigger words for this command (including name and aliases)
    description: str = ""
    requires_dm: bool = False
    requires_internet: bool = False  # Set to True if command needs internet access
    cooldown_seconds: int = 0
    category: str = "general"
    
    # Documentation fields - to be overridden by subclasses for website generation
    short_description: str = ""  # Brief description for website (without usage syntax)
    usage: str = ""  # Usage syntax, e.g., "wx <zipcode|city> [tomorrow|7d|hourly|alerts]"
    examples: List[str] = []  # Example commands, e.g., ["wx 98101", "wx seattle tomorrow"]
    parameters: List[Dict[str, str]] = []  # Parameter definitions, e.g., [{"name": "location", "description": "US zip code or city name"}]
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self._last_execution_time = 0
        
        # Per-user cooldown tracking (for commands that need per-user rate limiting)
        self._user_cooldowns: Dict[str, float] = {}
        
        # Load allowed channels from config (standardized channel override)
        self.allowed_channels = self._load_allowed_channels()
    
        # Load translated keywords after initialization
        self._load_translated_keywords()
        
        # Cache command prefix from config
        self._command_prefix = self._load_command_prefix()
    
    def translate(self, key: str, **kwargs) -> str:
        """Translate a key using the bot's translator.
        
        Args:
            key: Dot-separated key path (e.g., 'commands.wx.usage').
            **kwargs: Formatting parameters for string.format().
        
        Returns:
            str: Translated string, or key if translation not found.
        """
        if hasattr(self.bot, 'translator'):
            return self.bot.translator.translate(key, **kwargs)
        # Fallback if translator not available
        return key
    
    def translate_get_value(self, key: str) -> Any:
        """Get a raw value from translations (can be string, list, dict, etc.).
        
        Args:
            key: Dot-separated key path (e.g., 'commands.hacker.sudo_errors').
        
        Returns:
            Any: The value at the key path, or None if not found.
        """
        if hasattr(self.bot, 'translator'):
            return self.bot.translator.get_value(key)
        return None
    
    def get_config_value(self, section: str, key: str, fallback: Any = None, value_type: str = 'str') -> Any:
        """Get config value with backward compatibility for section name changes.
        
        For command configs, checks both old format (e.g., 'Hacker') and new format (e.g., 'Hacker_Command').
        This allows smooth migration from old config format to new standardized format.
        
        Args:
            section: Config section name (new format preferred).
            key: Config key name.
            fallback: Default value if not found.
            value_type: Type of value ('str', 'bool', 'int', 'float', 'list').
        
        Returns:
            Any: Config value of appropriate type, or fallback if not found.
        """
        # Map of old section names to new standardized names
        section_migration = {
            'Hacker': 'Hacker_Command',
            'Sports': 'Sports_Command',
            'Stats': 'Stats_Command',
            'Weather': 'Wx_Command',  # wx command reads from [Wx_Command]; [Weather] is legacy
        }
        # Legacy [Jokes] -> [Joke_Command] / [DadJoke_Command]: (requested_section, key) -> legacy section
        # For 'enabled', also try legacy key: (section, key) -> (legacy_section, legacy_key) or list of same
        legacy_key_alias = {
            ('Joke_Command', 'enabled'): ('Jokes', 'joke_enabled'),
            ('DadJoke_Command', 'enabled'): ('Jokes', 'dadjoke_enabled'),
            # Standard enabled with *_enabled fallback (same section, then old section)
            ('Stats_Command', 'enabled'): [
                ('Stats_Command', 'stats_enabled'),
                ('Stats', 'stats_enabled'),
            ],
            ('Sports_Command', 'enabled'): [
                ('Sports_Command', 'sports_enabled'),
                ('Sports', 'sports_enabled'),
            ],
            ('Hacker_Command', 'enabled'): [
                ('Hacker_Command', 'hacker_enabled'),
                ('Hacker', 'hacker_enabled'),
            ],
            ('Alert_Command', 'enabled'): [('Alert_Command', 'alert_enabled')],
        }
        legacy_section_fallback = {
            ('Joke_Command', 'joke_enabled'): 'Jokes',
            ('Joke_Command', 'seasonal_jokes'): 'Jokes',
            ('Joke_Command', 'long_jokes'): 'Jokes',
            ('DadJoke_Command', 'dadjoke_enabled'): 'Jokes',
            ('DadJoke_Command', 'long_jokes'): 'Jokes',
        }
        
        # Determine old and new section names
        new_section = section
        old_section = None
        for old, new in section_migration.items():
            if new == section:
                old_section = old
                break
        
        # Try new section first, then old section for backward compatibility, then legacy (e.g. Jokes)
        sections_to_try = [new_section]
        if old_section:
            sections_to_try.append(old_section)
        legacy_sec = legacy_section_fallback.get((section, key))
        if legacy_sec and legacy_sec not in sections_to_try:
            sections_to_try.append(legacy_sec)
        
        for sec in sections_to_try:
            if self.bot.config.has_section(sec):
                try:
                    if not self.bot.config.has_option(sec, key):
                        continue
                    
                    raw_value = self.bot.config.get(sec, key)
                    
                    # Type conversion
                    if value_type == 'str':
                        value = raw_value
                    elif value_type == 'bool':
                        value = self.bot.config.getboolean(sec, key, fallback=fallback)
                    elif value_type == 'int':
                        value = self.bot.config.getint(sec, key, fallback=fallback)
                    elif value_type == 'float':
                        value = self.bot.config.getfloat(sec, key, fallback=fallback)
                    elif value_type == 'list':
                        # Parse comma-separated list
                        value = [item.strip() for item in raw_value.split(',') if item.strip()]
                    else:
                        self.logger.warning(f"Unknown value_type '{value_type}' for {sec}.{key}, returning as string")
                        value = raw_value
                    
                    # If we got a value (not fallback), return it
                    if value != fallback or self.bot.config.has_option(sec, key):
                        # Log migration notice on first use of old/legacy section
                        if sec == old_section:
                            self.logger.info(f"Config migration: Using old section '[{old_section}]' for '{key}'. "
                                           f"Please update to '[{new_section}]' in config.ini")
                        elif legacy_sec and sec == legacy_sec:
                            self.logger.info(f"Config migration: Using old section '[{legacy_sec}]' for '{key}'. "
                                           f"Please update to '[{new_section}]' in config.ini")
                        return value
                except (ValueError, TypeError) as e:
                    self.logger.debug(f"Config conversion error for {sec}.{key}: {e}")
                    continue
                except Exception as e:
                    self.logger.debug(f"Error reading config {sec}.{key}: {e}")
                    continue
        
        # Try legacy key alias (e.g. [Jokes] joke_enabled when requesting Joke_Command enabled)
        alias = legacy_key_alias.get((section, key))
        if alias:
            aliases = [alias] if isinstance(alias, tuple) else alias
            for legacy_sec, legacy_key in aliases:
                if self.bot.config.has_section(legacy_sec) and self.bot.config.has_option(legacy_sec, legacy_key):
                    try:
                        if value_type == 'bool':
                            value = self.bot.config.getboolean(legacy_sec, legacy_key, fallback=fallback)
                        elif value_type == 'int':
                            value = self.bot.config.getint(legacy_sec, legacy_key, fallback=fallback)
                        elif value_type == 'float':
                            value = self.bot.config.getfloat(legacy_sec, legacy_key, fallback=fallback)
                        elif value_type == 'list':
                            raw = self.bot.config.get(legacy_sec, legacy_key)
                            value = [item.strip() for item in raw.split(',') if item.strip()]
                        else:
                            value = self.bot.config.get(legacy_sec, legacy_key)
                        return value
                    except (ValueError, TypeError) as e:
                        self.logger.debug(f"Config conversion error for {legacy_sec}.{legacy_key}: {e}")
        
        return fallback
    
    @abstractmethod
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the command with the given message.
        
        Args:
            message: The message that triggered the command.
            
        Returns:
            bool: True if execution was successful, False otherwise.
        """
        pass
    
    def get_help_text(self) -> str:
        """Get help text for this command.
        
        Returns:
            str: The help text (description) for this command.
        """
        return self.description or "No help available for this command."
    
    def get_usage_info(self) -> Dict[str, Any]:
        """Get structured usage information including sub-commands and options.
        
        Uses class attributes as defaults, with translation overrides for i18n support.
        
        Returns:
            Dict with keys:
                - 'description': Main command description (for help text)
                - 'short_description': Brief description for website (without usage syntax)
                - 'usage': Usage syntax string (e.g., "wx <zipcode|city> [option]")
                - 'subcommands': List of dicts with 'name' and 'description'
                - 'examples': List of example strings
                - 'parameters': List of dicts with 'name' and 'description'
        """
        # Start with class attribute defaults
        usage_info = {
            'description': self.description or "No description available",
            'short_description': self.short_description or "",
            'usage': self.usage or "",
            'subcommands': [],
            'examples': list(self.examples) if self.examples else [],
            'parameters': list(self.parameters) if self.parameters else []
        }
        
        # Try to get structured data from translations (i18n overrides)
        if hasattr(self.bot, 'translator'):
            try:
                # Get subcommands from translations
                subcommands_key = f"commands.{self.name}.subcommands"
                subcommands_data = self.translate_get_value(subcommands_key)
                if subcommands_data and isinstance(subcommands_data, list):
                    usage_info['subcommands'] = subcommands_data
                
                # Get examples from translations (override class attribute)
                examples_key = f"commands.{self.name}.examples"
                examples_data = self.translate_get_value(examples_key)
                if examples_data and isinstance(examples_data, list):
                    usage_info['examples'] = examples_data
                
                # Get usage from translations (override class attribute)
                usage_key = f"commands.{self.name}.usage_syntax"
                usage_data = self.translate_get_value(usage_key)
                if usage_data and isinstance(usage_data, str):
                    usage_info['usage'] = usage_data
                
                # Get parameters from translations (override class attribute)
                params_key = f"commands.{self.name}.parameters"
                params_data = self.translate_get_value(params_key)
                if params_data and isinstance(params_data, list):
                    usage_info['parameters'] = params_data
            except Exception as e:
                self.logger.debug(f"Could not load usage info from translations for {self.name}: {e}")
        
        return usage_info
    
    def _derive_config_section_name(self) -> str:
        """Derive config section name from command name.
        
        Handles camelCase names like "dadjoke" -> "DadJoke_Command"
        Regular names like "sports" -> "Sports_Command"
        
        Returns:
            str: The derived config section name.
        """
        # Special handling for camelCase names
        camel_case_map = {
            'dadjoke': 'DadJoke',
            'webviewer': 'WebViewer',
        }
        
        if self.name in camel_case_map:
            base_name = camel_case_map[self.name]
        else:
            # Use title() for regular names
            base_name = self.name.title().replace('_', '_')
        
        return f"{base_name}_Command"
    
    def get_queue_threshold_seconds(self) -> float:
        """Get threshold for queuing commands during global cooldown.
        
        Returns:
            float: Seconds remaining on cooldown below which commands should be queued.
        """
        section = self._derive_config_section_name()
        threshold = self.get_config_value(section, 'cooldown_queue_threshold_seconds',
                                         fallback=None, value_type='float')
        if threshold is None:
            # Fall back to global config
            threshold = self.bot.config.getfloat('Bot', 'cooldown_queue_threshold_seconds', 
                                                fallback=5.0)
        return max(0.0, min(threshold, self.cooldown_seconds))
    
    def _load_allowed_channels(self) -> Optional[List[str]]:
        """Load allowed channels from config.
        
        Config format: [CommandName_Command]
        channels = channel1,channel2,channel3
        
        Returns:
            Optional[List[str]]: 
                - None: Use global monitor_channels (default behavior)
                - Empty list []: Command disabled for all channels (only DMs)
                - List of channels: Command only works in these channels
        """
        # Derive section name from command name
        # Convert "sports" -> "Sports_Command", "greeter" -> "Greeter_Command", etc.
        # Handle camelCase names like "dadjoke" -> "DadJoke_Command"
        section_name = self._derive_config_section_name()
        
        # Try to get channels config
        channels_str = self.get_config_value(section_name, 'channels', fallback=None, value_type='str')
        
        if channels_str is None:
            return None  # Use global monitor_channels
        
        if channels_str.strip() == '':
            return []  # Disabled for all channels (DM only)
        
        # Parse comma-separated list
        channels = [ch.strip() for ch in channels_str.split(',') if ch.strip()]
        return channels if channels else None
    
    def is_channel_allowed(self, message: MeshMessage) -> bool:
        """Check if this command is allowed in the message's channel.
        
        Args:
            message: The message to check.
            
        Returns:
            bool: 
                - True if DM and command allows DMs (unless requires_dm is False, but that's separate)
                - True if channel is in allowed_channels (or None for global)
                - False otherwise
        """
        # DMs are always allowed (unless requires_dm is False, but that's checked separately)
        if message.is_dm:
            return True
        
        if not message.channel:
            return False
        
        # Normalize channel name for comparison (case-insensitive, preserve # prefix)
        message_channel_normalized = message.channel.lower().strip()
        
        # If no channel override, use global monitor_channels
        if self.allowed_channels is None:
            monitor_normalized = {ch.lower().strip() for ch in self.bot.command_manager.monitor_channels}
            return message_channel_normalized in monitor_normalized
        
        # If empty list, command is disabled for channels (DM only)
        if self.allowed_channels == []:
            return False
        
        # Normalize allowed channels for comparison (case-insensitive, preserve # prefix)
        allowed_normalized = {ch.lower().strip() for ch in self.allowed_channels}
        
        # Check if channel matches allowed list
        return message_channel_normalized in allowed_normalized
    
    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if this command can be executed with the given message.
        
        Checks channel permissions, DM requirements, cooldowns, and admin access.
        
        Args:
            message: The message to check execution for.
            skip_channel_check: If True, skip channel check (used when a parent
                command has already enforced its own channel override, e.g. wx delegating to gwx).
            
        Returns:
            bool: True if the command can be executed, False otherwise.
        """
        # Check channel access (standardized channel override)
        if not skip_channel_check and not self.is_channel_allowed(message):
            return False
        
        # Check if command requires DM and message is not DM
        if self.requires_dm and not message.is_dm:
            return False
        
        # Check cooldown (per-user if message has sender_id, otherwise global)
        if self.cooldown_seconds > 0:
            can_execute, _ = self.check_cooldown(message.sender_id if message.sender_id else None)
            if not can_execute:
                return False
        
        # Check admin ACL if this command requires admin access
        if self.requires_admin_access():
            if not self._check_admin_access(message):
                return False
        
        return True
    
    def get_metadata(self) -> Dict[str, Any]:
        """Get plugin metadata for discovery and registration.
        
        Returns:
            Dict[str, Any]: A dictionary containing metadata about the command.
        """
        return {
            'name': self.name,
            'keywords': self.keywords,
            'description': self.description,
            'requires_dm': self.requires_dm,
            'requires_internet': self.requires_internet,
            'cooldown_seconds': self.cooldown_seconds,
            'category': self.category,
            'class_name': self.__class__.__name__,
            'module_name': self.__class__.__module__
        }
    
    async def send_response(self, message: MeshMessage, content: str, skip_user_rate_limit: bool = False) -> bool:
        """Unified method for sending responses to users.
        
        Args:
            message: The message to respond to.
            content: The response content.
            skip_user_rate_limit: If True, skip the user rate limiter check (for automated responses).
            
        Returns:
            bool: True if the response was sent successfully, False otherwise.
        """
        try:
            # Use the command manager's send_response method to ensure response capture
            return await self.bot.command_manager.send_response(message, content, skip_user_rate_limit=skip_user_rate_limit)
        except Exception as e:
            self.logger.error(f"Failed to send response: {e}")
            return False
    
    def get_max_message_length(self, message: MeshMessage) -> int:
        """Calculate the maximum message length dynamically based on message type and bot username.
        
        Channel messages are formatted as "<username>: <message>", so:
        max_length = 150 - username_length - 2 (for ": ")
        
        DM messages don't have a username prefix, so:
        max_length = 150
        
        Args:
            message: The MeshMessage to calculate max length for.
            
        Returns:
            int: Maximum message length in characters.
        """
        # For DMs, no username prefix - full 150 characters available
        if message.is_dm:
            return 150
        
        # For channel messages, calculate based on bot username length
        # Try to get device username from meshcore first (actual radio username)
        username = None
        if hasattr(self.bot, 'meshcore') and self.bot.meshcore:
            try:
                if hasattr(self.bot.meshcore, 'self_info') and self.bot.meshcore.self_info:
                    self_info = self.bot.meshcore.self_info
                    # Try to get name from self_info (could be dict or object)
                    if isinstance(self_info, dict):
                        username = self_info.get('name') or self_info.get('user_name')
                    elif hasattr(self_info, 'name'):
                        username = self_info.name
                    elif hasattr(self_info, 'user_name'):
                        username = self_info.user_name
            except Exception as e:
                self.logger.debug(f"Could not get username from meshcore.self_info: {e}")
        
        # Fall back to bot_name from config if device username not available
        if not username:
            username = self.bot.config.get('Bot', 'bot_name', fallback='Bot')
        
        # Calculate max length: 150 - username_length - 2 (for ": ")
        max_length = 150 - len(str(username)) - 2
        
        # Ensure we don't return a negative or unreasonably small value
        # Minimum of 130 characters to ensure some functionality
        return max(130, max_length)
    
    def check_cooldown(self, user_id: Optional[str] = None) -> Tuple[bool, float]:
        """Check if user is on cooldown.
        
        Args:
            user_id: User ID to check cooldown for. If None, checks global cooldown.
        
        Returns:
            Tuple[bool, float]: A tuple containing:
                - can_execute: True if command can be executed, False otherwise.
                - remaining_seconds: Float representing seconds remaining on cooldown.
        """
        if self.cooldown_seconds <= 0:
            return True, 0.0
        
        import time
        
        if user_id:
            # Per-user cooldown
            last_exec = self._user_cooldowns.get(user_id, 0)
            elapsed = time.time() - last_exec
            remaining = self.cooldown_seconds - elapsed
            
            if remaining > 0:
                return False, remaining
            return True, 0.0
        else:
            # Global cooldown (backward compatibility)
            elapsed = time.time() - self._last_execution_time
            remaining = self.cooldown_seconds - elapsed
            
            if remaining > 0:
                return False, remaining
            return True, 0.0
    
    def record_execution(self, user_id: Optional[str] = None) -> None:
        """Record command execution for cooldown tracking.
        
        Args:
            user_id: User ID to record execution for. If None, records global execution.
        """
        import time
        current_time = time.time()
        
        if user_id:
            # Per-user cooldown
            self._user_cooldowns[user_id] = current_time
            
            # Clean up old entries periodically to prevent memory growth
            if len(self._user_cooldowns) > 1000:
                cutoff = current_time - (self.cooldown_seconds * 2)
                self._user_cooldowns = {
                    k: v for k, v in self._user_cooldowns.items() 
                    if v > cutoff
                }
        else:
            # Global cooldown (backward compatibility)
            self._last_execution_time = current_time
    
    def _record_execution(self, user_id: Optional[str] = None):
        """Record the execution time for cooldown tracking (backward compatibility).
        
        Args:
            user_id: User ID to record execution for. If None, records global execution.
        """
        self.record_execution(user_id)
    
    def get_remaining_cooldown(self, user_id: Optional[str] = None) -> int:
        """Get remaining cooldown time in seconds.
        
        Args:
            user_id: User ID to check cooldown for. If None, checks global cooldown.
        
        Returns:
            int: Remaining cooldown time in seconds (as integer).
        """
        _, remaining = self.check_cooldown(user_id)
        return max(0, int(remaining))
    
    def _load_translated_keywords(self):
        """Load translated keywords from translation files"""
        if not hasattr(self.bot, 'translator'):
            self.logger.debug(f"Translator not available for {self.name}, skipping keyword loading")
            return
        
        try:
            # Get translated keywords for this command
            key = f"keywords.{self.name}"
            translated_keywords = self.bot.translator.get_value(key)
            
            if translated_keywords and isinstance(translated_keywords, list):
                # Merge translated keywords with original keywords (avoid duplicates)
                original_count = len(self.keywords)
                all_keywords = list(self.keywords)  # Start with original
                for translated_keyword in translated_keywords:
                    if translated_keyword not in all_keywords:
                        all_keywords.append(translated_keyword)
                self.keywords = all_keywords
                added_count = len(self.keywords) - original_count
                if added_count > 0:
                    self.logger.debug(f"Loaded {added_count} translated keyword(s) for {self.name}: {self.keywords}")
            else:
                self.logger.debug(f"No translated keywords found for {self.name} (key: {key})")
        except Exception as e:
            # Log the error for debugging
            self.logger.debug(f"Could not load translated keywords for {self.name}: {e}")
    
    def _load_command_prefix(self) -> str:
        """Load command prefix from config.
        
        Returns:
            str: The command prefix, or empty string if not configured.
        """
        prefix = self.bot.config.get('Bot', 'command_prefix', fallback='')
        return prefix.strip() if prefix else ''
    
    def _get_bot_name(self) -> str:
        """Get bot name from device or config.
        
        Returns:
            str: The name of the bot/device.
        """
        # Try to get name from device first (actual radio username)
        if hasattr(self.bot, 'meshcore') and self.bot.meshcore:
            try:
                if hasattr(self.bot.meshcore, 'self_info') and self.bot.meshcore.self_info:
                    self_info = self.bot.meshcore.self_info
                    # Try to get name from self_info (could be dict or object)
                    if isinstance(self_info, dict):
                        device_name = self_info.get('name') or self_info.get('adv_name')
                        if device_name:
                            return device_name
                    elif hasattr(self_info, 'name'):
                        if self_info.name:
                            return self_info.name
                    elif hasattr(self_info, 'adv_name'):
                        if self_info.adv_name:
                            return self_info.adv_name
            except Exception as e:
                self.logger.debug(f"Could not get name from device: {e}")
        
        # Fallback to config
        bot_name = self.bot.config.get('Bot', 'bot_name', fallback='Bot')
        return bot_name
    
    def _extract_mentions(self, text: str) -> List[str]:
        """Extract all @[username] mentions from message content.
        
        Args:
            text: The message text to process.
            
        Returns:
            List[str]: List of mentioned usernames (without @[] brackets).
        """
        # Pattern to match @[username] - username can contain spaces, emojis, special chars
        pattern = r'@\[([^\]]+)\]'
        mentions = re.findall(pattern, text)
        return mentions
    
    def _is_bot_mentioned(self, text: str) -> bool:
        """Check if the bot is mentioned in the message.
        
        Args:
            text: The message text to check.
            
        Returns:
            bool: True if the bot is mentioned, False otherwise.
        """
        mentions = self._extract_mentions(text)
        if not mentions:
            return False
        
        bot_name = self._get_bot_name()
        bot_name_lower = bot_name.lower()
        
        # Check if any mention matches the bot name (case-insensitive)
        for mention in mentions:
            if mention.lower() == bot_name_lower:
                return True
        
        return False
    
    def _check_mentions_ok(self, text: str) -> bool:
        """Check if mentions are valid (bot is mentioned if any mentions exist).
        
        Args:
            text: The message text to check.
            
        Returns:
            bool: True if mentions are OK (no mentions, or bot is mentioned), False otherwise.
        """
        mentions = self._extract_mentions(text)
        if not mentions:
            # No mentions - always OK
            return True
        
        # If there are mentions, bot must be mentioned
        return self._is_bot_mentioned(text)
    
    def _strip_mentions(self, text: str) -> str:
        """Strip @[username] mentions from message content.
        
        Args:
            text: The message text to process.
            
        Returns:
            str: The text with mentions removed.
        """
        # Pattern to match @[username] - username can contain spaces, emojis, special chars
        # Match @[ followed by any characters until ]
        pattern = r'@\[([^\]]+)\]'
        # Remove all mentions and clean up extra whitespace
        cleaned = re.sub(pattern, '', text)
        # Clean up multiple spaces and strip
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if this command matches the message content based on keywords.
        
        Handles @[username] mentions: only responds if the bot is mentioned (if any mentions exist).
        If other users are mentioned but not the bot, returns False.
        
        Handles command prefix: if a prefix is configured, the message must start with it.
        
        Args:
            message: The message to check.
            
        Returns:
            bool: True if message matches a keyword and bot is mentioned (if any mentions exist), False otherwise.
        """
        if not self.keywords:
            return False
        
        content = message.content.strip()
        
        # Check for command prefix if configured
        if self._command_prefix:
            # If prefix is configured, message must start with it
            if not content.startswith(self._command_prefix):
                return False
            # Strip the prefix
            content = content[len(self._command_prefix):].strip()
        else:
            # If no prefix configured, strip legacy "!" prefix for backward compatibility
            if content.startswith('!'):
                content = content[1:].strip()
        
        # Check if mentions are valid (bot must be mentioned if any mentions exist)
        if not self._check_mentions_ok(content):
            return False
        
        # Strip @[username] mentions before checking keywords
        content = self._strip_mentions(content)
        content_lower = content.lower()
        
        for keyword in self.keywords:
            keyword_lower = keyword.lower()
            
            # Check for exact match first
            if keyword_lower == content_lower:
                return True
            
            # Check if the message starts with the keyword (followed by space or end of string)
            # This ensures the keyword is the first word in the message
            if content_lower.startswith(keyword_lower):
                # Check if it's followed by a space or is the end of the message
                if len(content_lower) == len(keyword_lower) or content_lower[len(keyword_lower)] == ' ':
                    return True
        
        return False
    
    def matches_custom_syntax(self, message: MeshMessage) -> bool:
        """Check if this command matches custom syntax patterns.
        
        Handles @[username] mentions: only responds if the bot is mentioned (if any mentions exist).
        Subclasses should call super().matches_custom_syntax() first if they override this method.
        
        Args:
            message: The message to check.
            
        Returns:
            bool: True if custom syntax matches and bot is mentioned (if any mentions exist), False otherwise.
        """
        content = message.content.strip()
        
        # Check if mentions are valid (bot must be mentioned if any mentions exist)
        if not self._check_mentions_ok(content):
            return False
        
        # Subclasses should override this method for custom syntax matching
        # This base implementation just checks mentions
        return False
    
    def should_execute(self, message: MeshMessage) -> bool:
        """Check if this command should execute for the given message"""
        # First check if keyword matches
        if not (self.matches_keyword(message) or self.matches_custom_syntax(message)):
            return False
        
        # For DM-only commands, only consider them if:
        # 1. Message is a DM, OR
        # 2. Channel is in allowed_channels (or monitor_channels if no override)
        # This prevents DM-only commands from being processed in public channels
        if self.requires_dm and not message.is_dm:
            # Check if channel is allowed for this command
            if not self.is_channel_allowed(message):
                # Channel not allowed - don't even consider this command
                return False
        
        return True
    
    def can_execute_now(self, message: MeshMessage) -> bool:
        """Check if this command can execute right now (permissions, cooldown, etc.)"""
        return self.can_execute(message)
    
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
    
    def format_timestamp(self, message: MeshMessage) -> str:
        """Format current bot time for display (not sender's timestamp to avoid clock issues)"""
        try:
            # Get configured timezone or use system timezone
            timezone_str = self.bot.config.get('Bot', 'timezone', fallback='')
            
            if timezone_str:
                try:
                    # Use configured timezone
                    tz = pytz.timezone(timezone_str)
                    dt = datetime.now(tz)
                except pytz.exceptions.UnknownTimeZoneError:
                    # Fallback to system timezone if configured timezone is invalid
                    dt = datetime.now()
            else:
                # Use system timezone
                dt = datetime.now()
            
            return dt.strftime("%H:%M:%S")
        except:
            return "Unknown"
    
    def format_elapsed(self, message: MeshMessage) -> str:
        """Format message elapsed for display. Uses 'Sync Device Clock' when device clock is invalid."""
        translator = getattr(self.bot, 'translator', None)
        return format_elapsed_display(message.timestamp, translator)

    def format_response(self, message: MeshMessage, response_format: str) -> str:
        """Format a response string with message data"""
        try:
            connection_info = self.build_enhanced_connection_info(message)
            timestamp = self.format_timestamp(message)
            
            return response_format.format(
                sender=message.sender_id or "Unknown",
                connection_info=connection_info,
                path=message.path or "Unknown",
                timestamp=timestamp,
                snr=message.snr or "Unknown",
                rssi=message.rssi or "Unknown"
            )
        except (KeyError, ValueError) as e:
            self.logger.warning(f"Error formatting response: {e}")
            return response_format
    
    def get_response_format(self) -> Optional[str]:
        """Get the response format for this command from config"""
        # Override in subclasses to provide custom response formats
        return None
    
    def requires_admin_access(self) -> bool:
        """Check if this command requires admin access"""
        if not hasattr(self.bot, 'config') or not self.bot.config.has_section('Admin_ACL'):
            return False
        
        try:
            # Get list of admin commands from config
            admin_commands = self.bot.config.get('Admin_ACL', 'admin_commands', fallback='')
            if not admin_commands:
                return False
            
            # Check if this command name is in the admin commands list
            admin_command_list = [cmd.strip() for cmd in admin_commands.split(',') if cmd.strip()]
            return self.name in admin_command_list
        except Exception as e:
            self.logger.warning(f"Error checking admin access requirement: {e}")
            return False
    
    def _check_admin_access(self, message: MeshMessage) -> bool:
        """
        Check if the message sender has admin access (security-hardened)
        
        Security features:
        - Strict pubkey format validation (64-char hex)
        - No fallback to sender_id (prevents spoofing)
        - Whitespace/empty config detection
        - Normalized comparison (lowercase)
        - Uses centralized validate_pubkey_format() function
        """
        import re # This import is needed for re.match
        if not hasattr(self.bot, 'config') or not self.bot.config.has_section('Admin_ACL'):
            return False
        
        try:
            # Get admin pubkeys from config
            admin_pubkeys = self.bot.config.get('Admin_ACL', 'admin_pubkeys', fallback='')
            
            # Check for empty or whitespace-only configuration
            if not admin_pubkeys.strip():
                self.logger.warning("No admin pubkeys configured or empty/whitespace config")
                return False
            
            # Parse and VALIDATE admin pubkeys
            admin_pubkey_list = []
            for key in admin_pubkeys.split(','):
                key = key.strip()
                if not key:
                    continue
                
                # Validate hex format (64 chars for ed25519 public keys)
                if not validate_pubkey_format(key, expected_length=64):
                    self.logger.error(f"Invalid admin pubkey format in config: {key[:16]}...")
                    continue  # Skip invalid keys but continue checking others
                
                admin_pubkey_list.append(key.lower())  # Normalize to lowercase
            
            if not admin_pubkey_list:
                self.logger.error("No valid admin pubkeys found in config after validation")
                return False
            
            # Get sender's public key - NEVER fall back to sender_id
            sender_pubkey = getattr(message, 'sender_pubkey', None)
            if not sender_pubkey:
                self.logger.warning(
                    f"No sender public key available for {message.sender_id} - "
                    "admin access denied (missing pubkey)"
                )
                return False
            
            # Validate sender pubkey format
            if not validate_pubkey_format(sender_pubkey, expected_length=64):
                self.logger.warning(
                    f"Invalid sender pubkey format from {message.sender_id}: "
                    f"{sender_pubkey[:16]}... - admin access denied"
                )
                return False
            
            # Normalize and compare
            sender_pubkey_normalized = sender_pubkey.lower()
            is_admin = sender_pubkey_normalized in admin_pubkey_list
            
            if not is_admin:
                self.logger.warning(
                    f"Access denied for {message.sender_id} "
                    f"(pubkey: {sender_pubkey[:16]}...) - not in admin ACL"
                )
            else:
                self.logger.info(
                    f"Admin access granted for {message.sender_id} "
                    f"(pubkey: {sender_pubkey[:16]}...)"
                )
            
            return is_admin
            
        except Exception as e:
            self.logger.error(f"Error checking admin access: {e}")
            return False  # Fail securely
    
    def _strip_quotes_from_config(self, value: str) -> str:
        """Strip quotes from config values if present"""
        if value and value.startswith('"') and value.endswith('"'):
            return value[1:-1]
        return value
    
    async def handle_keyword_match(self, message: MeshMessage) -> bool:
        """Handle keyword matching and response generation"""
        response_format = self.get_response_format()
        if response_format:
            response = self.format_response(message, response_format)
            return await self.send_response(message, response)
        else:
            # No response format configured - don't respond
            # This prevents recursion and allows disabling commands by commenting them out in config
            return False
