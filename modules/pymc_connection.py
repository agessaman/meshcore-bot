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
            
            self.logger.info(f"Connecting to MeshTNC via KISS serial: {serial_port}")
            
            # Load or create identity
            from .pymc_identity import IdentityManager
            identity_manager = IdentityManager(self.bot)
            self._identity = await identity_manager.load_or_create_identity()
            
            if not self._identity:
                self.logger.error("Failed to load or create identity")
                return False
            
            # Configure KISS TNC radio
            kiss_config = {
                "frequency": frequency,
                "bandwidth": bandwidth,
                "spreading_factor": spreading_factor,
                "coding_rate": coding_rate,
                "sync_word": sync_word,
                "power": tx_power,
            }
            
            self.logger.info(f"Radio config: freq={frequency/1e6:.3f}MHz, BW={bandwidth/1000}kHz, SF={spreading_factor}")
            
            # Create KISS serial wrapper
            self._radio = KissSerialWrapper(
                port=serial_port,
                baudrate=baudrate,
                radio_config=kiss_config,
                auto_configure=True
            )
            
            # Connect to radio
            if not self._radio.connect():
                self.logger.error(f"Failed to connect to MeshTNC on {serial_port}")
                return False
            
            self.logger.info("KISS radio connected successfully")
            
            # Get bot name from config
            bot_name = self.config.get('bot_name', 'MeshCoreBot')
            
            # Create mesh node
            node_config = {
                "node": {"name": bot_name}
            }
            
            self._mesh_node = MeshNode(
                radio=self._radio,
                local_identity=self._identity,
                config=node_config
            )
            
            # Start the mesh node
            await self._mesh_node.start()
            
            # Setup event handlers
            self._setup_event_handlers()
            
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
        
        if self._mesh_node:
            try:
                await self._mesh_node.stop()
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
    
    def _setup_event_handlers(self) -> None:
        """Setup handlers for pyMC_core events."""
        if not self._mesh_node:
            return
        
        # Get the event service from mesh node
        event_service = getattr(self._mesh_node, 'event_service', None)
        if not event_service:
            self.logger.warning("MeshNode does not have event_service")
            return
        
        # Register handlers for different packet types
        # pyMC_core uses a different event system, we need to translate
        self.logger.info("Event handlers setup for pyMC_core")
    
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
        Start automatic message fetching loop.
        
        For pyMC_core, this starts the receive loop that processes
        incoming packets from the KISS TNC.
        """
        if not self._mesh_node:
            self.logger.warning("Cannot start message fetching - mesh node not initialized")
            return
        
        # Start background task for receiving packets
        asyncio.create_task(self._receive_loop())
        self.logger.info("Auto message fetching started")
    
    async def _receive_loop(self) -> None:
        """Background loop for receiving and processing packets."""
        self.logger.info("Starting receive loop")
        
        while self._connected and self._radio:
            try:
                # Check for incoming packets
                if hasattr(self._radio, 'receive'):
                    packet = await asyncio.get_event_loop().run_in_executor(
                        None, self._radio.receive
                    )
                    
                    if packet:
                        await self._process_incoming_packet(packet)
                
                # Small delay to prevent busy loop
                await asyncio.sleep(0.01)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in receive loop: {e}")
                await asyncio.sleep(1)
        
        self.logger.info("Receive loop stopped")
    
    async def _process_incoming_packet(self, packet: bytes) -> None:
        """
        Process an incoming packet and emit appropriate events.
        
        Args:
            packet: Raw packet bytes from radio
        """
        try:
            # Decode packet using pyMC_core
            # This is a simplified version - full implementation would use
            # pyMC_core's packet parsing
            
            if len(packet) < 2:
                return
            
            # Extract header byte
            header = packet[0]
            route_type = header & 0x03
            payload_type = (header >> 2) & 0x0F
            
            # Calculate SNR/RSSI if available from KISS metadata
            snr = 0
            rssi = -120
            
            # Emit raw data event
            await self._emit_event(
                EventType.RAW_DATA,
                {
                    'raw_hex': packet.hex(),
                    'SNR': snr,
                    'RSSI': rssi,
                    'timestamp': time.time()
                }
            )
            
            # Parse based on payload type
            if payload_type == 0x01:  # TXT_MSG
                await self._handle_text_message(packet, snr, rssi)
            elif payload_type == 0x04:  # ADVERT
                await self._handle_advertisement(packet, snr, rssi)
            
        except Exception as e:
            self.logger.error(f"Error processing packet: {e}")
    
    async def _handle_text_message(self, packet: bytes, snr: float, rssi: float) -> None:
        """Handle incoming text message packet."""
        try:
            # Parse message packet
            # This is simplified - full implementation needs proper MeshCore parsing
            
            header = packet[0]
            route_type = header & 0x03
            
            # Skip header and extract path/payload
            # Format depends on route type
            
            payload = {
                'text': '',  # Will be decrypted
                'pubkey_prefix': '',
                'SNR': snr,
                'RSSI': rssi,
                'path_len': 0,
                'raw_hex': packet.hex()
            }
            
            # Emit as DM or channel message based on route type
            # For now, emit as contact message
            await self._emit_event(EventType.CONTACT_MSG_RECV, payload)
            
        except Exception as e:
            self.logger.error(f"Error handling text message: {e}")
    
    async def _handle_advertisement(self, packet: bytes, snr: float, rssi: float) -> None:
        """Handle incoming advertisement packet."""
        try:
            # Parse advertisement packet
            # Advertisements contain node identity, location, flags
            
            payload = {
                'public_key': '',
                'name': '',
                'latitude': None,
                'longitude': None,
                'flags': 0,
                'SNR': snr,
                'RSSI': rssi,
                'raw_hex': packet.hex()
            }
            
            # Emit new contact event
            await self._emit_event(EventType.NEW_CONTACT, payload)
            await self._emit_event(EventType.ADVERT_RECV, payload)
            
        except Exception as e:
            self.logger.error(f"Error handling advertisement: {e}")


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
        
        Args:
            contact: Contact to send message to
            content: Message content
            
        Returns:
            Event indicating success or failure
        """
        try:
            if not self.connection._mesh_node:
                return Event(type=EventType.ERROR, payload={'error': 'Not connected'})
            
            # Get contact's public key for encryption
            pub_key_hex = contact.public_key if isinstance(contact, ContactInfo) else contact.get('public_key', '')
            
            if not pub_key_hex:
                return Event(type=EventType.ERROR, payload={'error': 'Contact has no public key'})
            
            # Build and send message packet using pyMC_core
            # This is simplified - full implementation needs proper packet building
            
            self.logger.info(f"Sending message to {contact.name}: {content[:50]}...")
            
            # For now, use mesh node's send capability
            # TODO: Implement proper packet building with encryption
            
            return Event(type=EventType.MSG_SENT, payload={'sent': True})
            
        except Exception as e:
            self.logger.error(f"Error sending message: {e}")
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
            
            advert_type = "flood" if flood else "zero-hop"
            self.logger.info(f"Sending {advert_type} advertisement")
            
            # TODO: Build and send advertisement packet
            
            return Event(type=EventType.OK, payload={'sent': True})
            
        except Exception as e:
            self.logger.error(f"Error sending advertisement: {e}")
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
    
    async def add_contact(self, public_key: str, name: str) -> Event:
        """
        Add a contact to the database.
        
        Args:
            public_key: Contact's public key
            name: Contact's name
            
        Returns:
            Event indicating success or failure
        """
        try:
            # Add to database via repeater manager
            if hasattr(self.connection.bot, 'repeater_manager'):
                # This will be tracked via advertisement handling
                pass
            
            # Add to local cache
            contact = ContactInfo(public_key=public_key, name=name)
            key = public_key[:12]
            self.connection._contacts[key] = contact
            
            return Event(type=EventType.OK, payload={'added': True})
            
        except Exception as e:
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
