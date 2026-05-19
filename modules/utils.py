#!/usr/bin/env python3
"""
Utility functions for the MeshCore Bot
Shared helper functions used across multiple modules
"""

import asyncio
import hashlib
import re
import socket
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from shared.geocoding import calculate_distance
from shared.text_utils import format_elapsed_display

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[misc, assignment]


def is_valid_timezone(tz_str: str) -> bool:
    """Return True if the string is a valid IANA timezone name."""
    if not (tz_str and tz_str.strip()):
        return False
    if ZoneInfo is not None:
        try:
            ZoneInfo(tz_str.strip())
            return True
        except ZoneInfoNotFoundError:
            return False
    try:
        pytz = __import__("pytz")
        pytz.timezone(tz_str.strip())
        return True
    except Exception:
        return False


def get_config_timezone(config: Any, logger: Optional[Any] = None) -> tuple[Any, str]:
    """Resolve [Bot] timezone from config; fall back to system timezone if invalid or empty.

    Returns:
        (tz, iana_str): tz is a timezone object for datetime; iana_str is an IANA
        string for APIs (e.g. OpenMeteo). When falling back to system, iana_str is "UTC".
    """
    timezone_str = (config.get('Bot', 'timezone', fallback='') or '').strip()
    if timezone_str and is_valid_timezone(timezone_str):
        pytz = __import__("pytz")
        return (pytz.timezone(timezone_str), timezone_str)
    if timezone_str and logger:
        logger.warning("Invalid timezone '%s', using system timezone", timezone_str)
    # System timezone for datetime; use "UTC" for API when we don't have an IANA name
    tz = datetime.now().astimezone().tzinfo
    return (tz, "UTC")


def format_temperature_high_low(
    config: Any,
    high: Optional[Union[int, float]],
    low: Optional[Union[int, float]],
    units_str: str,
    logger: Optional[Any] = None,
) -> str:
    """Format a daily high/low pair (or single value) using [Weather] templates.

    Config keys (optional; defaults match prior bot behavior):
      temperature_high_low_format — both values: {high}, {low}, {units}
      temperature_high_only_format — {high}, {units}
      temperature_low_only_format — {low}, {units}
    """
    section = "Weather"
    default_pair = "H:{high}{units} L:{low}{units}"
    default_high_only = "H:{high}{units}"
    default_low_only = "L:{low}{units}"

    def _norm(v: Optional[Union[int, float]]) -> Optional[int]:
        if v is None:
            return None
        try:
            if isinstance(v, float):
                return int(round(v))
            return int(v)
        except (TypeError, ValueError):
            return None

    hi = _norm(high)
    lo = _norm(low)
    if hi is None and lo is None:
        return ""

    if config.has_section(section):
        pair_fmt = config.get(section, "temperature_high_low_format", fallback=default_pair)
        high_only_fmt = config.get(section, "temperature_high_only_format", fallback=default_high_only)
        low_only_fmt = config.get(section, "temperature_low_only_format", fallback=default_low_only)
    else:
        pair_fmt, high_only_fmt, low_only_fmt = default_pair, default_high_only, default_low_only

    def _try_format(fmt: str, **kwargs: Any) -> Optional[str]:
        try:
            return fmt.format(**kwargs)
        except (KeyError, ValueError, IndexError) as e:
            if logger is not None and hasattr(logger, "warning"):
                logger.warning("Invalid temperature format template %r: %s", fmt, e)
            return None

    if hi is not None and lo is not None:
        out = _try_format(pair_fmt, high=hi, low=lo, units=units_str)
        if out is not None:
            return out
        return _try_format(default_pair, high=hi, low=lo, units=units_str) or f"H:{hi}{units_str} L:{lo}{units_str}"

    if hi is not None:
        out = _try_format(high_only_fmt, high=hi, low=lo, units=units_str)
        if out is not None:
            return out
        return _try_format(default_high_only, high=hi, low=lo, units=units_str) or f"H:{hi}{units_str}"

    out = _try_format(low_only_fmt, high=hi, low=lo, units=units_str)
    if out is not None:
        return out
    return _try_format(default_low_only, high=hi, low=lo, units=units_str) or f"L:{lo}{units_str}"






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


















