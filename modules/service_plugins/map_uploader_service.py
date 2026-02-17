#!/usr/bin/env python3
"""
Map Uploader Service for MeshCore Bot
Uploads node adverts to map.meshcore.dev
Adapted from map.meshcore.dev-uploader Node.js implementation
"""

import asyncio
import json
import logging
import hashlib
import time
import copy
from typing import Optional, Dict, Any

# Import meshcore
import meshcore
from meshcore import EventType

# Import bot's enums
from ..enums import AdvertFlags, PayloadType

# Import HTTP client
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None
    AIOHTTP_AVAILABLE = False

# Import cryptography for signature verification
try:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False
    ed25519 = None  # type: ignore

# Import private key utilities
from .packet_capture_utils import (
    read_private_key_file,
    hex_to_bytes,
    bytes_to_hex
)

# Import base service
from .base_service import BaseServicePlugin

# Import utilities
from ..utils import resolve_path


class MapUploaderService(BaseServicePlugin):
    """Map uploader service.
    
    Uploads node adverts relative to the MeshCore network to map.meshcore.dev.
    Listens for ADVERT packets and uploads them to the centralized map service.
    Handles signing of data using the device's private key to ensure authenticity.
    """
    
    config_section = 'MapUploader'  # Explicit config section
    description = "Uploads node adverts to map.meshcore.dev"
    
    def __init__(self, bot: Any):
        """Initialize map uploader service.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        
        # Setup logging
        self.logger = logging.getLogger('MapUploaderService')
        self.logger.setLevel(bot.logger.level)
        
        # Clear any existing handlers to prevent duplicates
        self.logger.handlers.clear()
        
        # Use the same formatter as the bot (colored if enabled)
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
                bot_formatter = logging.Formatter(
                    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S'
                )
        
        # Add console handler with bot's formatter
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(bot_formatter)
        self.logger.addHandler(console_handler)
        
        # Also add file handler to write to the same log file as the bot (skip if no [Logging] section)
        log_file = (bot.config.get('Logging', 'log_file', fallback='meshcore_bot.log')
                    if bot.config.has_section('Logging') else '')
        if log_file:
            # Resolve log file path (relative paths resolved from bot root, absolute paths used as-is)
            log_file = resolve_path(log_file, bot.bot_root)
            
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(bot_formatter)
            self.logger.addHandler(file_handler)
        
        # Prevent propagation to root logger
        self.logger.propagate = False
        
        # Load configuration
        self._load_config()
        
        # Connection state
        self.connected = False
        
        # Track seen adverts: {pubkey: last_timestamp}
        # This prevents duplicate uploads and replay attacks
        # We'll periodically clean old entries to prevent unbounded growth
        self.seen_adverts: Dict[str, int] = {}
        
        # Device keys and info
        self.private_key_hex: Optional[str] = None
        self.public_key_hex: Optional[str] = None
        self.radio_params: Dict[str, Any] = {}
        
        # HTTP session
        # HTTP session
        self.http_session: Optional[aiohttp.ClientSession] = None  # type: ignore
        
        # Event subscriptions
        self.event_subscriptions = []
        
        # Exit flag
        self.should_exit = False
        
        # Cleanup tracking
        self._last_cleanup_time = 0
        self._cleanup_interval = 3600  # Clean up every hour
        
        self.logger.info("Map uploader service initialized")
    
    def _load_config(self) -> None:
        """Load configuration from bot's config."""
        config = self.bot.config
        
        # Check if enabled
        self.enabled = config.getboolean('MapUploader', 'enabled', fallback=False)
        
        # API URL
        self.api_url = config.get(
            'MapUploader', 
            'api_url', 
            fallback='https://map.meshcore.dev/api/v1/uploader/node'
        )
        
        # Private key path (optional, will fetch from device if not provided)
        self.private_key_path = config.get('MapUploader', 'private_key_path', fallback=None)
        if self.private_key_path:
            self.private_key_hex = read_private_key_file(self.private_key_path)
            if not self.private_key_hex:
                self.logger.warning(f"Could not load private key from {self.private_key_path}")
        
        # Minimum time between re-uploads (seconds)
        self.min_reupload_interval = config.getint('MapUploader', 'min_reupload_interval', fallback=3600)
        
        # Verbose logging
        self.verbose = config.getboolean('MapUploader', 'verbose', fallback=False)
    
    @property
    def meshcore(self) -> Any:
        """Get meshcore connection from bot (always current).
        
        Returns:
            Any: The meshcore connection object or None.
        """
        return self.bot.meshcore if self.bot else None
    
    async def start(self) -> None:
        """Start the map uploader service.
        
        Initializes connections, fetches device keys, and registers event handlers.
        Checks for required dependencies (aiohttp, cryptography) before starting.
        """
        if not self.enabled:
            self.logger.info("Map uploader service is disabled")
            return
        
        # Check dependencies
        if not AIOHTTP_AVAILABLE:
            self.logger.error("aiohttp is required for map uploader service. Install with: pip install aiohttp")
            return
        
        if not CRYPTOGRAPHY_AVAILABLE:
            self.logger.error("cryptography is required for signature verification. Install with: pip install cryptography")
            return
        
        # Wait for bot to be connected
        max_wait = 30  # seconds
        wait_time = 0
        while (not self.bot.connected or not self.meshcore) and wait_time < max_wait:
            await asyncio.sleep(0.5)
            wait_time += 0.5
        
        if not self.bot.connected or not self.meshcore:
            self.logger.warning("Bot not connected after waiting, cannot start map uploader")
            return
        
        self.logger.info("Starting map uploader service...")
        
        # Fetch device info and private key
        await self._fetch_device_info()
        await self._fetch_private_key()
        
        if not self.private_key_hex:
            self.logger.error("Could not obtain private key. Map uploader cannot sign uploads.")
            return
        
        if not self.public_key_hex:
            self.logger.error("Could not obtain public key. Map uploader cannot sign uploads.")
            return
        
        # Create HTTP session
        self.http_session = aiohttp.ClientSession()  # type: ignore
        
        # Setup event handlers
        await self._setup_event_handlers()
        
        self.connected = True
        self._running = True
        self.logger.info("Map uploader service started")
    
    async def stop(self) -> None:
        """Stop the map uploader service.
        
        Closes connections to the map service and the bot's meshcore.
        Cleans up resources and event subscriptions.
        """
        self.logger.info("Stopping map uploader service...")
        
        self.should_exit = True
        self._running = False
        self.connected = False
        
        # Clean up event subscriptions
        self._cleanup_event_subscriptions()
        
        # Close HTTP session
        if self.http_session:
            await self.http_session.close()
            self.http_session = None
        
        # Clear seen_adverts to free memory
        self.seen_adverts.clear()
        
        # Close file handlers
        for handler in self.logger.handlers[:]:
            if isinstance(handler, logging.FileHandler):
                handler.close()
                self.logger.removeHandler(handler)
        
        self.logger.info("Map uploader service stopped")
    
    async def _fetch_private_key(self) -> None:
        """Fetch private key from device if not already loaded.
        
        Attempts to read the private key from the connected MeshCore device
        for signing map uploads. This is required for valid uploads.
        """
        if self.private_key_hex:
            self.logger.debug("Private key already loaded from file")
            return
        
        if not self.meshcore or not self.meshcore.is_connected:
            self.logger.warning("Cannot fetch private key: not connected")
            return
        
        try:
            from .packet_capture_utils import _fetch_private_key_from_device
            
            self.logger.info("Fetching private key from device...")
            self.private_key_hex = await _fetch_private_key_from_device(self.meshcore)
            
            if self.private_key_hex:
                self.logger.info("✓ Successfully fetched private key from device")
            else:
                self.logger.warning("Could not fetch private key from device (may be disabled)")
        except Exception as e:
            self.logger.error(f"Error fetching private key from device: {e}")
    
    async def _fetch_device_info(self) -> None:
        """Fetch device info (public key and radio parameters).
        
        Retrieve public key and LoRa radio settings (frequency, coding rate, etc.)
        from the device self_info. These parameters are sent with map uploads.
        """
        if not self.meshcore or not self.meshcore.is_connected:
            self.logger.warning("Cannot fetch device info: not connected")
            return
        
        try:
            # Get self info from meshcore.self_info (it's a property, not a method)
            if not hasattr(self.meshcore, 'self_info') or not self.meshcore.self_info:
                self.logger.warning("Device self_info not available")
                # Use 0s to indicate unknown values
                self.radio_params = {
                    'freq': 0,
                    'cr': 0,
                    'sf': 0,
                    'bw': 0
                }
                return
            
            self_info = self.meshcore.self_info
            
            # Extract public key (handle both dict and object formats)
            if isinstance(self_info, dict):
                public_key = self_info.get('public_key', '')
            elif hasattr(self_info, 'public_key'):
                public_key = self_info.public_key
            else:
                public_key = None
            
            if public_key:
                if isinstance(public_key, bytes):
                    self.public_key_hex = bytes_to_hex(public_key)
                elif isinstance(public_key, str):
                    self.public_key_hex = public_key.replace('0x', '').replace(' ', '').lower()
                else:
                    self.public_key_hex = str(public_key)
                
                self.logger.debug(f"Got public key: {self.public_key_hex[:16]}...")
            else:
                self.logger.warning("Public key not found in self_info")
            
            # Extract radio parameters (handle both dict and object formats)
            # Use 0 as fallback to indicate unknown values
            if isinstance(self_info, dict):
                self.radio_params = {
                    'freq': self_info.get('radio_freq', self_info.get('freq', 0)),
                    'cr': self_info.get('radio_cr', self_info.get('cr', 0)),
                    'sf': self_info.get('radio_sf', self_info.get('sf', 0)),
                    'bw': self_info.get('radio_bw', self_info.get('bw', 0))
                }
            else:
                # Object format
                self.radio_params = {
                    'freq': getattr(self_info, 'radio_freq', getattr(self_info, 'freq', 0)),
                    'cr': getattr(self_info, 'radio_cr', getattr(self_info, 'cr', 0)),
                    'sf': getattr(self_info, 'radio_sf', getattr(self_info, 'sf', 0)),
                    'bw': getattr(self_info, 'radio_bw', getattr(self_info, 'bw', 0))
                }
            
            self.logger.debug(f"Radio params: {json.dumps(self.radio_params, indent=2)}")
        except Exception as e:
            self.logger.error(f"Error fetching device info: {e}", exc_info=True)
            # Use 0s to indicate unknown values
            self.radio_params = {
                'freq': 0,
                'cr': 0,
                'sf': 0,
                'bw': 0
            }
    
    async def _setup_event_handlers(self) -> None:
        """Setup event handlers for packet capture.
        
        Subscribes to RX_LOG_DATA events to intercept packets for upload.
        """
        if not self.meshcore:
            return
        
        # Handle RX log data
        async def on_rx_log_data(event, metadata=None):
            await self._handle_rx_log_data(event, metadata)
        
        # Subscribe to events
        self.meshcore.subscribe(EventType.RX_LOG_DATA, on_rx_log_data)
        
        self.event_subscriptions = [
            (EventType.RX_LOG_DATA, on_rx_log_data)
        ]
        
        self.logger.info("Map uploader event handlers registered")
    
    def _cleanup_event_subscriptions(self) -> None:
        """Clean up event subscriptions.
        
        Clears the list of tracked subscriptions. The actual unsubscription
        is handled by the meshcore library when the client disconnects,
        but this clears our local tracking.
        """
        # Note: meshcore library handles subscription cleanup automatically
        self.event_subscriptions = []
    
    async def _cleanup_old_seen_adverts(self, current_timestamp: int) -> None:
        """Clean up old entries from seen_adverts to prevent unbounded memory growth.
        
        Args:
            current_timestamp: The current timestamp from the latest packet.
        """
        current_time = time.time()
        
        # Only cleanup periodically (not on every packet)
        if current_time - self._last_cleanup_time < self._cleanup_interval:
            return
        
        self._last_cleanup_time = current_time
        
        # Remove entries older than min_reupload_interval * 2
        # This keeps recent entries but removes very old ones
        cutoff_timestamp = current_timestamp - (self.min_reupload_interval * 2)
        
        # Count entries before cleanup
        initial_count = len(self.seen_adverts)
        
        # Remove old entries
        keys_to_remove = [
            pubkey for pubkey, last_ts in self.seen_adverts.items()
            if last_ts < cutoff_timestamp
        ]
        
        for pubkey in keys_to_remove:
            del self.seen_adverts[pubkey]
        
        removed_count = len(keys_to_remove)
        
        if removed_count > 0:
            self.logger.debug(
                f"Cleaned up {removed_count} old seen_adverts entries "
                f"({initial_count} -> {len(self.seen_adverts)})"
            )
        
        # Safety limit: if dictionary still grows too large, use more aggressive cleanup
        if len(self.seen_adverts) > 10000:
            # Keep only the most recent 5000 entries
            sorted_entries = sorted(
                self.seen_adverts.items(),
                key=lambda x: x[1],
                reverse=True
            )
            self.seen_adverts = dict(sorted_entries[:5000])
            self.logger.warning(
                f"seen_adverts grew too large, trimmed to 5000 most recent entries"
            )
    
    async def _handle_rx_log_data(self, event: Any, metadata: Any = None) -> None:
        """Handle RX log data events.
        
        Args:
            event: The event object containing packet data.
            metadata: Optional metadata for the event.
        """
        try:
            # Copy payload immediately to avoid segfault if event is freed
            payload = copy.deepcopy(event.payload) if hasattr(event, 'payload') else None
            if payload is None:
                self.logger.warning("RX log data event has no payload")
                return
            
            # Get raw packet data
            raw_hex = None
            if 'payload' in payload and payload['payload']:
                raw_hex = payload['payload']
            elif 'raw_hex' in payload and payload['raw_hex']:
                raw_hex = payload['raw_hex'][4:]  # Skip first 2 bytes (4 hex chars)
            
            if not raw_hex:
                return
            
            # Process packet
            await self._process_packet(raw_hex)
        except Exception as e:
            self.logger.error(f"Error handling RX log data: {e}", exc_info=True)
    
    async def _process_packet(self, raw_hex: str) -> None:
        """Process a packet and upload if it's an ADVERT.
        
        Parses the raw packet hex, validates it is an ADVERT, checks for duplicates,
        verifies signature, and triggers upload if valid.
        
        Args:
            raw_hex: Hex string representation of the raw packet.
        """
        try:
            # Parse packet to check if it's an ADVERT
            byte_data = bytes.fromhex(raw_hex)
            
            if len(byte_data) < 2:
                return
            
            # Extract payload type from header
            header = byte_data[0]
            payload_type = (header >> 2) & 0x0F
            
            if payload_type != PayloadType.ADVERT.value:
                return  # Not an ADVERT packet
            
            # Extract payload (skip header, transport codes if present, path)
            route_type = header & 0x03
            has_transport = route_type in [0x00, 0x03]  # TRANSPORT_FLOOD or TRANSPORT_DIRECT
            
            offset = 1
            if has_transport:
                offset += 4  # Skip transport codes
            
            if len(byte_data) <= offset:
                return
            
            path_len = byte_data[offset]
            offset += 1
            
            # Skip path
            if path_len > 0 and len(byte_data) > offset + path_len:
                offset += path_len
            
            # Extract payload
            payload_bytes = byte_data[offset:]
            
            if len(payload_bytes) < 101:
                return  # Too short for ADVERT
            
            # Parse advert
            advert = self._parse_advert(payload_bytes)
            if not advert:
                return
            
            # Check if it's a CHAT advert (skip those)
            if advert.get('type') == 'CHAT':
                return
            
            # Verify signature
            if not await self._verify_advert_signature(advert, payload_bytes):
                self.logger.warning(f"Ignoring: signature verification failed for {advert.get('public_key', 'unknown')[:16]}...")
                return
            
            # Check for replay attacks
            pub_key = advert.get('public_key', '')
            timestamp = advert.get('advert_time', 0)
            
            if pub_key in self.seen_adverts:
                last_timestamp = self.seen_adverts[pub_key]
                
                # Check for replay (timestamp <= last seen)
                if timestamp <= last_timestamp:
                    if self.verbose:
                        self.logger.debug(f"Ignoring: possible replay attack for {pub_key[:16]}...")
                    return
                
                # Check if too soon to reupload
                if timestamp < last_timestamp + self.min_reupload_interval:
                    if self.verbose:
                        self.logger.debug(f"Ignoring: timestamp too new to reupload for {pub_key[:16]}...")
                    return
            
            # Skip adverts without coordinates or with any coordinate exactly 0.0 (invalid)
            lat = advert.get('lat')
            lon = advert.get('lon')
            if lat is None or lon is None or lat == 0.0 or lon == 0.0:
                if self.verbose:
                    self.logger.debug(f"Ignoring: advert missing or invalid coordinates (lat={lat}, lon={lon}) for {pub_key[:16]}...")
                return
            
            # Upload to map
            await self._upload_to_map(advert, raw_hex)
            
            # Update seen adverts
            self.seen_adverts[pub_key] = timestamp
            
            # Periodically clean up old entries to prevent unbounded memory growth
            await self._cleanup_old_seen_adverts(timestamp)
            
        except Exception as e:
            self.logger.error(f"Error processing packet: {e}", exc_info=True)
    
    def _parse_advert(self, payload: bytes) -> Optional[Dict[str, Any]]:
        """Parse advert payload.
        
        Args:
            payload: Binary payload of the packet.
            
        Returns:
            Optional[Dict[str, Any]]: Parsed advert data dictionary or None if invalid.
        """
        try:
            if len(payload) < 101:
                return None
            
            # Advert header
            pub_key = payload[0:32]
            timestamp = int.from_bytes(payload[32:36], "little")
            signature = payload[36:100]
            
            # App data
            app_data = payload[100:]
            if len(app_data) == 0:
                return None
            
            flags_byte = app_data[0]
            flags = AdvertFlags(flags_byte)
            
            # Extract type
            adv_type = flags_byte & 0x0F
            type_str = 'CHAT' if adv_type == AdvertFlags.ADV_TYPE_CHAT.value else \
                      'REPEATER' if adv_type == AdvertFlags.ADV_TYPE_REPEATER.value else \
                      'ROOM' if adv_type == AdvertFlags.ADV_TYPE_ROOM.value else \
                      'SENSOR' if adv_type == AdvertFlags.ADV_TYPE_SENSOR.value else \
                      f'Type{adv_type}'
            
            advert = {
                'public_key': pub_key.hex(),
                'advert_time': timestamp,
                'signature': signature.hex(),
                'type': type_str,
                'name': None,
                'lat': None,
                'lon': None
            }
            
            # Parse location data if present
            i = 1
            if AdvertFlags.ADV_LATLON_MASK in flags:
                if len(app_data) >= i + 8:
                    lat = int.from_bytes(app_data[i:i+4], 'little', signed=True)
                    lon = int.from_bytes(app_data[i+4:i+8], 'little', signed=True)
                    advert['lat'] = round(lat / 1000000.0, 6)
                    advert['lon'] = round(lon / 1000000.0, 6)
                    i += 8
            
            # Parse feat1 data if present
            if AdvertFlags.ADV_FEAT1_MASK in flags:
                i += 2
            
            # Parse feat2 data if present
            if AdvertFlags.ADV_FEAT2_MASK in flags:
                i += 2
            
            # Parse name if present
            if AdvertFlags.ADV_NAME_MASK in flags and len(app_data) >= i:
                try:
                    name = app_data[i:].decode('utf-8', errors='ignore').rstrip('\x00')
                    advert['name'] = name
                except Exception:
                    pass
            
            return advert
            
        except Exception as e:
            self.logger.error(f"Error parsing advert: {e}")
            return None
    
    async def _verify_advert_signature(self, advert: Dict[str, Any], payload: bytes) -> bool:
        """Verify advert signature using ed25519.
        
        Args:
            advert: The parsed advert dictionary containing the signature and public key.
            payload: The full binary payload used to verify the signature.
            
        Returns:
            bool: True if signature is valid, False otherwise.
        """
        if not CRYPTOGRAPHY_AVAILABLE:
            self.logger.error("Cryptography library not available, cannot verify signatures")
            return False  # Fail verification if library not available (security)
        
        try:
            # Extract signature and public key
            signature_hex = advert.get('signature', '')
            public_key_hex = advert.get('public_key', '')
            
            if not signature_hex or not public_key_hex:
                return False
            
            # Convert to bytes
            signature_bytes = hex_to_bytes(signature_hex)
            public_key_bytes = hex_to_bytes(public_key_hex)
            
            if len(signature_bytes) != 64:
                return False
            
            if len(public_key_bytes) != 32:
                return False
            
            # The signed data is: pub_key (32) + timestamp (4) + app_data
            # Signature covers: pub_key || timestamp || app_data
            signed_data = payload[0:32] + payload[32:36] + payload[100:]
            
            # Verify signature
            public_key = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
            public_key.verify(signature_bytes, signed_data)
            
            return True
            
        except Exception as e:
            if self.verbose:
                self.logger.debug(f"Signature verification failed: {e}")
            return False
    
    async def _upload_to_map(self, advert: Dict[str, Any], raw_packet_hex: str) -> None:
        """Upload advert to map.meshcore.dev.
        
        Signs the upload request and sends it via HTTP POST.
        
        Args:
            advert: Parsed advert data.
            raw_packet_hex: Raw hex of the packet to report.
        """
        if not self.http_session:
            self.logger.error("HTTP session not available")
            return
        
        if not self.private_key_hex or not self.public_key_hex:
            self.logger.error("Private or public key not available")
            return
        
        try:
            # Prepare upload data
            # Trust the firmware - use raw values directly without conversion
            upload_data = {
                'params': {
                    'freq': self.radio_params['freq'],
                    'cr': self.radio_params['cr'],
                    'sf': self.radio_params['sf'],
                    'bw': self.radio_params['bw']
                },
                'links': [f'meshcore://{raw_packet_hex}']
            }
            
            # Log upload data as JSON for debugging
            self.logger.debug(f"Upload data (before signing): {json.dumps(upload_data, indent=2)}")
            
            # Sign the data
            signed_data = self._sign_data(upload_data)
            
            # Add public key
            signed_data['publicKey'] = self.public_key_hex
            
            # Log signed data (without signature for brevity, but show structure)
            signed_data_for_log = {
                'data': signed_data['data'],
                'publicKey': signed_data['publicKey'],
                'signature': signed_data['signature'][:32] + '...' + signed_data['signature'][-32:] if len(signed_data['signature']) > 64 else signed_data['signature']
            }
            self.logger.debug(f"Signed data (for upload): {json.dumps(signed_data_for_log, indent=2)}")
            
            # Log upload
            node_info = {
                'pubKey': advert.get('public_key', '')[:16] + '...',
                'name': advert.get('name', 'unknown'),
                'ts': advert.get('advert_time', 0),
                'type': advert.get('type', 'unknown')
            }
            self.logger.info(f"Uploading {node_info}")
            
            # POST to API
            async with self.http_session.post(
                self.api_url,
                json=signed_data,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                try:
                    result = await response.json()
                except Exception as e:
                    # If response is not JSON, get text
                    error_text = await response.text()
                    self.logger.warning(f"Upload failed (status {response.status}): {error_text} (JSON parse error: {e})")
                    return
                
                # Check for errors in response (API may return 200 with error in JSON)
                if response.status == 200:
                    if 'error' in result:
                        self.logger.warning(f"Upload failed: {result.get('error', 'Unknown error')} (code: {result.get('code', 'unknown')})")
                    else:
                        self.logger.info(f"Upload successful: {result}")
                else:
                    # Handle non-200 status codes
                    if isinstance(result, dict):
                        error_text = result.get('error', 'Unknown error')
                    else:
                        error_text = str(result) if result else 'Unknown error'
                    self.logger.warning(f"Upload failed (status {response.status}): {error_text}")
        
        except asyncio.TimeoutError:
            self.logger.warning("Upload timeout")
        except Exception as e:
            self.logger.error(f"Error uploading to map: {e}", exc_info=True)
    
    def _sign_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Sign data using private key.
        
        Args:
            data: Dictionary of data to sign.
            
        Returns:
            Dict[str, Any]: Object containing original data (JSON string) and signature.
        """
        # Convert data to JSON
        json_str = json.dumps(data, separators=(',', ':'))
        
        # Hash the JSON
        data_hash = hashlib.sha256(json_str.encode('utf-8')).digest()
        
        # Sign the hash
        signature_hex = self._sign_hash(data_hash)
        
        return {
            'data': json_str,
            'signature': signature_hex
        }
    
    def _sign_hash(self, data_hash: bytes) -> str:
        """Sign a hash using ed25519 private key (orlp format).
        
        Args:
            data_hash: The SHA256 hash of the data to sign.
            
        Returns:
            str: Hex string of the signature.
            
        Raises:
            ImportError: If PyNaCl is required but missing.
            ValueError: If private key length is invalid.
        """
        try:
            # Convert private key to bytes
            private_key_bytes = hex_to_bytes(self.private_key_hex)
            public_key_bytes = hex_to_bytes(self.public_key_hex)
            
            # Handle orlp format (64 bytes) vs seed format (32 bytes)
            if len(private_key_bytes) == 64:
                # Orlp format: scalar (first 32) || prefix (last 32)
                # This format requires PyNaCl for proper signing
                scalar = private_key_bytes[:32]
                prefix = private_key_bytes[32:64]
                
                # Use orlp signing function (same as packet_capture_utils)
                # This matches the Node.js supercop implementation
                try:
                    from .packet_capture_utils import ed25519_sign_with_expanded_key
                    signature = ed25519_sign_with_expanded_key(
                        data_hash,
                        scalar,
                        prefix,
                        public_key_bytes
                    )
                except ImportError as e:
                    raise ImportError(
                        "PyNaCl is required for orlp format signing. "
                        "Install with: pip install pynacl"
                    ) from e
            elif len(private_key_bytes) == 32:
                # Seed format: use cryptography library
                private_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_key_bytes)
                signature = private_key.sign(data_hash)
            else:
                raise ValueError(f"Invalid private key length: {len(private_key_bytes)}")
            
            # Return as hex
            return bytes_to_hex(signature)
            
        except Exception as e:
            self.logger.error(f"Error signing data: {e}", exc_info=True)
            raise

