#!/usr/bin/env python3
"""
Mesh packet hash and advertisement signature utilities.

Used by both the bot (modules/) and the web viewer (web_viewer/).
"""

import hashlib
from typing import Optional

from shared.parsers.path_parser import decode_path_len_byte


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


def verify_meshcore_advert_ed25519(mesh_payload: bytes) -> bool:
    """Verify MeshCore ADVERT Ed25519 signature (layout from ``Mesh::createAdvert``).

    Signed message is ``pub_key (32) + timestamp (4, LE) + app_data``; signature is
    ``payload[36:100]`` (64 bytes); ``app_data`` starts at byte 100.
    """
    if len(mesh_payload) < 100:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        pub = mesh_payload[:32]
        msg = mesh_payload[:36] + mesh_payload[100:]
        sig = mesh_payload[36:100]
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
        return True
    except Exception:
        return False