def resolve_path(file_path: Union[str, Path], base_dir: Union[str, Path] = '.') -> str:
    """Resolve a file path relative to a base directory.

    If the path is absolute, it is returned as-is (no symlink/canonical resolution).
    If the path is relative, it is resolved relative to the base directory.

    Args:
        file_path: Path to resolve (can be string or Path object).
        base_dir: Base directory for resolving relative paths (default: current directory).

    Returns:
        str: Resolved absolute path as a string.

    Examples:
        >>> resolve_path('data.db', '/opt/bot')
        '/opt/bot/data.db'
        >>> resolve_path('/var/lib/bot/data.db', '/opt/bot')
        '/var/lib/bot/data.db'
    """
    file_path = Path(file_path) if not isinstance(file_path, Path) else file_path
    base_dir = Path(base_dir) if not isinstance(base_dir, Path) else base_dir

    if file_path.is_absolute():
        # Important on macOS: `/var/...` may be a symlink to `/private/var/...`.
        # Tests (and callers) expect the absolute path string to stay stable.
        return str(file_path)
    else:
        return str((base_dir.resolve() / file_path).resolve())


def check_internet_connectivity(host: str = "8.8.8.8", port: int = 53, timeout: float = 3.0) -> bool:
    """Check if internet connectivity is available by attempting to connect to a reliable host.

    First tries a lightweight DNS port check (faster, doesn't require DNS resolution).
    If that fails (e.g., DNS port is blocked), falls back to an HTTP request check.

    Args:
        host: Host to connect to (default: 8.8.8.8, Google's public DNS).
        port: Port to connect to (default: 53, DNS port).
        timeout: Connection timeout in seconds (default: 3.0).

    Returns:
        bool: True if connection successful, False otherwise.
    """
    # First try: DNS port check (fastest, works if DNS port is open)
    try:
        socket.setdefaulttimeout(timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((host, port))
        sock.close()
        socket.setdefaulttimeout(None)  # Reset to default
        return True
    except (OSError, socket.timeout):
        socket.setdefaulttimeout(None)  # Reset to default
        # DNS check failed, try HTTP fallback
        pass

    # Fallback: HTTP request check (works even if DNS port is blocked)
    try:
        # Use a reliable HTTP endpoint that's likely to be accessible
        # Using IP address to avoid DNS resolution issues
        http_url = "http://1.1.1.1"  # Cloudflare DNS
        urllib.request.urlopen(http_url, timeout=timeout).close()
        return True
    except (urllib.error.URLError, OSError, socket.timeout):
        # If IP-based check fails, try a hostname-based check
        try:
            http_url = "http://www.google.com"
            urllib.request.urlopen(http_url, timeout=timeout).close()
            return True
        except (urllib.error.URLError, OSError, socket.timeout):
            return False


async def check_internet_connectivity_async(host: str = "8.8.8.8", port: int = 53, timeout: float = 3.0) -> bool:
    """Async version of check_internet_connectivity.

    First tries a lightweight DNS port check (faster, doesn't require DNS resolution).
    If that fails (e.g., DNS port is blocked), falls back to an HTTP request check.

    Args:
        host: Host to connect to (default: 8.8.8.8, Google's public DNS).
        port: Port to connect to (default: 53, DNS port).
        timeout: Connection timeout in seconds (default: 3.0).

    Returns:
        bool: True if connection successful, False otherwise.
    """
    # First try: DNS port check (fastest, works if DNS port is open)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (asyncio.TimeoutError, OSError, ConnectionError):
        # DNS check failed, try HTTP fallback
        pass
    except Exception:
        # Unexpected error, try HTTP fallback
        pass

    # Fallback: HTTP request check (works even if DNS port is blocked)
    # Run urllib in executor to avoid blocking
    loop = asyncio.get_event_loop()
    try:
        # Use a reliable HTTP endpoint that's likely to be accessible
        # Using IP address to avoid DNS resolution issues
        http_url = "http://1.1.1.1"  # Cloudflare DNS
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: urllib.request.urlopen(http_url, timeout=timeout).close()
            ),
            timeout=timeout
        )
        return True
    except (asyncio.TimeoutError, urllib.error.URLError, OSError, socket.timeout):
        # If IP-based check fails, try a hostname-based check
        try:
            http_url = "http://www.google.com"
            await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: urllib.request.urlopen(http_url, timeout=timeout).close()
                ),
                timeout=timeout
            )
            return True
        except (asyncio.TimeoutError, urllib.error.URLError, OSError, socket.timeout):
            return False
    except Exception:
        return False


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


