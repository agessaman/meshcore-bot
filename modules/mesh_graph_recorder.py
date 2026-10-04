"""Mesh graph and observed-path recording, mixed into MessageHandler.

Edges from packet paths, adverts and traces, and the observed_paths rows behind them."""

from typing import Any

from .contacts_repo import unique_recent_repeater_key
from .graph_trace_helper import update_mesh_graph_from_trace_data
from .packet_decode import split_path_hex


class MeshGraphRecorderMixin:
    """Mixed into MessageHandler."""

    _path_hex_to_nodes: Any
    bot: Any
    logger: Any

    def _mesh_graph_capturing(self) -> bool:
        """True when the bot has a mesh graph and it is capturing edges.

        Truth-tests the graph and capture_enabled once each, as the inline guards did.
        """
        if not hasattr(self.bot, "mesh_graph") or not self.bot.mesh_graph:
            return False
        return bool(self.bot.mesh_graph.capture_enabled)

    def _update_mesh_graph(self, path_nodes: list[str], packet_info: dict[str, Any]) -> None:
        """Update mesh graph with edges from a message path.

        path_nodes may be 2, 4, or 6 hex chars per node depending on the packet's
        bytes_per_hop (sender setting). add_edge stores at the resolution provided;
        no truncation, so distinct links (e.g. 7e42→8611 and 7e99→86ff) stay separate.

        Args:
            path_nodes: List of node prefixes in path order (length per node from packet's bytes_per_hop).
            packet_info: Packet information dictionary with routing data.
        """
        if not path_nodes or len(path_nodes) < 2:
            self.logger.debug(f"Mesh graph: Skipping path with < 2 nodes: {path_nodes}")
            return  # Need at least 2 nodes to form an edge

        if not hasattr(self.bot, "mesh_graph") or not self.bot.mesh_graph:
            self.logger.debug("Mesh graph: Graph not initialized, skipping update")
            return  # Graph not initialized

        mesh_graph = self.bot.mesh_graph
        self.logger.debug(f"Mesh graph: Updating graph with path: {path_nodes} ({len(path_nodes)} nodes)")

        # Get recency window from config (default 7 days)
        recency_days = self.bot.config.getint("Path_Command", "graph_edge_expiration_days", fallback=7)

        # Get public keys if available from database
        # Note: We don't check device contacts because repeaters aren't stored on the device
        # IMPORTANT: Only use database lookup if prefix is unique (to avoid wrong public key assignment)
        # In busy meshes, prefixes are rarely unique, so we must verify uniqueness first
        # Also filter by recency to avoid using old/stale repeaters
        node_keys = {}

        for node_prefix in path_nodes:
            try:
                # First check if prefix is unique in database (within recency window)
                match_count, unique_key = unique_recent_repeater_key(self.bot.db_manager, node_prefix, recency_days)
                if match_count == 1:
                    # Prefix is unique within recency window - safe to use database lookup
                    if unique_key:
                        node_keys[node_prefix] = unique_key
                        self.logger.debug(
                            f"Mesh graph: Found unique public key for prefix {node_prefix} from database: {unique_key[:16]}..."
                        )
                else:
                    # Prefix collision or no recent matches - don't use database lookup (would risk wrong public key)
                    self.logger.debug(
                        f"Mesh graph: Prefix {node_prefix} has {match_count} recent matches in database, skipping public key lookup (not unique or stale)"
                    )
            except Exception as e:
                self.logger.debug(f"Error looking up public key for prefix {node_prefix}: {e}")

        # Calculate geographic distances if locations are available
        from .utils import _get_node_location_from_db, calculate_distance

        # Extract edges from path
        for i in range(len(path_nodes) - 1):
            from_prefix = path_nodes[i]
            to_prefix = path_nodes[i + 1]
            hop_position = i + 1  # Position in path (1-indexed)

            # Get public keys if available
            from_key = node_keys.get(from_prefix)
            to_key = node_keys.get(to_prefix)

            # Calculate geographic distance if both nodes have locations
            # IMPORTANT: Only use public keys that we're 100% certain of (from uniqueness check above)
            # For location lookups, we can use distance-based selection to get better distance calculations,
            # but we do NOT store those selected public keys - we only store keys we're certain of
            geographic_distance = None
            try:
                from_location = None
                to_location = None

                # Try to get location using full public key first (more accurate)
                if from_key:
                    from_location = self._get_location_by_public_key(from_key)
                if not from_location:
                    # For LoRa: prefer shorter edges - use to_location as reference if we have it
                    # This helps resolve prefix collisions by preferring closer repeaters for distance calculation
                    to_location_temp = None
                    if to_key:
                        to_location_temp = self._get_location_by_public_key(to_key)
                    if not to_location_temp:
                        # Try to get to_location first to use as reference
                        # Use bot location as fallback reference to ensure distance-based selection
                        bot_location_ref = self._get_bot_location_fallback()
                        to_location_result = _get_node_location_from_db(
                            self.bot, to_prefix, bot_location_ref, recency_days
                        )
                        if to_location_result:
                            to_location_temp, temp_key = to_location_result
                            if not to_key and temp_key:
                                to_key = temp_key  # Store the selected public key for distance-based selection

                    # Get from_location using to_location as reference (prefers shorter distance for LoRa)
                    # If to_location not available, use bot location as fallback reference
                    reference_for_from = to_location_temp if to_location_temp else self._get_bot_location_fallback()
                    # Capture the selected public key when distance-based selection is used
                    # Apply recency window to avoid using stale repeaters
                    from_location_result = _get_node_location_from_db(
                        self.bot, from_prefix, reference_for_from, recency_days
                    )
                    if from_location_result:
                        from_location, selected_from_key = from_location_result
                        if not from_key and selected_from_key:
                            from_key = selected_from_key  # Store the selected public key

                if to_key:
                    to_location = self._get_location_by_public_key(to_key)
                if not to_location:
                    # Use from_location as reference to prefer shorter distance for LoRa
                    # If from_location not available, use bot location as fallback reference
                    reference_for_to = from_location if from_location else self._get_bot_location_fallback()
                    # Capture the selected public key when distance-based selection is used
                    # Apply recency window to avoid using stale repeaters
                    to_location_result = _get_node_location_from_db(self.bot, to_prefix, reference_for_to, recency_days)
                    if to_location_result:
                        to_location, selected_to_key = to_location_result
                        if not to_key and selected_to_key:
                            to_key = selected_to_key  # Store the selected public key

                if from_location and to_location:
                    geographic_distance = calculate_distance(
                        from_location[0], from_location[1], to_location[0], to_location[1]
                    )
            except Exception as e:
                self.logger.debug(f"Could not calculate distance for edge {from_prefix}->{to_prefix}: {e}")

            # Add edge to graph - only use public keys we're 100% certain of (from uniqueness check)
            # Do NOT use public keys from distance-based selection - we can't be certain they're correct
            self.logger.debug(f"Mesh graph: Adding edge {from_prefix} -> {to_prefix} (hop {hop_position})")
            mesh_graph.add_edge(
                from_prefix=from_prefix,
                to_prefix=to_prefix,
                from_public_key=from_key,  # Only if prefix was unique (certain)
                to_public_key=to_key,  # Only if prefix was unique (certain)
                hop_position=hop_position,
                geographic_distance=geographic_distance,
            )

    def _store_observed_path(
        self,
        advert_data: dict[str, Any],
        path_hex: str,
        path_length: int,
        packet_type: str,
        packet_hash: str | None = None,
        bytes_per_hop: int | None = None,
    ) -> None:
        """Store a complete path in the observed_paths table.

        Args:
            advert_data: Advertisement data dictionary with public_key (for adverts).
            path_hex: Hex string of the complete path.
            path_length: Length of the path in bytes.
            packet_type: Type of packet ('advert', 'message', etc.).
            packet_hash: Optional packet hash to group paths from the same packet.
            bytes_per_hop: Optional bytes per hop (1, 2, or 3) for multi-byte path decode; None = legacy 1.
        """
        if not path_hex or path_length < 2:
            return  # Need at least 2 bytes (1 node) to form a path

        try:
            # Parse path to extract from_prefix and to_prefix (use bytes_per_hop when provided for multi-byte paths)
            hex_chars = (bytes_per_hop or 1) * 2
            if bytes_per_hop is not None and bytes_per_hop > 0:
                path_nodes = split_path_hex(path_hex, hex_chars)
            else:
                path_nodes = self._path_hex_to_nodes(path_hex)

            if len(path_nodes) < 1:
                return  # No valid path nodes

            from_prefix = path_nodes[0]
            to_prefix = path_nodes[-1]  # Last hop in path (last repeater that forwarded to bot)

            # Get public_key for adverts (NULL for messages)
            public_key = advert_data.get("public_key", "") if packet_type == "advert" else None

            # Check if path already exists
            if public_key:
                # For adverts: check by public_key, path_hex, packet_type
                query = """
                    SELECT id, observation_count, last_seen
                    FROM observed_paths
                    WHERE public_key = ? AND path_hex = ? AND packet_type = ?
                """
                existing = self.bot.db_manager.execute_query(query, (public_key, path_hex, packet_type))
            else:
                # For messages: check by from_prefix, to_prefix, path_hex, packet_type
                query = """
                    SELECT id, observation_count, last_seen
                    FROM observed_paths
                    WHERE from_prefix = ? AND to_prefix = ? AND path_hex = ? AND packet_type = ?
                    AND public_key IS NULL
                """
                existing = self.bot.db_manager.execute_query(query, (from_prefix, to_prefix, path_hex, packet_type))

            from datetime import datetime

            now = datetime.now()

            if existing and len(existing) > 0:
                # Path exists - update observation count and last_seen
                path_id = existing[0]["id"]
                current_count = existing[0].get("observation_count", 1)
                update_query = """
                    UPDATE observed_paths
                    SET observation_count = ?, last_seen = ?
                    WHERE id = ?
                """
                self.bot.db_manager.execute_update(update_query, (current_count + 1, now.isoformat(), path_id))
                self.logger.debug(
                    f"Updated observed_paths entry for {packet_type} path {path_hex[:20]}... (count: {current_count + 1})"
                )
            else:
                # New path - insert
                insert_query = """
                    INSERT INTO observed_paths
                    (public_key, packet_hash, from_prefix, to_prefix, path_hex, path_length, bytes_per_hop, packet_type, first_seen, last_seen, observation_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """
                # Only store packet_hash if it's valid (not None and not the default invalid hash)
                stored_packet_hash = packet_hash if (packet_hash and packet_hash != "0000000000000000") else None
                self.bot.db_manager.execute_update(
                    insert_query,
                    (
                        public_key,
                        stored_packet_hash,
                        from_prefix,
                        to_prefix,
                        path_hex,
                        path_length,
                        bytes_per_hop,
                        packet_type,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                self.logger.debug(
                    f"Stored new {packet_type} path in observed_paths: {from_prefix}->{to_prefix} ({path_length} bytes)"
                )

        except Exception as e:
            self.logger.warning(f"Error storing observed path: {e}")
            import traceback

            self.logger.debug(traceback.format_exc())

    def _get_bot_location_fallback(self) -> tuple[float, float] | None:
        """Get bot location from config to use as fallback reference for distance-based selection.

        Returns:
            Optional[Tuple[float, float]]: (latitude, longitude) or None if not configured.
        """
        try:
            lat = self.bot.config.getfloat("Bot", "bot_latitude", fallback=None)
            lon = self.bot.config.getfloat("Bot", "bot_longitude", fallback=None)

            if lat is not None and lon is not None:
                # Validate coordinates
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return (lat, lon)
            return None
        except Exception as e:
            self.logger.debug(f"Error getting bot location fallback: {e}")
            return None

    def _get_location_by_public_key(self, public_key: str) -> tuple[float, float] | None:
        """Get location for a full public key (more accurate than prefix lookup).

        Prefers starred repeaters if there are somehow multiple entries (shouldn't happen with full key).

        Args:
            public_key: Full public key string.

        Returns:
            Optional[Tuple[float, float]]: (latitude, longitude) or None.
        """
        try:
            query = """
                SELECT latitude, longitude
                FROM complete_contact_tracking
                WHERE public_key = ?
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
                AND role IN ('repeater', 'roomserver')
                ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 1
            """
            results = self.bot.db_manager.execute_query(query, (public_key,))
            if results:
                row = results[0]
                lat = row.get("latitude")
                lon = row.get("longitude")
                if lat is not None and lon is not None:
                    return (float(lat), float(lon))
        except Exception as e:
            self.logger.debug(f"Error getting location by public key {public_key[:16]}...: {e}")
        return None

    def _update_mesh_graph_from_advert(
        self, advert_data: dict[str, Any], out_path: str, out_path_len: int, packet_info: dict[str, Any]
    ) -> None:
        """Update mesh graph with edges from an advertisement's out_path.

        Creates an edge from the advertising device to the first hop in their out_path,
        and edges between subsequent hops in the path.

        Args:
            advert_data: Advertisement data dictionary with public_key.
            out_path: Hex string of the path the advert took to reach us.
            out_path_len: Length of the path in bytes.
            packet_info: Packet information dictionary with routing data.
        """
        if not out_path or out_path_len < 2:
            return  # Need at least 2 bytes (1 node) to form an edge

        if not hasattr(self.bot, "mesh_graph") or not self.bot.mesh_graph:
            return  # Graph not initialized

        mesh_graph = self.bot.mesh_graph

        # Get advertiser's public key
        advertiser_key = advert_data.get("public_key", "")
        if not advertiser_key:
            self.logger.debug("Mesh graph: No public key in advert data, skipping graph update")
            return

        advertiser_prefix = advertiser_key[: self.bot.prefix_hex_chars].lower()

        # Parse path from hex string (use bytes_per_hop from packet for multi-byte paths)
        hex_chars = (packet_info.get("bytes_per_hop") or 1) * 2
        path_nodes = []
        for i in range(0, len(out_path), hex_chars):
            if i + hex_chars <= len(out_path):
                path_nodes.append(out_path[i : i + hex_chars].lower())

        if len(path_nodes) == 0:
            return  # No valid path nodes

        self.logger.debug(f"Mesh graph: Updating graph from advert path: {advertiser_prefix} -> {path_nodes}")

        # Get recency window from config (default 7 days)
        recency_days = self.bot.config.getint("Path_Command", "graph_edge_expiration_days", fallback=7)

        # Calculate geographic distances if locations are available
        from .utils import _get_node_location_from_db, calculate_distance

        # Create edge from advertiser to first hop in path
        first_hop = path_nodes[0]
        geographic_distance = None
        first_hop_key = None

        # IMPORTANT: Only use public keys we're 100% certain of
        # For the first hop, we can only be certain if the prefix is unique (and recent)
        try:
            # Check if first_hop prefix is unique within recency window (only then can we be certain of the public key)
            match_count, unique_key = unique_recent_repeater_key(self.bot.db_manager, first_hop, recency_days)
            if match_count == 1:
                # Prefix is unique within recency window - safe to use database lookup
                if unique_key:
                    first_hop_key = unique_key
                    self.logger.debug(
                        f"Mesh graph: Found unique public key for first hop {first_hop}: {first_hop_key[:16]}..."
                    )
            else:
                self.logger.debug(
                    f"Mesh graph: First hop prefix {first_hop} has {match_count} recent matches, cannot be certain of public key"
                )
        except Exception as e:
            self.logger.debug(f"Error checking uniqueness for first hop {first_hop}: {e}")

        try:
            # Use full public key for advertiser (we're 100% certain - it's from the event)
            advertiser_location = None
            if advertiser_key:
                advertiser_location = self._get_location_by_public_key(advertiser_key)
            if not advertiser_location:
                # Get first_hop location first to use as reference for LoRa distance preference
                # Use bot location as fallback reference to ensure distance-based selection
                bot_location_ref = self._get_bot_location_fallback()
                first_hop_temp_result = _get_node_location_from_db(self.bot, first_hop, bot_location_ref, recency_days)
                first_hop_location_temp: tuple[float, float] | None
                if first_hop_temp_result:
                    first_hop_location_temp, _ = first_hop_temp_result
                else:
                    first_hop_location_temp = bot_location_ref  # Use bot location as fallback

                if first_hop_location_temp:
                    advertiser_result = _get_node_location_from_db(
                        self.bot, advertiser_prefix, first_hop_location_temp, recency_days
                    )
                    if advertiser_result:
                        advertiser_location, _ = advertiser_result

            # Get first_hop location using advertiser location as reference for LoRa preference
            # Capture the selected public key when distance-based selection is used
            # Apply recency window to avoid using stale repeaters
            first_hop_result = _get_node_location_from_db(self.bot, first_hop, advertiser_location, recency_days)
            if first_hop_result:
                first_hop_location, selected_first_hop_key = first_hop_result
                if not first_hop_key and selected_first_hop_key:
                    first_hop_key = selected_first_hop_key  # Store the selected public key

            if advertiser_location and first_hop_location:
                geographic_distance = calculate_distance(
                    advertiser_location[0], advertiser_location[1], first_hop_location[0], first_hop_location[1]
                )
        except Exception as e:
            self.logger.debug(f"Could not calculate distance for advert edge {advertiser_prefix}->{first_hop}: {e}")

        # Add edge from advertiser to first hop
        # from_public_key: advertiser_key (100% certain - from event)
        # to_public_key: first_hop_key (only if prefix was unique - certain)
        mesh_graph.add_edge(
            from_prefix=advertiser_prefix,
            to_prefix=first_hop,
            from_public_key=advertiser_key,  # 100% certain - from NEW_CONTACT event
            to_public_key=first_hop_key,  # Only if prefix was unique (certain)
            hop_position=1,  # First hop in path
            geographic_distance=geographic_distance,
        )

        # Create edges between subsequent hops in the path
        # Track previous location to use as reference for better distance-based selection
        # Start with first_hop_location (if available) or advertiser_location as reference
        previous_location = None
        try:
            if "first_hop_location" in locals() and first_hop_location:
                previous_location = first_hop_location
            elif advertiser_location:
                previous_location = advertiser_location
        except:
            pass

        for i in range(len(path_nodes) - 1):
            from_node = path_nodes[i]
            to_node = path_nodes[i + 1]
            hop_position = i + 2  # Position in path (1-indexed, advertiser is 0)

            # IMPORTANT: Only use public keys we're 100% certain of (when prefix is unique and recent)
            from_node_key = None
            to_node_key = None

            # Check if from_node prefix is unique within recency window
            try:
                _, unique_key = unique_recent_repeater_key(self.bot.db_manager, from_node, recency_days)
                if unique_key:
                    from_node_key = unique_key
                    self.logger.debug(
                        f"Mesh graph: Found unique public key for {from_node}: {from_node_key[:16]}..."
                    )
            except Exception as e:
                self.logger.debug(f"Error checking uniqueness for {from_node}: {e}")

            # Check if to_node prefix is unique within recency window
            try:
                _, unique_key = unique_recent_repeater_key(self.bot.db_manager, to_node, recency_days)
                if unique_key:
                    to_node_key = unique_key
                    self.logger.debug(f"Mesh graph: Found unique public key for {to_node}: {to_node_key[:16]}...")
            except Exception as e:
                self.logger.debug(f"Error checking uniqueness for {to_node}: {e}")

            # Calculate geographic distance if available
            # For LoRa, prefer shorter distances when resolving prefix collisions for location lookup
            # IMPORTANT: Use previous_location as reference to ensure we select the closer repeater
            # NOTE: We do NOT store public keys from distance-based selection - only use them for location
            # Apply recency window to avoid using stale repeaters
            geographic_distance = None
            try:
                from .utils import _get_node_location_from_db, calculate_distance

                # Get from_location using previous_location as reference (ensures we select closer repeater)
                # Use bot location as fallback if previous_location not available
                reference_for_from = previous_location if previous_location else self._get_bot_location_fallback()
                from_result = _get_node_location_from_db(self.bot, from_node, reference_for_from, recency_days)
                if from_result:
                    from_location, selected_from_key = from_result
                    if not from_node_key and selected_from_key:
                        from_node_key = selected_from_key  # Store the selected public key
                else:
                    from_location = None

                # Get to_location using from_location as reference
                # Use bot location as fallback if from_location not available
                reference_for_to = from_location if from_location else self._get_bot_location_fallback()
                to_result = _get_node_location_from_db(self.bot, to_node, reference_for_to, recency_days)
                if to_result:
                    to_location, selected_to_key = to_result
                    if not to_node_key and selected_to_key:
                        to_node_key = selected_to_key  # Store the selected public key
                else:
                    to_location = None

                # Re-get from_location with to_location as reference (for better collision resolution)
                if to_location:
                    from_result2 = _get_node_location_from_db(self.bot, from_node, to_location, recency_days)
                    if from_result2:
                        from_location, selected_from_key2 = from_result2
                        if not from_node_key and selected_from_key2:
                            from_node_key = selected_from_key2

                # Re-get to_location with from_location as reference
                if from_location:
                    to_result2 = _get_node_location_from_db(self.bot, to_node, from_location, recency_days)
                    if to_result2:
                        to_location, selected_to_key2 = to_result2
                        if not to_node_key and selected_to_key2:
                            to_node_key = selected_to_key2

                # Update previous_location for next iteration
                previous_location = to_location if to_location else from_location

                if from_location and to_location:
                    geographic_distance = calculate_distance(
                        from_location[0], from_location[1], to_location[0], to_location[1]
                    )
            except Exception as e:
                self.logger.debug(f"Could not calculate distance for edge {from_node}->{to_node}: {e}")

            # Add edge between path nodes - only use public keys we're 100% certain of (from uniqueness check)
            mesh_graph.add_edge(
                from_prefix=from_node,
                to_prefix=to_node,
                from_public_key=from_node_key,  # Only if prefix was unique (certain)
                to_public_key=to_node_key,  # Only if prefix was unique (certain)
                hop_position=hop_position,
                geographic_distance=geographic_distance,
            )

    def _update_mesh_graph_from_trace(self, path_hashes: list[str], packet_info: dict[str, Any]) -> None:
        """Update mesh graph with edges from a trace packet's pathHashes. Delegates to shared helper."""
        update_mesh_graph_from_trace_data(self.bot, path_hashes, packet_info)
