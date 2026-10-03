"""RF log ingest, mixed into MessageHandler.

handle_rf_log_data: each received packet's signal values, decode, packet hash, repeats of the bot's own transmissions, routing log line, web viewer capture, advert tracking and the RF row the correlation caches keep."""

from typing import Any

from .enums import PayloadType
from .utils import calculate_packet_hash


class RfLogMixin:
    """Mixed into MessageHandler."""

    _decode_authenticated_channel_identity: Any
    _insert_rf_row: Any
    _path_hex_to_nodes: Any
    _process_advertisement_packet: Any
    _remember_advert_rf: Any
    _remember_signal: Any
    _viewer_bridge: Any
    bot: Any
    decode_meshcore_packet: Any
    logger: Any
    rssi_cache: Any
    snr_cache: Any

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
                    self._remember_signal(self.snr_cache, packet_prefix, snr_value, "SNR")

                # Extract and cache RSSI if available
                if "rssi" in payload:
                    rssi_value = payload.get("rssi")
                    if packet_prefix and rssi_value is not None:
                        self._remember_signal(self.rssi_cache, packet_prefix, rssi_value, "RSSI")

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
                    self._insert_rf_row(rf_data, decoded_packet, current_time)

        except Exception as e:
            self.logger.error(f"Error handling RF log data: {e}")