def calculate_path_distances(
    bot: Any, path_str: str, message: Optional[Any] = None
) -> tuple[str, str]:
    """Calculate path distance metrics from a path string and optional message.

    When ``message`` is provided, node IDs are taken from ``routing_info.path_nodes``
    or multi-byte comma parsing of ``message.path`` (same as the test command),
    with a fallback to :func:`parse_path_string` for continuous hex without commas.

    Args:
        bot: Bot instance (must have db_manager).
        path_str: Path string when no message or for legacy callers.
        message: Optional mesh message for routing_info / path fields.

    Returns:
        Tuple[str, str]: A tuple containing:
            - path_distance_str: Total distance with segment info (e.g., "123.4km (3 segs, 1 no-loc)").
            - firstlast_distance_str: Distance between first and last repeater (e.g., "45.6km").
    """
    prefix_hex = getattr(bot, 'prefix_hex_chars', 2)

    if message is None:
        if not path_str or not str(path_str).strip():
            return "directly (0 hops)", "N/A (direct)"
        path_lower = path_str.lower()
        if "direct" in path_lower or "0 hops" in path_lower:
            return "directly (0 hops)", "N/A (direct)"

    if not hasattr(bot, 'db_manager'):
        return "unknown distance", "unknown"

    try:
        node_ids: list[str]
        if message is not None:
            node_ids = extract_path_node_ids_from_message(message)
            if not node_ids and (getattr(message, 'path', None) or ''):
                node_ids = node_ids_from_path_string(message.path, prefix_hex)
        else:
            node_ids = node_ids_from_path_string(path_str, prefix_hex)

        if len(node_ids) == 0:
            # No nodes parsed - likely direct connection
            return "directly (0 hops)", "N/A (direct)"
        elif len(node_ids) == 1:
            # Single node - local/one hop (no first/last distance since only one node)
            return "locally (1 hop)", "N/A (1 hop)"
        elif len(node_ids) < 2:
            # Edge case - less than 2 nodes
            return "locally (1 hop)", "N/A (1 hop)"

        # Look up locations for each node ID
        # _get_node_location_from_db returns ((lat, lon), public_key) or None
        node_locations: list[Optional[tuple[float, float]]] = []
        for node_id in node_ids:
            result = _get_node_location_from_db(bot, node_id)
            if result:
                location, _ = result  # Extract location tuple, ignore public_key
                node_locations.append(location)
            else:
                node_locations.append(None)

        # Calculate total path distance (sum of all segments)
        total_distance = 0.0
        segments_with_location = 0
        segments_without_location = 0

        for i in range(len(node_locations) - 1):
            loc1 = node_locations[i]
            loc2 = node_locations[i + 1]

            if loc1 and loc2:
                # Both nodes have locations
                segment_distance = calculate_distance(
                    loc1[0], loc1[1],
                    loc2[0], loc2[1]
                )
                total_distance += segment_distance
                segments_with_location += 1
            else:
                # At least one node missing location
                segments_without_location += 1

        # Format path_distance string
        if total_distance > 0:
            path_distance_str = f"{total_distance:.1f}km"
            if segments_with_location > 0 or segments_without_location > 0:
                seg_info = []
                if segments_with_location > 0:
                    seg_info.append(f"{segments_with_location} segs")
                if segments_without_location > 0:
                    seg_info.append(f"{segments_without_location} no-loc")
                if seg_info:
                    path_distance_str += f" ({', '.join(seg_info)})"
        else:
            # No distance calculated (all segments missing locations)
            if segments_without_location > 0:
                # We have segments but no location data
                hop_count = len(node_ids)
                path_distance_str = f"unknown distance ({hop_count} hops, no locations)"
            else:
                # Fallback - shouldn't happen but provide meaningful text
                hop_count = len(node_ids)
                path_distance_str = f"unknown distance ({hop_count} hops)"

        # Calculate first-to-last distance
        firstlast_distance_str = ""
        first_location = node_locations[0]
        last_location = node_locations[-1]

        if first_location and last_location:
            firstlast_distance = calculate_distance(
                first_location[0], first_location[1],
                last_location[0], last_location[1]
            )
            firstlast_distance_str = f"{firstlast_distance:.1f}km"
        elif len(node_ids) >= 2:
            # We have 2+ nodes but missing location data
            firstlast_distance_str = "unknown (no locations)"

        return path_distance_str, firstlast_distance_str

    except Exception as e:
        # Log error but don't fail - return empty strings
        if hasattr(bot, 'logger'):
            bot.logger.debug(f"Error calculating path distances: {e}")
        return "", ""


