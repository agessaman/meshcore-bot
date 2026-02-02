"""
MeshTNC Serial Wrapper - CLI Mode with RXLOG parsing

This wrapper uses MeshTNC's CLI mode instead of KISS mode as a workaround
for the KISS RX bug where received packets are not forwarded to serial.

In CLI mode:
- RX packets come as RXLOG lines: "timestamp,RXLOG,rssi,snr,hex_data"
- TX packets are sent via KISS mode (temporarily switch, send, switch back)
"""

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, Optional

import serial

logger = logging.getLogger("MeshTNCSerial")

# KISS Protocol Constants (for TX only)
KISS_FEND = 0xC0
KISS_FESC = 0xDB
KISS_TFEND = 0xDC
KISS_TFESC = 0xDD


class MeshTNCSerial:
    """
    MeshTNC Serial Interface using CLI mode for RX and KISS for TX.

    Implements the same interface as KissSerialWrapper for compatibility
    with pymc_core's Dispatcher.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 1.0,
        radio_config: Optional[Dict[str, Any]] = None,
        on_frame_received: Optional[Callable[[bytes], None]] = None,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.radio_config = radio_config or {}

        self.serial_conn: Optional[serial.Serial] = None
        self.is_connected = False
        self.kiss_mode_active = False

        # RX handling
        self.on_frame_received = on_frame_received
        self.rx_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        # TX queue for KISS frames
        self.tx_queue = deque(maxlen=100)
        self.tx_thread: Optional[threading.Thread] = None
        self.tx_lock = threading.Lock()

        # Stats
        self.stats = {
            "frames_sent": 0,
            "frames_received": 0,
            "bytes_sent": 0,
            "bytes_received": 0,
            "last_rssi": None,
            "last_snr": None,
        }

        # Line buffer for CLI mode parsing
        self._line_buffer = ""

        # Event loop reference for thread-safe callbacks
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def connect(self) -> bool:
        """Connect to MeshTNC and configure radio."""
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

            logger.info(f"MeshTNC connected to {self.port} at {self.baudrate} baud")

            # Exit any existing KISS mode
            self._exit_kiss_mode()
            time.sleep(0.3)
            self._clear_buffer()

            # Configure radio
            if self.radio_config:
                if not self._configure_radio():
                    logger.error("Failed to configure radio")
                    return False

            # Enable rxlog for receiving packets
            self._send_command("rxlog on")
            time.sleep(0.2)
            self._clear_buffer()

            # Start RX thread (CLI mode parsing)
            self.rx_thread = threading.Thread(target=self._rx_worker, daemon=True)
            self.rx_thread.start()

            # Start TX thread
            self.tx_thread = threading.Thread(target=self._tx_worker, daemon=True)
            self.tx_thread.start()

            logger.info("MeshTNC configured in CLI mode with rxlog enabled")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to MeshTNC: {e}")
            self.is_connected = False
            return False

    def disconnect(self):
        """Disconnect from MeshTNC."""
        self.is_connected = False
        self.stop_event.set()

        if self.rx_thread and self.rx_thread.is_alive():
            self.rx_thread.join(timeout=2.0)
        if self.tx_thread and self.tx_thread.is_alive():
            self.tx_thread.join(timeout=2.0)

        if self.serial_conn and self.serial_conn.is_open:
            try:
                self._send_command("rxlog off")
            except:
                pass
            self.serial_conn.close()

        logger.info("MeshTNC disconnected")

    def _configure_radio(self) -> bool:
        """Configure radio settings via CLI."""
        try:
            freq_hz = self.radio_config.get("frequency", 910525000)
            bw_hz = self.radio_config.get("bandwidth", 62500)
            sf = self.radio_config.get("spreading_factor", 7)
            cr = self.radio_config.get("coding_rate", 5)
            sync_word = self.radio_config.get("sync_word", 0x12)

            # Convert to CLI format
            freq_mhz = freq_hz / 1_000_000
            bw_khz = bw_hz / 1000

            if isinstance(sync_word, int):
                sync_str = f"0x{sync_word:02X}"
            else:
                sync_str = str(sync_word)

            cmd = f"set radio {freq_mhz},{bw_khz},{sf},{cr},{sync_str}"
            logger.info(f"Configuring radio: {cmd}")

            response = self._send_command(cmd)
            logger.info(f"Radio config response: {response}")

            return True

        except Exception as e:
            logger.error(f"Radio configuration error: {e}")
            return False

    def _send_command(self, cmd: str) -> str:
        """Send a CLI command and return response."""
        if not self.serial_conn or not self.serial_conn.is_open:
            return ""

        try:
            self.serial_conn.write(f"{cmd}\r\n".encode('ascii'))
            self.serial_conn.flush()
            time.sleep(0.3)

            response = ""
            if self.serial_conn.in_waiting > 0:
                response = self.serial_conn.read(
                    self.serial_conn.in_waiting
                ).decode('utf-8', errors='ignore')

            return response.strip()

        except Exception as e:
            logger.error(f"Command error: {e}")
            return ""

    def _clear_buffer(self):
        """Clear serial buffer."""
        if self.serial_conn and self.serial_conn.is_open:
            try:
                if self.serial_conn.in_waiting > 0:
                    self.serial_conn.read(self.serial_conn.in_waiting)
            except:
                pass

    def _exit_kiss_mode(self):
        """Exit KISS mode if active."""
        if self.serial_conn and self.serial_conn.is_open:
            try:
                self.serial_conn.write(bytes([KISS_FEND, 0xFF, KISS_FEND]))
                self.serial_conn.flush()
                self.kiss_mode_active = False
            except:
                pass

    def _rx_worker(self):
        """Background thread for receiving and parsing RXLOG lines."""
        logger.info("RX worker started (CLI/RXLOG mode)")

        while not self.stop_event.is_set() and self.is_connected:
            try:
                if self.serial_conn and self.serial_conn.in_waiting > 0:
                    data = self.serial_conn.read(self.serial_conn.in_waiting)
                    text = data.decode('utf-8', errors='ignore')
                    self._line_buffer += text

                    # Process complete lines
                    while '\n' in self._line_buffer:
                        line, self._line_buffer = self._line_buffer.split('\n', 1)
                        line = line.strip()
                        if line:
                            self._process_line(line)
                else:
                    time.sleep(0.01)

            except Exception as e:
                if self.is_connected:
                    logger.error(f"RX worker error: {e}")
                break

        logger.info("RX worker stopped")

    def _process_line(self, line: str):
        """Process a CLI output line, looking for RXLOG entries."""
        # RXLOG format: timestamp,RXLOG,rssi,snr,hex_data
        # Example: 946688557,RXLOG,-91.00,10.00,15125A077ECD3CE859BF...

        if ',RXLOG,' not in line:
            return

        try:
            parts = line.split(',')
            if len(parts) >= 5 and parts[1] == 'RXLOG':
                rssi = float(parts[2])
                snr = float(parts[3])
                hex_data = parts[4]

                # Convert hex to bytes
                packet_data = bytes.fromhex(hex_data)

                # Update stats
                self.stats["frames_received"] += 1
                self.stats["bytes_received"] += len(packet_data)
                self.stats["last_rssi"] = rssi
                self.stats["last_snr"] = snr

                logger.debug(f"RX packet: {len(packet_data)} bytes, RSSI={rssi}, SNR={snr}")

                # Call the callback (thread-safe)
                if self.on_frame_received and len(packet_data) > 0:
                    self._invoke_callback(packet_data)

        except Exception as e:
            logger.debug(f"Failed to parse RXLOG line: {line} - {e}")

    def _invoke_callback(self, packet_data: bytes):
        """Invoke the RX callback in a thread-safe manner."""
        if not self.on_frame_received:
            return

        try:
            # Try to get the running loop (works if called from async context)
            try:
                loop = asyncio.get_running_loop()
                # We're in an async context, create task directly
                loop.create_task(self._async_callback_wrapper(packet_data))
                return
            except RuntimeError:
                pass

            # Use stored loop reference if available
            if self._loop and self._loop.is_running():
                # Schedule callback on the event loop from this thread
                self._loop.call_soon_threadsafe(
                    lambda: self._loop.create_task(
                        self._async_callback_wrapper(packet_data)
                    )
                )
            else:
                # Fallback: direct call (may not work with async callbacks)
                self.on_frame_received(packet_data)

        except Exception as e:
            logger.error(f"Error invoking callback: {e}")

    async def _async_callback_wrapper(self, packet_data: bytes):
        """Async wrapper to invoke the callback."""
        try:
            self.on_frame_received(packet_data)
        except Exception as e:
            logger.error(f"Error in frame received callback: {e}")

    def _tx_worker(self):
        """Background thread for transmitting packets."""
        logger.info("TX worker started")

        while not self.stop_event.is_set() and self.is_connected:
            try:
                if self.tx_queue:
                    with self.tx_lock:
                        if self.tx_queue:
                            packet = self.tx_queue.popleft()
                            self._transmit_packet(packet)
                else:
                    time.sleep(0.01)

            except Exception as e:
                if self.is_connected:
                    logger.error(f"TX worker error: {e}")
                break

        logger.info("TX worker stopped")

    def _transmit_packet(self, data: bytes):
        """Transmit a packet using KISS mode temporarily."""
        if not self.serial_conn or not self.serial_conn.is_open:
            return

        try:
            # Enter KISS mode
            self._send_command("serial mode kiss")
            time.sleep(0.5)
            self.kiss_mode_active = True

            # Encode as KISS frame
            kiss_frame = self._encode_kiss_frame(data)

            # Send
            self.serial_conn.write(kiss_frame)
            self.serial_conn.flush()

            self.stats["frames_sent"] += 1
            self.stats["bytes_sent"] += len(data)

            logger.debug(f"TX packet: {len(data)} bytes")

            # Wait for transmission to complete
            time.sleep(0.3)

            # Exit KISS mode and re-enable rxlog
            self._exit_kiss_mode()
            time.sleep(0.2)
            self._clear_buffer()
            self._send_command("rxlog on")
            time.sleep(0.1)
            self._clear_buffer()

        except Exception as e:
            logger.error(f"Transmit error: {e}")
            # Try to recover
            self._exit_kiss_mode()
            self._send_command("rxlog on")

    def _encode_kiss_frame(self, data: bytes) -> bytes:
        """Encode data as a KISS frame."""
        frame = bytearray([KISS_FEND, 0x00])  # FEND + Data command

        for byte in data:
            if byte == KISS_FEND:
                frame.extend([KISS_FESC, KISS_TFEND])
            elif byte == KISS_FESC:
                frame.extend([KISS_FESC, KISS_TFESC])
            else:
                frame.append(byte)

        frame.append(KISS_FEND)
        return bytes(frame)

    # =========================================================================
    # Interface methods compatible with KissSerialWrapper / LoRaRadio
    # =========================================================================

    def set_rx_callback(self, callback: Callable[[bytes], None]):
        """Set the RX callback function."""
        self.on_frame_received = callback
        # Capture the current event loop for thread-safe callbacks
        try:
            self._loop = asyncio.get_running_loop()
            logger.debug("RX callback set with event loop reference")
        except RuntimeError:
            # No loop running yet, will be set later
            logger.debug("RX callback set (no event loop yet)")

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Explicitly set the event loop for thread-safe callbacks."""
        self._loop = loop
        logger.debug("Event loop reference set explicitly")

    def send_frame(self, data: bytes) -> bool:
        """Queue a frame for transmission."""
        if not self.is_connected:
            return False

        with self.tx_lock:
            if len(self.tx_queue) < 100:
                self.tx_queue.append(data)
                return True

        logger.warning("TX queue full")
        return False

    async def send(self, data: bytes) -> None:
        """Async send interface for pymc_core compatibility."""
        if not self.send_frame(data):
            raise Exception("Failed to queue frame for transmission")
        return None

    def begin(self):
        """Initialize the radio (called by some interfaces)."""
        if not self.connect():
            raise Exception("Failed to initialize MeshTNC")

    def get_last_rssi(self) -> float:
        """Get last received RSSI."""
        return self.stats.get("last_rssi") or -999

    def get_last_snr(self) -> float:
        """Get last received SNR."""
        return self.stats.get("last_snr") or -999

    def get_stats(self) -> Dict[str, Any]:
        """Get interface statistics."""
        return self.stats.copy()

    def sleep(self):
        """Put radio to sleep (not supported)."""
        pass

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
