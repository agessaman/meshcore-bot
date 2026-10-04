#!/usr/bin/env python3
"""
Graph update helper for trace data.
Shared by message_handler (on RX) and trace command (when TRACE_DATA is received).
"""

import time
from typing import Any, Optional

from .contacts_repo import unique_recent_repeater_key
from .transmission_tracker import own_public_key


def update_mesh_graph_from_trace_data(
    bot: Any,
    path_hashes: list[str],
    packet_info: dict[str, Any],
    *,
    is_our_trace: Optional[bool] = None,
) -> None:
    """Update mesh graph with edges from a trace packet's pathHashes.

    When the bot receives a trace packet, it's the destination, so we can confirm
    the edges in the path. The pathHashes represent the routing path the packet took.

    Special case: If this is a trace we sent that came back through an immediate neighbor,
    we can trust both directions (Bot -> Neighbor and Neighbor -> Bot).

    Args:
        bot: MeshCoreBot instance (mesh_graph, transmission_tracker, config, db_manager, meshcore).
        path_hashes: Per-hop hash strings from the trace payload (uppercase hex). Length in nibbles is
            ``2 * (1 << (flags & 3))`` per hop (1, 2, 4, or 8 bytes), not always 2 hex chars.
        packet_info: Packet information dictionary (packet_hash optional; used when is_our_trace is None).
        is_our_trace: If None, derived from packet_info['packet_hash'] and transmission_tracker.
            If True/False, use that value (e.g. trace command sets True when TRACE_DATA matches our tag).
    """
    if not path_hashes or len(path_hashes) == 0:
        bot.logger.debug("Mesh graph: Trace packet has no pathHashes, skipping graph update")
        return

    if not hasattr(bot, "mesh_graph") or not bot.mesh_graph:
        bot.logger.debug("Mesh graph: Graph not initialized, skipping trace update")
        return

    if not hasattr(bot, "transmission_tracker") or not bot.transmission_tracker:
        bot.logger.debug("Mesh graph: Cannot get bot prefix, skipping trace update")
        return

    mesh_graph = bot.mesh_graph
    # Name the bot at the trace's own hash width, so its edges match the hops'
    bot_key = own_public_key(bot)
    width = len(path_hashes[-1])
    bot_prefix = bot_key[:width] if bot_key and len(bot_key) >= width else bot.transmission_tracker.bot_prefix

    if not bot_prefix:
        bot.logger.debug("Mesh graph: Bot prefix not available, skipping trace update")
        return

    bot_prefix = bot_prefix.lower()
    # 2-byte and wider hashes confirm the link at that width
    edge_width = {"prefix_bytes": 2} if width >= 4 else {}

    # Resolve is_our_trace
    if is_our_trace is None:
        is_our_trace = False
        packet_hash = packet_info.get("packet_hash")
        if packet_hash and hasattr(bot, "transmission_tracker"):
            record = bot.transmission_tracker.match_packet_hash(packet_hash, time.time())
            if record:
                is_our_trace = True
                bot.logger.debug("Mesh graph: Trace packet is one we sent (matched transmission record)")

    # Check if this came back through an immediate neighbor
    is_immediate_neighbor = False
    if is_our_trace and len(path_hashes) == 1:
        is_immediate_neighbor = True
        bot.logger.info(
            f"Mesh graph: Trace came back through immediate neighbor {path_hashes[0]} - trusting both directions"
        )

    bot.logger.debug(
        f"Mesh graph: Updating graph from trace pathHashes: {path_hashes} "
        f"(bot is destination: {bot_prefix}, is_our_trace: {is_our_trace}, immediate_neighbor: {is_immediate_neighbor})"
    )

    recency_days = bot.config.getint("Path_Command", "graph_edge_expiration_days", fallback=7)

    from .utils import _get_node_location_from_db, calculate_distance

    bot_location = None
    try:
        bot_location_result = _get_node_location_from_db(bot, bot_prefix, None, recency_days)
        if bot_location_result:
            bot_location, _ = bot_location_result
    except Exception as e:
        bot.logger.debug(f"Could not get bot location: {e}")

    if len(path_hashes) == 0:
        return


    if is_immediate_neighbor:
        neighbor_prefix = path_hashes[0].lower()
        if neighbor_prefix == bot_prefix:
            # The graph, keyed by prefix, can't tell this neighbor from the bot
            bot.logger.debug(f"Mesh graph: Neighbor {neighbor_prefix} shares the bot's prefix, skipping trace update")
            return
        neighbor_key = None
        try:
            _, unique_key = unique_recent_repeater_key(bot.db_manager, neighbor_prefix, recency_days)
            if unique_key:
                neighbor_key = unique_key
        except Exception as e:
            bot.logger.debug(f"Error checking uniqueness for immediate neighbor {neighbor_prefix}: {e}")

        geographic_distance = None
        try:
            if bot_location:
                neighbor_result = _get_node_location_from_db(bot, neighbor_prefix, bot_location, recency_days)
                if neighbor_result:
                    # The location match is only for the distance, not the neighbor's identity
                    neighbor_location, _ = neighbor_result
                    if neighbor_location and bot_location:
                        geographic_distance = calculate_distance(
                            neighbor_location[0], neighbor_location[1],
                            bot_location[0], bot_location[1],
                        )
        except Exception as e:
            bot.logger.debug(f"Could not calculate distance for immediate neighbor edge: {e}")

        mesh_graph.add_edge(
            from_prefix=bot_prefix,
            to_prefix=neighbor_prefix,
            from_public_key=bot_key,
            to_public_key=neighbor_key,
            hop_position=1,
            geographic_distance=geographic_distance,
            **edge_width,
        )
        mesh_graph.add_edge(
            from_prefix=neighbor_prefix,
            to_prefix=bot_prefix,
            from_public_key=neighbor_key,
            to_public_key=bot_key,
            hop_position=1,
            geographic_distance=geographic_distance,
            **edge_width,
        )
        bot.logger.info(f"Mesh graph: Created trusted bidirectional edge with immediate neighbor {neighbor_prefix}")
        return

    # Regular case: the bot received the trace at the end of its path. Our own
    # trace also left from the bot, so it confirms the bot -> first hop link too.
    chain = [h.lower() for h in path_hashes]
    if is_our_trace:
        chain = [bot_prefix] + chain
    chain = chain + [bot_prefix]
    # Which chain positions are the bot itself (a hop may share its short prefix)
    is_bot = [is_our_trace and i == 0 or i == len(chain) - 1 for i in range(len(chain))]

    hop_keys: dict[str, Optional[str]] = {}
    keys: list[Optional[str]] = []
    for i, node in enumerate(chain):
        if is_bot[i]:
            keys.append(bot_key)
        else:
            if node not in hop_keys:
                hop_keys[node] = _unique_repeater_key(bot, node, recency_days)
            keys.append(hop_keys[node])

    # Locations only feed distances; a location match among colliding prefixes is a
    # guess, so it never becomes the stored public key.
    locations: dict[int, Optional[tuple[float, float]]] = {}
    reference = bot_location
    for i, node in enumerate(chain):
        if is_bot[i]:
            locations[i] = bot_location
        else:
            locations[i] = None
            try:
                found = _get_node_location_from_db(bot, node, reference, recency_days)
                if found and found[0]:
                    locations[i] = found[0]
            except Exception as e:
                bot.logger.debug(f"Could not locate trace node {node}: {e}")
        if locations[i]:
            reference = locations[i]

    for i in range(len(chain) - 1):
        from_node, to_node = chain[i], chain[i + 1]
        if from_node == to_node:
            # A hop sharing its neighbor's prefix: the graph, keyed by prefix, can't tell them apart
            continue
        geographic_distance = None
        from_location, to_location = locations[i], locations[i + 1]
        if from_location and to_location:
            try:
                geographic_distance = calculate_distance(
                    from_location[0], from_location[1], to_location[0], to_location[1],
                )
            except Exception as e:
                bot.logger.debug(f"Could not calculate distance for trace edge {from_node}->{to_node}: {e}")
        mesh_graph.add_edge(
            from_prefix=from_node,
            to_prefix=to_node,
            from_public_key=keys[i],
            to_public_key=keys[i + 1],
            hop_position=i + 1,
            geographic_distance=geographic_distance,
            **edge_width,
        )


def _unique_repeater_key(bot: Any, prefix: str, recency_days: int) -> Optional[str]:
    """The public key of the only recently heard repeater or room server with ``prefix``, else None."""
    try:
        _, unique_key = unique_recent_repeater_key(bot.db_manager, prefix, recency_days)
        if unique_key:
            return unique_key
    except Exception as e:
        bot.logger.debug(f"Error checking uniqueness for trace node {prefix}: {e}")
    return None
