#!/usr/bin/env python3
"""
Mesh Graph Module
Tracks observed connections between repeaters to improve path guessing accuracy.
Persists graph state across bot restarts for development scenarios.
"""

import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict


class MeshGraph:
    """Graph structure tracking observed connections between mesh nodes."""
    
    def __init__(self, bot):
        """Initialize the mesh graph.
        
        Args:
            bot: Bot instance with db_manager and config access.
        """
        self.bot = bot
        self.logger = bot.logger
        self.db_manager = bot.db_manager
        
        # In-memory graph storage: {(from_prefix, to_prefix): edge_data}
        self.edges: Dict[Tuple[str, str], Dict] = {}
        
        # Track pending updates for batched writes
        self.pending_updates: Set[Tuple[str, str]] = set()
        self.pending_lock = threading.Lock()
        
        # Write strategy configuration
        self.write_strategy = bot.config.get('Path_Command', 'graph_write_strategy', fallback='hybrid')
        self.batch_interval = bot.config.getint('Path_Command', 'graph_batch_interval_seconds', fallback=30)
        self.batch_max_pending = bot.config.getint('Path_Command', 'graph_batch_max_pending', fallback=100)
        self.startup_load_days = bot.config.getint('Path_Command', 'graph_startup_load_days', fallback=0)
        
        # Background task for batched writes
        self._batch_task = None
        self._shutdown_event = threading.Event()
        
        # Load graph from database on startup
        self._load_from_database()
        
        # Start background batch writer if needed
        if self.write_strategy in ('batched', 'hybrid'):
            self._start_batch_writer()
    
    def _load_from_database(self):
        """Load graph edges from database on startup."""
        try:
            query = '''
                SELECT from_prefix, to_prefix, from_public_key, to_public_key,
                       observation_count, first_seen, last_seen, avg_hop_position,
                       geographic_distance
                FROM mesh_connections
            '''
            
            # Apply date filter if configured
            if self.startup_load_days > 0:
                cutoff_date = datetime.now() - timedelta(days=self.startup_load_days)
                query += f" WHERE last_seen >= '{cutoff_date.isoformat()}'"
            
            query += " ORDER BY last_seen DESC"
            
            results = self.db_manager.execute_query(query)
            
            edge_count = 0
            for row in results:
                from_prefix = row['from_prefix']
                to_prefix = row['to_prefix']
                edge_key = (from_prefix, to_prefix)
                
                self.edges[edge_key] = {
                    'from_prefix': from_prefix,
                    'to_prefix': to_prefix,
                    'from_public_key': row.get('from_public_key'),
                    'to_public_key': row.get('to_public_key'),
                    'observation_count': row.get('observation_count', 1),
                    'first_seen': row.get('first_seen'),
                    'last_seen': row.get('last_seen'),
                    'avg_hop_position': row.get('avg_hop_position'),
                    'geographic_distance': row.get('geographic_distance')
                }
                edge_count += 1
            
            self.logger.info(f"Loaded {edge_count} graph edges from database")
            
            # Log statistics
            if edge_count > 0:
                total_observations = sum(e['observation_count'] for e in self.edges.values())
                self.logger.info(f"Graph statistics: {edge_count} edges, {total_observations} total observations")
        
        except Exception as e:
            self.logger.warning(f"Error loading graph from database: {e}")
            # Continue with empty graph
    
    def add_edge(self, from_prefix: str, to_prefix: str, 
                 from_public_key: Optional[str] = None,
                 to_public_key: Optional[str] = None,
                 hop_position: Optional[int] = None,
                 geographic_distance: Optional[float] = None):
        """Add or update an edge in the graph.
        
        Args:
            from_prefix: Source node prefix (2 hex chars).
            to_prefix: Destination node prefix (2 hex chars).
            from_public_key: Full public key of source node (optional).
            to_public_key: Full public key of destination node (optional).
            hop_position: Position in path where this edge was observed (optional).
            geographic_distance: Distance in km between nodes (optional).
        """
        if not from_prefix or not to_prefix:
            return
        
        # Normalize prefixes to lowercase
        from_prefix = from_prefix.lower()[:2]
        to_prefix = to_prefix.lower()[:2]
        
        edge_key = (from_prefix, to_prefix)
        now = datetime.now()
        
        # Update or create edge
        if edge_key in self.edges:
            edge = self.edges[edge_key]
            edge['observation_count'] += 1
            edge['last_seen'] = now
            
            # Update average hop position
            if hop_position is not None:
                current_avg = edge.get('avg_hop_position')
                count = edge['observation_count']
                if current_avg is not None:
                    # Weighted average: (old_avg * (count-1) + new_pos) / count
                    edge['avg_hop_position'] = ((current_avg * (count - 1)) + hop_position) / count
                else:
                    # First time setting hop position
                    edge['avg_hop_position'] = hop_position
            
            # Update public keys if provided (always update if we have a better key)
            # This allows us to fill in missing keys on existing edges
            if from_public_key:
                edge['from_public_key'] = from_public_key
            if to_public_key:
                edge['to_public_key'] = to_public_key
            
            # Update geographic distance if provided
            if geographic_distance is not None:
                edge['geographic_distance'] = geographic_distance
            
            is_new_edge = False
        else:
            # New edge
            self.edges[edge_key] = {
                'from_prefix': from_prefix,
                'to_prefix': to_prefix,
                'from_public_key': from_public_key,
                'to_public_key': to_public_key,
                'observation_count': 1,
                'first_seen': now,
                'last_seen': now,
                'avg_hop_position': hop_position if hop_position is not None else None,
                'geographic_distance': geographic_distance
            }
            is_new_edge = True
        
        # Persist according to write strategy
        self.logger.debug(f"Mesh graph: Edge {edge_key} - new={is_new_edge}, strategy={self.write_strategy}")
        if self.write_strategy == 'immediate':
            self._write_edge_to_db(edge_key, is_new_edge)
        elif self.write_strategy == 'batched':
            with self.pending_lock:
                self.pending_updates.add(edge_key)
                if len(self.pending_updates) >= self.batch_max_pending:
                    # Force flush if too many pending
                    self._flush_pending_updates_sync()
        elif self.write_strategy == 'hybrid':
            if is_new_edge:
                # Immediate write for new edges
                self._write_edge_to_db(edge_key, True)
            else:
                # Batched for updates
                with self.pending_lock:
                    self.pending_updates.add(edge_key)
                    if len(self.pending_updates) >= self.batch_max_pending:
                        # Force flush if too many pending
                        self._flush_pending_updates_sync()
        
        # Notify web viewer of edge update
        self._notify_web_viewer_edge(edge_key, is_new_edge)
    
    def _notify_web_viewer_edge(self, edge_key: Tuple[str, str], is_new: bool):
        """Notify web viewer of edge update via bot integration"""
        try:
            if not hasattr(self.bot, 'web_viewer_integration') or not self.bot.web_viewer_integration:
                return
            
            if not hasattr(self.bot.web_viewer_integration, 'bot_integration'):
                return
            
            edge = self.edges.get(edge_key)
            if not edge:
                return
            
            # Prepare edge data for web viewer
            edge_data = {
                'from_prefix': edge['from_prefix'],
                'to_prefix': edge['to_prefix'],
                'from_public_key': edge.get('from_public_key'),
                'to_public_key': edge.get('to_public_key'),
                'observation_count': edge['observation_count'],
                'first_seen': edge['first_seen'].isoformat() if isinstance(edge['first_seen'], datetime) else str(edge['first_seen']),
                'last_seen': edge['last_seen'].isoformat() if isinstance(edge['last_seen'], datetime) else str(edge['last_seen']),
                'avg_hop_position': edge.get('avg_hop_position'),
                'geographic_distance': edge.get('geographic_distance'),
                'is_new': is_new
            }
            
            # Send update asynchronously
            self.bot.web_viewer_integration.bot_integration.send_mesh_edge_update(edge_data)
        except Exception as e:
            self.logger.debug(f"Error notifying web viewer of edge update: {e}")
    
    def _recalculate_distance_if_needed(
        self,
        edge: Dict,
        conn: Optional[sqlite3.Connection] = None,
        location_cache: Optional[Dict[str, Tuple[float, float]]] = None,
    ) -> Optional[float]:
        """Recalculate geographic distance using full public keys if available.
        
        This ensures we get the correct location when there are prefix collisions.
        
        Args:
            edge: Edge dictionary with prefix and optional public keys.
            conn: Optional existing DB connection for batch operations.
            location_cache: Optional cache for location lookups within a flush (keyed by pk: or prefix:).
            
        Returns:
            Optional[float]: Recalculated distance in km, or None if can't calculate.
        """
        from .utils import calculate_distance
        
        # Get location for 'from' node (conn optional for single-connection batch flush)
        if edge.get('from_public_key'):
            from_location = self._get_location_by_public_key(
                edge['from_public_key'], conn=conn, location_cache=location_cache
            )
        else:
            from_location = None
        if not from_location:
            to_location_temp = None
            if edge.get('to_public_key'):
                to_location_temp = self._get_location_by_public_key(
                    edge['to_public_key'], conn=conn, location_cache=location_cache
                )
            if not to_location_temp:
                to_location_temp = self._get_location_by_prefix(
                    edge['to_prefix'], conn=conn, location_cache=location_cache
                )
            from_location = self._get_location_by_prefix(
                edge['from_prefix'], to_location_temp, conn=conn, location_cache=location_cache
            )
        
        # Get location for 'to' node
        if edge.get('to_public_key'):
            to_location = self._get_location_by_public_key(
                edge['to_public_key'], conn=conn, location_cache=location_cache
            )
        else:
            to_location = None
        if not to_location:
            to_location = self._get_location_by_prefix(
                edge['to_prefix'], from_location, conn=conn, location_cache=location_cache
            )
        
        # Calculate distance if we have both locations
        if from_location and to_location:
            return calculate_distance(
                from_location[0], from_location[1],
                to_location[0], to_location[1]
            )
        
        return None
    
    def _get_location_by_public_key(
        self,
        public_key: str,
        conn: Optional[sqlite3.Connection] = None,
        location_cache: Optional[Dict[str, Tuple[float, float]]] = None,
    ) -> Optional[Tuple[float, float]]:
        """Get location for a full public key (more accurate than prefix lookup).
        
        Prefers starred repeaters if there are somehow multiple entries (shouldn't happen with full key).
        """
        cache_key = f"pk:{public_key}" if location_cache is not None else None
        if cache_key is not None and cache_key in location_cache:
            return location_cache[cache_key]
        try:
            query = '''
                SELECT latitude, longitude 
                FROM complete_contact_tracking 
                WHERE public_key = ?
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
                AND role IN ('repeater', 'roomserver')
                ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 1
            '''
            if conn is not None:
                results = self.db_manager.execute_query_on_connection(conn, query, (public_key,))
            else:
                results = self.db_manager.execute_query(query, (public_key,))
            if results:
                row = results[0]
                lat = row.get('latitude')
                lon = row.get('longitude')
                if lat is not None and lon is not None:
                    result = (float(lat), float(lon))
                    if cache_key is not None:
                        location_cache[cache_key] = result
                    return result
        except Exception as e:
            self.logger.debug(f"Error getting location by public key {public_key[:16]}...: {e}")
        return None
    
    def _get_location_by_prefix(
        self,
        prefix: str,
        reference_location: Optional[Tuple[float, float]] = None,
        conn: Optional[sqlite3.Connection] = None,
        location_cache: Optional[Dict[str, Tuple[float, float]]] = None,
    ) -> Optional[Tuple[float, float]]:
        """Get location for a prefix (fallback when full public key not available).
        
        For LoRa networks, prefers shorter distances when there are prefix collisions,
        as LoRa range is limited by the curve of the earth.
        
        Args:
            prefix: 2-character hex prefix.
            reference_location: Optional (lat, lon) to calculate distance from for LoRa preference.
            conn: Optional existing DB connection for batch operations.
            location_cache: Optional cache for location lookups within a flush.
        """
        if location_cache is not None:
            if reference_location is not None:
                cache_key = f"prefix:{prefix}:{reference_location[0]}:{reference_location[1]}"
            else:
                cache_key = f"prefix:{prefix}"
            if cache_key in location_cache:
                return location_cache[cache_key]
        try:
            prefix_pattern = f"{prefix}%"
            
            # Get all candidates with locations
            query = '''
                SELECT latitude, longitude, is_starred,
                       COALESCE(last_advert_timestamp, last_heard) as last_seen
                FROM complete_contact_tracking 
                WHERE public_key LIKE ?
                AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0
                AND role IN ('repeater', 'roomserver')
            '''
            if conn is not None:
                results = self.db_manager.execute_query_on_connection(conn, query, (prefix_pattern,))
            else:
                results = self.db_manager.execute_query(query, (prefix_pattern,))
            
            if not results:
                return None
            
            # If we have a reference location, prefer shorter distances (LoRa range limitation)
            if reference_location and len(results) > 1:
                from .utils import calculate_distance
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
                    # Sort by: starred first (False < True), then distance (shorter = better for LoRa), then recency
                    candidates_with_distance.sort(key=lambda x: (
                        not x[1],  # Starred first (False < True, so starred=True comes before starred=False)
                        x[0],  # Distance (shorter first)
                        x[2] if x[2] else ''  # More recent first (newer timestamps sort later in string comparison)
                    ))
                    
                    # Get the best candidate
                    best_row = candidates_with_distance[0][3]
                    lat = best_row.get('latitude')
                    lon = best_row.get('longitude')
                    if lat is not None and lon is not None:
                        result = (float(lat), float(lon))
                        if location_cache is not None and reference_location is not None:
                            cache_key = f"prefix:{prefix}:{reference_location[0]}:{reference_location[1]}"
                            location_cache[cache_key] = result
                        return result
            
            # No reference location or single result - use standard ordering
            # Prefer starred, then most recent
            results.sort(key=lambda x: (
                not x.get('is_starred', False),  # Starred first (False < True)
                x.get('last_seen', '') if x.get('last_seen') else ''  # More recent first
            ))
            
            row = results[0]
            lat = row.get('latitude')
            lon = row.get('longitude')
            if lat is not None and lon is not None:
                result = (float(lat), float(lon))
                if location_cache is not None:
                    cache_key = f"prefix:{prefix}" if reference_location is None else f"prefix:{prefix}:{reference_location[0]}:{reference_location[1]}"
                    location_cache[cache_key] = result
                return result
        except Exception as e:
            self.logger.debug(f"Error getting location by prefix {prefix}: {e}")
        return None
    
    def _write_edge_to_db(
        self,
        edge_key: Tuple[str, str],
        is_new: bool,
        conn: Optional[sqlite3.Connection] = None,
        location_cache: Optional[Dict[str, Tuple[float, float]]] = None,
    ):
        """Write a single edge to the database.
        
        Args:
            edge_key: (from_prefix, to_prefix) tuple.
            is_new: True if this is a new edge, False if updating existing.
            conn: Optional existing DB connection for batch operations (caller commits).
            location_cache: Optional cache for location lookups within a flush.
        """
        if edge_key not in self.edges:
            return
        
        edge = self.edges[edge_key]
        
        # Recalculate distance using full public keys if available (more accurate)
        # This fixes issues where prefix collisions cause wrong locations to be used
        if edge.get('from_public_key') or edge.get('to_public_key'):
            recalculated_distance = self._recalculate_distance_if_needed(
                edge, conn=conn, location_cache=location_cache
            )
            if recalculated_distance is not None:
                edge['geographic_distance'] = recalculated_distance
                self.logger.debug(f"Mesh graph: Recalculated distance for {edge_key} using public keys: {recalculated_distance:.1f} km")
        
        try:
            if is_new:
                # Insert new edge
                query = '''
                    INSERT INTO mesh_connections 
                    (from_prefix, to_prefix, from_public_key, to_public_key,
                     observation_count, first_seen, last_seen, avg_hop_position,
                     geographic_distance)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                '''
                params = (
                    edge['from_prefix'],
                    edge['to_prefix'],
                    edge.get('from_public_key'),
                    edge.get('to_public_key'),
                    edge['observation_count'],
                    edge['first_seen'].isoformat() if isinstance(edge['first_seen'], datetime) else edge['first_seen'],
                    edge['last_seen'].isoformat() if isinstance(edge['last_seen'], datetime) else edge['last_seen'],
                    edge.get('avg_hop_position'),
                    edge.get('geographic_distance')
                )
            else:
                # Update existing edge - recalculate distance if we now have public keys
                # Only update distance if we have at least one public key and current distance seems wrong
                current_distance = edge.get('geographic_distance')
                if (edge.get('from_public_key') or edge.get('to_public_key')) and current_distance:
                    recalculated = self._recalculate_distance_if_needed(
                        edge, conn=conn, location_cache=location_cache
                    )
                    if recalculated is not None:
                        # Update if recalculated distance is significantly different (more than 20% difference)
                        if abs(recalculated - current_distance) / max(current_distance, 1.0) > 0.2:
                            edge['geographic_distance'] = recalculated
                            self.logger.info(f"Mesh graph: Corrected distance for {edge_key}: {current_distance:.1f} -> {recalculated:.1f} km")
                
                # Update existing edge
                # Always update public keys if provided (allows filling in missing keys on existing edges)
                from_key = edge.get('from_public_key')
                to_key = edge.get('to_public_key')
                query = self._MESH_EDGE_UPDATE_QUERY
                params = (
                    edge['observation_count'],
                    edge['last_seen'].isoformat() if isinstance(edge['last_seen'], datetime) else edge['last_seen'],
                    edge.get('avg_hop_position'),
                    edge.get('geographic_distance'),
                    from_key,  # First occurrence for CASE WHEN check
                    from_key,  # Second occurrence for value assignment
                    to_key,  # First occurrence for CASE WHEN check
                    to_key,  # Second occurrence for value assignment
                    edge['from_prefix'],
                    edge['to_prefix']
                )
            
            if conn is not None:
                rows_affected = self.db_manager.execute_update_on_connection(conn, query, params)
            else:
                rows_affected = self.db_manager.execute_update(query, params)
            if rows_affected > 0:
                self.logger.debug(f"Mesh graph: Successfully wrote edge {edge_key} to database ({'INSERT' if is_new else 'UPDATE'}, {rows_affected} rows)")
            else:
                self.logger.warning(f"Mesh graph: Edge write returned 0 rows affected for {edge_key}")
        
        except Exception as e:
            self.logger.warning(f"Error writing edge to database: {e}")
            import traceback
            self.logger.debug(traceback.format_exc())
    
    # UPDATE statement used for both single-edge writes and batch executemany
    _MESH_EDGE_UPDATE_QUERY = '''
        UPDATE mesh_connections
        SET observation_count = ?, last_seen = ?,
            avg_hop_position = ?, geographic_distance = ?,
            from_public_key = CASE WHEN ? IS NOT NULL THEN ? ELSE from_public_key END,
            to_public_key = CASE WHEN ? IS NOT NULL THEN ? ELSE to_public_key END
        WHERE from_prefix = ? AND to_prefix = ?
    '''
    
    def _build_update_params_for_edge(
        self,
        edge_key: Tuple[str, str],
        conn: Optional[sqlite3.Connection],
        location_cache: Optional[Dict[str, Tuple[float, float]]],
    ) -> Optional[Tuple]:
        """Build UPDATE params for an edge (for batch executemany). Returns None to skip."""
        if edge_key not in self.edges:
            return None
        try:
            edge = self.edges[edge_key]
            # Recalculate distance if we have public keys (same logic as _write_edge_to_db)
            if edge.get('from_public_key') or edge.get('to_public_key'):
                recalculated_distance = self._recalculate_distance_if_needed(
                    edge, conn=conn, location_cache=location_cache
                )
                if recalculated_distance is not None:
                    edge['geographic_distance'] = recalculated_distance
            current_distance = edge.get('geographic_distance')
            if (edge.get('from_public_key') or edge.get('to_public_key')) and current_distance:
                recalculated = self._recalculate_distance_if_needed(
                    edge, conn=conn, location_cache=location_cache
                )
                if recalculated is not None and abs(recalculated - current_distance) / max(current_distance, 1.0) > 0.2:
                    edge['geographic_distance'] = recalculated
            from_key = edge.get('from_public_key')
            to_key = edge.get('to_public_key')
            last_seen = edge['last_seen']
            if isinstance(last_seen, datetime):
                last_seen = last_seen.isoformat()
            return (
                edge['observation_count'],
                last_seen,
                edge.get('avg_hop_position'),
                edge.get('geographic_distance'),
                from_key,
                from_key,
                to_key,
                to_key,
                edge['from_prefix'],
                edge['to_prefix'],
            )
        except Exception as e:
            self.logger.debug(f"Error building update params for {edge_key}: {e}")
            return None
    
    def _start_batch_writer(self):
        """Start background task for batched writes."""
        def batch_writer_loop():
            while not self._shutdown_event.is_set():
                self._shutdown_event.wait(self.batch_interval)
                if not self._shutdown_event.is_set():
                    # Flush synchronously (database operations are synchronous)
                    self._flush_pending_updates_sync()
        
        import threading
        batch_thread = threading.Thread(target=batch_writer_loop, daemon=True)
        batch_thread.start()
        self._batch_thread = batch_thread
    
    def _flush_pending_updates_sync(self):
        """Flush all pending edge updates to database (synchronous version).
        Uses a single connection for the entire batch to avoid 'unable to open database file'
        when many edges are written in quick succession.
        Handles both new edges (INSERT) and existing edges (UPDATE).
        """
        with self.pending_lock:
            if not self.pending_updates:
                return

            updates = list(self.pending_updates)
            self.pending_updates.clear()

        conn = None
        location_cache: Dict[str, Tuple[float, float]] = {}
        try:
            conn = self.db_manager.get_connection()
            cursor = conn.cursor()
            for edge_key in updates:
                if edge_key not in self.edges:
                    continue
                edge = self.edges[edge_key]
                # Recalculate distance if we have public keys
                if edge.get('from_public_key') or edge.get('to_public_key'):
                    recalculated = self._recalculate_distance_if_needed(
                        edge, conn=conn, location_cache=location_cache
                    )
                    if recalculated is not None:
                        edge['geographic_distance'] = recalculated
                # Check if edge exists in DB
                cursor.execute(
                    'SELECT 1 FROM mesh_connections WHERE from_prefix = ? AND to_prefix = ?',
                    (edge_key[0], edge_key[1]),
                )
                is_new = cursor.fetchone() is None
                self._write_edge_to_db(edge_key, is_new, conn=conn, location_cache=location_cache)
            if conn:
                conn.commit()
        except Exception as e:
            self.logger.warning(f"Error flushing graph updates: {e}")
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        
        if updates:
            self.logger.debug(f"Flushed {len(updates)} pending graph edge updates")
    
    async def _flush_pending_updates(self):
        """Flush all pending edge updates to database (async wrapper)."""
        self._flush_pending_updates_sync()
    
    def has_edge(self, from_prefix: str, to_prefix: str) -> bool:
        """Check if an edge exists in the graph.
        
        Args:
            from_prefix: Source node prefix.
            to_prefix: Destination node prefix.
            
        Returns:
            bool: True if edge exists.
        """
        from_prefix = from_prefix.lower()[:2]
        to_prefix = to_prefix.lower()[:2]
        return (from_prefix, to_prefix) in self.edges
    
    def get_edge(self, from_prefix: str, to_prefix: str) -> Optional[Dict]:
        """Get edge data if it exists.
        
        Args:
            from_prefix: Source node prefix.
            to_prefix: Destination node prefix.
            
        Returns:
            Dict with edge data or None if not found.
        """
        from_prefix = from_prefix.lower()[:2]
        to_prefix = to_prefix.lower()[:2]
        return self.edges.get((from_prefix, to_prefix))
    
    def get_outgoing_edges(self, prefix: str) -> List[Dict]:
        """Get all edges originating from a node.
        
        Args:
            prefix: Node prefix.
            
        Returns:
            List of edge dictionaries.
        """
        prefix = prefix.lower()[:2]
        return [edge for (f, t), edge in self.edges.items() if f == prefix]
    
    def get_incoming_edges(self, prefix: str) -> List[Dict]:
        """Get all edges ending at a node.
        
        Args:
            prefix: Node prefix.
            
        Returns:
            List of edge dictionaries.
        """
        prefix = prefix.lower()[:2]
        return [edge for (f, t), edge in self.edges.items() if t == prefix]
    
    def validate_path_segment(self, from_prefix: str, to_prefix: str, 
                             min_observations: int = 1,
                             check_bidirectional: bool = False) -> Tuple[bool, float]:
        """Validate a path segment using graph data.
        
        Args:
            from_prefix: Source node prefix.
            to_prefix: Destination node prefix.
            min_observations: Minimum observations required for confidence.
            check_bidirectional: If True, check if reverse edge exists and boost confidence.
            
        Returns:
            Tuple of (is_valid, confidence_score) where confidence is 0.0-1.0.
        """
        edge = self.get_edge(from_prefix, to_prefix)
        
        if not edge:
            return (False, 0.0)
        
        if edge['observation_count'] < min_observations:
            return (False, 0.0)
        
        # Confidence based on observation count and recency
        obs_count = edge['observation_count']
        last_seen = edge['last_seen']
        
        if isinstance(last_seen, str):
            last_seen = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
        
        hours_ago = (datetime.now() - last_seen).total_seconds() / 3600.0
        
        # Observation count confidence (logarithmic scale)
        obs_confidence = min(1.0, 0.3 + (0.7 * (1.0 - 1.0 / (1.0 + obs_count / 10.0))))
        
        # Recency confidence (exponential decay, 48 hour half-life for longer advert intervals)
        recency_confidence = 1.0 if hours_ago < 1 else max(0.0, 2.0 ** (-hours_ago / 48.0))
        
        # Combined confidence
        confidence = (obs_confidence * 0.6) + (recency_confidence * 0.4)
        
        # Bidirectional edge bonus
        if check_bidirectional:
            reverse_edge = self.get_edge(to_prefix, from_prefix)
            if reverse_edge and reverse_edge['observation_count'] >= min_observations:
                # Bidirectional connection is more reliable
                confidence = min(1.0, confidence + 0.15)
        
        return (True, confidence)
    
    def validate_path(self, path_nodes: List[str], min_observations: int = 1) -> Tuple[bool, float]:
        """Validate an entire path using graph data.
        
        Args:
            path_nodes: List of node prefixes in path order.
            min_observations: Minimum observations required per edge.
            
        Returns:
            Tuple of (is_valid, average_confidence).
        """
        if len(path_nodes) < 2:
            return (True, 1.0)  # Single node or empty path is always valid
        
        validations = []
        for i in range(len(path_nodes) - 1):
            from_node = path_nodes[i]
            to_node = path_nodes[i + 1]
            is_valid, confidence = self.validate_path_segment(from_node, to_node, min_observations)
            
            if not is_valid:
                return (False, 0.0)
            
            validations.append(confidence)
        
        # Return average confidence
        avg_confidence = sum(validations) / len(validations) if validations else 0.0
        return (True, avg_confidence)
    
    def get_candidate_score(self, candidate_prefix: str, prev_prefix: Optional[str],
                           next_prefix: Optional[str], min_observations: int = 1,
                           hop_position: Optional[int] = None,
                           use_bidirectional: bool = True,
                           use_hop_position: bool = True) -> float:
        """Get graph-based score for a candidate node in a path.
        
        Args:
            candidate_prefix: The candidate node prefix.
            prev_prefix: Previous node in path (if available).
            next_prefix: Next node in path (if available).
            min_observations: Minimum observations required.
            hop_position: Current position in path (0-based index) for hop position validation.
            use_bidirectional: If True, check bidirectional edges for higher confidence.
            use_hop_position: If True, validate against avg_hop_position if available.
            
        Returns:
            Score from 0.0 to 1.0 based on graph evidence.
        """
        score = 0.0
        evidence_count = 0
        scores = []
        
        # Check edge from previous node
        if prev_prefix:
            is_valid, confidence = self.validate_path_segment(
                prev_prefix, candidate_prefix, min_observations,
                check_bidirectional=use_bidirectional
            )
            if is_valid:
                scores.append(confidence)
                score += confidence
                evidence_count += 1
        
        # Check edge to next node
        if next_prefix:
            is_valid, confidence = self.validate_path_segment(
                candidate_prefix, next_prefix, min_observations,
                check_bidirectional=use_bidirectional
            )
            if is_valid:
                scores.append(confidence)
                score += confidence
                evidence_count += 1
        
        if evidence_count == 0:
            return 0.0
        
        # Calculate base score as average
        base_score = score / evidence_count
        
        # Hop position validation bonus
        if use_hop_position and hop_position is not None:
            # Check if candidate appears in expected position based on avg_hop_position
            # Check both incoming and outgoing edges for hop position data
            hop_position_match = False
            
            if prev_prefix:
                edge = self.get_edge(prev_prefix, candidate_prefix)
                if edge and edge.get('avg_hop_position') is not None:
                    # Allow some tolerance (within 0.5 of expected position)
                    expected_pos = edge['avg_hop_position']
                    if abs(hop_position - expected_pos) <= 0.5:
                        hop_position_match = True
            
            if not hop_position_match and next_prefix:
                edge = self.get_edge(candidate_prefix, next_prefix)
                if edge and edge.get('avg_hop_position') is not None:
                    # For outgoing edge, expected position is one less (since it's the from node)
                    expected_pos = edge['avg_hop_position'] - 1
                    if abs(hop_position - expected_pos) <= 0.5:
                        hop_position_match = True
            
            if hop_position_match:
                base_score = min(1.0, base_score + 0.1)
        
        # Geographic distance validation (if available)
        # Use stored geographic_distance from edges when available (more accurate)
        if prev_prefix or next_prefix:
            # Check if we have geographic distance data that suggests reasonable routing
            # This is informational - we don't heavily penalize based on distance alone
            # but can use it as a tie-breaker
            geographic_available = False
            if prev_prefix:
                edge = self.get_edge(prev_prefix, candidate_prefix)
                if edge and edge.get('geographic_distance') is not None:
                    geographic_available = True
            if not geographic_available and next_prefix:
                edge = self.get_edge(candidate_prefix, next_prefix)
                if edge and edge.get('geographic_distance') is not None:
                    geographic_available = True
            
            # Having geographic data increases confidence slightly (indicates well-tracked edge)
            if geographic_available:
                base_score = min(1.0, base_score + 0.05)
        
        return base_score
    
    def find_intermediate_nodes(self, from_prefix: str, to_prefix: str,
                              min_observations: int = 1,
                              max_hops: int = 2) -> List[Tuple[str, float]]:
        """Find intermediate nodes that connect from_prefix to to_prefix.
        
        Uses multi-hop path inference to find nodes that connect two prefixes
        when a direct edge may not exist or have low confidence.
        
        Args:
            from_prefix: Source node prefix.
            to_prefix: Destination node prefix.
            min_observations: Minimum observations required per edge.
            max_hops: Maximum number of hops to search (default: 2, fallback to 3).
            
        Returns:
            List of (candidate_prefix, score) tuples sorted by score (highest first).
            Score is 0.0-1.0 based on path strength.
        """
        from_prefix = from_prefix.lower()[:2]
        to_prefix = to_prefix.lower()[:2]
        
        candidates: Dict[str, float] = {}
        
        # Try 2-hop paths first: from_prefix -> intermediate -> to_prefix
        outgoing_edges = self.get_outgoing_edges(from_prefix)
        
        for edge in outgoing_edges:
            intermediate_prefix = edge['to_prefix']
            
            # Skip if this is the destination (direct edge case)
            if intermediate_prefix == to_prefix:
                continue
            
            # Check if intermediate connects to destination
            to_edge = self.get_edge(intermediate_prefix, to_prefix)
            if not to_edge or to_edge['observation_count'] < min_observations:
                continue
            
            # Validate both edges
            from_valid, from_confidence = self.validate_path_segment(
                from_prefix, intermediate_prefix, min_observations,
                check_bidirectional=True
            )
            to_valid, to_confidence = self.validate_path_segment(
                intermediate_prefix, to_prefix, min_observations,
                check_bidirectional=True
            )
            
            if from_valid and to_valid:
                # Score is minimum of both edges (weakest link)
                path_score = min(from_confidence, to_confidence)
                
                # Bidirectional path bonus
                reverse_from = self.get_edge(intermediate_prefix, from_prefix)
                reverse_to = self.get_edge(to_prefix, intermediate_prefix)
                bidirectional_bonus = 1.0
                if reverse_from and reverse_from['observation_count'] >= min_observations:
                    if reverse_to and reverse_to['observation_count'] >= min_observations:
                        # Both edges are bidirectional - strong evidence
                        bidirectional_bonus = 1.2
                    else:
                        bidirectional_bonus = 1.1
                elif reverse_to and reverse_to['observation_count'] >= min_observations:
                    bidirectional_bonus = 1.1
                
                path_score = min(1.0, path_score * bidirectional_bonus)
                
                # Use best score if we've seen this candidate before
                if intermediate_prefix not in candidates or path_score > candidates[intermediate_prefix]:
                    candidates[intermediate_prefix] = path_score
        
        # If no 2-hop paths found and max_hops >= 3, try 3-hop paths
        if not candidates and max_hops >= 3:
            # Find 3-hop paths: from_prefix -> intermediate1 -> intermediate2 -> to_prefix
            for edge1 in outgoing_edges:
                intermediate1 = edge1['to_prefix']
                if intermediate1 == to_prefix:
                    continue
                
                # Get edges from intermediate1
                intermediate1_edges = self.get_outgoing_edges(intermediate1)
                
                for edge2 in intermediate1_edges:
                    intermediate2 = edge2['to_prefix']
                    if intermediate2 == from_prefix or intermediate2 == intermediate1:
                        continue
                    
                    # Check if intermediate2 connects to destination
                    to_edge = self.get_edge(intermediate2, to_prefix)
                    if not to_edge or to_edge['observation_count'] < min_observations:
                        continue
                    
                    # Validate all three edges
                    valid1, conf1 = self.validate_path_segment(
                        from_prefix, intermediate1, min_observations
                    )
                    valid2, conf2 = self.validate_path_segment(
                        intermediate1, intermediate2, min_observations
                    )
                    valid3, conf3 = self.validate_path_segment(
                        intermediate2, to_prefix, min_observations
                    )
                    
                    if valid1 and valid2 and valid3:
                        # Score is minimum of all three edges
                        path_score = min(conf1, conf2, conf3)
                        
                        # 3-hop paths are less reliable, so reduce score
                        path_score *= 0.8
                        
                        # Use intermediate2 as candidate (the one before destination)
                        if intermediate2 not in candidates or path_score > candidates[intermediate2]:
                            candidates[intermediate2] = path_score
        
        # Sort by score (highest first) and return
        sorted_candidates = sorted(candidates.items(), key=lambda x: x[1], reverse=True)
        return sorted_candidates
    
    def shutdown(self):
        """Shutdown graph, flushing all pending writes."""
        self.logger.info("Shutting down mesh graph, flushing pending writes...")
        
        # Signal shutdown
        self._shutdown_event.set()
        
        # Flush pending updates
        try:
            self._flush_pending_updates_sync()
        except Exception as e:
            self.logger.warning(f"Error flushing graph updates on shutdown: {e}")
        
        # Log final statistics
        if self.edges:
            total_observations = sum(e['observation_count'] for e in self.edges.values())
            self.logger.info(f"Graph shutdown complete: {len(self.edges)} edges, {total_observations} total observations")
