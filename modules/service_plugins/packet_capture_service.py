#!/usr/bin/env python3
"""
Packet Capture Service for MeshCore Bot
Captures packets from MeshCore radios and outputs to console, file, and MQTT.
Adapted from meshcore-packet-capture project.
"""

import asyncio
import json
import logging
import hashlib
import time
import re
import os
import copy
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
import socket

# Import meshcore
import meshcore
from meshcore import EventType

# Import bot's enums
from ..enums import AdvertFlags, PayloadType, PayloadVersion, RouteType, DeviceRole

# Import bot's utilities for packet hash
from ..utils import calculate_packet_hash

# Import MQTT client
try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

# Import auth token utilities
from .packet_capture_utils import (
    create_auth_token_async,
    read_private_key_file
)

# Import base service
from .base_service import BaseServicePlugin


class PacketCaptureService(BaseServicePlugin):
    """Packet capture service using bot's meshcore connection.
    
    Captures packets from MeshCore network and publishes to MQTT.
    Supports multiple MQTT brokers, auth tokens, and output to file.
    """
    
    config_section = 'PacketCapture'  # Explicit config section
    description = "Captures packets from MeshCore network and publishes to MQTT"
    
    def __init__(self, bot):
        """Initialize packet capture service.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        
        # Don't store meshcore here - it's None until bot connects
        # Use self.meshcore property to get current connection
        
        # Setup logging (use bot's formatter and configuration)
        self.logger = logging.getLogger('PacketCaptureService')
        self.logger.setLevel(bot.logger.level)
        
        # Only setup handlers if none exist to prevent duplicates
        if not self.logger.handlers:
            # Use the same formatter as the bot (colored if enabled)
            # Get formatter from bot's console handler
            bot_formatter = None
            for handler in bot.logger.handlers:
                if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                    bot_formatter = handler.formatter
                    break
            
            # If no formatter found, create one matching bot's style
            if not bot_formatter:
                try:
                    import colorlog
                    colored = (bot.config.getboolean('Logging', 'colored_output', fallback=True)
                               if bot.config.has_section('Logging') else True)
                    if colored:
                        bot_formatter = colorlog.ColoredFormatter(
                            '%(log_color)s%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            log_colors={
                                'DEBUG': 'cyan',
                                'INFO': 'green',
                                'WARNING': 'yellow',
                                'ERROR': 'red',
                                'CRITICAL': 'red,bg_white',
                            }
                        )
                    else:
                        bot_formatter = logging.Formatter(
                            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S'
                        )
                except ImportError:
                    # Fallback if colorlog not available
                    bot_formatter = logging.Formatter(
                        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S'
                    )
            
            # Add console handler with bot's formatter
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(bot_formatter)
            self.logger.addHandler(console_handler)
        
        # Prevent propagation to root logger to avoid duplicate output
        self.logger.propagate = False
        
        # Load configuration from bot's config
        self._load_config()
        
        # Connection state (uses bot's connection, but track our state)
        self.connected = False
        
        # Packet tracking
        self.packet_count = 0
        self.output_handle = None
        
        # MQTT
        self.mqtt_clients: List[Dict[str, Any]] = []
        self.mqtt_connected = False
        
        # Stats/status publishing
        self.stats_status_enabled = self.get_config_bool('stats_in_status_enabled', True)
        self.stats_refresh_interval = self.get_config_int('stats_refresh_interval', 300)
        self.latest_stats = None
        self.last_stats_fetch = 0
        self.stats_supported = False
        self.stats_capability_state = None
        self.stats_update_task = None
        self.stats_fetch_lock = asyncio.Lock()
        self.cached_firmware_info = None
        self.radio_info = None
        
        # Background tasks
        self.background_tasks: List[asyncio.Task] = []
        self.should_exit = False
        
        # JWT renewal (default: 12 hours, tokens valid for 24 hours)
        self.jwt_renewal_interval = self.get_config_int('jwt_renewal_interval', 43200)
        
        # Health check
        self.health_check_interval = self.get_config_int('health_check_interval', 30)
        self.health_check_grace_period = self.get_config_int('health_check_grace_period', 2)
        self.health_check_failure_count = 0
        
        # Event subscriptions (track for cleanup)
        self.event_subscriptions = []
        
        self.logger.info("Packet capture service initialized")
    
    def _load_config(self) -> None:
        """Load configuration from bot's config.
        
        Loads settings for output file, MQTT brokers, auth tokens, and
        other service options.
        """
        config = self.bot.config
        
        # Check if enabled
        self.enabled = config.getboolean('PacketCapture', 'enabled', fallback=False)
        
        # Output file
        self.output_file = config.get('PacketCapture', 'output_file', fallback=None)
        
        # Verbose/debug
        self.verbose = config.getboolean('PacketCapture', 'verbose', fallback=False)
        self.debug = config.getboolean('PacketCapture', 'debug', fallback=False)
        
        # MQTT configuration
        self.mqtt_enabled = config.getboolean('PacketCapture', 'mqtt_enabled', fallback=True)
        self.mqtt_brokers = self._parse_mqtt_brokers(config)
        
        # Global IATA
        self.global_iata = config.get('PacketCapture', 'iata', fallback='LOC').lower()
        
        # Owner information
        self.owner_public_key = config.get('PacketCapture', 'owner_public_key', fallback=None)
        self.owner_email = config.get('PacketCapture', 'owner_email', fallback=None)
        
        # Private key for auth tokens (fallback if device signing not available)
        self.private_key_path = config.get('PacketCapture', 'private_key_path', fallback=None)
        self.private_key_hex = None
        if self.private_key_path:
            self.private_key_hex = read_private_key_file(self.private_key_path)
            if not self.private_key_hex:
                self.logger.warning(f"Could not load private key from {self.private_key_path}")
        
        # Auth token method preference
        self.auth_token_method = config.get('PacketCapture', 'auth_token_method', fallback='device').lower()
        # 'device' = try on-device signing first, fallback to Python
        # 'python' = use Python signing only
        
        # Note: Python signing can fetch private key from device if not provided via file
        # The create_auth_token_async function will automatically try to export the key
        # from the device if private_key_hex is None and meshcore_instance is available
    
    def _parse_mqtt_brokers(self, config) -> List[Dict[str, Any]]:
        """Parse MQTT broker configuration (mqttN_* format).
        
        Args:
            config: ConfigParser object containing the configuration.
            
        Returns:
            List[Dict[str, Any]]: List of configured MQTT broker dictionaries.
        """
        brokers = []
        
        # Parse multiple brokers (mqtt1_*, mqtt2_*, etc.)
        broker_num = 1
        while True:
            enabled_key = f'mqtt{broker_num}_enabled'
            server_key = f'mqtt{broker_num}_server'
            
            if not config.has_option('PacketCapture', server_key):
                break
            
            enabled = config.getboolean('PacketCapture', enabled_key, fallback=True)
            if not enabled:
                broker_num += 1
                continue
            
            broker = {
                'enabled': True,
                'host': config.get('PacketCapture', server_key, fallback='localhost'),
                'port': config.getint('PacketCapture', f'mqtt{broker_num}_port', fallback=1883),
                'username': config.get('PacketCapture', f'mqtt{broker_num}_username', fallback=None),
                'password': config.get('PacketCapture', f'mqtt{broker_num}_password', fallback=None),
                'topic_prefix': config.get('PacketCapture', f'mqtt{broker_num}_topic_prefix', fallback=None),
                'topic_status': config.get('PacketCapture', f'mqtt{broker_num}_topic_status', fallback=None),
                'topic_packets': config.get('PacketCapture', f'mqtt{broker_num}_topic_packets', fallback=None),
                'use_auth_token': config.getboolean('PacketCapture', f'mqtt{broker_num}_use_auth_token', fallback=False),
                'token_audience': config.get('PacketCapture', f'mqtt{broker_num}_token_audience', fallback=None),
                'transport': config.get('PacketCapture', f'mqtt{broker_num}_transport', fallback='tcp').lower(),
                'use_tls': config.getboolean('PacketCapture', f'mqtt{broker_num}_use_tls', fallback=False),
                'websocket_path': config.get('PacketCapture', f'mqtt{broker_num}_websocket_path', fallback='/mqtt'),
                'client_id': config.get('PacketCapture', f'mqtt{broker_num}_client_id', fallback=None)
            }
            
            # Set default topic_prefix if not set
            if not broker['topic_prefix']:
                broker['topic_prefix'] = 'meshcore/packets'
            
            brokers.append(broker)
            broker_num += 1
        
        return brokers
    
    def get_config_bool(self, key: str, fallback: bool = False) -> bool:
        """Get boolean config value.
        
        Args:
            key: Config key to retrieve.
            fallback: Default value if key is missing.
            
        Returns:
            bool: Config value or fallback.
        """
        return self.bot.config.getboolean('PacketCapture', key, fallback=fallback)
    
    def get_config_int(self, key: str, fallback: int = 0) -> int:
        """Get integer config value.
        
        Args:
            key: Config key to retrieve.
            fallback: Default value if key is missing.
            
        Returns:
            int: Config value or fallback.
        """
        return self.bot.config.getint('PacketCapture', key, fallback=fallback)
    
    def get_config_float(self, key: str, fallback: float = 0.0) -> float:
        """Get float config value.
        
        Args:
            key: Config key to retrieve.
            fallback: Default value if key is missing.
            
        Returns:
            float: Config value or fallback.
        """
        return self.bot.config.getfloat('PacketCapture', key, fallback=fallback)
    
    def get_config_str(self, key: str, fallback: str = '') -> str:
        """Get string config value.
        
        Args:
            key: Config key to retrieve.
            fallback: Default value if key is missing.
            
        Returns:
            str: Config value or fallback.
        """
        return self.bot.config.get('PacketCapture', key, fallback=fallback)
    
    @property
    def meshcore(self):
        """Get meshcore connection from bot (always current).
        
        Returns:
            MeshCore: The meshcore instance from the bot.
        """
        return self.bot.meshcore if self.bot else None

    def is_healthy(self) -> bool:
        return (
            self._running
            and bool(self.meshcore and self.meshcore.is_connected)
        )

    async def start(self) -> None:
        """Start the packet capture service.
        
        Initializes output file, MQTT connections, and event handlers.
        Waits for bot connection before starting.
        """
        if not self.enabled:
            self.logger.info("Packet capture service is disabled")
            return
        
        # Wait for bot to be connected (with timeout)
        max_wait = 30  # seconds
        wait_time = 0
        while (not self.bot.connected or not self.meshcore) and wait_time < max_wait:
            await asyncio.sleep(0.5)
            wait_time += 0.5
        
        if not self.bot.connected or not self.meshcore:
            self.logger.warning("Bot not connected after waiting, cannot start packet capture")
            return
        
        self.logger.info("Starting packet capture service...")
        
        # Open output file if specified
        if self.output_file:
            try:
                self.output_handle = open(self.output_file, 'a')
                self.logger.info(f"Writing packets to: {self.output_file}")
            except Exception as e:
                self.logger.error(f"Failed to open output file: {e}")
        
        # Setup event handlers
        await self.setup_event_handlers()
        
        # Connect to MQTT brokers
        if self.mqtt_enabled:
            if self._require_mqtt():
                await self.connect_mqtt_brokers()
                # Give MQTT connections a moment to establish
                await asyncio.sleep(2)
                if self.mqtt_connected:
                    self.logger.info(f"MQTT connected to {len(self.mqtt_clients)} broker(s)")
                else:
                    self.logger.warning("MQTT enabled but no brokers connected")
        
        # Start background tasks
        await self.start_background_tasks()
        
        self.connected = True
        self._running = True
        self.logger.info(f"Packet capture service started (MQTT: {'connected' if self.mqtt_connected else 'not connected'})")
    
    async def stop(self) -> None:
        """Stop the packet capture service.
        
        Closes output file, disconnects MQTT, and stops background tasks.
        """
        self.logger.info("Stopping packet capture service...")
        
        self.should_exit = True
        self._running = False
        self.connected = False
        
        # Cancel background tasks
        for task in self.background_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Clean up event subscriptions
        self.cleanup_event_subscriptions()
        
        # Disconnect MQTT
        for mqtt_client_info in self.mqtt_clients:
            try:
                mqtt_client_info['client'].disconnect()
                mqtt_client_info['client'].loop_stop()
            except (AttributeError, RuntimeError, OSError) as e:
                # Silently ignore expected errors during cleanup (client already disconnected, etc.)
                self.logger.debug(f"Error disconnecting MQTT client during cleanup: {e}")
            except Exception as e:
                # Log unexpected errors but don't fail cleanup
                self.logger.warning(f"Unexpected error disconnecting MQTT client: {e}")
        
        # Close output file
        if self.output_handle:
            self.output_handle.close()
            self.output_handle = None
        
        self.logger.info(f"Packet capture service stopped. Total packets captured: {self.packet_count}")
    
    def cleanup_event_subscriptions(self) -> None:
        """Clean up event subscriptions.
        
        Clears local subscription tracking list.
        """
        # Note: meshcore library handles subscription cleanup automatically
        # This is mainly for tracking/logging
        self.event_subscriptions = []
    
    async def setup_event_handlers(self) -> None:
        """Setup event handlers for packet capture.
        
        Subscribes to RX_LOG_DATA and RAW_DATA events.
        """
        if not self.meshcore:
            return
        
        # Handle RX log data
        async def on_rx_log_data(event, metadata=None):
            await self.handle_rx_log_data(event, metadata)
        
        # Handle raw data
        async def on_raw_data(event, metadata=None):
            await self.handle_raw_data(event, metadata)
        
        # Subscribe to events (meshcore supports multiple subscribers)
        self.meshcore.subscribe(EventType.RX_LOG_DATA, on_rx_log_data)
        self.meshcore.subscribe(EventType.RAW_DATA, on_raw_data)
        
        self.event_subscriptions = [
            (EventType.RX_LOG_DATA, on_rx_log_data),
            (EventType.RAW_DATA, on_raw_data)
        ]
        
        self.logger.info("Packet capture event handlers registered")
    
    async def handle_rx_log_data(self, event: Any, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Handle RX log data events (matches original script).
        
        Args:
            event: The RX log data event.
            metadata: Optional metadata dictionary.
        """
        try:
            # Copy payload immediately to avoid segfault if event is freed
            payload = copy.deepcopy(event.payload) if hasattr(event, 'payload') else None
            if payload is None:
                self.logger.warning("RX log data event has no payload")
                return
            
            if 'snr' in payload:
                # Try to get packet data - prefer 'payload' field, fallback to 'raw_hex'
                # This matches the original script's logic exactly
                raw_hex = None
                
                # First, try the 'payload' field (already stripped of framing bytes)
                if 'payload' in payload and payload['payload']:
                    raw_hex = payload['payload']
                # Fallback to raw_hex with first 2 bytes stripped
                elif 'raw_hex' in payload and payload['raw_hex']:
                    raw_hex = payload['raw_hex'][4:]  # Skip first 2 bytes (4 hex chars)
                
                if raw_hex:
                    if self.debug:
                        self.logger.debug(f"Received RX_LOG_DATA: {raw_hex[:50]}...")
                    
                    # Process packet
                    await self.process_packet(raw_hex, payload, metadata)
                else:
                    self.logger.warning(f"RF log data missing both 'payload' and 'raw_hex' fields: {list(payload.keys())}")
            
        except Exception as e:
            self.logger.error(f"Error handling RX log data: {e}")
    
    async def handle_raw_data(self, event: Any, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Handle raw data events.
        
        Args:
            event: The raw data event.
            metadata: Optional metadata dictionary.
        """
        try:
            # Copy payload immediately to avoid segfault if event is freed
            payload = copy.deepcopy(event.payload) if hasattr(event, 'payload') else None
            if payload is None:
                self.logger.warning("Raw data event has no payload")
                return
            raw_data = payload.get('data', '')
            
            if not raw_data:
                return
            
            # Convert to hex string if needed
            if isinstance(raw_data, bytes):
                raw_hex = raw_data.hex()
            elif isinstance(raw_data, str):
                raw_hex = raw_data
                if raw_hex.startswith('0x'):
                    raw_hex = raw_hex[2:]
            else:
                return
            
            # Process packet
            await self.process_packet(raw_hex, payload, metadata)
            
        except Exception as e:
            self.logger.error(f"Error handling raw data: {e}")
    
    def _format_packet_data(self, raw_hex: str, packet_info: Dict[str, Any], payload: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Format packet data to match original script's format_packet_data exactly.
        
        Args:
            raw_hex: Raw hex string of the packet.
            packet_info: Decoded packet information.
            payload: Payload dictionary from the event.
            metadata: Optional metadata dictionary.
            
        Returns:
            Dict[str, Any]: Formatted packet dictionary.
        """
        current_time = datetime.now()
        timestamp = current_time.isoformat()
        
        # Remove 0x prefix if present
        clean_raw_hex = raw_hex.replace('0x', '').upper()
        packet_len = len(clean_raw_hex) // 2  # Convert hex string to byte count
        
        # Map route type to single letter (matches original script)
        route_map = {
            "TRANSPORT_FLOOD": "F",
            "FLOOD": "F",
            "DIRECT": "D",
            "TRANSPORT_DIRECT": "T",
            "UNKNOWN": "U"
        }
        route = route_map.get(packet_info.get('route_type', 'UNKNOWN'), "U")
        
        # Map payload type to string number (matches original script)
        payload_type_map = {
            "REQ": "0",
            "RESPONSE": "1",
            "TXT_MSG": "2",
            "ACK": "3",
            "ADVERT": "4",
            "GRP_TXT": "5",
            "GRP_DATA": "6",
            "ANON_REQ": "7",
            "PATH": "8",
            "TRACE": "9",
            "MULTIPART": "10",
            "Type11": "11",
            "Type12": "12",
            "Type13": "13",
            "Type14": "14",
            "RAW_CUSTOM": "15",
            "UNKNOWN": "0"
        }
        packet_type = payload_type_map.get(packet_info.get('payload_type', 'UNKNOWN'), "0")
        
        # Calculate payload length (matches original script logic)
        firmware_payload_len = payload.get('payload_length')
        if firmware_payload_len is not None:
            payload_len = str(firmware_payload_len)
        else:
            # Calculate from packet structure
            path_len = packet_info.get('path_len', 0)
            has_transport = packet_info.get('has_transport_codes', False)
            transport_bytes = 4 if has_transport else 0
            payload_len = str(max(0, packet_len - 1 - transport_bytes - 1 - path_len))
        
        # Get device name and public key
        device_name = self._get_bot_name()
        if not device_name:
            device_name = "MeshCore Device"
        
        # Get device public key for origin_id
        origin_id = None
        if self.meshcore and hasattr(self.meshcore, 'self_info'):
            try:
                self_info = self.meshcore.self_info
                if isinstance(self_info, dict):
                    origin_id = self_info.get('public_key', '')
                elif hasattr(self_info, 'public_key'):
                    origin_id = self_info.public_key
                
                # Convert to hex string if bytes
                if isinstance(origin_id, bytes):
                    origin_id = origin_id.hex()
                elif isinstance(origin_id, bytearray):
                    origin_id = bytes(origin_id).hex()
            except Exception:
                pass
        
        # Normalize origin_id to uppercase
        if origin_id:
            origin_id = origin_id.replace('0x', '').replace(' ', '').upper()
        else:
            origin_id = 'UNKNOWN'
        
        # Extract RF data
        snr = str(payload.get('snr', 'Unknown'))
        rssi = str(payload.get('rssi', 'Unknown'))
        
        # Get packet hash - check multiple sources in order of preference, then calculate if needed
        # This matches the original script's approach: use hash from routing_info if available, otherwise calculate
        packet_hash = '0000000000000000'
        
        # 1. Check if payload has packet_hash directly (from bot's processing)
        if isinstance(payload, dict):
            if 'packet_hash' in payload:
                packet_hash = payload['packet_hash']
            # 2. Check if payload has routing_info with packet_hash
            elif 'routing_info' in payload:
                routing_info = payload.get('routing_info', {})
                if isinstance(routing_info, dict) and 'packet_hash' in routing_info:
                    packet_hash = routing_info['packet_hash']
        
        # 3. Check metadata if available
        if packet_hash == '0000000000000000' and metadata and isinstance(metadata, dict):
            if 'packet_hash' in metadata:
                packet_hash = metadata['packet_hash']
            elif 'routing_info' in metadata:
                routing_info = metadata.get('routing_info', {})
                if isinstance(routing_info, dict) and 'packet_hash' in routing_info:
                    packet_hash = routing_info['packet_hash']
        
        # 4. Try to get from bot's recent_rf_data cache (if message_handler has processed it)
        # Note: The bot stores full raw_hex (with framing bytes) for packet_prefix, but we use stripped raw_hex
        # So we need to use the full raw_hex from payload for prefix matching
        # Optimized: Use indexed rf_data_by_pubkey for O(1) lookup instead of linear search
        if packet_hash == '0000000000000000' and hasattr(self.bot, 'message_handler'):
            try:
                message_handler = self.bot.message_handler
                
                # Get full raw_hex from payload for prefix matching (bot uses full raw_hex for packet_prefix)
                full_raw_hex = payload.get('raw_hex', '')
                if full_raw_hex:
                    packet_prefix = full_raw_hex.replace('0x', '')[:32] if len(full_raw_hex.replace('0x', '')) >= 32 else full_raw_hex.replace('0x', '')
                else:
                    # Fallback: use stripped raw_hex (might not match, but worth trying)
                    clean_raw_hex_for_lookup = raw_hex.replace('0x', '')
                    packet_prefix = clean_raw_hex_for_lookup[:32] if len(clean_raw_hex_for_lookup) >= 32 else clean_raw_hex_for_lookup
                
                # Use indexed lookup (O(1)) instead of linear search (O(n))
                if hasattr(message_handler, 'rf_data_by_pubkey') and packet_prefix:
                    rf_data_list = message_handler.rf_data_by_pubkey.get(packet_prefix, [])
                    # Check most recent entries first (last in list, since they're appended in order)
                    for rf_data in reversed(rf_data_list):
                        if 'packet_hash' in rf_data:
                            packet_hash = rf_data['packet_hash']
                            break
                        elif 'routing_info' in rf_data:
                            routing_info = rf_data.get('routing_info', {})
                            if isinstance(routing_info, dict) and 'packet_hash' in routing_info:
                                packet_hash = routing_info['packet_hash']
                                break
                
                # Fallback to linear search only if indexed lookup not available (backward compatibility)
                if packet_hash == '0000000000000000' and hasattr(message_handler, 'recent_rf_data'):
                    for rf_data in message_handler.recent_rf_data:
                        # Match by packet_prefix (bot uses full raw_hex for this)
                        if rf_data.get('packet_prefix') == packet_prefix:
                            if 'packet_hash' in rf_data:
                                packet_hash = rf_data['packet_hash']
                                break
                            elif 'routing_info' in rf_data:
                                routing_info = rf_data.get('routing_info', {})
                                if isinstance(routing_info, dict) and 'packet_hash' in routing_info:
                                    packet_hash = routing_info['packet_hash']
                                    break
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"Error checking recent_rf_data for hash: {e}")
        
        # 5. Fall back to hash from decoded packet_info (should be calculated correctly)
        if packet_hash == '0000000000000000':
            packet_hash = packet_info.get('packet_hash', '0000000000000000')
        
        # 6. If still no hash, calculate it from raw_hex (matches original script's format_packet_data)
        if packet_hash == '0000000000000000':
            try:
                # Use payload_type_value from packet_info if available, otherwise None (will be extracted from header)
                payload_type_value = packet_info.get('payload_type_value')
                if payload_type_value is not None:
                    # Ensure it's an integer (handle enum.value if passed)
                    if hasattr(payload_type_value, 'value'):
                        payload_type_value = payload_type_value.value
                    payload_type_value = int(payload_type_value) & 0x0F
                packet_hash = calculate_packet_hash(clean_raw_hex, payload_type_value)
            except Exception as e:
                if self.debug:
                    self.logger.debug(f"Error calculating packet hash: {e}")
                packet_hash = '0000000000000000'
        
        # Build packet data structure (matches original script exactly)
        packet_data = {
            "origin": device_name,
            "origin_id": origin_id,
            "timestamp": timestamp,
            "type": "PACKET",
            "direction": "rx",
            "time": current_time.strftime("%H:%M:%S"),
            "date": current_time.strftime("%d/%m/%Y"),
            "len": str(packet_len),
            "packet_type": packet_type,
            "route": route,
            "payload_len": payload_len,
            "raw": clean_raw_hex,
            "SNR": snr,
            "RSSI": rssi,
            "hash": packet_hash
        }
        
        # Add optional fields from payload if present (score, duration, etc.)
        if 'score' in payload:
            packet_data['score'] = str(payload['score'])
        if 'duration' in payload:
            packet_data['duration'] = str(payload['duration'])
        
        # Add path for route=D (matches original script)
        if route == "D" and packet_info.get('path'):
            packet_data["path"] = ",".join(packet_info['path'])
        
        return packet_data
    
    async def process_packet(self, raw_hex: str, payload: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> None:
        """Process a captured packet.
        
        Decodes the packet, formats it, writes to file, and publishes to MQTT.
        
        Args:
            raw_hex: Raw hex string of the packet.
            payload: Payload dictionary from the event.
            metadata: Optional metadata dictionary.
        """
        try:
            self.packet_count += 1
            
            # Extract packet information (decode may fail, but we still publish)
            packet_info = self.decode_packet(raw_hex, payload)
            
            # If decode failed, create minimal packet_info with defaults (matches original script)
            if not packet_info:
                if self.debug:
                    self.logger.debug(f"Packet {self.packet_count} decode failed, using defaults (raw_hex: {raw_hex[:50]}...)")
                # Try to calculate packet hash even if decode failed (extract payload_type from header if possible)
                packet_hash = '0000000000000000'
                payload_type_value = None
                try:
                    # Try to extract payload type from header for hash calculation
                    byte_data = bytes.fromhex(raw_hex.replace('0x', ''))
                    if len(byte_data) >= 1:
                        header = byte_data[0]
                        payload_type_value = (header >> 2) & 0x0F
                        packet_hash = calculate_packet_hash(raw_hex.replace('0x', ''), payload_type_value)
                except Exception:
                    pass  # Use default hash if calculation fails
                
                # Create minimal packet info with defaults (matches original script's format_packet_data)
                packet_info = {
                    'route_type': 'UNKNOWN',
                    'payload_type': 'UNKNOWN',
                    'payload_type_value': payload_type_value or 0,
                    'payload_version': 0,
                    'path_len': 0,
                    'path_hex': '',
                    'path': [],
                    'payload_hex': raw_hex.replace('0x', ''),
                    'payload_bytes': len(raw_hex.replace('0x', '')) // 2,
                    'raw_hex': raw_hex.replace('0x', ''),
                    'packet_hash': packet_hash,
                    'has_transport_codes': False,
                    'transport_codes': None
                }
            
            # Format packet data to match original script's format
            formatted_packet = self._format_packet_data(raw_hex, packet_info, payload, metadata)
            
            # Write to file
            if self.output_handle:
                self.output_handle.write(json.dumps(formatted_packet, default=str) + '\n')
                self.output_handle.flush()
            
            # Publish to MQTT if enabled
            # The publish function will check per-broker connection status
            publish_metrics = {"attempted": 0, "succeeded": 0}
            if self.mqtt_enabled:
                if self.debug:
                    self.logger.debug(f"Calling publish_packet_mqtt for packet {self.packet_count}")
                publish_metrics = await self.publish_packet_mqtt(formatted_packet)
            
            # Log DEBUG level for each packet (verbose; use INFO only for service lifecycle)
            self.logger.debug(f"📦 Captured packet #{self.packet_count}: {formatted_packet['route']} type {formatted_packet['packet_type']}, {formatted_packet['len']} bytes, SNR: {formatted_packet['SNR']}, RSSI: {formatted_packet['RSSI']}, hash: {formatted_packet['hash']} (MQTT: {publish_metrics['succeeded']}/{publish_metrics['attempted']})")
            
            # Output full packet data structure in debug mode only (matches original script)
            if self.debug:
                self.logger.debug("📋 Full packet data structure:")
                self.logger.debug(json.dumps(formatted_packet, indent=2))
            
        except Exception as e:
            self.logger.error(f"Error processing packet: {e}")
    
    def decode_packet(self, raw_hex: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Decode a MeshCore packet - matches original packet_capture.py functionality.
        
        Args:
            raw_hex: Raw hex string of the packet.
            payload: Payload dictionary from the event (unused in this method but kept for compatibility).
            
        Returns:
            Optional[Dict[str, Any]]: Decoded packet info, or None if decoding fails.
        """
        try:
            # Remove 0x prefix if present
            if raw_hex.startswith('0x'):
                raw_hex = raw_hex[2:]
            
            byte_data = bytes.fromhex(raw_hex)
            
            if len(byte_data) < 2:
                if self.debug:
                    self.logger.debug(f"Packet too short ({len(byte_data)} bytes), cannot decode")
                return None
            
            header = byte_data[0]
            
            # Extract route type
            route_type = RouteType(header & 0x03)
            has_transport = route_type in [RouteType.TRANSPORT_FLOOD, RouteType.TRANSPORT_DIRECT]
            
            # Extract transport codes if present
            transport_codes = None
            offset = 1
            if has_transport and len(byte_data) >= 5:
                transport_bytes = byte_data[1:5]
                transport_codes = {
                    'code1': int.from_bytes(transport_bytes[0:2], byteorder='little'),
                    'code2': int.from_bytes(transport_bytes[2:4], byteorder='little'),
                    'hex': transport_bytes.hex()
                }
                offset = 5
            
            if len(byte_data) <= offset:
                if self.debug:
                    self.logger.debug(f"Packet too short after transport codes ({len(byte_data)} bytes, offset {offset}), cannot decode")
                return None
            
            path_len = byte_data[offset]
            offset += 1
            
            if len(byte_data) < offset + path_len:
                if self.debug:
                    self.logger.debug(f"Packet too short for path ({len(byte_data)} bytes, need {offset + path_len}), cannot decode")
                return None
            
            # Extract path
            path_bytes = byte_data[offset:offset + path_len]
            offset += path_len
            
            # Convert path to list of hex node IDs (for compatibility)
            path_hex = path_bytes.hex()
            path_nodes = []
            for i in range(0, len(path_hex), 2):
                if i + 1 < len(path_hex):
                    path_nodes.append(path_hex[i:i+2])
            
            # Remaining data is payload
            packet_payload = byte_data[offset:]
            
            # Extract payload version
            payload_version = PayloadVersion((header >> 6) & 0x03)
            
            if payload_version != PayloadVersion.VER_1:
                if self.debug:
                    self.logger.debug(f"Unsupported payload version: {payload_version} (expected VER_1), skipping")
                return None
            
            # Extract payload type
            payload_type = PayloadType((header >> 2) & 0x0F)
            
            # Calculate packet hash (for tracking same message via different paths)
            packet_hash = calculate_packet_hash(raw_hex, payload_type.value)
            
            # Build packet info (matching original format)
            packet_info = {
                'header': f"0x{header:02x}",
                'route_type': route_type.name,
                'route_type_value': route_type.value,
                'payload_type': payload_type.name,
                'payload_type_value': payload_type.value,
                'payload_version': payload_version.value,
                'path_len': path_len,
                'path_hex': path_hex,
                'path': path_nodes,  # List of hex node IDs
                'payload_hex': packet_payload.hex(),
                'payload_bytes': len(packet_payload),
                'raw_hex': raw_hex,
                'packet_hash': packet_hash,
                'has_transport_codes': has_transport,
                'transport_codes': transport_codes
            }
            
            return packet_info
            
        except Exception as e:
            self.logger.debug(f"Error decoding packet: {e} (raw_hex: {raw_hex[:50]}...)")
            import traceback
            if self.debug:
                self.logger.debug(f"Decode error traceback: {traceback.format_exc()}")
            return None
    
    def _get_bot_name(self) -> str:
        """Get bot name from device or config.
        
        Returns:
            str: The name of the bot/device.
        """
        # Try to get name from device first
        if self.meshcore and hasattr(self.meshcore, 'self_info'):
            try:
                self_info = self.meshcore.self_info
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
        bot_name = self.bot.config.get('Bot', 'bot_name', fallback='MeshCoreBot')
        return bot_name
    
    def _require_mqtt(self) -> bool:
        """Check if MQTT is available and required.
        
        Returns:
            bool: True if MQTT requirements are met, False otherwise.
        """
        if mqtt is None:
            self.logger.warning(
                "MQTT support not available. Install paho-mqtt: "
                "pip install paho-mqtt"
            )
            return False
        return True
    
    async def connect_mqtt_brokers(self) -> None:
        """Connect to MQTT brokers.
        
        Establish connections to all configured MQTT brokers.
        """
        if not self._require_mqtt():
            return
        
        # Get bot name for client ID
        bot_name = self._get_bot_name()
        
        for broker_config in self.mqtt_brokers:
            if not broker_config.get('enabled', True):
                continue
                
            try:
                # Use configured client_id, or generate from bot name
                client_id = broker_config.get('client_id')
                if not client_id:
                    # Sanitize bot name for MQTT client ID (alphanumeric and hyphens only)
                    safe_name = ''.join(c if c.isalnum() or c == '-' else '-' for c in bot_name)
                    client_id = f"{safe_name}-packet-capture-{os.getpid()}"
                
                # Create client based on transport type
                transport = broker_config.get('transport', 'tcp').lower()
                if transport == 'websockets':
                    try:
                        client = mqtt.Client(
                            client_id=client_id,
                            transport='websockets'
                        )
                        # Set WebSocket path (must be done before connect)
                        ws_path = broker_config.get('websocket_path', '/mqtt')
                        client.ws_set_options(path=ws_path, headers=None)
                    except Exception as e:
                        self.logger.error(f"WebSockets transport not available: {e}")
                        continue
                else:
                    client = mqtt.Client(client_id=client_id)
                
                # Enable paho-mqtt's built-in automatic reconnection (matches original script)
                client.reconnect_delay_set(min_delay=1, max_delay=120)
                
                # Set TLS if enabled
                if broker_config.get('use_tls', False):
                    try:
                        import ssl
                        # For WebSockets with TLS (WSS), we need to set TLS on the client
                        # The TLS handshake happens during the WebSocket upgrade
                        client.tls_set(cert_reqs=ssl.CERT_NONE)  # Allow self-signed certs
                        if self.debug:
                            self.logger.debug(f"TLS enabled for {broker_config['host']} ({transport})")
                    except Exception as e:
                        self.logger.warning(f"TLS setup failed for {broker_config['host']}: {e}")
                
                # Set username/password if provided
                username = broker_config.get('username')
                password = broker_config.get('password')
                
                if broker_config.get('use_auth_token'):
                    # Use auth token with audience if specified
                    token_audience = broker_config.get('token_audience') or broker_config['host']
                    
                    # Get device's public key (from self_info) - this is what we use for username and JWT publicKey
                    # The owner_public_key is ONLY for the 'owner' field in the JWT payload
                    device_public_key_hex = None
                    if self.meshcore and hasattr(self.meshcore, 'self_info'):
                        try:
                            self_info = self.meshcore.self_info
                            if isinstance(self_info, dict):
                                device_public_key_hex = self_info.get('public_key', '')
                            elif hasattr(self_info, 'public_key'):
                                device_public_key_hex = self_info.public_key
                            
                            # Convert to hex string if bytes
                            if isinstance(device_public_key_hex, bytes):
                                device_public_key_hex = device_public_key_hex.hex()
                            elif isinstance(device_public_key_hex, bytearray):
                                device_public_key_hex = bytes(device_public_key_hex).hex()
                        except Exception as e:
                            self.logger.debug(f"Could not get public key from device: {e}")
                    
                    if not device_public_key_hex:
                        self.logger.warning(f"No device public key available for auth token (broker: {broker_config['host']})")
                        continue
                    
                    # Create auth token (tries on-device signing first if available)
                    use_device = (self.auth_token_method == 'device' and 
                                 self.meshcore and 
                                 self.meshcore.is_connected)
                    
                    # For Python signing, we still need meshcore_instance to fetch the private key
                    # The use_device flag only controls whether we try on-device signing first
                    meshcore_for_key_fetch = self.meshcore if self.meshcore and self.meshcore.is_connected else None
                    
                    try:
                        # Use v1_{device_public_key} format for username (device's actual key, not owner key)
                        if not username:
                            username = f"v1_{device_public_key_hex.upper()}"
                        
                        token = await create_auth_token_async(
                            meshcore_instance=meshcore_for_key_fetch,
                            public_key_hex=device_public_key_hex,  # Device's actual public key (for JWT publicKey field)
                            private_key_hex=self.private_key_hex,
                            iata=self.global_iata,
                            audience=token_audience,
                            owner_public_key=self.owner_public_key,  # Owner's key (only for 'owner' field in JWT)
                            owner_email=self.owner_email,
                            use_device=use_device
                        )
                        if token:
                            password = token
                            self.logger.debug(
                                f"Created auth token for {broker_config['host']} "
                                f"(username: {username}, valid for 24 hours) "
                                f"using {'device' if use_device else 'Python'} signing"
                            )
                        else:
                            self.logger.warning(f"Failed to create auth token for {broker_config['host']}")
                    except Exception as e:
                        self.logger.error(f"Error creating auth token for {broker_config['host']}: {e}")
                
                if username:
                    client.username_pw_set(username, password)
                
                # Setup callbacks
                def on_connect(client, userdata, flags, rc, properties=None):
                    if rc == 0:
                        self.logger.info(f"✓ Connected to MQTT broker: {broker_config['host']}:{broker_config['port']} ({transport})")
                        # Track connection per broker
                        for mqtt_info in self.mqtt_clients:
                            if mqtt_info['client'] == client:
                                mqtt_info['connected'] = True
                                break
                        # Set global connected flag if any broker is connected
                        self.mqtt_connected = any(m.get('connected', False) for m in self.mqtt_clients)
                    else:
                        # MQTT error codes: 0=success, 1=protocol, 2=client, 3=network, 4=transport, 5=auth
                        error_messages = {
                            1: "protocol version rejected",
                            2: "client identifier rejected",
                            3: "server unavailable",
                            4: "bad username or password",
                            5: "not authorized"
                        }
                        error_msg = error_messages.get(rc, f"unknown error ({rc})")
                        self.logger.error(
                            f"✗ Failed to connect to MQTT broker {broker_config['host']}: {rc} ({error_msg})"
                        )
                        # Mark this broker as disconnected
                        for mqtt_info in self.mqtt_clients:
                            if mqtt_info['client'] == client:
                                mqtt_info['connected'] = False
                                break
                        # Update global flag
                        self.mqtt_connected = any(m.get('connected', False) for m in self.mqtt_clients)
                
                def on_disconnect(client, userdata, rc, properties=None):
                    # Mark this broker as disconnected
                    for mqtt_info in self.mqtt_clients:
                        if mqtt_info['client'] == client:
                            mqtt_info['connected'] = False
                            if rc != 0:
                                self.logger.warning(f"Disconnected from MQTT broker {broker_config['host']} (rc={rc})")
                            else:
                                self.logger.debug(f"Disconnected from MQTT broker {broker_config['host']}")
                            break
                    # Update global flag
                    self.mqtt_connected = any(m.get('connected', False) for m in self.mqtt_clients)
                
                client.on_connect = on_connect
                client.on_disconnect = on_disconnect
                
                # Connect
                try:
                    host = broker_config['host']
                    port = broker_config['port']
                    
                    # Validate hostname (basic check)
                    if not host or not host.strip():
                        self.logger.error(f"Invalid MQTT broker hostname: '{host}'")
                        continue
                    
                    # Try to resolve hostname first (for better error messages)
                    try:
                        import socket
                        socket.gethostbyname(host)
                    except socket.gaierror as dns_error:
                        # Only log DNS errors at debug level, not as errors
                        if self.debug:
                            self.logger.debug(f"DNS resolution check for '{host}': {dns_error}")
                        # Continue anyway - connection attempt will show actual error
                    except Exception as resolve_error:
                        if self.debug:
                            self.logger.debug(f"Hostname resolution check for '{host}': {resolve_error}")
                    
                    # Add client to list BEFORE starting the loop, so callbacks can find it
                    self.mqtt_clients.append({
                        'client': client,
                        'config': broker_config,
                        'connected': False  # Track connection status per broker
                    })
                    
                    if transport == 'websockets':
                        # WebSocket path already set via ws_set_options above
                        ws_path = broker_config.get('websocket_path', '/mqtt')
                        self.logger.debug(f"Connecting to MQTT broker {host}:{port} via WebSockets (path: {ws_path}, TLS: {broker_config.get('use_tls', False)})")
                        # For WebSockets, connect without path parameter (path set via ws_set_options)
                        # Run connect in executor to avoid blocking the event loop
                        loop = asyncio.get_event_loop()
                        try:
                            await loop.run_in_executor(None, client.connect, host, port, 60)
                        except Exception as connect_error:
                            # Connection failed, but don't block - let loop_start handle retries
                            self.logger.debug(f"Initial connect() call failed (non-blocking): {connect_error}")
                    else:
                        self.logger.debug(f"Connecting to MQTT broker {host}:{port} via TCP (TLS: {broker_config.get('use_tls', False)})")
                        # Run connect in executor to avoid blocking the event loop
                        loop = asyncio.get_event_loop()
                        try:
                            await loop.run_in_executor(None, client.connect, host, port, 60)
                        except Exception as connect_error:
                            # Connection failed, but don't block - let loop_start handle retries
                            self.logger.debug(f"Initial connect() call failed (non-blocking): {connect_error}")
                    
                    # Start network loop (non-blocking)
                    client.loop_start()
                    
                    self.logger.info(f"MQTT connection initiated to {host}:{port} ({transport})")
                    
                    # Give connection a moment to establish (especially for WebSockets)
                    await asyncio.sleep(1)
                except Exception as e:
                    error_msg = str(e)
                    if "nodename nor servname provided" in error_msg or "Name or service not known" in error_msg:
                        # Log DNS errors at debug level, actual connection errors at warning
                        if self.debug:
                            self.logger.debug(f"DNS/Connection error for '{broker_config['host']}': {error_msg}")
                        else:
                            self.logger.warning(f"Could not connect to MQTT broker '{broker_config['host']}' (check network/DNS)")
                    elif "Connection refused" in error_msg:
                        if self.debug:
                            self.logger.debug(f"Connection refused by '{broker_config['host']}:{broker_config['port']}': {error_msg}")
                        else:
                            self.logger.warning(f"Connection refused by MQTT broker '{broker_config['host']}:{broker_config['port']}'")
                    else:
                        if self.debug:
                            self.logger.debug(f"MQTT connection error for '{broker_config['host']}': {error_msg}")
                        else:
                            self.logger.warning(f"Error connecting to MQTT broker '{broker_config['host']}'")
                    
            except Exception as e:
                self.logger.error(f"Error setting up MQTT broker: {e}")
        
        # Wait a bit for connections to establish
        await asyncio.sleep(2)
        
        # Log summary and publish initial status (matches original script)
        connected_count = sum(1 for m in self.mqtt_clients if m.get('connected', False))
        if connected_count > 0:
            self.logger.info(f"Connected to {connected_count} MQTT broker(s)")
            # Publish initial status with firmware version now that MQTT is connected (matches original script)
            await asyncio.sleep(1)  # Give MQTT connections a moment to stabilize
            await self.publish_status("online")
        else:
            self.logger.warning("MQTT enabled but no brokers connected")
    
    def _resolve_topic_template(self, template: str, packet_type: str = 'packet') -> Optional[str]:
        """Resolve topic template with placeholders.
        
        Args:
            template: Topic template string.
            packet_type: Type of packet ('packet' or 'status').
            
        Returns:
            Optional[str]: Resolved topic string, or None if template is empty.
        """
        if not template:
            return None
        
        # Get device's public key (NOT owner's key - owner key is only for JWT 'owner' field)
        # This matches the original script which uses self.device_public_key from self_info
        device_public_key = None
        if self.meshcore and hasattr(self.meshcore, 'self_info'):
            try:
                self_info = self.meshcore.self_info
                if isinstance(self_info, dict):
                    device_public_key = self_info.get('public_key', '')
                elif hasattr(self_info, 'public_key'):
                    device_public_key = self_info.public_key
                
                # Convert to hex string if bytes
                if isinstance(device_public_key, bytes):
                    device_public_key = device_public_key.hex()
                elif isinstance(device_public_key, bytearray):
                    device_public_key = bytes(device_public_key).hex()
            except Exception as e:
                self.logger.debug(f"Could not get public key from device: {e}")
        
        # Normalize to uppercase (remove 0x prefix if present)
        if device_public_key:
            device_public_key = device_public_key.replace('0x', '').replace(' ', '').upper()
        
        # Replace placeholders (matches original script's resolve_topic_template)
        topic = template.replace('{IATA}', self.global_iata.upper())
        topic = topic.replace('{iata}', self.global_iata.lower())
        topic = topic.replace('{PUBLIC_KEY}', device_public_key if device_public_key and device_public_key != 'Unknown' else 'DEVICE')
        topic = topic.replace('{public_key}', (device_public_key if device_public_key and device_public_key != 'Unknown' else 'DEVICE').lower())
        
        return topic
    
    async def publish_packet_mqtt(self, packet_info: Dict[str, Any]) -> Dict[str, int]:
        """Publish packet to MQTT - returns metrics dict with 'attempted' and 'succeeded' counts.
        
        Args:
            packet_info: Formatted packet dictionary.
            
        Returns:
            Dict[str, int]: Dictionary with 'attempted' and 'succeeded' counts.
        """
        # Always log when function is called (helps diagnose if it's not being invoked)
        self.logger.debug(f"publish_packet_mqtt called (packet {self.packet_count}, {len(self.mqtt_clients)} clients)")
        
        # Initialize metrics
        metrics = {"attempted": 0, "succeeded": 0}
        
        # Check per-broker connection status (more accurate than global flag)
        # Don't use early return - let the loop check each broker individually
        if not self.mqtt_clients:
            self.logger.debug("No MQTT clients configured, skipping publish")
            return metrics
        
        connected_count = sum(1 for m in self.mqtt_clients if m.get('connected', False))
        self.logger.debug(f"Publishing packet to MQTT ({connected_count}/{len(self.mqtt_clients)} brokers connected)")
        
        for mqtt_client_info in self.mqtt_clients:
            # Only publish to connected brokers
            if not mqtt_client_info.get('connected', False):
                self.logger.debug(f"Skipping MQTT broker {mqtt_client_info['config'].get('host', 'unknown')} (not connected)")
                continue
            try:
                client = mqtt_client_info['client']
                config = mqtt_client_info['config']
                
                # Determine topic
                topic = None
                if config.get('topic_packets'):
                    topic = self._resolve_topic_template(config['topic_packets'], 'packet')
                elif config.get('topic_prefix'):
                    topic = f"{config['topic_prefix']}/packet"
                else:
                    topic = 'meshcore/packets/packet'
                
                if not topic:
                    continue
                
                payload = json.dumps(packet_info, default=str)
                
                # Log topic and payload size for debugging
                self.logger.debug(f"Publishing to topic '{topic}' on {config['host']} (payload: {len(payload)} bytes)")
                
                # Count as attempted
                metrics["attempted"] += 1
                
                # Use QoS 0 (matches original script - prevents retry storms)
                result = client.publish(topic, payload, qos=0)
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    metrics["succeeded"] += 1
                    self.logger.debug(f"Published packet to MQTT topic '{topic}' on {config['host']} (qos=0)")
                else:
                    self.logger.warning(f"Failed to publish packet to MQTT topic '{topic}' on {config['host']}: {result.rc} ({mqtt.error_string(result.rc)})")
                    
            except Exception as e:
                self.logger.error(f"Error publishing packet to MQTT on {config.get('host', 'unknown')}: {e}")
        
        # Log summary
        if metrics["succeeded"] > 0:
            self.logger.debug(f"Published packet to {metrics['succeeded']} MQTT broker(s)")
        elif connected_count == 0:
            self.logger.debug(f"No MQTT brokers connected, packet not published")
        
        return metrics
    
    async def start_background_tasks(self) -> None:
        """Start background tasks.
        
        Initializes scheduler for stats refresh, JWT renewal, health checks,
        and MQTT reconnection monitor.
        """
        # Stats refresh scheduler (matches original script)
        if self.stats_status_enabled and self.stats_refresh_interval > 0:
            self.stats_update_task = asyncio.create_task(self.stats_refresh_scheduler())
            self.background_tasks.append(self.stats_update_task)
        
        # JWT renewal scheduler
        if self.jwt_renewal_interval > 0:
            task = asyncio.create_task(self.jwt_renewal_scheduler())
            self.background_tasks.append(task)
        
        # Health check
        if self.health_check_interval > 0:
            task = asyncio.create_task(self.health_check_loop())
            self.background_tasks.append(task)
        
        # MQTT reconnection monitor (proactive reconnection for failed/disconnected brokers)
        if self.mqtt_enabled:
            task = asyncio.create_task(self.mqtt_reconnection_monitor())
            self.background_tasks.append(task)
    
    async def stats_refresh_scheduler(self) -> None:
        """Periodically refresh stats and publish them via MQTT (matches original script).
        
        Fetches updated radio stats and triggers status publication.
        """
        if self.stats_refresh_interval <= 0 or not self.stats_status_enabled:
            return
        
        while not self.should_exit:
            try:
                # Only fetch stats when we're about to publish status
                if self.mqtt_enabled:
                    connected_count = sum(1 for m in self.mqtt_clients if m.get('connected', False))
                    if connected_count > 0:
                        await self.publish_status("online", refresh_stats=True)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.debug(f"Stats refresh error: {exc}")
            
            if await self._wait_with_shutdown(self.stats_refresh_interval):
                break
    
    async def _wait_with_shutdown(self, timeout: float) -> bool:
        """Wait for specified time but return immediately if shutdown is requested.
        
        Args:
            timeout: Time to wait in seconds.
            
        Returns:
            bool: True if shutdown requested, False if timeout completed.
        """
        if self.should_exit:
            return True
        await asyncio.sleep(timeout)
        return False
    
    def _load_client_version(self) -> str:
        """Load client version (matches original script).
        
        Returns:
            str: Version string (e.g., 'meshcore-bot/1.0.0-abcdef').
        """
        try:
            import os
            import subprocess
            script_dir = os.path.dirname(os.path.abspath(__file__))
            version_file = os.path.join(script_dir, '..', '..', '.version_info')
            
            # First try to load from .version_info file (created by installer)
            if os.path.exists(version_file):
                with open(version_file, 'r') as f:
                    version_data = json.load(f)
                    installer_ver = version_data.get('installer_version', 'unknown')
                    git_hash = version_data.get('git_hash', 'unknown')
                    return f"meshcore-bot/{installer_ver}-{git_hash}"
            
            # Fallback: try to get git information directly
            try:
                result = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], 
                                      cwd=os.path.dirname(script_dir), capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    git_hash = result.stdout.strip()
                    return f"meshcore-bot/dev-{git_hash}"
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
                pass
                
        except Exception as e:
            self.logger.debug(f"Could not load version info: {e}")
        
        # Final fallback
        return "meshcore-bot/unknown"
    
    async def get_firmware_info(self) -> Dict[str, str]:
        """Get firmware information from meshcore device (matches original script).
        
        Returns:
            Dict[str, str]: Dictionary containing 'model' and 'version'.
        """
        try:
            # During shutdown, always use cached info - don't query the device
            if self.should_exit:
                if self.cached_firmware_info:
                    self.logger.debug("Using cached firmware info (shutdown in progress)")
                    return self.cached_firmware_info
                else:
                    return {"model": "unknown", "version": "unknown"}
            
            # Always use cached info if available - firmware info doesn't change during runtime
            if self.cached_firmware_info:
                if self.debug:
                    self.logger.debug("Using cached firmware info")
                return self.cached_firmware_info
            
            # Only query if we don't have cached info
            if not self.meshcore or not self.meshcore.is_connected:
                return {"model": "unknown", "version": "unknown"}
            
            self.logger.debug("Querying device for firmware info...")
            # Use send_device_query() to get firmware version
            result = await self.meshcore.commands.send_device_query()
            
            if result is None:
                self.logger.debug("Device query failed")
                return {"model": "unknown", "version": "unknown"}
            
            if result.type == EventType.ERROR:
                self.logger.debug(f"Device query failed: {result}")
                return {"model": "unknown", "version": "unknown"}
            
            if result.payload:
                payload = result.payload
                self.logger.debug(f"Device query payload: {payload}")
                
                # Check firmware version format
                fw_ver = payload.get('fw ver', 0)
                self.logger.debug(f"Firmware version number: {fw_ver}")
                
                if fw_ver >= 3:
                    # For newer firmware versions (v3+)
                    model = payload.get('model', 'Unknown')
                    version = payload.get('ver', 'Unknown')
                    build_date = payload.get('fw_build', 'Unknown')
                    # Remove 'v' prefix from version if it already has one
                    if version.startswith('v'):
                        version = version[1:]
                    version_str = f"v{version} (Build: {build_date})"
                    self.logger.debug(f"New firmware format - Model: {model}, Version: {version_str}")
                    firmware_info = {"model": model, "version": version_str}
                    self.cached_firmware_info = firmware_info  # Cache the result
                    return firmware_info
                else:
                    # For older firmware versions
                    version_str = f"v{fw_ver}"
                    self.logger.debug(f"Old firmware format - Model: unknown, Version: {version_str}")
                    firmware_info = {"model": "unknown", "version": version_str}
                    self.cached_firmware_info = firmware_info  # Cache the result
                    return firmware_info
            
            self.logger.debug("No payload in device query result")
            return {"model": "unknown", "version": "unknown"}
            
        except Exception as e:
            self.logger.debug(f"Error getting firmware info: {e}")
            return {"model": "unknown", "version": "unknown"}
    
    def stats_commands_available(self) -> bool:
        """Detect whether the connected meshcore build exposes stats commands (matches original script).
        
        Returns:
            bool: True if stats commands are available.
        """
        if not self.meshcore or not hasattr(self.meshcore, "commands"):
            return False
        
        commands = self.meshcore.commands
        required = ["get_stats_core", "get_stats_radio"]
        available = all(callable(getattr(commands, attr, None)) for attr in required)
        state = "available" if available else "missing"
        if state != self.stats_capability_state:
            if available:
                self.logger.info("MeshCore stats commands detected - status messages will include device stats")
            else:
                self.logger.info("MeshCore stats commands not available - skipping stats in status messages")
            self.stats_capability_state = state
        self.stats_supported = available
        return available
    
    async def refresh_stats(self, force: bool = False) -> Optional[Dict[str, Any]]:
        """Fetch stats from the radio and cache them for status publishing (matches original script).
        
        Args:
            force: Force refresh even if cache is fresh.
            
        Returns:
            Optional[Dict[str, Any]]: Dictionary of stats or None if unavailable.
        """
        if not self.stats_status_enabled:
            if self.debug:
                self.logger.debug("Stats refresh skipped: stats_status_enabled is False")
            return None
        
        if not self.meshcore or not self.meshcore.is_connected:
            return None
        
        if self.stats_refresh_interval <= 0:
            if self.debug:
                self.logger.debug("Stats refresh skipped: stats_refresh_interval is 0 or negative")
            return None
        
        if not self.stats_commands_available():
            if self.debug:
                self.logger.debug("Stats refresh skipped: stats commands not available")
            return None
        
        now = time.time()
        if (
            not force
            and self.latest_stats
            and (now - self.last_stats_fetch) < max(60, self.stats_refresh_interval // 2)
        ):
            return dict(self.latest_stats)
        
        async with self.stats_fetch_lock:
            # Another coroutine may have completed the refresh while we waited
            if (
                not force
                and self.latest_stats
                and (time.time() - self.last_stats_fetch) < max(60, self.stats_refresh_interval // 2)
            ):
                return dict(self.latest_stats)
            
            stats_payload = {}
            try:
                core_result = await self.meshcore.commands.get_stats_core()
                if core_result and core_result.type == EventType.STATS_CORE and core_result.payload:
                    stats_payload.update(core_result.payload)
                elif core_result and core_result.type == EventType.ERROR:
                    self.logger.debug(f"Core stats unavailable: {core_result.payload}")
            except Exception as exc:
                self.logger.debug(f"Error fetching core stats: {exc}")
            
            try:
                radio_result = await self.meshcore.commands.get_stats_radio()
                if radio_result and radio_result.type == EventType.STATS_RADIO and radio_result.payload:
                    stats_payload.update(radio_result.payload)
                elif radio_result and radio_result.type == EventType.ERROR:
                    self.logger.debug(f"Radio stats unavailable: {radio_result.payload}")
            except Exception as exc:
                self.logger.debug(f"Error fetching radio stats: {exc}")
            
            if stats_payload:
                self.latest_stats = stats_payload
                self.last_stats_fetch = time.time()
                if self.debug:
                    self.logger.debug(f"Updated stats cache: {self.latest_stats}")
            elif self.debug:
                self.logger.debug("Stats refresh completed but returned no data")
        
        return dict(self.latest_stats) if self.latest_stats else None
    
    async def publish_status(self, status: str, refresh_stats: bool = True) -> None:
        """Publish status with additional information (matches original script exactly).
        
        Args:
            status: Status string (e.g., 'online', 'offline').
            refresh_stats: Whether to refresh stats before publishing.
        """
        firmware_info = await self.get_firmware_info()
        
        # Get device name and public key
        device_name = self._get_bot_name()
        if not device_name:
            device_name = "MeshCore Device"
        
        # Get device public key for origin_id
        device_public_key = None
        if self.meshcore and hasattr(self.meshcore, 'self_info'):
            try:
                self_info = self.meshcore.self_info
                if isinstance(self_info, dict):
                    device_public_key = self_info.get('public_key', '')
                elif hasattr(self_info, 'public_key'):
                    device_public_key = self_info.public_key
                
                # Convert to hex string if bytes
                if isinstance(device_public_key, bytes):
                    device_public_key = device_public_key.hex()
                elif isinstance(device_public_key, bytearray):
                    device_public_key = bytes(device_public_key).hex()
            except Exception:
                pass
        
        # Normalize origin_id to uppercase
        if device_public_key:
            device_public_key = device_public_key.replace('0x', '').replace(' ', '').upper()
        else:
            device_public_key = 'DEVICE'
        
        # Get radio info if available
        if not self.radio_info and self.meshcore and hasattr(self.meshcore, 'self_info'):
            try:
                self_info = self.meshcore.self_info
                radio_freq = self_info.get('radio_freq', 0) if isinstance(self_info, dict) else getattr(self_info, 'radio_freq', 0)
                radio_bw = self_info.get('radio_bw', 0) if isinstance(self_info, dict) else getattr(self_info, 'radio_bw', 0)
                radio_sf = self_info.get('radio_sf', 0) if isinstance(self_info, dict) else getattr(self_info, 'radio_sf', 0)
                radio_cr = self_info.get('radio_cr', 0) if isinstance(self_info, dict) else getattr(self_info, 'radio_cr', 0)
                self.radio_info = f"{radio_freq},{radio_bw},{radio_sf},{radio_cr}"
            except Exception:
                pass
        
        status_msg = {
            "status": status,
            "timestamp": datetime.now().isoformat(),
            "origin": device_name,
            "origin_id": device_public_key,
            "model": firmware_info.get('model', 'unknown'),
            "firmware_version": firmware_info.get('version', 'unknown'),
            "radio": self.radio_info or "unknown",
            "client_version": self._load_client_version()
        }
        
        # Attach stats (online status only) if supported and enabled
        if (
            status.lower() == "online"
            and self.stats_status_enabled
        ):
            stats_payload = None
            if refresh_stats:
                # Always force refresh stats right before publishing to ensure fresh data
                stats_payload = await self.refresh_stats(force=True)
                if not stats_payload:
                    self.logger.debug("Stats refresh returned no data - stats will not be included in status message")
            elif self.latest_stats:
                stats_payload = dict(self.latest_stats)
            
            if stats_payload:
                status_msg["stats"] = stats_payload
            elif self.debug:
                self.logger.debug("No stats payload available - status message will not include stats")
        
        # Publish status to all connected brokers
        for mqtt_client_info in self.mqtt_clients:
            # Only publish to connected brokers
            if not mqtt_client_info.get('connected', False):
                continue
            try:
                client = mqtt_client_info['client']
                config = mqtt_client_info['config']
                
                # Determine topic
                topic = None
                if config.get('topic_status'):
                    topic = self._resolve_topic_template(config['topic_status'], 'status')
                elif config.get('topic_prefix'):
                    topic = f"{config['topic_prefix']}/status"
                else:
                    topic = 'meshcore/status'
                
                if not topic:
                    continue
                
                payload = json.dumps(status_msg, default=str)
                
                # Use QoS 0 with retain=True for status (matches original script)
                result = client.publish(topic, payload, qos=0, retain=True)
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    if self.debug:
                        self.logger.debug(f"Published status: {status}")
                else:
                    self.logger.warning(f"Failed to publish status to MQTT topic '{topic}': {result.rc}")
                    
            except Exception as e:
                self.logger.error(f"Error publishing status to MQTT: {e}")
    
    async def jwt_renewal_scheduler(self) -> None:
        """Background task to proactively renew JWT tokens before expiration.

        Renews auth tokens for all MQTT brokers that use token authentication.
        Runs every jwt_renewal_interval seconds (default: 12 hours).
        Tokens are valid for 24 hours, so this provides a 12-hour buffer.
        """
        if self.jwt_renewal_interval <= 0:
            return

        while not self.should_exit:
            try:
                await asyncio.sleep(self.jwt_renewal_interval)

                if self.should_exit:
                    break

                # Renew tokens for all MQTT brokers that use auth tokens
                for mqtt_client_info in self.mqtt_clients:
                    config = mqtt_client_info['config']
                    client = mqtt_client_info['client']

                    # Only renew for brokers that use auth tokens
                    if not config.get('use_auth_token'):
                        continue

                    try:
                        broker_host = config.get('host', 'unknown')
                        self.logger.debug(f"Renewing auth token for MQTT broker {broker_host}...")

                        # Get device's public key
                        device_public_key_hex = None
                        if self.meshcore and hasattr(self.meshcore, 'self_info'):
                            try:
                                self_info = self.meshcore.self_info
                                if isinstance(self_info, dict):
                                    device_public_key_hex = self_info.get('public_key', '')
                                elif hasattr(self_info, 'public_key'):
                                    device_public_key_hex = self_info.public_key

                                # Convert to hex string if bytes
                                if isinstance(device_public_key_hex, bytes):
                                    device_public_key_hex = device_public_key_hex.hex()
                                elif isinstance(device_public_key_hex, bytearray):
                                    device_public_key_hex = bytes(device_public_key_hex).hex()
                            except Exception as e:
                                self.logger.debug(f"Could not get public key from device: {e}")

                        if not device_public_key_hex:
                            self.logger.warning(f"No device public key available for token renewal (broker: {broker_host})")
                            continue

                        # Create new auth token
                        token_audience = config.get('token_audience') or broker_host
                        username = f"v1_{device_public_key_hex.upper()}"

                        use_device = (self.auth_token_method == 'device' and
                                     self.meshcore and
                                     self.meshcore.is_connected)
                        meshcore_for_key_fetch = self.meshcore if self.meshcore and self.meshcore.is_connected else None

                        token = await create_auth_token_async(
                            meshcore_instance=meshcore_for_key_fetch,
                            public_key_hex=device_public_key_hex,
                            private_key_hex=self.private_key_hex,
                            iata=self.global_iata,
                            audience=token_audience,
                            owner_public_key=self.owner_public_key,
                            owner_email=self.owner_email,
                            use_device=use_device
                        )

                        if token:
                            # Update client credentials with new token
                            client.username_pw_set(username, token)
                            self.logger.info(f"✓ Renewed auth token for MQTT broker {broker_host} (valid for 24 hours)")
                        else:
                            self.logger.warning(f"Failed to renew auth token for MQTT broker {broker_host}")

                    except Exception as e:
                        self.logger.error(f"Error renewing token for MQTT broker {config.get('host', 'unknown')}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in JWT renewal scheduler: {e}")
                await asyncio.sleep(60)
    
    async def health_check_loop(self) -> None:
        """Background task for health checks.
        
        Monitors connection status and warns on failures.
        """
        if self.health_check_interval <= 0:
            return
        
        while not self.should_exit:
            try:
                await asyncio.sleep(self.health_check_interval)
                
                if not self.meshcore or not self.meshcore.is_connected:
                    self.health_check_failure_count += 1
                    if self.health_check_failure_count >= self.health_check_grace_period:
                        self.logger.warning("Health check failed - connection lost")
                else:
                    self.health_check_failure_count = 0
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in health check loop: {e}")
                await asyncio.sleep(60)
    
    async def mqtt_reconnection_monitor(self) -> None:
        """Proactive MQTT reconnection monitor - checks and reconnects disconnected brokers.
        
        Periodically checks connectivity of all configured MQTT brokers and attempts
        reconnection if disconnected.
        """
        if not self.mqtt_enabled:
            return
        
        # Reconnection check interval (check every 30 seconds)
        check_interval = 30
        
        while not self.should_exit:
            try:
                await asyncio.sleep(check_interval)
                
                if not self.mqtt_clients:
                    continue
                
                # Check each broker's connection status
                for mqtt_client_info in self.mqtt_clients:
                    client = mqtt_client_info['client']
                    config = mqtt_client_info['config']
                    broker_host = config.get('host', 'unknown')
                    
                    # Check if client is connected
                    if not client.is_connected():
                        # Client is disconnected - attempt reconnection
                        try:
                            self.logger.info(f"MQTT broker {broker_host} is disconnected, attempting reconnection...")
                            
                            # If using auth tokens, try to renew the token before reconnecting
                            if config.get('use_auth_token'):
                                # Get device's public key for username
                                device_public_key_hex = None
                                if self.meshcore and hasattr(self.meshcore, 'self_info'):
                                    try:
                                        self_info = self.meshcore.self_info
                                        if isinstance(self_info, dict):
                                            device_public_key_hex = self_info.get('public_key', '')
                                        elif hasattr(self_info, 'public_key'):
                                            device_public_key_hex = self_info.public_key
                                        
                                        # Convert to hex string if bytes
                                        if isinstance(device_public_key_hex, bytes):
                                            device_public_key_hex = device_public_key_hex.hex()
                                        elif isinstance(device_public_key_hex, bytearray):
                                            device_public_key_hex = bytes(device_public_key_hex).hex()
                                    except Exception:
                                        pass
                                
                                if device_public_key_hex:
                                    # Create new auth token
                                    token_audience = config.get('token_audience') or broker_host
                                    username = f"v1_{device_public_key_hex.upper()}"
                                    
                                    use_device = (self.auth_token_method == 'device' and 
                                                 self.meshcore and 
                                                 self.meshcore.is_connected)
                                    meshcore_for_key_fetch = self.meshcore if self.meshcore and self.meshcore.is_connected else None
                                    
                                    try:
                                        token = await create_auth_token_async(
                                            meshcore_instance=meshcore_for_key_fetch,
                                            public_key_hex=device_public_key_hex,
                                            private_key_hex=self.private_key_hex,
                                            iata=self.global_iata,
                                            audience=token_audience,
                                            owner_public_key=self.owner_public_key,
                                            owner_email=self.owner_email,
                                            use_device=use_device
                                        )
                                        if token:
                                            # Update credentials
                                            client.username_pw_set(username, token)
                                            self.logger.debug(f"Renewed auth token for {broker_host} before reconnection")
                                    except Exception as e:
                                        self.logger.debug(f"Error renewing auth token for {broker_host}: {e}")
                            
                            # Attempt reconnection (non-blocking to avoid blocking event loop)
                            host = config['host']
                            port = config['port']
                            loop = asyncio.get_event_loop()
                            try:
                                await loop.run_in_executor(None, client.reconnect)
                            except Exception as reconnect_error:
                                # Reconnection failed, but don't block - will retry on next cycle
                                self.logger.debug(f"Reconnect() call failed (non-blocking): {reconnect_error}")
                            
                            # Give it a moment to connect
                            await asyncio.sleep(2)
                            
                            # Check if reconnection succeeded
                            if client.is_connected():
                                self.logger.info(f"✓ Successfully reconnected to MQTT broker {broker_host}")
                                mqtt_client_info['connected'] = True
                                # Update global flag
                                self.mqtt_connected = any(m.get('connected', False) for m in self.mqtt_clients)
                            else:
                                if self.debug:
                                    self.logger.debug(f"Reconnection attempt to {broker_host} still in progress or failed")
                                
                        except Exception as e:
                            self.logger.debug(f"Error attempting MQTT reconnection to {broker_host}: {e}")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in MQTT reconnection monitor: {e}")
                await asyncio.sleep(60)

