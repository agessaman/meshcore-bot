#!/usr/bin/env python3
"""
Message handling functionality for the MeshCore Bot
Processes incoming messages and routes them to appropriate command handlers
"""

import asyncio
import copy
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from . import packet_decode, scope_gate
from .contact_events import ContactEventsMixin
from .enums import (
    AdvertFlags,
    DeviceRole,
    PayloadType,
    PayloadVersion,  # noqa: F401
    RouteType,
)
from .graph_trace_helper import update_mesh_graph_from_trace_data  # noqa: F401  re-exported
from .mesh_graph_recorder import MeshGraphRecorderMixin
from .meshcore_payload_decode import channel_hash_for_key, decrypt_group_text  # noqa: F401  re-exported
from .models import MeshMessage
from .neighbors_discovery import upsert_zero_hop_observed_path_via_manager  # noqa: F401  re-exported
from .packet_decode import split_path_hex
from .region_warning import VERDICT_GLOBAL, VERDICT_SCOPED, VERDICT_UNKNOWN  # noqa: F401  re-exported
from .rf_correlation import RfCorrelationMixin
from .rf_match import (  # noqa: F401 - re-exported for callers and tests
    RF_MATCH_CHANNEL_AUTHENTICATED,
    RF_MATCH_EXACT,
    RF_MATCH_FALLBACK,
    RF_MATCH_KEY,
    RF_MATCH_PARTIAL,
    RF_MATCH_PAYLOAD,
    RF_MATCH_PUBKEY,
    rf_data_is_correlated,
)
from .security_utils import sanitize_input, sanitize_name
from .utils import (
    calculate_packet_hash,
    decode_path_len_byte,  # noqa: F401
    encode_path_len_byte,  # noqa: F401
    format_elapsed_display,
)

# Stand-in used when a channel message carries no "Name: " prefix to extract a
# sender from. It is not a node: every such message would share this identity.
CHANNEL_SENDER_FALLBACK = "Channel User"

def _signal_value(
    payload: dict[str, Any],
    metadata: dict[str, Any] | None,
    payload_keys: tuple[str, ...],
    metadata_keys: tuple[str, ...],
    convert: Callable[[Any], Any],
) -> Any:
    """Read a signal metric (SNR or RSSI) from an event payload, else its metadata.

    The first of ``payload_keys`` present in the payload decides, even when its
    value is None; metadata is consulted only when no payload key is present.
    A present value is passed through ``convert``.
    """
    for key in payload_keys:
        if key in payload:
            raw = payload.get(key)
            return convert(raw) if raw is not None else None
    if metadata:
        for key in metadata_keys:
            if key in metadata:
                raw = metadata.get(key)
                return convert(raw) if raw is not None else None
    return None




