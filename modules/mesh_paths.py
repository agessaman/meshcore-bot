"""Mesh path helpers: parsing path strings, hop counts and node locations along a path."""

from __future__ import annotations

import re
from typing import Any, Optional


def public_key_has_prefix(public_key: str, prefix: str) -> bool:
    """Return True if public_key starts with prefix (case-insensitive hex match)."""
    if not public_key or not prefix:
        return False
    return public_key.lower().startswith(prefix.lower())


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

    When the packet carries a path and ``routing_info`` includes ``bytes_per_hop``
    in 1..3, that value wins. Otherwise uses minimum half-byte width among
    ``node_ids`` (comma or path_nodes). Returns ``1`` when no nodes (direct /
    unknown).

    A hopless packet always reports ``1``, whatever ``bytes_per_hop`` says.
    ``bytes_per_hop`` describes how a path is *encoded*, and a direct packet has no
    path for it to describe, so letting the format field through would make
    ``pathbytes_min:2`` treat "Direct" as a multi-byte path and print a label for a
    distance that does not exist.
    """
    path_length = (routing_info or {}).get('path_length')
    if not node_ids and not path_length:
        return 1
    if routing_info:
        bph = routing_info.get('bytes_per_hop')
        if isinstance(bph, int) and 1 <= bph <= 3:
            return bph
    if node_ids:
        return min(len(n) // 2 for n in node_ids)
    return 1


def message_hop_count(message: Any) -> Optional[int]:
    """Hop count for the message, or ``None`` when it cannot be determined.

    Prefers ``message.hops``, then ``routing_info`` (``path_length``, else the
    number of ``path_nodes``), then a count parsed from the path display string
    (``"01,5f (2 hops)"``; ``"Direct"`` or ``"0 hops"`` mean zero).

    ``None`` means unknown, which is not the same as zero: callers that gate on
    hop count should treat it as "cannot confirm" rather than "direct".
    """
    hops_val = getattr(message, 'hops', None)
    routing_info = getattr(message, 'routing_info', None)

    if not isinstance(hops_val, int) and isinstance(routing_info, dict):
        hops_val = routing_info.get('path_length')
        if hops_val is None and routing_info.get('path_nodes'):
            hops_val = len(routing_info['path_nodes'])

    if not isinstance(hops_val, int):
        path_str = getattr(message, 'path', None) or ""
        hop_match = re.search(r'\((\d+)\s*hops?', path_str, re.IGNORECASE)
        if hop_match:
            hops_val = int(hop_match.group(1))
        elif re.search(r'\bdirect\b|\b0\s*hops?\b', path_str, re.IGNORECASE):
            hops_val = 0

    return hops_val if isinstance(hops_val, int) else None


def message_path_bytes_per_hop(message: Any, *, prefix_hex_chars: int = 2) -> int:
    """Best-effort bytes per hop for the message path (RF metadata or inferred from path text).

    Uses ``routing_info.bytes_per_hop`` when present (1..3). Otherwise prefers
    :func:`extract_path_node_ids_from_message`, then comma/continuous hex via
    :func:`node_ids_from_path_string` using ``prefix_hex_chars`` for legacy paths.

    Returns ``1`` when there is no usable path (direct / unparseable) so conservative
    gates (e.g. ``pathbytes_min:2``) treat neither unknown nor hopless as multibyte.
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
    from .utils import calculate_distance  # utils re-exports this module; import late
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
    from .utils import calculate_distance  # utils re-exports this module; import late
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
