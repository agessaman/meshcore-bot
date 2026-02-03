"""
MeshCore KISS Modem Serial Interface

Implements the MeshCore KISS modem protocol for communicating with
radios running the MeshCore KISS modem firmware.

Protocol features:
- Standard KISS framing with byte stuffing (FEND=0xC0, FESC=0xDB)
- Received packets include SNR + RSSI metadata
- Supports radio configuration, crypto operations, and diagnostics
- Identity stored on modem (Ed25519 keypair in flash)
"""

import asyncio
import logging
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable, Dict, Optional, Tuple

import serial

logger = logging.getLogger("KISSModem")


# =============================================================================
# KISS Protocol Constants
# =============================================================================

KISS_FEND = 0xC0   # Frame delimiter
KISS_FESC = 0xDB   # Escape character
KISS_TFEND = 0xDC  # Escaped FEND (FESC + TFEND = 0xC0)
KISS_TFESC = 0xDD  # Escaped FESC (FESC + TFESC = 0xDB)

MAX_FRAME_SIZE = 512


# =============================================================================
# Command Constants (Host -> Modem)
# =============================================================================

class KISSCommand(IntEnum):
    """Commands sent from host to modem."""
    CMD_DATA = 0x00             # Send packet (2-255 bytes)
    CMD_GET_IDENTITY = 0x01    # Get modem's public key
    CMD_GET_RANDOM = 0x02      # Get random bytes (1-64)
    CMD_VERIFY_SIGNATURE = 0x03  # Verify Ed25519 signature
    CMD_SIGN_DATA = 0x04       # Sign data with modem's key
    CMD_ENCRYPT_DATA = 0x05    # Encrypt data
    CMD_DECRYPT_DATA = 0x06    # Decrypt data
    CMD_KEY_EXCHANGE = 0x07    # ECDH key exchange
    CMD_HASH = 0x08            # SHA-256 hash
    CMD_SET_RADIO = 0x09       # Set radio parameters
    CMD_SET_TX_POWER = 0x0A    # Set TX power (dBm)
    CMD_SET_SYNC_WORD = 0x0B   # Set LoRa sync word
    CMD_GET_RADIO = 0x0C       # Get radio parameters
    CMD_GET_TX_POWER = 0x0D    # Get TX power
    CMD_GET_SYNC_WORD = 0x0E   # Get sync word
    CMD_GET_VERSION = 0x0F     # Get firmware version
    CMD_GET_CURRENT_RSSI = 0x10  # Get current RSSI
    CMD_IS_CHANNEL_BUSY = 0x11   # Check if channel is busy
    CMD_GET_AIRTIME = 0x12     # Calculate airtime for packet
    CMD_GET_NOISE_FLOOR = 0x13 # Get noise floor
    CMD_GET_STATS = 0x14       # Get RX/TX stats
    CMD_GET_BATTERY = 0x15     # Get battery voltage
    CMD_PING = 0x16            # Ping modem
    CMD_GET_SENSORS = 0x17     # Get sensor data (CayenneLPP)


# =============================================================================
# Response Constants (Modem -> Host)
# =============================================================================

class KISSResponse(IntEnum):
    """Responses sent from modem to host."""
    CMD_DATA = 0x00            # Received packet (SNR + RSSI + data)
    RESP_IDENTITY = 0x21       # Public key (32 bytes)
    RESP_RANDOM = 0x22         # Random bytes (1-64)
    RESP_VERIFY = 0x23         # Verification result (1 byte)
    RESP_SIGNATURE = 0x24      # Signature (64 bytes)
    RESP_ENCRYPTED = 0x25      # MAC (2) + ciphertext
    RESP_DECRYPTED = 0x26      # Plaintext
    RESP_SHARED_SECRET = 0x27  # Shared secret (32 bytes)
    RESP_HASH = 0x28           # SHA-256 hash (32 bytes)
    RESP_OK = 0x29             # Command successful
    RESP_RADIO = 0x2A          # Radio params (freq + bw + sf + cr)
    RESP_TX_POWER = 0x2B       # TX power (1 byte)
    RESP_SYNC_WORD = 0x2C      # Sync word (1 byte)
    RESP_VERSION = 0x2D        # Version (2 bytes)
    RESP_ERROR = 0x2E          # Error code (1 byte)
    RESP_TX_DONE = 0x2F        # TX complete (1 byte: 0=fail, 1=success)
    RESP_CURRENT_RSSI = 0x30   # Current RSSI (1 byte, signed)
    RESP_CHANNEL_BUSY = 0x31   # Channel busy (1 byte)
    RESP_AIRTIME = 0x32        # Airtime in ms (4 bytes)
    RESP_NOISE_FLOOR = 0x33    # Noise floor (2 bytes, signed)
    RESP_STATS = 0x34          # Stats: RX(4) + TX(4) + Errors(4)
    RESP_BATTERY = 0x35        # Battery millivolts (2 bytes)
    RESP_PONG = 0x36           # Ping response
    RESP_SENSORS = 0x37        # Sensor data (CayenneLPP)


