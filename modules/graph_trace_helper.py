#!/usr/bin/env python3
"""
Graph update helper for trace data.
Shared by message_handler (on RX) and trace command (when TRACE_DATA is received).
"""

import time
from typing import Any, Dict, List, Optional


def update_mesh_graph_from_trace_data(
    bot: Any,
    path_hashes: List[str],
    packet_info: Dict[str, Any],
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
        path_hashes: List of node hash prefixes (1-byte each, as 2-char hex strings) from trace payload.
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
    bot_prefix = bot.transmission_tracker.bot_prefix

    if not bot_prefix:
        bot.logger.debug("Mesh graph: Bot prefix not available, skipping trace update")
        return

    bot_prefix = bot_prefix.lower()

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

    from .utils import calculate_distance, _get_node_location_from_db

    bot_location = None
    try:
        bot_location_result = _get_node_location_from_db(bot, bot_prefix, None, recency_days)
        if bot_location_result:
            bot_location, _ = bot_location_result
    except Exception as e:
        bot.logger.debug(f"Could not get bot location: {e}")

    if len(path_hashes) == 0:
        return

    last_node = path_hashes[-1].lower()

    if is_immediate_neighbor:
        neighbor_prefix = path_hashes[0].lower()
        neighbor_key = None
        try:
            count_query = f"""
                SELECT COUNT(DISTINCT public_key) as count
                FROM complete_contact_tracking
                WHERE public_key LIKE ?
                AND role IN ('repeater', 'roomserver')
                AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
            """
            prefix_pattern = f"{neighbor_prefix}%"
            count_results = bot.db_manager.execute_query(count_query, (prefix_pattern,))
            if count_results and count_results[0].get("count", 0) == 1:
                query = f"""
                    SELECT public_key
                    FROM complete_contact_tracking
                    WHERE public_key LIKE ?
                    AND role IN ('repeater', 'roomserver')
                    AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                    ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                    LIMIT 1
                """
                results = bot.db_manager.execute_query(query, (prefix_pattern,))
                if results and results[0].get("public_key"):
                    neighbor_key = results[0]["public_key"]
        except Exception as e:
            bot.logger.debug(f"Error checking uniqueness for immediate neighbor {neighbor_prefix}: {e}")

        bot_key = None
        if hasattr(bot, "meshcore") and bot.meshcore and getattr(bot.meshcore, "device", None):
            try:
                device_info = bot.meshcore.device
                if hasattr(device_info, "public_key"):
                    pubkey = device_info.public_key
                    if isinstance(pubkey, str):
                        bot_key = pubkey
                    elif isinstance(pubkey, bytes):
                        bot_key = pubkey.hex()
            except Exception as e:
                bot.logger.debug(f"Could not get bot public key: {e}")

        geographic_distance = None
        try:
            if bot_location:
                neighbor_result = _get_node_location_from_db(bot, neighbor_prefix, bot_location, recency_days)
                if neighbor_result:
                    neighbor_location, selected_neighbor_key = neighbor_result
                    if not neighbor_key and selected_neighbor_key:
                        neighbor_key = selected_neighbor_key
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
        )
        mesh_graph.add_edge(
            from_prefix=neighbor_prefix,
            to_prefix=bot_prefix,
            from_public_key=neighbor_key,
            to_public_key=bot_key,
            hop_position=1,
            geographic_distance=geographic_distance,
        )
        bot.logger.info(f"Mesh graph: Created trusted bidirectional edge with immediate neighbor {neighbor_prefix}")
        return

    # Regular case: trace from elsewhere, bot is destination
    geographic_distance = None
    last_node_key = None
    try:
        count_query = f"""
            SELECT COUNT(DISTINCT public_key) as count
            FROM complete_contact_tracking
            WHERE public_key LIKE ?
            AND role IN ('repeater', 'roomserver')
            AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
        """
        prefix_pattern = f"{last_node}%"
        count_results = bot.db_manager.execute_query(count_query, (prefix_pattern,))
        if count_results and count_results[0].get("count", 0) == 1:
            query = f"""
                SELECT public_key
                FROM complete_contact_tracking
                WHERE public_key LIKE ?
                AND role IN ('repeater', 'roomserver')
                AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 1
            """
            results = bot.db_manager.execute_query(query, (prefix_pattern,))
            if results and results[0].get("public_key"):
                last_node_key = results[0]["public_key"]
    except Exception as e:
        bot.logger.debug(f"Error checking uniqueness for trace last_node {last_node}: {e}")

    bot_key = None
    if hasattr(bot, "meshcore") and bot.meshcore and getattr(bot.meshcore, "device", None):
        try:
            device_info = bot.meshcore.device
            if hasattr(device_info, "public_key"):
                pubkey = device_info.public_key
                if isinstance(pubkey, str):
                    bot_key = pubkey
                elif isinstance(pubkey, bytes):
                    bot_key = pubkey.hex()
        except Exception as e:
            bot.logger.debug(f"Could not get bot public key: {e}")

    try:
        if bot_location:
            last_node_result = _get_node_location_from_db(bot, last_node, bot_location, recency_days)
            if last_node_result:
                last_node_location, selected_key = last_node_result
                if not last_node_key and selected_key:
                    last_node_key = selected_key
                if last_node_location and bot_location:
                    geographic_distance = calculate_distance(
                        last_node_location[0], last_node_location[1],
                        bot_location[0], bot_location[1],
                    )
    except Exception as e:
        bot.logger.debug(f"Could not calculate distance for trace edge {last_node}->{bot_prefix}: {e}")

    mesh_graph.add_edge(
        from_prefix=last_node,
        to_prefix=bot_prefix,
        from_public_key=last_node_key,
        to_public_key=bot_key,
        hop_position=len(path_hashes),
        geographic_distance=geographic_distance,
    )

    # Create edges between nodes in the pathHashes (if more than one)
    previous_location = bot_location
    for i in range(len(path_hashes) - 1, 0, -1):
        from_node = path_hashes[i - 1].lower()
        to_node = path_hashes[i].lower()
        hop_position = len(path_hashes) - i

        from_node_key = None
        to_node_key = None
        for node, key_var in [(from_node, "from_node_key"), (to_node, "to_node_key")]:
            try:
                count_query = f"""
                    SELECT COUNT(DISTINCT public_key) as count
                    FROM complete_contact_tracking
                    WHERE public_key LIKE ?
                    AND role IN ('repeater', 'roomserver')
                    AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                """
                prefix_pattern = f"{node}%"
                count_results = bot.db_manager.execute_query(count_query, (prefix_pattern,))
                if count_results and count_results[0].get("count", 0) == 1:
                    query = f"""
                        SELECT public_key
                        FROM complete_contact_tracking
                        WHERE public_key LIKE ?
                        AND role IN ('repeater', 'roomserver')
                        AND COALESCE(last_advert_timestamp, last_heard) >= datetime('now', '-{recency_days} days')
                        ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                        LIMIT 1
                    """
                    results = bot.db_manager.execute_query(query, (prefix_pattern,))
                    if results and results[0].get("public_key"):
                        if key_var == "from_node_key":
                            from_node_key = results[0]["public_key"]
                        else:
                            to_node_key = results[0]["public_key"]
            except Exception as e:
                bot.logger.debug(f"Error checking uniqueness for trace node {node}: {e}")

        geographic_distance = None
        try:
            if previous_location:
                from_result = _get_node_location_from_db(bot, from_node, previous_location, recency_days)
                if from_result:
                    from_location, selected_from_key = from_result
                    if not from_node_key and selected_from_key:
                        from_node_key = selected_from_key
                    to_result = _get_node_location_from_db(bot, to_node, from_location, recency_days)
                    if to_result:
                        to_location, selected_to_key = to_result
                        if not to_node_key and selected_to_key:
                            to_node_key = selected_to_key
                        if from_location and to_location:
                            geographic_distance = calculate_distance(
                                from_location[0], from_location[1],
                                to_location[0], to_location[1],
                            )
                            previous_location = from_location
        except Exception as e:
            bot.logger.debug(f"Could not calculate distance for trace edge {from_node}->{to_node}: {e}")

        mesh_graph.add_edge(
            from_prefix=from_node,
            to_prefix=to_node,
            from_public_key=from_node_key,
            to_public_key=to_node_key,
            hop_position=hop_position,
            geographic_distance=geographic_distance,
        )
