"""MeshCore packet decoding: header, transport codes, path and payload split.

Pure functions matching the firmware's Packet.cpp layout. MessageHandler keeps
its old methods as thin delegates that supply the configured node-prefix width
and its logger.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from .enums import PayloadType, PayloadVersion, RouteType


def decode_path_len_byte(path_len_byte: int, max_path_size: int = 64) -> tuple[int, int] | None:
    """Decode the RF packet path_len byte per firmware ``Packet::isValidPathLen``.

    Encoding: low 6 bits = hop count, high 2 bits = size code.
    ``bytes_per_hop = (path_len >> 6) + 1`` → 1, 2, 3, or 4 (4 is reserved and invalid).

    Args:
        path_len_byte: The single path_len byte from the packet.
        max_path_size: Max path bytes (default 64, matches ``MAX_PATH_SIZE``).

    Returns:
        ``(path_byte_length, bytes_per_hop)`` if the encoding is valid on the wire.
        ``None`` if reserved size class (4) or ``hop_count * bytes_per_hop > max_path_size``
        — matching MeshCore where ``readFrom`` rejects the packet (no legacy reinterpretation).
    """
    hop_count = path_len_byte & 63
    size_code = path_len_byte >> 6
    bytes_per_hop = size_code + 1  # 1, 2, 3, or 4
    if bytes_per_hop == 4:
        return None
    path_byte_length = hop_count * bytes_per_hop
    if path_byte_length > max_path_size:
        return None
    return (path_byte_length, bytes_per_hop)


def parse_trace_payload_route_hashes(payload: bytes) -> list[str]:
    """Extract TRACE route hash segments from mesh payload (after tag, auth, flags).

    Matches MeshCore: ``bytes_per_hash = 1 << (flags & 3)`` for bytes at ``payload[9:]``.
    If the tail length is not a multiple of ``bytes_per_hash``, falls back to 1-byte
    segments (same as MessageHandler._process_packet_path).

    Args:
        payload: Full mesh payload bytes (not including header/path).

    Returns:
        List of uppercase hex strings, one per hop hash.
    """
    if len(payload) < 9:
        return []
    flags = payload[8]
    path_hash_len = 1 << (flags & 3)
    if path_hash_len <= 0:
        path_hash_len = 1
    path_hashes_bytes = payload[9:]
    if not path_hashes_bytes:
        return []
    try:
        if len(path_hashes_bytes) % path_hash_len == 0:
            return [
                path_hashes_bytes[i : i + path_hash_len].hex().upper()
                for i in range(0, len(path_hashes_bytes), path_hash_len)
            ]
    except Exception:
        pass
    return [f"{b:02X}" for b in path_hashes_bytes]


def encode_path_len_byte(hop_count: int, bytes_per_hop: int) -> int:
    """Pack hop count and hash size into the single path_len wire byte (inverse of decode_path_len_byte).

    Firmware: low 6 bits = hop count, high 2 bits = size code with bytes_per_hop = (code + 1).
    Valid bytes_per_hop are 1, 2, or 3 (size code 4 is reserved).
    """
    if bytes_per_hop not in (1, 2, 3):
        raise ValueError(f"bytes_per_hop must be 1, 2, or 3, got {bytes_per_hop}")
    hop_count = int(hop_count) & 0x3F
    size_code = (int(bytes_per_hop) - 1) & 0x03
    return (size_code << 6) | hop_count


def calculate_packet_hash(raw_hex: str, payload_type: Optional[int] = None) -> str:
    """Calculate hash for packet identification - based on packet.cpp.

    Packet hashes are unique to the originally sent message, allowing
    identification of the same message arriving via different paths.

    Args:
        raw_hex: Raw packet data as hex string.
        payload_type: Optional payload type as integer (if None, extracted from header).
                      Must be numeric value (0-15).

    Returns:
        str: 16-character hex string (8 bytes) in uppercase, or "0000000000000000" on error.
    """
    try:
        # Parse the packet to extract payload type and payload data
        byte_data = bytes.fromhex(raw_hex)
        header = byte_data[0]

        # Get payload type from header (bits 2-5)
        if payload_type is None:
            payload_type = (header >> 2) & 0x0F
        else:
            # Ensure payload_type is an integer (handle enum.value if passed)
            if hasattr(payload_type, 'value'):
                payload_type = payload_type.value
            payload_type = int(payload_type) & 0x0F  # Ensure it's 0-15

        # Check if transport codes are present
        route_type = header & 0x03
        has_transport = route_type in [0x00, 0x03]  # TRANSPORT_FLOOD or TRANSPORT_DIRECT

        # Calculate path length offset dynamically based on transport codes
        offset = 1  # After header
        if has_transport:
            offset += 4  # Skip 4 bytes of transport codes

        # Validate we have enough bytes for path_len
        if len(byte_data) <= offset:
            return "0000000000000000"

        path_len_byte = byte_data[offset]
        offset += 1
        path_parts = decode_path_len_byte(path_len_byte)
        if path_parts is None:
            return "0000000000000000"
        path_byte_length, _ = path_parts

        # Validate we have enough bytes for the path
        if len(byte_data) < offset + path_byte_length:
            return "0000000000000000"

        # Skip past the path to get to payload
        payload_start = offset + path_byte_length

        # Validate we have payload data
        if len(byte_data) <= payload_start:
            return "0000000000000000"

        payload_data = byte_data[payload_start:]

        # Calculate hash exactly like MeshCore Packet::calculatePacketHash():
        # 1. Payload type (1 byte)
        # 2. Path length (2 bytes as uint16_t, little-endian) - ONLY for TRACE packets (type 9)
        # 3. Payload data
        hash_obj = hashlib.sha256()
        hash_obj.update(bytes([payload_type]))

        if payload_type == 9:  # PAYLOAD_TYPE_TRACE
            # C++ does: sha.update(&path_len, sizeof(path_len))
            # path_len is the raw wire byte (uint16_t in firmware), not the decoded byte count
            hash_obj.update(path_len_byte.to_bytes(2, byteorder='little'))

        hash_obj.update(payload_data)

        # Return first 16 hex characters (8 bytes) in uppercase
        return hash_obj.hexdigest()[:16].upper()
    except Exception:
        # Return default hash on error (caller should handle logging)
        return "0000000000000000"


def split_path_hex(path_hex: str, hex_chars: int) -> list[str]:
    """Split a hex path into lowercase node IDs of ``hex_chars`` each.

    When the path does not divide evenly (or yields nothing), fall back to
    one-byte (two-character) nodes, the legacy path encoding.
    """
    nodes = [path_hex[i : i + hex_chars].lower() for i in range(0, len(path_hex), hex_chars)]
    if (len(path_hex) % hex_chars) != 0 or not nodes:
        nodes = [path_hex[i : i + 2].lower() for i in range(0, len(path_hex), 2)]
    return nodes


def path_bytes_to_nodes(path_bytes: bytes, prefix_hex_chars: int) -> tuple[str, list[str]]:
    """Path bytes as ``(hex, uppercase node IDs)``, ``prefix_hex_chars`` per node.

    A non-positive width means one byte per node; see :func:`split_path_hex`
    for the fallback when the path does not divide evenly.
    """
    n = prefix_hex_chars if prefix_hex_chars > 0 else 2
    path_hex = path_bytes.hex()
    return path_hex, [node.upper() for node in split_path_hex(path_hex, n)]


def decode_meshcore_packet(
    raw_hex: str, payload_hex: str | None = None, *, prefix_hex_chars: int, logger: Any
) -> dict | None:
    """
    Decode a MeshCore packet from raw hex data - matches Packet.cpp exactly

    Args:
        raw_hex: Raw packet data as hex string (may be RF data or direct MeshCore packet)
        payload_hex: Optional extracted payload hex string (preferred over raw_hex)

    Returns:
        Decoded packet information or None if parsing fails
    """
    # Ensure these are always defined for error logging (BUG-028)
    byte_data: bytes = b""
    hex_data: str = ""
    try:
        # Use payload_hex if provided (this is the actual MeshCore packet)
        if payload_hex:
            logger.debug("Using provided payload_hex for decoding")
            hex_data = payload_hex
        elif raw_hex:
            logger.debug("Using raw_hex for decoding")
            hex_data = raw_hex
        else:
            logger.debug("No packet data provided for decoding")
            return None

        # Remove 0x prefix if present (like in your other project)
        if hex_data.startswith("0x"):
            hex_data = hex_data[2:]

        byte_data = bytes.fromhex(hex_data)

        # Validate minimum packet size
        if len(byte_data) < 2:
            logger.error(f"Packet too short: {len(byte_data)} bytes")
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
            logger.error(f"Packet too short for path_len at offset {offset}: {len(byte_data)} bytes")
            return None

        path_len_byte = byte_data[offset]
        offset += 1
        # Decode per firmware: low 6 bits = hop count, high 2 bits = size code (bytes_per_hop = code+1)
        path_parts = decode_path_len_byte(path_len_byte)
        if path_parts is None:
            logger.debug("decode_meshcore_packet: invalid path_len byte (firmware would reject)")
            return None
        path_byte_length, bytes_per_hop = path_parts

        # Check if we have enough data for the full path
        if len(byte_data) < offset + path_byte_length:
            logger.error(
                f"Packet too short for path (need {offset + path_byte_length}, have {len(byte_data)})"
            )
            return None

        # Extract path
        path_bytes = byte_data[offset : offset + path_byte_length]
        offset += path_byte_length

        # Remaining data is payload
        payload = byte_data[offset:]

        # Extract payload version (bits 6-7)
        payload_version = PayloadVersion((header >> 6) & 0x03)

        # Only accept VER_1 (version 0)
        if payload_version != PayloadVersion.VER_1:
            logger.warning(
                f"Encountered an unknown packet version. Version: {payload_version.value} RAW: {hex_data}"
            )
            return None

        # Extract payload type (bits 2-5)
        payload_type = PayloadType((header >> 2) & 0x0F)

        # Chunk path by bytes_per_hop from packet (1, 2, or 3)
        path_hex, path_values = path_bytes_to_nodes(path_bytes, bytes_per_hop * 2)

        # Process path based on packet type
        path_info = process_packet_path(
                path_bytes, payload, route_type, payload_type, prefix_hex_chars=prefix_hex_chars, logger=logger
            )

        # Extract transport codes if present (only for TRANSPORT_FLOOD and TRANSPORT_DIRECT)
        transport_codes = None
        if has_transport and len(byte_data) >= 5:  # header(1) + transport(4)
            transport_bytes = byte_data[1:5]
            transport_codes = {
                "code1": int.from_bytes(transport_bytes[0:2], byteorder="little"),
                "code2": int.from_bytes(transport_bytes[2:4], byteorder="little"),
                "hex": transport_bytes.hex(),
            }

        packet_info = {
            "header": f"0x{header:02x}",
            # Raw values for backward compatibility
            "route_type": route_type.value,
            "route_type_name": route_type.name,
            "payload_type": payload_type.value,
            "payload_type_name": payload_type.name,
            "payload_version": payload_version.value,
            # Enum objects for improved type safety
            "route_type_enum": route_type,
            "payload_type_enum": payload_type,
            "payload_version_enum": payload_version,
            # Transport and path information
            "has_transport_codes": has_transport,
            "transport_codes": transport_codes,
            "transport_size": 4 if has_transport else 0,
            "path_len": len(path_values),  # Hop count for display / routing_info
            "path_len_byte": path_len_byte,  # Raw wire byte (same as firmware Packet path_len)
            "path_byte_length": path_byte_length,  # Path bytes (for logs showing "X bytes")
            "bytes_per_hop": bytes_per_hop,  # For multi-byte path storage/retrieval
            "path_info": path_info,
            "path": path_values,  # For backward compatibility
            "path_hex": path_hex,
            "payload_hex": payload.hex(),
            "payload_bytes": len(payload),
        }

        logger.debug(
            f"Successfully decoded: route={packet_info.get('route_type_name')}, type={packet_info.get('payload_type_name')}"
        )
        return packet_info

    except Exception as e:
        # Log as ERROR not DEBUG so we can see what's failing
        logger.error(f"Error decoding packet (len={len(byte_data)}): {e}", exc_info=True)
        logger.error(f"Failed packet hex: {hex_data}")
        return None


def process_packet_path(
    path_bytes: bytes,
    payload: bytes,
    route_type: RouteType,
    payload_type: PayloadType,
    *,
    prefix_hex_chars: int,
    logger: Any,
) -> dict:
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
        # Chunk path bytes into node IDs using configured prefix length (with legacy fallback)
        _, path_nodes = path_bytes_to_nodes(path_bytes, prefix_hex_chars)

        # Special handling for TRACE packets
        if payload_type == PayloadType.TRACE:
            # RF path bytes are per-hop SNR×4 (int8), not node hashes. The commanded route is
            # in the payload after tag(4)+auth(4)+flags(1); use path_info / parse_trace_payload_route_hashes for display.
            # In TRACE packets, path field contains SNR data
            # Real routing path is in the payload as pathHashes (after tag(4) + auth(4) + flags(1))
            snr_values = []
            for b in path_bytes:
                # Convert SNR byte to dB (signed value)
                snr_db = (b - 256) / 4 if b > 127 else b / 4
                snr_values.append(snr_db)

            # Decode trace payload to extract pathHashes (routing path)
            # path_hash_len from flags (bits 0-1): 1 << (flags & 3) = 1, 2, 4, or 8 bytes per hop
            path_hashes = []
            if len(payload) >= 9:  # Minimum: tag(4) + auth(4) + flags(1)
                try:
                    path_hashes_bytes = payload[9:]
                    flags = payload[8]
                    path_hash_len = 1 << (flags & 3)  # 1, 2, 4, or 8 bytes per hop
                    if path_hash_len <= 0:
                        path_hash_len = 1
                    if len(path_hashes_bytes) % path_hash_len == 0:
                        path_hashes = [
                            path_hashes_bytes[i : i + path_hash_len].hex().upper()
                            for i in range(0, len(path_hashes_bytes), path_hash_len)
                        ]
                    else:
                        # Fallback: 1 byte per hop (legacy)
                        path_hashes = [f"{b:02x}".upper() for b in path_hashes_bytes]
                except Exception as e:
                    logger.debug(f"Error extracting pathHashes from trace payload: {e}")
                    path_hashes = [f"{b:02x}".upper() for b in payload[9:]]

            return {
                "type": "trace",
                "snr_data": snr_values,
                "snr_path": path_nodes,  # SNR data as hex for reference
                "path": path_hashes,  # Actual routing path from payload pathHashes
                "path_hashes": path_hashes,  # Explicit field for pathHashes
                "description": f"TRACE packet with {len(snr_values)} SNR readings and {len(path_hashes)} path nodes",
            }

        # Regular packets - determine path type based on route type
        is_direct = route_type in [RouteType.DIRECT, RouteType.TRANSPORT_DIRECT]

        if is_direct:
            # Direct routing: path contains routing instructions
            # Bytes are stripped at each hop
            return {
                "type": "routing_instructions",
                "path": path_nodes,
                "meaning": "bytes_stripped_at_each_hop",
                "description": f"Direct route via {','.join(path_nodes)} ({len(path_nodes)} hops)",
            }
        else:
            # Flood routing: path contains historical route
            # Bytes are added as packet floods through network
            return {
                "type": "historical_route",
                "path": path_nodes,
                "meaning": "bytes_added_as_packet_floods",
                "description": f"Flooded through {','.join(path_nodes)} ({len(path_nodes)} hops)",
            }

    except Exception as e:
        logger.error(f"Error processing packet path: {e}")
        # Return basic path info as fallback (legacy 1-byte-per-hop)
        _, path_nodes = path_bytes_to_nodes(path_bytes, 2)
        return {"type": "unknown", "path": path_nodes, "description": f"Path: {','.join(path_nodes)}"}


def format_path_string(hex_path: str, bytes_per_hop: int | None = None, *, logger: Any) -> str:
    """
    Convert a hex path string to node prefix format.

    Args:
        hex_path: Hex string representing the path (e.g., "01025f7e" or "01025fab" for 2-byte hops).
        bytes_per_hop: Optional bytes per hop (1, 2, or 3) for multi-byte paths; None = legacy 1 byte per node.

    Returns:
        str: Formatted path string (e.g., "01,02,5f,7e" or "0102,5fab")
    """
    try:
        if not hex_path:
            return "Direct"

        if bytes_per_hop is not None and bytes_per_hop > 0:
            hex_chars = bytes_per_hop * 2
            path_nodes = split_path_hex(hex_path, hex_chars)
            if path_nodes:
                return ",".join(path_nodes)
            return "Direct"

        # Legacy: one byte per node (two hex chars)
        path_bytes = bytes.fromhex(hex_path)
        path_nodes = []
        for i in range(len(path_bytes)):
            node_id = path_bytes[i]
            path_nodes.append(f"{node_id:02x}")

        if path_nodes:
            return ",".join(path_nodes)
        else:
            return "Direct"

    except Exception as e:
        logger.debug(f"Error formatting path string: {e}")
        truncated = hex_path[:16] if len(hex_path) > 16 else hex_path
        return f"Raw: {truncated}{'...' if len(hex_path) > 16 else ''}"


def route_type_name(route_type: int) -> str:
    """Get human-readable name for route type"""
    route_types = {
        0x00: "ROUTE_TYPE_TRANSPORT_FLOOD",
        0x01: "ROUTE_TYPE_FLOOD",
        0x02: "ROUTE_TYPE_DIRECT",
        0x03: "ROUTE_TYPE_TRANSPORT_DIRECT",
    }
    return route_types.get(route_type, f"UNKNOWN_ROUTE_{route_type:02x}")


def payload_type_name(payload_type: int) -> str:
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
        0x0F: "RAW_CUSTOM",
    }
    return payload_types.get(payload_type, f"UNKNOWN_{payload_type:02x}")
