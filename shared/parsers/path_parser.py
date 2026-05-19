#!/usr/bin/env python3
"""
Canonical path string / path-len byte parsers.

Used by both the bot (modules/) and the web viewer (web_viewer/).
These replace three prior duplicated implementations in message_handler.py,
web_viewer/app.py, and utils.py.
"""

import re
from typing import Any, Optional


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


def parse_path_string(path_str: str, prefix_hex_chars: int = 2) -> list[str]:
    """Parse a path string to extract node IDs.

    Handles various formats:
    - "11,98,a4,49,cd,5f,01" (comma-separated)
    - "11 98 a4 49 cd 5f 01" (space-separated)
    - "1198a449cd5f01" (continuous hex)
    - "01,5f (2 hops)" (with hop count suffix)

    Args:
        path_str: Path string in various formats.
        prefix_hex_chars: Number of hex characters per node (2 = 1 byte, 4 = 2 bytes). Default 2.

    Returns:
        List[str]: List of uppercase hex node IDs (each of length prefix_hex_chars).
    """
    if not path_str:
        return []

    # Remove hop count suffix if present (e.g., " (2 hops)")
    path_str = re.sub(r'\s*\([^)]*hops?[^)]*\)', '', path_str, flags=re.IGNORECASE)
    path_str = path_str.strip()

    # Replace common separators with spaces
    path_str = path_str.replace(',', ' ').replace(':', ' ')

    # Extract hex values using regex (prefix_hex_chars-wide hex tokens)
    hex_pattern = rf'[0-9a-fA-F]{{{prefix_hex_chars}}}'
    hex_matches = re.findall(hex_pattern, path_str)

    # Legacy fallback: if configured length > 2 and no matches, retry with 2-char (1-byte) nodes
    if not hex_matches and prefix_hex_chars > 2:
        legacy_pattern = r'[0-9a-fA-F]{2}'
        hex_matches = re.findall(legacy_pattern, path_str)

    # Convert to uppercase for consistency
    return [match.upper() for match in hex_matches]


_HEX_BYTE_TOKEN = frozenset('0123456789aAbBcCdDeEfF')


def extract_path_node_ids_from_message(message: Any) -> list[str]:
    """Extract path node IDs from a mesh message (MeshCore multi-byte paths).

    Prefers ``routing_info.path_nodes``; else parses comma-separated hop tokens
    (2, 4, or 6 hex chars each) from ``message.path``. Matches TestCommand logic.

    Returns:
        List of node IDs (uppercase hex). Empty when direct / unparseable.
    """
    routing_info = getattr(message, 'routing_info', None)
    if routing_info is not None and routing_info.get('path_length', 0) == 0:
        return []
    if routing_info and routing_info.get('path_nodes'):
        return [str(n).upper().strip() for n in routing_info['path_nodes']]
    path_string = getattr(message, 'path', None) or ''
    if not path_string or "Direct" in path_string or "0 hops" in path_string:
        return []
    if " via ROUTE_TYPE_" in path_string:
        path_string = path_string.split(" via ROUTE_TYPE_")[0]
    if '(' in path_string:
        path_string = path_string.split('(')[0].strip()
    if ',' in path_string:
        parts = [p.strip() for p in path_string.split(',') if p.strip()]
        if parts and all(
            len(p) in (2, 4, 6) and all(c in _HEX_BYTE_TOKEN for c in p)
            for p in parts
        ):
            return [p.upper() for p in parts]
    return []


def _normalized_message_path_string(message: Any) -> str:
    """Strip route suffix and hop-count suffix from message.path for continuous-hex parsing."""
    path_string = (getattr(message, 'path', None) or '').strip()
    if not path_string or 'Direct' in path_string or '0 hops' in path_string:
        return ''
    if ' via ROUTE_TYPE_' in path_string:
        path_string = path_string.split(' via ROUTE_TYPE_')[0]
    path_string = re.sub(r'\s*\([^)]*hops?[^)]*\)', '', path_string, flags=re.IGNORECASE).strip()
    return path_string


def bytes_per_hop_from_routing_and_nodes(
    routing_info: Optional[dict[str, Any]],
    node_ids: list[str],
) -> int:
    """Bytes per hop from packet routing metadata, else inferred from hex node width.

    When ``routing_info`` includes ``bytes_per_hop`` in 1..3, that value wins.
    Otherwise uses minimum half-byte width among ``node_ids`` (comma or path_nodes).
    Returns ``1`` when no nodes (direct / unknown).
    """
    if routing_info:
        bph = routing_info.get('bytes_per_hop')
        if isinstance(bph, int) and 1 <= bph <= 3:
            return bph
    if node_ids:
        return min(len(n) // 2 for n in node_ids)
    return 1


def message_path_bytes_per_hop(message: Any, *, prefix_hex_chars: int = 2) -> int:
    """Best-effort bytes per hop for the message path (RF metadata or inferred from path text).

    Uses ``routing_info.bytes_per_hop`` when present (1..3). Otherwise prefers
    :func:`extract_path_node_ids_from_message`, then comma/continuous hex via
    :func:`node_ids_from_path_string` using ``prefix_hex_chars`` for legacy paths.

    Returns ``1`` when no usable path (direct / unparseable) so conservative gates
    (e.g. ``pathbytes_min:2``) do not treat unknown as multibyte.
    """
    routing_info = getattr(message, 'routing_info', None)
    node_ids = extract_path_node_ids_from_message(message)
    if not node_ids:
        ps = _normalized_message_path_string(message)
        if ps:
            node_ids = node_ids_from_path_string(ps, prefix_hex_chars)
    return bytes_per_hop_from_routing_and_nodes(routing_info, node_ids)


def node_ids_from_path_string(path_str: str, prefix_hex_chars: int = 2) -> list[str]:
    """Parse path display string into node IDs: multi-byte comma tokens, else fixed-width scan.

    Comma-separated tokens must each be 2, 4, or 6 hex digits (one hop per token).
    Otherwise falls back to :func:`parse_path_string` (legacy continuous / 1-byte paths).
    """
    if not path_str or not path_str.strip():
        return []
    path_lower = path_str.lower()
    if "direct" in path_lower or "0 hops" in path_lower:
        return []
    s = path_str.strip()
    if " via ROUTE_TYPE_" in s:
        s = s.split(" via ROUTE_TYPE_")[0].strip()
    s = re.sub(r'\s*\([^)]*hops?[^)]*\)', '', s, flags=re.IGNORECASE).strip()
    if not s:
        return []
    if ',' in s:
        parts = [p.strip() for p in s.split(',') if p.strip()]
        if parts and all(
            len(p) in (2, 4, 6) and all(c in _HEX_BYTE_TOKEN for c in p)
            for p in parts
        ):
            return [p.upper() for p in parts]
    return parse_path_string(s, prefix_hex_chars)
