#!/usr/bin/env python3
"""
Command management functionality for the MeshCore Bot
Handles all bot commands, keyword matching, and response generation
"""

import re
import time
import asyncio
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional, Any
from datetime import datetime
import pytz
from meshcore import EventType

from .models import MeshMessage
from .plugin_loader import PluginLoader
from .commands.base_command import BaseCommand
from .utils import check_internet_connectivity_async, format_keyword_response_with_placeholders


@dataclass
class InternetStatusCache:
    """Thread-safe cache for internet connectivity status.
    
    Attributes:
        has_internet: Boolean indicating if internet is available.
        timestamp: Timestamp of the last check.
        _lock: Asyncio lock for thread-safe operations (lazily initialized).
    """
    has_internet: bool
    timestamp: float
    _lock: Optional[asyncio.Lock] = None
    
    def _get_lock(self) -> asyncio.Lock:
        """Lazily initialize the async lock.
        
        Creates the lock only when first needed in an async context,
        preventing RuntimeError when instantiated before event loop is running.
        
        Returns:
            asyncio.Lock: The lock instance.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock
    
    def is_valid(self, cache_duration: float) -> bool:
        """Check if cache entry is still valid.
        
        Args:
            cache_duration: Duration in seconds for which the cache is valid.
            
        Returns:
            bool: True if the cache is still valid, False otherwise.
        """
        return time.time() - self.timestamp < cache_duration


@dataclass
class QueuedCommand:
    """Represents a queued command waiting for cooldown to expire."""
    command: BaseCommand
    message: MeshMessage
    queued_at: float
    expires_at: float  # When cooldown expires


class CommandManager:
    """Manages all bot commands and responses using dynamic plugin loading.
    
    This class handles loading commands from plugins, matching messages against
    commands and keywords, checking permissions and rate limits, and executing
    command logic. It also manages channel monitoring and banned users.
    """
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        
        # Load configuration
        self.keywords = self.load_keywords()
        self.custom_syntax = self.load_custom_syntax()
        self.banned_users = self.load_banned_users()
        self.monitor_channels = self.load_monitor_channels()
        self.command_prefix = self.load_command_prefix()
        
        # Initialize plugin loader and load all plugins
        self.plugin_loader = PluginLoader(bot)
        self.commands = self.plugin_loader.load_all_plugins()
        
        # Cache for internet connectivity status to avoid checking on every command
        # Thread-safe cache with asyncio.Lock
        self._internet_cache = InternetStatusCache(has_internet=True, timestamp=0)
        self._internet_cache_duration = 30  # Cache for 30 seconds
        
        # Command queue for near-expiring global cooldowns
        # Key: (command_name, user_id) tuple, Value: QueuedCommand
        self._command_queue: Dict[Tuple[str, str], QueuedCommand] = {}
        self._queue_processor_task: Optional[asyncio.Task] = None
        
        self.logger.info(f"CommandManager initialized with {len(self.commands)} plugins")
    
    def _should_queue_command(self, command: BaseCommand, message: MeshMessage) -> Tuple[bool, float]:
        """Check if command should be queued instead of rejected.
        
        Only queues for global cooldowns when near expiring, and only if the user
        didn't just execute the command themselves.
        
        Args:
            command: The command to check.
            message: The message triggering the command.
            
        Returns:
            Tuple[bool, float]: (should_queue, remaining_seconds)
                should_queue: True if command should be queued
                remaining_seconds: Seconds until cooldown expires (0 if not queuing)
        """
        # Only queue for global cooldowns (not per-user)
        if not message.sender_id:
            return False, 0.0
        
        if command.cooldown_seconds <= 0:
            return False, 0.0
        
        # Check global cooldown
        can_execute, remaining = command.check_cooldown(None)  # None = global
        if can_execute:
            return False, 0.0
        
        # Don't queue if this user just executed the command
        # Check if user has a recent per-user cooldown entry
        if message.sender_id in command._user_cooldowns:
            user_last_exec = command._user_cooldowns[message.sender_id]
            time_since_user_exec = time.time() - user_last_exec
            
            # If user executed within last 3 seconds, they likely just triggered the global cooldown
            # Don't queue in this case
            if time_since_user_exec < 3.0:
                return False, 0.0
        
        # Check if within queue threshold
        threshold = command.get_queue_threshold_seconds()
        if remaining <= threshold:
            return True, remaining
        
        return False, 0.0
    
    def _queue_command(self, command: BaseCommand, message: MeshMessage, remaining_seconds: float) -> bool:
        """Queue a command for execution after cooldown expires.
        
        Args:
            command: The command to queue.
            message: The message to queue.
            remaining_seconds: Seconds until cooldown expires.
            
        Returns:
            bool: True if queued, False if user already has queued command
        """
        user_id = message.sender_id or 'global'
        queue_key = (command.name, user_id)
        
        # Max 1 command per user
        if queue_key in self._command_queue:
            return False
        
        current_time = time.time()
        self._command_queue[queue_key] = QueuedCommand(
            command=command,
            message=message,
            queued_at=current_time,
            expires_at=current_time + remaining_seconds
        )
        
        self.logger.debug(f"Queued command '{command.name}' for user {user_id}, "
                         f"expires in {remaining_seconds:.1f}s")
        
        # Start processor if not running
        if self._queue_processor_task is None or self._queue_processor_task.done():
            self._start_queue_processor()
        
        return True
    
    def _start_queue_processor(self):
        """Start background task to process command queue."""
        if hasattr(self.bot, 'main_event_loop') and self.bot.main_event_loop:
            self._queue_processor_task = asyncio.create_task(self._process_command_queue())
        else:
            # Bot not fully started yet, will start in bot.start()
            pass
    
    async def _process_command_queue(self):
        """Background task to process queued commands when cooldown expires."""
        while True:
            try:
                current_time = time.time()
                ready_commands = []
                
                # Find commands ready to execute
                for queue_key, queued_cmd in list(self._command_queue.items()):
                    if current_time >= queued_cmd.expires_at:
                        ready_commands.append((queue_key, queued_cmd))
                
                # Execute ready commands
                for queue_key, queued_cmd in ready_commands:
                    command = queued_cmd.command
                    message = queued_cmd.message
                    del self._command_queue[queue_key]
                    
                    self.logger.debug(f"Executing queued command '{command.name}' for user {message.sender_id}")
                    
                    # Record execution to prevent immediate re-queuing
                    command.record_execution(message.sender_id if message.sender_id else None)
                    
                    # Execute the command (bypass normal flow)
                    try:
                        await self._execute_queued_command(command, message)
                    except Exception as e:
                        self.logger.error(f"Error executing queued command '{command.name}': {e}", 
                                        exc_info=True)
                
                # Wait before next check
                if ready_commands:
                    await asyncio.sleep(0.1)  # Small delay between executions
                else:
                    await asyncio.sleep(0.5)  # Check every 500ms when idle
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in command queue processor: {e}", exc_info=True)
                await asyncio.sleep(1.0)
    
    async def _execute_queued_command(self, command: BaseCommand, message: MeshMessage):
        """Execute a queued command (bypasses normal cooldown checks).
        
        Args:
            command: The command to execute.
            message: The queued message.
        """
        # Execute directly
        success = await command.execute(message)
        
        # Record in stats
        if 'stats' in self.commands:
            stats_command = self.commands['stats']
            if stats_command:
                stats_command.record_command(message, command.name, success)
    
    async def _apply_tx_delay(self):
        """Apply transmission delay to prevent message collisions"""
        if self.bot.tx_delay_ms > 0:
            self.logger.debug(f"Applying {self.bot.tx_delay_ms}ms transmission delay")
            await asyncio.sleep(self.bot.tx_delay_ms / 1000.0)
    
    async def _check_rate_limits(self, skip_user_rate_limit: bool = False) -> Tuple[bool, str]:
        """Check all rate limits before sending.
        
        Checks both the user-specific rate limits and the global bot transmission
        limits. Also applies transmission delays if configured.
        
        Args:
            skip_user_rate_limit: If True, skip the user rate limiter check (for automated responses).
        
        Returns:
            Tuple[bool, str]: A tuple containing:
                - can_send: True if the message can be sent, False otherwise.
                - reason: Reason string if rate limited, empty string otherwise.
        """
        # Check user rate limiter (unless skipped for automated responses)
        if not skip_user_rate_limit:
            if not self.bot.rate_limiter.can_send():
                wait_time = self.bot.rate_limiter.time_until_next()
                # Only log warning if there's a meaningful wait time (> 0.1 seconds)
                # This avoids misleading "Wait 0.0 seconds" messages from timing edge cases
                if wait_time > 0.1:
                    return False, f"Rate limited. Wait {wait_time:.1f} seconds"
                return False, ""  # Still rate limited, just don't log for very short waits
        
        # Wait for bot TX rate limiter
        await self.bot.bot_tx_rate_limiter.wait_for_tx()
        
        # Apply transmission delay
        await self._apply_tx_delay()
        
        return True, ""
    
    def _handle_send_result(self, result, operation_name: str, target: str, used_retry_method: bool = False) -> bool:
        """Handle result from message send operations.
        
        Args:
            result: Result object from meshcore send operation.
            operation_name: Name of the operation ("DM" or "Channel message").
            target: Recipient name or channel name for logging.
            used_retry_method: True if send_msg_with_retry was used (affects logging).
        
        Returns:
            bool: True if send succeeded (ACK received or sent successfully), False otherwise.
        """
        if not result:
            if used_retry_method:
                self.logger.error(f"❌ {operation_name} to {target} failed - no ACK received after retries")
            else:
                self.logger.error(f"❌ {operation_name} to {target} failed - no result returned")
            return False
        
        if hasattr(result, 'type'):
            if result.type == EventType.ERROR:
                error_payload = result.payload if hasattr(result, 'payload') else {}
                self.logger.error(f"❌ {operation_name} failed to {target}: {error_payload if error_payload else 'Unknown error'}")
                return False
            
            if result.type in (EventType.MSG_SENT, EventType.OK):
                if used_retry_method and operation_name == "DM":
                    self.logger.info(f"✅ {operation_name} sent and ACK received from {target}")
                else:
                    self.logger.info(f"✅ {operation_name} sent to {target}")
                self.bot.rate_limiter.record_send()
                self.bot.bot_tx_rate_limiter.record_tx()
                return True
            
            # Handle unexpected event types
            event_name = getattr(result.type, 'name', str(result.type))
            
            # Special handling for channel messages with timeout/no_event_received
            if operation_name == "Channel message":
                error_payload = result.payload if hasattr(result, 'payload') else {}
                if isinstance(error_payload, dict) and error_payload.get('reason') == 'no_event_received':
                    # Message likely sent but confirmation timed out - treat as success with warning
                    self.logger.warning(f"Channel message sent to {target} but confirmation event not received (message may have been sent)")
                    self.bot.rate_limiter.record_send()
                    self.bot.bot_tx_rate_limiter.record_tx()
                    return True
            
            # Unknown event type - log warning
            self.logger.warning(f"{operation_name} to {target}: unexpected event type {event_name}")
            return False
        
        # Assume success if result exists but has no type attribute
        self.logger.info(f"✅ {operation_name} sent to {target} (result: {result})")
        self.bot.rate_limiter.record_send()
        self.bot.bot_tx_rate_limiter.record_tx()
        return True
    
    def _decode_escape_sequences(self, text: str) -> str:
        """Decode escape sequences in config strings.
        
        Processes common escape sequences like \\n (newline), \\t (tab), \\\\ (backslash).
        This allows users to add newlines in keyword responses using \n (single backslash).
        
        Behavior:
        - \n in config file → newline character
        - \\n in config file → literal backslash + n
        
        Args:
            text: The text string to process.
            
        Returns:
            str: The text with escape sequences decoded.
        """
        # Replace escape sequences
        # Order matters: \\ must be processed first to avoid double-processing
        # This preserves literal backslashes (\\n becomes \n, not a newline)
        text = text.replace('\\\\', '\x00')  # Temporary placeholder for backslash
        text = text.replace('\\n', '\n')     # Newline
        text = text.replace('\\t', '\t')     # Tab
        text = text.replace('\\r', '\r')     # Carriage return
        text = text.replace('\x00', '\\')    # Restore backslash
        return text
    
    def load_keywords(self) -> Dict[str, str]:
        """Load keywords from config.
        
        Returns:
            Dict[str, str]: Dictionary mapping keywords to response strings.
        """
        keywords = {}
        if self.bot.config.has_section('Keywords'):
            for keyword, response in self.bot.config.items('Keywords'):
                # Strip quotes from the response if present
                if response.startswith('"') and response.endswith('"'):
                    response = response[1:-1]
                # Decode escape sequences (e.g., \\n for newlines)
                response = self._decode_escape_sequences(response)
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
                # Decode escape sequences (e.g., \\n for newlines)
                response_format = self._decode_escape_sequences(response_format)
                syntax_patterns[pattern] = response_format
        return syntax_patterns
    
    def load_banned_users(self) -> List[str]:
        """Load banned users from config"""
        banned = self.bot.config.get('Banned_Users', 'banned_users', fallback='')
        return [user.strip() for user in banned.split(',') if user.strip()]
    
    def is_user_banned(self, sender_id: Optional[str]) -> bool:
        """Check if sender is banned using prefix (starts-with) matching.
        
        A banned entry "Awful Username" matches "Awful Username" and "Awful Username 🍆".
        """
        if not sender_id:
            return False
        return any(sender_id.startswith(entry) for entry in self.banned_users)
    
    def load_monitor_channels(self) -> List[str]:
        """Load monitored channels from config"""
        channels = self.bot.config.get('Channels', 'monitor_channels', fallback='')
        return [channel.strip() for channel in channels.split(',') if channel.strip()]
    
    def load_command_prefix(self) -> str:
        """Load command prefix from config.
        
        Returns:
            str: The command prefix, or empty string if not configured.
        """
        prefix = self.bot.config.get('Bot', 'command_prefix', fallback='')
        return prefix.strip() if prefix else ''
    
    def format_keyword_response(self, response_format: str, message: MeshMessage) -> str:
        """Format a keyword response string with message data.
        
        Args:
            response_format: The response string format with placeholders.
            message: The message object containing context for placeholders.
            
        Returns:
            str: The formatted response string.
        """
        # Use shared formatting function from utils
        return format_keyword_response_with_placeholders(
            response_format,
            message,
            self.bot,
            mesh_info=None  # Keywords don't use mesh info placeholders
        )
    
    def check_keywords(self, message: MeshMessage) -> List[tuple]:
        """Check message content for keywords and return matching responses.
        
        Evaluates the message against configured keywords, custom syntax patterns,
        and command triggers.
        
        Args:
            message: The incoming message to check.
            
        Returns:
            List[tuple]: List of (trigger, response) tuples for matched keywords.
        """
        matches = []
        content = message.content.strip()
        
        # Check for command prefix if configured
        if self.command_prefix:
            # If prefix is configured, message must start with it
            if not content.startswith(self.command_prefix):
                return matches  # No prefix, no match
            # Strip the prefix
            content = content[len(self.command_prefix):].strip()
        else:
            # If no prefix configured, strip legacy "!" prefix for backward compatibility
            if content.startswith('!'):
                content = content[1:].strip()
        
        content_lower = content.lower()
        
        # Check for help requests first (special handling)
        # Check both English "help" and translated help keywords
        help_keywords = ['help']
        if 'help' in self.commands:
            help_command = self.commands['help']
            if hasattr(help_command, 'keywords'):
                help_keywords = [k.lower() for k in help_command.keywords]
        
        # Check if message starts with any help keyword
        for help_keyword in help_keywords:
            if content_lower.startswith(help_keyword + ' ') or content_lower == help_keyword:
                # Check channel restrictions for help keyword (same as other keywords/commands)
                # DMs are allowed if respond_to_dms is enabled
                if message.is_dm:
                    if not self.bot.config.getboolean('Channels', 'respond_to_dms', fallback=True):
                        break  # DMs disabled, skip help keyword
                else:
                    # For channel messages, check if channel is in monitor_channels
                    if message.channel not in self.monitor_channels:
                        break  # Channel not monitored, skip help keyword
                
                # Channel check passed, process help request
                if content_lower.startswith(help_keyword + ' '):
                    command_name = content_lower[len(help_keyword):].strip()  # Remove help keyword prefix
                    help_text = self.get_help_for_command(command_name, message)
                    # Format the help response with message data (same as other keywords)
                    help_text = self.format_keyword_response(help_text, message)
                    matches.append(('help', help_text))
                    return matches
                elif content_lower == help_keyword:
                    help_text = self.get_general_help()
                    # Format the help response with message data (same as other keywords)
                    help_text = self.format_keyword_response(help_text, message)
                    matches.append(('help', help_text))
                    return matches
        
        # Check all loaded plugins for matches
        for command_name, command in self.commands.items():
            if command.should_execute(message):
                # Check if we should queue instead of skip (for global cooldowns near expiring)
                should_queue, remaining = self._should_queue_command(command, message)
                if should_queue:
                    if self._queue_command(command, message, remaining):
                        continue  # Silently queue, don't add to matches
                    # Queue failed, fall through to normal check
                
                # Check if command can execute (includes channel access check)
                if not command.can_execute(message):
                    continue  # Skip this command if it can't execute (wrong channel, cooldown, etc.)
                
                # Check network connectivity for commands that require internet
                if command.requires_internet:
                    has_internet = self._check_internet_cached()
                    if not has_internet:
                        self.logger.warning(f"Command '{command_name}' requires internet but network is unavailable")
                        # Skip this command - don't add to matches
                        continue
                
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
            if any(keyword.lower() in [k.lower() for k in cmd.keywords] for cmd in self.commands.values()):
                continue
            
            # Check channel restrictions for plain keywords (same as commands)
            # DMs are allowed if respond_to_dms is enabled
            if message.is_dm:
                if not self.bot.config.getboolean('Channels', 'respond_to_dms', fallback=True):
                    continue  # DMs disabled, skip this keyword
            else:
                # For channel messages, check if channel is in monitor_channels
                if message.channel not in self.monitor_channels:
                    continue  # Channel not monitored, skip this keyword
            
            keyword_lower = keyword.lower()
            
            # Check for exact match first
            if keyword_lower == content_lower:
                try:
                    # Format the response with available message data
                    response = self.format_keyword_response(response_format, message)
                    matches.append((keyword, response))
                except Exception as e:
                    # Fallback to simple response if formatting fails
                    self.logger.warning(f"Error formatting response for '{keyword}': {e}")
                    matches.append((keyword, response_format))
            # Check if the message starts with the keyword (followed by space or end of string)
            # This ensures the keyword is the first word in the message
            elif content_lower.startswith(keyword_lower):
                # Check if it's followed by a space or is the end of the message
                if len(content_lower) == len(keyword_lower) or content_lower[len(keyword_lower)] == ' ':
                    try:
                        # Format the response with available message data
                        response = self.format_keyword_response(response_format, message)
                        matches.append((keyword, response))
                    except Exception as e:
                        # Fallback to simple response if formatting fails
                        self.logger.warning(f"Error formatting response for '{keyword}': {e}")
                        matches.append((keyword, response_format))
        
        return matches
    
    async def handle_advert_command(self, message: MeshMessage):
        """Handle the advert command from DM.
        
        Executes the advert command specifically, ensuring proper stat recording
        and response handling.
        
        Args:
            message: The message triggering the advert command.
        """
        command = self.commands['advert']
        success = await command.execute(message)
        
        # Small delay to ensure send_response has completed
        await asyncio.sleep(0.1)
        
        # Determine if a response was sent
        response_sent = False
        if hasattr(command, 'last_response') and command.last_response:
            response_sent = True
        elif hasattr(self, '_last_response') and self._last_response:
            response_sent = True
        
        # Record command execution in stats database
        if 'stats' in self.commands:
            stats_command = self.commands['stats']
            if stats_command:
                stats_command.record_command(message, 'advert', response_sent)
    
    async def send_dm(self, recipient_id: str, content: str, command_id: Optional[str] = None, skip_user_rate_limit: bool = False) -> bool:
        """Send a direct message using meshcore-cli command.
        
        Handles contact lookup, rate limiting, and uses retry logic if available.
        
        Args:
            recipient_id: The recipient's name or ID.
            content: The message content to send.
            command_id: Optional command_id for repeat tracking (if not provided, one will be generated).
            
        Returns:
            bool: True if sent successfully, False otherwise.
        """
        if not self.bot.connected or not self.bot.meshcore:
            return False
        
        # Check all rate limits
        can_send, reason = await self._check_rate_limits(skip_user_rate_limit=skip_user_rate_limit)
        if not can_send:
            if reason:
                self.logger.warning(reason)
            return False
        
        try:
            # Find the contact by name (since recipient_id is the contact name)
            contact = self.bot.meshcore.get_contact_by_name(recipient_id)
            if not contact:
                self.logger.error(f"Contact not found for name: {recipient_id}")
                return False
            
            # Use the contact name for logging
            contact_name = contact.get('name', contact.get('adv_name', recipient_id))
            self.logger.info(f"Sending DM to {contact_name}: {content}")
            
            # Record transmission for repeat tracking (don't let this block sending)
            try:
                if hasattr(self.bot, 'transmission_tracker') and self.bot.transmission_tracker:
                    if not command_id:
                        command_id = f"dm_{contact_name}_{int(time.time())}"
                    self.bot.transmission_tracker.record_transmission(
                        content=content,
                        target=contact_name,
                        message_type='dm',
                        command_id=command_id
                    )
            except Exception as e:
                self.logger.debug(f"Error recording transmission for repeat tracking: {e}")
                # Don't fail the send if transmission tracking fails
            
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
            
            # Check if send_msg_with_retry was used
            used_retry_method = (hasattr(self.bot.meshcore, 'commands') and 
                               hasattr(self.bot.meshcore.commands, 'send_msg_with_retry'))
            
            # Handle result using unified handler
            return self._handle_send_result(result, "DM", contact_name, used_retry_method)
                
        except Exception as e:
            self.logger.error(f"Failed to send DM: {e}")
            return False
    
    async def send_channel_message(self, channel: str, content: str, command_id: Optional[str] = None, skip_user_rate_limit: bool = False) -> bool:
        """Send a channel message using meshcore-cli command.
        
        Resolves channel names to numbers and handles rate limiting.
        
        Args:
            channel: The channel name (e.g., "LongFast").
            content: The message content to send.
            command_id: Optional command_id for repeat tracking (if not provided, one will be generated).
            
        Returns:
            bool: True if sent successfully, False otherwise.
        """
        if not self.bot.connected or not self.bot.meshcore:
            return False
        
        # Check all rate limits
        can_send, reason = await self._check_rate_limits(skip_user_rate_limit=skip_user_rate_limit)
        if not can_send:
            if reason:
                self.logger.warning(reason)
            return False
        
        try:
            # Get channel number from channel name
            channel_num = self.bot.channel_manager.get_channel_number(channel)
            
            # Check if channel was found (None indicates channel name not found)
            if channel_num is None:
                self.logger.error(f"Channel '{channel}' not found. Cannot send message.")
                return False
            
            self.logger.info(f"Sending channel message to {channel} (channel {channel_num}): {content}")
            
            # Record transmission for repeat tracking (don't let this block sending)
            try:
                if hasattr(self.bot, 'transmission_tracker') and self.bot.transmission_tracker:
                    if not command_id:
                        command_id = f"channel_{channel}_{int(time.time())}"
                    self.bot.transmission_tracker.record_transmission(
                        content=content,
                        target=channel,
                        message_type='channel',
                        command_id=command_id
                    )
            except Exception as e:
                self.logger.debug(f"Error recording transmission for repeat tracking: {e}")
                # Don't fail the send if transmission tracking fails
            
            # Use meshcore-cli send_chan_msg function
            from meshcore_cli.meshcore_cli import send_chan_msg
            result = await send_chan_msg(self.bot.meshcore, channel_num, content)
            
            # Handle result using unified handler
            target = f"{channel} (channel {channel_num})"
            return self._handle_send_result(result, "Channel message", target)
                
        except Exception as e:
            self.logger.error(f"Failed to send channel message: {e}")
            return False
    
    def get_help_for_command(self, command_name: str, message: MeshMessage = None) -> str:
        """Get help text for a specific command (LoRa-friendly compact format).
        
        Args:
            command_name: The name of the command to retrieve help for.
            message: Optional message object for context-aware help (e.g. translated).
            
        Returns:
            str: The help text for the command.
        """
        # Special handling for common help requests
        if command_name.lower() in ['commands', 'list', 'all']:
            # User is asking for a list of commands, show general help
            return self.get_general_help()
        
        # Map command aliases to their actual command names
        command_aliases = {
            't': 't_phrase',
            'advert': 'advert',
            'test': 'test',
            'ping': 'ping',
            'help': 'help'
        }
        
        # Normalize the command name using aliases
        normalized_name = command_aliases.get(command_name, command_name)
        
        # First, try to find a command by exact name
        command = self.commands.get(normalized_name)
        if command:
            # Try to pass message context to get_help_text if supported
            try:
                help_text = command.get_help_text(message)
            except TypeError:
                # Fallback for commands that don't accept message parameter
                help_text = command.get_help_text()
            # Use translator if available
            if hasattr(self.bot, 'translator'):
                return self.bot.translator.translate('commands.help.specific', command=command_name, help_text=help_text)
            return f"Help {command_name}: {help_text}"
        
        # If not found, search through all commands and their keywords
        for cmd_name, cmd_instance in self.commands.items():
            # Check if the requested command name matches any of this command's keywords
            if hasattr(cmd_instance, 'keywords') and command_name in cmd_instance.keywords:
                # Try to pass message context to get_help_text if supported
                try:
                    help_text = cmd_instance.get_help_text(message)
                except TypeError:
                    # Fallback for commands that don't accept message parameter
                    help_text = cmd_instance.get_help_text()
                # Use translator if available
                if hasattr(self.bot, 'translator'):
                    return self.bot.translator.translate('commands.help.specific', command=command_name, help_text=help_text)
                return f"Help {command_name}: {help_text}"
        
        # If still not found, return unknown command message with helpful suggestion
        # Use the help command's method to get popular commands (only primary names, no aliases)
        available_str = ""
        if 'help' in self.commands:
            help_command = self.commands['help']
            if hasattr(help_command, 'get_available_commands_list'):
                available_str = help_command.get_available_commands_list()
        
        # Fallback if help command doesn't have the method
        if not available_str:
            # Only show primary command names, not keywords
            primary_names = sorted([
                cmd.name if hasattr(cmd, 'name') else name
                for name, cmd in self.commands.items()
            ])
            available_str = ', '.join(primary_names)
        
        if hasattr(self.bot, 'translator'):
            return self.bot.translator.translate('commands.help.unknown', command=command_name, available=available_str)
        return f"Unknown: {command_name}. Available: {available_str}. Try 'help' for command list."
    
    def get_general_help(self) -> str:
        """Get general help text from config (LoRa-friendly compact format)"""
        # Get the help response from the keywords config
        return self.keywords.get('help', 'Help not configured')
    
    def get_available_commands_list(self) -> str:
        """Get a formatted list of available commands"""
        commands_list = ""
        
        # Group commands by category
        basic_commands = ['test', 'ping', 'help', 'cmd']
        custom_syntax = ['t_phrase']  # Use the actual command key
        special_commands = ['advert']
        weather_commands = ['wx', 'aqi']
        solar_commands = ['sun', 'moon', 'solar', 'hfcond', 'satpass']
        sports_commands = ['sports']
        
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
        
        commands_list += "\n**Sports Commands:**\n"
        for cmd in sports_commands:
            if cmd in self.commands:
                help_text = self.commands[cmd].get_help_text()
                commands_list += f"• `{cmd}` - {help_text}\n"
        
        return commands_list
    
    async def send_response(self, message: MeshMessage, content: str, skip_user_rate_limit: bool = False) -> bool:
        """Unified method for sending responses to users.
        
        Automatically determines whether to send a DM or channel message based
        on the incoming message type.
        
        Args:
            message: The original message being responded to.
            content: The response content.
            skip_user_rate_limit: If True, skip the user rate limiter check (for automated responses).
            
        Returns:
            bool: True if response was sent successfully, False otherwise.
        """
        try:
            # Store the response content for web viewer capture
            if hasattr(self, '_last_response'):
                self._last_response = content
            else:
                self._last_response = content
            
            if message.is_dm:
                return await self.send_dm(message.sender_id, content, skip_user_rate_limit=skip_user_rate_limit)
            else:
                return await self.send_channel_message(message.channel, content, skip_user_rate_limit=skip_user_rate_limit)
        except Exception as e:
            self.logger.error(f"Failed to send response: {e}")
            return False
    
    async def execute_commands(self, message):
        """Execute command objects that handle their own responses.
        
        Identifies and executes commands that were not handled by simple keyword
        matching, managing permissions, internet checks, and error handling.
        
        Args:
            message: The message triggering the command execution.
        """
        content = message.content.strip()
        
        # Check for command prefix if configured
        if self.command_prefix:
            # If prefix is configured, message must start with it
            if not content.startswith(self.command_prefix):
                return  # No prefix, no match
            # Strip the prefix
            content = content[len(self.command_prefix):].strip()
        else:
            # If no prefix configured, strip legacy "!" prefix for backward compatibility
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
                
                # Check if we should queue instead of reject (for global cooldowns near expiring)
                should_queue, remaining = self._should_queue_command(command, message)
                if should_queue:
                    if self._queue_command(command, message, remaining):
                        # Successfully queued - silently return (no message sent)
                        # Still record in stats as attempted
                        if 'stats' in self.commands:
                            stats_command = self.commands['stats']
                            if stats_command:
                                stats_command.record_command(message, command_name, False)
                        return
                    # Queue failed (user already has queued command) - fall through to normal rejection
                
                # Check if command can execute (cooldown, DM requirements, etc.)
                if not command.can_execute_now(message):
                    response_sent = False
                    # For DM-only commands in public channels, only show error if channel is allowed
                    # (i.e., channel is in monitor_channels or command's allowed_channels)
                    # This prevents prompting users in channels where the command shouldn't work at all
                    if command.requires_dm and not message.is_dm:
                        # Only prompt if channel is allowed (configured channels)
                        if command.is_channel_allowed(message):
                            error_msg = command.translate('errors.dm_only', command=command_name)
                            await self.send_response(message, error_msg)
                            response_sent = True
                        # Otherwise, silently ignore (channel not configured for this command)
                    elif command.requires_admin_access():
                        error_msg = command.translate('errors.access_denied', command=command_name)
                        await self.send_response(message, error_msg)
                        response_sent = True
                    elif hasattr(command, 'get_remaining_cooldown') and callable(command.get_remaining_cooldown):
                        # Check if it's the per-user version (takes user_id parameter)
                        import inspect
                        sig = inspect.signature(command.get_remaining_cooldown)
                        if len(sig.parameters) > 0:
                            remaining = command.get_remaining_cooldown(message.sender_id)
                        else:
                            remaining = command.get_remaining_cooldown()
                        
                        if remaining > 0:
                            error_msg = command.translate('errors.cooldown', command=command_name, seconds=remaining)
                            await self.send_response(message, error_msg)
                            response_sent = True
                    
                    # Record command execution in stats database (even if it failed checks)
                    if 'stats' in self.commands:
                        stats_command = self.commands['stats']
                        if stats_command:
                            stats_command.record_command(message, command_name, response_sent)
                    
                    return
                
                # Check network connectivity for commands that require internet
                if command.requires_internet:
                    has_internet = await self._check_internet_cached_async()
                    if not has_internet:
                        self.logger.warning(f"Command '{command_name}' requires internet but network is unavailable")
                        # Try to get translated error message, fallback to default
                        error_msg = command.translate('errors.no_internet', command=command_name)
                        # If translation returns the key itself (translation not found), use fallback
                        if error_msg == 'errors.no_internet':
                            error_msg = f"{command_name} unavailable: No internet connection available"
                        await self.send_response(message, error_msg)
                        
                        # Record command execution in stats database (error response was sent)
                        if 'stats' in self.commands:
                            stats_command = self.commands['stats']
                            if stats_command:
                                stats_command.record_command(message, command_name, True)
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
                    
                    # Execute the command
                    success = await command.execute(message)
                    
                    # Small delay to ensure send_response has completed
                    await asyncio.sleep(0.1)
                    
                    # Determine if a response was sent by checking response tracking
                    response_sent = False
                    response = None
                    if hasattr(command, 'last_response') and command.last_response:
                        response = command.last_response
                        response_sent = True
                    elif hasattr(self, '_last_response') and self._last_response:
                        response = self._last_response
                        response_sent = True
                    
                    # Record command execution in stats database
                    if 'stats' in self.commands:
                        stats_command = self.commands['stats']
                        if stats_command:
                            stats_command.record_command(message, command_name, response_sent)
                    
                    # Capture command data for web viewer
                    if (hasattr(self.bot, 'web_viewer_integration') and 
                        self.bot.web_viewer_integration and 
                        self.bot.web_viewer_integration.bot_integration):
                        try:
                            # Use the response we found, or default
                            if response is None:
                                response = "Command executed"
                            
                            # Generate command_id for repeat tracking
                            command_id = f"{command_name}_{message.sender_id}_{int(time.time())}"
                            
                            # Try to find matching transmission by content and timestamp
                            if (hasattr(self.bot, 'transmission_tracker') and 
                                self.bot.transmission_tracker and 
                                response):
                                # Search for recent transmission with matching content
                                current_time = time.time()
                                matched = False
                                for timestamp_key in range(int(current_time - 10), int(current_time + 1)):
                                    if timestamp_key in self.bot.transmission_tracker.pending_transmissions:
                                        for record in self.bot.transmission_tracker.pending_transmissions[timestamp_key]:
                                            # Match by exact content and recent timestamp to avoid false positives
                                            # Using substring matching (e.g., "ok" in "outlook") would cause incorrect correlations
                                            if record.content == response and \
                                               abs(record.timestamp - current_time) < 10:
                                                record.command_id = command_id
                                                self.logger.debug(f"Linked command {command_id} to transmission: {record.message_type} to {record.target}")
                                                matched = True
                                                break
                                        if matched:
                                            break
                                
                                # Also check confirmed transmissions
                                if not matched:
                                    for packet_hash, record in self.bot.transmission_tracker.confirmed_transmissions.items():
                                        # Match by exact content and recent timestamp to avoid false positives
                                        if record.content == response and \
                                           abs(record.timestamp - current_time) < 10:
                                            record.command_id = command_id
                                            self.logger.debug(f"Linked command {command_id} to confirmed transmission: {record.message_type} to {record.target}")
                                            break
                            
                            self.bot.web_viewer_integration.bot_integration.capture_command(
                                message, command_name, response, success if success is not None else True, command_id
                            )
                        except Exception as e:
                            self.logger.debug(f"Failed to capture command data for web viewer: {e}")
                    
                except Exception as e:
                    self.logger.error(f"Error executing command '{command_name}': {e}")
                    # Send error message to user
                    error_msg = command.translate('errors.execution_error', command=command_name, error=str(e))
                    await self.send_response(message, error_msg)
                    
                    # Record command execution in stats database (error response was sent)
                    if 'stats' in self.commands:
                        stats_command = self.commands['stats']
                        if stats_command:
                            stats_command.record_command(message, command_name, True)  # Error message counts as response
                    
                    # Capture failed command for web viewer
                    if (hasattr(self.bot, 'web_viewer_integration') and 
                        self.bot.web_viewer_integration and 
                        self.bot.web_viewer_integration.bot_integration):
                        try:
                            command_id = f"{command_name}_{message.sender_id}_{int(time.time())}"
                            self.bot.web_viewer_integration.bot_integration.capture_command(
                                message, command_name, f"Error: {e}", False, command_id
                            )
                        except Exception as capture_error:
                            self.logger.debug(f"Failed to capture failed command data: {capture_error}")
                return
    
    def _check_internet_cached(self) -> bool:
        """Check internet connectivity with caching to avoid checking on every command.
        
        Uses synchronous check for keyword matching. Note: This is a synchronous
        method, but the cache itself is thread-safe.
        
        Returns:
            bool: True if internet is available, False otherwise.
        """
        current_time = time.time()
        
        # Check if we have a valid cached result (no lock needed for read-only check)
        if self._internet_cache.is_valid(self._internet_cache_duration):
            return self._internet_cache.has_internet
        
        # Cache expired or doesn't exist - perform actual check
        from .utils import check_internet_connectivity
        has_internet = check_internet_connectivity()
        
        # Update cache (synchronous update, but cache structure is thread-safe)
        self._internet_cache.has_internet = has_internet
        self._internet_cache.timestamp = current_time
        
        return has_internet
    
    async def _check_internet_cached_async(self) -> bool:
        """Check internet connectivity with caching to avoid checking on every command.
        
        Uses async check for command execution. Thread-safe with asyncio.Lock
        to prevent race conditions.
        
        Returns:
            bool: True if internet is available, False otherwise.
        """
        # Use lock to prevent race conditions when checking/updating cache
        async with self._internet_cache._get_lock():
            current_time = time.time()
            
            # Check if we have a valid cached result
            if self._internet_cache.is_valid(self._internet_cache_duration):
                return self._internet_cache.has_internet
            
            # Cache expired or doesn't exist - perform actual check
            has_internet = await check_internet_connectivity_async()
            
            # Update cache
            self._internet_cache.has_internet = has_internet
            self._internet_cache.timestamp = current_time
            
            return has_internet
    
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
