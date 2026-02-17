#!/usr/bin/env python3
"""
Multitest command for the MeshCore Bot
Listens for a period of time and collects all unique paths from incoming messages
"""

import asyncio
import time
from typing import Set, Optional, Dict
from dataclasses import dataclass
from .base_command import BaseCommand
from ..models import MeshMessage
from ..utils import calculate_packet_hash


@dataclass
class MultitestSession:
    """Represents an active multitest listening session"""
    user_id: str
    target_packet_hash: str
    triggering_timestamp: float
    listening_start_time: float
    listening_duration: float
    collected_paths: Set[str]
    initial_path: Optional[str] = None


class MultitestCommand(BaseCommand):
    """Handles the multitest command - listens for multiple path variations"""
    
    # Plugin metadata
    name = "multitest"
    keywords = ['multitest', 'mt']
    description = "Listens for 6 seconds and collects all unique paths from incoming messages"
    category = "meshcore_info"
    
    # Documentation
    short_description = "Listens for 6 seconds and collects all unique paths your incoming messages took to reach the bot"
    usage = "multitest"
    examples = ["multitest", "mt"]
    
    def __init__(self, bot):
        super().__init__(bot)
        self.multitest_enabled = self.get_config_value('Multitest_Command', 'enabled', fallback=True, value_type='bool')
        # Track active sessions per user to prevent race conditions
        # Key: user_id, Value: MultitestSession
        self._active_sessions: Dict[str, MultitestSession] = {}
        # Lock to prevent concurrent execution from interfering (lazily initialized)
        self._execution_lock: Optional[asyncio.Lock] = None
        self._load_config()
    
    def _get_execution_lock(self) -> asyncio.Lock:
        """Get or create the execution lock (lazy initialization)"""
        if self._execution_lock is None:
            self._execution_lock = asyncio.Lock()
        return self._execution_lock
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.multitest_enabled:
            return False
        return super().can_execute(message)
    
    def _load_config(self):
        """Load configuration for multitest command"""
        response_format = self.get_config_value('Multitest_Command', 'response_format', fallback='')
        if response_format and response_format.strip():
            # Strip quotes if present (config parser may add them)
            response_format = self._strip_quotes_from_config(response_format).strip()
            # Decode escape sequences (e.g., \n -> newline)
            try:
                # Use encode/decode to convert escape sequences to actual characters
                self.response_format = response_format.encode('latin-1').decode('unicode_escape')
            except (UnicodeDecodeError, UnicodeEncodeError):
                # If decoding fails, use as-is (fallback)
                self.response_format = response_format
        else:
            self.response_format = None  # Use default format
    
    def get_help_text(self) -> str:
        return self.translate('commands.multitest.help', fallback="Listens for 6 seconds and collects all unique paths from incoming messages")
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message matches multitest keyword"""
        content = message.content.strip()
        
        # Handle exclamation prefix
        if content.startswith('!'):
            content = content[1:].strip()
        
        content_lower = content.lower()
        
        # Check for exact match or keyword followed by space
        for keyword in self.keywords:
            if content_lower == keyword or content_lower.startswith(keyword + ' '):
                return True
        
        # Check for variants: "mt long", "mt xlong", "multitest long", "multitest xlong"
        if content_lower.startswith('mt ') or content_lower.startswith('multitest '):
            parts = content_lower.split()
            if len(parts) >= 2 and parts[0] in ['mt', 'multitest']:
                variant = parts[1]
                if variant in ['long', 'xlong']:
                    return True
        
        return False
    
    def extract_path_from_rf_data(self, rf_data: dict) -> Optional[str]:
        """Extract path in prefix string format from RF data routing_info"""
        try:
            routing_info = rf_data.get('routing_info')
            if not routing_info:
                return None
            
            path_nodes = routing_info.get('path_nodes', [])
            if not path_nodes:
                # Try to extract from path_hex if path_nodes not available
                path_hex = routing_info.get('path_hex', '')
                if path_hex:
                    # Convert hex string to node list (every 2 characters = 1 node)
                    path_nodes = [path_hex[i:i+2] for i in range(0, len(path_hex), 2)]
            
            if path_nodes:
                # Validate and format path nodes
                valid_parts = []
                for node in path_nodes:
                    # Convert to string if needed
                    node_str = str(node).lower().strip()
                    # Check if it's a 2-character hex value
                    if len(node_str) == 2 and all(c in '0123456789abcdef' for c in node_str):
                        valid_parts.append(node_str)
                
                if valid_parts:
                    return ','.join(valid_parts)
            
            return None
        except Exception as e:
            self.logger.debug(f"Error extracting path from RF data: {e}")
            return None
    
    def extract_path_from_message(self, message: MeshMessage) -> Optional[str]:
        """Extract path in prefix string format from a message"""
        if not message.path:
            return None
        
        # Check if it's a direct connection
        if "Direct" in message.path or "0 hops" in message.path:
            return None
        
        # Try to extract path nodes from the path string
        # Path strings are typically in format: "node1,node2,node3 via ROUTE_TYPE_*"
        # or just "node1,node2,node3"
        path_string = message.path
        
        # Remove route type suffix if present
        if " via ROUTE_TYPE_" in path_string:
            path_string = path_string.split(" via ROUTE_TYPE_")[0]
        
        # Check if it looks like a comma-separated path
        if ',' in path_string:
            # Clean up any extra info (like hop counts in parentheses)
            # Example: "01,7e,55,86 (4 hops)" -> "01,7e,55,86"
            if '(' in path_string:
                path_string = path_string.split('(')[0].strip()
            
            # Validate that all parts are 2-character hex values
            parts = path_string.split(',')
            valid_parts = []
            for part in parts:
                part = part.strip()
                # Check if it's a 2-character hex value
                if len(part) == 2 and all(c in '0123456789abcdefABCDEF' for c in part):
                    valid_parts.append(part.lower())
            
            if valid_parts:
                return ','.join(valid_parts)
        
        return None
    
    def get_rf_data_for_message(self, message: MeshMessage) -> Optional[dict]:
        """Get RF data for a message by looking it up in recent RF data"""
        try:
            # Try multiple correlation strategies
            # Strategy 1: Use sender_pubkey to find recent RF data
            if message.sender_pubkey:
                # Try full pubkey first
                recent_rf_data = self.bot.message_handler.find_recent_rf_data(message.sender_pubkey)
                if recent_rf_data:
                    return recent_rf_data
                
                # Try pubkey prefix (first 16 chars)
                if len(message.sender_pubkey) >= 16:
                    pubkey_prefix = message.sender_pubkey[:16]
                    recent_rf_data = self.bot.message_handler.find_recent_rf_data(pubkey_prefix)
                    if recent_rf_data:
                        return recent_rf_data
            
            # Strategy 2: Look through recent RF data for matching pubkey
            if message.sender_pubkey and self.bot.message_handler.recent_rf_data:
                # Search recent RF data for matching pubkey
                for rf_data in reversed(self.bot.message_handler.recent_rf_data):
                    rf_pubkey = rf_data.get('pubkey_prefix', '')
                    if rf_pubkey and message.sender_pubkey.startswith(rf_pubkey):
                        return rf_data
            
            # Strategy 3: Use most recent RF data as fallback
            # This is less reliable but might work if timing is very close
            if self.bot.message_handler.recent_rf_data:
                # Get the most recent RF data entry within a short time window
                current_time = time.time()
                recent_entries = [
                    rf for rf in self.bot.message_handler.recent_rf_data
                    if current_time - rf.get('timestamp', 0) < 5.0  # Within last 5 seconds
                ]
                if recent_entries:
                    most_recent = max(recent_entries, key=lambda x: x.get('timestamp', 0))
                    return most_recent
            
            return None
        except Exception as e:
            self.logger.debug(f"Error getting RF data for message: {e}")
            return None
    
    def on_message_received(self, message: MeshMessage):
        """Callback method called by message handler when a message is received during listening.
        
        Checks all active sessions to see if this message matches any of them.
        """
        if not self._active_sessions:
            return
        
        # Get RF data for this message (contains pre-calculated packet hash)
        rf_data = self.get_rf_data_for_message(message)
        if not rf_data:
            # Can't get RF data, skip this message
            self.logger.debug(f"Skipping message - no RF data found (sender: {message.sender_id})")
            return
        
        # Use pre-calculated packet hash if available, otherwise calculate it
        message_hash = rf_data.get('packet_hash')
        if not message_hash and rf_data.get('raw_hex'):
            # Fallback: calculate hash if not stored (for older RF data)
            try:
                payload_type = None
                routing_info = rf_data.get('routing_info', {})
                if routing_info:
                    # Try to get payload type from routing_info if available
                    payload_type = routing_info.get('payload_type')
                message_hash = calculate_packet_hash(rf_data['raw_hex'], payload_type)
            except Exception as e:
                self.logger.debug(f"Error calculating packet hash: {e}")
                message_hash = None
        
        if not message_hash:
            # Can't determine hash, skip this message
            self.logger.debug(f"Skipping message - could not determine packet hash (sender: {message.sender_id})")
            return
        
        # Check all active sessions to see if this message matches any of them
        current_time = time.time()
        for user_id, session in list(self._active_sessions.items()):
            # Check if we're still in the listening window for this session
            elapsed = current_time - session.listening_start_time
            if elapsed >= session.listening_duration:
                continue  # Session expired, skip it
            
            # CRITICAL: Only collect paths if this message has the same hash as the target
            # This ensures we only track variations of the same original message
            if message_hash == session.target_packet_hash:
                # Try to extract path from RF data first (more reliable)
                path = self.extract_path_from_rf_data(rf_data)
                
                # Fallback to message path if RF data extraction failed
                if not path:
                    path = self.extract_path_from_message(message)
                
                if path:
                    session.collected_paths.add(path)
                    self.logger.info(f"✓ Collected path for user {user_id}: {path} (hash: {message_hash[:8]}...)")
                else:
                    # Log when we have a matching hash but can't extract path
                    routing_info = rf_data.get('routing_info', {})
                    path_length = routing_info.get('path_length', 0)
                    if path_length == 0:
                        self.logger.debug(f"Matched hash {message_hash[:8]}... but path is direct (0 hops) for user {user_id}")
                    else:
                        self.logger.debug(f"Matched hash {message_hash[:8]}... but couldn't extract path from routing_info: {routing_info} for user {user_id}")
            else:
                # Log hash mismatches for debugging (but limit to avoid spam)
                self.logger.debug(f"✗ Hash mismatch for user {user_id} - target: {session.target_packet_hash[:8]}..., received: {message_hash[:8]}... (sender: {message.sender_id})")
    
    def _scan_recent_rf_data(self, session: MultitestSession):
        """Scan recent RF data for packets with matching hash (for messages that haven't been processed yet)
        
        Args:
            session: The multitest session to scan for
        """
        if not session.target_packet_hash:
            return
        
        try:
            current_time = time.time()
            matching_count = 0
            mismatching_count = 0
            
            # Look at RF data from the last few seconds (before listening started, in case packets arrived just before)
            for rf_data in self.bot.message_handler.recent_rf_data:
                # Check if this RF data is recent enough
                rf_timestamp = rf_data.get('timestamp', 0)
                time_diff = current_time - rf_timestamp
                
                # Only include RF data from the triggering message timestamp onwards
                # This prevents collecting packets from earlier messages that happen to have the same hash
                if rf_timestamp >= session.triggering_timestamp and time_diff <= session.listening_duration:
                    packet_hash = rf_data.get('packet_hash')
                    
                    # CRITICAL: Only process if hash matches exactly and is not None/empty
                    if packet_hash and packet_hash == session.target_packet_hash:
                        matching_count += 1
                        # Extract path from this RF data
                        path = self.extract_path_from_rf_data(rf_data)
                        if path:
                            session.collected_paths.add(path)
                            self.logger.info(f"✓ Collected path from RF scan for user {session.user_id}: {path} (hash: {packet_hash[:8]}..., time: {time_diff:.2f}s)")
                        else:
                            self.logger.debug(f"Matched hash {packet_hash[:8]}... in RF scan but couldn't extract path for user {session.user_id}")
                    elif packet_hash:
                        mismatching_count += 1
                        # Only log first few mismatches to avoid spam
                        if mismatching_count <= 3:
                            self.logger.debug(f"✗ RF scan hash mismatch for user {session.user_id} - target: {session.target_packet_hash[:8]}..., found: {packet_hash[:8]}... (time: {time_diff:.2f}s)")
            
            if matching_count > 0 or mismatching_count > 0:
                self.logger.debug(f"RF scan complete for user {session.user_id}: {matching_count} matching, {mismatching_count} mismatching packets")
        except Exception as e:
            self.logger.debug(f"Error scanning recent RF data for user {session.user_id}: {e}")
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the multitest command"""
        user_id = message.sender_id or "unknown"
        
        # Use lock to prevent concurrent execution from interfering
        async with self._get_execution_lock():
            # Check if user already has an active session
            if user_id in self._active_sessions:
                existing_session = self._active_sessions[user_id]
                elapsed = time.time() - existing_session.listening_start_time
                if elapsed < existing_session.listening_duration:
                    # User already has an active session - silently ignore second Mt
                    # so the first session can complete and send its response
                    return True
            
            # Record execution time BEFORE starting async work to prevent race conditions
            self.record_execution(user_id)
            
            # Determine listening duration based on command variant
            content = message.content.strip()
            if content.startswith('!'):
                content = content[1:].strip()
            
            content_lower = content.lower()
            listening_duration = 6.0  # Default
            # Check for variants: "mt long", "mt xlong", "multitest long", "multitest xlong"
            if content_lower.startswith('mt ') or content_lower.startswith('multitest '):
                parts = content_lower.split()
                if len(parts) >= 2 and parts[0] in ['mt', 'multitest']:
                    variant = parts[1]
                    if variant == 'long':
                        listening_duration = 10.0
                        self.logger.info(f"Multitest command (long) executed by {user_id} - starting 10 second listening window")
                    elif variant == 'xlong':
                        listening_duration = 14.0
                        self.logger.info(f"Multitest command (xlong) executed by {user_id} - starting 14 second listening window")
                    else:
                        self.logger.info(f"Multitest command executed by {user_id} - starting 6 second listening window")
                else:
                    self.logger.info(f"Multitest command executed by {user_id} - starting 6 second listening window")
            else:
                self.logger.info(f"Multitest command executed by {user_id} - starting 6 second listening window")
            
            # Get RF data for the triggering message (contains pre-calculated packet hash)
            rf_data = self.get_rf_data_for_message(message)
            if not rf_data:
                response = "Error: Could not find packet data for this message. Please try again."
                await self.send_response(message, response)
                return True
            
            # Use pre-calculated packet hash if available, otherwise calculate it
            packet_hash = rf_data.get('packet_hash')
            if not packet_hash and rf_data.get('raw_hex'):
                # Fallback: calculate hash if not stored (for older RF data)
                # IMPORTANT: Must use same payload_type that was used during ingestion
                payload_type = None
                routing_info = rf_data.get('routing_info', {})
                if routing_info:
                    payload_type = routing_info.get('payload_type')
                packet_hash = calculate_packet_hash(rf_data['raw_hex'], payload_type)
            
            if not packet_hash:
                response = "Error: Could not calculate packet hash for this message. Please try again."
                await self.send_response(message, response)
                return True
            
            # Store the timestamp of the triggering message to avoid collecting older packets
            triggering_rf_timestamp = rf_data.get('timestamp', time.time())
            
            # Also extract path from the triggering message itself
            initial_path = self.extract_path_from_message(message)
            # Also try to extract from RF data (more reliable)
            if not initial_path and rf_data:
                initial_path = self.extract_path_from_rf_data(rf_data)
            
            if initial_path:
                self.logger.debug(f"Initial path from triggering message for user {user_id}: {initial_path}")
            
            # Create a new session for this user
            session = MultitestSession(
                user_id=user_id,
                target_packet_hash=packet_hash,
                triggering_timestamp=triggering_rf_timestamp,
                listening_start_time=time.time(),
                listening_duration=listening_duration,
                collected_paths=set(),
                initial_path=initial_path
            )
            
            # Add initial path if available
            if initial_path:
                session.collected_paths.add(initial_path)
            
            # Register this session
            self._active_sessions[user_id] = session
            
            # Register this command instance as the active listener (if not already registered)
            # Store reference in message handler so it can call on_message_received
            if self.bot.message_handler.multitest_listener is None:
                self.bot.message_handler.multitest_listener = self
            
            self.logger.info(f"Tracking packet hash for user {user_id}: {packet_hash[:16]}... (full: {packet_hash})")
            self.logger.debug(f"Triggering message timestamp for user {user_id}: {triggering_rf_timestamp}")
        
        # Release lock before async sleep to allow other users to start their sessions
        # Also scan recent RF data for matching hashes (in case messages haven't been processed yet)
        # But only include packets that arrived at or after the triggering message
        self._scan_recent_rf_data(session)
        
        try:
            # Wait for the listening duration
            await asyncio.sleep(session.listening_duration)
        finally:
            # Re-acquire lock to clean up session
            async with self._get_execution_lock():
                # Remove this session
                if user_id in self._active_sessions:
                    del self._active_sessions[user_id]
                
                # Unregister listener if no more active sessions
                if not self._active_sessions and self.bot.message_handler.multitest_listener == self:
                    self.bot.message_handler.multitest_listener = None
        
        # Do a final scan of RF data in case any matching packets arrived
        self._scan_recent_rf_data(session)
        
        # Store hash for error message before clearing it
        tracking_hash = session.target_packet_hash
        
        # Format the collected paths
        if session.collected_paths:
            # Sort paths for consistent output
            sorted_paths = sorted(session.collected_paths)
            paths_text = "\n".join(sorted_paths)
            path_count = len(sorted_paths)
            
            # Use configured format if available, otherwise use default
            if self.response_format:
                try:
                    response = self.response_format.format(
                        sender=message.sender_id or "Unknown",
                        path_count=path_count,
                        paths=paths_text,
                        listening_duration=int(session.listening_duration)
                    )
                except (KeyError, ValueError) as e:
                    # If formatting fails, fall back to default
                    self.logger.debug(f"Error formatting multitest response: {e}, using default format")
                    response = f"Found {path_count} unique path(s):\n{paths_text}"
            else:
                # Default format
                response = f"Found {path_count} unique path(s):\n{paths_text}"
        else:
            # Provide more helpful error message with diagnostic info
            matching_packets = 0
            if self.bot.message_handler.recent_rf_data and tracking_hash:
                for rf_data in self.bot.message_handler.recent_rf_data:
                    if rf_data.get('packet_hash') == tracking_hash:
                        matching_packets += 1
            
            if tracking_hash is None:
                response = ("Error: Could not determine packet hash for tracking. "
                           "The triggering message may not have valid packet data.")
            elif matching_packets > 0:
                response = (f"No paths extracted from {matching_packets} matching packet(s) "
                           f"(hash: {tracking_hash}). "
                           f"Packets may be direct (0 hops) or path extraction failed.")
            else:
                response = (f"No matching packets found during {session.listening_duration}s window. "
                           f"Tracking hash: {tracking_hash}. ")
        
        # Wait for bot TX rate limiter cooldown to expire before sending
        # This ensures we respond even if another command put the bot on cooldown
        await self.bot.bot_tx_rate_limiter.wait_for_tx()
        
        # Also wait for user rate limiter if needed
        if not self.bot.rate_limiter.can_send():
            wait_time = self.bot.rate_limiter.time_until_next()
            if wait_time > 0:
                self.logger.info(f"Waiting {wait_time:.1f} seconds for rate limiter")
                await asyncio.sleep(wait_time + 0.1)  # Small buffer
        
        # Send the response
        await self.send_response(message, response)
        
        return True

