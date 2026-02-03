#!/usr/bin/env python3
"""
pyMC_core Connection Module

Wrapper for pyMC_core MeshNode with KISS TNC support.
Provides a compatible interface for meshcore-bot to use pyMC_core
instead of the meshcore package, enabling unlimited contact storage
in the database rather than being limited by radio firmware.
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field
from enum import IntEnum

# Import pyMC_core components
try:
    from pymc_core import LocalIdentity
    from pymc_core.node.node import MeshNode
    from pymc_core.hardware.kiss_serial_wrapper import KissSerialWrapper
    # Import payload type constants (not an enum in pymc_core)
    from pymc_core.protocol import constants as pymc_constants
    PYMC_AVAILABLE = True
except ImportError as e:
    PYMC_AVAILABLE = False
    LocalIdentity = None
    MeshNode = None
    KissSerialWrapper = None
    pymc_constants = None

# Import MeshCore KISS modem interface
try:
    from modules.kiss_modem import KISSModem
    KISS_MODEM_AVAILABLE = True
except ImportError:
    KISSModem = None
    KISS_MODEM_AVAILABLE = False

# Legacy: MeshTNC CLI mode wrapper (workaround for old KISS RX bug)
try:
    from modules.meshtnc_serial import MeshTNCSerial
    MESHTNC_SERIAL_AVAILABLE = True
except ImportError:
    MeshTNCSerial = None
    MESHTNC_SERIAL_AVAILABLE = False


class EventType(IntEnum):
    """Event types compatible with meshcore EventType for bot compatibility."""
    CONTACT_MSG_RECV = 1
    CHANNEL_MSG_RECV = 2
    RX_LOG_DATA = 3
    RAW_DATA = 4
    NEW_CONTACT = 5
    MSG_SENT = 6
    OK = 7
    ERROR = 8
    ADVERT_RECV = 9


@dataclass
class Event:
    """Event object compatible with meshcore events."""
    type: EventType
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


class KISSModemIdentity:
    """
    LocalIdentity-compatible wrapper for MeshCore KISS modem identity.

    The KISS modem stores its Ed25519 keypair in flash and provides
    crypto operations via the KISS protocol. This class wraps those
    operations to be compatible with pymc_core's LocalIdentity interface.
    """

    def __init__(self, modem, public_key: bytes):
        """
        Initialize with modem reference and cached public key.

        Args:
            modem: KISSModem instance
            public_key: 32-byte Ed25519 public key from modem
        """
        self._modem = modem
        self._public_key = public_key

    def get_public_key(self) -> bytes:
        """Get the 32-byte Ed25519 public key."""
        return self._public_key

    def sign(self, data: bytes) -> bytes:
        """Sign data using the modem's private key."""
        signature = self._modem.sign_data(data)
        if signature is None:
            raise Exception("Modem signing failed")
        return signature

    def compute_shared_secret(self, remote_public_key: bytes) -> bytes:
        """Compute ECDH shared secret with remote public key."""
        secret = self._modem.key_exchange(remote_public_key)
        if secret is None:
            raise Exception("Modem key exchange failed")
        return secret

    def get_private_key(self) -> bytes:
        """Not available: KISS modem keeps the private key in hardware and never exposes it.
        DM decryption is done in the raw packet path using modem key_exchange + decrypt_data.
        """
        raise NotImplementedError(
            "KISS modem does not expose private key; DMs are decrypted via modem key_exchange + decrypt_data"
        )


