#!/usr/bin/env python3
"""
Announcements command for the MeshCore Bot
Allows authorized users to send announcements to channels via DM
"""

import time
from typing import Optional, Dict
from .base_command import BaseCommand
from ..models import MeshMessage
from ..security_utils import validate_pubkey_format


class AnnouncementsCommand(BaseCommand):
    """Handles announcements command for sending messages to channels.
    
    Allows authorized users to trigger pre-configured announcements to be sent
    to specific channels. Requires specific ACL access and operates via DM only.
    """
    
    # Plugin metadata
    name = "announcements"
    keywords = ['announce']
    description = "Send announcements to channels (DM only, requires announcements ACL)"
    requires_dm = True
    category = "admin"
    
    def __init__(self, bot):
        super().__init__(bot)
        
        # Per-trigger cooldown tracking: trigger_name -> last_execution_time
        self.trigger_cooldowns: Dict[str, float] = {}
        
        # Per-trigger lockout tracking: trigger_name -> last_send_time
        # Prevents duplicate sends from retried DMs (60 second lockout)
        self.trigger_lockouts: Dict[str, float] = {}
        self.lockout_seconds = 60  # 60 second lockout to prevent duplicate sends
        
        # Load configuration
        self.enabled = self.get_config_value('Announcements_Command', 'enabled', fallback=False, value_type='bool')
        self.default_channel = self.get_config_value('Announcements_Command', 'default_announcement_channel', fallback='Public', value_type='str')
        self.cooldown_minutes = self.get_config_value('Announcements_Command', 'announcement_cooldown', fallback=60, value_type='int')
        self.cooldown_seconds = self.cooldown_minutes * 60
        
        # Load announcement triggers from config
        self.triggers = self._load_triggers()
        
        # Load announcements ACL (inherits admin ACL)
        self.announcements_acl = self._load_announcements_acl()
    
    def _load_triggers(self) -> Dict[str, str]:
        """Load announcement triggers from config.
        
        Returns:
            Dict[str, str]: Dictionary mapping trigger names to announcement text.
        """
        triggers = {}
        if self.bot.config.has_section('Announcements_Command'):
            for key, value in self.bot.config.items('Announcements_Command'):
                if key.startswith('announce.'):
                    trigger_name = key.replace('announce.', '').strip()
                    triggers[trigger_name] = value.strip()
        return triggers
    
    def _load_announcements_acl(self) -> list:
        """Load announcements ACL from config.
        
        Inherits members of admin ACL if announcements_acl is not explicitly set.
        
        Returns:
            list: List of permitted public keys.
        """
        acl_list = []
        
        # First, get explicit announcements_acl
        announcements_acl_str = self.get_config_value('Announcements_Command', 'announcements_acl', fallback='', value_type='str')
        
        if announcements_acl_str and announcements_acl_str.strip():
            # Parse explicit announcements ACL
            for key in announcements_acl_str.split(','):
                key = key.strip()
                if not key:
                    continue
                if validate_pubkey_format(key, expected_length=64):
                    acl_list.append(key.lower())
                else:
                    self.logger.warning(f"Invalid pubkey in announcements_acl: {key[:16]}...")
        
        # Always include admin ACL members (inheritance)
        try:
            if not self.bot.config.has_section('Admin_ACL'):
                return acl_list
            admin_pubkeys = self.bot.config.get('Admin_ACL', 'admin_pubkeys', fallback='')
            if admin_pubkeys and admin_pubkeys.strip():
                for key in admin_pubkeys.split(','):
                    key = key.strip()
                    if not key:
                        continue
                    if validate_pubkey_format(key, expected_length=64):
                        normalized_key = key.lower()
                        # Add to list if not already present (avoid duplicates)
                        if normalized_key not in acl_list:
                            acl_list.append(normalized_key)
        except Exception as e:
            self.logger.debug(f"Error loading admin ACL for announcements inheritance: {e}")
        
        return acl_list
    
    def _check_announcements_access(self, message: MeshMessage) -> bool:
        """Check if the message sender has announcements access.
        
        Uses the same security-hardened approach as admin ACL checking.
        
        Args:
            message: The message to check access for.
            
        Returns:
            bool: True if access is granted, False otherwise.
        """
        if not hasattr(self.bot, 'config'):
            return False
        
        if not self.announcements_acl:
            self.logger.warning("No announcements ACL configured")
            return False
        
        # Get sender's public key - NEVER fall back to sender_id
        sender_pubkey = getattr(message, 'sender_pubkey', None)
        if not sender_pubkey:
            self.logger.warning(
                f"No sender public key available for {message.sender_id} - "
                "announcements access denied (missing pubkey)"
            )
            return False
        
        # Validate sender pubkey format
        if not validate_pubkey_format(sender_pubkey, expected_length=64):
            self.logger.warning(
                f"Invalid sender pubkey format from {message.sender_id}: "
                f"{sender_pubkey[:16]}... - announcements access denied"
            )
            return False
        
        # Normalize and compare
        sender_pubkey_normalized = sender_pubkey.lower()
        has_access = sender_pubkey_normalized in self.announcements_acl
        
        if not has_access:
            self.logger.warning(
                f"Announcements access denied for {message.sender_id} "
                f"(pubkey: {sender_pubkey[:16]}...) - not in announcements ACL"
            )
        else:
            self.logger.info(
                f"Announcements access granted for {message.sender_id} "
                f"(pubkey: {sender_pubkey[:16]}...)"
            )
        
        return has_access
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if announcements command can be executed.
        
        Args:
            message: The message trigger.
            
        Returns:
            bool: True if allowed to execute.
        """
        # Check if command is enabled
        if not self.enabled:
            return False
        
        # Check if message is DM (required)
        if not message.is_dm:
            return False
        
        # Check announcements ACL access
        if not self._check_announcements_access(message):
            return False
        
        return True
    
    def _get_trigger_cooldown_remaining(self, trigger_name: str) -> int:
        """Get remaining cooldown time in minutes for a trigger.
        
        Args:
            trigger_name: Name of the announcement trigger.
            
        Returns:
            int: Remaining cooldown in minutes (0 if ready).
        """
        if self.cooldown_seconds <= 0:
            return 0
        
        if trigger_name not in self.trigger_cooldowns:
            return 0
        
        current_time = time.time()
        last_execution = self.trigger_cooldowns[trigger_name]
        elapsed = current_time - last_execution
        remaining_seconds = self.cooldown_seconds - elapsed
        
        if remaining_seconds <= 0:
            return 0
        
        # Convert to minutes (round up)
        remaining_minutes = int((remaining_seconds + 59) // 60)
        return remaining_minutes
    
    def _record_trigger_execution(self, trigger_name: str) -> None:
        """Record the execution time for a trigger.
        
        Args:
            trigger_name: Name of the announcement trigger.
        """
        current_time = time.time()
        self.trigger_cooldowns[trigger_name] = current_time
        self.trigger_lockouts[trigger_name] = current_time
    
    def _is_trigger_locked(self, trigger_name: str) -> bool:
        """Check if a trigger is currently locked (within 60 seconds of last send).
        
        Args:
            trigger_name: Name of the announcement trigger.
            
        Returns:
            bool: True if locked, False otherwise.
        """
        if trigger_name not in self.trigger_lockouts:
            return False
        
        current_time = time.time()
        last_send = self.trigger_lockouts[trigger_name]
        elapsed = current_time - last_send
        
        return elapsed < self.lockout_seconds
    
    def _parse_command(self, content: str) -> tuple:
        """Parse the announce command.
        
        Format: announce <trigger> [channel] [override]
        
        Args:
            content: Command content string.
            
        Returns:
            tuple: (trigger_name, channel_name, is_override) or (None, None, False) if invalid.
        """
        # Remove 'announce' keyword
        parts = content.strip().split(None, 1)
        if len(parts) < 2:
            return (None, None, False)
        
        remaining = parts[1].strip()
        
        # Check for override at the end
        is_override = remaining.lower().endswith(' override')
        if is_override:
            remaining = remaining[:-8].strip()  # Remove " override"
        
        # Split into trigger and optional channel
        parts = remaining.split(None, 1)
        trigger_name = parts[0].strip()
        
        # Check if there's a channel specified
        channel_name = None
        if len(parts) > 1:
            channel_name = parts[1].strip()
        
        return (trigger_name, channel_name, is_override)
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the announcements command.
        
        Args:
            message: The input message trigger.
            
        Returns:
            bool: True if execution was successful.
        """
        try:
            # Parse command
            trigger_name, channel_name, is_override = self._parse_command(message.content)
            
            if not trigger_name:
                # Show list of available triggers with usage
                available_triggers = ', '.join(sorted(self.triggers.keys()))
                if available_triggers:
                    await self.send_response(
                        message,
                        f"Available triggers: {available_triggers}\n"
                        f"Usage: announce <trigger> [channel] [override]"
                    )
                else:
                    await self.send_response(
                        message,
                        "No triggers configured. Usage: announce <trigger> [channel] [override]"
                    )
                return True
            
            # Check if user wants to list triggers (special case)
            if trigger_name.lower() == 'list':
                available_triggers = ', '.join(sorted(self.triggers.keys()))
                if available_triggers:
                    await self.send_response(
                        message,
                        f"Available triggers: {available_triggers}\n"
                        f"Usage: announce <trigger> [channel] [override]"
                    )
                else:
                    await self.send_response(
                        message,
                        "No triggers configured. Usage: announce <trigger> [channel] [override]"
                    )
                return True
            
            # Check if trigger exists
            if trigger_name not in self.triggers:
                available_triggers = ', '.join(sorted(self.triggers.keys()))
                await self.send_response(
                    message, 
                    f"Unknown trigger: {trigger_name}. Available: {available_triggers}"
                )
                return True
            
            # Check lockout (applies even with override - prevents duplicate sends from retries)
            if self._is_trigger_locked(trigger_name):
                remaining_seconds = int(self.lockout_seconds - (time.time() - self.trigger_lockouts[trigger_name]))
                await self.send_response(
                    message,
                    f"That announcement was just sent. Please wait {remaining_seconds} seconds to prevent duplicate sends."
                )
                return True
            
            # Check cooldown (unless override)
            if not is_override:
                remaining_minutes = self._get_trigger_cooldown_remaining(trigger_name)
                if remaining_minutes > 0:
                    await self.send_response(
                        message,
                        f"That announcement is on cooldown for {remaining_minutes} minutes, "
                        "add 'override' at the end to send anyway."
                    )
                    return True
            
            # Get announcement text
            announcement_text = self.triggers[trigger_name]
            
            # Determine channel
            target_channel = channel_name if channel_name else self.default_channel
            
            # Send announcement to channel
            success = await self.bot.command_manager.send_channel_message(target_channel, announcement_text)
            
            if success:
                # Record execution (resets cooldown timer)
                self._record_trigger_execution(trigger_name)
                
                await self.send_response(
                    message,
                    f"Announcement '{trigger_name}' sent to {target_channel}"
                )
                self.logger.info(
                    f"User {message.sender_id} sent announcement '{trigger_name}' to {target_channel}"
                )
            else:
                await self.send_response(
                    message,
                    f"Failed to send announcement to {target_channel}"
                )
                self.logger.error(
                    f"Failed to send announcement '{trigger_name}' to {target_channel}"
                )
            
            return True
            
        except Exception as e:
            error_msg = f"Error sending announcement: {str(e)}"
            self.logger.error(f"Error in announcements command: {e}")
            await self.send_response(message, error_msg)
            return False

