#!/usr/bin/env python3
"""
Message handling functionality for the MeshCore Bot
Processes incoming messages and routes them to appropriate command handlers
"""

import asyncio
import time
import json
import re
import copy
from typing import List, Optional, Dict, Any, Tuple
from meshcore import EventType

from .models import MeshMessage
from .enums import PayloadType, PayloadVersion, RouteType, AdvertFlags, DeviceRole
from .utils import calculate_packet_hash, format_elapsed_display
from .security_utils import sanitize_input


class MessageHandler:
    """Handles incoming messages and routes them to command processors.
    
    This class is responsible for processing various types of MeshCore events,
    including contact messages (DMs), raw data packets, advertisement packets,
    and RF log data. It also maintains caches for SNR/RSSI data and correlates
    messages with routing information.
    """
    
    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        # Cache for storing SNR and RSSI data from RF log events
        self.snr_cache = {}
        self.rssi_cache = {}
        
        # Load configuration for RF data correlation
        self.rf_data_timeout = float(bot.config.get('Bot', 'rf_data_timeout', fallback='15.0'))
        self.message_timeout = float(bot.config.get('Bot', 'message_correlation_timeout', fallback='10.0'))
        self.enhanced_correlation = bot.config.getboolean('Bot', 'enable_enhanced_correlation', fallback=True)
        
        # Time-based cache for recent RF log data
        self.recent_rf_data = []
        
        # Message correlation system to prevent race conditions
        self.pending_messages = {}  # Store messages waiting for RF data
        
        # Enhanced RF data storage with better correlation
        self.rf_data_by_timestamp = {}  # Index by timestamp for faster lookup
        self.rf_data_by_pubkey = {}     # Index by pubkey for exact matches
        
        # Cache memory management
        self._max_rf_cache_size = 1000  # Maximum entries per cache
        self._cache_cleanup_interval = 60  # Cleanup every 60 seconds
        self._last_cache_cleanup = time.time()
        
        # Multitest command listener (for collecting paths during listening window)
        self.multitest_listener = None
        
        self.logger.info(f"RF Data Correlation: timeout={self.rf_data_timeout}s, enhanced={self.enhanced_correlation}")
    
    def _is_old_cached_message(self, timestamp: Any) -> bool:
        """Check if a message timestamp indicates it's from before bot connection.
        
        Args:
            timestamp: Message sender timestamp (int, float, None, or 'unknown').
            
        Returns:
            bool: True if message is from before connection, False otherwise.
        """
        # If no connection time tracked, process all messages (backward compatibility)
        if not hasattr(self.bot, 'connection_time') or self.bot.connection_time is None:
            return False
        
        # Handle invalid/unknown timestamps - process them (they might be current)
        if timestamp is None or timestamp == 'unknown':
            return False
        
        try:
            # Convert timestamp to float for comparison
            msg_time = float(timestamp)
            
            # If timestamp is invalid (0, negative, or far in future), process it
            # (might be device clock sync issue, not necessarily old)
            if msg_time <= 0 or msg_time > time.time() + 3600:  # More than 1 hour in future
                return False
            
            # Check if message timestamp is before connection time
            # Allow small buffer (5 seconds) to account for clock differences
            return msg_time < (self.bot.connection_time - 5)
        except (TypeError, ValueError):
            # If we can't parse timestamp, process the message (safer to process than skip)
            return False
    
    async def handle_contact_message(self, event, metadata=None):
        """Handle incoming contact message (DM).
        
        Processes direct messages, extracts path information, correlates with
        RF data for signal metrics (SNR/RSSI), and forwards to the command processor.
        
        Args:
            event: The MeshCore event object containing the message payload.
            metadata: Optional metadata dictionary associated with the event.
        """
        try:
            # Copy payload immediately to avoid segfault if event is freed
            import copy
            payload = copy.deepcopy(event.payload) if hasattr(event, 'payload') else None
            if payload is None:
                self.logger.warning("Contact message event has no payload")
                return
            
            # Debug: Log the full payload structure
            self.logger.debug(f"Contact message payload: {payload}")
            self.logger.debug(f"Payload keys: {list(payload.keys())}")
            self.logger.debug(f"Event metadata: {event.metadata if hasattr(event, 'metadata') else 'None'}")
            
            self.logger.info(f"Received DM from {payload.get('pubkey_prefix', 'unknown')}: {payload.get('text', '')}")
            
            # Extract path information from contacts using pubkey_prefix
            path_info = "Unknown"
            path_len = payload.get('path_len', 255)
            
            if metadata and 'pubkey_prefix' in metadata:
                pubkey_prefix = metadata.get('pubkey_prefix', '')
                if pubkey_prefix:
                    self.logger.debug(f"Looking up path for pubkey_prefix: {pubkey_prefix}")
                    
                    # Look up the contact to get path information
                    if hasattr(self.bot.meshcore, 'contacts') and self.bot.meshcore.contacts:
                        for contact_key, contact_data in self.bot.meshcore.contacts.items():
                            if contact_data.get('public_key', '').startswith(pubkey_prefix):
                                out_path = contact_data.get('out_path', '')
                                out_path_len = contact_data.get('out_path_len', -1)
                                
                                if out_path and out_path_len > 0:
                                    # Convert hex path to readable node IDs using first 2 chars of pubkey
                                    try:
                                        path_bytes = bytes.fromhex(out_path)
                                        path_nodes = []
                                        for i in range(0, len(path_bytes), 2):
                                            if i + 1 < len(path_bytes):
                                                node_id = int.from_bytes(path_bytes[i:i+2], byteorder='little')
                                                # Convert to 2-character hex representation
                                                path_nodes.append(f"{node_id:02x}")
                                        
                                        path_info = f"{','.join(path_nodes)} ({out_path_len} hops)"
                                        self.logger.debug(f"Found path info: {path_info}")
                                    except Exception as e:
                                        self.logger.debug(f"Error converting path: {e}")
                                        path_info = f"Path: {out_path} ({out_path_len} hops)"
                                    break
                                elif out_path_len == 0:
                                    path_info = "Direct"
                                    self.logger.debug(f"Direct connection: {path_info}")
                                    break
                                else:
                                    path_info = "Unknown path"
                                    self.logger.debug(f"No path info available: {path_info}")
                                    break
            
            # Fallback to basic path logic if no detailed info found
            if path_info == "Unknown":
                if path_len == 255:
                    path_info = "Direct"
                elif path_len > 0:
                    path_info = f"Routed ({path_len} hops)"
                elif path_len == 0:
                    path_info = "Direct"
            
            # Try to decode packet and extract routing information from stored raw data
            decoded_packet = None
            routing_info = None
            # Look for raw packet data in recent RF data
            # Extract packet prefix from message raw_hex for correlation
            message_raw_hex = payload.get('raw_hex', '')
            message_packet_prefix = message_raw_hex[:32] if message_raw_hex else None
            message_pubkey = payload.get('pubkey_prefix', '')  # Keep for contact lookup
            
            if message_packet_prefix:
                recent_rf_data = self.find_recent_rf_data(message_packet_prefix)
            elif message_pubkey:
                # Fallback to pubkey correlation if no raw_hex
                recent_rf_data = self.find_recent_rf_data(message_pubkey)
                if recent_rf_data and recent_rf_data.get('raw_hex'):
                    # Use payload field if available, otherwise fall back to raw_hex
                    payload_hex = recent_rf_data.get('payload')
                    decoded_packet = self.decode_meshcore_packet(recent_rf_data['raw_hex'], payload_hex)
                    if decoded_packet:
                        self.logger.debug(f"Decoded packet for routing from RF data: {decoded_packet}")
                        
                        # Extract routing information
                        if recent_rf_data.get('routing_info'):
                            routing_info = recent_rf_data['routing_info']
                            self.logger.debug(f"Found routing info: {routing_info}")
                
                # If we have routing info, use it for path information
                if routing_info:
                    path_len = routing_info.get('path_length', 0)
                    if path_len > 0:
                        path_hex = routing_info.get('path_hex', '')
                        path_nodes = routing_info.get('path_nodes', [])
                        route_type = routing_info.get('route_type', 'Unknown')
                        
                        # Convert path to readable format
                        if path_nodes:
                            path_info = f"{','.join(path_nodes)} ({path_len} hops via {route_type})"
                        else:
                            path_info = f"Path: {path_hex} ({path_len} hops via {route_type})"
                        
                        self.logger.info(f"🛣️  MESSAGE ROUTING: {path_info}")
                    else:
                        path_info = f"Direct via {routing_info.get('route_type', 'Unknown')}"
                        self.logger.info(f"📡 DIRECT MESSAGE: {path_info}")
            
            # Get additional metadata - try multiple sources for SNR and RSSI
            snr = 'unknown'
            rssi = 'unknown'
            
            # Try to get SNR from payload first - check multiple possible field names
            if 'SNR' in payload:
                snr = payload.get('SNR')
            elif 'snr' in payload:
                snr = payload.get('snr')
            elif 'signal_to_noise' in payload:
                snr = payload.get('signal_to_noise')
            elif 'signal_noise_ratio' in payload:
                snr = payload.get('signal_noise_ratio')
            # Try to get SNR from event metadata if available
            elif metadata:
                if 'snr' in metadata:
                    snr = metadata.get('snr')
                elif 'SNR' in metadata:
                    snr = metadata.get('SNR')
            
            # If still no SNR, try to get it from the cache using pubkey prefix from payload
            if snr == 'unknown':
                pubkey_prefix = payload.get('pubkey_prefix', '')
                if pubkey_prefix and pubkey_prefix in self.snr_cache:
                    snr = self.snr_cache[pubkey_prefix]
                    self.logger.debug(f"Retrieved cached SNR {snr} for pubkey {pubkey_prefix}")
            
            # Try to get RSSI from payload first
            if 'RSSI' in payload:
                rssi = payload.get('RSSI')
            elif 'rssi' in payload:
                rssi = payload.get('rssi')
            elif 'signal_strength' in payload:
                rssi = payload.get('signal_strength')
            # Try to get RSSI from event metadata if available
            elif metadata:
                if 'rssi' in metadata:
                    rssi = metadata.get('rssi')
                elif 'RSSI' in metadata:
                    rssi = metadata.get('RSSI')
            
            # If still no RSSI, try to get it from the cache using pubkey prefix from payload
            if rssi == 'unknown':
                pubkey_prefix = payload.get('pubkey_prefix', '')
                if pubkey_prefix and pubkey_prefix in self.rssi_cache:
                    rssi = self.rssi_cache[pubkey_prefix]
                    self.logger.debug(f"Retrieved cached RSSI {rssi} for pubkey {pubkey_prefix}")
            
            # For DMs, we can't decode the encrypted packet, but we can get SNR/RSSI from the payload
            # For channel messages, we can decode the packet since they use shared keys
            self.logger.debug(f"Processing DM from packet prefix: {message_packet_prefix}, pubkey: {message_pubkey}")
            
            # DMs are encrypted with recipient's public key, so we can't decode the raw packet
            # But we can get SNR/RSSI from the message payload if available
            if 'SNR' in payload:
                snr = payload.get('SNR')
                self.logger.debug(f"Using SNR from DM payload: {snr}")
            elif 'snr' in payload:
                snr = payload.get('snr')
                self.logger.debug(f"Using SNR from DM payload: {snr}")
            
            if 'RSSI' in payload:
                rssi = payload.get('RSSI')
                self.logger.debug(f"Using RSSI from DM payload: {rssi}")
            elif 'rssi' in payload:
                rssi = payload.get('rssi')
                self.logger.debug(f"Using RSSI from DM payload: {rssi}")
            
            # Since DMs don't include SNR/RSSI in payload, try to get it from recent RF data
            # This is a fallback since RF data often comes right before/after the message
            if snr == 'unknown' or rssi == 'unknown':
                recent_rf_data = self.find_recent_rf_data()
                if recent_rf_data:
                    self.logger.debug(f"Found recent RF data for DM: {recent_rf_data}")
                    
                    if snr == 'unknown' and recent_rf_data.get('snr'):
                        snr = recent_rf_data['snr']
                        self.logger.debug(f"Using SNR from recent RF data: {snr}")
                    
                    if rssi == 'unknown' and recent_rf_data.get('rssi'):
                        rssi = recent_rf_data['rssi']
                        self.logger.debug(f"Using RSSI from recent RF data: {rssi}")
            
            # For DMs, we can't determine the actual routing path from encrypted data
            # Use the path_len from the payload (255 means unknown/direct)
            path_len = payload.get('path_len', 255)
            if path_len == 255:
                path_info = "Direct (0 hops)"
            else:
                path_info = f"Routed through {path_len} hops"
            
            self.logger.debug(f"DM path info: {path_info}")
            
            timestamp = payload.get('sender_timestamp', 'unknown')
            
            # Look up contact name from pubkey prefix
            sender_id = payload.get('pubkey_prefix', '')
            sender_name = sender_id  # Default to sender_id
            if hasattr(self.bot.meshcore, 'contacts') and self.bot.meshcore.contacts:
                for contact_key, contact_data in self.bot.meshcore.contacts.items():
                    if contact_data.get('public_key', '').startswith(sender_id):
                        # Use the contact name if available, otherwise use adv_name
                        contact_name = contact_data.get('name', contact_data.get('adv_name', sender_id))
                        sender_name = contact_name
                        break
            
            # Get the full public key from contacts if available
            sender_pubkey = payload.get('pubkey_prefix', '')
            sender_pubkey = sender_id  # Default to sender_id
            if hasattr(self.bot.meshcore, 'contacts') and self.bot.meshcore.contacts:
                for contact_key, contact_data in self.bot.meshcore.contacts.items():
                    if contact_data.get('public_key', '').startswith(sender_id):
                        # Use the full public key from the contact
                        sender_pubkey = contact_data.get('public_key', sender_id)
                        self.logger.debug(f"Found full public key for {sender_name}: {sender_pubkey[:16]}...")
                        break
            
            # Sanitize message content to prevent injection attacks
            # Note: Firmware enforces 150-char limit at hardware level, so we disable length check
            # but still strip control characters for security
            message_content = payload.get('text', '')
            message_content = sanitize_input(message_content, max_length=None, strip_controls=True)

            # Elapsed: "Nms" when device clock is valid, or "Sync Device Clock" when
            # invalid (e.g. T-Deck before GPS sync: 0, future, or far in the past).
            translator = getattr(self.bot, 'translator', None)
            elapsed_str = format_elapsed_display(timestamp, translator)

            # Convert to our message format
            message = MeshMessage(
                content=message_content,
                sender_id=sender_name,
                sender_pubkey=sender_pubkey,
                is_dm=True,
                timestamp=timestamp,
                snr=snr,
                rssi=rssi,
                elapsed=elapsed_str,
                hops=path_len if path_len != 255 else 0,
                path=path_info
            )
            
            # Always decode and log path information for debugging (regardless of keywords)
            recent_rf_data = self.find_recent_rf_data()
            
            # If we have RF data with routing information, update the path with that instead
            if recent_rf_data and recent_rf_data.get('routing_info'):
                rf_routing = recent_rf_data['routing_info']
                if rf_routing.get('path_length', 0) > 0:
                    path_nodes = rf_routing.get('path_nodes', [])
                    route_type = rf_routing.get('route_type', 'Unknown')
                    if path_nodes:
                        message.path = f"{','.join(path_nodes)} ({len(path_nodes)} hops via {route_type})"
                        self.logger.info(f"🛣️  CONTACT USING RF ROUTING: {message.path}")
                    else:
                        message.path = f"{rf_routing.get('path_hex', 'Unknown')} ({rf_routing.get('path_length', 0)} hops via {route_type})"
                        self.logger.info(f"🛣️  CONTACT USING RF ROUTING: {message.path}")
                else:
                    message.path = f"Direct via {rf_routing.get('route_type', 'Unknown')}"
                    self.logger.info(f"📡 CONTACT USING RF ROUTING: {message.path}")
            
            await self._debug_decode_message_path(message, sender_id, recent_rf_data)
            
            # Always attempt packet decoding and log the results for debugging
            await self._debug_decode_packet_for_message(message, sender_id, recent_rf_data)
            
            # Check if this is an old cached message from before bot connection
            if self._is_old_cached_message(timestamp):
                self.logger.debug(f"Skipping old cached message from {sender_name} (timestamp: {timestamp}, connection: {self.bot.connection_time})")
                return  # Read the message to clear cache, but don't process it
            
            await self.process_message(message)
            
        except Exception as e:
            self.logger.error(f"Error handling contact message: {e}")
    
    async def handle_raw_data(self, event, metadata=None):
        """Handle raw data events (full packet data from debug mode).
        
        Processes raw packet data, attempts to decode it, and if successful,
        checking if it's an advertisement packet to track.
        
        Args:
            event: The MeshCore event object containing the raw data payload.
            metadata: Optional metadata dictionary.
        """
        try:
            # Copy payload immediately to avoid segfault if event is freed
            # Make a deep copy to ensure we have all the data we need
            payload = copy.deepcopy(event.payload) if hasattr(event, 'payload') else None
            if payload is None:
                self.logger.warning("RAW_DATA event has no payload")
                return
            
            self.logger.info(f"📦 RAW_DATA EVENT RECEIVED: {payload}")
            self.logger.info(f"📦 Event type: {type(event)}")
            self.logger.info(f"📦 Metadata: {metadata}")
            
            # This should contain the full packet data we need
            if hasattr(payload, 'data') or 'data' in payload:
                raw_data = payload.get('data', payload.data if hasattr(payload, 'data') else None)
                if raw_data:
                    self.logger.info(f"🔍 FULL PACKET DATA: {raw_data}")
                    
                    # Try to decode this as a MeshCore packet
                    if isinstance(raw_data, str):
                        # Convert to hex if it's not already
                        if not raw_data.startswith('0x'):
                            raw_hex = raw_data
                        else:
                            raw_hex = raw_data[2:]  # Remove 0x prefix
                        
                        # Decode the packet
                        packet_info = self.decode_meshcore_packet(raw_hex)
                        if packet_info:
                            self.logger.info(f"✅ SUCCESSFULLY DECODED RAW PACKET: {packet_info}")
                            
                            # Check if this is an advertisement packet and track it
                            await self._process_advertisement_packet(packet_info, metadata)
                        else:
                            self.logger.warning("❌ Failed to decode raw packet data")
                    else:
                        self.logger.warning(f"❌ Unexpected raw data type: {type(raw_data)}")
                else:
                    self.logger.warning("❌ No data field in RAW_DATA event")
            else:
                self.logger.warning(f"❌ Unexpected RAW_DATA payload structure: {payload}")
                
        except Exception as e:
            self.logger.error(f"Error handling raw data event: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
    
    async def _process_advertisement_packet(self, packet_info: Dict, metadata=None):
        """Process advertisement packets for complete repeater tracking.
        
        Extracts node information, location data, and routing path from
        advertisement packets and updates the repeater database.
        
        Args:
            packet_info: Dictionary containing decoded packet information.
            metadata: Optional metadata dictionary with signal metrics.
        """
        try:
            # Check if this is an advertisement packet
            if (packet_info.get('payload_type') == 'ADVERT' or 
                packet_info.get('payload_type_name') == 'ADVERT' or 
                packet_info.get('type') == 'advert'):
                self.logger.debug(f"Processing advertisement packet: {packet_info}")
                
                # Parse the advert payload if we have it
                advert_data = {}
                if 'payload_hex' in packet_info:
                    try:
                        payload_bytes = bytes.fromhex(packet_info['payload_hex'])
                        parsed_advert = self.parse_advert(payload_bytes)
                        if parsed_advert:
                            advert_data = parsed_advert
                            self.logger.info(f"✅ Parsed ADVERT: {advert_data.get('mode', 'Unknown')} - {advert_data.get('name', 'No name')}")
                    except Exception as e:
                        self.logger.warning(f"Failed to parse ADVERT payload: {e}")
                
                # Fallback to basic information if parsing failed
                if not advert_data:
                    advert_data = {
                        'public_key': packet_info.get('sender_id', ''),
                        'name': packet_info.get('name', packet_info.get('adv_name', 'Unknown')),
                        'mode': 'Unknown'
                    }
                
                # Add advert data to packet_info for web viewer
                if advert_data:
                    packet_info['advert_name'] = advert_data.get('name')
                    packet_info['advert_mode'] = advert_data.get('mode')
                    packet_info['advert_public_key'] = advert_data.get('public_key')
                
                # Extract signal information from metadata
                signal_info = {}
                if metadata:
                    signal_info.update(metadata)
                
                # Add hop count if available
                if 'hops' in packet_info:
                    signal_info['hops'] = packet_info['hops']
                
                # Extract packet_hash and path information if available (from routing_info or packet_info)
                packet_hash = None
                out_path = ''
                out_path_len = -1
                
                if 'routing_info' in packet_info and packet_info['routing_info']:
                    routing_info = packet_info['routing_info']
                    packet_hash = routing_info.get('packet_hash')
                    # Extract path information from routing_info
                    path_hex = routing_info.get('path_hex', '')
                    path_length = routing_info.get('path_length', 0)
                    if path_hex and path_length > 0:
                        out_path = path_hex
                        out_path_len = path_length
                    elif path_length == 0:
                        # Direct connection
                        out_path = ''
                        out_path_len = 0
                elif 'packet_hash' in packet_info:
                    packet_hash = packet_info['packet_hash']
                
                # Also check packet_info directly for path information (fallback)
                if out_path_len == -1:
                    if 'path_hex' in packet_info:
                        out_path = packet_info.get('path_hex', '')
                        out_path_len = packet_info.get('path_len', -1)
                    elif 'path_len' in packet_info:
                        out_path_len = packet_info.get('path_len', -1)
                        if out_path_len == 0:
                            out_path = ''
                
                # Add path information to advert_data so it gets saved to the database
                if out_path_len >= 0:
                    advert_data['out_path'] = out_path
                    advert_data['out_path_len'] = out_path_len
                
                # Update mesh graph with edges from the advert path
                # Create edge from advertising device to first hop in path
                if out_path and out_path_len > 0 and hasattr(self.bot, 'mesh_graph') and self.bot.mesh_graph:
                    self._update_mesh_graph_from_advert(advert_data, out_path, out_path_len, packet_info)
                
                # Store complete path in observed_paths table
                if out_path and out_path_len > 0:
                    self._store_observed_path(advert_data, out_path, out_path_len, 'advert', packet_hash=packet_hash)
                
                # Track this advertisement in the complete database
                if hasattr(self.bot, 'repeater_manager'):
                    # Track all advertisements regardless of type
                    success = await self.bot.repeater_manager.track_contact_advertisement(
                        advert_data, signal_info, packet_hash=packet_hash
                    )
                    if success:
                        # Log rich advert information
                        mode = advert_data.get('mode', 'Unknown')
                        name = advert_data.get('name', 'No name')
                        location = ""
                        if 'lat' in advert_data and 'lon' in advert_data:
                            # Try to get resolved location from database if available
                            try:
                                if hasattr(self.bot, 'repeater_manager'):
                                    # Look up the contact to get resolved location
                                    public_key = advert_data.get('public_key')
                                    if public_key:
                                        contact_query = self.bot.db_manager.execute_query(
                                            'SELECT city, state, country FROM complete_contact_tracking WHERE public_key = ?',
                                            (public_key,)
                                        )
                                        if contact_query:
                                            contact = contact_query[0]
                                            city = contact.get('city')
                                            state = contact.get('state')
                                            if city and state:
                                                location = f" at {city}, {state}"
                                            elif city:
                                                location = f" at {city}"
                                            else:
                                                # Fallback to coordinates if no resolved location
                                                location = f" at {advert_data['lat']:.4f},{advert_data['lon']:.4f}"
                                        else:
                                            # No contact found yet, use coordinates
                                            location = f" at {advert_data['lat']:.4f},{advert_data['lon']:.4f}"
                                    else:
                                        # No public key, use coordinates
                                        location = f" at {advert_data['lat']:.4f},{advert_data['lon']:.4f}"
                                else:
                                    # No repeater manager, use coordinates
                                    location = f" at {advert_data['lat']:.4f},{advert_data['lon']:.4f}"
                            except Exception as e:
                                # If lookup fails, fallback to coordinates
                                self.logger.debug(f"Could not get resolved location for logging: {e}")
                                location = f" at {advert_data['lat']:.4f},{advert_data['lon']:.4f}"
                        
                        # Show hop count in log
                        hop_count = signal_info.get('hops', 0)
                        hop_info = f" ({hop_count} hop{'s' if hop_count != 1 else ''})" if hop_count is not None else ""
                        
                        self.logger.info(f"📡 Tracked {mode}: {name}{location}{hop_info}")
                    else:
                        self.logger.warning(f"Failed to track contact advertisement: {advert_data.get('name', 'Unknown')}")
                
        except Exception as e:
            self.logger.error(f"Error processing advertisement packet: {e}")
    
    async def handle_rf_log_data(self, event, metadata=None):
        """Handle RF log data events to cache SNR information and store raw packet data.
        
        Captures low-level RF information (SNR, RSSI) and raw packet data to
        correlate with higher-level messages for detailed signal reporting.
        
        Args:
            event: The MeshCore event object containing RF data.
            metadata: Optional metadata dictionary.
        """
        try:
            # Copy payload immediately to avoid segfault if event is freed
            import copy
            payload = copy.deepcopy(event.payload) if hasattr(event, 'payload') else None
            if payload is None:
                self.logger.warning("RF log data event has no payload")
                return
            
            # Extract SNR from payload
            if 'snr' in payload:
                snr_value = payload.get('snr')
                
                # Use raw_hex prefix for correlation instead of trying to extract pubkey
                raw_hex = payload.get('raw_hex', '')
                packet_prefix = None
                
                if raw_hex:
                    # Use first 32 characters as correlation key (16 bytes)
                    # This provides unique identification while being consistent
                    packet_prefix = raw_hex[:32]
                    self.logger.debug(f"Using packet prefix for correlation: {packet_prefix}")
                
                # Keep pubkey_prefix for contact lookup (from metadata if available)
                pubkey_prefix = None
                if metadata and 'pubkey_prefix' in metadata:
                    pubkey_prefix = metadata.get('pubkey_prefix')
                    self.logger.debug(f"Got pubkey_prefix from metadata: {pubkey_prefix[:16]}...")
                
                if packet_prefix and snr_value is not None:
                    # Cache the SNR value for this packet prefix
                    self.snr_cache[packet_prefix] = snr_value
                    self.logger.debug(f"Cached SNR {snr_value} for packet prefix {packet_prefix}")
                
                # Extract and cache RSSI if available
                if 'rssi' in payload:
                    rssi_value = payload.get('rssi')
                    if packet_prefix and rssi_value is not None:
                        # Cache the RSSI value for this packet prefix
                        self.rssi_cache[packet_prefix] = rssi_value
                        self.logger.debug(f"Cached RSSI {rssi_value} for packet prefix {packet_prefix}")
                
                # Store recent RF data with timestamp for SNR/RSSI matching only
                if packet_prefix:
                    import time
                    current_time = time.time()
                    
                    # Store both raw packet data and extracted payload for analysis
                    raw_hex = payload.get('raw_hex', '')
                    extracted_payload = payload.get('payload', '')
                    payload_length = payload.get('payload_length', 0)
                    
                    # Extract routing information from raw packet if available
                    routing_info = None
                    packet_hash = None
                    if raw_hex:
                        # Use extracted payload if available, otherwise use raw_hex
                        decoded_packet = self.decode_meshcore_packet(raw_hex, extracted_payload)
                        if decoded_packet:
                            # Calculate packet hash for this packet (useful for tracking same message via different paths)
                            # Use extracted_payload if available (actual MeshCore packet), otherwise use raw_hex
                            # This matches the logic in decode_meshcore_packet which prefers extracted_payload
                            # extracted_payload is the actual MeshCore packet without RF wrapper, so use it if available
                            packet_hex_for_hash = extracted_payload if (extracted_payload and len(extracted_payload) > 0) else raw_hex
                            
                            # Ensure we use the numeric payload_type value (not enum or string)
                            payload_type_value = decoded_packet.get('payload_type', None)
                            if payload_type_value is not None:
                                # Handle enum.value if it's an enum
                                if hasattr(payload_type_value, 'value'):
                                    payload_type_value = payload_type_value.value
                                payload_type_value = int(payload_type_value)
                            packet_hash = calculate_packet_hash(packet_hex_for_hash, payload_type_value)
                            
                            # Check if this is a repeat of one of our transmissions
                            if (hasattr(self.bot, 'transmission_tracker') and 
                                self.bot.transmission_tracker and 
                                packet_hash and packet_hash != "0000000000000000"):
                                
                                # Extract repeater prefixes from path - try multiple field names
                                # decode_meshcore_packet returns 'path' not 'path_nodes'
                                path_nodes = decoded_packet.get('path', [])
                                # Also try 'path_nodes' field (from routing_info)
                                if not path_nodes:
                                    path_nodes = decoded_packet.get('path_nodes', [])
                                
                                path_hex = decoded_packet.get('path_hex', '')
                                
                                # If we don't have path_nodes but have path_hex, convert it
                                if not path_nodes and path_hex and len(path_hex) >= 2:
                                    path_nodes = [path_hex[i:i+2] for i in range(0, len(path_hex), 2)]
                                
                                path_string = ','.join(path_nodes) if path_nodes else None
                                
                                # Debug logging
                                if path_nodes:
                                    self.logger.debug(f"📡 Extracting prefixes from path_nodes: {path_nodes}, path_hex: {path_hex}, bot_prefix: {self.bot.transmission_tracker.bot_prefix}")
                                
                                # Try to match this packet hash to a transmission
                                record = self.bot.transmission_tracker.match_packet_hash(
                                    packet_hash, current_time
                                )
                                
                                if record:
                                    # This is one of our transmissions - check for repeats
                                    # Extract repeater prefix from the last hop in the path
                                    # (the repeater that sent this packet to us)
                                    prefixes = self.bot.transmission_tracker.extract_repeater_prefixes_from_path(
                                        path_string, path_nodes
                                    )
                                    
                                    # Log for debugging
                                    if prefixes:
                                        self.logger.info(f"📡 Found {len(prefixes)} repeater prefix(es) in repeat: {', '.join(prefixes)}")
                                    elif path_nodes or path_hex:
                                        self.logger.debug(f"📡 Repeat detected but no repeater prefixes extracted (path_nodes: {path_nodes}, path_hex: {path_hex}, bot_prefix: {self.bot.transmission_tracker.bot_prefix})")
                                    
                                    # Record the repeat
                                    for prefix in prefixes:
                                        self.bot.transmission_tracker.record_repeat(packet_hash, prefix)
                                    
                                    # If no prefixes but we have a path, it might be a direct repeat
                                    # (path contains our own node, so we filter it out)
                                    if not prefixes and (path_nodes or path_hex):
                                        # Still count as a repeat (heard by our radio)
                                        self.bot.transmission_tracker.record_repeat(packet_hash, None)
                            
                            routing_info = {
                                'path_length': decoded_packet.get('path_len', 0),
                                'path_hex': decoded_packet.get('path_hex', ''),
                                'path_nodes': decoded_packet.get('path', []),
                                'route_type': decoded_packet.get('route_type_name', 'Unknown'),
                                'payload_length': payload_length,  # Use the actual payload length
                                'payload_type': decoded_packet.get('payload_type_name', 'Unknown'),
                                'packet_hash': packet_hash  # Store hash for packet tracking
                            }
                            
                            # Log the routing information for analysis
                            if routing_info['path_length'] > 0:
                                # Format path with comma separation (every 2 characters)
                                path_hex = routing_info['path_hex']
                                formatted_path = ','.join([path_hex[i:i+2] for i in range(0, len(path_hex), 2)])
                                log_message = f"🛣️  ROUTING INFO: {routing_info['route_type']} | Path: {formatted_path} ({routing_info['path_length']} bytes) | Payload: {routing_info['payload_length']} bytes | Type: {routing_info['payload_type']}"
                                self.logger.info(log_message)
                            else:
                                log_message = f"📡 DIRECT MESSAGE: {routing_info['route_type']} | Type: {routing_info['payload_type']}"
                                self.logger.info(log_message)
                            
                            # Capture full packet data for web viewer (for all packets)
                            if (hasattr(self.bot, 'web_viewer_integration') and 
                                self.bot.web_viewer_integration and 
                                self.bot.web_viewer_integration.bot_integration):
                                # Use extracted_payload which is the full MeshCore packet
                                # (header + path_len + path + payload, without RF wrapper)
                                decoded_packet['raw_packet_hex'] = extracted_payload if extracted_payload else raw_hex
                                decoded_packet['packet_hash'] = packet_hash
                                self.bot.web_viewer_integration.bot_integration.capture_full_packet_data(decoded_packet)
                            
                            # Process ADVERT packets for contact tracking (regardless of path length)
                            if routing_info['payload_type'] == 'ADVERT':
                                # Add routing_info to decoded_packet so it's available in _process_advertisement_packet
                                decoded_packet['routing_info'] = routing_info
                                # Create signal info from available data
                                signal_info = {
                                    'snr': snr_value,
                                    'rssi': payload.get('rssi') if 'rssi' in payload else None,
                                    'hops': routing_info['path_length']
                                }
                                await self._process_advertisement_packet(decoded_packet, signal_info)
                    
                    rf_data = {
                        'timestamp': current_time,
                        'packet_prefix': packet_prefix,  # Use packet prefix for correlation
                        'pubkey_prefix': pubkey_prefix,  # Keep for contact lookup
                        'snr': snr_value,
                        'rssi': payload.get('rssi') if 'rssi' in payload else None,
                        'raw_hex': raw_hex,  # Full packet data
                        'payload': extracted_payload,  # Extracted payload
                        'payload_length': payload_length,  # Payload length
                        'routing_info': routing_info,  # Extracted routing information
                        'packet_hash': packet_hash  # Packet hash for tracking same message via different paths
                    }
                    self.recent_rf_data.append(rf_data)
                    
                    # Update correlation indexes
                    self.rf_data_by_timestamp[current_time] = rf_data
                    if packet_prefix:
                        if packet_prefix not in self.rf_data_by_pubkey:
                            self.rf_data_by_pubkey[packet_prefix] = []
                        self.rf_data_by_pubkey[packet_prefix].append(rf_data)
                    
                    # Clean up old data from all indexes
                    self._cleanup_stale_cache_entries(current_time)
                    
                    # Try to correlate with any pending messages
                    self.try_correlate_pending_messages(rf_data)
                    
                    self.logger.debug(f"Stored recent RF data with routing info: {rf_data}")
                    
                    # Clean up old pending messages
                    self.cleanup_old_messages()
                        
        except Exception as e:
            self.logger.error(f"Error handling RF log data: {e}")
    
    def extract_path_from_raw_hex(self, raw_hex: str, expected_hops: int) -> Optional[str]:
        """Extract path information directly from raw hex data.
        
        Attempts to find a sequence of node IDs in the raw packet data that matches
        the expected number of hops.
        
        Args:
            raw_hex: Raw packet data as a hex string.
            expected_hops: The expected number of hops in the path.
            
        Returns:
            Optional[str]: Comma-separated path string if found, None otherwise.
        """
        try:
            if not raw_hex or len(raw_hex) < 20:
                return None
            
            # For 0-hop (direct) messages, don't try to extract a path
            if expected_hops == 0:
                self.logger.debug("Direct message (0 hops) - no path to extract")
                return "Direct"
            
            # Skip the header area - don't look for paths in the first 6-8 bytes
            # Header (1 byte) + transport codes (2-4 bytes) + path length (1 byte) = 4-6 bytes minimum
            min_start = 8  # Start looking after header + transport + path length
            
            # Look for path patterns in the hex data, but skip the header area
            # Based on the example: ea9a1503777e5fd5658eea506990ad18...
            # The path 77,7e,5f appears to be at positions 6-11 (3 bytes = 6 hex chars)
            
            # Try different positions where path might be located, but avoid header area
            path_positions = [
                (8, 14),   # Position 8-13 (3 bytes) 
                (10, 16),  # Position 10-15 (3 bytes)
                (12, 18),  # Position 12-17 (3 bytes)
                (14, 20),  # Position 14-19 (3 bytes)
            ]
            
            for start, end in path_positions:
                if end <= len(raw_hex) and start >= min_start:
                    path_hex = raw_hex[start:end]
                    if len(path_hex) >= 6:  # At least 3 bytes
                        # Convert hex to path nodes
                        path_nodes = []
                        for i in range(0, len(path_hex), 2):
                            if i + 1 < len(path_hex):
                                node_hex = path_hex[i:i+2]
                                path_nodes.append(node_hex)
                        
                        if len(path_nodes) == expected_hops:
                            path_string = ','.join(path_nodes)
                            self.logger.debug(f"Found path at position {start}-{end}: {path_string}")
                            return path_string
            
            # If no exact match, try to find any 3-byte pattern that looks like a path
            # But skip the header area
            for i in range(min_start, len(raw_hex) - 6, 2):
                path_hex = raw_hex[i:i+6]
                if len(path_hex) == 6:
                    # Check if this looks like a valid path (all hex chars)
                    if all(c in '0123456789abcdef' for c in path_hex.lower()):
                        path_nodes = [path_hex[j:j+2] for j in range(0, 6, 2)]
                        path_string = ','.join(path_nodes)
                        self.logger.debug(f"Found potential path at position {i}: {path_string}")
                        return path_string
            
            return None
            
        except Exception as e:
            self.logger.debug(f"Error extracting path from raw hex: {e}")
            return None

    def _cleanup_stale_cache_entries(self, current_time: Optional[float] = None) -> None:
        """Remove stale entries from RF data caches and enforce maximum size limits.
        
        Args:
            current_time: Optional timestamp to use as "now". Defaults to time.time().
        """
        if current_time is None:
            current_time = time.time()
        
        # Only run periodic cleanup if enough time has passed
        if current_time - self._last_cache_cleanup < self._cache_cleanup_interval:
            # Still do basic timeout cleanup, but skip size enforcement
            cutoff_time = current_time - self.rf_data_timeout
            
            # Clean timestamp-indexed cache (timeout only)
            stale_timestamps = [ts for ts in self.rf_data_by_timestamp.keys() 
                              if ts < cutoff_time]
            for ts in stale_timestamps:
                del self.rf_data_by_timestamp[ts]
            
            # Clean pubkey-indexed cache (timeout only)
            for pubkey in list(self.rf_data_by_pubkey.keys()):
                self.rf_data_by_pubkey[pubkey] = [data for data in self.rf_data_by_pubkey[pubkey] 
                                                 if current_time - data['timestamp'] < self.rf_data_timeout]
                if not self.rf_data_by_pubkey[pubkey]:
                    del self.rf_data_by_pubkey[pubkey]
            
            # Clean recent_rf_data list (timeout only)
            self.recent_rf_data = [data for data in self.recent_rf_data 
                                 if current_time - data['timestamp'] < self.rf_data_timeout]
            return
        
        # Full cleanup with size enforcement
        self._last_cache_cleanup = current_time
        cutoff_time = current_time - self.rf_data_timeout
        
        # Clean timestamp-indexed cache
        stale_timestamps = [ts for ts in self.rf_data_by_timestamp.keys() 
                          if ts < cutoff_time]
        for ts in stale_timestamps:
            del self.rf_data_by_timestamp[ts]
        
        # Enforce maximum size on timestamp cache (keep most recent)
        if len(self.rf_data_by_timestamp) > self._max_rf_cache_size:
            sorted_items = sorted(self.rf_data_by_timestamp.items(), 
                                 key=lambda x: x[1].get('timestamp', 0), 
                                 reverse=True)
            self.rf_data_by_timestamp = dict(sorted_items[:self._max_rf_cache_size])
        
        # Clean pubkey-indexed cache
        for pubkey in list(self.rf_data_by_pubkey.keys()):
            self.rf_data_by_pubkey[pubkey] = [data for data in self.rf_data_by_pubkey[pubkey] 
                                             if current_time - data['timestamp'] < self.rf_data_timeout]
            if not self.rf_data_by_pubkey[pubkey]:
                del self.rf_data_by_pubkey[pubkey]
        
        # Enforce maximum size on pubkey cache (keep most recent per pubkey)
        total_pubkey_entries = sum(len(entries) for entries in self.rf_data_by_pubkey.values())
        if total_pubkey_entries > self._max_rf_cache_size:
            # Sort all entries by timestamp and keep most recent
            all_pubkey_entries = []
            for pubkey, entries in self.rf_data_by_pubkey.items():
                for entry in entries:
                    all_pubkey_entries.append((pubkey, entry))
            all_pubkey_entries.sort(key=lambda x: x[1].get('timestamp', 0), reverse=True)
            
            # Rebuild pubkey cache with only the most recent entries
            self.rf_data_by_pubkey = {}
            for pubkey, entry in all_pubkey_entries[:self._max_rf_cache_size]:
                if pubkey not in self.rf_data_by_pubkey:
                    self.rf_data_by_pubkey[pubkey] = []
                self.rf_data_by_pubkey[pubkey].append(entry)
        
        # Clean recent_rf_data list
        self.recent_rf_data = [data for data in self.recent_rf_data 
                             if current_time - data['timestamp'] < self.rf_data_timeout]
        
        # Enforce maximum size on recent_rf_data (keep most recent)
        if len(self.recent_rf_data) > self._max_rf_cache_size:
            self.recent_rf_data.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
            self.recent_rf_data = self.recent_rf_data[:self._max_rf_cache_size]

    def find_recent_rf_data(self, correlation_key=None, max_age_seconds=None):
        """Find recent RF data for SNR/RSSI and packet decoding with improved correlation
        
        Args:
            correlation_key: Can be either:
                - packet_prefix (from raw_hex[:32]) for RF data correlation
                - pubkey_prefix (from message payload) for message correlation
        """
        import time
        current_time = time.time()
        
        # Use default timeout if not specified
        if max_age_seconds is None:
            max_age_seconds = self.rf_data_timeout
        
        # Filter recent RF data by age
        recent_data = [data for data in self.recent_rf_data 
                      if current_time - data['timestamp'] < max_age_seconds]
        
        if not recent_data:
            self.logger.debug(f"No recent RF data found within {max_age_seconds}s window")
            return None
        
        # Strategy 1: Try exact packet prefix match first (for RF data correlation)
        if correlation_key:
            for data in recent_data:
                rf_packet_prefix = data.get('packet_prefix', '') or ''
                if rf_packet_prefix == correlation_key:
                    self.logger.debug(f"Found exact packet prefix match: {rf_packet_prefix}")
                    return data
        
        # Strategy 2: Try pubkey prefix match (for message correlation)
        if correlation_key:
            for data in recent_data:
                rf_pubkey_prefix = data.get('pubkey_prefix', '') or ''
                if rf_pubkey_prefix == correlation_key:
                    self.logger.debug(f"Found exact pubkey prefix match: {rf_pubkey_prefix}")
                    return data
        
        # Strategy 3: Try partial packet prefix matches
        if correlation_key:
            for data in recent_data:
                rf_packet_prefix = data.get('packet_prefix', '') or ''
                # Check for partial match (at least 16 characters)
                min_length = min(len(rf_packet_prefix), len(correlation_key), 16)
                if (rf_packet_prefix[:min_length] == correlation_key[:min_length] and min_length >= 16):
                    self.logger.debug(f"Found partial packet prefix match: {rf_packet_prefix[:16]}... matches {correlation_key[:16]}...")
                    return data
        
        # Strategy 4: Use most recent data (fallback for timing issues)
        if recent_data:
            most_recent = max(recent_data, key=lambda x: x['timestamp'])
            packet_prefix = most_recent.get('packet_prefix', 'unknown')
            self.logger.debug(f"Using most recent RF data (fallback): {packet_prefix} at {most_recent['timestamp']}")
            return most_recent
        
        return None
    
    def store_message_for_correlation(self, message_id, message_data):
        """Store a message temporarily to wait for RF data correlation"""
        import time
        self.pending_messages[message_id] = {
            'data': message_data,
            'timestamp': time.time(),
            'processed': False
        }
        self.logger.debug(f"Stored message {message_id} for RF data correlation")
    
    def correlate_message_with_rf_data(self, message_id):
        """Try to correlate a stored message with available RF data"""
        if message_id not in self.pending_messages:
            return None
            
        message_info = self.pending_messages[message_id]
        message_data = message_info['data']
        
        # Try to find RF data for this message
        pubkey_prefix = message_data.get('pubkey_prefix', '')
        rf_data = self.find_recent_rf_data(pubkey_prefix)
        
        if rf_data:
            self.logger.debug(f"Successfully correlated message {message_id} with RF data")
            message_info['processed'] = True
            return rf_data
        
        return None
    
    def cleanup_old_messages(self):
        """Clean up old pending messages that couldn't be correlated"""
        import time
        current_time = time.time()
        
        to_remove = []
        for message_id, message_info in self.pending_messages.items():
            if current_time - message_info['timestamp'] > self.message_timeout:
                to_remove.append(message_id)
        
        for message_id in to_remove:
            del self.pending_messages[message_id]
            self.logger.debug(f"Cleaned up old pending message {message_id}")
    
    def try_correlate_pending_messages(self, rf_data):
        """Try to correlate new RF data with any pending messages"""
        pubkey_prefix = rf_data.get('pubkey_prefix', '') or ''
        
        for message_id, message_info in self.pending_messages.items():
            if message_info['processed']:
                continue
                
            message_pubkey = message_info['data'].get('pubkey_prefix', '') or ''
            
            # Check if this RF data matches the pending message
            if (pubkey_prefix == message_pubkey or 
                (len(pubkey_prefix) >= 16 and len(message_pubkey) >= 16 and 
                 pubkey_prefix[:16] == message_pubkey[:16])):
                self.logger.debug(f"Correlated RF data with pending message {message_id}")
                message_info['processed'] = True
                break
    

    
    def decode_meshcore_packet(self, raw_hex: str, payload_hex: str = None) -> Optional[dict]:
        """
        Decode a MeshCore packet from raw hex data - matches Packet.cpp exactly
        
        Args:
            raw_hex: Raw packet data as hex string (may be RF data or direct MeshCore packet)
            payload_hex: Optional extracted payload hex string (preferred over raw_hex)
            
        Returns:
            Decoded packet information or None if parsing fails
        """
        try:
            # Use payload_hex if provided (this is the actual MeshCore packet)
            if payload_hex:
                self.logger.debug("Using provided payload_hex for decoding")
                hex_data = payload_hex
            elif raw_hex:
                self.logger.debug("Using raw_hex for decoding")
                hex_data = raw_hex
            else:
                self.logger.debug("No packet data provided for decoding")
                return None
            
            # Remove 0x prefix if present (like in your other project)
            if hex_data.startswith('0x'):
                hex_data = hex_data[2:]
            
            byte_data = bytes.fromhex(hex_data)
            
            # Validate minimum packet size
            if len(byte_data) < 2:
                self.logger.error(f"Packet too short: {len(byte_data)} bytes")
                return None
            
            header = byte_data[0]

            # Extract route type
            route_type = RouteType(header & 0x03)
            has_transport = route_type in [RouteType.TRANSPORT_FLOOD, RouteType.TRANSPORT_DIRECT]
            
            # Calculate path length offset based on presence of transport codes
            offset = 1
            if has_transport:
                offset += 4
            
            # Check if we have enough data for path_len
            if len(byte_data) <= offset:
                self.logger.error(f"Packet too short for path_len at offset {offset}: {len(byte_data)} bytes")
                return None
            
            path_len = byte_data[offset]
            offset += 1
            
            # Check if we have enough data for the full path
            if len(byte_data) < offset + path_len:
                self.logger.error(f"Packet too short for path (need {offset + path_len}, have {len(byte_data)})")
                return None
            
            # Extract path
            path_bytes = byte_data[offset:offset + path_len]
            offset += path_len
            
            # Remaining data is payload
            payload = byte_data[offset:]
            
            # Extract payload version (bits 6-7)
            payload_version = PayloadVersion((header >> 6) & 0x03)
            
            # Only accept VER_1 (version 0)
            if payload_version != PayloadVersion.VER_1:
                self.logger.warning(f"Encountered an unknown packet version. Version: {payload_version.value} RAW: {hex_data}")
                return None

            # Extract payload type (bits 2-5)
            payload_type = PayloadType((header >> 2) & 0x0F)

            # Convert path to list of hex values
            path_hex = path_bytes.hex()
            path_values = []
            i = 0
            while i < len(path_hex):
                path_values.append(path_hex[i:i+2])
                i += 2
            
            # Process path based on packet type
            path_info = self._process_packet_path(
                path_bytes, 
                payload, 
                route_type, 
                payload_type
            )
            
            # Extract transport codes if present (only for TRANSPORT_FLOOD and TRANSPORT_DIRECT)
            transport_codes = None
            if has_transport and len(byte_data) >= 5:  # header(1) + transport(4)
                transport_bytes = byte_data[1:5]
                transport_codes = {
                    'code1': int.from_bytes(transport_bytes[0:2], byteorder='little'),
                    'code2': int.from_bytes(transport_bytes[2:4], byteorder='little'),
                    'hex': transport_bytes.hex()
                }
            
            packet_info = {
                'header': f"0x{header:02x}",
                # Raw values for backward compatibility
                'route_type': route_type.value,
                'route_type_name': route_type.name,
                'payload_type': payload_type.value,
                'payload_type_name': payload_type.name,
                'payload_version': payload_version.value,
                # Enum objects for improved type safety
                'route_type_enum': route_type,
                'payload_type_enum': payload_type,
                'payload_version_enum': payload_version,
                # Transport and path information
                'has_transport_codes': has_transport,
                'transport_codes': transport_codes,
                'transport_size': 4 if has_transport else 0,
                'path_len': path_len,
                'path_info': path_info,
                'path': path_values,  # For backward compatibility
                'path_hex': path_hex,
                'payload_hex': payload.hex(),
                'payload_bytes': len(payload)
            }
            
            self.logger.debug(f"Successfully decoded: route={packet_info.get('route_type_name')}, type={packet_info.get('payload_type_name')}")
            return packet_info
            
        except Exception as e:
            # Log as ERROR not DEBUG so we can see what's failing
            self.logger.error(f"Error decoding packet (len={len(byte_data)}): {e}", exc_info=True)
            self.logger.error(f"Failed packet hex: {hex_data}")
            return None

    def parse_advert(self, payload):
        """Parse advert payload - matches C++ AdvertDataHelpers.h implementation"""
        try:
            # Validate minimum payload size
            if len(payload) < 101:
                self.logger.error(f"ADVERT payload too short: {len(payload)} bytes")
                return {}
            
            # advert header
            pub_key = payload[0:32]
            timestamp = int.from_bytes(payload[32:32+4], "little")
            signature = payload[36:36+64]

            # appdata - parse according to C++ AdvertDataParser
            app_data = payload[100:]
            if len(app_data) == 0:
                self.logger.error("ADVERT has no app data")
                return {}
            
            flags_byte = app_data[0]
            
            # Log the full flag byte for debugging
            if hasattr(self, 'debug') and self.debug:
                self.logger.debug(f"ADVERT flags: 0x{flags_byte:02X} (binary: {flags_byte:08b})")
            
            # Create flags object with the full byte value
            flags = AdvertFlags(flags_byte)
            
            advert = {
                "public_key": pub_key.hex(),
                "advert_time": timestamp,
                "signature": signature.hex(),
            }

            # Extract type from lower 4 bits (matches C++ getType())
            adv_type = flags_byte & 0x0F
            if adv_type == AdvertFlags.ADV_TYPE_CHAT.value:
                advert.update({"mode": DeviceRole.Companion.name})
            elif adv_type == AdvertFlags.ADV_TYPE_REPEATER.value:
                advert.update({"mode": DeviceRole.Repeater.name})
            elif adv_type == AdvertFlags.ADV_TYPE_ROOM.value:
                advert.update({"mode": DeviceRole.RoomServer.name})
            elif adv_type == AdvertFlags.ADV_TYPE_SENSOR.value:
                advert.update({"mode": "Sensor"})
            else:
                advert.update({"mode": f"Type{adv_type}"})

            # Parse data according to C++ AdvertDataParser logic
            i = 1  # Start after flags byte
            
            # Parse location data if present (matches C++ hasLatLon())
            if AdvertFlags.ADV_LATLON_MASK in flags:
                if len(app_data) < i + 8:
                    self.logger.error(f"ADVERT with location flag too short: {len(app_data)} bytes")
                    return advert
                
                lat = int.from_bytes(app_data[i:i+4], 'little', signed=True)
                lon = int.from_bytes(app_data[i+4:i+8], 'little', signed=True)
                advert.update({"lat": round(lat / 1000000.0, 6), "lon": round(lon / 1000000.0, 6)})
                i += 8
            
            # Parse feat1 data if present
            if AdvertFlags.ADV_FEAT1_MASK in flags:
                if len(app_data) < i + 2:
                    self.logger.error(f"ADVERT with feat1 flag too short: {len(app_data)} bytes")
                    return advert
                feat1 = int.from_bytes(app_data[i:i+2], 'little')
                advert.update({"feat1": feat1})
                i += 2
            
            # Parse feat2 data if present
            if AdvertFlags.ADV_FEAT2_MASK in flags:
                if len(app_data) < i + 2:
                    self.logger.error(f"ADVERT with feat2 flag too short: {len(app_data)} bytes")
                    return advert
                feat2 = int.from_bytes(app_data[i:i+2], 'little')
                advert.update({"feat2": feat2})
                i += 2
            
            # Parse name data if present (matches C++ hasName())
            if AdvertFlags.ADV_NAME_MASK in flags:
                if len(app_data) >= i:
                    name_len = len(app_data) - i
                    if name_len > 0:
                        try:
                            # Decode name and handle potential null terminators
                            name = app_data[i:].decode('utf-8', errors='ignore').rstrip('\x00')
                            advert.update({"name": name})
                        except Exception as e:
                            self.logger.warning(f"Failed to decode ADVERT name: {e}")

            return advert
            
        except Exception as e:
            self.logger.error(f"Error parsing ADVERT payload: {e}", exc_info=True)
            return {}

    def _process_packet_path(self, path_bytes: bytes, payload: bytes, 
                             route_type: RouteType, payload_type: PayloadType) -> dict:
        """
        Process the path field based on packet and route type
        
        Args:
            path_bytes: Raw path bytes
            payload: Payload bytes (needed for TRACE packets)
            route_type: Route type from header
            payload_type: Payload type from header
            
        Returns:
            dict: Processed path information
        """
        try:
            # Convert path bytes to hex node IDs
            path_nodes = [f"{b:02x}" for b in path_bytes]
            
            # Special handling for TRACE packets
            if payload_type == PayloadType.TRACE:
                # In TRACE packets, path field contains SNR data
                # Real routing path is in the payload as pathHashes (after tag(4) + auth(4) + flags(1))
                snr_values = []
                for b in path_bytes:
                    # Convert SNR byte to dB (signed value)
                    snr_db = (b - 256) / 4 if b > 127 else b / 4
                    snr_values.append(snr_db)
                
                # Decode trace payload to extract pathHashes (routing path)
                path_hashes = []
                if len(payload) >= 9:  # Minimum: tag(4) + auth(4) + flags(1)
                    try:
                        # Skip tag(4) + auth(4) + flags(1) = 9 bytes
                        path_hashes_bytes = payload[9:]
                        # Each byte is a node hash (1-byte prefix)
                        path_hashes = [f"{b:02x}" for b in path_hashes_bytes]
                    except Exception as e:
                        self.logger.debug(f"Error extracting pathHashes from trace payload: {e}")
                
                return {
                    'type': 'trace',
                    'snr_data': snr_values,
                    'snr_path': path_nodes,  # SNR data as hex for reference
                    'path': path_hashes,  # Actual routing path from payload pathHashes
                    'path_hashes': path_hashes,  # Explicit field for pathHashes
                    'description': f"TRACE packet with {len(snr_values)} SNR readings and {len(path_hashes)} path nodes"
                }
            
            # Regular packets - determine path type based on route type
            is_direct = route_type in [RouteType.DIRECT, RouteType.TRANSPORT_DIRECT]
            
            if is_direct:
                # Direct routing: path contains routing instructions
                # Bytes are stripped at each hop
                return {
                    'type': 'routing_instructions',
                    'path': path_nodes,
                    'meaning': 'bytes_stripped_at_each_hop',
                    'description': f"Direct route via {','.join(path_nodes)} ({len(path_nodes)} hops)"
                }
            else:
                # Flood routing: path contains historical route
                # Bytes are added as packet floods through network
                return {
                    'type': 'historical_route', 
                    'path': path_nodes,
                    'meaning': 'bytes_added_as_packet_floods',
                    'description': f"Flooded through {','.join(path_nodes)} ({len(path_nodes)} hops)"
                }
                
        except Exception as e:
            self.logger.error(f"Error processing packet path: {e}")
            # Return basic path info as fallback
            path_nodes = [f"{b:02x}" for b in path_bytes]
            return {
                'type': 'unknown',
                'path': path_nodes,
                'description': f"Path: {','.join(path_nodes)}"
            }
    
    def _get_route_type_name(self, route_type):
        """Get human-readable name for route type"""
        route_types = {
            0x00: "ROUTE_TYPE_TRANSPORT_FLOOD",
            0x01: "ROUTE_TYPE_FLOOD", 
            0x02: "ROUTE_TYPE_DIRECT",
            0x03: "ROUTE_TYPE_TRANSPORT_DIRECT"
        }
        return route_types.get(route_type, f"UNKNOWN_ROUTE_{route_type:02x}")
    
    def get_payload_type_name(self, payload_type: int) -> str:
        """Get human-readable name for payload type"""
        payload_types = {
            0x00: "REQ",
            0x01: "RESPONSE", 
            0x02: "TXT_MSG",
            0x03: "ACK",
            0x04: "ADVERT",
            0x05: "GRP_TXT",
            0x06: "GRP_DATA",
            0x07: "ANON_REQ",
            0x08: "PATH",
            0x09: "TRACE",
            0x0A: "MULTIPART",
            # Additional payload types found in meshcore library (may not be in official spec)
            0x0B: "UNKNOWN_0b",  # Not defined in official spec
            0x0C: "UNKNOWN_0c",  # Not defined in official spec  
            0x0D: "UNKNOWN_0d",  # Not defined in official spec
            0x0E: "UNKNOWN_0e",  # Not defined in official spec
            0x0F: "RAW_CUSTOM"
        }
        return payload_types.get(payload_type, f"UNKNOWN_{payload_type:02x}")
    
    async def handle_channel_message(self, event, metadata=None):
        """Handle incoming channel message"""
        try:
            # Copy payload immediately to avoid segfault if event is freed
            import copy
            payload = copy.deepcopy(event.payload) if hasattr(event, 'payload') else None
            if payload is None:
                self.logger.warning("Channel message event has no payload")
                return
            
            channel_idx = payload.get('channel_idx', 0)
            
            # Debug: Log the full payload structure
            self.logger.debug(f"Channel message payload: {payload}")
            self.logger.debug(f"Payload keys: {list(payload.keys())}")
            
            # Get sender information from text field if it's in "SENDER: message" format
            text = payload.get('text', '')
            sender_id = "Channel User"  # Default fallback
            
            # Try to extract sender from text field (e.g., "HOWL: Test" -> "HOWL")
            message_content = text  # Default to full text
            if ':' in text and not text.startswith(':'):
                parts = text.split(':', 1)
                if len(parts) == 2 and parts[0].strip():
                    sender_id = parts[0].strip()
                    message_content = parts[1].strip()  # Use the part after the colon for keyword processing
                    self.logger.debug(f"Extracted sender from text: {sender_id}")
                    self.logger.debug(f"Message content for processing: {message_content}")
            
            # Always strip trailing whitespace/newlines from message content to handle cases like "Wx 98104\n"
            message_content = message_content.strip()
            
            # Get channel name from channel number
            channel_name = self.bot.channel_manager.get_channel_name(channel_idx)
            
            self.logger.info(f"Received channel message ({channel_name}) from {sender_id}: {text}")
            
            # Get SNR and RSSI using the same logic as contact messages
            snr = 'unknown'
            rssi = 'unknown'
            
            # Try to get SNR from payload first
            if 'SNR' in payload:
                snr = payload.get('SNR')
            elif 'snr' in payload:
                snr = payload.get('snr')
            # Try to get SNR from event metadata if available
            elif metadata:
                if 'snr' in metadata:
                    snr = metadata.get('snr')
                elif 'SNR' in metadata:
                    snr = metadata.get('SNR')
            
            # If still no SNR, try to get it from the cache using pubkey prefix from payload
            if snr == 'unknown':
                pubkey_prefix = payload.get('pubkey_prefix', '')
                if pubkey_prefix and pubkey_prefix in self.snr_cache:
                    snr = self.snr_cache[pubkey_prefix]
                    self.logger.debug(f"Retrieved cached SNR {snr} for pubkey {pubkey_prefix}")
            
            # Try to get RSSI from payload first
            if 'RSSI' in payload:
                rssi = payload.get('RSSI')
            elif 'rssi' in payload:
                rssi = payload.get('rssi')
            elif 'signal_strength' in payload:
                rssi = payload.get('signal_strength')
            # Try to get RSSI from event metadata if available
            elif metadata:
                if 'rssi' in metadata:
                    rssi = metadata.get('rssi')
                elif 'RSSI' in metadata:
                    rssi = metadata.get('RSSI')
            
            # If still no RSSI, try to get it from the cache using pubkey prefix from payload
            if rssi == 'unknown':
                pubkey_prefix = payload.get('pubkey_prefix', '')
                if pubkey_prefix and pubkey_prefix in self.rssi_cache:
                    rssi = self.rssi_cache[pubkey_prefix]
                    self.logger.debug(f"Retrieved cached RSSI {rssi} for pubkey {pubkey_prefix}")
            
            # For channel messages, we can decode the packet since they use shared channel keys
            # This gives us access to the actual routing information
            # Extract packet prefix from message raw_hex for correlation
            message_raw_hex = payload.get('raw_hex', '')
            message_packet_prefix = message_raw_hex[:32] if message_raw_hex else None
            message_pubkey = payload.get('pubkey_prefix', '')  # Keep for contact lookup
            self.logger.debug(f"Processing channel message from packet prefix: {message_packet_prefix}, pubkey: {message_pubkey}")
            
            # Enhanced RF data correlation with multiple strategies
            recent_rf_data = None
            
            # Strategy 1: Try immediate correlation using packet prefix
            if message_packet_prefix:
                recent_rf_data = self.find_recent_rf_data(message_packet_prefix)
            elif message_pubkey:
                # Fallback to pubkey correlation
                recent_rf_data = self.find_recent_rf_data(message_pubkey)
            
            # Strategy 2: If no immediate match and enhanced correlation is enabled, store message and wait briefly
            if not recent_rf_data and self.enhanced_correlation:
                import time
                correlation_key = message_packet_prefix or message_pubkey
                message_id = f"{correlation_key}_{int(time.time() * 1000)}"
                self.store_message_for_correlation(message_id, payload)
                
                # Wait a short time for RF data to arrive (non-blocking)
                await asyncio.sleep(0.1)  # 100ms wait
                recent_rf_data = self.correlate_message_with_rf_data(message_id)
            
            # Strategy 3: Try with extended timeout if still no match
            if not recent_rf_data:
                extended_timeout = self.rf_data_timeout * 2  # Double the normal timeout
                if message_packet_prefix:
                    recent_rf_data = self.find_recent_rf_data(message_packet_prefix, max_age_seconds=extended_timeout)
                elif message_pubkey:
                    recent_rf_data = self.find_recent_rf_data(message_pubkey, max_age_seconds=extended_timeout)
            
            # Strategy 4: Use most recent RF data as last resort
            if not recent_rf_data:
                extended_timeout = self.rf_data_timeout * 2  # Double the normal timeout
                recent_rf_data = self.find_recent_rf_data(max_age_seconds=extended_timeout)
            
            if recent_rf_data and recent_rf_data.get('raw_hex'):
                raw_hex = recent_rf_data['raw_hex']
                self.logger.info(f"🔍 FOUND RF DATA: {len(raw_hex)} chars, starts with: {raw_hex[:32]}...")
                self.logger.debug(f"Full RF data: {raw_hex}")
                
                # Extract SNR/RSSI from the RF data
                if recent_rf_data.get('snr'):
                    snr = recent_rf_data['snr']
                    self.logger.debug(f"Using SNR from RF data: {snr}")
                
                if recent_rf_data.get('rssi'):
                    rssi = recent_rf_data['rssi']
                    self.logger.debug(f"Using RSSI from RF data: {rssi}")
                
                # Try to extract path information from raw hex directly
                path_string = None
                hops = payload.get('path_len', 255)
                
                # First try the packet decoder
                # Use payload field if available, otherwise use raw_hex
                payload_hex = recent_rf_data.get('payload')
                packet_info = self.decode_meshcore_packet(raw_hex, payload_hex)
                
                # Get packet_hash from recent_rf_data if available (for trace correlation)
                packet_hash = recent_rf_data.get('packet_hash')
                if packet_hash and packet_info:
                    packet_info['packet_hash'] = packet_hash
                
                if packet_info and packet_info.get('path_len') is not None:
                    # Valid packet decoded - use the results even if path is empty (0 hops = direct)
                    hops = packet_info.get('path_len', 0)
                    
                    # Check if this is a TRACE packet with SNR data
                    if packet_info.get('payload_type') == 9:  # TRACE packet
                        # For TRACE packets, extract routing path from payload pathHashes
                        # The path field contains SNR data, but the actual routing path is in payload
                        path_info = packet_info.get('path_info', {})
                        path_hashes = path_info.get('path_hashes') or path_info.get('path', [])
                        
                        if path_hashes:
                            # Convert pathHashes to path string
                            path_string = ','.join(path_hashes)
                            self.logger.info(f"🎯 EXTRACTED PATH FROM TRACE PACKET: {path_string} ({len(path_hashes)} hops)")
                            
                            # Update mesh graph with trace path - bot is the destination, so we can confirm these edges
                            # Since the bot received this trace packet, it's the destination node
                            self._update_mesh_graph_from_trace(path_hashes, packet_info)
                        else:
                            path_string = "Direct" if hops == 0 else f"Unknown routing ({hops} hops)"
                            self.logger.info(f"🎯 EXTRACTED PATH FROM TRACE PACKET: {path_string}")
                    else:
                        # For all other packet types, try multiple methods to get the path
                        path_string = None
                        
                        # Method 1: Try path_nodes field first
                        path_nodes = packet_info.get('path_nodes', [])
                        if path_nodes:
                            path_string = ','.join(path_nodes)
                            self.logger.info(f"🎯 EXTRACTED PATH FROM PACKET: {path_string} ({hops} hops)")
                            # Update mesh graph with path edges
                            self._update_mesh_graph(path_nodes, packet_info)
                        else:
                            # Method 2: Try path_hex field
                            path_hex = packet_info.get('path_hex', '')
                            if path_hex and len(path_hex) >= 2:
                                # Convert hex string to node list (every 2 characters = 1 node)
                                path_nodes = [path_hex[i:i+2] for i in range(0, len(path_hex), 2)]
                                path_string = ','.join(path_nodes)
                                self.logger.info(f"🎯 EXTRACTED PATH FROM PACKET HEX: {path_string} ({hops} hops)")
                                # Update mesh graph with path edges
                                self._update_mesh_graph(path_nodes, packet_info)
                            else:
                                # Method 3: Try path_info.path field
                                path_info = packet_info.get('path_info', {})
                                if path_info and path_info.get('path'):
                                    path_nodes = path_info['path']
                                    path_string = ','.join(path_nodes)
                                    self.logger.info(f"🎯 EXTRACTED PATH FROM PATH_INFO: {path_string} ({hops} hops)")
                                    # Update mesh graph with path edges
                                    self._update_mesh_graph(path_nodes, packet_info)
                                else:
                                    # No path found - this is truly unknown
                                    path_string = "Direct" if hops == 0 else "Unknown routing"
                                    self.logger.info(f"🎯 EXTRACTED PATH FROM PACKET: {path_string} ({hops} hops)")
                else:
                    # Packet decoding failed - try to extract path directly from raw hex
                    self.logger.debug("Packet decoding failed, trying direct hex parsing")
                    path_string = self.extract_path_from_raw_hex(raw_hex, hops)
                    if path_string:
                        self.logger.info(f"🎯 EXTRACTED PATH FROM RAW HEX: {path_string} ({hops} hops)")
                    else:
                        # Try to use routing info from RF data as fallback
                        if recent_rf_data.get('routing_info') and recent_rf_data['routing_info'].get('path_nodes'):
                            routing_info = recent_rf_data['routing_info']
                            hops = len(routing_info['path_nodes'])
                            path_string = ','.join(routing_info['path_nodes'])
                            self.logger.info(f"🎯 EXTRACTED PATH FROM RF ROUTING INFO: {path_string} ({hops} hops)")
                        else:
                            # Final fallback to basic path info
                            self.logger.debug("No path info available, using basic path info")
                            path_string = None
            else:
                self.logger.warning("❌ NO RF DATA found for channel message after all correlation attempts")
                hops = payload.get('path_len', 255)
                path_string = None
            
            # Get the full public key from contacts if available
            sender_pubkey = payload.get('pubkey_prefix', '')
            if hasattr(self.bot.meshcore, 'contacts') and self.bot.meshcore.contacts:
                for contact_key, contact_data in self.bot.meshcore.contacts.items():
                    if contact_data.get('public_key', '').startswith(sender_pubkey):
                        # Use the full public key from the contact
                        sender_pubkey = contact_data.get('public_key', sender_pubkey)
                        self.logger.debug(f"Found full public key for {sender_id}: {sender_pubkey[:16]}...")
                        break
            
            # Elapsed: "Nms" when device clock is valid, or "Sync Device Clock" when invalid.
            _translator = getattr(self.bot, 'translator', None)
            _elapsed = format_elapsed_display(payload.get('sender_timestamp'), _translator)

            # Convert to our message format
            message = MeshMessage(
                content=message_content,  # Use the extracted message content
                sender_id=sender_id,
                sender_pubkey=sender_pubkey,
                channel=channel_name,
                timestamp=payload.get('sender_timestamp', 0),
                snr=snr,
                rssi=rssi,
                hops=hops,
                path=path_string,  # Use the path information extracted from RF data
                elapsed=_elapsed,
                is_dm=False
            )
            
            # Path information is now set directly in the MeshMessage constructor from RF data
            # No need for additional path extraction since we're using the actual routing data
            
            # Path information is now set directly in the MeshMessage constructor
            # No need for additional path processing since we're using the actual routing data
            self.logger.debug(f"Message routing info: hops={message.hops}, routing={message.path}")
            
            # Always decode and log packet information for debugging (regardless of keywords)
            await self._debug_decode_message_path(message, sender_id, recent_rf_data)
            
            # Always attempt packet decoding and log the results for debugging
            await self._debug_decode_packet_for_message(message, sender_id, recent_rf_data)
            
            # Check if this is an old cached message from before bot connection
            timestamp = payload.get('sender_timestamp', 0)
            if self._is_old_cached_message(timestamp):
                self.logger.debug(f"Skipping old cached channel message from {sender_id} (timestamp: {timestamp}, connection: {self.bot.connection_time})")
                return  # Read the message to clear cache, but don't process it
            
            # Process the message
            await self.process_message(message)
            
        except Exception as e:
            self.logger.error(f"Error handling channel message: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
    
    def _update_mesh_graph(self, path_nodes: List[str], packet_info: Dict[str, Any]):
        """Update mesh graph with edges from a message path.
        
        Args:
            path_nodes: List of node prefixes in path order.
            packet_info: Packet information dictionary with routing data.
        """
        if not path_nodes or len(path_nodes) < 2:
            self.logger.debug(f"Mesh graph: Skipping path with < 2 nodes: {path_nodes}")
            return  # Need at least 2 nodes to form an edge
        
        if not hasattr(self.bot, 'mesh_graph') or not self.bot.mesh_graph:
            self.logger.debug("Mesh graph: Graph not initialized, skipping update")
            return  # Graph not initialized
        
        mesh_graph = self.bot.mesh_graph
        self.logger.debug(f"Mesh graph: Updating graph with path: {path_nodes} ({len(path_nodes)} nodes)")
        
        # Get recency window from config (default 7 days)
        recency_days = self.bot.config.getint('Path_Command', 'graph_edge_expiration_days', fallback=7)
        
        # Get public keys if available from database
        # Note: We don't check device contacts because repeaters aren't stored on the device
        # IMPORTANT: Only use database lookup if prefix is unique (to avoid wrong public key assignment)
        # In busy meshes, prefixes are rarely unique, so we must verify uniqueness first
        # Also filter by recency to avoid using old/stale repeaters
        node_keys = {}
        
        for node_prefix in path_nodes:
            try:
                # First check if prefix is unique in database (within recency window)
                count_query = f'''
                    SELECT COUNT(DISTINCT public_key) as count
                    FROM complete_contact_tracking 
                    WHERE public_key LIKE ?
                    AND role IN ('repeater', 'roomserver')
                    AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                '''
                prefix_pattern = f"{node_prefix}%"
                count_results = self.bot.db_manager.execute_query(count_query, (prefix_pattern,))
                
                if count_results and count_results[0].get('count', 0) == 1:
                    # Prefix is unique within recency window - safe to use database lookup
                    query = f'''
                        SELECT public_key 
                        FROM complete_contact_tracking 
                        WHERE public_key LIKE ?
                        AND role IN ('repeater', 'roomserver')
                        AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                        ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                        LIMIT 1
                    '''
                    results = self.bot.db_manager.execute_query(query, (prefix_pattern,))
                    if results and results[0].get('public_key'):
                        node_keys[node_prefix] = results[0]['public_key']
                        self.logger.debug(f"Mesh graph: Found unique public key for prefix {node_prefix} from database: {results[0]['public_key'][:16]}...")
                else:
                    # Prefix collision or no recent matches - don't use database lookup (would risk wrong public key)
                    count = count_results[0].get('count', 0) if count_results else 0
                    self.logger.debug(f"Mesh graph: Prefix {node_prefix} has {count} recent matches in database, skipping public key lookup (not unique or stale)")
            except Exception as e:
                self.logger.debug(f"Error looking up public key for prefix {node_prefix}: {e}")
        
        # Calculate geographic distances if locations are available
        from .utils import calculate_distance, _get_node_location_from_db
        
        # Extract edges from path
        for i in range(len(path_nodes) - 1):
            from_prefix = path_nodes[i]
            to_prefix = path_nodes[i + 1]
            hop_position = i + 1  # Position in path (1-indexed)
            
            # Get public keys if available
            from_key = node_keys.get(from_prefix)
            to_key = node_keys.get(to_prefix)
            
            # Calculate geographic distance if both nodes have locations
            # IMPORTANT: Only use public keys that we're 100% certain of (from uniqueness check above)
            # For location lookups, we can use distance-based selection to get better distance calculations,
            # but we do NOT store those selected public keys - we only store keys we're certain of
            geographic_distance = None
            try:
                from_location = None
                to_location = None
                
                # Try to get location using full public key first (more accurate)
                if from_key:
                    from_location = self._get_location_by_public_key(from_key)
                if not from_location:
                    # For LoRa: prefer shorter edges - use to_location as reference if we have it
                    # This helps resolve prefix collisions by preferring closer repeaters for distance calculation
                    to_location_temp = None
                    if to_key:
                        to_location_temp = self._get_location_by_public_key(to_key)
                    if not to_location_temp:
                        # Try to get to_location first to use as reference
                        # Use bot location as fallback reference to ensure distance-based selection
                        bot_location_ref = self._get_bot_location_fallback()
                        to_location_result = _get_node_location_from_db(self.bot, to_prefix, bot_location_ref, recency_days)
                        if to_location_result:
                            to_location_temp, temp_key = to_location_result
                            if not to_key and temp_key:
                                to_key = temp_key  # Store the selected public key for distance-based selection
                    
                    # Get from_location using to_location as reference (prefers shorter distance for LoRa)
                    # If to_location not available, use bot location as fallback reference
                    reference_for_from = to_location_temp if to_location_temp else self._get_bot_location_fallback()
                    # Capture the selected public key when distance-based selection is used
                    # Apply recency window to avoid using stale repeaters
                    from_location_result = _get_node_location_from_db(self.bot, from_prefix, reference_for_from, recency_days)
                    if from_location_result:
                        from_location, selected_from_key = from_location_result
                        if not from_key and selected_from_key:
                            from_key = selected_from_key  # Store the selected public key
                
                if to_key:
                    to_location = self._get_location_by_public_key(to_key)
                if not to_location:
                    # Use from_location as reference to prefer shorter distance for LoRa
                    # If from_location not available, use bot location as fallback reference
                    reference_for_to = from_location if from_location else self._get_bot_location_fallback()
                    # Capture the selected public key when distance-based selection is used
                    # Apply recency window to avoid using stale repeaters
                    to_location_result = _get_node_location_from_db(self.bot, to_prefix, reference_for_to, recency_days)
                    if to_location_result:
                        to_location, selected_to_key = to_location_result
                        if not to_key and selected_to_key:
                            to_key = selected_to_key  # Store the selected public key
                
                if from_location and to_location:
                    geographic_distance = calculate_distance(
                        from_location[0], from_location[1],
                        to_location[0], to_location[1]
                    )
            except Exception as e:
                self.logger.debug(f"Could not calculate distance for edge {from_prefix}->{to_prefix}: {e}")
            
            # Add edge to graph - only use public keys we're 100% certain of (from uniqueness check)
            # Do NOT use public keys from distance-based selection - we can't be certain they're correct
            self.logger.debug(f"Mesh graph: Adding edge {from_prefix} -> {to_prefix} (hop {hop_position})")
            mesh_graph.add_edge(
                from_prefix=from_prefix,
                to_prefix=to_prefix,
                from_public_key=from_key,  # Only if prefix was unique (certain)
                to_public_key=to_key,      # Only if prefix was unique (certain)
                hop_position=hop_position,
                geographic_distance=geographic_distance
            )
    
    def _store_observed_path(self, advert_data: Dict[str, Any], path_hex: str, path_length: int, packet_type: str, packet_hash: Optional[str] = None):
        """Store a complete path in the observed_paths table.
        
        Args:
            advert_data: Advertisement data dictionary with public_key (for adverts).
            path_hex: Hex string of the complete path.
            path_length: Length of the path in bytes.
            packet_type: Type of packet ('advert', 'message', etc.).
            packet_hash: Optional packet hash to group paths from the same packet.
        """
        if not path_hex or path_length < 2:
            return  # Need at least 2 bytes (1 node) to form a path
        
        try:
            # Parse path to extract from_prefix and to_prefix
            path_nodes = []
            for i in range(0, len(path_hex), 2):
                if i + 1 < len(path_hex):
                    path_nodes.append(path_hex[i:i+2].lower())
            
            if len(path_nodes) < 1:
                return  # No valid path nodes
            
            from_prefix = path_nodes[0]
            to_prefix = path_nodes[-1]  # Last hop in path (last repeater that forwarded to bot)
            
            # Get public_key for adverts (NULL for messages)
            public_key = advert_data.get('public_key', '') if packet_type == 'advert' else None
            
            # Check if path already exists
            if public_key:
                # For adverts: check by public_key, path_hex, packet_type
                query = '''
                    SELECT id, observation_count, last_seen
                    FROM observed_paths
                    WHERE public_key = ? AND path_hex = ? AND packet_type = ?
                '''
                existing = self.bot.db_manager.execute_query(query, (public_key, path_hex, packet_type))
            else:
                # For messages: check by from_prefix, to_prefix, path_hex, packet_type
                query = '''
                    SELECT id, observation_count, last_seen
                    FROM observed_paths
                    WHERE from_prefix = ? AND to_prefix = ? AND path_hex = ? AND packet_type = ?
                    AND public_key IS NULL
                '''
                existing = self.bot.db_manager.execute_query(query, (from_prefix, to_prefix, path_hex, packet_type))
            
            from datetime import datetime
            now = datetime.now()
            
            if existing and len(existing) > 0:
                # Path exists - update observation count and last_seen
                path_id = existing[0]['id']
                current_count = existing[0].get('observation_count', 1)
                update_query = '''
                    UPDATE observed_paths
                    SET observation_count = ?, last_seen = ?
                    WHERE id = ?
                '''
                self.bot.db_manager.execute_update(update_query, (current_count + 1, now.isoformat(), path_id))
                self.logger.debug(f"Updated observed_paths entry for {packet_type} path {path_hex[:20]}... (count: {current_count + 1})")
            else:
                # New path - insert
                insert_query = '''
                    INSERT INTO observed_paths
                    (public_key, packet_hash, from_prefix, to_prefix, path_hex, path_length, packet_type, first_seen, last_seen, observation_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                '''
                # Only store packet_hash if it's valid (not None and not the default invalid hash)
                stored_packet_hash = packet_hash if (packet_hash and packet_hash != "0000000000000000") else None
                self.bot.db_manager.execute_update(insert_query, (
                    public_key,
                    stored_packet_hash,
                    from_prefix,
                    to_prefix,
                    path_hex,
                    path_length,
                    packet_type,
                    now.isoformat(),
                    now.isoformat()
                ))
                self.logger.debug(f"Stored new {packet_type} path in observed_paths: {from_prefix}->{to_prefix} ({path_length} bytes)")
        
        except Exception as e:
            self.logger.warning(f"Error storing observed path: {e}")
            import traceback
            self.logger.debug(traceback.format_exc())
    
    def _get_bot_location_fallback(self) -> Optional[Tuple[float, float]]:
        """Get bot location from config to use as fallback reference for distance-based selection.
        
        Returns:
            Optional[Tuple[float, float]]: (latitude, longitude) or None if not configured.
        """
        try:
            lat = self.bot.config.getfloat('Bot', 'bot_latitude', fallback=None)
            lon = self.bot.config.getfloat('Bot', 'bot_longitude', fallback=None)
            
            if lat is not None and lon is not None:
                # Validate coordinates
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return (lat, lon)
            return None
        except Exception as e:
            self.logger.debug(f"Error getting bot location fallback: {e}")
            return None
    
    def _get_location_by_public_key(self, public_key: str) -> Optional[Tuple[float, float]]:
        """Get location for a full public key (more accurate than prefix lookup).
        
        Prefers starred repeaters if there are somehow multiple entries (shouldn't happen with full key).
        
        Args:
            public_key: Full public key string.
            
        Returns:
            Optional[Tuple[float, float]]: (latitude, longitude) or None.
        """
        try:
            query = '''
                SELECT latitude, longitude 
                FROM complete_contact_tracking 
                WHERE public_key = ?
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
                AND role IN ('repeater', 'roomserver')
                ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 1
            '''
            results = self.bot.db_manager.execute_query(query, (public_key,))
            if results:
                row = results[0]
                lat = row.get('latitude')
                lon = row.get('longitude')
                if lat is not None and lon is not None:
                    return (float(lat), float(lon))
        except Exception as e:
            self.logger.debug(f"Error getting location by public key {public_key[:16]}...: {e}")
        return None
    
    def _update_mesh_graph_from_advert(self, advert_data: Dict[str, Any], out_path: str, 
                                      out_path_len: int, packet_info: Dict[str, Any]):
        """Update mesh graph with edges from an advertisement's out_path.
        
        Creates an edge from the advertising device to the first hop in their out_path,
        and edges between subsequent hops in the path.
        
        Args:
            advert_data: Advertisement data dictionary with public_key.
            out_path: Hex string of the path the advert took to reach us.
            out_path_len: Length of the path in bytes.
            packet_info: Packet information dictionary with routing data.
        """
        if not out_path or out_path_len < 2:
            return  # Need at least 2 bytes (1 node) to form an edge
        
        if not hasattr(self.bot, 'mesh_graph') or not self.bot.mesh_graph:
            return  # Graph not initialized
        
        mesh_graph = self.bot.mesh_graph
        
        # Get advertiser's public key
        advertiser_key = advert_data.get('public_key', '')
        if not advertiser_key:
            self.logger.debug("Mesh graph: No public key in advert data, skipping graph update")
            return
        
        advertiser_prefix = advertiser_key[:2].lower()
        
        # Parse path from hex string
        path_nodes = []
        for i in range(0, len(out_path), 2):
            if i + 1 < len(out_path):
                path_nodes.append(out_path[i:i+2].lower())
        
        if len(path_nodes) == 0:
            return  # No valid path nodes
        
        self.logger.debug(f"Mesh graph: Updating graph from advert path: {advertiser_prefix} -> {path_nodes}")
        
        # Get recency window from config (default 7 days)
        recency_days = self.bot.config.getint('Path_Command', 'graph_edge_expiration_days', fallback=7)
        
        # Calculate geographic distances if locations are available
        from .utils import calculate_distance, _get_node_location_from_db
        
        # Create edge from advertiser to first hop in path
        first_hop = path_nodes[0]
        geographic_distance = None
        first_hop_key = None
        
        # IMPORTANT: Only use public keys we're 100% certain of
        # For the first hop, we can only be certain if the prefix is unique (and recent)
        try:
            # Check if first_hop prefix is unique within recency window (only then can we be certain of the public key)
            count_query = f'''
                SELECT COUNT(DISTINCT public_key) as count
                FROM complete_contact_tracking 
                WHERE public_key LIKE ?
                AND role IN ('repeater', 'roomserver')
                AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
            '''
            prefix_pattern = f"{first_hop}%"
            count_results = self.bot.db_manager.execute_query(count_query, (prefix_pattern,))
            
            if count_results and count_results[0].get('count', 0) == 1:
                # Prefix is unique within recency window - safe to use database lookup
                query = f'''
                    SELECT public_key 
                    FROM complete_contact_tracking 
                    WHERE public_key LIKE ?
                    AND role IN ('repeater', 'roomserver')
                    AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                    ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                    LIMIT 1
                '''
                results = self.bot.db_manager.execute_query(query, (prefix_pattern,))
                if results and results[0].get('public_key'):
                    first_hop_key = results[0]['public_key']
                    self.logger.debug(f"Mesh graph: Found unique public key for first hop {first_hop}: {first_hop_key[:16]}...")
            else:
                count = count_results[0].get('count', 0) if count_results else 0
                self.logger.debug(f"Mesh graph: First hop prefix {first_hop} has {count} recent matches, cannot be certain of public key")
        except Exception as e:
            self.logger.debug(f"Error checking uniqueness for first hop {first_hop}: {e}")
        
        try:
            # Use full public key for advertiser (we're 100% certain - it's from the event)
            advertiser_location = None
            if advertiser_key:
                advertiser_location = self._get_location_by_public_key(advertiser_key)
            if not advertiser_location:
                # Get first_hop location first to use as reference for LoRa distance preference
                # Use bot location as fallback reference to ensure distance-based selection
                bot_location_ref = self._get_bot_location_fallback()
                first_hop_temp_result = _get_node_location_from_db(self.bot, first_hop, bot_location_ref, recency_days)
                if first_hop_temp_result:
                    first_hop_location_temp, _ = first_hop_temp_result
                else:
                    first_hop_location_temp = bot_location_ref  # Use bot location as fallback
                
                if first_hop_location_temp:
                    advertiser_result = _get_node_location_from_db(self.bot, advertiser_prefix, first_hop_location_temp, recency_days)
                    if advertiser_result:
                        advertiser_location, _ = advertiser_result
            
            # Get first_hop location using advertiser location as reference for LoRa preference
            # Capture the selected public key when distance-based selection is used
            # Apply recency window to avoid using stale repeaters
            first_hop_result = _get_node_location_from_db(self.bot, first_hop, advertiser_location, recency_days)
            if first_hop_result:
                first_hop_location, selected_first_hop_key = first_hop_result
                if not first_hop_key and selected_first_hop_key:
                    first_hop_key = selected_first_hop_key  # Store the selected public key
            
            if advertiser_location and first_hop_location:
                geographic_distance = calculate_distance(
                    advertiser_location[0], advertiser_location[1],
                    first_hop_location[0], first_hop_location[1]
                )
        except Exception as e:
            self.logger.debug(f"Could not calculate distance for advert edge {advertiser_prefix}->{first_hop}: {e}")
        
        # Add edge from advertiser to first hop
        # from_public_key: advertiser_key (100% certain - from event)
        # to_public_key: first_hop_key (only if prefix was unique - certain)
        mesh_graph.add_edge(
            from_prefix=advertiser_prefix,
            to_prefix=first_hop,
            from_public_key=advertiser_key,  # 100% certain - from NEW_CONTACT event
            to_public_key=first_hop_key,     # Only if prefix was unique (certain)
            hop_position=1,  # First hop in path
            geographic_distance=geographic_distance
        )
        
        # Create edges between subsequent hops in the path
        # Track previous location to use as reference for better distance-based selection
        # Start with first_hop_location (if available) or advertiser_location as reference
        previous_location = None
        try:
            if 'first_hop_location' in locals() and first_hop_location:
                previous_location = first_hop_location
            elif advertiser_location:
                previous_location = advertiser_location
        except:
            pass
        
        for i in range(len(path_nodes) - 1):
            from_node = path_nodes[i]
            to_node = path_nodes[i + 1]
            hop_position = i + 2  # Position in path (1-indexed, advertiser is 0)
            
            # IMPORTANT: Only use public keys we're 100% certain of (when prefix is unique and recent)
            from_node_key = None
            to_node_key = None
            
            # Check if from_node prefix is unique within recency window
            try:
                count_query = f'''
                    SELECT COUNT(DISTINCT public_key) as count
                    FROM complete_contact_tracking 
                    WHERE public_key LIKE ?
                    AND role IN ('repeater', 'roomserver')
                    AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                '''
                prefix_pattern = f"{from_node}%"
                count_results = self.bot.db_manager.execute_query(count_query, (prefix_pattern,))
                
                if count_results and count_results[0].get('count', 0) == 1:
                    query = f'''
                        SELECT public_key 
                        FROM complete_contact_tracking 
                        WHERE public_key LIKE ?
                        AND role IN ('repeater', 'roomserver')
                        AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                        ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                        LIMIT 1
                    '''
                    results = self.bot.db_manager.execute_query(query, (prefix_pattern,))
                    if results and results[0].get('public_key'):
                        from_node_key = results[0]['public_key']
                        self.logger.debug(f"Mesh graph: Found unique public key for {from_node}: {from_node_key[:16]}...")
            except Exception as e:
                self.logger.debug(f"Error checking uniqueness for {from_node}: {e}")
            
            # Check if to_node prefix is unique within recency window
            try:
                count_query = f'''
                    SELECT COUNT(DISTINCT public_key) as count
                    FROM complete_contact_tracking 
                    WHERE public_key LIKE ?
                    AND role IN ('repeater', 'roomserver')
                    AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                '''
                prefix_pattern = f"{to_node}%"
                count_results = self.bot.db_manager.execute_query(count_query, (prefix_pattern,))
                
                if count_results and count_results[0].get('count', 0) == 1:
                    query = f'''
                        SELECT public_key 
                        FROM complete_contact_tracking 
                        WHERE public_key LIKE ?
                        AND role IN ('repeater', 'roomserver')
                        AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                        ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                        LIMIT 1
                    '''
                    results = self.bot.db_manager.execute_query(query, (prefix_pattern,))
                    if results and results[0].get('public_key'):
                        to_node_key = results[0]['public_key']
                        self.logger.debug(f"Mesh graph: Found unique public key for {to_node}: {to_node_key[:16]}...")
            except Exception as e:
                self.logger.debug(f"Error checking uniqueness for {to_node}: {e}")
            
            # Calculate geographic distance if available
            # For LoRa, prefer shorter distances when resolving prefix collisions for location lookup
            # IMPORTANT: Use previous_location as reference to ensure we select the closer repeater
            # NOTE: We do NOT store public keys from distance-based selection - only use them for location
            # Apply recency window to avoid using stale repeaters
            geographic_distance = None
            try:
                from .utils import _get_node_location_from_db, calculate_distance
                
                # Get from_location using previous_location as reference (ensures we select closer repeater)
                # Use bot location as fallback if previous_location not available
                reference_for_from = previous_location if previous_location else self._get_bot_location_fallback()
                from_result = _get_node_location_from_db(self.bot, from_node, reference_for_from, recency_days)
                if from_result:
                    from_location, selected_from_key = from_result
                    if not from_node_key and selected_from_key:
                        from_node_key = selected_from_key  # Store the selected public key
                else:
                    from_location = None
                
                # Get to_location using from_location as reference
                # Use bot location as fallback if from_location not available
                reference_for_to = from_location if from_location else self._get_bot_location_fallback()
                to_result = _get_node_location_from_db(self.bot, to_node, reference_for_to, recency_days)
                if to_result:
                    to_location, selected_to_key = to_result
                    if not to_node_key and selected_to_key:
                        to_node_key = selected_to_key  # Store the selected public key
                else:
                    to_location = None
                
                # Re-get from_location with to_location as reference (for better collision resolution)
                if to_location:
                    from_result2 = _get_node_location_from_db(self.bot, from_node, to_location, recency_days)
                    if from_result2:
                        from_location, selected_from_key2 = from_result2
                        if not from_node_key and selected_from_key2:
                            from_node_key = selected_from_key2
                
                # Re-get to_location with from_location as reference
                if from_location:
                    to_result2 = _get_node_location_from_db(self.bot, to_node, from_location, recency_days)
                    if to_result2:
                        to_location, selected_to_key2 = to_result2
                        if not to_node_key and selected_to_key2:
                            to_node_key = selected_to_key2
                
                # Update previous_location for next iteration
                previous_location = to_location if to_location else from_location
                
                if from_location and to_location:
                    geographic_distance = calculate_distance(
                        from_location[0], from_location[1],
                        to_location[0], to_location[1]
                    )
            except Exception as e:
                self.logger.debug(f"Could not calculate distance for edge {from_node}->{to_node}: {e}")
            
            # Add edge between path nodes - only use public keys we're 100% certain of (from uniqueness check)
            mesh_graph.add_edge(
                from_prefix=from_node,
                to_prefix=to_node,
                from_public_key=from_node_key,  # Only if prefix was unique (certain)
                to_public_key=to_node_key,      # Only if prefix was unique (certain)
                hop_position=hop_position,
                geographic_distance=geographic_distance
            )
    
    def _update_mesh_graph_from_trace(self, path_hashes: List[str], packet_info: Dict[str, Any]):
        """Update mesh graph with edges from a trace packet's pathHashes.
        
        When the bot receives a trace packet, it's the destination, so we can confirm
        the edges in the path. The pathHashes represent the routing path the packet took.
        
        Special case: If this is a trace we sent that came back through an immediate neighbor,
        we can trust both directions (Bot -> Neighbor and Neighbor -> Bot).
        
        Args:
            path_hashes: List of node hash prefixes (1-byte each, as 2-char hex strings) from trace payload.
            packet_info: Packet information dictionary with routing data.
        """
        if not path_hashes or len(path_hashes) == 0:
            self.logger.debug("Mesh graph: Trace packet has no pathHashes, skipping graph update")
            return
        
        if not hasattr(self.bot, 'mesh_graph') or not self.bot.mesh_graph:
            self.logger.debug("Mesh graph: Graph not initialized, skipping trace update")
            return
        
        if not hasattr(self.bot, 'transmission_tracker') or not self.bot.transmission_tracker:
            self.logger.debug("Mesh graph: Cannot get bot prefix, skipping trace update")
            return
        
        mesh_graph = self.bot.mesh_graph
        bot_prefix = self.bot.transmission_tracker.bot_prefix
        
        if not bot_prefix:
            self.logger.debug("Mesh graph: Bot prefix not available, skipping trace update")
            return
        
        bot_prefix = bot_prefix.lower()
        
        # Check if this is a trace we sent (by matching packet hash)
        is_our_trace = False
        packet_hash = packet_info.get('packet_hash')
        if packet_hash and hasattr(self.bot, 'transmission_tracker'):
            import time
            record = self.bot.transmission_tracker.match_packet_hash(packet_hash, time.time())
            if record:
                is_our_trace = True
                self.logger.debug(f"Mesh graph: Trace packet is one we sent (matched transmission record)")
        
        # Check if this came back through an immediate neighbor
        # For a trace we sent that came back, if pathHashes has exactly one node, that's our immediate neighbor
        is_immediate_neighbor = False
        if is_our_trace and len(path_hashes) == 1:
            is_immediate_neighbor = True
            self.logger.info(f"Mesh graph: Trace came back through immediate neighbor {path_hashes[0]} - trusting both directions")
        
        self.logger.debug(f"Mesh graph: Updating graph from trace pathHashes: {path_hashes} (bot is destination: {bot_prefix}, is_our_trace: {is_our_trace}, immediate_neighbor: {is_immediate_neighbor})")
        
        # Get recency window from config (default 7 days)
        recency_days = self.bot.config.getint('Path_Command', 'graph_edge_expiration_days', fallback=7)
        
        # Get bot's location for distance calculations
        from .utils import calculate_distance, _get_node_location_from_db
        bot_location = None
        try:
            bot_location_result = _get_node_location_from_db(self.bot, bot_prefix, None, recency_days)
            if bot_location_result:
                bot_location, _ = bot_location_result
        except Exception as e:
            self.logger.debug(f"Could not get bot location: {e}")
        
        # Create edges from pathHashes to bot
        # Since bot is the destination, the last node in pathHashes sent directly to bot
        if len(path_hashes) > 0:
            last_node = path_hashes[-1].lower()
            
            # If this is our trace that came back through an immediate neighbor, create bidirectional edge
            if is_immediate_neighbor:
                # We can trust both directions:
                # 1. Bot -> Neighbor (we sent it, so we know this edge)
                # 2. Neighbor -> Bot (we received it back, so we know this edge)
                neighbor_prefix = path_hashes[0].lower()
                
                # Get neighbor's public key if unique
                neighbor_key = None
                try:
                    count_query = f'''
                        SELECT COUNT(DISTINCT public_key) as count
                        FROM complete_contact_tracking 
                        WHERE public_key LIKE ?
                        AND role IN ('repeater', 'roomserver')
                        AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                    '''
                    prefix_pattern = f"{neighbor_prefix}%"
                    count_results = self.bot.db_manager.execute_query(count_query, (prefix_pattern,))
                    
                    if count_results and count_results[0].get('count', 0) == 1:
                        query = f'''
                            SELECT public_key 
                            FROM complete_contact_tracking 
                            WHERE public_key LIKE ?
                            AND role IN ('repeater', 'roomserver')
                            AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                            ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                            LIMIT 1
                        '''
                        results = self.bot.db_manager.execute_query(query, (prefix_pattern,))
                        if results and results[0].get('public_key'):
                            neighbor_key = results[0]['public_key']
                except Exception as e:
                    self.logger.debug(f"Error checking uniqueness for immediate neighbor {neighbor_prefix}: {e}")
                
                # Get bot's public key (100% certain - it's us)
                bot_key = None
                if hasattr(self.bot.meshcore, 'device') and self.bot.meshcore.device:
                    try:
                        device_info = self.bot.meshcore.device
                        if hasattr(device_info, 'public_key'):
                            pubkey = device_info.public_key
                            if isinstance(pubkey, str):
                                bot_key = pubkey
                            elif isinstance(pubkey, bytes):
                                bot_key = pubkey.hex()
                    except Exception as e:
                        self.logger.debug(f"Could not get bot public key: {e}")
                
                # Calculate distance
                geographic_distance = None
                try:
                    if bot_location:
                        neighbor_result = _get_node_location_from_db(self.bot, neighbor_prefix, bot_location, recency_days)
                        if neighbor_result:
                            neighbor_location, selected_neighbor_key = neighbor_result
                            if not neighbor_key and selected_neighbor_key:
                                neighbor_key = selected_neighbor_key
                            
                            if neighbor_location and bot_location:
                                geographic_distance = calculate_distance(
                                    neighbor_location[0], neighbor_location[1],
                                    bot_location[0], bot_location[1]
                                )
                except Exception as e:
                    self.logger.debug(f"Could not calculate distance for immediate neighbor edge: {e}")
                
                # Create bidirectional edge: Bot <-> Neighbor (both directions trusted)
                # Direction 1: Bot -> Neighbor (we sent it)
                mesh_graph.add_edge(
                    from_prefix=bot_prefix,
                    to_prefix=neighbor_prefix,
                    from_public_key=bot_key,      # 100% certain - it's the bot
                    to_public_key=neighbor_key,  # Only if prefix was unique (certain)
                    hop_position=1,  # First hop
                    geographic_distance=geographic_distance
                )
                
                # Direction 2: Neighbor -> Bot (we received it back)
                mesh_graph.add_edge(
                    from_prefix=neighbor_prefix,
                    to_prefix=bot_prefix,
                    from_public_key=neighbor_key,  # Only if prefix was unique (certain)
                    to_public_key=bot_key,          # 100% certain - it's the bot
                    hop_position=1,  # First hop (return path)
                    geographic_distance=geographic_distance
                )
                
                self.logger.info(f"Mesh graph: Created trusted bidirectional edge with immediate neighbor {neighbor_prefix}")
                return  # Done - no need to process further for immediate neighbor case
            
            # Regular case: trace from elsewhere, bot is destination
            # Create edge: last_node -> bot
            geographic_distance = None
            last_node_key = None
            
            # Check if last_node prefix is unique (for public key)
            try:
                count_query = f'''
                    SELECT COUNT(DISTINCT public_key) as count
                    FROM complete_contact_tracking 
                    WHERE public_key LIKE ?
                    AND role IN ('repeater', 'roomserver')
                    AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                '''
                prefix_pattern = f"{last_node}%"
                count_results = self.bot.db_manager.execute_query(count_query, (prefix_pattern,))
                
                if count_results and count_results[0].get('count', 0) == 1:
                    query = f'''
                        SELECT public_key 
                        FROM complete_contact_tracking 
                        WHERE public_key LIKE ?
                        AND role IN ('repeater', 'roomserver')
                        AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                        ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                        LIMIT 1
                    '''
                    results = self.bot.db_manager.execute_query(query, (prefix_pattern,))
                    if results and results[0].get('public_key'):
                        last_node_key = results[0]['public_key']
            except Exception as e:
                self.logger.debug(f"Error checking uniqueness for trace last_node {last_node}: {e}")
            
            # Get bot's public key (100% certain - it's us)
            bot_key = None
            if hasattr(self.bot.meshcore, 'device') and self.bot.meshcore.device:
                try:
                    device_info = self.bot.meshcore.device
                    if hasattr(device_info, 'public_key'):
                        pubkey = device_info.public_key
                        if isinstance(pubkey, str):
                            bot_key = pubkey
                        elif isinstance(pubkey, bytes):
                            bot_key = pubkey.hex()
                except Exception as e:
                    self.logger.debug(f"Could not get bot public key: {e}")
            
            # Calculate distance if locations available
            try:
                if bot_location:
                    last_node_result = _get_node_location_from_db(self.bot, last_node, bot_location, recency_days)
                    if last_node_result:
                        last_node_location, selected_key = last_node_result
                        if not last_node_key and selected_key:
                            last_node_key = selected_key
                        
                        if last_node_location and bot_location:
                            geographic_distance = calculate_distance(
                                last_node_location[0], last_node_location[1],
                                bot_location[0], bot_location[1]
                            )
            except Exception as e:
                self.logger.debug(f"Could not calculate distance for trace edge {last_node}->{bot_prefix}: {e}")
            
            # Add edge: last_node -> bot (100% certain - bot received the packet)
            mesh_graph.add_edge(
                from_prefix=last_node,
                to_prefix=bot_prefix,
                from_public_key=last_node_key,  # Only if prefix was unique (certain)
                to_public_key=bot_key,          # 100% certain - it's the bot
                hop_position=len(path_hashes),  # Position in path
                geographic_distance=geographic_distance
            )
            
            # Create edges between nodes in the pathHashes (if more than one)
            # These represent intermediate hops
            previous_location = bot_location
            for i in range(len(path_hashes) - 1, 0, -1):  # Go backwards from bot
                from_node = path_hashes[i-1].lower()
                to_node = path_hashes[i].lower()
                hop_position = len(path_hashes) - i  # Position from bot
                
                # Get public keys if unique
                from_node_key = None
                to_node_key = None
                
                # Check uniqueness for both nodes
                for node, key_var in [(from_node, 'from_node_key'), (to_node, 'to_node_key')]:
                    try:
                        count_query = f'''
                            SELECT COUNT(DISTINCT public_key) as count
                            FROM complete_contact_tracking 
                            WHERE public_key LIKE ?
                            AND role IN ('repeater', 'roomserver')
                            AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                        '''
                        prefix_pattern = f"{node}%"
                        count_results = self.bot.db_manager.execute_query(count_query, (prefix_pattern,))
                        
                        if count_results and count_results[0].get('count', 0) == 1:
                            query = f'''
                                SELECT public_key 
                                FROM complete_contact_tracking 
                                WHERE public_key LIKE ?
                                AND role IN ('repeater', 'roomserver')
                                AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                                ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                                LIMIT 1
                            '''
                            results = self.bot.db_manager.execute_query(query, (prefix_pattern,))
                            if results and results[0].get('public_key'):
                                if key_var == 'from_node_key':
                                    from_node_key = results[0]['public_key']
                                else:
                                    to_node_key = results[0]['public_key']
                    except Exception as e:
                        self.logger.debug(f"Error checking uniqueness for trace node {node}: {e}")
                
                # Calculate distance
                geographic_distance = None
                try:
                    if previous_location:
                        from_result = _get_node_location_from_db(self.bot, from_node, previous_location, recency_days)
                        if from_result:
                            from_location, selected_from_key = from_result
                            if not from_node_key and selected_from_key:
                                from_node_key = selected_from_key
                            
                            to_result = _get_node_location_from_db(self.bot, to_node, from_location, recency_days)
                            if to_result:
                                to_location, selected_to_key = to_result
                                if not to_node_key and selected_to_key:
                                    to_node_key = selected_to_key
                                
                                if from_location and to_location:
                                    geographic_distance = calculate_distance(
                                        from_location[0], from_location[1],
                                        to_location[0], to_location[1]
                                    )
                                    previous_location = from_location
                except Exception as e:
                    self.logger.debug(f"Could not calculate distance for trace edge {from_node}->{to_node}: {e}")
                
                # Add edge between path nodes
                mesh_graph.add_edge(
                    from_prefix=from_node,
                    to_prefix=to_node,
                    from_public_key=from_node_key,  # Only if prefix was unique (certain)
                    to_public_key=to_node_key,      # Only if prefix was unique (certain)
                    hop_position=hop_position,
                    geographic_distance=geographic_distance
                )
    
    async def discover_message_path(self, sender_id: str, rf_data: dict) -> tuple[int, str]:
        """
        Discover the actual routing path for a message using CLI commands.
        This is more reliable than trying to decode packet fragments.
        
        Args:
            sender_id: The name or ID of the sender
            rf_data: The RF data containing pubkey information
            
        Returns:
            tuple[int, str]: (Number of hops, formatted path string)
        """
        try:
            # First try to find the contact by name
            if hasattr(self.bot.meshcore, 'contacts') and self.bot.meshcore.contacts:
                contact = None
                pubkey_prefix = rf_data.get('pubkey_prefix', '')
                
                # Look for contact by name first
                for contact_key, contact_data in self.bot.meshcore.contacts.items():
                    if contact_data.get('adv_name') == sender_id:
                        contact = contact_data
                        break
                
                # If not found by name, try by pubkey prefix
                if not contact and pubkey_prefix:
                    for contact_key, contact_data in self.bot.meshcore.contacts.items():
                        if contact_data.get('public_key', '').startswith(pubkey_prefix):
                            contact = contact_data
                            break
                
                if contact:
                    # Use the stored path information if available
                    out_path = contact.get('out_path', '')
                    out_path_len = contact.get('out_path_len', -1)
                    
                    if out_path_len == 0:
                        self.logger.debug(f"Direct connection to {sender_id}")
                        return 0, "Direct"
                    elif out_path_len > 0:
                        # Format the path string with two-character node prefixes
                        path_string = self._format_path_string(out_path)
                        self.logger.debug(f"Stored path to {sender_id}: {out_path_len} hops via {path_string}")
                        return out_path_len, path_string
                    else:
                        # Path not set - use basic info
                        self.logger.debug(f"No stored path for {sender_id}, using basic info")
                        return 255, "No stored path"
                else:
                    self.logger.debug(f"Contact {sender_id} not found in contacts")
                    return 255, "Unknown"  # Unknown path
            
            return 255, "Unknown"  # Fallback to unknown
            
        except Exception as e:
            self.logger.error(f"Error discovering message path: {e}")
            return 255
    
    # CLI path discovery removed - focusing only on packet decoding
    
    async def _debug_decode_message_path(self, message: MeshMessage, sender_id: str, rf_data: dict):
        """
        Debug method to decode and log path information for ALL incoming messages.
        This runs regardless of whether the message matches keywords, helping with
        network topology debugging.
        
        Args:
            message: The received message
            sender_id: The name or ID of the sender
            rf_data: The RF data containing pubkey information
        """
        try:
            if not rf_data:
                self.logger.debug(f"No RF data for {sender_id}")
                return
            
            pubkey_prefix = rf_data.get('pubkey_prefix', '')
            if not pubkey_prefix:
                self.logger.debug(f"No pubkey prefix for {sender_id}")
                return
            
            # Try to find the contact to get stored path information
            if hasattr(self.bot.meshcore, 'contacts') and self.bot.meshcore.contacts:
                contact = None
                
                # Look for contact by name first
                for contact_key, contact_data in self.bot.meshcore.contacts.items():
                    if contact_data.get('adv_name') == sender_id:
                        contact = contact_data
                        break
                
                # If not found by name, try by pubkey prefix
                if not contact:
                    for contact_key, contact_data in self.bot.meshcore.contacts.items():
                        if contact_data.get('public_key', '').startswith(pubkey_prefix):
                            contact = contact_data
                            break
                
                if contact:
                    out_path = contact.get('out_path', '')
                    out_path_len = contact.get('out_path_len', -1)
                    
                    if out_path_len == 0:
                        self.logger.info(f"📡 {sender_id} → Direct connection")
                    elif out_path_len > 0:
                        path_string = self._format_path_string(out_path)
                        self.logger.info(f"📡 {sender_id} → {path_string} ({out_path_len} hops)")
                    else:
                        self.logger.info(f"📡 {sender_id} → Path not set")
                else:
                    self.logger.info(f"📡 {sender_id} → Contact not found")
            else:
                self.logger.debug(f"No contacts available for {sender_id}")
                
        except Exception as e:
            self.logger.error(f"Error in debug path decoding: {e}")
    
    async def _debug_decode_packet_for_message(self, message: MeshMessage, sender_id: str, rf_data: dict):
        """
        Debug method to decode and log packet information for ALL incoming messages.
        This provides comprehensive packet analysis for debugging purposes.
        
        Args:
            message: The received message
            sender_id: The name or ID of the sender
            rf_data: The RF data containing raw packet information
        """
        try:
            if not rf_data:
                self.logger.debug(f"No RF data available for {sender_id}")
                return
            
            raw_hex = rf_data.get('raw_hex', '')
            if not raw_hex:
                self.logger.debug(f"No raw_hex in RF data for {sender_id}")
                return
            
            self.logger.debug(f"Decoding packet for {sender_id} ({len(raw_hex)} chars)")
            
            # Log basic payload info if available
            extracted_payload = rf_data.get('payload', '')
            payload_length = rf_data.get('payload_length', 0)
            
            if extracted_payload:
                self.logger.debug(f"Payload: {payload_length} bytes")
            else:
                self.logger.debug("No payload data available")
                
        except Exception as e:
            self.logger.error(f"Error in debug packet decoding: {e}")
    
    def _format_path_string(self, hex_path: str) -> str:
        """
        Convert a hex path string to the two-character node prefix format.
        
        Args:
            hex_path: Hex string representing the path (e.g., "01025f7e")
            
        Returns:
            str: Formatted path string (e.g., "01,02,5f,7e")
        """
        try:
            if not hex_path:
                return "Direct"
            
            # Convert hex to bytes and extract one-byte chunks for two-character format
            path_bytes = bytes.fromhex(hex_path)
            path_nodes = []
            
            for i in range(len(path_bytes)):
                # Extract each byte and convert to two-character hex
                node_id = path_bytes[i]
                path_nodes.append(f"{node_id:02x}")
            
            if path_nodes:
                return ",".join(path_nodes)
            else:
                return "Direct"
                
        except Exception as e:
            self.logger.debug(f"Error formatting path string: {e}")
            truncated = hex_path[:16] if len(hex_path) > 16 else hex_path
            return f"Raw: {truncated}{'...' if len(hex_path) > 16 else ''}"
    
    async def process_message(self, message: MeshMessage):
        """Process a received message"""
        # Check if multitest is listening and notify it
        if self.multitest_listener:
            try:
                self.multitest_listener.on_message_received(message)
            except AttributeError as e:
                self.logger.warning(f"Multitest listener missing method: {e}")
                self.multitest_listener = None  # Disable broken listener
            except Exception as e:
                self.logger.error(f"Error notifying multitest listener: {e}", exc_info=True)
        
        # Record all messages in stats database FIRST (before any filtering)
        # This ensures we collect stats for all channels, not just monitored ones
        if 'stats' in self.bot.command_manager.commands:
            stats_command = self.bot.command_manager.commands['stats']
            if stats_command:
                stats_command.record_message(message)
                stats_command.record_path_stats(message)
        
        # Check greeter command for public channel messages (BEFORE general message filtering)
        # This allows greeter to work on its own configured channels even if not in monitor_channels
        if 'greeter' in self.bot.command_manager.commands:
            greeter_command = self.bot.command_manager.commands['greeter']
            # First, check if this message should cancel a pending greeting (human greeting detection)
            if greeter_command:
                greeter_command.check_message_for_human_greeting(message)
            # Then check if we should greet this user
            if greeter_command and greeter_command.should_execute(message):
                try:
                    success = await greeter_command.execute(message)
                    
                    # Small delay to ensure send_response has completed
                    await asyncio.sleep(0.1)
                    
                    # Determine if a response was sent
                    response_sent = False
                    if hasattr(greeter_command, 'last_response') and greeter_command.last_response:
                        response_sent = True
                    elif hasattr(self.bot.command_manager, '_last_response') and self.bot.command_manager._last_response:
                        response_sent = True
                    
                    # Record command execution in stats database
                    if 'stats' in self.bot.command_manager.commands:
                        stats_command = self.bot.command_manager.commands['stats']
                        if stats_command:
                            stats_command.record_command(message, 'greeter', response_sent)
                except Exception as e:
                    self.logger.error(f"Error executing greeter command: {e}")
        
        # Now check if we should process this message for bot responses
        if not self.should_process_message(message):
            return
        
        self.logger.info(f"Processing message: {message.content}")
        
        # Check for advert command (DM only)
        if message.is_dm and message.content.strip().lower() == "advert":
            await self.bot.command_manager.handle_advert_command(message)
            return
        
        # Check for keywords and custom syntax
        keyword_matches = self.bot.command_manager.check_keywords(message)
        
        help_response_sent = False
        plugin_command_with_response_matched = False
        if keyword_matches:
            for keyword, response in keyword_matches:
                # Use translator if available for logging
                if hasattr(self.bot, 'translator'):
                    log_msg = self.bot.translator.translate('messages.keyword_matched', keyword=keyword)
                    self.logger.info(log_msg)
                else:
                    self.logger.info(f"Keyword '{keyword}' matched, responding")
                
                # Track if this is a help response
                if keyword == 'help':
                    help_response_sent = True
                
                # Track if this is a plugin command that has a response format
                if keyword in self.bot.command_manager.commands and response is not None:
                    plugin_command_with_response_matched = True
                
                # Skip commands that handle their own responses (response is None)
                # These will be recorded when they execute via execute_commands
                if response is None:
                    continue
                
                # Record command execution in stats database for keyword-matched commands with responses
                # Commands without responses (response is None) are recorded in execute_commands to avoid double-counting
                if 'stats' in self.bot.command_manager.commands:
                    stats_command = self.bot.command_manager.commands['stats']
                    if stats_command:
                        # response is not None here, so we know a response will be sent
                        stats_command.record_command(message, keyword, True)
                
                # Generate command_id for repeat tracking (before sending)
                import time
                command_id = f"keyword_{keyword}_{message.sender_id}_{int(time.time())}"
                
                # Send response (pass command_id so transmission record uses it directly)
                try:
                    rate_limit_key = self.bot.command_manager.get_rate_limit_key(message)
                    if message.is_dm:
                        success = await self.bot.command_manager.send_dm(
                            message.sender_id, response, command_id, rate_limit_key=rate_limit_key
                        )
                    else:
                        success = await self.bot.command_manager.send_channel_message(
                            message.channel, response, command_id, rate_limit_key=rate_limit_key
                        )
                    
                    if not success:
                        self.logger.warning(f"Failed to send keyword response for '{keyword}' to {message.sender_id if message.is_dm else message.channel}")
                except Exception as e:
                    self.logger.error(f"Error sending keyword response for '{keyword}': {e}", exc_info=True)
                    success = False
                
                # Capture keyword command data for web viewer
                if (hasattr(self.bot, 'web_viewer_integration') and 
                    self.bot.web_viewer_integration and 
                    self.bot.web_viewer_integration.bot_integration):
                    try:
                        self.bot.web_viewer_integration.bot_integration.capture_command(
                            message, keyword, response, success, command_id
                        )
                    except Exception as e:
                        self.logger.debug(f"Failed to capture keyword data for web viewer: {e}")
        
        # Only execute commands if no help response was sent and no plugin command with response was matched
        # Help responses and plugin commands with responses should be the final response for that message
        # Plugin commands without responses (response is None) should still be executed
        if not help_response_sent and not plugin_command_with_response_matched:
            await self.bot.command_manager.execute_commands(message)
    
    def should_process_message(self, message: MeshMessage) -> bool:
        """Check if message should be processed by the bot"""
        # Check if bot is enabled
        if not self.bot.config.getboolean('Bot', 'enabled'):
            return False
        
        # Check if sender is banned (starts-with matching)
        if self.bot.command_manager.is_user_banned(message.sender_id):
            self.logger.debug(f"Ignoring message from banned user: {message.sender_id}")
            return False
        
        # Check if channel is monitored (with command override support)
        if not message.is_dm and message.channel:
            # Check if channel is in global monitor_channels
            if message.channel in self.bot.command_manager.monitor_channels:
                return True  # Global allow - all commands can work
            
            # Check if ANY command allows this channel (for selective access)
            for command_name, command in self.bot.command_manager.commands.items():
                if hasattr(command, 'is_channel_allowed') and callable(command.is_channel_allowed):
                    if command.is_channel_allowed(message):
                        # At least one command allows this channel
                        self.logger.debug(f"Channel {message.channel} allowed by command '{command_name}' override")
                        return True
            
            # Channel not in global list and no command allows it
            self.logger.debug(f"Channel {message.channel} not in monitored channels: {self.bot.command_manager.monitor_channels}")
            return False
        
        # Check if DMs are enabled
        if message.is_dm and not self.bot.config.getboolean('Channels', 'respond_to_dms'):
            self.logger.debug("DMs are disabled")
            return False
        
        return True
    
    async def handle_new_contact(self, event, metadata=None):
        """Handle NEW_CONTACT events for automatic contact management"""
        try:
            # Copy payload immediately to avoid segfault if event is freed
            # Make a deep copy to ensure we have all the data we need
            if hasattr(event, 'payload'):
                contact_data = copy.deepcopy(event.payload)
            else:
                # Fallback: try to copy the event itself if it's a dict-like object
                contact_data = copy.deepcopy(event) if isinstance(event, dict) else None
            
            if not contact_data:
                self.logger.warning("NEW_CONTACT event has no payload data")
                return
            
            self.logger.info(f"🔍 NEW_CONTACT EVENT RECEIVED: {event}")
            self.logger.info(f"📦 Event type: {type(event)}")
            self.logger.info(f"📦 Event payload: {contact_data}")
            
            # Get contact details
            contact_name = contact_data.get('name', contact_data.get('adv_name', 'Unknown'))
            public_key = contact_data.get('public_key', '')
            
            self.logger.info(f"Processing new contact: {contact_name} (key: {public_key[:16]}...)")
            
            # Extract additional signal information from the event
            signal_info = {}
            if metadata:
                signal_info.update(metadata)
            
            # Try to get signal data, packet_hash, and path information from recent RF data correlation
            # Only collect RSSI/SNR for zero-hop (direct) advertisements
            packet_hash = None
            try:
                # Look for recent RF data that might correlate with this contact
                recent_rf_data = self.bot.message_handler.recent_rf_data
                if recent_rf_data:
                    # Find RF data that might match this contact's public key
                    for rf_entry in recent_rf_data[-10:]:  # Check last 10 RF entries
                        if 'routing_info' in rf_entry:
                            routing_info = rf_entry['routing_info']
                            
                            # Extract packet_hash if available
                            packet_hash = routing_info.get('packet_hash') or rf_entry.get('packet_hash')
                            
                            # Extract path information from routing_info
                            path_hex = routing_info.get('path_hex', '')
                            path_length = routing_info.get('path_length', 0)
                            
                            # Add path information to contact_data if not already present
                            if 'out_path' not in contact_data or not contact_data.get('out_path'):
                                if path_hex and path_length > 0:
                                    contact_data['out_path'] = path_hex
                                    contact_data['out_path_len'] = path_length
                                elif path_length == 0:
                                    contact_data['out_path'] = ''
                                    contact_data['out_path_len'] = 0
                            
                            # Update mesh graph with this NEW_CONTACT event's path information
                            # This captures public keys for edges that we might not see in regular message paths
                            if path_hex and path_length > 0 and public_key:
                                try:
                                    packet_info = {
                                        'routing_info': routing_info,
                                        'packet_hash': packet_hash
                                    }
                                    self._update_mesh_graph_from_advert(contact_data, path_hex, path_length, packet_info)
                                    self.logger.debug(f"Mesh graph: Updated from NEW_CONTACT event for {contact_name} (key: {public_key[:16]}...)")
                                    # Store complete path in observed_paths table
                                    self._store_observed_path(contact_data, path_hex, path_length, 'advert', packet_hash=packet_hash)
                                except Exception as e:
                                    self.logger.debug(f"Error updating mesh graph from NEW_CONTACT: {e}")
                            
                            # Only collect signal data for direct (zero-hop) advertisements
                            if path_length == 0:
                                # Direct advertisement - collect signal data
                                if 'snr' in rf_entry:
                                    signal_info['snr'] = rf_entry['snr']
                                if 'rssi' in rf_entry:
                                    signal_info['rssi'] = rf_entry['rssi']
                                signal_info['hops'] = 0
                                self.logger.debug(f"📡 Direct advertisement - collecting signal data: SNR={rf_entry.get('snr')}, RSSI={rf_entry.get('rssi')}")
                            else:
                                # Multi-hop advertisement - only collect hop count, not signal data
                                signal_info['hops'] = path_length
                                self.logger.debug(f"📡 Multi-hop advertisement ({path_length} hops) - skipping signal data collection")
                            break
            except Exception as e:
                self.logger.debug(f"Could not correlate RF data: {e}")
            
            # Log captured signal information
            if signal_info:
                self.logger.info(f"📡 Signal data: {signal_info}")
            else:
                self.logger.info(f"📡 No signal data available")
            
            # Check if this is a repeater or companion
            if hasattr(self.bot, 'repeater_manager'):
                is_repeater = self.bot.repeater_manager._is_repeater_device(contact_data)
                
                if is_repeater:
                    # REPEATER: Track directly in SQLite database (no device contact list)
                    self.logger.info(f"📡 New repeater discovered: {contact_name} - tracking in database only")
                    
                    # Track repeater in complete database with signal info
                    await self.bot.repeater_manager.track_contact_advertisement(contact_data, signal_info, packet_hash=packet_hash)
                    
                    # Notify web viewer of new node
                    if (hasattr(self.bot, 'web_viewer_integration') and 
                        self.bot.web_viewer_integration and 
                        self.bot.web_viewer_integration.bot_integration):
                        try:
                            node_data = {
                                'public_key': public_key,
                                'prefix': public_key[:2].lower() if public_key else '',
                                'name': contact_name,
                                'role': 'repeater'
                            }
                            self.bot.web_viewer_integration.bot_integration.send_mesh_node_update(node_data)
                        except Exception as e:
                            self.logger.debug(f"Failed to notify web viewer of new node: {e}")
                    
                    # Check if auto-purge is needed (run after tracking to ensure data is captured)
                    await self.bot.repeater_manager.check_and_auto_purge()
                    
                    self.logger.info(f"✅ Repeater {contact_name} tracked in database - not added to device contacts")
                    return
                else:
                    # COMPANION: Track in database AND add to device contact list
                    self.logger.info(f"👤 New companion discovered: {contact_name} - will be added to device contacts")
                    
                    # Track companion in complete database with signal info
                    await self.bot.repeater_manager.track_contact_advertisement(contact_data, signal_info, packet_hash=packet_hash)
                    
                    # Add companion to device contact list
                    try:
                        result = await self.bot.meshcore.commands.add_contact(contact_data)
                        if hasattr(result, 'type') and result.type.name == 'OK':
                            self.logger.info(f"✅ Companion {contact_name} added to device contacts")
                        else:
                            self.logger.warning(f"❌ Failed to add companion {contact_name} to device: {result}")
                    except Exception as e:
                        self.logger.error(f"❌ Error adding companion {contact_name} to device: {e}")
                    
                    # Check if auto-purge is needed
                    await self.bot.repeater_manager.check_and_auto_purge()
                    return
            
            # Fallback: Track in database for unknown contact types
            if hasattr(self.bot, 'repeater_manager'):
                await self.bot.repeater_manager.track_contact_advertisement(contact_data, packet_hash=packet_hash)
                await self.bot.repeater_manager.check_and_auto_purge()
            
            # For unknown contact types, handle based on auto_manage_contacts setting
            if hasattr(self.bot, 'repeater_manager'):
                auto_manage_setting = self.bot.config.get('Bot', 'auto_manage_contacts', fallback='false').lower()
                
                if auto_manage_setting == 'device':
                    # Device mode: Let device handle auto-addition, bot manages capacity
                    self.logger.info(f"Device auto-addition mode - new contact '{contact_name}' will be handled by device")
                    
                    # Check contact list capacity and manage if needed
                    status = await self.bot.repeater_manager.get_contact_list_status()
                    
                    if status and status.get('is_near_limit', False):
                        self.logger.warning(f"Contact list near limit ({status['usage_percentage']:.1f}%) - managing capacity")
                        await self.bot.repeater_manager.manage_contact_list(auto_cleanup=True)
                    else:
                        self.logger.info(f"New contact '{contact_name}' - contact list has adequate space")
                        
                elif auto_manage_setting == 'bot':
                    # Bot mode: Bot automatically adds companion contacts to device and manages capacity
                    self.logger.info(f"Bot auto-addition mode - automatically adding new companion contact '{contact_name}' to device")
                    
                    # Add the contact to the device's contact list
                    success = await self.bot.repeater_manager.add_discovered_contact(
                        contact_name, 
                        public_key, 
                        f"Auto-added companion contact discovered via NEW_CONTACT event"
                    )
                    
                    if success:
                        self.logger.info(f"Successfully added companion contact '{contact_name}' to device")
                    else:
                        self.logger.warning(f"Failed to add companion contact '{contact_name}' to device")
                    
                    # Check contact list capacity and manage if needed
                    status = await self.bot.repeater_manager.get_contact_list_status()
                    
                    if status and status.get('is_near_limit', False):
                        self.logger.warning(f"Contact list near limit ({status['usage_percentage']:.1f}%) - managing capacity")
                        await self.bot.repeater_manager.manage_contact_list(auto_cleanup=True)
                    else:
                        self.logger.info(f"New contact '{contact_name}' - contact list has adequate space")
                        
                else:  # false or any other value
                    # Manual mode: Just log the discovery, no automatic actions
                    self.logger.info(f"Manual mode - new companion contact '{contact_name}' discovered (not auto-added)")
            
            # Log the new contact discovery
            if hasattr(self.bot, 'repeater_manager'):
                self.bot.repeater_manager.db_manager.execute_update(
                    'INSERT INTO purging_log (action, details) VALUES (?, ?)',
                    ('new_contact_discovered', f'New contact discovered: {contact_name} (key: {public_key[:16]}...)')
                )
            
        except Exception as e:
            self.logger.error(f"Error handling new contact event: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