@dataclass
class ContactInfo:
    """Contact information stored in database."""
    public_key: str
    name: str
    role: str = "companion"
    device_type: str = "unknown"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    out_path: str = ""
    out_path_len: int = 0
    last_heard: Optional[float] = None

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-like access for compatibility."""
        return getattr(self, key, default)


class ContactBookAdapter:
    """
    Adapter to provide contact information from the bot's database to pymc_core.

    pymc_core's TextMessageHandler expects a contacts object with a 'contacts'
    attribute that is iterable and contains contact objects with 'public_key'.
    """

    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger("ContactBookAdapter")
        self._contact_cache: List[Any] = []
        self._cache_time = 0
        self._cache_ttl = 30  # Refresh cache every 30 seconds

    @property
    def contacts(self) -> List[Any]:
        """Get contacts list (required by pymc_core's dispatcher)."""
        return self._get_contacts()

    def _get_contacts(self) -> List[Any]:
        """Get all contacts from the database."""
        import time as _time

        # Check if cache is still valid
        now = _time.time()
        if now - self._cache_time < self._cache_ttl and self._contact_cache:
            return self._contact_cache

        contacts = []

        try:
            if not hasattr(self.bot, 'db_manager'):
                self.logger.debug("Database manager not available for contacts")
                return contacts

            # Query contacts from database
            query = """
                SELECT public_key, name, role, device_type
                FROM complete_contact_tracking
                WHERE public_key IS NOT NULL AND public_key != ''
            """

            rows = self.bot.db_manager.execute_query(query)

            for row in rows:
                # Create a simple object that pymc_core can use
                contact = type('Contact', (), {
                    'public_key': row.get('public_key', ''),
                    'name': row.get('name', ''),
                    'role': row.get('role', 'companion'),
                })()
                contacts.append(contact)

            self._contact_cache = contacts
            self._cache_time = now

            self.logger.debug(f"Loaded {len(contacts)} contacts from database")

        except Exception as e:
            self.logger.error(f"Error loading contacts: {e}")

        return contacts

    def get_contact_by_public_key(self, public_key: str):
        """Find a contact by public key."""
        for contact in self.contacts:
            if contact.public_key == public_key:
                return contact
        return None


class ChannelDatabaseAdapter:
    """
    Adapter to provide channel information from the bot's database to pymc_core.

    pymc_core's GroupTextHandler expects a channel_db with a get_channels() method
    that returns a list of dicts with 'name' and 'secret' keys.
    """

    # Well-known Public channel secret (from MeshCore spec)
    PUBLIC_CHANNEL_SECRET = "d8ee687c9be53be08d24a7f7aede4dac5de3168dea03c12e7b9c96c5511e807f"

    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger("ChannelDatabaseAdapter")
        self._channel_cache: List[Dict[str, Any]] = []
        self._cache_time = 0
        self._cache_ttl = 30  # Refresh cache every 30 seconds

        # Pre-load channels and log what we have
        self.logger.info("ChannelDatabaseAdapter initialized - loading channels...")
        channels = self.get_channels()
        self.logger.info(f"Initialized with {len(channels)} channels:")
        for ch in channels:
            ch_hash = self.get_channel_hash(ch['secret'])
            self.logger.info(f"  - {ch['name']}: hash=0x{ch_hash:02X}")

    @staticmethod
    def derive_hashtag_key(channel_name: str) -> str:
        """
        Derive channel key from a hashtag channel name.

        MeshCore uses the first 16 bytes of SHA256(channel_name) as the
        encryption key for hashtag channels.

        Args:
            channel_name: Channel name (e.g., "#howltest")

        Returns:
            Hex string of the 16-byte derived key (32 hex chars)
        """
        import hashlib
        # Hash the channel name, take first 16 bytes as the key
        full_hash = hashlib.sha256(channel_name.encode('utf-8')).digest()
        key_16 = full_hash[:16]
        return key_16.hex()

    @staticmethod
    def get_channel_hash(secret_hex: str) -> int:
        """
        Get the channel hash from a secret (first byte of SHA256(secret)).

        This matches pymc_core's _derive_channel_hash method.
        """
        import hashlib
        try:
            secret_bytes = bytes.fromhex(secret_hex)
        except ValueError:
            secret_bytes = secret_hex.encode('utf-8')
        return hashlib.sha256(secret_bytes).digest()[0]

    def get_channels(self) -> List[Dict[str, Any]]:
        """
        Get all channels with their secrets.

        Includes:
        - Public channel (well-known key)
        - Hashtag channels from config (derived keys)
        - Private channels from config (name:key pairs)
        - Custom channels from database

        Config format in [Channels] section:
            monitor_channels = #howltest, #mychannel
            private_channels = mynet:abc123..., othernet:def456...

        Returns:
            List of channel dicts with 'name' and 'secret' keys
        """
        import time as _time

        # Check if cache is still valid
        now = _time.time()
        if now - self._cache_time < self._cache_ttl and self._channel_cache:
            self.logger.debug(f"Returning cached {len(self._channel_cache)} channels")
            return self._channel_cache

        channels = []
        self.logger.info("Building channel list...")

        # Always include the Public channel (well-known secret)
        public_hash = self.get_channel_hash(self.PUBLIC_CHANNEL_SECRET)
        channels.append({
            'name': 'Public',
            'secret': self.PUBLIC_CHANNEL_SECRET,
            'idx': 0
        })
        self.logger.info(f"Added Public channel with hash 0x{public_hash:02X}")

        # Add channels from config
        try:
            if hasattr(self.bot, 'config'):
                # Hashtag channels (key derived from name)
                monitor_channels = self.bot.config.get('Channels', 'monitor_channels', fallback='')
                if monitor_channels:
                    for ch_name in monitor_channels.split(','):
                        ch_name = ch_name.strip()
                        if ch_name and not any(c['name'] == ch_name for c in channels):
                            # Derive key from channel name
                            derived_key = self.derive_hashtag_key(ch_name)
                            ch_hash = self.get_channel_hash(derived_key)
                            channels.append({
                                'name': ch_name,
                                'secret': derived_key,
                                'idx': len(channels)
                            })
                            self.logger.info(f"Added channel {ch_name} with hash 0x{ch_hash:02X}")

                # Private channels (explicit name:key pairs)
                private_channels = self.bot.config.get('Channels', 'private_channels', fallback='')
                if private_channels:
                    for pair in private_channels.split(','):
                        pair = pair.strip()
                        if ':' in pair:
                            ch_name, ch_key = pair.split(':', 1)
                            ch_name = ch_name.strip()
                            ch_key = ch_key.strip()
                            if ch_name and ch_key and not any(c['name'] == ch_name for c in channels):
                                ch_hash = self.get_channel_hash(ch_key)
                                channels.append({
                                    'name': ch_name,
                                    'secret': ch_key,
                                    'idx': len(channels)
                                })
                                self.logger.info(f"Added private channel {ch_name} with hash 0x{ch_hash:02X}")
        except Exception as e:
            self.logger.warning(f"Error loading channels from config: {e}")

        # Add channels from database (added via web viewer)
        try:
            if hasattr(self.bot, 'db_manager'):
                query = """
                    SELECT channel_idx, channel_name, channel_key_hex
                    FROM channels
                    WHERE channel_key_hex IS NOT NULL AND channel_key_hex != ''
                """

                rows = self.bot.db_manager.execute_query(query)

                for row in rows:
                    channel_name = row.get('channel_name', '')
                    channel_key_hex = row.get('channel_key_hex', '')

                    if channel_name and channel_key_hex:
                        # Don't duplicate if already added from config
                        if not any(c['name'] == channel_name for c in channels):
                            ch_hash = self.get_channel_hash(channel_key_hex)
                            channels.append({
                                'name': channel_name,
                                'secret': channel_key_hex,
                                'idx': row.get('channel_idx', 0)
                            })
                            self.logger.info(f"Added database channel {channel_name} with hash 0x{ch_hash:02X}")
        except Exception as e:
            self.logger.warning(f"Error loading channels from database: {e}")

        self._channel_cache = channels
        self._cache_time = now

        self.logger.info(f"Loaded {len(channels)} channels (Public + {len(channels)-1} configured)")

        return channels


class PyMCConnection:
    """
    Wrapper around pyMC_core MeshNode providing a meshcore-compatible interface.
    
    This class bridges the meshcore-bot's expected interface with pyMC_core,
    enabling the bot to use a KISS TNC radio (MeshTNC) for mesh communication
    while storing unlimited contacts in the SQLite database.
    """
    
    def __init__(self, bot, config: Dict[str, Any]):
        """
        Initialize the pyMC connection wrapper.
        
        Args:
            bot: The MeshCoreBot instance
            config: Configuration dictionary with connection settings
        """
        self.bot = bot
        self.config = config
        self.logger = logging.getLogger('PyMCConnection')
        
        # Connection state
        self._connected = False
        self._mesh_node: Optional[MeshNode] = None
        self._radio: Optional[KissSerialWrapper] = None
        self._identity: Optional[LocalIdentity] = None
        
        # Event subscribers
        self._subscribers: Dict[EventType, List[Callable]] = {
            event_type: [] for event_type in EventType
        }
        
        # Contact cache (loaded from database)
        self._contacts: Dict[str, ContactInfo] = {}

        # Self info (bot's identity)
        self._self_info: Dict[str, Any] = {}

        # Message deduplication cache: hash -> timestamp
        # Used to ignore duplicate messages received via different mesh paths
        self._seen_messages: Dict[str, float] = {}
        self._seen_messages_ttl = 30.0  # Ignore duplicates within 30 seconds

        # Commands interface
        self.commands = PyMCCommands(self)
    
    @property
    def is_connected(self) -> bool:
        """Check if connected to radio."""
        return self._connected and self._radio is not None
    
    @property
    def contacts(self) -> Dict[str, ContactInfo]:
        """Get contacts dictionary (loaded from database)."""
        return self._contacts
    
    @property
    def self_info(self) -> Dict[str, Any]:
        """Get bot's self info."""
        return self._self_info
    
    async def connect(self) -> bool:
        """
        Connect to MeshTNC radio via KISS serial.
        
        Returns:
            bool: True if connection successful
        """
        if not PYMC_AVAILABLE:
            self.logger.error("pymc-core package not installed. Install with: pip install pymc-core[radio]")
            return False
        
        try:
            # Get configuration
            serial_port = self.config.get('pymc_serial_port', '/dev/ttyUSB0')
            baudrate = self.config.get('pymc_baudrate', 115200)
            frequency = self.config.get('pymc_frequency', 869618000)  # Default EU frequency
            bandwidth = self.config.get('pymc_bandwidth', 62500)
            spreading_factor = self.config.get('pymc_spreading_factor', 8)
            coding_rate = self.config.get('pymc_coding_rate', 8)
            tx_power = self.config.get('pymc_tx_power', 22)
            sync_word = self.config.get('pymc_sync_word', 0x12)
            
            self.logger.info(f"Connecting to KISS modem via serial: {serial_port}")

            # Configure radio parameters
            radio_config = {
                "frequency": frequency,
                "bandwidth": bandwidth,
                "spreading_factor": spreading_factor,
                "coding_rate": coding_rate,
                "sync_word": sync_word,
                "tx_power": tx_power,
            }

            self.logger.info(f"Radio config: freq={frequency/1e6:.3f}MHz, BW={bandwidth/1000}kHz, SF={spreading_factor}")

            # Use MeshCore KISS modem (preferred) or fall back to legacy options
            if KISS_MODEM_AVAILABLE:
                self.logger.info("Using MeshCore KISS modem")
                self._radio = KISSModem(
                    port=serial_port,
                    baudrate=baudrate,
                    radio_config=radio_config,
                )
            elif MESHTNC_SERIAL_AVAILABLE:
                # Legacy: MeshTNC CLI mode (workaround for old KISS RX bug)
                self.logger.info("Using MeshTNCSerial (CLI/rxlog mode) - legacy fallback")
                self._radio = MeshTNCSerial(
                    port=serial_port,
                    baudrate=baudrate,
                    radio_config=radio_config,
                )
            else:
                self.logger.info("Using KissSerialWrapper (pymc_core KISS mode)")
                self._radio = KissSerialWrapper(
                    port=serial_port,
                    baudrate=baudrate,
                    radio_config=radio_config,
                    auto_configure=True
                )

            # Connect to radio
            if not self._radio.connect():
                self.logger.error(f"Failed to connect to KISS modem on {serial_port}")
                return False

            self.logger.info("Radio connected successfully")

            # Get or create identity based on modem type
            if KISS_MODEM_AVAILABLE and isinstance(self._radio, KISSModem):
                # MeshCore KISS modem - identity is stored on modem
                modem_pubkey = self._radio.get_identity()
                if modem_pubkey:
                    self.logger.info(f"Using modem identity: {modem_pubkey.hex()[:16]}...")
                    # Create a wrapper that uses the modem for crypto operations
                    self._identity = KISSModemIdentity(self._radio, modem_pubkey)
                else:
                    self.logger.error("Failed to get identity from modem")
                    return False
            else:
                # Non-KISS modem - load or create local identity
                from .pymc_identity import IdentityManager
                identity_manager = IdentityManager(self.bot)
                self._identity = await identity_manager.load_or_create_identity()

                if not self._identity:
                    self.logger.error("Failed to load or create identity")
                    return False

            # Set the event loop on the radio for thread-safe callbacks
            # This is needed because RX happens in a background thread
            if hasattr(self._radio, 'set_event_loop'):
                self._radio.set_event_loop(asyncio.get_running_loop())
                self.logger.info("Event loop set on radio for thread-safe callbacks")

            # Get bot name from config
            bot_name = self.config.get('bot_name', 'MeshCoreBot')

            # Create adapters for pymc_core
            self._channel_db = ChannelDatabaseAdapter(self.bot)
            self._contact_book = ContactBookAdapter(self.bot)

            # Create mesh node with channel database and contacts
            node_config = {
                "node": {"name": bot_name}
            }

            self._mesh_node = MeshNode(
                radio=self._radio,
                local_identity=self._identity,
                config=node_config,
                channel_db=self._channel_db,
                contacts=self._contact_book
            )

            # Setup event handlers BEFORE starting the node
            # This ensures callbacks are registered before packets arrive
            self._setup_event_handlers()

            # Start the mesh node dispatcher as a background task
            # (MeshNode.start() calls dispatcher.run_forever() which blocks)
            self._dispatcher_task = asyncio.create_task(self._mesh_node.start())
            self.logger.info("Mesh node dispatcher started as background task")

            # Load contacts from database
            await self._load_contacts_from_database()

            # Set self info
            pub_key = self._identity.get_public_key()
            self._self_info = {
                'name': bot_name,
                'public_key': pub_key.hex() if pub_key else '',
                'pubkey_prefix': pub_key[:6].hex() if pub_key else '',
            }
            
            self._connected = True
            self.logger.info(f"PyMC connection established. Bot identity: {self._self_info.get('pubkey_prefix', 'unknown')}")
            
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to connect: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False
    
    async def disconnect(self) -> None:
        """Disconnect from radio."""
        self._connected = False

        # Cancel the dispatcher task
        if hasattr(self, '_dispatcher_task') and self._dispatcher_task:
            if not self._dispatcher_task.done():
                self._dispatcher_task.cancel()
                try:
                    await self._dispatcher_task
                except asyncio.CancelledError:
                    pass
            self._dispatcher_task = None

        if self._mesh_node:
            try:
                self._mesh_node.stop()
            except Exception as e:
                self.logger.warning(f"Error stopping mesh node: {e}")
            self._mesh_node = None

        if self._radio:
            try:
                self._radio.disconnect()
            except Exception as e:
                self.logger.warning(f"Error disconnecting radio: {e}")
            self._radio = None

        self.logger.info("PyMC connection closed")
    
    def subscribe(self, event_type: EventType, callback: Callable) -> None:
        """
        Subscribe to an event type.
        
        Args:
            event_type: The event type to subscribe to
            callback: Async callback function to call when event occurs
        """
        if event_type in self._subscribers:
            self._subscribers[event_type].append(callback)
            self.logger.debug(f"Subscribed to event type: {event_type.name}")
    
    def unsubscribe(self, event_type: EventType, callback: Callable) -> None:
        """Unsubscribe from an event type."""
        if event_type in self._subscribers and callback in self._subscribers[event_type]:
            self._subscribers[event_type].remove(callback)
    
    async def _emit_event(self, event_type: EventType, payload: Dict[str, Any], metadata: Dict[str, Any] = None) -> None:
        """Emit an event to all subscribers."""
        event = Event(type=event_type, payload=payload, metadata=metadata or {})

        for callback in self._subscribers.get(event_type, []):
            try:
                await callback(event, metadata)
            except Exception as e:
                self.logger.error(f"Error in event callback: {e}")

    def _is_duplicate_message(self, msg_hash: str) -> bool:
        """
        Check if a message is a duplicate (received via different mesh path).

        Args:
            msg_hash: Hash of message content (sender + channel + text)

        Returns:
            True if this message was recently seen (duplicate)
        """
        now = time.time()

        # Clean up old entries
        expired = [h for h, ts in self._seen_messages.items()
                   if now - ts > self._seen_messages_ttl]
        for h in expired:
            del self._seen_messages[h]

        # Check if we've seen this message
        if msg_hash in self._seen_messages:
            return True

        # Mark as seen
        self._seen_messages[msg_hash] = now
        return False

    def _setup_event_handlers(self) -> None:
        """Setup handlers for pyMC_core events via dispatcher callbacks."""
        if not self._mesh_node:
            self.logger.warning("Cannot setup event handlers - mesh node not initialized")
            return

        dispatcher = self._mesh_node.dispatcher

        # Register raw packet callback for logging and RAW_DATA events
        dispatcher.set_raw_packet_callback(self._on_raw_packet)
        self.logger.info("Registered raw packet callback with dispatcher")

        # Register packet received callback for processed packets
        dispatcher.set_packet_received_callback(self._on_packet_received)
        self.logger.info("Registered packet received callback with dispatcher")

        self.logger.info("Event handlers setup for pyMC_core dispatcher")
    
    async def _load_contacts_from_database(self) -> None:
        """Load contacts from the complete_contact_tracking table."""
        try:
            if not hasattr(self.bot, 'db_manager'):
                self.logger.warning("Database manager not available")
                return
            
            # Query all tracked contacts from database
            query = """
                SELECT public_key, name, role, device_type, latitude, longitude,
                       out_path, out_path_len, last_heard
                FROM complete_contact_tracking
                WHERE role IN ('companion', 'repeater', 'roomserver')
                ORDER BY last_heard DESC
            """
            
            rows = self.bot.db_manager.execute_query(query)
            
            self._contacts.clear()
            for row in rows:
                contact = ContactInfo(
                    public_key=row['public_key'],
                    name=row['name'],
                    role=row.get('role', 'companion'),
                    device_type=row.get('device_type', 'unknown'),
                    latitude=row.get('latitude'),
                    longitude=row.get('longitude'),
                    out_path=row.get('out_path', ''),
                    out_path_len=row.get('out_path_len', 0),
                    last_heard=row.get('last_heard')
                )
                # Use public key prefix as key for quick lookup
                key = row['public_key'][:12] if row['public_key'] else row['name']
                self._contacts[key] = contact
            
            self.logger.info(f"Loaded {len(self._contacts)} contacts from database")
            
        except Exception as e:
            self.logger.error(f"Error loading contacts from database: {e}")
    
    def get_contact_by_name(self, name: str) -> Optional[ContactInfo]:
        """
        Find a contact by name.
        
        Args:
            name: Contact name to search for
            
        Returns:
            ContactInfo if found, None otherwise
        """
        name_lower = name.lower()
        for contact in self._contacts.values():
            if contact.name.lower() == name_lower:
                return contact
        return None
    
    def get_contact_by_key_prefix(self, prefix: str) -> Optional[ContactInfo]:
        """
        Find a contact by public key prefix.
        
        Args:
            prefix: Public key prefix (hex string)
            
        Returns:
            ContactInfo if found, None otherwise
        """
        prefix_lower = prefix.lower()
        for key, contact in self._contacts.items():
            if contact.public_key.lower().startswith(prefix_lower):
                return contact
        return None
    
    async def start_auto_message_fetching(self) -> None:
        """
        Start automatic message fetching.

        For pyMC_core, message fetching is handled by the dispatcher's
        callback system. This method ensures the dispatcher task is running.
        """
        if not self._mesh_node:
            self.logger.warning("Cannot start message fetching - mesh node not initialized")
            return

        # The dispatcher task was started in connect()
        # Just verify it's still running
        if hasattr(self, '_dispatcher_task') and self._dispatcher_task:
            if self._dispatcher_task.done():
                # Check if it failed with an exception
                try:
                    self._dispatcher_task.result()
                except Exception as e:
                    self.logger.error(f"Dispatcher task failed: {e}")
                    # Restart it
                    self._dispatcher_task = asyncio.create_task(self._mesh_node.start())
                    self.logger.info("Restarted dispatcher task")

        self.logger.info("Auto message fetching active (via dispatcher callbacks)")

    async def _on_raw_packet(self, pkt, data: bytes, analysis: dict = None) -> None:
        """
        Callback for raw packets from the dispatcher.

        This is called for EVERY packet received, before handler processing.
        Emits RAW_DATA events for the bot's event system.
        """
        try:
            # Get signal strength from packet or radio
            snr = getattr(pkt, '_snr', 0) or (self._radio.get_last_snr() if self._radio else 0)
            rssi = getattr(pkt, '_rssi', -120) or (self._radio.get_last_rssi() if self._radio else -120)

            self.logger.debug(f"Raw packet received: {len(data)} bytes, SNR={snr}, RSSI={rssi}")

            # Emit raw data event (with 'data' field for bot compatibility)
            await self._emit_event(
                EventType.RAW_DATA,
                {
                    'data': data.hex(),  # Bot expects 'data' field
                    'raw_hex': data.hex(),
                    'SNR': snr,
                    'RSSI': rssi,
                    'timestamp': time.time()
                }
            )

            # Also emit RX_LOG_DATA for signal metrics
            await self._emit_event(
                EventType.RX_LOG_DATA,
                {
                    'SNR': snr,
                    'RSSI': rssi,
                    'timestamp': time.time()
                }
            )

            # Handle TXT_MSG (DM) with KISS modem: decrypt via modem key_exchange + decrypt_data
            # (pymc_core's handler would call get_private_key() which the modem does not expose)
            if (
                KISS_MODEM_AVAILABLE
                and isinstance(self._radio, KISSModem)
                and isinstance(self._identity, KISSModemIdentity)
            ):
                from pymc_core.protocol.constants import (
                    PAYLOAD_TYPE_TXT_MSG,
                    PH_TYPE_SHIFT,
                )
                payload_type = (pkt.header >> PH_TYPE_SHIFT) if hasattr(pkt, 'header') else None
                if payload_type == PAYLOAD_TYPE_TXT_MSG:
                    await self._handle_raw_txt_msg_kiss(pkt, snr, rssi, data)

        except Exception as e:
            self.logger.error(f"Error in raw packet callback: {e}")

    async def _handle_raw_txt_msg_kiss(self, pkt, snr: float, rssi: float, raw_data: bytes = None) -> None:
        """
        Decrypt and emit TXT_MSG (DM) using KISS modem key_exchange + decrypt_data.
        MeshCore TXT_MSG payload: dest_hash (1), src_hash (1), MAC (2), ciphertext.
        Plaintext: timestamp (4), flags (1), message (utf-8).
        Deduplicates by packet hash. Sends ACK after successful decrypt.
        """
        try:
            from pymc_core.protocol.constants import PAYLOAD_TYPE_TXT_MSG

            payload = getattr(pkt, 'payload', None) or b''
            if len(payload) < 4:
                self.logger.debug("TXT_MSG payload too short")
                return

            # Deduplicate by packet hash (same DM can arrive via multiple paths)
            if raw_data and len(raw_data) > 0:
                from .utils import calculate_packet_hash
                pkt_hash = calculate_packet_hash(raw_data.hex(), PAYLOAD_TYPE_TXT_MSG)
            else:
                import hashlib
                pkt_hash = hashlib.sha256(payload).hexdigest()[:16].upper()
            if self._is_duplicate_message(pkt_hash):
                self.logger.debug(f"Ignoring duplicate TXT_MSG (hash: {pkt_hash[:8]})")
                return

            dest_hash = payload[0]
            src_hash = payload[1]
            mac = payload[2:4]
            ciphertext = payload[4:]

            # Resolve sender public key from contacts (first byte of pubkey = src_hash)
            sender_pubkey = None
            contacts_adapter = getattr(self._mesh_node, 'contacts', None)
            if contacts_adapter is not None:
                contacts_list = getattr(contacts_adapter, 'contacts', None) or []
                for contact in contacts_list:
                    pk = getattr(contact, 'public_key', None)
                    if not pk:
                        continue
                    try:
                        pk_bytes = bytes.fromhex(pk) if isinstance(pk, str) else pk
                        if len(pk_bytes) == 32 and pk_bytes[0] == src_hash:
                            sender_pubkey = pk_bytes
                            break
                    except (ValueError, TypeError):
                        continue
            if sender_pubkey is None:
                self.logger.debug(f"TXT_MSG from unknown sender (src_hash=0x{src_hash:02x}), cannot decrypt")
                return

            shared = self._radio.key_exchange(sender_pubkey)
            if not shared:
                self.logger.warning("KISS modem key_exchange failed for TXT_MSG")
                return
            plaintext = self._radio.decrypt_data(shared, mac, ciphertext)
            if not plaintext:
                self.logger.warning("KISS modem decrypt_data failed for TXT_MSG")
                return

            # Plaintext: timestamp (4), flags (1), message (rest)
            if len(plaintext) < 5:
                self.logger.debug("TXT_MSG plaintext too short")
                return
            msg_text = plaintext[5:].decode('utf-8', errors='replace').rstrip('\x00').strip()
            if not msg_text:
                self.logger.debug("TXT_MSG empty message")
                return

            timestamp_bytes = plaintext[:4]

            event_payload = {
                'text': msg_text,
                'pubkey_prefix': f'{src_hash:02x}',
                'SNR': snr,
                'RSSI': rssi,
                'path_len': 0,
                'raw_hex': payload.hex() if payload else '',
            }
            self.logger.info(f"DM from {event_payload['pubkey_prefix']}: {msg_text[:50]}...")
            await self._emit_event(EventType.CONTACT_MSG_RECV, event_payload)

            # Send ACK per MeshCore: dest_hash (1) + src_hash (1) + checksum (4) = CRC of timestamp, text, sender pubkey.
            # Wait for TX complete so the modem is free for the reply DM and the ACK is on air.
            try:
                import zlib
                from pymc_core.protocol.packet import Packet
                from pymc_core.protocol.constants import PAYLOAD_TYPE_ACK, PAYLOAD_VER_1
                from pymc_core.protocol.packet_utils import PacketHeaderUtils, RouteTypeUtils

                checksum_input = timestamp_bytes + msg_text.encode('utf-8') + sender_pubkey
                checksum = zlib.crc32(checksum_input) & 0xFFFFFFFF
                our_pubkey = self._identity.get_public_key()
                dest_hash = src_hash  # ACK recipient = DM sender
                src_hash_ack = our_pubkey[0]
                ack_payload = bytes([dest_hash, src_hash_ack]) + checksum.to_bytes(4, 'little')

                route_value = RouteTypeUtils.get_route_type_value("direct", has_routing_path=False)
                header = PacketHeaderUtils.create_header(PAYLOAD_TYPE_ACK, route_value, PAYLOAD_VER_1)
                ack_pkt = Packet()
                ack_pkt.header = header
                ack_pkt.payload = ack_payload
                ack_pkt.payload_len = len(ack_payload)
                ack_bytes = ack_pkt.write_to()
                if hasattr(self._radio, 'send') and asyncio.iscoroutinefunction(self._radio.send):
                    await self._radio.send(ack_bytes)
                else:
                    self._radio.send_frame(ack_bytes)
                self.logger.debug("Sent ACK for DM")
            except Exception as ack_err:
                self.logger.warning(f"Failed to send DM ACK: {ack_err}")
        except Exception as e:
            self.logger.error(f"Error handling TXT_MSG with KISS modem: {e}")

    async def _on_packet_received(self, pkt) -> None:
        """
        Callback for processed packets from the dispatcher.

        This is called after the dispatcher has parsed and handled the packet.
        We translate pymc_core packets to our event system.
        """
        try:
            # Import constants for payload types
            from pymc_core.protocol.constants import (
                PAYLOAD_TYPE_ADVERT,
                PAYLOAD_TYPE_ACK,
                PAYLOAD_TYPE_TXT_MSG,
                PAYLOAD_TYPE_GRP_TXT,
                PH_TYPE_SHIFT,
            )

            payload_type = pkt.header >> PH_TYPE_SHIFT
            snr = getattr(pkt, '_snr', 0)
            rssi = getattr(pkt, '_rssi', -120)

            self.logger.info(f"Packet received: type={payload_type}, SNR={snr}, RSSI={rssi}")

            if payload_type == PAYLOAD_TYPE_ADVERT:
                await self._handle_advert_packet(pkt, snr, rssi)
            elif payload_type == PAYLOAD_TYPE_TXT_MSG:
                # TXT_MSG already handled in raw callback when using KISS modem (decrypt + emit)
                # Skip here to avoid double CONTACT_MSG_RECV and duplicate execution
                if not (
                    KISS_MODEM_AVAILABLE
                    and isinstance(self._radio, KISSModem)
                    and isinstance(self._identity, KISSModemIdentity)
                ):
                    await self._handle_text_packet(pkt, snr, rssi)
            elif payload_type == PAYLOAD_TYPE_GRP_TXT:
                await self._handle_group_text_packet(pkt, snr, rssi)
            # ACKs are handled by the dispatcher internally

        except Exception as e:
            self.logger.error(f"Error in packet received callback: {e}")
            import traceback
            self.logger.error(traceback.format_exc())

    async def _handle_advert_packet(self, pkt, snr: float, rssi: float) -> None:
        """Handle advertisement packet from dispatcher."""
        try:
            # Use pymc_core's advert parsing utilities
            from pymc_core.protocol.utils import parse_advert_payload, decode_appdata

            payload = pkt.payload if hasattr(pkt, 'payload') else b''

            # Default event payload
            event_payload = {
                'public_key': '',
                'name': '',
                'latitude': None,
                'longitude': None,
                'flags': 0,
                'SNR': snr,
                'RSSI': rssi,
                'raw_hex': payload.hex() if payload else ''
            }

            # Parse the advert packet properly
            try:
                parsed = parse_advert_payload(payload)
                event_payload['public_key'] = parsed.get('pubkey', '')

                # Decode the appdata to get name, location, flags
                appdata = parsed.get('appdata', b'')
                if appdata:
                    decoded = decode_appdata(appdata)
                    event_payload['name'] = decoded.get('node_name', '') or decoded.get('name', '')
                    event_payload['latitude'] = decoded.get('latitude')
                    event_payload['longitude'] = decoded.get('longitude')
                    event_payload['flags'] = decoded.get('flags', 0)
            except Exception as e:
                self.logger.warning(f"Error parsing advert payload: {e}")

            # Extract path information from the packet so path length is recorded in the database
            path_len = getattr(pkt, 'path_len', 0)
            path_bytes = getattr(pkt, 'path', b'')
            if path_len > 0 and path_bytes:
                path_nodes = [f"{b:02x}" for b in path_bytes[:path_len]]
                event_payload['out_path'] = ','.join(path_nodes)
                event_payload['out_path_len'] = path_len
                event_payload['path_len'] = path_len
            else:
                event_payload['out_path'] = ''
                event_payload['out_path_len'] = 0
                event_payload['path_len'] = 0

            self.logger.info(f"Advertisement from {event_payload.get('name', 'unknown')}")

            # Emit new contact event
            await self._emit_event(EventType.NEW_CONTACT, event_payload)
            await self._emit_event(EventType.ADVERT_RECV, event_payload)

        except Exception as e:
            self.logger.error(f"Error handling advertisement: {e}")

    async def _handle_text_packet(self, pkt, snr: float, rssi: float) -> None:
        """Handle text message packet from dispatcher."""
        try:
            # The text handler in pymc_core decrypts and parses the message
            # We need to extract the relevant info
            payload = pkt.payload if hasattr(pkt, 'payload') else b''

            # Get source hash from payload
            src_hash = payload[1] if len(payload) > 1 else 0

            event_payload = {
                'text': '',  # Will need decryption via handler
                'pubkey_prefix': f'{src_hash:02x}',
                'SNR': snr,
                'RSSI': rssi,
                'path_len': 0,
                'raw_hex': payload.hex() if payload else ''
            }

            self.logger.info(f"Text message from {event_payload['pubkey_prefix']}")

            # Emit contact message event
            await self._emit_event(EventType.CONTACT_MSG_RECV, event_payload)

        except Exception as e:
            self.logger.error(f"Error handling text message: {e}")

    async def _handle_group_text_packet(self, pkt, snr: float, rssi: float) -> None:
        """Handle group/channel text message packet from dispatcher."""
        try:
            payload = pkt.payload if hasattr(pkt, 'payload') else b''

            # The GroupTextHandler in pymc_core stores decrypted data in pkt.decrypted
            group_data = getattr(pkt, 'decrypted', {}).get('group_text_data', {})

            # Strip null bytes from text (padding from decryption)
            text = group_data.get('text', '').rstrip('\x00')
            channel_name = group_data.get('channel_name', '')
            sender_name = group_data.get('sender_name', 'Unknown')
            channel_hash = group_data.get('channel_hash', 0)

            # If group_data is empty, the message wasn't decrypted (missing channel key)
            # This is normal - we only decrypt channels we're configured to monitor
            if not group_data:
                ch_hash = payload[0] if payload else 0
                self.logger.debug(f"Skipping channel message - no key for hash 0x{ch_hash:02X}")
                # Don't emit an event for undecryptable messages
                return

            # Deduplicate messages received via different mesh paths
            # Use the packet's built-in hash (used by mesh for flood deduplication)
            pkt_hash = pkt.get_packet_hash_hex() if hasattr(pkt, 'get_packet_hash_hex') else None
            if pkt_hash and self._is_duplicate_message(pkt_hash):
                self.logger.debug(f"Ignoring duplicate channel message from {sender_name} on [{channel_name}] (hash: {pkt_hash[:8]})")
                return

            # Extract path information from the packet
            path_len = getattr(pkt, 'path_len', 0)
            path_bytes = getattr(pkt, 'path', b'')
            path_string = None
            if path_len > 0 and path_bytes:
                # Convert path bytes to hex string (each byte is a node hash)
                path_nodes = [f"{b:02x}" for b in path_bytes[:path_len]]
                path_string = f"{','.join(path_nodes)} ({path_len} hops)"
            elif path_len == 0:
                path_string = "Direct"

            event_payload = {
                'text': text,
                'channel': channel_name,
                'channel_name': channel_name,
                'channel_idx': channel_hash,  # Use hash as index for compatibility
                'sender': sender_name,
                'sender_name': sender_name,
                'SNR': snr,
                'RSSI': rssi,
                'path': path_string,
                'path_len': path_len,
                'raw_hex': payload.hex() if payload else ''
            }

            self.logger.info(f"Channel message on [{channel_name}] from {sender_name}: {text[:50]}...")

            # Emit channel message event
            await self._emit_event(EventType.CHANNEL_MSG_RECV, event_payload)

        except Exception as e:
            self.logger.error(f"Error handling group text: {e}")


class PyMCCommands:
    """
    Commands interface compatible with meshcore.commands.
    
    Provides methods for sending messages, advertisements, and
    other mesh operations using pyMC_core.
    """
    
    def __init__(self, connection: PyMCConnection):
        self.connection = connection
        self.logger = connection.logger
    
    async def send_msg(self, contact: ContactInfo, content: str) -> Event:
        """
        Send a direct message to a contact.
        Uses pyMC_core PacketBuilder.create_text_message + dispatcher.send_packet(wait_for_ack=True)
        when the identity supports it; falls back to KISS modem key_exchange + encrypt_data when not.
        """
        try:
            if not self.connection._mesh_node:
                return Event(type=EventType.ERROR, payload={'error': 'Not connected'})
            if not self.connection._identity:
                return Event(type=EventType.ERROR, payload={'error': 'No identity'})

            pub_key_hex = contact.public_key if isinstance(contact, ContactInfo) else contact.get('public_key', '')
            if not pub_key_hex:
                return Event(type=EventType.ERROR, payload={'error': 'Contact has no public key'})

            try:
                recipient_pubkey = bytes.fromhex(pub_key_hex)
            except ValueError:
                return Event(type=EventType.ERROR, payload={'error': 'Invalid contact public key'})
            if len(recipient_pubkey) != 32:
                return Event(type=EventType.ERROR, payload={'error': 'Contact public key must be 32 bytes'})

            contact_name = contact.name if isinstance(contact, ContactInfo) else contact.get('name', '')
            self.logger.info(f"Sending DM to {contact_name}: {content[:50]}...")

            # Contact-like object for PacketBuilder.create_text_message (name, public_key hex, out_path)
            class _ContactAdapter:
                def __init__(self, name: str, public_key_hex: str):
                    self.name = name
                    self.public_key = public_key_hex
                    self.out_path = []

            contact_adapter = _ContactAdapter(contact_name or "Contact", pub_key_hex)

            # Prefer pyMC_core PacketBuilder.create_text_message + dispatcher.send_packet (like send_text_message example)
            try:
                from pymc_core.protocol.packet_builder import PacketBuilder
                packet, crc = PacketBuilder.create_text_message(
                    contact=contact_adapter,
                    local_identity=self.connection._identity,
                    message=content,
                    attempt=0,
                    message_type="direct",
                )
                success = await self.connection._mesh_node.dispatcher.send_packet(packet, wait_for_ack=True)
                if success:
                    to_label = contact_name if contact_name else f"{recipient_pubkey[0]:02x}"
                    self.logger.info(f"DM sent to {to_label} (ACK received)")
                    return Event(type=EventType.MSG_SENT, payload={'sent': True})
                self.logger.warning("DM send: no ACK received")
                return Event(type=EventType.ERROR, payload={'error': 'No ACK received'})
            except NotImplementedError:
                # KISS modem: identity does not expose private key; build packet via modem crypto
                pass
            except Exception as e:
                if "private key" in str(e).lower() or "get_private_key" in str(e):
                    pass  # Fall through to KISS path
                else:
                    raise

            # KISS modem: build TXT_MSG with modem key_exchange + encrypt_data, send via dispatcher when possible
            if (
                KISS_MODEM_AVAILABLE
                and isinstance(self.connection._radio, KISSModem)
                and isinstance(self.connection._identity, KISSModemIdentity)
            ):
                return await self._send_msg_kiss(recipient_pubkey, content, contact_name)
            self.logger.warning("DM send not implemented for this identity")
            return Event(type=EventType.ERROR, payload={'error': 'DM send not implemented for this identity'})
        except Exception as e:
            self.logger.error(f"Error sending message: {e}")
            return Event(type=EventType.ERROR, payload={'error': str(e)})

    async def _send_msg_kiss(
        self, recipient_pubkey: bytes, content: str, contact_name: str = ""
    ) -> Event:
        """Build encrypted TXT_MSG via KISS modem key_exchange + encrypt_data; send via dispatcher (wait_for_ack) when possible."""
        try:
            from pymc_core.protocol.packet import Packet
            from pymc_core.protocol.constants import PAYLOAD_TYPE_TXT_MSG, PAYLOAD_VER_1
            from pymc_core.protocol.packet_utils import PacketHeaderUtils, RouteTypeUtils

            our_pubkey = self.connection._identity.get_public_key()
            dest_hash = recipient_pubkey[0]
            src_hash = our_pubkey[0]

            timestamp_bytes = int(time.time()).to_bytes(4, "little")
            flags = 0x00
            plaintext = timestamp_bytes + bytes([flags]) + content.encode("utf-8")

            shared = self.connection._radio.key_exchange(recipient_pubkey)
            if not shared:
                self.logger.warning("KISS modem key_exchange failed for DM send")
                return Event(type=EventType.ERROR, payload={'error': 'Key exchange failed'})
            result = self.connection._radio.encrypt_data(shared, plaintext)
            if not result:
                self.logger.warning("KISS modem encrypt_data failed for DM send")
                return Event(type=EventType.ERROR, payload={'error': 'Encryption failed'})
            mac, ciphertext = result

            payload = bytes([dest_hash, src_hash]) + mac + ciphertext
            route_value = RouteTypeUtils.get_route_type_value("direct", has_routing_path=False)
            header = PacketHeaderUtils.create_header(
                PAYLOAD_TYPE_TXT_MSG, route_value, PAYLOAD_VER_1
            )
            pkt = Packet()
            pkt.header = header
            pkt.payload = payload
            pkt.payload_len = len(payload)

            # Prefer dispatcher.send_packet(packet, wait_for_ack=True) like pyMC_core send_text_message example
            dispatcher = getattr(self.connection._mesh_node, "dispatcher", None)
            if dispatcher is not None and hasattr(dispatcher, "send_packet"):
                success = await dispatcher.send_packet(pkt, wait_for_ack=True)
                if success:
                    to_label = contact_name if contact_name else f"{dest_hash:02x}"
                    self.logger.info(f"DM sent to {to_label} (ACK received)")
                    return Event(type=EventType.MSG_SENT, payload={'sent': True})
                self.logger.warning("DM send via KISS: no ACK received")
                return Event(type=EventType.ERROR, payload={'error': 'No ACK received'})

            # Fallback: send raw frame (no wait_for_ack)
            packet_bytes = pkt.write_to()
            self.connection._radio.send_frame(packet_bytes)
            to_label = contact_name if contact_name else f"{dest_hash:02x}"
            self.logger.info(f"DM sent to {to_label} ({len(packet_bytes)} bytes)")
            return Event(type=EventType.MSG_SENT, payload={'sent': True})
        except Exception as e:
            self.logger.error(f"Error sending DM via KISS: {e}")
            return Event(type=EventType.ERROR, payload={'error': str(e)})
    
    async def send_msg_with_retry(self, contact: ContactInfo, content: str,
                                   max_attempts: int = 3,
                                   max_flood_attempts: int = 2,
                                   flood_after: int = 2,
                                   timeout: int = 0) -> Event:
        """
        Send a direct message with retry logic.
        
        Args:
            contact: Contact to send message to
            content: Message content
            max_attempts: Maximum retry attempts
            max_flood_attempts: Max flood mode attempts
            flood_after: Switch to flood after N attempts
            timeout: Timeout in seconds (0 = use default)
            
        Returns:
            Event indicating success or failure
        """
        # For initial implementation, just call send_msg
        # Full retry logic can be added later
        return await self.send_msg(contact, content)
    
    async def send_advert(self, flood: bool = False) -> Event:
        """
        Send an advertisement packet.

        Args:
            flood: If True, send flood advertisement

        Returns:
            Event indicating success or failure
        """
        try:
            if not self.connection._mesh_node:
                return Event(type=EventType.ERROR, payload={'error': 'Not connected'})

            if not self.connection._identity:
                return Event(type=EventType.ERROR, payload={'error': 'No identity'})

            advert_type = "flood" if flood else "direct"
            self.logger.info(f"Sending {advert_type} advertisement")

            # Get bot name and location from config
            bot_name = self.connection.bot.config.get('Bot', 'name', fallback='PyMC-Bot')
            lat = self.connection.bot.config.getfloat('Bot', 'latitude', fallback=0.0)
            lon = self.connection.bot.config.getfloat('Bot', 'longitude', fallback=0.0)

            # Build advertisement packet using pymc_core
            from pymc_core.protocol.packet_builder import PacketBuilder

            if flood:
                packet = PacketBuilder.create_flood_advert(
                    local_identity=self.connection._identity,
                    name=bot_name,
                    lat=lat,
                    lon=lon
                )
            else:
                packet = PacketBuilder.create_direct_advert(
                    local_identity=self.connection._identity,
                    name=bot_name,
                    lat=lat,
                    lon=lon
                )

            # Send packet via radio
            packet_bytes = packet.write_to()
            self.connection._radio.send_frame(packet_bytes)

            self.logger.info(f"Advertisement sent: {len(packet_bytes)} bytes")
            return Event(type=EventType.OK, payload={'sent': True, 'size': len(packet_bytes)})

        except Exception as e:
            self.logger.error(f"Error sending advertisement: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return Event(type=EventType.ERROR, payload={'error': str(e)})

    async def send_channel_msg(self, channel_name: str, content: str) -> Event:
        """
        Send a message to a channel as a flood (so it propagates across the mesh).

        Uses pymc_core's PacketBuilder.create_group_datagram for the packet, then
        overrides the header to flood routing and sends as raw frame.
        """
        try:
            if not self.connection._mesh_node:
                return Event(type=EventType.ERROR, payload={'error': 'Not connected'})
            if not self.connection._identity:
                return Event(type=EventType.ERROR, payload={'error': 'No identity'})

            self.logger.info(f"Sending channel message to {channel_name}: {content[:50]}...")

            from pymc_core.protocol.packet_builder import PacketBuilder
            from pymc_core.protocol.constants import PAYLOAD_TYPE_GRP_TXT, PAYLOAD_VER_1
            from pymc_core.protocol.packet_utils import RouteTypeUtils, PacketHeaderUtils

            channel_db = self.connection._mesh_node.channel_db
            channels_config = channel_db.get_channels() if channel_db else []
            if not channels_config:
                return Event(type=EventType.ERROR, payload={'error': 'No channels configured'})

            sender_name = self.connection._mesh_node.node_name

            # Build encrypted group packet via pymc_core API (defaults to direct routing)
            pkt = PacketBuilder.create_group_datagram(
                channel_name,
                self.connection._identity,
                content,
                sender_name=sender_name,
                channels_config=channels_config,
            )

            # Override header to flood so the message propagates across the mesh
            route_value = RouteTypeUtils.get_route_type_value("flood", has_routing_path=False)
            pkt.header = PacketHeaderUtils.create_header(
                PAYLOAD_TYPE_GRP_TXT, route_value, PAYLOAD_VER_1
            )

            packet_bytes = pkt.write_to()
            self.connection._radio.send_frame(packet_bytes)

            self.logger.info(f"Channel message sent to {channel_name} ({len(packet_bytes)} bytes)")
            return Event(type=EventType.MSG_SENT, payload={'sent': True, 'channel': channel_name})

        except ValueError as e:
            self.logger.error(f"Channel message error: {e}")
            return Event(type=EventType.ERROR, payload={'error': str(e)})
        except Exception as e:
            self.logger.error(f"Error sending channel message: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return Event(type=EventType.ERROR, payload={'error': str(e)})

    async def get_time(self) -> Event:
        """
        Get device time (not applicable for pyMC, return current time).
        
        Returns:
            Event with current time
        """
        return Event(type=EventType.OK, payload={'time': int(time.time())})
    
    async def set_time(self, timestamp: int) -> Event:
        """
        Set device time (not applicable for pyMC).
        
        Returns:
            Event indicating success
        """
        # pyMC manages time locally, no device to sync
        return Event(type=EventType.OK, payload={'time': timestamp})
    
    async def get_contacts(self) -> Event:
        """
        Get contact list from database.
        
        Returns:
            Event with contact list
        """
        contacts = list(self.connection._contacts.values())
        return Event(type=EventType.OK, payload={'contacts': contacts})
    
    async def add_contact(self, public_key_or_data, name: str = None) -> Event:
        """
        Add a contact to the database.

        Supports multiple calling conventions:
            add_contact(contact_data_dict)
            add_contact(public_key, name)
            add_contact(name, public_key)

        Returns:
            Event indicating success or failure
        """
        try:
            # Handle dict argument (contact_data)
            if isinstance(public_key_or_data, dict):
                contact_data = public_key_or_data
                public_key = contact_data.get('public_key', '')
                name = contact_data.get('name', contact_data.get('adv_name', ''))
            elif name is None:
                # Single string argument - assume it's a name
                name = public_key_or_data
                public_key = ''
            else:
                # Two arguments - could be (key, name) or (name, key)
                if len(public_key_or_data) == 64:
                    # Looks like a public key (64 hex chars)
                    public_key = public_key_or_data
                else:
                    # Assume first is name, second is key
                    public_key = name
                    name = public_key_or_data

            if not name:
                name = f"Contact_{public_key[:8]}" if public_key else "Unknown"

            # Add to local cache
            contact = ContactInfo(public_key=public_key, name=name)
            key = public_key[:12] if public_key else name
            self.connection._contacts[key] = contact

            self.logger.info(f"Added contact: {name} ({public_key[:12] if public_key else 'no key'})")
            return Event(type=EventType.OK, payload={'added': True})

        except Exception as e:
            self.logger.error(f"Error adding contact: {e}")
            return Event(type=EventType.ERROR, payload={'error': str(e)})
    
    async def remove_contact(self, public_key: str) -> Event:
        """
        Remove a contact from the database.
        
        Args:
            public_key: Contact's public key
            
        Returns:
            Event indicating success or failure
        """
        try:
            key = public_key[:12]
            if key in self.connection._contacts:
                del self.connection._contacts[key]
            
            return Event(type=EventType.OK, payload={'removed': True})

        except Exception as e:
            return Event(type=EventType.ERROR, payload={'error': str(e)})

    async def get_channel(self, channel_idx: int) -> Event:
        """
        Get channel information from database.

        Args:
            channel_idx: Channel index

        Returns:
            Event with channel info
        """
        try:
            if not hasattr(self.connection.bot, 'db_manager'):
                return Event(type=EventType.ERROR, payload={'error': 'Database not available'})

            query = """
                SELECT channel_idx, channel_name, channel_type, channel_key_hex
                FROM channels
                WHERE channel_idx = ?
            """
            rows = self.connection.bot.db_manager.execute_query(query, (channel_idx,))

            if rows:
                row = rows[0]
                return Event(type=EventType.OK, payload={
                    'channel_idx': row.get('channel_idx'),
                    'channel_name': row.get('channel_name', ''),
                    'channel_type': row.get('channel_type', ''),
                    'channel_key_hex': row.get('channel_key_hex', ''),
                    'channel_secret': bytes.fromhex(row.get('channel_key_hex', '')) if row.get('channel_key_hex') else b''
                })
            else:
                return Event(type=EventType.ERROR, payload={'error': f'Channel {channel_idx} not found'})

        except Exception as e:
            self.logger.error(f"Error getting channel: {e}")
            return Event(type=EventType.ERROR, payload={'error': str(e)})

    async def set_channel(self, channel_idx: int, channel_name: str, channel_secret) -> Event:
        """
        Set/create a channel in the database.

        Args:
            channel_idx: Channel index
            channel_name: Channel name
            channel_secret: Channel key (bytes or hex string)

        Returns:
            Event indicating success or failure
        """
        try:
            if not hasattr(self.connection.bot, 'db_manager'):
                return Event(type=EventType.ERROR, payload={'error': 'Database not available'})

            # Convert secret to hex string if needed
            if isinstance(channel_secret, bytes):
                channel_key_hex = channel_secret.hex()
            else:
                channel_key_hex = str(channel_secret)

            # Upsert channel in database
            query = """
                INSERT OR REPLACE INTO channels (channel_idx, channel_name, channel_key_hex, last_updated)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """
            self.connection.bot.db_manager.execute_query(query, (channel_idx, channel_name, channel_key_hex))

            # Invalidate channel cache
            if hasattr(self.connection, '_channel_db'):
                self.connection._channel_db._cache_time = 0

            self.logger.info(f"Set channel {channel_idx}: {channel_name}")
            return Event(type=EventType.OK, payload={
                'channel_idx': channel_idx,
                'channel_name': channel_name,
                'channel_key_hex': channel_key_hex
            })

        except Exception as e:
            self.logger.error(f"Error setting channel: {e}")
            return Event(type=EventType.ERROR, payload={'error': str(e)})