def _get_node_location_from_db(bot: Any, node_id: str, reference_location: Optional[tuple[float, float]] = None, recency_days: Optional[int] = None) -> Optional[tuple[tuple[float, float], Optional[str]]]:
    """Get location for a node ID from the database.

    For LoRa networks, prefers shorter distances when there are prefix collisions,
    as LoRa range is limited by the curve of the earth.

    Args:
        bot: Bot instance (must have db_manager).
        node_id: 2-character hex node ID (e.g., "01", "5f").
        reference_location: Optional (lat, lon) to calculate distance from for LoRa preference.
        recency_days: Optional number of days to filter by recency (only use repeaters heard within this window).

    Returns:
        Optional[Tuple[Tuple[float, float], Optional[str]]]:
        - ((latitude, longitude), public_key) if found, where public_key may be None
        - None if not found
    """
    if not hasattr(bot, 'db_manager'):
        return None

    try:
        # Look up node by public key prefix (first 2 characters)
        prefix_pattern = f"{node_id}%"

        # Get all candidates with locations, optionally filtered by recency
        # Include public_key so we can return it when distance-based selection is used
        if recency_days is not None:
            query = f'''
                SELECT latitude, longitude, is_starred, public_key,
                       COALESCE(last_advert_timestamp, last_heard) as last_seen
                FROM complete_contact_tracking
                WHERE public_key LIKE ?
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
                AND role IN ('repeater', 'roomserver')
                AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
            '''
            results = bot.db_manager.execute_query(query, (prefix_pattern,))
        else:
            query = '''
                SELECT latitude, longitude, is_starred, public_key,
                       COALESCE(last_advert_timestamp, last_heard) as last_seen
                FROM complete_contact_tracking
                WHERE public_key LIKE ?
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
                AND role IN ('repeater', 'roomserver')
            '''
            results = bot.db_manager.execute_query(query, (prefix_pattern,))

        if not results:
            return None

            # If we have a reference location, prefer shorter distances (LoRa range limitation)
        if reference_location and len(results) > 1:
            ref_lat, ref_lon = reference_location

            # Calculate distances and sort by distance (shorter first)
            candidates_with_distance = []
            for row in results:
                lat = row.get('latitude')
                lon = row.get('longitude')
                if lat is not None and lon is not None:
                    distance = calculate_distance(ref_lat, ref_lon, float(lat), float(lon))
                    is_starred = row.get('is_starred', False)
                    last_seen = row.get('last_seen', '')
                    candidates_with_distance.append((distance, is_starred, last_seen, row))

            if candidates_with_distance:
                # Sort by: starred first, then distance (shorter = better for LoRa), then recency (newer first)
                # For recency, we need newer timestamps to sort first. Use a two-pass stable sort:
                # First sort by starred and distance, then stable sort by recency in reverse
                from datetime import datetime

                def get_timestamp_key(ts_str: Optional[str]) -> float:
                    """Convert timestamp string to sortable key (newer = smaller key for reverse sort)"""
                    if not ts_str:
                        return float('inf')  # Empty timestamps sort last
                    try:
                        # Parse timestamp and return negative timestamp for descending sort
                        dt = datetime.fromisoformat(ts_str.replace(' ', 'T'))
                        return -dt.timestamp()  # Negate: newer timestamps have larger timestamps, so -timestamp is smaller
                    except:
                        # Fallback: use string comparison (newer strings are lexicographically greater)
                        # To reverse, we'll use a large value minus a hash
                        return -len(ts_str) * 1000000 - hash(ts_str)

                # Sort by: starred first, then distance (shorter = better for LoRa), then recency (newer first)
                # IMPORTANT: Distance takes priority over recency when we have a reference location
                # Use a single sort with all three criteria to ensure proper ordering
                candidates_with_distance.sort(key=lambda x: (
                    not x[1],  # Starred first (False < True, so starred=True comes before starred=False)
                    x[0],  # Distance (shorter first) - THIS IS THE PRIMARY FACTOR for LoRa
                    get_timestamp_key(x[2])  # Recency (newer first) - only as tiebreaker
                ))

                # Get the best candidate
                best_row = candidates_with_distance[0][3]
                lat = best_row.get('latitude')
                lon = best_row.get('longitude')
                if lat is not None and lon is not None:
                    # Return location and also the public key of the selected node (for distance-based selection)
                    # This allows us to store which specific node was selected when there's a prefix collision
                    # Always return a tuple: (location, public_key or None)
                    public_key = best_row.get('public_key')
                    return ((float(lat), float(lon)), public_key)

        # No reference location or single result - use standard ordering
        # Prefer starred, then most recent
        # For recency, parse timestamps properly to ensure newer comes first
        from datetime import datetime

        def get_timestamp_key_no_ref(ts_str: Optional[str]) -> float:
            """Convert timestamp string to sortable key (newer = smaller key)"""
            if not ts_str:
                return float('inf')  # Empty timestamps sort last
            try:
                dt = datetime.fromisoformat(ts_str.replace(' ', 'T'))
                return -dt.timestamp()  # Negate: newer timestamps have larger timestamps, so -timestamp is smaller
            except:
                return -len(ts_str) * 1000000 - hash(ts_str)

        results.sort(key=lambda x: (
            not x.get('is_starred', False),  # Starred first (False < True)
            get_timestamp_key_no_ref(x.get('last_seen', ''))  # More recent first (newer = smaller key)
        ))

        row = results[0]
        lat = row.get('latitude')
        lon = row.get('longitude')
        if lat is not None and lon is not None:
            # Return location and also the public key if available (for distance-based selection)
            # Always return a tuple: (location, public_key or None)
            public_key = row.get('public_key')
            return ((float(lat), float(lon)), public_key)

        return None
    except Exception as e:
        if hasattr(bot, 'logger'):
            bot.logger.debug(f"Error getting node location for {node_id}: {e}")
        return None