class KISSError(IntEnum):
    """Error codes from modem."""
    ERR_INVALID_LENGTH = 0x01  # Request data too short
    ERR_INVALID_PARAM = 0x02   # Invalid parameter value
    ERR_NO_CALLBACK = 0x03     # Feature not available
    ERR_MAC_FAILED = 0x04      # MAC verification failed
    ERR_UNKNOWN_CMD = 0x05     # Unknown command
    ERR_ENCRYPT_FAILED = 0x06  # Encryption failed
    ERR_TX_PENDING = 0x07      # TX already in progress


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class RadioConfig:
    """Radio configuration parameters."""
    frequency: int = 910525000     # Hz (US default)
    bandwidth: int = 62500         # Hz (62.5 kHz)
    spreading_factor: int = 7      # SF7
    coding_rate: int = 5           # 4/5
    tx_power: int = 22             # dBm
    sync_word: int = 0x12          # MeshCore default


@dataclass
class ReceivedPacket:
    """A received packet with metadata."""
    data: bytes
    snr: float      # SNR * 4 for 0.25 dB precision
    rssi: int       # dBm (signed)
    timestamp: float


@dataclass
class ModemStats:
    """Modem statistics."""
    rx_packets: int = 0
    tx_packets: int = 0
    rx_errors: int = 0


# =============================================================================
# KISS Modem Interface
# =============================================================================