class MessageHandler(MeshGraphRecorderMixin, ContactEventsMixin, RfCorrelationMixin):
    """Handles incoming messages and routes them to command processors.

    This class is responsible for processing various types of MeshCore events,
    including contact messages (DMs), raw data packets, advertisement packets,
    and RF log data. It also maintains caches for SNR/RSSI data and correlates
    messages with routing information.
    """

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.logger = bot.logger
        # Cache for storing SNR and RSSI data from RF log events (bounded LRU)
        self.snr_cache: OrderedDict[str, float] = OrderedDict()
        self.rssi_cache: OrderedDict[str, float] = OrderedDict()

        # Load configuration for RF data correlation
        self.rf_data_timeout = float(bot.config.get("Bot", "rf_data_timeout", fallback="15.0"))
        self.message_timeout = float(bot.config.get("Bot", "message_correlation_timeout", fallback="10.0"))
        self.enhanced_correlation = bot.config.getboolean("Bot", "enable_enhanced_correlation", fallback=True)

        # Time-based cache for recent RF log data
        self.recent_rf_data: list[dict[str, Any]] = []

        # Adverts heard on RF, recorded before any await so NEW_CONTACT can find its own
        self._advert_rf: list[dict[str, Any]] = []

        # (public_key, packet_hash) of adverts NEW_CONTACT already added to the device
        self._new_contact_adds: dict[tuple[str, str], None] = {}

        # Authenticated channel rows outlive the short best-effort RF window.
        # A queued CHANNEL_MSG_RECV can be delayed while the mesh stays busy.
        self.channel_rf_data: list[dict[str, Any]] = []
        self._channel_rf_cache_timeout = max(300.0, self.rf_data_timeout * 4)

        # Enhanced RF data storage with better correlation
        self.rf_data_by_timestamp: dict[int | float, dict[str, Any]] = {}  # Index by timestamp for faster lookup
        self.rf_data_by_pubkey: dict[str, list[dict[str, Any]]] = {}  # Index by pubkey for exact matches

        # Cache memory management
        self._max_rf_cache_size = 1000  # Maximum entries per cache
        self._cache_cleanup_interval = 60  # Cleanup every 60 seconds
        self._last_cache_cleanup = time.time()

        # Maximum entries for SNR/RSSI LRU caches
        self._max_signal_cache_size = 1000

        # Multitest command listener (for collecting paths during listening window)
        self.multitest_listener: Any | None = None

        self.logger.info(f"RF Data Correlation: timeout={self.rf_data_timeout}s, enhanced={self.enhanced_correlation}")

    _match_scope = staticmethod(scope_gate.match_scope)

    _scope_fields_from_packet_info = staticmethod(scope_gate.scope_fields_from_packet_info)

    def _resolve_reply_scope_from_rf_data(
        self,
        recent_rf_data: dict[str, Any],
        packet_info: dict[str, Any] | None,
        scope_keys: dict[str, bytes],
    ) -> str | None:
        return scope_gate.resolve_reply_scope_from_rf_data(recent_rf_data, packet_info, scope_keys, self.logger)

    _effective_route_type_int = staticmethod(scope_gate.effective_route_type_int)

    _grp_txt_payload_type_int = staticmethod(scope_gate.grp_txt_payload_type_int)

    _is_confirmed_global_flood = staticmethod(scope_gate.is_confirmed_global_flood)

    _is_rf_data_scope_eligible = staticmethod(scope_gate.is_rf_data_scope_eligible)

    _classify_channel_flood_scope = staticmethod(scope_gate.classify_channel_flood_scope)

    async def _observe_flood_scope(
        self,
        *,
        sender_id: str | None,
        sender_pubkey: str | None,
        channel: str | None,
        sender_timestamp: Any,
        reply_scope: str | None,
        recent_rf_data: dict[str, Any] | None,
        packet_info: dict[str, Any] | None,
        scope_rf_data: dict[str, Any] | None,
        scope_packet_info: dict[str, Any] | None,
    ) -> None:
        """Hand this channel message's scope verdict to the region-warning monitor.

        Messages the radio cached from before this connection are skipped: on a
        reconnect they arrive as a burst of old traffic, and counting them would
        both distort the tallies and let a stale message earn someone a warning.
        """
        monitor = getattr(self.bot, "region_warning_monitor", None)
        if monitor is None:
            return
        if self._is_old_cached_message(sender_timestamp):
            return
        try:
            verdict = self._classify_channel_flood_scope(
                reply_scope=reply_scope,
                recent_rf_data=recent_rf_data,
                packet_info=packet_info,
                scope_rf_data=scope_rf_data,
                scope_packet_info=scope_packet_info,
            )
            await monitor.observe(
                verdict=verdict,
                sender_id=sender_id,
                sender_pubkey=sender_pubkey,
                channel=channel,
            )
        except Exception:
            self.logger.exception("Flood scope observation failed")

    def _is_old_cached_message(self, timestamp: Any) -> bool:
        """Check if a message timestamp indicates it's from before bot connection.

        Args:
            timestamp: Message sender timestamp (int, float, None, or 'unknown').

        Returns:
            bool: True if message is from before connection, False otherwise.
        """
        # If no connection time tracked, process all messages (backward compatibility)
        if not hasattr(self.bot, "connection_time") or self.bot.connection_time is None:
            return False

        # Handle invalid/unknown timestamps - process them (they might be current)
        if timestamp is None or timestamp == "unknown":
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

    def _transmission_evidence(self, packet_info: dict[str, Any]) -> dict[str, Any]:
        """What a received packet says about itself, for TransmissionTracker to recognize our own."""
        evidence: dict[str, Any] = {
            "payload_type": packet_info.get("payload_type"),
            "payload_hex": packet_info.get("payload_hex") or "",
        }
        channel = self._decode_authenticated_channel_identity(packet_info, include_text=True)
        if channel:
            evidence["channel_idx"] = channel["channel_idx"]
            evidence["channel_text"] = channel["channel_text"]
            evidence["channel_message"] = channel["channel_message"]
        return evidence

    def _find_contact(self, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        """The first radio contact matching ``predicate``, or None (also when there are no contacts)."""
        contacts = getattr(self.bot.meshcore, "contacts", None)
        if not contacts:
            return None
        return next((contact for contact in contacts.values() if predicate(contact)), None)

    def _viewer_bridge(self) -> Any:
        """The web viewer's bot-side bridge, or None when the viewer integration is off."""
        integration = getattr(self.bot, "web_viewer_integration", None)
        return integration.bot_integration if integration else None

    def _log_dm_routing_from_rf(self, message_pubkey: str) -> None:
        """Log the route of a DM's correlated RF packet (INFO), when one is found.

        Only for DMs without raw_hex, correlated by sender pubkey. This is
        logging only: the route actually attached to the message is decided
        later in handle_contact_message.
        """
        recent_rf_data = self.find_recent_rf_data(message_pubkey)
        if not (recent_rf_data and recent_rf_data.get("raw_hex")):
            return
        decoded_packet = self.decode_meshcore_packet(recent_rf_data["raw_hex"], recent_rf_data.get("payload"))
        if not decoded_packet:
            return
        self.logger.debug(f"Decoded packet for routing from RF data: {decoded_packet}")
        if not recent_rf_data.get("routing_info"):
            return
        if not rf_data_is_correlated(recent_rf_data):
            self.logger.debug("Ignoring routing info from an uncorrelated fallback packet")
            return
        routing_info = recent_rf_data["routing_info"]
        self.logger.debug(f"Found routing info: {routing_info}")
        path_len = routing_info.get("path_length", 0)
        if path_len > 0:
            path_hex = routing_info.get("path_hex", "")
            path_nodes = routing_info.get("path_nodes", [])
            route_type = routing_info.get("route_type", "Unknown")
            if path_nodes:
                path_info = f"{','.join(path_nodes)} ({path_len} hops via {route_type})"
            else:
                path_info = f"Path: {path_hex} ({path_len} hops via {route_type})"
            self.logger.info(f"🛣️  MESSAGE ROUTING: {path_info}")
        else:
            self.logger.info(f"📡 DIRECT MESSAGE: Direct via {routing_info.get('route_type', 'Unknown')}")

    async def handle_contact_message(self, event: Any, metadata: dict[str, Any] | None = None) -> None:
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

            payload = copy.deepcopy(event.payload) if hasattr(event, "payload") else None
            if payload is None:
                self.logger.warning("Contact message event has no payload")
                return

            # Debug: Log the full payload structure
            self.logger.debug(f"Contact message payload: {payload}")
            self.logger.debug(f"Payload keys: {list(payload.keys())}")
            self.logger.debug(f"Event metadata: {event.metadata if hasattr(event, 'metadata') else 'None'}")

            self.logger.info(
                f"Received DM from {sanitize_name(payload.get('pubkey_prefix', 'unknown'))}: {sanitize_name(payload.get('text', ''))}"
            )

            # Correlation keys for this DM's RF data (packet prefix, else sender pubkey).
            message_raw_hex = payload.get("raw_hex", "")
            message_packet_prefix = message_raw_hex[:32] if message_raw_hex else None
            message_pubkey = payload.get("pubkey_prefix", "")  # Keep for contact lookup
            if not message_packet_prefix and message_pubkey:
                self._log_dm_routing_from_rf(message_pubkey)

            # Get additional metadata - try multiple sources for SNR and RSSI
            snr: float | None = None
            rssi: int | None = None

            # Try to get SNR from payload first - check multiple possible field names
            snr = _signal_value(payload, metadata, ("SNR", "snr", "signal_to_noise", "signal_noise_ratio"), ("snr", "SNR"), float)

            # If still no SNR, try to get it from the cache using pubkey prefix from payload
            if snr is None:
                pubkey_prefix = payload.get("pubkey_prefix", "")
                if pubkey_prefix and pubkey_prefix in self.snr_cache:
                    snr = self.snr_cache[pubkey_prefix]
                    self.logger.debug(f"Retrieved cached SNR {snr} for pubkey {pubkey_prefix}")

            # Try to get RSSI from payload first
            rssi = _signal_value(payload, metadata, ("RSSI", "rssi", "signal_strength"), ("rssi", "RSSI"), int)

            # If still no RSSI, try to get it from the cache using pubkey prefix from payload
            if rssi is None:
                pubkey_prefix = payload.get("pubkey_prefix", "")
                if pubkey_prefix and pubkey_prefix in self.rssi_cache:
                    rssi = int(self.rssi_cache[pubkey_prefix])
                    self.logger.debug(f"Retrieved cached RSSI {rssi} for pubkey {pubkey_prefix}")

            # For DMs, we can't decode the encrypted packet, but we can get SNR/RSSI from the payload
            # For channel messages, we can decode the packet since they use shared keys
            self.logger.debug(f"Processing DM from packet prefix: {message_packet_prefix}, pubkey: {message_pubkey}")

            # DMs are encrypted with recipient's public key, so we can't decode the raw packet
            # But we can get SNR/RSSI from the message payload if available
            if "SNR" in payload:
                _snr = payload.get("SNR")
                snr = float(_snr) if _snr is not None else None
                self.logger.debug(f"Using SNR from DM payload: {snr}")
            elif "snr" in payload:
                _snr = payload.get("snr")
                snr = float(_snr) if _snr is not None else None
                self.logger.debug(f"Using SNR from DM payload: {snr}")

            if "RSSI" in payload:
                _rssi = payload.get("RSSI")
                rssi = int(_rssi) if _rssi is not None else None
                self.logger.debug(f"Using RSSI from DM payload: {rssi}")
            elif "rssi" in payload:
                _rssi = payload.get("rssi")
                rssi = int(_rssi) if _rssi is not None else None
                self.logger.debug(f"Using RSSI from DM payload: {rssi}")

            # Since DMs don't include SNR/RSSI in payload, try to get it from recent RF data
            # This is a fallback since RF data often comes right before/after the message
            if snr is None or rssi is None:
                recent_rf_data = self.find_recent_rf_data()
                if recent_rf_data:
                    self.logger.debug(f"Found recent RF data for DM: {recent_rf_data}")

                    if snr is None and recent_rf_data.get("snr") is not None:
                        snr = float(recent_rf_data["snr"])
                        self.logger.debug(f"Using SNR from recent RF data: {snr}")

                    if rssi is None and recent_rf_data.get("rssi") is not None:
                        rssi = int(recent_rf_data["rssi"])
                        self.logger.debug(f"Using RSSI from recent RF data: {rssi}")

            # For DMs, we can't determine the actual routing path from encrypted data
            # Use the path_len from the payload (255 means unknown/direct)
            path_len = payload.get("path_len", 255)
            path_info = "Direct (0 hops)" if path_len == 255 else f"Routed through {path_len} hops"

            self.logger.debug(f"DM path info: {path_info}")

            timestamp = payload.get("sender_timestamp", "unknown")

            # Look up contact name from pubkey prefix
            sender_id = sanitize_name(payload.get("pubkey_prefix", ""))
            sender_name = sender_id  # Default to sender_id
            # An empty prefix matches the first contact here; that is long-standing
            # behavior for the display name, but the full key below requires a prefix.
            contact = self._find_contact(lambda c: c.get("public_key", "").startswith(sender_id))
            if contact:
                # Use the contact name if available, otherwise use adv_name
                sender_name = sanitize_name(contact.get("name", contact.get("adv_name", sender_id)))

            # Get the full public key from contacts if available
            sender_pubkey = sender_id  # Default to pubkey prefix (same value as sender_id at this point)
            if sender_id and contact:
                sender_pubkey = contact.get("public_key", sender_id)
                self.logger.debug(f"Found full public key for {sender_name}: {sender_pubkey[:16]}...")

            # Sanitize message content to prevent injection attacks
            # Note: Firmware enforces 150-char limit at hardware level, so we disable length check
            # but still strip control characters for security
            message_content = payload.get("text", "")
            message_content = sanitize_input(message_content, max_length=None, strip_controls=True)

            # Elapsed: "Nms" when device clock is valid, or "Sync Device Clock" when
            # invalid (e.g. T-Deck before GPS sync: 0, future, or far in the past).
            translator = getattr(self.bot, "translator", None)
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
                path=path_info,
            )

            # Always decode and log path information for debugging (regardless of keywords)
            # Use same correlation as above so we attach this DM's path, not another packet's
            if message_packet_prefix:
                recent_rf_data = self.find_recent_rf_data(message_packet_prefix)
            elif message_pubkey:
                recent_rf_data = self.find_recent_rf_data(message_pubkey)
            else:
                recent_rf_data = self.find_recent_rf_data()

            # If we have RF data with routing information, update the path with that
            # instead — but only when the RF data is known to be this message's packet.
            # An uncorrelated fallback is simply the most recent packet heard, and
            # attributing its route here misreports the DM's path and feeds a wrong
            # routing_info to the path command (#80).
            if recent_rf_data and not rf_data_is_correlated(recent_rf_data):
                self.logger.debug(
                    "Skipping RF routing for this DM: correlation was a fallback, "
                    "so the route belongs to a different packet"
                )
            elif recent_rf_data and recent_rf_data.get("routing_info"):
                rf_routing = recent_rf_data["routing_info"]
                message.routing_info = rf_routing  # Path command uses this for multi-byte path (no re-parse)
                if rf_routing.get("path_length", 0) > 0:
                    path_nodes = rf_routing.get("path_nodes", [])
                    route_type = rf_routing.get("route_type", "Unknown")
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
                self.logger.debug(
                    f"Skipping old cached message from {sender_name} (timestamp: {timestamp}, connection: {self.bot.connection_time})"
                )
                return  # Read the message to clear cache, but don't process it

            await self.process_message(message)

        except Exception as e:
            self.logger.error(f"Error handling contact message: {e}")

    async def handle_raw_data(self, event: Any, metadata: dict[str, Any] | None = None) -> None:
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
            payload = copy.deepcopy(event.payload) if hasattr(event, "payload") else None
            if payload is None:
                self.logger.warning("RAW_DATA event has no payload")
                return

            self.logger.debug(f"📦 RAW_DATA EVENT RECEIVED: {payload}")
            self.logger.debug(f"📦 Metadata: {metadata}")

            # This should contain the full packet data we need
            if hasattr(payload, "data") or "data" in payload:
                raw_data = payload.get("data", payload.data if hasattr(payload, "data") else None)
                if raw_data:
                    # Try to decode this as a MeshCore packet
                    if isinstance(raw_data, str):
                        # Convert to hex if it's not already
                        if not raw_data.startswith("0x"):
                            raw_hex = raw_data
                        else:
                            raw_hex = raw_data[2:]  # Remove 0x prefix

                        # Decode the packet
                        packet_info = self.decode_meshcore_packet(raw_hex)
                        if packet_info:
                            self.logger.debug(f"✅ SUCCESSFULLY DECODED RAW PACKET: {packet_info}")

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

    async def handle_rf_log_data(self, event: Any, metadata: dict[str, Any] | None = None) -> None:
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

            payload = copy.deepcopy(event.payload) if hasattr(event, "payload") else None
            if payload is None:
                self.logger.warning("RF log data event has no payload")
                return

            # Extract SNR from payload
            if "snr" in payload:
                snr_value = payload.get("snr")

                # Use raw_hex prefix for correlation instead of trying to extract pubkey
                raw_hex = payload.get("raw_hex", "")
                packet_prefix = None

                if raw_hex:
                    # Use first 32 characters as correlation key (16 bytes)
                    # This provides unique identification while being consistent
                    packet_prefix = raw_hex[:32]
                    self.logger.debug(f"Using packet prefix for correlation: {packet_prefix}")

                # Keep pubkey_prefix for contact lookup (from metadata if available)
                pubkey_prefix = None
                if metadata and "pubkey_prefix" in metadata:
                    pubkey_prefix = metadata.get("pubkey_prefix")
                    if isinstance(pubkey_prefix, str):
                        self.logger.debug(f"Got pubkey_prefix from metadata: {pubkey_prefix[:16]}...")

                if packet_prefix and snr_value is not None:
                    # Cache the SNR value for this packet prefix (LRU-bounded)
                    self.snr_cache[packet_prefix] = snr_value
                    self.snr_cache.move_to_end(packet_prefix)
                    while len(self.snr_cache) > self._max_signal_cache_size:
                        self.snr_cache.popitem(last=False)
                    self.logger.debug(f"Cached SNR {snr_value} for packet prefix {packet_prefix}")

                # Extract and cache RSSI if available
                if "rssi" in payload:
                    rssi_value = payload.get("rssi")
                    if packet_prefix and rssi_value is not None:
                        # Cache the RSSI value for this packet prefix (LRU-bounded)
                        self.rssi_cache[packet_prefix] = rssi_value
                        self.rssi_cache.move_to_end(packet_prefix)
                        while len(self.rssi_cache) > self._max_signal_cache_size:
                            self.rssi_cache.popitem(last=False)
                        self.logger.debug(f"Cached RSSI {rssi_value} for packet prefix {packet_prefix}")

                # Store recent RF data with timestamp for SNR/RSSI matching only
                if packet_prefix:
                    import time

                    current_time = time.time()

                    # Store both raw packet data and extracted payload for analysis
                    raw_hex = payload.get("raw_hex", "")
                    extracted_payload = payload.get("payload", "")
                    payload_length = payload.get("payload_length", 0)

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
                            packet_hex_for_hash = (
                                extracted_payload if (extracted_payload and len(extracted_payload) > 0) else raw_hex
                            )

                            # Ensure we use the numeric payload_type value (not enum or string)
                            payload_type_value = decoded_packet.get("payload_type", None)
                            if payload_type_value is not None:
                                # Handle enum.value if it's an enum
                                if hasattr(payload_type_value, "value"):
                                    payload_type_value = payload_type_value.value
                                payload_type_value = int(payload_type_value)
                            packet_hash = calculate_packet_hash(packet_hex_for_hash, payload_type_value)

                            is_trace = decoded_packet.get("payload_type") == PayloadType.TRACE.value

                            # Check if this is a repeat of one of our transmissions
                            if (
                                hasattr(self.bot, "transmission_tracker")
                                and self.bot.transmission_tracker
                                and packet_hash
                                and packet_hash != "0000000000000000"
                            ):
                                # TRACE: RF path bytes are per-hop SNR×4, not repeater hashes — do not
                                # extract prefixes or record repeats from them.
                                if not is_trace:
                                    # Extract repeater prefixes from path - try multiple field names
                                    # decode_meshcore_packet returns 'path' not 'path_nodes'
                                    path_nodes = decoded_packet.get("path", [])
                                    # Also try 'path_nodes' field (from routing_info)
                                    if not path_nodes:
                                        path_nodes = decoded_packet.get("path_nodes", [])

                                    path_hex = decoded_packet.get("path_hex", "")

                                    # If we don't have path_nodes but have path_hex, convert it
                                    if not path_nodes and path_hex and len(path_hex) >= 2:
                                        path_nodes = self._path_hex_to_nodes(path_hex)

                                    path_string = ",".join(path_nodes) if path_nodes else None

                                    # Debug logging
                                    if path_nodes:
                                        self.logger.debug(
                                            f"📡 Extracting prefixes from path_nodes: {path_nodes}, path_hex: {path_hex}, bot_prefix: {self.bot.transmission_tracker.bot_prefix}"
                                        )

                                    # Try to match this packet hash to a transmission
                                    record = self.bot.transmission_tracker.match_packet_hash(
                                        packet_hash, current_time, self._transmission_evidence(decoded_packet)
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
                                            self.logger.info(
                                                f"📡 Found {len(prefixes)} repeater prefix(es) in repeat: {', '.join(prefixes)}"
                                            )
                                        elif path_nodes or path_hex:
                                            self.logger.debug(
                                                f"📡 Repeat detected but no repeater prefixes extracted (path_nodes: {path_nodes}, path_hex: {path_hex}, bot_prefix: {self.bot.transmission_tracker.bot_prefix})"
                                            )

                                        # Record the repeat
                                        for prefix in prefixes:
                                            self.bot.transmission_tracker.record_repeat(packet_hash, prefix)

                                        # If no prefixes but we have a path, it might be a direct repeat
                                        # (path contains our own node, so we filter it out)
                                        if not prefixes and (path_nodes or path_hex):
                                            # Still count as a repeat (heard by our radio)
                                            self.bot.transmission_tracker.record_repeat(packet_hash, None)
                                else:
                                    record = self.bot.transmission_tracker.match_packet_hash(
                                        packet_hash, current_time, self._transmission_evidence(decoded_packet)
                                    )
                                    if record:
                                        self.logger.debug(
                                            "📡 TRACE packet matched our transmission; skipping repeater prefix "
                                            "extraction (RF path holds SNR bytes, not node hashes)"
                                        )

                            pi = decoded_packet.get("path_info") or {}
                            trace_route_hashes = list(pi.get("path_hashes") or pi.get("path") or [])
                            trace_snr_db = list(pi.get("snr_data") or [])

                            if is_trace:
                                routing_info = {
                                    "path_length": len(trace_route_hashes)
                                    if trace_route_hashes
                                    else decoded_packet.get("path_len", 0),
                                    "path_len_byte": decoded_packet.get("path_len_byte"),
                                    "path_byte_length": decoded_packet.get("path_byte_length"),
                                    "bytes_per_hop": decoded_packet.get("bytes_per_hop", 1),
                                    "path_hex": decoded_packet.get("path_hex", ""),
                                    "path_nodes": trace_route_hashes,
                                    "trace_route_hashes": trace_route_hashes,
                                    "trace_snr_db": trace_snr_db,
                                    "trace_snr_path_hex": decoded_packet.get("path_hex", ""),
                                    "route_type": decoded_packet.get("route_type_name", "Unknown"),
                                    "payload_length": payload_length,
                                    "payload_type": decoded_packet.get("payload_type_name", "Unknown"),
                                    "packet_hash": packet_hash,
                                }
                            else:
                                routing_info = {
                                    "path_length": decoded_packet.get("path_len", 0),
                                    "path_len_byte": decoded_packet.get("path_len_byte"),
                                    "path_byte_length": decoded_packet.get("path_byte_length"),
                                    "bytes_per_hop": decoded_packet.get("bytes_per_hop", 1),
                                    "path_hex": decoded_packet.get("path_hex", ""),
                                    "path_nodes": decoded_packet.get("path", []),
                                    "route_type": decoded_packet.get("route_type_name", "Unknown"),
                                    "payload_length": payload_length,
                                    "payload_type": decoded_packet.get("payload_type_name", "Unknown"),
                                    "packet_hash": packet_hash,
                                }
                            # Validate path consistency (path_byte_length, path_hex, path_nodes, bytes_per_hop)
                            if not is_trace:
                                path_len = routing_info["path_length"]
                                path_byte_len = routing_info.get("path_byte_length")
                                path_hex_str = routing_info.get("path_hex", "")
                                path_nodes_list = routing_info.get("path_nodes") or []
                                bph = routing_info.get("bytes_per_hop", 1) or 1
                                expected_hex_len = (
                                    (path_byte_len * 2) if path_byte_len is not None else (path_len * bph * 2)
                                )
                                if path_len > 0 and path_hex_str:
                                    if len(path_hex_str) != expected_hex_len:
                                        self.logger.warning(
                                            "Path length mismatch: path_hex has %d hex chars, expected %d (path_byte_length=%s, path_length=%s, bytes_per_hop=%s)",
                                            len(path_hex_str),
                                            expected_hex_len,
                                            path_byte_len,
                                            path_len,
                                            bph,
                                        )
                                    if path_nodes_list and len(path_nodes_list) != path_len:
                                        self.logger.warning(
                                            "Path nodes count mismatch: %d nodes, path_length=%d",
                                            len(path_nodes_list),
                                            path_len,
                                        )
                                    if (
                                        path_nodes_list
                                        and bph >= 1
                                        and any(len(str(n)) != bph * 2 for n in path_nodes_list)
                                    ):
                                        self.logger.warning(
                                            "Path node width mismatch: bytes_per_hop=%d expects %d hex chars per node, nodes=%s",
                                            bph,
                                            bph * 2,
                                            path_nodes_list[:5],
                                        )
                            # Log the routing information for analysis
                            rf_path_bytes = decoded_packet.get("path_byte_length") or 0
                            trace_has_route = bool(trace_route_hashes)
                            trace_has_snr_path = rf_path_bytes > 0

                            if is_trace and (trace_has_route or trace_has_snr_path):
                                route_part = (
                                    f"Trace route: {','.join(h.lower() for h in trace_route_hashes)}"
                                    if trace_route_hashes
                                    else "Trace route: (none decoded yet)"
                                )
                                snr_part = ""
                                if trace_snr_db:
                                    snr_fmt = ",".join(f"{v:.2f}" for v in trace_snr_db)
                                    snr_part = f" | Trace SNR (dB): {snr_fmt}"
                                elif routing_info.get("trace_snr_path_hex"):
                                    snr_part = (
                                        f" | Trace SNR path (raw hex, int8×4 per hop): "
                                        f"{routing_info['trace_snr_path_hex']}"
                                    )
                                hops_display = (
                                    len(trace_route_hashes) if trace_route_hashes else decoded_packet.get("path_len", 0)
                                )
                                log_message = (
                                    f"🛣️  ROUTING INFO: {routing_info['route_type']} | {route_part}{snr_part} "
                                    f"({hops_display} route hops, {rf_path_bytes} RF path bytes) | "
                                    f"Payload: {routing_info['payload_length']} bytes | Type: {routing_info['payload_type']}"
                                )
                                self.logger.info(log_message)
                            elif routing_info["path_length"] > 0:
                                # Use path_nodes when present (multi-byte); else chunk path_hex
                                path_nodes_list = routing_info.get("path_nodes") or []
                                if path_nodes_list:
                                    formatted_path = ",".join(str(n).lower() for n in path_nodes_list)
                                else:
                                    path_hex = routing_info["path_hex"]
                                    path_nodes_fmt = self._path_hex_to_nodes(path_hex)
                                    formatted_path = ",".join(path_nodes_fmt)
                                path_bytes_str = decoded_packet.get("path_byte_length", routing_info["path_length"])
                                log_message = f"🛣️  ROUTING INFO: {routing_info['route_type']} | Path: {formatted_path} ({routing_info['path_length']} hops, {path_bytes_str} bytes) | Payload: {routing_info['payload_length']} bytes | Type: {routing_info['payload_type']}"
                                self.logger.info(log_message)
                            else:
                                log_message = f"📡 DIRECT MESSAGE: {routing_info['route_type']} | Type: {routing_info['payload_type']}"
                                self.logger.info(log_message)

                            # Capture full packet data for web viewer (for all packets)
                            if viewer := self._viewer_bridge():
                                decoded_packet["routing_info"] = routing_info
                                if is_trace and trace_route_hashes:
                                    decoded_packet["path"] = list(trace_route_hashes)
                                    decoded_packet["path_len"] = len(trace_route_hashes)
                                # Use extracted_payload which is the full MeshCore packet
                                # (header + path_len + path + payload, without RF wrapper)
                                decoded_packet["raw_packet_hex"] = extracted_payload if extracted_payload else raw_hex
                                decoded_packet["packet_hash"] = packet_hash
                                decoded_packet["snr"] = snr_value
                                if "rssi" in payload:
                                    decoded_packet["rssi"] = payload.get("rssi")
                                viewer.capture_full_packet_data(decoded_packet)

                            # Process ADVERT packets for contact tracking (regardless of path length)
                            if routing_info["payload_type"] == "ADVERT":
                                # Add routing_info to decoded_packet so it's available in _process_advertisement_packet
                                decoded_packet["routing_info"] = routing_info
                                # Create signal info from available data
                                signal_info = {
                                    "snr": snr_value,
                                    "rssi": payload.get("rssi") if "rssi" in payload else None,
                                    "hops": routing_info["path_length"],
                                }
                                self._remember_advert_rf(
                                    decoded_packet, routing_info, packet_hash, signal_info, current_time
                                )
                                await self._process_advertisement_packet(decoded_packet, signal_info)

                    # Prefer library-provided scope fields (already parsed by meshcore-py).
                    # The library's parsePacketPayload populates these directly from the
                    # inner MeshCore packet, avoiding any raw_hex prefix/offset issues.
                    _lib_route_type = payload.get("route_type")  # int: 0=TC_FLOOD, 1=FLOOD
                    _lib_tc_hex = payload.get("transport_code")  # hex str e.g. "26f10000"
                    _lib_payload_type = payload.get("payload_type")  # int
                    _lib_pkt_payload = payload.get("pkt_payload")  # bytes after path

                    # Compute transport code1 (uint16 LE) from library hex string
                    _lib_tc_code1 = None
                    if _lib_tc_hex and len(_lib_tc_hex) >= 4:
                        try:
                            _lib_tc_code1 = int.from_bytes(bytes.fromhex(_lib_tc_hex[:4]), "little")
                        except ValueError:
                            pass

                    # pkt_payload may be bytes or hex string depending on library version
                    _lib_pkt_hex = None
                    if isinstance(_lib_pkt_payload, bytes):
                        _lib_pkt_hex = _lib_pkt_payload.hex()
                    elif isinstance(_lib_pkt_payload, str) and _lib_pkt_payload:
                        _lib_pkt_hex = _lib_pkt_payload

                    rf_data = {
                        "timestamp": current_time,
                        "packet_prefix": packet_prefix,  # Use packet prefix for correlation
                        "pubkey_prefix": pubkey_prefix,  # Keep for contact lookup
                        "snr": snr_value,
                        "rssi": payload.get("rssi") if "rssi" in payload else None,
                        "raw_hex": raw_hex,  # Full packet data
                        "payload": extracted_payload,  # Extracted payload
                        "payload_length": payload_length,  # Payload length
                        "routing_info": routing_info,  # Extracted routing information
                        "packet_hash": packet_hash,  # Packet hash for tracking same message via different paths
                        # Fields for TC_FLOOD scope matching — use library values first, decoded_packet as fallback
                        "route_type_int": _lib_route_type
                        if _lib_route_type is not None
                        else (decoded_packet.get("route_type") if decoded_packet else None),
                        "transport_code1": _lib_tc_code1
                        if _lib_tc_code1 is not None
                        else ((decoded_packet.get("transport_codes") or {}).get("code1") if decoded_packet else None),
                        "payload_type_int": _lib_payload_type
                        if _lib_payload_type is not None
                        else (decoded_packet.get("payload_type") if decoded_packet else None),
                        "scope_payload_hex": _lib_pkt_hex
                        if _lib_pkt_hex
                        else (decoded_packet.get("payload_hex") if decoded_packet else None),
                    }
                    self._cache_authenticated_channel_rf_data(
                        rf_data, decoded_packet, current_time
                    )
                    if rf_data.get("route_type_int") == 0:
                        self.logger.debug(
                            "TC_FLOOD scope fields: tc_code1=%s payload_type=%s payload_hex_prefix=%s",
                            rf_data.get("transport_code1"),
                            rf_data.get("payload_type_int"),
                            (rf_data.get("scope_payload_hex") or "")[:16],
                        )
                    self.recent_rf_data.append(rf_data)

                    # Update correlation indexes
                    self.rf_data_by_timestamp[current_time] = rf_data
                    if packet_prefix:
                        if packet_prefix not in self.rf_data_by_pubkey:
                            self.rf_data_by_pubkey[packet_prefix] = []
                        self.rf_data_by_pubkey[packet_prefix].append(rf_data)

                    # Clean up old data from all indexes
                    self._cleanup_stale_cache_entries(current_time)

                    self.logger.debug(f"Stored recent RF data with routing info: {rf_data}")

        except Exception as e:
            self.logger.error(f"Error handling RF log data: {e}")

    def extract_path_from_raw_hex(self, raw_hex: str, expected_hops: int) -> str | None:
        """Extract path information directly from raw hex data.

        Attempts to find a sequence of node IDs in the raw packet data that matches
        the expected number of hops.

        Args:
            raw_hex: Raw packet data as a hex string.
            expected_hops: The expected number of hops in the path.

        Returns:
            str | None: Comma-separated path string if found, None otherwise.
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
                (8, 14),  # Position 8-13 (3 bytes)
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
                                node_hex = path_hex[i : i + 2]
                                path_nodes.append(node_hex)

                        if len(path_nodes) == expected_hops:
                            path_string = ",".join(path_nodes)
                            self.logger.debug(f"Found path at position {start}-{end}: {path_string}")
                            return path_string

            # If no exact match, try to find any 3-byte pattern that looks like a path
            # But skip the header area
            for i in range(min_start, len(raw_hex) - 6, 2):
                path_hex = raw_hex[i : i + 6]
                if len(path_hex) == 6:
                    # Check if this looks like a valid path (all hex chars)
                    if all(c in "0123456789abcdef" for c in path_hex.lower()):
                        path_nodes = [path_hex[j : j + 2] for j in range(0, 6, 2)]
                        path_string = ",".join(path_nodes)
                        self.logger.debug(f"Found potential path at position {i}: {path_string}")
                        return path_string

            return None

        except Exception as e:
            self.logger.debug(f"Error extracting path from raw hex: {e}")
            return None

    def decode_meshcore_packet(self, raw_hex: str, payload_hex: str | None = None) -> dict | None:
        """Decode a MeshCore packet; see :func:`modules.packet_decode.decode_meshcore_packet`."""
        return packet_decode.decode_meshcore_packet(
            raw_hex, payload_hex, prefix_hex_chars=self._prefix_hex_chars(), logger=self.logger
        )

    def parse_advert(self, payload: bytes) -> dict[str, Any]:
        """Parse advert payload - matches C++ AdvertDataHelpers.h implementation"""
        try:
            # Validate minimum payload size
            if len(payload) < 101:
                self.logger.error(f"ADVERT payload too short: {len(payload)} bytes")
                return {}

            # advert header
            pub_key = payload[0:32]
            timestamp = int.from_bytes(payload[32 : 32 + 4], "little")
            signature = payload[36 : 36 + 64]

            # appdata - parse according to C++ AdvertDataParser
            app_data = payload[100:]
            if len(app_data) == 0:
                self.logger.error("ADVERT has no app data")
                return {}

            flags_byte = app_data[0]

            # Bit tests match firmware AdvertDataParser (do not use AdvertFlags(flags_byte):
            # enum.Flag rejects some valid uint8 values, e.g. corrupt wires or type nibble > 4).
            has_latlon = (flags_byte & AdvertFlags.ADV_LATLON_MASK.value) != 0
            has_feat1 = (flags_byte & AdvertFlags.ADV_FEAT1_MASK.value) != 0
            has_feat2 = (flags_byte & AdvertFlags.ADV_FEAT2_MASK.value) != 0
            has_name = (flags_byte & AdvertFlags.ADV_NAME_MASK.value) != 0

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
            if has_latlon:
                if len(app_data) < i + 8:
                    self.logger.error(f"ADVERT with location flag too short: {len(app_data)} bytes")
                    return advert

                lat = int.from_bytes(app_data[i : i + 4], "little", signed=True)
                lon = int.from_bytes(app_data[i + 4 : i + 8], "little", signed=True)
                advert.update({"lat": round(lat / 1000000.0, 6), "lon": round(lon / 1000000.0, 6)})
                i += 8

            # Parse feat1 data if present
            if has_feat1:
                if len(app_data) < i + 2:
                    self.logger.error(f"ADVERT with feat1 flag too short: {len(app_data)} bytes")
                    return advert
                feat1 = int.from_bytes(app_data[i : i + 2], "little")
                advert.update({"feat1": feat1})
                i += 2

            # Parse feat2 data if present
            if has_feat2:
                if len(app_data) < i + 2:
                    self.logger.error(f"ADVERT with feat2 flag too short: {len(app_data)} bytes")
                    return advert
                feat2 = int.from_bytes(app_data[i : i + 2], "little")
                advert.update({"feat2": feat2})
                i += 2

            # Parse name data if present (matches C++ hasName())
            if has_name and len(app_data) >= i:
                name_len = len(app_data) - i
                if name_len > 0:
                    try:
                        # Decode name and handle potential null terminators
                        name = app_data[i:].decode("utf-8", errors="ignore").rstrip("\x00")
                        advert.update({"name": name})
                    except Exception as e:
                        self.logger.warning(f"Failed to decode ADVERT name: {e}")

            return advert

        except Exception as e:
            self.logger.warning(f"Error parsing ADVERT payload: {e}")
            return {}

    def _prefix_hex_chars(self) -> int:
        """Configured node-prefix width in hex chars (2 when the bot does not say)."""
        return getattr(getattr(self, "bot", None), "prefix_hex_chars", 2)

    def _path_bytes_to_nodes(self, path_bytes: bytes, prefix_hex_chars: int | None = None) -> tuple:
        """Chunk path bytes into ``(hex, node IDs)``; width defaults to the configured prefix length."""
        width = prefix_hex_chars if prefix_hex_chars is not None else self._prefix_hex_chars()
        return packet_decode.path_bytes_to_nodes(path_bytes, width)

    def _path_hex_to_nodes(self, path_hex: str) -> list[str]:
        """Chunk path_hex string into node list using configured prefix length, with legacy 2-char fallback.

        Use when path_hex comes from decoded packet path data (so chunk size should match decode layer).
        """
        if not path_hex or len(path_hex) < 2:
            return []
        n = self._prefix_hex_chars()
        if n <= 0:
            n = 2
        return split_path_hex(path_hex, n)

    def _get_path_from_rf_data(
        self, rf_data: dict[str, Any], payload_hex: str | None = None, packet_info: dict[str, Any] | None = None
    ) -> tuple[str | None, list[str] | None, int]:
        """Get path string, path nodes, and hop count from RF data (single source for path extraction).

        Prefers routing_info.path_nodes when present (no re-decode; correct multi-byte).
        Otherwise decodes (or uses provided packet_info) and gets path from decoder's 'path'
        or chunks path_hex using bytes_per_hop from the packet.

        Returns:
            (path_string, path_nodes, hops). path_nodes is a list for mesh graph; hops is path_length or 255.
        """
        routing_info = rf_data.get("routing_info") or {}
        path_nodes_list = routing_info.get("path_nodes")
        if path_nodes_list:
            path_str = ",".join(str(n).lower() for n in path_nodes_list)
            return (path_str, list(path_nodes_list), len(path_nodes_list))
        raw_hex = rf_data.get("raw_hex")
        if not raw_hex:
            return (None, None, 255)
        if packet_info is None:
            payload = payload_hex or rf_data.get("payload") or None
            packet_info = self.decode_meshcore_packet(raw_hex, str(payload) if payload is not None else None)
        if not packet_info:
            return (None, None, 255)
        hops = packet_info.get("path_len", 255)
        path_nodes_list = packet_info.get("path_nodes") or packet_info.get("path") or []
        if path_nodes_list:
            path_str = ",".join(str(n).lower() for n in path_nodes_list)
            return (path_str, list(path_nodes_list), len(path_nodes_list))
        path_hex = packet_info.get("path_hex", "")
        if path_hex and len(path_hex) >= 2:
            bytes_per_hop = packet_info.get("bytes_per_hop", 1)
            n = (bytes_per_hop * 2) if bytes_per_hop and bytes_per_hop >= 1 else 2
            path_nodes_list = split_path_hex(path_hex, n)
            if path_nodes_list:
                return (",".join(path_nodes_list), path_nodes_list, len(path_nodes_list))
        path_info = packet_info.get("path_info") or {}
        path_nodes_list = path_info.get("path") or []
        if path_nodes_list:
            path_str = ",".join(str(n).lower() for n in path_nodes_list)
            return (path_str, list(path_nodes_list), len(path_nodes_list))
        return (None, None, hops)

    def _process_packet_path(
        self, path_bytes: bytes, payload: bytes, route_type: RouteType, payload_type: PayloadType
    ) -> dict:
        return packet_decode.process_packet_path(
            path_bytes, payload, route_type, payload_type,
            prefix_hex_chars=self._prefix_hex_chars(), logger=self.logger,
        )

    def _get_route_type_name(self, route_type: int) -> str:
        return packet_decode.route_type_name(route_type)

    def get_payload_type_name(self, payload_type: int) -> str:
        return packet_decode.payload_type_name(payload_type)

    async def handle_channel_message(self, event: Any, metadata: dict[str, Any] | None = None) -> None:
        """Handle incoming channel message"""
        try:
            # Copy payload immediately to avoid segfault if event is freed
            import copy

            payload = copy.deepcopy(event.payload) if hasattr(event, "payload") else None
            if payload is None:
                self.logger.warning("Channel message event has no payload")
                return

            channel_idx = payload.get("channel_idx", 0)

            # Debug: Log the full payload structure
            self.logger.debug(f"Channel message payload: {payload}")
            self.logger.debug(f"Payload keys: {list(payload.keys())}")

            # Get sender information from text field if it's in "SENDER: message" format
            text = payload.get("text", "")
            sender_id = CHANNEL_SENDER_FALLBACK  # Default fallback

            # Try to extract sender from text field (e.g., "HOWL: Test" -> "HOWL")
            message_content = text  # Default to full text
            if ":" in text and not text.startswith(":"):
                parts = text.split(":", 1)
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
            snr: float | None = None
            rssi: int | None = None

            # Try to get SNR from payload first
            snr = _signal_value(payload, metadata, ("SNR", "snr"), ("snr", "SNR"), float)

            # If still no SNR, try to get it from the cache using pubkey prefix from payload
            if snr is None:
                pubkey_prefix = payload.get("pubkey_prefix", "")
                if pubkey_prefix and pubkey_prefix in self.snr_cache:
                    snr = self.snr_cache[pubkey_prefix]
                    self.logger.debug(f"Retrieved cached SNR {snr} for pubkey {pubkey_prefix}")

            # Try to get RSSI from payload first
            rssi = _signal_value(payload, metadata, ("RSSI", "rssi", "signal_strength"), ("rssi", "RSSI"), int)

            # If still no RSSI, try to get it from the cache using pubkey prefix from payload
            if rssi is None:
                pubkey_prefix = payload.get("pubkey_prefix", "")
                if pubkey_prefix and pubkey_prefix in self.rssi_cache:
                    rssi = int(self.rssi_cache[pubkey_prefix])
                    self.logger.debug(f"Retrieved cached RSSI {rssi} for pubkey {pubkey_prefix}")

            # For channel messages, we can decode the packet since they use shared channel keys
            # This gives us access to the actual routing information
            # Extract packet prefix from message raw_hex for correlation
            message_raw_hex = payload.get("raw_hex", "")
            message_packet_prefix = message_raw_hex[:32] if message_raw_hex else None
            message_pubkey = payload.get("pubkey_prefix", "")  # Keep for contact lookup
            self.logger.debug(
                f"Processing channel message from packet prefix: {message_packet_prefix}, pubkey: {message_pubkey}"
            )

            extended_timeout = self.rf_data_timeout * 2
            recent_rf_data = await self._correlate_channel_message_rf_data(
                message_packet_prefix,
                message_pubkey,
                payload,
                scope_eligible_only=False,
                extended_timeout=extended_timeout,
            )
            scope_rf_data = await self._correlate_channel_message_rf_data(
                message_packet_prefix,
                message_pubkey,
                payload,
                scope_eligible_only=True,
                extended_timeout=extended_timeout,
            )
            if scope_rf_data and scope_rf_data is not recent_rf_data:
                self.logger.debug(
                    "Using separate scope-eligible RF correlation (path/SNR source differs)"
                )

            packet_info: dict[str, Any] | None = None
            scope_packet_info: dict[str, Any] | None = None
            if recent_rf_data and recent_rf_data.get("raw_hex"):
                raw_hex = recent_rf_data["raw_hex"]
                self.logger.info(f"🔍 FOUND RF DATA: {len(raw_hex)} chars, starts with: {raw_hex[:32]}...")
                self.logger.debug(f"Full RF data: {raw_hex}")

                # Extract SNR/RSSI from the RF data
                if recent_rf_data.get("snr"):
                    snr = recent_rf_data["snr"]
                    self.logger.debug(f"Using SNR from RF data: {snr}")

                if recent_rf_data.get("rssi"):
                    rssi = recent_rf_data["rssi"]
                    self.logger.debug(f"Using RSSI from RF data: {rssi}")

                # Single path source: prefer routing_info, else decode/fallback via helper
                path_string = None
                hops = payload.get("path_len", 255)
                payload_hex = recent_rf_data.get("payload")
                packet_info = self.decode_meshcore_packet(raw_hex, payload_hex)
                packet_hash = recent_rf_data.get("packet_hash")
                if packet_hash and packet_info:
                    packet_info["packet_hash"] = packet_hash

                # A fallback correlation is just the most recent packet heard, not this
                # message's packet. Attributing its route here is how a multi-hop message
                # ended up recorded as a single direct hop (#80) — and it would write a
                # fabricated edge into the mesh graph. Leave the route unknown instead.
                route_is_attributable = rf_data_is_correlated(recent_rf_data)

                if not route_is_attributable:
                    # Terminal on purpose. Falling through would reach the raw-hex and
                    # routing_info fallbacks below, which would take the route from the
                    # unrelated packet — the exact bug this guards against (#80).
                    self.logger.debug(
                        "RF data for this channel message is an uncorrelated fallback; "
                        "not attributing its route (hops/path left unresolved)"
                    )
                    hops = payload.get("path_len", 255)
                    path_string = None
                elif packet_info and packet_info.get("path_len") is not None:
                    hops = packet_info.get("path_len", 0)
                    if packet_info.get("payload_type") == 9:  # TRACE packet
                        path_info = packet_info.get("path_info", {})
                        path_hashes = path_info.get("path_hashes") or path_info.get("path", [])
                        if path_hashes:
                            path_string = ",".join(path_hashes)
                            self.logger.debug(f"Path from TRACE packet: {path_string} ({len(path_hashes)} hops)")
                            if self._mesh_graph_capturing():
                                self._update_mesh_graph_from_trace(path_hashes, packet_info)
                        else:
                            path_string = "Direct" if hops == 0 else f"Unknown routing ({hops} hops)"
                            self.logger.debug(f"Path from TRACE packet: {path_string}")
                    else:
                        had_routing_nodes = bool((recent_rf_data.get("routing_info") or {}).get("path_nodes"))
                        path_string, path_nodes, hops = self._get_path_from_rf_data(
                            recent_rf_data, payload_hex=payload_hex, packet_info=packet_info
                        )
                        if path_string and path_nodes and self._mesh_graph_capturing():
                            self._update_mesh_graph(path_nodes, packet_info)
                        if path_string and not had_routing_nodes:
                            self.logger.debug(f"Path from fallback decode: {path_string} ({hops} hops)")
                else:
                    self.logger.debug("Packet decoding failed, trying direct hex or routing_info fallback")
                    path_string = self.extract_path_from_raw_hex(raw_hex, hops)
                    if (
                        not path_string
                        and recent_rf_data.get("routing_info")
                        and recent_rf_data["routing_info"].get("path_nodes")
                    ):
                        routing_info = recent_rf_data["routing_info"]
                        path_nodes = routing_info["path_nodes"]
                        hops = len(path_nodes)
                        path_string = ",".join(str(n).lower() for n in path_nodes)
                        self.logger.debug(f"Path from RF routing_info fallback: {path_string} ({hops} hops)")
            else:
                self.logger.warning("❌ NO RF DATA found for channel message after all correlation attempts")
                hops = payload.get("path_len", 255)
                path_string = None

            if scope_rf_data and scope_rf_data.get("raw_hex"):
                # Decode the full inner MeshCore packet (header + path + ciphertext).
                # scope_payload_hex is ciphertext-only for HMAC; do not pass it to decode_meshcore_packet.
                inner_packet_hex = scope_rf_data.get("payload")
                if inner_packet_hex:
                    scope_packet_info = self.decode_meshcore_packet(inner_packet_hex)
                if scope_rf_data.get("packet_hash") and scope_packet_info:
                    scope_packet_info["packet_hash"] = scope_rf_data["packet_hash"]

            # Scope matching: use scope-eligible RF only (never a stale ADVERT fallback).
            # The scope also has to come from *this* message's packet. The HMAC proves
            # the cached packet belongs to an allowed scope, not that this message does,
            # so an uncorrelated fallback would let a recent allowed-scope packet admit
            # an unrelated message past the flood_scopes allowlist.
            reply_scope: str | None = None
            cmd_mgr = getattr(self.bot, "command_manager", None)
            scope_keys = getattr(cmd_mgr, "flood_scope_keys", {})
            scope_rf_is_correlated = rf_data_is_correlated(scope_rf_data)
            if scope_rf_data and scope_keys:
                if scope_rf_is_correlated:
                    reply_scope = self._resolve_reply_scope_from_rf_data(
                        scope_rf_data, scope_packet_info, scope_keys
                    )
                else:
                    self.logger.info(
                        "Scope for this channel message is unknown: the only scope-eligible "
                        "RF data is an uncorrelated fallback from another packet, so it "
                        "cannot authorise a reply under flood_scopes"
                    )

            # Region-code observation happens here, ahead of the flood_scopes
            # allowlist below, because an unscoped message is exactly what that
            # allowlist drops — running it after the gate would blind the
            # monitor to the traffic it exists to measure.
            await self._observe_flood_scope(
                # A message with no "Name: " prefix has no attributable sender,
                # so it is counted but can never earn anyone a warning.
                sender_id=None if sender_id == CHANNEL_SENDER_FALLBACK else sender_id,
                sender_pubkey=payload.get("pubkey_prefix", ""),
                channel=channel_name,
                sender_timestamp=payload.get("sender_timestamp", 0),
                reply_scope=reply_scope,
                recent_rf_data=recent_rf_data,
                packet_info=packet_info,
                scope_rf_data=scope_rf_data,
                scope_packet_info=scope_packet_info,
            )

            # Allowlist enforcement: when flood_scopes is configured, only reply to
            # messages whose scope matched an entry.  Unscoped FLOOD is allowed only
            # when '*' (or equivalent) is explicitly listed.
            allow_global = getattr(cmd_mgr, "flood_scope_allow_global", False)
            # A '*'-only flood_scopes leaves scope_keys empty but still means an
            # allowlist is configured (global only). Gating on scope_keys alone let
            # that configuration skip authorisation entirely.
            if (scope_keys or allow_global) and reply_scope is None:
                if (
                    scope_rf_data
                    and scope_rf_is_correlated
                    and self._is_rf_data_scope_eligible(scope_rf_data, scope_packet_info)
                ):
                    self.logger.info("Ignoring TC_FLOOD: scope not in flood_scopes allowlist")
                    return
                # '*' permits *unscoped global* traffic, not traffic of unknown scope,
                # so it needs positive evidence that this message's own packet was
                # ordinary FLOOD. The general RF correlation carries that evidence for
                # the normal case; without it the scope is unknown and an allowlist
                # should fail closed rather than assume global.
                if allow_global and not self._is_confirmed_global_flood(
                    recent_rf_data,
                    packet_info,
                    scoped_traffic_in_window=scope_rf_data is not None,
                ):
                    self.logger.info(
                        "Ignoring channel message: flood_scopes lists '*', but a scoped "
                        "TC_FLOOD packet was heard alongside this message and it could "
                        "not be confirmed as unscoped FLOOD (scope unknown, not global)"
                    )
                    return

                if not allow_global:
                    if scope_rf_data is None:
                        self.logger.info(
                            "Ignoring channel message: no TC_FLOOD RF correlation for "
                            "flood_scopes allowlist (avoid replying on wrong scope)"
                        )
                    else:
                        self.logger.debug(
                            "Ignoring FLOOD: unscoped messages not permitted (add '*' to flood_scopes)"
                        )
                    return

            # Get the full public key from contacts if available
            sender_pubkey = payload.get("pubkey_prefix", "")
            if sender_pubkey:
                prefix = sender_pubkey
                contact = self._find_contact(lambda c: c.get("public_key", "").startswith(prefix))
                if contact:
                    sender_pubkey = contact.get("public_key", sender_pubkey)
                    self.logger.debug(f"Found full public key for {sender_id}: {sender_pubkey[:16]}...")

            # Elapsed: "Nms" when device clock is valid, or "Sync Device Clock" when invalid.
            _translator = getattr(self.bot, "translator", None)
            _elapsed = format_elapsed_display(payload.get("sender_timestamp"), _translator)

            # Convert to our message format
            message = MeshMessage(
                content=message_content,  # Use the extracted message content
                sender_id=sender_id,
                sender_pubkey=sender_pubkey,
                channel=channel_name,
                timestamp=payload.get("sender_timestamp", 0),
                snr=snr,
                rssi=rssi,
                hops=hops,
                path=path_string,  # Use the path information extracted from RF data
                elapsed=_elapsed,
                is_dm=False,
                reply_scope=reply_scope,
            )
            # Only a correlated packet's routing_info belongs to this message. The path
            # command reads message.routing_info directly, so an uncorrelated fallback
            # here would show a different packet's route to the user (#80).
            if (
                recent_rf_data
                and recent_rf_data.get("routing_info")
                and rf_data_is_correlated(recent_rf_data)
            ):
                message.routing_info = recent_rf_data["routing_info"]

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
            timestamp = payload.get("sender_timestamp", 0)
            if self._is_old_cached_message(timestamp):
                self.logger.debug(
                    f"Skipping old cached channel message from {sender_id} (timestamp: {timestamp}, connection: {self.bot.connection_time})"
                )
                return  # Read the message to clear cache, but don't process it

            # Process the message
            await self.process_message(message)

            # Capture for web viewer live monitor
            if viewer := self._viewer_bridge():
                try:
                    viewer.capture_channel_message(message)
                except Exception:
                    pass

        except Exception as e:
            self.logger.error(f"Error handling channel message: {e}")
            import traceback

            self.logger.error(traceback.format_exc())

    # CLI path discovery removed - focusing only on packet decoding

    async def _debug_decode_message_path(
        self, message: MeshMessage, sender_id: str, rf_data: dict[str, Any] | None
    ) -> None:
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

            pubkey_prefix = rf_data.get("pubkey_prefix", "")
            if not pubkey_prefix:
                self.logger.debug(f"No pubkey prefix for {sender_id}")
                return

            # Try to find the contact to get stored path information
            if getattr(self.bot.meshcore, "contacts", None):
                # By name first, then by pubkey prefix
                contact = self._find_contact(lambda c: c.get("adv_name") == sender_id) or self._find_contact(
                    lambda c: c.get("public_key", "").startswith(pubkey_prefix)
                )

                if contact:
                    out_path = contact.get("out_path", "")
                    out_path_len = contact.get("out_path_len", -1)

                    if out_path_len == 0:
                        self.logger.info(f"📡 {sender_id} → Direct connection")
                    elif out_path_len > 0:
                        bph = contact.get("out_bytes_per_hop")
                        if bph is None and out_path_len > 0 and out_path:
                            byte_len = len(out_path) // 2
                            if byte_len > 0 and (byte_len % out_path_len) == 0:
                                bph = byte_len // out_path_len
                        path_string = self._format_path_string(out_path, bytes_per_hop=bph)
                        self.logger.info(f"📡 {sender_id} → {path_string} ({out_path_len} hops)")
                    else:
                        self.logger.info(f"📡 {sender_id} → Path not set")
                else:
                    self.logger.info(f"📡 {sender_id} → Contact not found")
            else:
                self.logger.debug(f"No contacts available for {sender_id}")

        except Exception as e:
            self.logger.error(f"Error in debug path decoding: {e}")

    async def _debug_decode_packet_for_message(
        self, message: MeshMessage, sender_id: str, rf_data: dict[str, Any] | None
    ) -> None:
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

            raw_hex = rf_data.get("raw_hex", "")
            if not raw_hex:
                self.logger.debug(f"No raw_hex in RF data for {sender_id}")
                return

            self.logger.debug(f"Decoding packet for {sender_id} ({len(raw_hex)} chars)")

            # Log basic payload info if available
            extracted_payload = rf_data.get("payload", "")
            payload_length = rf_data.get("payload_length", 0)

            if extracted_payload:
                self.logger.debug(f"Payload: {payload_length} bytes")
            else:
                self.logger.debug("No payload data available")

        except Exception as e:
            self.logger.error(f"Error in debug packet decoding: {e}")

    def _format_path_string(self, hex_path: str, bytes_per_hop: int | None = None) -> str:
        return packet_decode.format_path_string(hex_path, bytes_per_hop, logger=self.logger)

    async def process_message(self, message: MeshMessage) -> None:
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
        if "stats" in self.bot.command_manager.commands:
            stats_command = self.bot.command_manager.commands["stats"]
            if stats_command:
                stats_command.record_message(message)
                stats_command.record_path_stats(message)

        # Check greeter command for public channel messages (BEFORE general message filtering)
        # This allows greeter to work on its own configured channels even if not in monitor_channels
        if self._channel_responses_allowed(message) and "greeter" in self.bot.command_manager.commands:
            greeter_command = self.bot.command_manager.commands["greeter"]
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
                    if (
                        hasattr(greeter_command, "last_response")
                        and greeter_command.last_response
                        or hasattr(self.bot.command_manager, "_last_response")
                        and self.bot.command_manager._last_response
                    ):
                        response_sent = True

                    # Record command execution in stats database
                    if "stats" in self.bot.command_manager.commands:
                        stats_command = self.bot.command_manager.commands["stats"]
                        if stats_command:
                            stats_command.record_command(message, "greeter", response_sent)
                except Exception as e:
                    self.logger.error(f"Error executing greeter command: {e}")

        # Now check if we should process this message for bot responses
        if not self.should_process_message(message):
            return

        # Handle respond_to_mentions for channel messages
        if not message.is_dm:
            _mention_mode = self.bot.config.get("Bot", "respond_to_mentions", fallback="also").strip().lower()
            if _mention_mode in ("also", "only"):
                import re

                _bot_name = self.bot.config.get("Bot", "bot_name", fallback="Bot")
                _mention = f"@[{_bot_name}]"
                _has_mention = _mention.lower() in message.content.lower()
                if _mention_mode == "only" and not _has_mention:
                    self.logger.debug(f"Ignoring channel message (respond_to_mentions=only, no mention of {_mention})")
                    return
                if _has_mention:
                    message.content = re.sub(re.escape(_mention), "", message.content, flags=re.IGNORECASE).strip()

        self.logger.info(
            f"Processing message: '{message.content}' from {message.sender_id} in {'DM' if message.is_dm else message.channel}"
        )

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
                if hasattr(self.bot, "translator"):
                    log_msg = self.bot.translator.translate("messages.keyword_matched", keyword=keyword)
                    self.logger.info(log_msg)
                else:
                    self.logger.info(f"Keyword '{keyword}' matched, responding")

                # Track if this is a help response
                if keyword == "help":
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
                if "stats" in self.bot.command_manager.commands:
                    stats_command = self.bot.command_manager.commands["stats"]
                    if stats_command:
                        # response is not None here, so we know a response will be sent
                        stats_command.record_command(message, keyword, True)

                # Generate command_id for repeat tracking (before sending)
                import time

                command_id = f"keyword_{keyword}_{message.sender_id}_{int(time.time())}"

                try:
                    success = await self.bot.command_manager.send_response(
                        message,
                        response,
                        command_id=command_id,
                    )

                    if not success:
                        self.logger.warning(
                            f"Failed to send keyword response for '{keyword}' to {message.sender_id if message.is_dm else message.channel}"
                        )
                except Exception as e:
                    self.logger.error(f"Error sending keyword response for '{keyword}': {e}", exc_info=True)
                    success = False

                # Capture keyword command data for web viewer
                if viewer := self._viewer_bridge():
                    try:
                        viewer.capture_command(
                            message, keyword, response, success, command_id
                        )
                    except Exception as e:
                        self.logger.debug(f"Failed to capture keyword data for web viewer: {e}")

        # Only execute commands if no help response was sent and no plugin command with response was matched
        # Help responses and plugin commands with responses should be the final response for that message
        # Plugin commands without responses (response is None) should still be executed
        if not help_response_sent and not plugin_command_with_response_matched:
            # After keyword handling, try RandomLine
            randomline_match = self.bot.command_manager.match_randomline(message)
            if randomline_match:
                key, response = randomline_match
                plugin_command_with_response_matched = True
                import time

                command_id = f"randomline_{key}_{message.sender_id}_{int(time.time())}"

                try:
                    success = await self.bot.command_manager.send_response(
                        message,
                        response,
                        command_id=command_id,
                    )

                    if not success:
                        self.logger.warning(
                            f"Failed to send randomline response for '{key}' to "
                            f"{message.sender_id if message.is_dm else message.channel}"
                        )
                except Exception as e:
                    self.logger.error(f"Error sending randomline response for '{key}': {e}", exc_info=True)
                    success = False

            else:
                # If no keyword or RandomLine match, try all other commands
                await self.bot.command_manager.execute_commands(message)

    def should_process_message(self, message: MeshMessage) -> bool:
        """Check if message should be processed by the bot"""
        # Check if bot is enabled
        if not self.bot.config.getboolean("Bot", "enabled"):
            return False

        # Check if sender is banned (starts-with matching)
        if self.bot.command_manager.is_user_banned(message.sender_id):
            self.logger.debug(f"Ignoring message from banned user: {message.sender_id}")
            return False

        # Channel-only pause (DM-only admin command); DMs still processed
        if not message.is_dm and not getattr(self.bot, "channel_responses_enabled", True):
            self.logger.debug("Ignoring non-DM message: channel responses paused")
            return False

        # Don't reply to messages from so far away the sender wont see response
        max_response_hops = max(1, self.bot.config.getint("Channels", "max_response_hops", fallback=64))
        if message.hops is not None:
            try:
                if int(message.hops) > max_response_hops:
                    self.logger.debug(
                        f"Ignoring message from {message.sender_id}: "
                        f"{message.hops} hops > max_response_hops ({max_response_hops})"
                    )
                    return False
            except (TypeError, ValueError):
                pass

        # Check if channel is monitored (with command override support)
        if not message.is_dm and message.channel:
            # Check if channel is in global monitor_channels
            if message.channel in self.bot.command_manager.monitor_channels:
                return True  # Global allow - all commands can work

            # Check if ANY command allows this channel (for selective access)
            for command_name, command in self.bot.command_manager.commands.items():
                if hasattr(command, "is_channel_allowed") and callable(command.is_channel_allowed):
                    if command.is_channel_allowed(message):
                        # At least one command allows this channel
                        self.logger.debug(f"Channel {message.channel} allowed by command '{command_name}' override")
                        return True

            # Channel not in global list and no command allows it
            self.logger.debug(
                f"Channel {message.channel} not in monitored channels: {self.bot.command_manager.monitor_channels}"
            )
            return False

        # Check if DMs are enabled
        if message.is_dm and not self.bot.config.getboolean("Channels", "respond_to_dms"):
            self.logger.debug("DMs are disabled")
            return False

        return True

    def _channel_responses_allowed(self, message: MeshMessage) -> bool:
        """True if channel-driven bot responses are allowed for this message (DMs always True here)."""
        if message.is_dm:
            return True
        return getattr(self.bot, "channel_responses_enabled", True)

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
            if hasattr(self.bot.meshcore, "contacts") and self.bot.meshcore.contacts:
                contact = None
                pubkey_prefix = rf_data.get("pubkey_prefix", "")

                # Look for contact by name first
                for _contact_key, contact_data in self.bot.meshcore.contacts.items():
                    if contact_data.get("adv_name") == sender_id:
                        contact = contact_data
                        break

                # If not found by name, try by pubkey prefix
                if not contact and pubkey_prefix:
                    for _contact_key, contact_data in self.bot.meshcore.contacts.items():
                        if contact_data.get("public_key", "").startswith(pubkey_prefix):
                            contact = contact_data
                            break

                if contact:
                    # Use the stored path information if available
                    out_path = contact.get("out_path", "")
                    out_path_len = contact.get("out_path_len", -1)

                    if out_path_len == 0:
                        self.logger.debug(f"Direct connection to {sender_id}")
                        return 0, "Direct"
                    elif out_path_len > 0:
                        # Format the path string (use stored bytes_per_hop for multi-byte paths)
                        bph = contact.get("out_bytes_per_hop")
                        if bph is None and out_path_len > 0 and out_path:
                            byte_len = len(out_path) // 2
                            if byte_len > 0 and (byte_len % out_path_len) == 0:
                                bph = byte_len // out_path_len
                        path_string = self._format_path_string(out_path, bytes_per_hop=bph)
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
            return 255, "Error"
