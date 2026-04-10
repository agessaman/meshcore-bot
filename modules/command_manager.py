#!/usr/bin/env python3
"""
Command management functionality for the MeshCore Bot
Handles all bot commands, keyword matching, and response generation
"""

import asyncio
import random
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Optional

from meshcore import EventType

from .commands.base_command import BaseCommand
from .config_validation import (
    PUBLIC_CHANNEL_KEY_HEX,  # noqa: F401 — re-exported; used by core.py
    PUBLIC_CHANNEL_OVERRIDE_KEY,
    _channel_name_is_public,
    strip_optional_quotes,
)
from .models import CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD, MeshMessage
from .plugin_loader import PluginLoader
from .security_utils import sanitize_name, validate_safe_path
from .utils import check_internet_connectivity_async, decode_escape_sequences, format_keyword_response_with_placeholders


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
        self.channel_keywords = self.load_channel_keywords()
        self.command_prefix = self.load_command_prefix()

        # Initialize plugin loader and load all plugins
        local_commands_dir = (
            str(bot._local_root / "commands")
            if getattr(bot, "_local_root", None) is not None
            else str(bot.bot_root / "local" / "commands")
        )
        self.plugin_loader = PluginLoader(bot, local_commands_dir=local_commands_dir)
        self.commands = self.plugin_loader.load_all_plugins()

        # Cache for internet connectivity status to avoid checking on every command
        # Thread-safe cache with asyncio.Lock
        self._internet_cache = InternetStatusCache(has_internet=True, timestamp=0)
        self._internet_cache_duration = 30  # Cache for 30 seconds

        # Command queue for near-expiring global cooldowns
        # Key: (command_name, user_id) tuple, Value: QueuedCommand
        self._command_queue: dict[tuple[str, str], QueuedCommand] = {}
        self._queue_processor_task: Optional[asyncio.Task] = None

        # Multi-scope reply: map of normalized scope name → 16-byte HMAC key.
        # flood_scope_allow_global is True when '*' (or equivalent) appears in
        # flood_scopes, meaning unscoped FLOOD messages are also permitted.
        self.flood_scope_allow_global: bool = False
        self.flood_scope_keys: dict[str, bytes] = self._load_flood_scope_keys()

        self.logger.info(f"CommandManager initialized with {len(self.commands)} plugins")

    def _load_flood_scope_keys(self) -> dict[str, bytes]:
        """Load flood_scopes config into a name→16-byte-key dict for HMAC matching.

        Global/wildcard entries ('*', '', '0', 'None') are not added to the key
        dict (they have no HMAC) but set flood_scope_allow_global so unscoped
        FLOOD messages are still permitted through the allowlist check.
        """
        scope_keys: dict[str, bytes] = {}
        if not (self.bot.config.has_section("Channels") and
                self.bot.config.has_option("Channels", "flood_scopes")):
            return scope_keys
        raw = (self.bot.config.get("Channels", "flood_scopes") or "").strip()
        for entry in (s.strip() for s in raw.split(",") if s.strip()):
            normalized = self._normalize_scope_name(entry)
            if normalized in ("", "*", "0", "None"):
                self.flood_scope_allow_global = True
            elif normalized:
                scope_keys[normalized] = sha256(normalized.encode()).digest()[:16]
        if scope_keys or self.flood_scope_allow_global:
            self.logger.info(
                f"Flood scope allowlist active: {list(scope_keys.keys())} "
                f"(global/unscoped permitted: {self.flood_scope_allow_global})"
            )
        return scope_keys

    @staticmethod
    def _normalize_scope_name(scope: str) -> str:
        """Return scope with '#' prepended if it is a non-global named region without one."""
        if scope in ("", "*", "0", "None"):
            return scope
        if not scope.startswith("#"):
            return "#" + scope
        return scope

    def _should_queue_command(self, command: BaseCommand, message: MeshMessage) -> tuple[bool, float]:
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

    def get_rate_limit_key(self, message: MeshMessage) -> Optional[str]:
        """Return the key used for per-user rate limiting (pubkey when available, else sender name)."""
        return message.sender_pubkey or message.sender_id or None

    def get_rate_limit_wait_seconds(self, rate_limit_key: Optional[str] = None) -> float:
        """Return seconds to wait until we could pass rate limits (for reply retry)."""
        wait = 0.0
        if not self.bot.rate_limiter.can_send():
            wait = max(wait, self.bot.rate_limiter.time_until_next())
        if getattr(self.bot, "per_user_rate_limit_enabled", False) and rate_limit_key:
            per_user = getattr(self.bot, "per_user_rate_limiter", None)
            if per_user and not per_user.can_send(rate_limit_key):
                wait = max(wait, per_user.time_until_next(rate_limit_key))
        return wait

    async def _check_rate_limits(
        self, skip_user_rate_limit: bool = False, rate_limit_key: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> tuple[bool, str]:
        """Check all rate limits before sending.

        Checks both the user-specific rate limits and the global bot transmission
        limits. Also applies transmission delays if configured.

        Args:
            skip_user_rate_limit: If True, skip the user rate limiter check (for automated responses).
            rate_limit_key: Optional key for per-user rate limit (e.g. from get_rate_limit_key(message)).
            channel: Optional channel name for per-channel rate limit check.

        Returns:
            Tuple[bool, str]: A tuple containing:
                - can_send: True if the message can be sent, False otherwise.
                - reason: Reason string if rate limited, empty string otherwise.
        """
        # Check global user rate limiter (unless skipped for automated responses)
        if not skip_user_rate_limit:
            if not self.bot.rate_limiter.can_send():
                wait_time = self.bot.rate_limiter.time_until_next()
                if wait_time > 0.1:
                    return False, f"Rate limited. Wait {wait_time:.1f} seconds"
                return False, ""
            # Per-user rate limit when enabled and key present.
            # Admin ACL controls command authorization only; it does not bypass send rate limits.
            if getattr(self.bot, 'per_user_rate_limit_enabled', False) and rate_limit_key:
                per_user = getattr(self.bot, 'per_user_rate_limiter', None)
                if per_user and not per_user.can_send(rate_limit_key):
                    wait_time = per_user.time_until_next(rate_limit_key)
                    if wait_time > 0.1:
                        return False, f"Rate limited. Wait {wait_time:.1f} seconds"
                    return False, ""

        # Per-channel rate limit
        if channel:
            ch_limiter = getattr(self.bot, 'channel_rate_limiter', None)
            if ch_limiter and not ch_limiter.can_send(channel):
                wait_time = ch_limiter.time_until_next(channel)
                if wait_time > 0.1:
                    return False, f"Channel {channel!r} rate limited. Wait {wait_time:.1f} seconds"
                return False, ""

        # Wait for bot TX rate limiter
        await self.bot.bot_tx_rate_limiter.wait_for_tx()

        # Apply transmission delay
        await self._apply_tx_delay()

        return True, ""

    def _is_no_event_received(self, result) -> bool:
        """Return True when result is an ERROR event with reason 'no_event_received'."""
        if not result or not hasattr(result, 'type'):
            return False
        if result.type != EventType.ERROR:
            return False
        payload = result.payload if hasattr(result, 'payload') else {}
        return isinstance(payload, dict) and payload.get('reason') == 'no_event_received'

    def _handle_send_result(
        self,
        result,
        operation_name: str,
        target: str,
        used_retry_method: bool = False,
        rate_limit_key: Optional[str] = None,
    ) -> bool:
        """Handle result from message send operations.

        Args:
            result: Result object from meshcore send operation.
            operation_name: Name of the operation ("DM" or "Channel message").
            target: Recipient name or channel name for logging.
            used_retry_method: True if send_msg_with_retry was used (affects logging).
            rate_limit_key: Optional key for per-user rate limit recording.

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
                if getattr(self.bot, 'per_user_rate_limit_enabled', False) and rate_limit_key:
                    per_user = getattr(self.bot, 'per_user_rate_limiter', None)
                    if per_user:
                        per_user.record_send(rate_limit_key)
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
                    if getattr(self.bot, 'per_user_rate_limit_enabled', False) and rate_limit_key:
                        per_user = getattr(self.bot, 'per_user_rate_limiter', None)
                        if per_user:
                            per_user.record_send(rate_limit_key)
                    return True

            # Unknown event type - log warning
            self.logger.warning(f"{operation_name} to {target}: unexpected event type {event_name}")
            return False

        # Assume success if result exists but has no type attribute
        self.logger.info(f"✅ {operation_name} sent to {target} (result: {result})")
        self.bot.rate_limiter.record_send()
        self.bot.bot_tx_rate_limiter.record_tx()
        if getattr(self.bot, 'per_user_rate_limit_enabled', False) and rate_limit_key:
            per_user = getattr(self.bot, 'per_user_rate_limiter', None)
            if per_user:
                per_user.record_send(rate_limit_key)
        return True

    def load_keywords(self) -> dict[str, str]:
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
                # Decode escape sequences (e.g., \n for newlines)
                response = decode_escape_sequences(response)
                keywords[keyword.lower()] = response
        return keywords

    def load_custom_syntax(self) -> dict[str, str]:
        """Load custom syntax patterns from config"""
        syntax_patterns = {}
        if self.bot.config.has_section('Custom_Syntax'):
            for pattern, response_format in self.bot.config.items('Custom_Syntax'):
                # Strip quotes from the response format if present
                if response_format.startswith('"') and response_format.endswith('"'):
                    response_format = response_format[1:-1]
                # Decode escape sequences (e.g., \n for newlines)
                response_format = decode_escape_sequences(response_format)
                syntax_patterns[pattern] = response_format
        return syntax_patterns

    def load_banned_users(self) -> list[str]:
        """Load banned users from config"""
        if not self.bot.config.has_section('Banned_Users'):
            return []
        banned = self.bot.config.get('Banned_Users', 'banned_users', fallback='')
        return [user.strip() for user in banned.split(',') if user.strip()]

    def is_user_banned(self, sender_id: Optional[str]) -> bool:
        """Check if sender is banned using prefix (starts-with) matching.

        A banned entry "Awful Username" matches "Awful Username" and "Awful Username 🍆".
        """
        if not sender_id:
            return False
        return any(sender_id.startswith(entry) for entry in self.banned_users)

    def load_monitor_channels(self) -> list[str]:
        """Load monitored channels from config.
        Values may be quoted, e.g. \"#bot,#bot-everett,#bots\" or unquoted.
        """
        raw = self.bot.config.get('Channels', 'monitor_channels', fallback='')
        channels = strip_optional_quotes(raw)
        channel_list = [channel.strip() for channel in channels.split(',') if channel.strip()]

        if any(_channel_name_is_public(ch) for ch in channel_list):
            override = self.bot.config.get("Bot", PUBLIC_CHANNEL_OVERRIDE_KEY, fallback="").strip().lower()
            if override != "true":
                self.logger.error(
                    "FATAL: monitor_channels includes the Public channel. Running a bot on "
                    "Public is disruptive to other mesh users. To override, add to [Bot]:\n"
                    f"  {PUBLIC_CHANNEL_OVERRIDE_KEY} = true"
                )
                raise SystemExit(1)

        return channel_list

    def load_channel_keywords(self) -> Optional[list[str]]:
        """Load channel keyword whitelist from config.

        When set, only these triggers (command/keyword names) are answered in channels;
        DMs always get all triggers. Use to reduce channel floods by making heavy
        triggers DM-only. Names are case-insensitive.
        """
        raw = self.bot.config.get('Channels', 'channel_keywords', fallback='').strip()
        if not raw:
            return None
        return [k.strip().lower() for k in raw.split(',') if k.strip()]

    def _is_channel_trigger_allowed(self, trigger: str, message: MeshMessage) -> bool:
        """Return True if this trigger is allowed for the message context.
        When channel_keywords is set, channel messages only allow listed triggers."""
        if message.is_dm:
            return True
        if self.channel_keywords is None:
            return True
        return trigger.lower() in self.channel_keywords

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

    def get_max_message_length(self, message: MeshMessage) -> int:
        """Return max message body size in UTF-8 bytes (DM=158, channel per firmware budget).

        Regional (non-global) flood scope reduces the channel body budget by
        ``CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD`` bytes.

        Mirrors ``BaseCommand.get_max_message_length`` but works on the manager level so it
        can be called outside of a specific command instance.
        """
        if message.is_dm:
            return 158
        username: str | None = None
        try:
            if hasattr(self.bot, 'meshcore') and self.bot.meshcore:
                self_info = getattr(self.bot.meshcore, 'self_info', None)
                if self_info:
                    if isinstance(self_info, dict):
                        username = self_info.get('name') or self_info.get('user_name')
                    else:
                        username = getattr(self_info, 'name', None) or getattr(self_info, 'user_name', None)
        except Exception:
            pass
        if not username:
            username = self.bot.config.get('Bot', 'bot_name', fallback='Bot')
        max_length = max(130, 160 - len(str(username).encode('utf-8')) - 2)
        if not MeshMessage.is_global_flood_scope(message.effective_outgoing_flood_scope(self.bot)):
            max_length -= CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD
        return max_length

    def check_keywords(self, message: MeshMessage) -> list[tuple]:
        """Check message content for keywords and return matching responses.

        Evaluates the message against configured keywords, custom syntax patterns,
        and command triggers.

        Args:
            message: The incoming message to check.

        Returns:
            List[tuple]: List of (trigger, response) tuples for matched keywords.
        """
        matches: list[tuple[str, Optional[str]]] = []
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
                    # When channel_keywords is set, only allow listed triggers in channel
                    if not self._is_channel_trigger_allowed('help', message):
                        break

                # Channel check passed, process help request
                if content_lower.startswith(help_keyword + ' '):
                    command_name = content_lower[len(help_keyword):].strip()  # Remove help keyword prefix
                    help_text = self.get_help_for_command(command_name, message)
                    # Format the help response with message data (same as other keywords)
                    help_text = self.format_keyword_response(help_text, message)
                    matches.append(('help', help_text))
                    return matches
                elif content_lower == help_keyword:
                    help_text = self.get_general_help(message)
                    # Format the help response with message data (same as other keywords)
                    help_text = self.format_keyword_response(help_text, message)
                    matches.append(('help', help_text))
                    return matches

        # Check all loaded plugins for matches
        for command_name, command in self.commands.items():
            if command.should_execute(message):
                # Check if we should queue instead of skip (for global cooldowns near expiring)
                should_queue, remaining = self._should_queue_command(command, message)
                if should_queue and self._queue_command(command, message, remaining):
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

                # When channel_keywords is set, only allow listed triggers in channel
                if not self._is_channel_trigger_allowed(command_name, message):
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
                # When channel_keywords is set, only allow listed triggers in channel
                if not self._is_channel_trigger_allowed(keyword, message):
                    continue

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

    def _normalize_trigger_text(self, raw: str) -> str:
        """
        Normalize user input / triggers:
        - strip configured command_prefix if present
        - strip legacy leading "!" if no command_prefix configured
        - lowercase
        - trim + collapse whitespace
        """
        if raw is None:
            return ""
        text = raw.strip()

        # Mirror check_keywords() prefix handling
        if self.command_prefix:
            if not text.startswith(self.command_prefix):
                return ""  # No prefix -> treat as non-matchable
            text = text[len(self.command_prefix):].strip()
        else:
            # Backward compatibility
            if text.startswith('!'):
                text = text[1:].strip()

        # case-insensitive + ignore extra spaces
        return " ".join(text.lower().split())

    def match_randomline(self, message: MeshMessage) -> Optional[tuple[str, str]]:
        """
        Exact-match message content against RandomLine triggers.
        Returns (key, response) or None.
        Matching is case-insensitive and ignores extra spaces.
        """
        if not self.bot.config.has_section('RandomLine'):
            return None

        # Start with the same content + prefix stripping logic as check_keywords()
        content = (message.content or "").strip()

        # Check for command prefix if configured
        if self.command_prefix:
            if not content.startswith(self.command_prefix):
                return None
            content = content[len(self.command_prefix):].strip()
        else:
            # Legacy "!" prefix compatibility
            if content.startswith('!'):
                content = content[1:].strip()

        # Normalize: lowercase + collapse whitespace
        content_norm = " ".join(content.lower().split())
        if not content_norm:
            return None

        # Build trigger -> key map from config: triggers.<key> = csv list
        trigger_map = {}
        for cfg_key, cfg_val in self.bot.config.items('RandomLine'):
            if not cfg_key.startswith('triggers.'):
                continue

            key = cfg_key.split('.', 1)[1].strip()
            if not key:
                continue

            raw_triggers = [t.strip() for t in (cfg_val or "").split(",") if t.strip()]
            for trig in raw_triggers:
                trig_norm = " ".join(trig.lower().split())
                if trig_norm:
                    trigger_map[trig_norm] = key

        key = trigger_map.get(content_norm)
        if not key:
            return None

        # Channel restrictions (mirror the plain keyword restrictions)
        if message.is_dm:
            if not self.bot.config.getboolean('Channels', 'respond_to_dms', fallback=True):
                return None
        else:
            # Optional per-trigger channel list: channel.<key> or channels.<key> (e.g. channel.momjoke = #jokes)
            # When set, trigger is allowed only in those channels (even if not in global monitor_channels)
            channel_opt = self.bot.config.get('RandomLine', f'channel.{key}', fallback='').strip()
            if not channel_opt:
                channel_opt = self.bot.config.get('RandomLine', f'channels.{key}', fallback='').strip()
            if channel_opt:
                allowed = [ch.strip() for ch in channel_opt.split(',') if ch.strip()]
                if allowed:
                    # Normalize for comparison: lowercase, strip optional #
                    msg_ch = (message.channel or '').lower().strip().lstrip('#')
                    allowed_normalized = {ch.lower().strip().lstrip('#') for ch in allowed}
                    if msg_ch not in allowed_normalized:
                        return None
                    # Per-trigger channels allowed even when not in monitor_channels; skip global check
                else:
                    if message.channel not in self.monitor_channels:
                        return None
            else:
                if message.channel not in self.monitor_channels:
                    return None
            if not self._is_channel_trigger_allowed(key, message):
                return None

        file_path = self.bot.config.get('RandomLine', f'file.{key}', fallback='').strip()
        if not file_path:
            self.logger.warning(f"RandomLine matched '{key}' but missing config file.{key}")
            return None

        try:
            validated_path = validate_safe_path(file_path, allow_absolute=True)
        except ValueError:
            validated_path = None
        if validated_path is None:
            self.logger.warning(f"RandomLine: unsafe or restricted path rejected for '{key}': {file_path}")
            return None
        file_path = str(validated_path)

        # Read usable lines
        try:
            with open(file_path, encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.readlines()]
            lines = [ln for ln in lines if ln]  # drop blank lines
        except Exception as e:
            self.logger.error(f"RandomLine error reading {file_path} for '{key}': {e}", exc_info=True)
            return None

        if not lines:
            self.logger.warning(f"RandomLine file is empty for '{key}': {file_path}")
            return None

        chosen = random.choice(lines)

        prefix = self.bot.config.get('RandomLine', f'prefix.{key}', fallback='').strip()
        if not prefix:
            prefix = (self.bot.config.get('RandomLine', 'prefix.default', fallback='') or '').strip()

        response = f"{prefix} {chosen}".strip() if prefix else chosen
        return key, response

    async def handle_advert_command(self, message: MeshMessage):
        """Handle the advert command from DM.

        Executes the advert command specifically, ensuring proper stat recording
        and response handling.

        Args:
            message: The message triggering the advert command.
        """
        command = self.commands['advert']
        await command.execute(message)

        # Small delay to ensure send_response has completed
        await asyncio.sleep(0.1)

        # Determine if a response was sent
        response_sent = False
        if hasattr(command, 'last_response') and command.last_response or hasattr(self, '_last_response') and self._last_response:
            response_sent = True

        # Record command execution in stats database
        if 'stats' in self.commands:
            stats_command = self.commands['stats']
            if stats_command:
                stats_command.record_command(message, 'advert', response_sent)

    async def send_dm(
        self,
        recipient_id: str,
        content: str,
        command_id: Optional[str] = None,
        skip_user_rate_limit: bool = False,
        rate_limit_key: Optional[str] = None,
    ) -> bool:
        """Send a direct message using meshcore-cli command.

        Handles contact lookup, rate limiting, and uses retry logic if available.

        Args:
            recipient_id: The recipient's name or ID.
            content: The message content to send.
            command_id: Optional command_id for repeat tracking (if not provided, one will be generated).
            skip_user_rate_limit: If True, skip user rate limiter checks (for automated responses).
            rate_limit_key: Optional key for per-user rate limiting (e.g. from get_rate_limit_key(message)).

        Returns:
            bool: True if sent successfully, False otherwise.
        """
        if not self.bot.connected or not self.bot.meshcore:
            return False

        if self.bot.is_radio_zombie:
            self.bot.logger.warning("send_dm suppressed — radio is in zombie state; power cycle required")
            return False

        # Check all rate limits
        can_send, reason = await self._check_rate_limits(
            skip_user_rate_limit=skip_user_rate_limit, rate_limit_key=rate_limit_key
        )
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
            self.logger.info("Sending DM to %s", sanitize_name(contact_name))

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
            return self._handle_send_result(
                result, "DM", contact_name, used_retry_method, rate_limit_key=rate_limit_key
            )

        except Exception as e:
            self.logger.error(f"Failed to send DM: {e}")
            return False

    async def send_channel_message(
        self,
        channel: str,
        content: str,
        command_id: Optional[str] = None,
        skip_user_rate_limit: bool = False,
        rate_limit_key: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> bool:
        """Send a channel message using meshcore_py (optional flood scope).

        Resolves channel names to numbers and handles rate limiting.
        If [Channels] outgoing_flood_scope_override is set (or scope is passed explicitly),
        uses that scope for this send then restores global flood. When neither is set,
        scope defaults to global flood. Scope values "" / "*" / "0" mean global.
        """
        if not self.bot.connected or not self.bot.meshcore:
            return False

        if self.bot.is_radio_zombie:
            self.bot.logger.warning("send_channel_message suppressed — radio is in zombie state; power cycle required")
            return False

        # Check all rate limits (including per-channel)
        can_send, reason = await self._check_rate_limits(
            skip_user_rate_limit=skip_user_rate_limit, rate_limit_key=rate_limit_key,
            channel=channel,
        )
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

            # Optional flood scope (region): set before send, restore after
            scope_cfg = ""
            if self.bot.config.has_section("Channels") and self.bot.config.has_option("Channels", "outgoing_flood_scope_override"):
                scope_cfg = (self.bot.config.get("Channels", "outgoing_flood_scope_override") or "").strip()
            scope_to_use = (scope if scope is not None else scope_cfg) or ""
            scope_is_global = scope_to_use in ("", "*", "0", "None")
            if not scope_is_global:
                scope_to_use = self._normalize_scope_name(scope_to_use)
            if not scope_is_global and hasattr(self.bot.meshcore.commands, "set_flood_scope"):
                await self.bot.meshcore.commands.set_flood_scope(scope_to_use)

            target = f"{channel} (channel {channel_num})"
            # Retry on no_event_received: max 2 extra attempts, 2s apart
            _max_retries = 2
            for _attempt in range(_max_retries + 1):
                try:
                    result = await self.bot.meshcore.commands.send_chan_msg(channel_num, content)
                finally:
                    if not scope_is_global and hasattr(self.bot.meshcore.commands, "set_flood_scope"):
                        await self.bot.meshcore.commands.set_flood_scope("*")

                if self._is_no_event_received(result) and _attempt < _max_retries:
                    self.logger.warning(
                        f"Channel message to {target}: no_event_received "
                        f"(attempt {_attempt + 1}/{_max_retries + 1}), retrying in 2s"
                    )
                    await asyncio.sleep(2)
                    # Re-apply scope for next attempt
                    if not scope_is_global and hasattr(self.bot.meshcore.commands, "set_flood_scope"):
                        await self.bot.meshcore.commands.set_flood_scope(scope_to_use)
                    continue
                break

            # Handle result using unified handler
            success = self._handle_send_result(
                result, "Channel message", target, rate_limit_key=rate_limit_key
            )
            if success:
                ch_limiter = getattr(self.bot, 'channel_rate_limiter', None)
                if ch_limiter:
                    ch_limiter.record_send(channel)
            if success and getattr(self.bot, 'channel_sent_listeners', None):
                bot_name = self.bot.config.get('Bot', 'bot_name', fallback='Bot')
                payload = {'channel_idx': channel_num, 'text': f'{bot_name}: {content}'}
                synthetic_event = type('Event', (), {'payload': payload})()
                for cb in list(self.bot.channel_sent_listeners):
                    async def _run_listener(listener, event):
                        try:
                            await listener(event, None)
                        except Exception as e:
                            self.logger.warning(
                                "Channel sent listener error: %s", e, exc_info=True
                            )
                    asyncio.create_task(_run_listener(cb, synthetic_event))
            return success

        except Exception as e:
            self.logger.error(f"Failed to send channel message: {e}")
            return False

    async def send_channel_messages_chunked(
        self,
        channel: str,
        chunks: list[str],
        *,
        command_id: Optional[str] = None,
        skip_user_rate_limit: bool = True,
        rate_limit_key: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> bool:
        """Send multiple channel messages with rate-limit spacing between chunks.

        Uses bot_tx_rate_limiter and configured bot_tx_rate_limit_seconds so each
        chunk after the first is spaced correctly. For the first chunk, uses the
        provided skip_user_rate_limit and rate_limit_key; subsequent chunks
        always use skip_user_rate_limit=True so automated multi-part sends work.

        Args:
            channel: Channel name to send to.
            chunks: List of message strings to send in order.
            command_id: Optional command_id for repeat tracking.
            skip_user_rate_limit: If True, skip user/global rate limit for first chunk (default True for services).
            rate_limit_key: Optional key for per-user rate limit on first chunk only.
            scope: Optional flood scope for send (see send_channel_message).

        Returns:
            bool: True if all chunks were sent successfully, False on first failure.
        """
        if not chunks:
            return True
        rate_limit_seconds = self.bot.config.getfloat('Bot', 'bot_tx_rate_limit_seconds', fallback=1.0)
        sleep_time = max(rate_limit_seconds + 0.5, 1.0)
        for i, chunk in enumerate(chunks):
            if i > 0:
                await self.bot.bot_tx_rate_limiter.wait_for_tx()
                await asyncio.sleep(sleep_time)
            skip_first = skip_user_rate_limit if i == 0 else True
            key_first = rate_limit_key if i == 0 else None
            success = await self.send_channel_message(
                channel,
                chunk,
                command_id=command_id,
                skip_user_rate_limit=skip_first,
                rate_limit_key=key_first,
                scope=scope,
            )
            if not success:
                self.logger.warning(
                    "Chunked channel send failed at chunk %d of %d to %s", i + 1, len(chunks), channel
                )
                return False
        return True

    def get_help_for_command(self, command_name: str, message: Optional[MeshMessage] = None) -> str:
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
            return self.get_general_help(message)

        requested_name = command_name.strip()
        normalized_name = requested_name.lower()

        # First, try to find a command by exact name
        command = self.commands.get(normalized_name) or self.commands.get(requested_name)
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

        # Next, consult plugin_loader keyword mappings (if available)
        mapped_name: Optional[str] = None
        if hasattr(self, 'plugin_loader') and hasattr(self.plugin_loader, 'keyword_mappings'):
            mapped_name = self.plugin_loader.keyword_mappings.get(normalized_name)
        if mapped_name:
            command = self.commands.get(mapped_name)
            if command:
                try:
                    help_text = command.get_help_text(message)
                except TypeError:
                    help_text = command.get_help_text()
                if hasattr(self.bot, 'translator'):
                    return self.bot.translator.translate('commands.help.specific', command=command_name, help_text=help_text)
                return f"Help {command_name}: {help_text}"

        # If still not found, search through all commands and their keywords
        for _cmd_name, cmd_instance in self.commands.items():
            # Check if the requested command name matches any of this command's keywords
            if (
                hasattr(cmd_instance, 'keywords')
                and normalized_name in [k.lower() for k in cmd_instance.keywords]
            ):
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
                available_str = help_command.get_available_commands_list(message)

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

    # Prefix and suffix for general help (reserve space so suffix is never cut off)
    _HELP_PREFIX = "Bot Help: "
    _HELP_SUFFIX = " | More: 'help <command>'"

    def get_general_help(self, message: Optional[MeshMessage] = None) -> str:
        """Get general help text from config (LoRa-friendly compact format).

        When message is provided, only lists commands valid for the message's channel.
        Reserves space for the suffix so the message always ends with | More: 'help <command>'.
        """
        # Prefer keywords config if user has customized help
        if 'help' in self.keywords:
            return self.keywords['help']
        # Fallback: build compact list from available commands (filtered by channel)
        if 'help' in self.commands:
            help_command = self.commands['help']
            if hasattr(help_command, 'get_available_commands_list'):
                max_list = None
                if message and hasattr(help_command, 'get_max_message_length'):
                    max_total = help_command.get_max_message_length(message)
                    max_list = max_total - len(self._HELP_PREFIX) - len(self._HELP_SUFFIX)
                available_str = help_command.get_available_commands_list(message, max_length=max_list)
                return f"{self._HELP_PREFIX}{available_str}{self._HELP_SUFFIX}"
        # Last resort: simple list of command names (filtered by channel when message provided)
        help_cmd = self.commands.get('help')
        if help_cmd and hasattr(help_cmd, '_is_command_valid_for_channel') and message:
            primary_names = sorted([
                cmd.name if hasattr(cmd, 'name') else name
                for name, cmd in self.commands.items()
                if help_cmd._is_command_valid_for_channel(name, cmd, message)
            ])
        else:
            primary_names = sorted([
                cmd.name if hasattr(cmd, 'name') else name
                for name, cmd in self.commands.items()
            ])
        # Truncate list to reserve space for suffix when message (and thus max length) is known
        if message and help_cmd and hasattr(help_cmd, 'get_max_message_length'):
            max_total = help_cmd.get_max_message_length(message)
            max_list = max_total - len(self._HELP_PREFIX) - len(self._HELP_SUFFIX)
            if hasattr(help_cmd, '_format_commands_list_to_length'):
                list_str = help_cmd._format_commands_list_to_length(primary_names, max_list)
            else:
                list_str = ', '.join(primary_names)
        else:
            list_str = ', '.join(primary_names)
        return f"{self._HELP_PREFIX}{list_str}{self._HELP_SUFFIX}"

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

            rate_limit_key = self.get_rate_limit_key(message)
            if message.is_dm:
                return await self.send_dm(
                    message.sender_id or "", content,
                    skip_user_rate_limit=skip_user_rate_limit,
                    rate_limit_key=rate_limit_key,
                )
            else:
                return await self.send_channel_message(
                    message.channel or "", content,
                    skip_user_rate_limit=skip_user_rate_limit,
                    rate_limit_key=rate_limit_key,
                    scope=getattr(message, 'reply_scope', None),
                )
        except Exception as e:
            self.logger.error(f"Failed to send response: {e}")
            return False

    @staticmethod
    def split_text_into_chunks(text: str, max_len: int) -> list[str]:
        """Split *text* into a list of strings each at most *max_len* characters.

        Splitting prefers the last space within the limit so words are not broken;
        if no space is found the chunk is hard-split at *max_len*.

        Args:
            text: The text to split.
            max_len: Maximum length of each chunk (must be >= 1).

        Returns:
            List of non-empty chunk strings.  Returns ``[""]`` when *text* is empty.
        """
        if max_len < 1:
            max_len = 1
        if len(text) <= max_len:
            return [text]
        chunks: list[str] = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Try to split on the last space within the window
            split_at = text.rfind(' ', 0, max_len + 1)
            if split_at <= 0:
                split_at = max_len
            chunks.append(text[:split_at].rstrip())
            text = text[split_at:].lstrip()
        return chunks

    async def send_response_chunked(
        self, message: MeshMessage, chunks: list[str], *, skip_user_rate_limit_first: bool = True
    ) -> bool:
        """Send multiple response messages (channel or DM) with rate-limit spacing.

        For channel: delegates to send_channel_messages_chunked. For DM: loops
        with wait_for_tx + sleep between chunks and send_dm per chunk. First chunk
        may count against user rate limit depending on skip_user_rate_limit_first;
        subsequent chunks always skip user rate limit.

        Args:
            message: The original message being responded to.
            chunks: List of message strings to send in order.
            skip_user_rate_limit_first: If True, skip user rate limit for first chunk too (default).

        Returns:
            bool: True if all chunks were sent successfully, False on first failure.
        """
        if not chunks:
            return True
        rate_limit_key = self.get_rate_limit_key(message)
        if message.is_dm:
            rate_limit_seconds = self.bot.config.getfloat('Bot', 'bot_tx_rate_limit_seconds', fallback=1.0)
            sleep_time = max(rate_limit_seconds + 0.5, 1.0)
            for i, chunk in enumerate(chunks):
                if i > 0:
                    await self.bot.bot_tx_rate_limiter.wait_for_tx()
                    await asyncio.sleep(sleep_time)
                skip = skip_user_rate_limit_first if i == 0 else True
                success = await self.send_dm(
                    message.sender_id or "",
                    chunk,
                    skip_user_rate_limit=skip,
                    rate_limit_key=rate_limit_key,
                )
                if not success:
                    self.logger.warning(
                        "Chunked DM send failed at chunk %d of %d to %s",
                        i + 1, len(chunks), message.sender_id,
                    )
                    return False
            return True
        return await self.send_channel_messages_chunked(
            message.channel or "",
            chunks,
            skip_user_rate_limit=skip_user_rate_limit_first,
            rate_limit_key=rate_limit_key,
            scope=getattr(message, 'reply_scope', None),
        )

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

        content = content.lower()

        # Check each command to see if it should execute
        for command_name, command in self.commands.items():
            # Skip commands not allowed in this channel (silent - no stats, no error)
            # This mirrors the check_keywords() path which calls can_execute() before matching.
            # Messages may reach execute_commands() via a per-command channel override (e.g.
            # greeter allowing Public) even when other commands aren't configured for that channel.
            if not command.is_channel_allowed(message):
                continue

            if command.should_execute(message):
                # Only execute commands that don't have a response format (they handle their own responses)
                response_format = command.get_response_format()
                if response_format is not None:
                    # This command was already handled by keyword matching
                    continue

                self.logger.info(f"Command '{command_name}' matched, executing")

                # Check if we should queue instead of reject (for global cooldowns near expiring)
                should_queue, remaining = self._should_queue_command(command, message)
                if should_queue and self._queue_command(command, message, remaining):
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
                                    for _packet_hash, record in self.bot.transmission_tracker.confirmed_transmissions.items():
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

    def get_plugin_metadata(self, plugin_name: Optional[str] = None) -> dict[str, Any]:
        """Get plugin metadata"""
        return self.plugin_loader.get_plugin_metadata(plugin_name)
