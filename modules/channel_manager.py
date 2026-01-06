#!/usr/bin/env python3
"""
Channel management functionality for the MeshCore Bot
Handles efficient concurrent channel fetching with caching
"""

import asyncio
import sys
import os
import hashlib
import copy
from typing import Dict, Any, List, Optional
from meshcore import EventType


class ChannelManager:
    """Manages channel operations and information with enhanced concurrent fetching"""
    
    def __init__(self, bot, max_channels: int = 40):
        """
        Initialize the channel manager
        
        Args:
            bot: The MeshCore bot instance
            max_channels: Maximum number of channels to fetch (default 40)
        """
        self.bot = bot
        self.logger = bot.logger
        self.max_channels = max_channels
        self._channels_cache: Dict[int, Dict[str, Any]] = {}
        self._cache_valid = False
        self._fetch_timeout = 2.0  # Timeout for individual channel fetches
    
    async def fetch_channels(self):
        """Fetch channels from the MeshCore node using enhanced concurrent fetching"""
        self.logger.info("Fetching channels from MeshCore node using enhanced concurrent method...")
        try:
            # Wait a moment for the device to be ready
            await asyncio.sleep(2)
            
            # Fetch all channels concurrently
            channels = await self.fetch_all_channels(force_refresh=True)
            
            if channels:
                self.logger.info(f"Successfully fetched {len(channels)} channels from MeshCore node")
                for channel in channels:
                    channel_name = channel.get('channel_name', f'Channel{channel.get("channel_idx", "?")}')
                    channel_idx = channel.get('channel_idx', '?')
                    if channel_name:  # Only log non-empty channel names
                        self.logger.info(f"  Channel {channel_idx}: {channel_name}")
                    else:
                        self.logger.debug(f"  Channel {channel_idx}: (empty)")
            else:
                self.logger.warning("No channels found on MeshCore node")
                self.bot.meshcore.channels = {}
                
        except Exception as e:
            self.logger.error(f"Failed to fetch channels: {e}")
            self.bot.meshcore.channels = {}
    
    async def fetch_all_channels(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Fetch all channels efficiently using optimized sequential requests
        
        Args:
            force_refresh: If True, bypass cache and fetch fresh data
            
        Returns:
            List of channel dictionaries with channel info
        """
        if not force_refresh and self._cache_valid:
            return self._get_cached_channels()
        
        self.logger.info(f"Fetching all channels (0-{self.max_channels-1}) with optimized sequential method...")
        
        # Check if device is connected before attempting fetch
        if not hasattr(self.bot, 'connected') or not self.bot.connected:
            self.logger.warning("Device not connected, skipping channel fetch")
            return []
        
        # Clear cache for fresh fetch
        self._channels_cache.clear()
        valid_channels = []
        consecutive_timeouts = 0
        max_consecutive_timeouts = 3  # Abort if first 3 channels all timeout
        
        # Fetch channels sequentially but with optimized logic
        for channel_idx in range(self.max_channels):
            try:
                result = await self._fetch_single_channel(channel_idx)
                
                if result and result.get("channel_name"):
                    self._channels_cache[channel_idx] = result
                    valid_channels.append(result)
                    consecutive_timeouts = 0  # Reset timeout counter on success
                    self.logger.debug(f"Found channel {channel_idx}: {result.get('channel_name')}")
                elif result and not result.get("channel_name"):
                    # Empty channel - log but don't stop
                    consecutive_timeouts = 0  # Reset timeout counter (device responded)
                    self.logger.debug(f"Channel {channel_idx} is empty")
                else:
                    # No response - channel doesn't exist
                    consecutive_timeouts += 1
                    self.logger.debug(f"Channel {channel_idx} not found")
                    
                    # If first few channels all timeout, device is likely unresponsive
                    if consecutive_timeouts >= max_consecutive_timeouts and channel_idx < max_consecutive_timeouts:
                        self.logger.warning(f"First {max_consecutive_timeouts} channels all timed out - device may be unresponsive, aborting fetch")
                        break
                
                # Small delay between requests to avoid overwhelming the device
                await asyncio.sleep(0.1)
                
            except Exception as e:
                consecutive_timeouts += 1
                self.logger.debug(f"Error fetching channel {channel_idx}: {e}")
                
                # Abort early if device appears unresponsive
                if consecutive_timeouts >= max_consecutive_timeouts and channel_idx < max_consecutive_timeouts:
                    self.logger.warning(f"Multiple consecutive errors fetching channels - device may be unresponsive, aborting fetch")
                    break
                continue
        
        self._cache_valid = True
        self.logger.info(f"Successfully fetched {len(valid_channels)} channels")
        
        # Update the bot's meshcore channels for compatibility
        self.bot.meshcore.channels = self._channels_cache
        
        # Store channels in database for web viewer access
        self._store_channels_in_db(valid_channels)
        
        return valid_channels
    
    def _store_channels_in_db(self, channels: List[Dict[str, Any]]):
        """Store channel information in database for web viewer access (full refresh - clears all first)"""
        try:
            import sqlite3
            db_path = self.bot.db_manager.db_path
            
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                
                # Clear existing channels (full refresh)
                cursor.execute('DELETE FROM channels')
                
                # Insert all channels
                for channel in channels:
                    self._insert_channel_in_db(cursor, channel)
                
                conn.commit()
                self.logger.debug(f"Stored {len(channels)} channels in database (full refresh)")
        except Exception as e:
            self.logger.warning(f"Failed to store channels in database: {e}")
    
    def _store_single_channel_in_db(self, channel: Dict[str, Any]):
        """Store or update a single channel in database (without clearing others)"""
        try:
            import sqlite3
            db_path = self.bot.db_manager.db_path
            
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                self._insert_channel_in_db(cursor, channel)
                conn.commit()
                self.logger.debug(f"Stored/updated channel {channel.get('channel_idx')} in database")
        except Exception as e:
            self.logger.warning(f"Failed to store single channel in database: {e}")
    
    def _insert_channel_in_db(self, cursor, channel: Dict[str, Any]):
        """Helper method to insert/update a single channel in database"""
        channel_idx = channel.get('channel_idx')
        channel_name = channel.get('channel_name', '')
        channel_key_hex = channel.get('channel_key_hex', '')
        
        # Determine channel type based on key derivation
        # If key matches hashtag derivation, it's a hashtag channel
        channel_type = 'hashtag'  # Default assumption
        if channel_name and channel_key_hex:
            # Check if key matches hashtag derivation
            expected_key = self.generate_hashtag_key(channel_name)
            if expected_key.hex() == channel_key_hex:
                channel_type = 'hashtag'
            else:
                channel_type = 'custom'
        
        if channel_name:  # Only store non-empty channels
            cursor.execute('''
                INSERT OR REPLACE INTO channels 
                (channel_idx, channel_name, channel_type, channel_key_hex, last_updated)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (channel_idx, channel_name, channel_type, channel_key_hex))
    
    async def _fetch_single_channel(self, channel_idx: int) -> Optional[Dict[str, Any]]:
        """
        Fetch a single channel with error handling
        
        Args:
            channel_idx: The channel index to fetch
            
        Returns:
            Channel info dictionary or None if not configured
        """
        try:
            # Create a future to capture the channel info event
            channel_event = None
            event_received = asyncio.Event()
            
            async def on_channel_info(event):
                nonlocal channel_event
                if event.payload.get('channel_idx') == channel_idx:
                    channel_event = event
                    event_received.set()
            
            # Subscribe to channel info events
            subscription = self.bot.meshcore.subscribe(EventType.CHANNEL_INFO, on_channel_info)
            
            try:
                # Send the command (suppress raw JSON output)
                from meshcore_cli.meshcore_cli import next_cmd
                
                with open(os.devnull, 'w') as devnull:
                    old_stdout = sys.stdout
                    sys.stdout = devnull
                    try:
                        await next_cmd(self.bot.meshcore, ["get_channel", str(channel_idx)])
                    finally:
                        sys.stdout = old_stdout
                
                # Wait for the event with timeout
                try:
                    await asyncio.wait_for(event_received.wait(), timeout=self._fetch_timeout)
                except asyncio.TimeoutError:
                    self.logger.debug(f"Timeout waiting for channel {channel_idx} response")
                    return None
                
                # Check if we got the channel info
                if channel_event and channel_event.payload:
                    payload = channel_event.payload
                    
                    # Store channel key for decryption
                    channel_secret = payload.get('channel_secret', b'')
                    if isinstance(channel_secret, bytes) and len(channel_secret) == 16:
                        # Convert to hex for easier handling
                        payload['channel_key_hex'] = channel_secret.hex()
                    
                    # Check if this is an empty channel (all-zero channel secret)
                    if isinstance(channel_secret, bytes) and channel_secret == b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00':
                        self.logger.debug(f"Channel {channel_idx} is empty (all-zero secret)")
                        return None
                    
                    return payload
                else:
                    self.logger.debug(f"No channel {channel_idx} found")
                    return None
                    
            finally:
                # Unsubscribe
                self.bot.meshcore.unsubscribe(subscription)
                
        except asyncio.TimeoutError:
            self.logger.debug(f"Timeout fetching channel {channel_idx}")
            return None
        except Exception as e:
            self.logger.debug(f"Error fetching channel {channel_idx}: {e}")
            return None
    
    def _get_cached_channels(self) -> List[Dict[str, Any]]:
        """Get channels from cache, sorted by index"""
        return [
            self._channels_cache[idx] 
            for idx in sorted(self._channels_cache.keys())
        ]
    
    async def get_channel(self, channel_idx: int, use_cache: bool = True) -> Optional[Dict[str, Any]]:
        """
        Get a specific channel, optionally from cache
        
        Args:
            channel_idx: The channel index
            use_cache: If True, return from cache if available
            
        Returns:
            Channel info dictionary or None
        """
        if use_cache and channel_idx in self._channels_cache:
            return self._channels_cache[channel_idx]
        
        result = await self._fetch_single_channel(channel_idx)
        
        if result:
            self._channels_cache[channel_idx] = result
        
        return result
    
    def get_channel_name(self, channel_num: int) -> str:
        """Get channel name from channel number"""
        if channel_num in self._channels_cache:
            channel_info = self._channels_cache[channel_num]
            return channel_info.get('channel_name', f"Channel{channel_num}")
        else:
            self.logger.warning(f"Channel {channel_num} not found in cached channels")
            return f"Channel{channel_num}"
    
    def get_channel_number(self, channel_name: str) -> Optional[int]:
        """
        Get channel number from channel name
        
        Args:
            channel_name: The channel name to look up
            
        Returns:
            Channel number if found, None if not found (to distinguish from channel 0)
        """
        for num, channel_info in self._channels_cache.items():
            if channel_info.get('channel_name', '').lower() == channel_name.lower():
                return num
        
        self.logger.warning(f"Channel name '{channel_name}' not found in cached channels")
        return None
    
    def get_channel_key(self, channel_num: int) -> str:
        """Get channel encryption key from channel number"""
        if channel_num in self._channels_cache:
            channel_info = self._channels_cache[channel_num]
            return channel_info.get('channel_key_hex', '')
        return ''
    
    def get_channel_info(self, channel_num: int) -> dict:
        """Get complete channel information including name and key"""
        if channel_num in self._channels_cache:
            channel_info = self._channels_cache[channel_num]
            return {
                'name': self.get_channel_name(channel_num),
                'key': self.get_channel_key(channel_num),
                'info': channel_info
            }
        return {'name': f"Channel{channel_num}", 'key': '', 'info': {}}
    
    def get_channel_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Find a channel by name from cache
        
        Args:
            name: The channel name to search for
            
        Returns:
            Channel info dictionary or None
        """
        if not self._cache_valid:
            self.logger.warning("Cache not valid, call fetch_all_channels() first")
            return None
        
        name_lower = name.lower()
        for channel in self._channels_cache.values():
            if channel.get("channel_name", "").lower() == name_lower:
                return channel
        
        return None
    
    def get_configured_channels(self) -> List[Dict[str, Any]]:
        """
        Get only configured channels from cache
        
        Returns:
            List of configured channels
        """
        if not self._cache_valid:
            self.logger.warning("Cache not valid, call fetch_all_channels() first")
            return []
        
        return [
            ch for ch in self._channels_cache.values()
            if ch.get("channel_name") and ch["channel_name"].strip()
        ]
    
    def invalidate_cache(self):
        """Invalidate the channels cache"""
        self._cache_valid = False
        self.logger.debug("Channels cache invalidated")
    
    @staticmethod
    def generate_hashtag_key(channel_name: str) -> bytes:
        """
        Generate a hashtag channel key from the channel name
        
        The key is the first 16 bytes of the SHA256 hash of the channel name
        (including the # symbol), converted to lowercase.
        
        Args:
            channel_name: The channel name (e.g., "#general" or "general")
            
        Returns:
            16-byte key for the hashtag channel
        """
        # Ensure channel name starts with # and is lowercase
        if not channel_name.startswith('#'):
            channel_name = '#' + channel_name
        channel_name_lower = channel_name.lower()
        
        # Compute SHA256 hash
        hash_obj = hashlib.sha256(channel_name_lower.encode('utf-8'))
        hash_bytes = hash_obj.digest()
        
        # Take first 16 bytes
        return hash_bytes[:16]
    
    async def add_hashtag_channel(self, channel_idx: int, channel_name: str) -> bool:
        """
        Add or update a hashtag channel on the radio
        
        Hashtag channels use publicly derivable keys based on the channel name.
        The firmware automatically generates the key when the channel name starts with #.
        
        Args:
            channel_idx: The channel index (0-39)
            channel_name: The name of the channel (with or without # prefix)
            
        Returns:
            True if successful, False otherwise
        """
        # Ensure channel name has # prefix for consistency
        if not channel_name.startswith('#'):
            channel_name = '#' + channel_name
        
        self.logger.info(f"Adding hashtag channel {channel_idx}: {channel_name}")
        
        # Use the simplified add_channel method - firmware will auto-generate key
        return await self.add_channel(channel_idx, channel_name)
    
    async def add_channel(self, channel_idx: int, channel_name: str, channel_secret: Optional[bytes] = None, channel_secret_hex: Optional[str] = None) -> bool:
        """
        Add or update a channel on the radio
        
        For hashtag channels (name starts with #), the firmware automatically generates the key.
        For custom channels, provide either channel_secret (bytes) or channel_secret_hex (hex string).
        
        Args:
            channel_idx: The channel index (0-39)
            channel_name: The name of the channel
            channel_secret: Optional 16-byte encryption key for custom channels
            channel_secret_hex: Optional hex string (32 chars) for the encryption key. Takes precedence over channel_secret.
            
        Returns:
            True if successful, False otherwise
        """
        if not self.bot.connected or not self.bot.meshcore:
            self.logger.error("Not connected to MeshCore node")
            return False
        
        if channel_idx < 0 or channel_idx >= self.max_channels:
            self.logger.error(f"Channel index {channel_idx} out of range (0-{self.max_channels-1})")
            return False
        
        try:
            # Check if this is a hashtag channel (firmware auto-generates key)
            is_hashtag = channel_name.startswith('#')
            
            # For custom channels, validate and prepare the key
            if not is_hashtag:
                if channel_secret_hex:
                    # Validate hex string
                    if len(channel_secret_hex) != 32:
                        self.logger.error(f"Channel secret hex must be exactly 32 characters (16 bytes), got {len(channel_secret_hex)}")
                        return False
                    try:
                        channel_secret = bytes.fromhex(channel_secret_hex)
                    except ValueError as e:
                        self.logger.error(f"Invalid hex string for channel secret: {e}")
                        return False
                elif channel_secret is None:
                    self.logger.error("Custom channel requires a channel key (channel_secret or channel_secret_hex)")
                    return False
                elif len(channel_secret) != 16:
                    self.logger.error(f"Channel secret must be exactly 16 bytes, got {len(channel_secret)}")
                    return False
                
                self.logger.info(f"Adding custom channel {channel_idx}: {channel_name} (key: {channel_secret.hex()[:8]}...)")
            else:
                self.logger.info(f"Adding hashtag channel {channel_idx}: {channel_name} (firmware will auto-generate key)")
            
            # Use meshcore.commands.set_channel API directly
            if hasattr(self.bot.meshcore, 'commands') and hasattr(self.bot.meshcore.commands, 'set_channel'):
                # For hashtag channels, just pass the name (firmware generates key)
                if is_hashtag:
                    res = await self.bot.meshcore.commands.set_channel(channel_idx, channel_name)
                else:
                    # For custom channels, we need to pass the key
                    # Check if set_channel accepts a key parameter
                    # Try with key as third parameter
                    try:
                        res = await self.bot.meshcore.commands.set_channel(channel_idx, channel_name, channel_secret)
                    except TypeError:
                        # If that doesn't work, try with hex string
                        try:
                            res = await self.bot.meshcore.commands.set_channel(channel_idx, channel_name, channel_secret_hex or channel_secret.hex())
                        except TypeError:
                            # Fallback to CLI method if API doesn't support key parameter
                            self.logger.warning("meshcore.commands.set_channel doesn't accept key parameter, using CLI fallback")
                            return await self._add_channel_via_cli(channel_idx, channel_name, channel_secret.hex() if channel_secret else channel_secret_hex)
                
                # Check for errors
                if hasattr(res, 'type') and res.type == EventType.ERROR:
                    self.logger.error(f"Failed to set channel {channel_idx}: {res.payload if hasattr(res, 'payload') else 'Unknown error'}")
                    return False
                
                # Fetch the channel back to get the generated key and verify
                res = await self.bot.meshcore.commands.get_channel(channel_idx)
                
                if hasattr(res, 'type') and res.type == EventType.ERROR:
                    self.logger.error(f"Failed to get channel {channel_idx} after setting: {res.payload if hasattr(res, 'payload') else 'Unknown error'}")
                    return False
                
                # Extract channel info from response
                if hasattr(res, 'payload'):
                    channel_info = res.payload
                else:
                    # Fallback: try to get from event subscription
                    channel_info = await self._fetch_single_channel(channel_idx)
                    if not channel_info:
                        self.logger.error(f"Could not retrieve channel {channel_idx} after setting")
                        return False
                
                # Verify channel was set correctly
                if channel_info.get('channel_name') != channel_name:
                    self.logger.error(f"Channel name mismatch: expected {channel_name}, got {channel_info.get('channel_name')}")
                    return False
                
                # For custom channels, verify the key matches
                if not is_hashtag:
                    channel_secret_from_device = channel_info.get('channel_secret', b'')
                    if isinstance(channel_secret_from_device, bytes) and channel_secret_from_device != channel_secret:
                        self.logger.error(f"Channel key mismatch for custom channel {channel_idx}")
                        return False
                
                # Update cache and database
                channel_info['channel_key_hex'] = channel_info.get('channel_secret', b'').hex() if isinstance(channel_info.get('channel_secret'), bytes) else ''
                self._channels_cache[channel_idx] = channel_info
                self._store_single_channel_in_db(channel_info)
                
                self.logger.info(f"Successfully added channel {channel_idx}: {channel_name}")
                return True
            else:
                # Fallback to CLI method if commands API not available
                self.logger.warning("meshcore.commands.set_channel not available, using CLI fallback")
                channel_secret_hex = channel_secret.hex() if channel_secret else channel_secret_hex
                if is_hashtag:
                    # For hashtag, generate key ourselves as fallback
                    channel_secret = self.generate_hashtag_key(channel_name)
                    channel_secret_hex = channel_secret.hex()
                return await self._add_channel_via_cli(channel_idx, channel_name, channel_secret_hex)
                
        except Exception as e:
            self.logger.error(f"Error adding channel {channel_idx}: {e}")
            import traceback
            self.logger.debug(traceback.format_exc())
            return False
    
    async def _add_channel_via_cli(self, channel_idx: int, channel_name: str, channel_secret_hex: str) -> bool:
        """
        Fallback method to add channel using CLI wrapper (for older meshcore versions)
        
        Args:
            channel_idx: The channel index
            channel_name: The channel name
            channel_secret_hex: The channel key as hex string
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Subscribe to channel info events to confirm the channel was set
            channel_set = False
            event_received = asyncio.Event()
            
            async def on_channel_info(event):
                nonlocal channel_set
                # Copy payload immediately to avoid segfault if event is freed
                payload = copy.deepcopy(event.payload) if hasattr(event, 'payload') else None
                if payload and payload.get('channel_idx') == channel_idx:
                    if payload.get('channel_name') == channel_name:
                        channel_set = True
                        event_received.set()
            
            subscription = self.bot.meshcore.subscribe(EventType.CHANNEL_INFO, on_channel_info)
            
            try:
                from meshcore_cli.meshcore_cli import next_cmd
                
                # Suppress raw JSON output
                with open(os.devnull, 'w') as devnull:
                    old_stdout = sys.stdout
                    sys.stdout = devnull
                    try:
                        await next_cmd(
                            self.bot.meshcore, 
                            ["set_channel", str(channel_idx), channel_name, channel_secret_hex]
                        )
                    finally:
                        sys.stdout = old_stdout
                
                # Wait for confirmation with timeout
                try:
                    await asyncio.wait_for(event_received.wait(), timeout=self._fetch_timeout * 2)
                except asyncio.TimeoutError:
                    self.logger.warning(f"Timeout waiting for channel {channel_idx} set confirmation")
                    await asyncio.sleep(0.5)
                    result = await self._fetch_single_channel(channel_idx)
                    if result and result.get('channel_name') == channel_name:
                        channel_set = True
                
                if channel_set:
                    # Update cache
                    result = await self._fetch_single_channel(channel_idx)
                    if result:
                        self._channels_cache[channel_idx] = result
                        self._store_single_channel_in_db(result)
                        self.logger.info(f"Successfully added channel {channel_idx}: {channel_name}")
                        return True
                    else:
                        self.logger.warning(f"Channel {channel_idx} was set but could not be verified")
                        return False
                else:
                    self.logger.error(f"Failed to set channel {channel_idx}")
                    return False
                    
            finally:
                self.bot.meshcore.unsubscribe(subscription)
                
        except Exception as e:
            self.logger.error(f"Error in CLI fallback for channel {channel_idx}: {e}")
            return False
    
    async def remove_channel(self, channel_idx: int) -> bool:
        """
        Remove a channel from the radio by clearing it
        
        Args:
            channel_idx: The channel index to remove
            
        Returns:
            True if successful, False otherwise
        """
        if not self.bot.connected or not self.bot.meshcore:
            self.logger.error("Not connected to MeshCore node")
            return False
        
        if channel_idx < 0 or channel_idx >= self.max_channels:
            self.logger.error(f"Channel index {channel_idx} out of range (0-{self.max_channels-1})")
            return False
        
        try:
            self.logger.info(f"Removing channel {channel_idx}")
            
            # Create all-zero channel secret (16 bytes) to clear the channel
            empty_secret = b'\x00' * 16
            empty_secret_hex = empty_secret.hex()
            
            # Subscribe to channel info events to confirm the channel was cleared
            channel_cleared = False
            event_received = asyncio.Event()
            
            async def on_channel_info(event):
                nonlocal channel_cleared
                # Copy payload immediately to avoid segfault if event is freed
                payload = copy.deepcopy(event.payload) if hasattr(event, 'payload') else None
                if payload and payload.get('channel_idx') == channel_idx:
                    event_secret = payload.get('channel_secret', b'')
                    # Check if the channel was cleared (all zeros or empty name)
                    if isinstance(event_secret, bytes) and event_secret == empty_secret:
                        channel_cleared = True
                        event_received.set()
                    elif not payload.get('channel_name') or payload.get('channel_name') == '':
                        channel_cleared = True
                        event_received.set()
            
            subscription = self.bot.meshcore.subscribe(EventType.CHANNEL_INFO, on_channel_info)
            
            try:
                from meshcore_cli.meshcore_cli import next_cmd
                
                # Suppress raw JSON output
                with open(os.devnull, 'w') as devnull:
                    old_stdout = sys.stdout
                    sys.stdout = devnull
                    try:
                        # Clear the channel by setting it with empty name and all-zero secret
                        # Format: set_channel <idx> "" <empty_secret_hex>
                        await next_cmd(
                            self.bot.meshcore, 
                            ["set_channel", str(channel_idx), "", empty_secret_hex]
                        )
                    finally:
                        sys.stdout = old_stdout
                
                # Wait for confirmation with timeout
                try:
                    await asyncio.wait_for(event_received.wait(), timeout=self._fetch_timeout * 2)
                except asyncio.TimeoutError:
                    self.logger.warning(f"Timeout waiting for channel {channel_idx} removal confirmation")
                    # Still try to verify by fetching the channel
                    await asyncio.sleep(0.5)
                    result = await self._fetch_single_channel(channel_idx)
                    if not result or not result.get('channel_name'):
                        channel_cleared = True
                
                if channel_cleared:
                    # Remove from cache
                    if channel_idx in self._channels_cache:
                        del self._channels_cache[channel_idx]
                    # Update database - remove the channel
                    try:
                        import sqlite3
                        db_path = self.bot.db_manager.db_path
                        with sqlite3.connect(db_path) as conn:
                            cursor = conn.cursor()
                            cursor.execute('DELETE FROM channels WHERE channel_idx = ?', (channel_idx,))
                            conn.commit()
                    except Exception as e:
                        self.logger.warning(f"Failed to remove channel from database: {e}")
                    self.logger.info(f"Successfully removed channel {channel_idx}")
                    return True
                else:
                    self.logger.error(f"Failed to remove channel {channel_idx}")
                    return False
                    
            finally:
                # Unsubscribe
                self.bot.meshcore.unsubscribe(subscription)
                
        except Exception as e:
            self.logger.error(f"Error removing channel {channel_idx}: {e}")
            return False