class KISSModem:
    """
    MeshCore KISS Modem Serial Interface.

    Implements the MeshCore KISS modem protocol for full-duplex packet
    communication with radio metadata (SNR/RSSI).
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 1.0,
        radio_config: Optional[Dict[str, Any]] = None,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout

        # Parse radio config
        if radio_config:
            self.radio_config = RadioConfig(
                frequency=radio_config.get('frequency', 910525000),
                bandwidth=radio_config.get('bandwidth', 62500),
                spreading_factor=radio_config.get('spreading_factor', 7),
                coding_rate=radio_config.get('coding_rate', 5),
                tx_power=radio_config.get('tx_power', 22),
                sync_word=radio_config.get('sync_word', 0x12),
            )
        else:
            self.radio_config = RadioConfig()

        self.serial_conn: Optional[serial.Serial] = None
        self.is_connected = False

        # RX handling
        self.on_frame_received: Optional[Callable[[bytes, float, int], None]] = None
        self.rx_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        # Frame reassembly buffer
        self._rx_buffer = bytearray()
        self._in_frame = False

        # TX state
        self._tx_pending = False
        self._tx_lock = threading.Lock()

        # Response handling for synchronous commands
        self._response_event = threading.Event()
        self._response_data: Optional[Tuple[int, bytes]] = None
        self._response_lock = threading.Lock()

        # Stats
        self._last_snr: float = 0.0
        self._last_rssi: int = -120
        self._stats = ModemStats()

        # Event loop for async callbacks
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Modem identity (cached)
        self._identity: Optional[bytes] = None

    def connect(self) -> bool:
        """Connect to the KISS modem and configure radio."""
        try:
            self.serial_conn = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )

            self.is_connected = True
            self.stop_event.clear()

            logger.info(f"KISS modem connected to {self.port} at {self.baudrate} baud")

            # Clear any pending data (from previous sessions)
            time.sleep(0.2)
            while self.serial_conn.in_waiting > 0:
                discarded = self.serial_conn.read(self.serial_conn.in_waiting)
                logger.debug(f"Discarded {len(discarded)} bytes from buffer")
                time.sleep(0.05)

            # Start RX thread
            self.rx_thread = threading.Thread(target=self._rx_worker, daemon=True)
            self.rx_thread.start()

            # Give RX thread time to start
            time.sleep(0.05)

            # Ping modem to verify communication
            if not self._ping():
                logger.error("Modem did not respond to ping")
                self.disconnect()
                return False

            # Get modem identity
            self._identity = self._get_identity()
            if self._identity:
                logger.info(f"Modem identity: {self._identity.hex()[:16]}...")

            # Configure radio
            if not self._configure_radio():
                logger.error("Failed to configure radio")
                self.disconnect()
                return False

            # Get firmware version
            version = self._get_version()
            if version:
                logger.info(f"Modem firmware version: {version}")

            logger.info("KISS modem ready")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to KISS modem: {e}")
            import traceback
            logger.error(traceback.format_exc())
            self.is_connected = False
            return False

    def disconnect(self):
        """Disconnect from the modem."""
        self.is_connected = False
        self.stop_event.set()

        if self.rx_thread and self.rx_thread.is_alive():
            self.rx_thread.join(timeout=2.0)

        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()

        logger.info("KISS modem disconnected")

    # =========================================================================
    # KISS Frame Encoding/Decoding
    # =========================================================================

    def _encode_frame(self, command: int, data: bytes = b'') -> bytes:
        """Encode a KISS frame with byte stuffing."""
        frame = bytearray([KISS_FEND, command])

        for byte in data:
            if byte == KISS_FEND:
                frame.extend([KISS_FESC, KISS_TFEND])
            elif byte == KISS_FESC:
                frame.extend([KISS_FESC, KISS_TFESC])
            else:
                frame.append(byte)

        frame.append(KISS_FEND)
        return bytes(frame)

    def _decode_frame(self, frame: bytes) -> Tuple[int, bytes]:
        """Decode a KISS frame, removing byte stuffing."""
        if len(frame) < 1:
            return -1, b''

        command = frame[0]
        data = bytearray()
        escape = False

        # Process remaining bytes (if any) with byte unstuffing
        for byte in frame[1:]:
            if escape:
                if byte == KISS_TFEND:
                    data.append(KISS_FEND)
                elif byte == KISS_TFESC:
                    data.append(KISS_FESC)
                else:
                    data.append(byte)
                escape = False
            elif byte == KISS_FESC:
                escape = True
            else:
                data.append(byte)

        return command, bytes(data)

    # =========================================================================
    # RX Worker
    # =========================================================================

    def _rx_worker(self):
        """Background thread for receiving KISS frames."""
        logger.info("RX worker started")

        while not self.stop_event.is_set() and self.is_connected:
            try:
                if self.serial_conn and self.serial_conn.in_waiting > 0:
                    data = self.serial_conn.read(self.serial_conn.in_waiting)
                    self._process_rx_data(data)
                else:
                    time.sleep(0.001)

            except Exception as e:
                if self.is_connected:
                    logger.error(f"RX worker error: {e}")
                    time.sleep(0.1)

        logger.info("RX worker stopped")

    def _process_rx_data(self, data: bytes):
        """Process incoming serial data, extracting KISS frames."""
        for byte in data:
            if byte == KISS_FEND:
                if self._in_frame and len(self._rx_buffer) > 0:
                    # Complete frame received
                    self._handle_frame(bytes(self._rx_buffer))
                    self._rx_buffer.clear()
                self._in_frame = True
            elif self._in_frame:
                if len(self._rx_buffer) < MAX_FRAME_SIZE:
                    self._rx_buffer.append(byte)
                else:
                    # Frame too large, discard
                    logger.warning("Frame too large, discarding")
                    self._rx_buffer.clear()
                    self._in_frame = False

    def _handle_frame(self, frame: bytes):
        """Handle a complete KISS frame."""
        if len(frame) < 1:
            return

        command, data = self._decode_frame(frame)
        logger.debug(f"Received frame: cmd=0x{command:02X}, {len(data)} bytes")

        if command == KISSResponse.CMD_DATA:
            # Received packet: SNR (1) + RSSI (1) + data
            self._handle_rx_packet(data)
        elif command == KISSResponse.RESP_TX_DONE:
            # TX complete
            self._handle_tx_done(data)
        else:
            # Response to a command - signal waiting thread
            with self._response_lock:
                self._response_data = (command, data)
                self._response_event.set()

    def _handle_rx_packet(self, data: bytes):
        """Handle a received packet with SNR/RSSI metadata."""
        if len(data) < 3:  # Need at least SNR + RSSI + 1 byte data
            logger.warning(f"RX packet too short: {len(data)} bytes")
            return

        # Parse SNR (signed, *4 for 0.25dB precision)
        snr_raw = data[0]
        if snr_raw > 127:
            snr_raw -= 256
        snr = snr_raw / 4.0

        # Parse RSSI (signed dBm)
        rssi = data[1]
        if rssi > 127:
            rssi -= 256

        packet_data = data[2:]

        # Update stats
        self._last_snr = snr
        self._last_rssi = rssi
        self._stats.rx_packets += 1

        logger.debug(f"RX packet: {len(packet_data)} bytes, SNR={snr:.2f}, RSSI={rssi}")

        # Invoke callback
        if self.on_frame_received:
            self._invoke_callback(packet_data, snr, rssi)

    def _handle_tx_done(self, data: bytes):
        """Handle TX done response."""
        success = data[0] == 0x01 if len(data) > 0 else False
        self._tx_pending = False

        if success:
            self._stats.tx_packets += 1
            logger.debug("TX complete (success)")
        else:
            logger.warning("TX complete (failed)")

    def _invoke_callback(self, packet_data: bytes, snr: float, rssi: int):
        """Invoke RX callback in a thread-safe manner."""
        if not self.on_frame_received:
            return

        try:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(
                    lambda: self._loop.create_task(
                        self._async_callback_wrapper(packet_data, snr, rssi)
                    )
                )
            else:
                # Direct call (synchronous)
                self.on_frame_received(packet_data, snr, rssi)

        except Exception as e:
            logger.error(f"Error invoking callback: {e}")

    async def _async_callback_wrapper(self, packet_data: bytes, snr: float, rssi: int):
        """Async wrapper for callback invocation."""
        try:
            self.on_frame_received(packet_data, snr, rssi)
        except Exception as e:
            logger.error(f"Error in RX callback: {e}")

    # =========================================================================
    # Synchronous Command Interface
    # =========================================================================

    def _send_command(self, command: int, data: bytes = b'', timeout: float = 2.0) -> Optional[Tuple[int, bytes]]:
        """Send a command and wait for response."""
        if not self.serial_conn or not self.serial_conn.is_open:
            return None

        with self._response_lock:
            self._response_event.clear()
            self._response_data = None

        # Send frame
        frame = self._encode_frame(command, data)
        self.serial_conn.write(frame)
        self.serial_conn.flush()

        # Wait for response
        if self._response_event.wait(timeout):
            with self._response_lock:
                return self._response_data

        logger.warning(f"Command 0x{command:02X} timed out")
        return None

    def _ping(self) -> bool:
        """Ping the modem to verify communication."""
        response = self._send_command(KISSCommand.CMD_PING)
        if response and response[0] == KISSResponse.RESP_PONG:
            logger.debug("Ping successful")
            return True
        return False

    def _get_identity(self) -> Optional[bytes]:
        """Get the modem's public key."""
        response = self._send_command(KISSCommand.CMD_GET_IDENTITY)
        if response and response[0] == KISSResponse.RESP_IDENTITY:
            return response[1]  # 32-byte public key
        return None

    def _get_version(self) -> Optional[str]:
        """Get firmware version."""
        response = self._send_command(KISSCommand.CMD_GET_VERSION)
        if response and response[0] == KISSResponse.RESP_VERSION:
            if len(response[1]) >= 2:
                major = response[1][0]
                minor = response[1][1]
                return f"{major}.{minor}"
        return None

    def _configure_radio(self) -> bool:
        """Configure radio parameters."""
        try:
            # Set radio params: Freq(4) + BW(4) + SF(1) + CR(1)
            data = struct.pack('<IIBB',
                self.radio_config.frequency,
                self.radio_config.bandwidth,
                self.radio_config.spreading_factor,
                self.radio_config.coding_rate
            )

            response = self._send_command(KISSCommand.CMD_SET_RADIO, data)
            if not response or response[0] == KISSResponse.RESP_ERROR:
                logger.error("Failed to set radio parameters")
                return False

            # Set TX power
            response = self._send_command(
                KISSCommand.CMD_SET_TX_POWER,
                bytes([self.radio_config.tx_power])
            )
            if not response or response[0] == KISSResponse.RESP_ERROR:
                logger.error("Failed to set TX power")
                return False

            # Set sync word (optional - not all firmware versions support this)
            response = self._send_command(
                KISSCommand.CMD_SET_SYNC_WORD,
                bytes([self.radio_config.sync_word])
            )
            if response and response[0] == KISSResponse.RESP_ERROR:
                # Check if it's an unknown command error
                if len(response[1]) > 0 and response[1][0] == KISSError.ERR_UNKNOWN_CMD:
                    logger.info("Sync word command not supported by firmware (using default)")
                else:
                    logger.warning(f"Failed to set sync word: error {response[1].hex() if response[1] else 'unknown'}")

            logger.info(f"Radio configured: {self.radio_config.frequency/1e6:.3f}MHz, "
                       f"BW={self.radio_config.bandwidth/1000}kHz, "
                       f"SF={self.radio_config.spreading_factor}, "
                       f"CR={self.radio_config.coding_rate}, "
                       f"TX={self.radio_config.tx_power}dBm")
            return True

        except Exception as e:
            logger.error(f"Radio configuration error: {e}")
            return False

    # =========================================================================
    # Public API
    # =========================================================================

    def set_rx_callback(self, callback: Callable[[bytes], None]):
        """Set the RX callback function.

        Callback signature: callback(packet_data: bytes)
        SNR/RSSI are available via get_last_snr()/get_last_rssi() after callback.
        """
        # Wrap the callback to match our internal 3-arg signature
        def wrapped_callback(data: bytes, snr: float, rssi: int):
            # Store SNR/RSSI first so they're available when callback queries them
            self._last_snr = snr
            self._last_rssi = rssi
            # Call the external callback with just the data
            callback(data)

        self.on_frame_received = wrapped_callback

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Set the event loop for async callback invocation."""
        self._loop = loop
        logger.debug("Event loop set for async callbacks")

    def send_frame(self, data: bytes) -> bool:
        """Send a packet frame."""
        if not self.is_connected:
            return False

        if self._tx_pending:
            logger.warning("TX already pending")
            return False

        if len(data) > 255:
            logger.error(f"Packet too large: {len(data)} bytes")
            return False

        with self._tx_lock:
            self._tx_pending = True

        frame = self._encode_frame(KISSCommand.CMD_DATA, data)
        try:
            self.serial_conn.write(frame)
            self.serial_conn.flush()
            logger.debug(f"TX queued: {len(data)} bytes")
            return True
        except Exception as e:
            logger.error(f"TX error: {e}")
            self._tx_pending = False
            return False

    async def send(self, data: bytes) -> None:
        """Async send interface for pymc_core compatibility."""
        if not self.send_frame(data):
            raise Exception("Failed to send frame")

        # Wait for TX_DONE
        timeout = 5.0
        start = time.time()
        while self._tx_pending and (time.time() - start) < timeout:
            await asyncio.sleep(0.01)

        if self._tx_pending:
            self._tx_pending = False
            raise Exception("TX timeout")

    def get_last_snr(self) -> float:
        """Get last received SNR."""
        return self._last_snr

    def get_last_rssi(self) -> int:
        """Get last received RSSI."""
        return self._last_rssi

    def begin(self):
        """Initialize radio (pymc_core LoRaRadio interface compatibility)."""
        if not self.is_connected:
            if not self.connect():
                raise Exception("Failed to initialize KISS modem")

    def sleep(self):
        """Put radio to sleep (not implemented for KISS modem)."""
        pass

    def wait_for_rx(self) -> bytes:
        """
        Wait for and return a received packet (blocking).

        This is used by pymc_core's Dispatcher for synchronous RX.
        Returns the raw packet data (without SNR/RSSI metadata).
        """
        # Use a queue to receive packets from the callback
        if not hasattr(self, '_rx_queue'):
            import queue
            self._rx_queue = queue.Queue()

            # Wrap existing callback to also queue packets
            original_callback = self.on_frame_received

            def queuing_callback(data: bytes, snr: float, rssi: int):
                self._rx_queue.put(data)
                if original_callback:
                    original_callback(data, snr, rssi)

            self.on_frame_received = queuing_callback

        # Block waiting for a packet
        try:
            return self._rx_queue.get(timeout=10.0)
        except Exception:
            return b''

    def get_identity(self) -> Optional[bytes]:
        """Get modem's cached identity (public key)."""
        return self._identity

    def get_stats(self) -> Dict[str, Any]:
        """Get modem statistics."""
        return {
            'rx_packets': self._stats.rx_packets,
            'tx_packets': self._stats.tx_packets,
            'rx_errors': self._stats.rx_errors,
            'last_snr': self._last_snr,
            'last_rssi': self._last_rssi,
        }

    # =========================================================================
    # Crypto Operations (delegated to modem)
    # =========================================================================

    def sign_data(self, data: bytes) -> Optional[bytes]:
        """Sign data using the modem's private key."""
        response = self._send_command(KISSCommand.CMD_SIGN_DATA, data)
        if response and response[0] == KISSResponse.RESP_SIGNATURE:
            return response[1]  # 64-byte signature
        return None

    def verify_signature(self, pubkey: bytes, signature: bytes, data: bytes) -> bool:
        """Verify an Ed25519 signature."""
        payload = pubkey + signature + data
        response = self._send_command(KISSCommand.CMD_VERIFY_SIGNATURE, payload)
        if response and response[0] == KISSResponse.RESP_VERIFY:
            return response[1][0] == 0x01 if len(response[1]) > 0 else False
        return False

    def key_exchange(self, remote_pubkey: bytes) -> Optional[bytes]:
        """Perform ECDH key exchange."""
        response = self._send_command(KISSCommand.CMD_KEY_EXCHANGE, remote_pubkey)
        if response and response[0] == KISSResponse.RESP_SHARED_SECRET:
            return response[1]  # 32-byte shared secret
        return None

    def encrypt_data(self, key: bytes, plaintext: bytes) -> Optional[Tuple[bytes, bytes]]:
        """Encrypt data. Returns (mac, ciphertext) or None."""
        payload = key + plaintext
        response = self._send_command(KISSCommand.CMD_ENCRYPT_DATA, payload)
        if response and response[0] == KISSResponse.RESP_ENCRYPTED:
            if len(response[1]) >= 2:
                mac = response[1][:2]
                ciphertext = response[1][2:]
                return mac, ciphertext
        return None

    def decrypt_data(self, key: bytes, mac: bytes, ciphertext: bytes) -> Optional[bytes]:
        """Decrypt data. Returns plaintext or None."""
        payload = key + mac + ciphertext
        response = self._send_command(KISSCommand.CMD_DECRYPT_DATA, payload)
        if response and response[0] == KISSResponse.RESP_DECRYPTED:
            return response[1]
        return None

    def sha256_hash(self, data: bytes) -> Optional[bytes]:
        """Compute SHA-256 hash."""
        response = self._send_command(KISSCommand.CMD_HASH, data)
        if response and response[0] == KISSResponse.RESP_HASH:
            return response[1]  # 32-byte hash
        return None

    def get_random(self, length: int) -> Optional[bytes]:
        """Get random bytes from modem (1-64 bytes)."""
        if length < 1 or length > 64:
            return None
        response = self._send_command(KISSCommand.CMD_GET_RANDOM, bytes([length]))
        if response and response[0] == KISSResponse.RESP_RANDOM:
            return response[1]
        return None

    # =========================================================================
    # Radio Status
    # =========================================================================

    def get_current_rssi(self) -> Optional[int]:
        """Get current RSSI reading."""
        response = self._send_command(KISSCommand.CMD_GET_CURRENT_RSSI)
        if response and response[0] == KISSResponse.RESP_CURRENT_RSSI:
            rssi = response[1][0] if len(response[1]) > 0 else 0
            if rssi > 127:
                rssi -= 256
            return rssi
        return None

    def is_channel_busy(self) -> bool:
        """Check if channel is busy (CAD)."""
        response = self._send_command(KISSCommand.CMD_IS_CHANNEL_BUSY)
        if response and response[0] == KISSResponse.RESP_CHANNEL_BUSY:
            return response[1][0] == 0x01 if len(response[1]) > 0 else False
        return False

    def get_airtime(self, packet_length: int) -> Optional[int]:
        """Get airtime in milliseconds for a packet."""
        response = self._send_command(KISSCommand.CMD_GET_AIRTIME, bytes([packet_length]))
        if response and response[0] == KISSResponse.RESP_AIRTIME:
            if len(response[1]) >= 4:
                return struct.unpack('<I', response[1][:4])[0]
        return None

    def get_noise_floor(self) -> Optional[int]:
        """Get noise floor in dBm."""
        response = self._send_command(KISSCommand.CMD_GET_NOISE_FLOOR)
        if response and response[0] == KISSResponse.RESP_NOISE_FLOOR:
            if len(response[1]) >= 2:
                nf = struct.unpack('<h', response[1][:2])[0]
                return nf
        return None

    def get_battery_voltage(self) -> Optional[int]:
        """Get battery voltage in millivolts."""
        response = self._send_command(KISSCommand.CMD_GET_BATTERY)
        if response and response[0] == KISSResponse.RESP_BATTERY:
            if len(response[1]) >= 2:
                return struct.unpack('<H', response[1][:2])[0]
        return None

    # =========================================================================
    # Context Manager
    # =========================================================================

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