def _get_node_location_and_key_from_db(bot: Any, node_id: str, reference_location: Optional[tuple[float, float]] = None) -> Optional[tuple[tuple[float, float], str]]:
    """Get location and public key for a node ID from the database.

    For LoRa networks, prefers shorter distances when there are prefix collisions,
    as LoRa range is limited by the curve of the earth.

    Args:
        bot: Bot instance (must have db_manager).
        node_id: 2-character hex node ID (e.g., "01", "5f").
        reference_location: Optional (lat, lon) to calculate distance from for LoRa preference.

    Returns:
        Optional[Tuple[Tuple[float, float], str]]: Tuple of ((latitude, longitude), public_key) or None if not found.
    """
    if not hasattr(bot, 'db_manager'):
        return None

    try:
        # Look up node by public key prefix (first 2 characters)
        prefix_pattern = f"{node_id}%"

        # Get all candidates with locations
        query = '''
            SELECT latitude, longitude, is_starred, public_key,
                   COALESCE(last_advert_timestamp, last_heard) as last_seen
            FROM complete_contact_tracking
            WHERE public_key LIKE ?
            AND latitude IS NOT NULL AND longitude IS NOT NULL
            AND latitude != 0 AND longitude != 0
            AND role IN ('repeater', 'roomserver')
        '''

        results = bot.db_manager.execute_query(query, (prefix_pattern,))

        if not results:
            return None

        # If we have a reference location, prefer shorter distances (LoRa range limitation)
        if reference_location and len(results) > 1:
            ref_lat, ref_lon = reference_location

            # Calculate distances and sort by distance (shorter first)
            # For LoRa networks, shorter distances are more likely to be correct single-hop connections
            candidates_with_distance = []
            for row in results:
                lat = row.get('latitude')
                lon = row.get('longitude')
                if lat is not None and lon is not None:
                    distance = calculate_distance(ref_lat, ref_lon, float(lat), float(lon))
                    is_starred = row.get('is_starred', False)
                    last_seen = row.get('last_seen', '')
                    public_key = row.get('public_key', '')
                    candidates_with_distance.append((distance, is_starred, last_seen, public_key, row))

            if candidates_with_distance:
                # Sort by: starred first (False < True), then distance (shorter = better for LoRa), then recency
                candidates_with_distance.sort(key=lambda x: (
                    not x[1],  # Starred first (False < True, so starred=True comes before starred=False)
                    x[0],  # Distance (shorter first - important for LoRa range limitations)
                    x[2] if x[2] else ''  # More recent first (newer timestamps sort later in string comparison)
                ))

                # Get the best candidate
                best_row = candidates_with_distance[0][4]
                lat = best_row.get('latitude')
                lon = best_row.get('longitude')
                public_key = candidates_with_distance[0][3]
                if lat is not None and lon is not None and public_key:
                    return ((float(lat), float(lon)), public_key)

        # No reference location or single result - use standard ordering
        # Prefer starred, then most recent
        results.sort(key=lambda x: (
            not x.get('is_starred', False),  # Starred first (False < True)
            x.get('last_seen', '') if x.get('last_seen') else ''  # More recent first
        ))

        row = results[0]
        lat = row.get('latitude')
        lon = row.get('longitude')
        public_key = row.get('public_key', '')
        if lat is not None and lon is not None and public_key:
            return ((float(lat), float(lon)), public_key)

        return None
    except Exception as e:
        if hasattr(bot, 'logger'):
            bot.logger.debug(f"Error getting node location and key for {node_id}: {e}")
        return None


# Maximum plausible elapsed ms (5 minutes) for device clock validation.
# Values above indicate device time is far in the past (e.g. epoch); negative = in the future.

def format_keyword_response_with_placeholders(
    response_format: str,
    message: Any,
    bot: Any,
    mesh_info: Optional[dict[str, Any]] = None
) -> str:
    """Format a keyword response string with all available placeholders.

    Supports both message-based placeholders and mesh-info-based placeholders.
    This is a shared function used by both Keywords and Scheduled_Messages.

    Args:
        response_format: Response format string with placeholders.
        message: MeshMessage instance (can be None for scheduled messages).
        bot: Bot instance (must have config, db_manager).
        mesh_info: Optional mesh network info dict (for scheduled message placeholders).

    Returns:
        str: Formatted response string.
    """
    try:
        replacements = {}

        # Message-based placeholders (require message object)
        if message:
            # Basic message fields
            replacements['sender'] = message.sender_id or "Unknown"
            replacements['path'] = message.path or "Unknown"
            replacements['snr'] = message.snr or "Unknown"
            replacements['rssi'] = message.rssi or "Unknown"
            # Compute elapsed from message.timestamp (same as TestCommand) so it's available
            # for all keywords. Using message.elapsed would miss when it's unset on some paths.
            _translator = getattr(bot, 'translator', None)
            replacements['elapsed'] = format_elapsed_display(
                getattr(message, 'timestamp', None), _translator
            )

            # Build connection_info
            routing_info = message.path or "Unknown routing"
            if "via ROUTE_TYPE_" in routing_info:
                parts = routing_info.split(" via ROUTE_TYPE_")
                if len(parts) > 0:
                    routing_info = parts[0]

            snr_info = f"SNR: {message.snr or 'Unknown'} dB"
            rssi_info = f"RSSI: {message.rssi or 'Unknown'} dBm"
            connection_info = f"{routing_info} | {snr_info} | {rssi_info}"
            replacements['connection_info'] = connection_info

            # Calculate path distances
            path_distance, firstlast_distance = calculate_path_distances(
                bot, message.path or "", message=message
            )
            replacements['path_distance'] = path_distance
            replacements['firstlast_distance'] = firstlast_distance

            # Format timestamp
            try:
                tz, _ = get_config_timezone(bot.config, getattr(bot, 'logger', None))
                dt = datetime.now(tz)
                time_str = dt.strftime("%H:%M:%S")
            except Exception:
                time_str = "Unknown"

            replacements['timestamp'] = time_str

            # Total hops: use message.hops when set, else parse from path string (e.g. "01,5f (2 hops)")
            hops_val = getattr(message, 'hops', None)
            if hops_val is not None and isinstance(hops_val, int):
                replacements['hops'] = str(hops_val)
            else:
                path_str = message.path or ""
                hop_match = re.search(r'\((\d+)\s*hops?', path_str, re.IGNORECASE)
                if hop_match:
                    replacements['hops'] = hop_match.group(1)
                elif re.search(r'\bdirect\b|\b0\s*hops?\b', path_str, re.IGNORECASE):
                    replacements['hops'] = "0"
                else:
                    replacements['hops'] = "?"
            # Pluralized label: "1 hop", "2 hops", or "?" when unknown
            h = replacements['hops']
            if h == "?":
                replacements['hops_label'] = "?"
            else:
                n = int(h)
                replacements['hops_label'] = "1 hop" if n == 1 else f"{n} hops"
        else:
            # No message - use defaults for message-based placeholders
            replacements['sender'] = "Unknown"
            replacements['path'] = "Unknown"
            replacements['snr'] = "Unknown"
            replacements['rssi'] = "Unknown"
            replacements['elapsed'] = "Unknown"
            replacements['connection_info'] = "Unknown"
            replacements['path_distance'] = ""
            replacements['firstlast_distance'] = ""
            replacements['timestamp'] = "Unknown"
            replacements['hops'] = "?"
            replacements['hops_label'] = "?"

        # Mesh-info-based placeholders (from scheduled messages)
        if mesh_info:
            replacements.update({
                'total_contacts': mesh_info.get('total_contacts', 0),
                'total_repeaters': mesh_info.get('total_repeaters', 0),
                'total_companions': mesh_info.get('total_companions', 0),
                'total_roomservers': mesh_info.get('total_roomservers', 0),
                'total_sensors': mesh_info.get('total_sensors', 0),
                'recent_activity_24h': mesh_info.get('recent_activity_24h', 0),
                'new_companions_7d': mesh_info.get('new_companions_7d', 0),
                'new_repeaters_7d': mesh_info.get('new_repeaters_7d', 0),
                'new_roomservers_7d': mesh_info.get('new_roomservers_7d', 0),
                'new_sensors_7d': mesh_info.get('new_sensors_7d', 0),
                'total_contacts_30d': mesh_info.get('total_contacts_30d', 0),
                'total_repeaters_30d': mesh_info.get('total_repeaters_30d', 0),
                'total_companions_30d': mesh_info.get('total_companions_30d', 0),
                'total_roomservers_30d': mesh_info.get('total_roomservers_30d', 0),
                'total_sensors_30d': mesh_info.get('total_sensors_30d', 0),
                # Legacy placeholders
                'repeaters': mesh_info.get('total_repeaters', 0),
                'companions': mesh_info.get('total_companions', 0),
            })
        else:
            # No mesh_info - use defaults
            mesh_defaults = {
                'total_contacts': 0,
                'total_repeaters': 0,
                'total_companions': 0,
                'total_roomservers': 0,
                'total_sensors': 0,
                'recent_activity_24h': 0,
                'new_companions_7d': 0,
                'new_repeaters_7d': 0,
                'new_roomservers_7d': 0,
                'new_sensors_7d': 0,
                'total_contacts_30d': 0,
                'total_repeaters_30d': 0,
                'total_companions_30d': 0,
                'total_roomservers_30d': 0,
                'total_sensors_30d': 0,
                'repeaters': 0,
                'companions': 0,
            }
            replacements.update(mesh_defaults)

        # Format the response with all replacements
        return response_format.format(**replacements)

    except (KeyError, ValueError) as e:
        # If formatting fails, return as-is (might not have all placeholders)
        if hasattr(bot, 'logger'):
            bot.logger.debug(f"Error formatting response with placeholders: {e}")
        return response_format
