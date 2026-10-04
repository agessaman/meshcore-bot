"""Database statistics, table info, cache view and optimization for the web viewer."""

from __future__ import annotations

import time
from typing import Any

from modules.security_utils import validate_sql_identifier
from modules.web_viewer.dashboard_stats import humanize_span


class DatabaseAdminMixin:
    """Mixed into BotDataViewer."""

    ALLOWED_TABLES: Any
    _bucket_hop_chunks: Any
    _clients_lock: Any
    _contact_has_multibyte_path_evidence: Any
    _db_connection: Any
    _get_cached_contact_multibyte_hop_chunks: Any
    connected_clients: Any
    db_path: Any
    logger: Any

    def _get_database_stats(self, top_users_window='all', top_commands_window='all',
                           top_paths_window='all', top_channels_window='all'):
        """Get comprehensive database statistics for dashboard"""
        try:
            with self._db_connection() as conn:
                cursor = conn.cursor()

                # Get all available tables
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cursor.fetchall()]

                # Filter tables by ALLOWED_TABLES whitelist for security
                tables = [t for t in tables if t in self.ALLOWED_TABLES]

                with self._clients_lock:
                    client_count = len(self.connected_clients)

                stats = {
                    'timestamp': time.time(),
                    'connected_clients': client_count,
                    'tables': tables
                }
                self._stats_contacts(cursor, tables, stats)
                self._stats_incoming_packets(cursor, tables, stats)
                self._stats_adverts(cursor, tables, stats)
                self._stats_repeaters(cursor, tables, stats)
                self._stats_caches(cursor, tables, stats)
                self._stats_messages(cursor, tables, stats, top_users_window, top_channels_window)
                self._stats_commands(cursor, tables, stats, top_commands_window)
                self._stats_paths(cursor, tables, stats, top_paths_window)
                self._stats_network_health(cursor, tables, stats)
                self._stats_geography(cursor, tables, stats)

                return stats

        except Exception as e:
            self.logger.error(f"Error getting database stats: {e}")
            return {'error': str(e)}

    def _stats_contacts(self, cursor, tables, stats):
        """Contact and tracking statistics."""
        if 'complete_contact_tracking' in tables:
            cursor.execute("SELECT COUNT(*) FROM complete_contact_tracking")
            stats['total_contacts'] = cursor.fetchone()[0]

            cursor.execute("""
                    SELECT COUNT(*) FROM complete_contact_tracking
                    WHERE last_heard > datetime('now', 'localtime', '-24 hours')
                """)
            stats['contacts_24h'] = cursor.fetchone()[0]

            cursor.execute("""
                    SELECT COUNT(*) FROM complete_contact_tracking
                    WHERE last_heard > datetime('now', 'localtime', '-7 days')
                """)
            stats['contacts_7d'] = cursor.fetchone()[0]

            # Contacts heard in 7d with multibyte path evidence. Scope observed_paths to 7d so
            # the pie chart matches "last 7 days" (lifetime paths + stale out_bytes_per_hop
            # otherwise inflated the percentage).
            stats['contacts_7d_multibyte_path'] = 0
            chunk_buckets = self._bucket_hop_chunks(set())
            mb_advert_pks: set[str] = set()
            if 'observed_paths' in tables:
                try:
                    chunk_buckets = self._bucket_hop_chunks(
                        self._get_cached_contact_multibyte_hop_chunks(
                            cursor, recent_days=7
                        )
                    )
                    # Use date() — julianday(iso8601) often returns NULL for Python isoformat() strings
                    cursor.execute(
                        """
                            SELECT DISTINCT public_key FROM observed_paths
                            WHERE packet_type = 'advert' AND public_key IS NOT NULL
                            AND bytes_per_hop IN (2, 3)
                            AND date(last_seen) >= date('now', 'localtime', '-7 days')
                            """
                    )
                    mb_advert_pks = {
                        row["public_key"] for row in cursor.fetchall() if row["public_key"]
                    }
                except Exception as e:
                    self.logger.debug(f"Could not load multibyte path sets for 7d stats: {e}")
            try:
                cursor.execute(
                    """
                        SELECT public_key, role, out_bytes_per_hop
                        FROM complete_contact_tracking
                        WHERE last_heard > datetime('now', 'localtime', '-7 days')
                        """
                )
                mb_7d = 0
                for row in cursor.fetchall():
                    if self._contact_has_multibyte_path_evidence(
                        row["public_key"],
                        row["role"],
                        row["out_bytes_per_hop"],
                        mb_advert_pks,
                        chunk_buckets,
                    ):
                        mb_7d += 1
                stats['contacts_7d_multibyte_path'] = mb_7d
            except Exception as e:
                self.logger.debug(f"Could not compute contacts_7d_multibyte_path: {e}")

            cursor.execute("""
                    SELECT COUNT(*) FROM complete_contact_tracking
                    WHERE is_currently_tracked = 1
                """)
            stats['tracked_contacts'] = cursor.fetchone()[0]

            cursor.execute("""
                    SELECT AVG(hop_count) FROM complete_contact_tracking
                    WHERE hop_count IS NOT NULL
                """)
            avg_hops = cursor.fetchone()[0]
            stats['avg_hop_count'] = round(avg_hops, 1) if avg_hops else 0

            cursor.execute("""
                    SELECT MAX(hop_count) FROM complete_contact_tracking
                    WHERE hop_count IS NOT NULL
                """)
            stats['max_hop_count'] = cursor.fetchone()[0] or 0

            cursor.execute("""
                    SELECT COUNT(DISTINCT role) FROM complete_contact_tracking
                    WHERE role IS NOT NULL
                """)
            stats['unique_roles'] = cursor.fetchone()[0]

            cursor.execute("""
                    SELECT COUNT(DISTINCT device_type) FROM complete_contact_tracking
                    WHERE device_type IS NOT NULL
                """)
            stats['unique_device_types'] = cursor.fetchone()[0]

    def _stats_incoming_packets(self, cursor, tables, stats):
        """Multi-byte path share of incoming packets over what packet_stream retains."""
        # Incoming packets: multibyte path share over whatever packet_stream
        # actually retains.  The key names still say 7d for compatibility,
        # but the window is reported honestly alongside them —
        # packet_stream is pruned at 3 days, so the old label was never true.
        stats['incoming_packets_7d'] = 0
        stats['incoming_packets_7d_multibyte_path'] = 0
        if 'packet_stream' in tables:
            try:
                cutoff_ts = time.time() - 7 * 86400
                cursor.execute(
                    """
                        SELECT COUNT(*),
                               SUM(CASE WHEN bytes_per_hop IN (2, 3) THEN 1 ELSE 0 END),
                               MIN(timestamp)
                        FROM packet_stream
                        WHERE type = ? AND timestamp > ? AND route_type_name IS NOT NULL
                        """,
                    ("packet", cutoff_ts),
                )
                row = cursor.fetchone()
                stats['incoming_packets_7d'] = row[0] or 0
                stats['incoming_packets_7d_multibyte_path'] = row[1] or 0
                stats['incoming_packets_from'] = row[2]
                stats['incoming_packets_window_label'] = humanize_span(
                    time.time() - row[2] if row[2] else None
                )
            except Exception as e:
                self.logger.debug(f"Could not compute incoming packet multibyte stats: {e}")

    def _stats_adverts(self, cursor, tables, stats):
        """Advertisement statistics from the daily tracking table."""
        if 'daily_stats' in tables:
            # Total advertisements (all time)
            cursor.execute("""
                    SELECT SUM(advert_count) FROM daily_stats
                """)
            total_adverts = cursor.fetchone()[0]
            stats['total_advertisements'] = total_adverts or 0

            # 24h advertisements
            cursor.execute("""
                    SELECT SUM(advert_count) FROM daily_stats
                    WHERE date = date('now', 'localtime')
                """)
            stats['advertisements_24h'] = cursor.fetchone()[0] or 0

            # 7d advertisements (last 7 days, excluding today)
            cursor.execute("""
                    SELECT SUM(advert_count) FROM daily_stats
                    WHERE date >= date('now', 'localtime', '-7 days') AND date < date('now', 'localtime')
                """)
            stats['advertisements_7d'] = cursor.fetchone()[0] or 0

            # Nodes per day statistics
            cursor.execute("""
                    SELECT COUNT(DISTINCT public_key) FROM daily_stats
                    WHERE date = date('now', 'localtime')
                """)
            stats['nodes_24h'] = cursor.fetchone()[0] or 0

            cursor.execute("""
                    SELECT COUNT(DISTINCT public_key) FROM daily_stats
                    WHERE date >= date('now', 'localtime', '-6 days')
                """)
            stats['nodes_7d'] = cursor.fetchone()[0] or 0

            cursor.execute("""
                    SELECT COUNT(DISTINCT public_key) FROM daily_stats
                """)
            stats['nodes_all'] = cursor.fetchone()[0] or 0
        else:
            # Fallback to old method if daily table doesn't exist yet
            if 'complete_contact_tracking' in tables:
                cursor.execute("""
                        SELECT SUM(advert_count) FROM complete_contact_tracking
                    """)
                total_adverts = cursor.fetchone()[0]
                stats['total_advertisements'] = total_adverts or 0

                cursor.execute("""
                        SELECT SUM(advert_count) FROM complete_contact_tracking
                        WHERE last_heard > datetime('now', 'localtime', '-24 hours')
                    """)
                stats['advertisements_24h'] = cursor.fetchone()[0] or 0

                cursor.execute("""
                        SELECT SUM(advert_count) FROM complete_contact_tracking
                        WHERE last_heard > datetime('now', 'localtime', '-7 days')
                    """)
                stats['advertisements_7d'] = cursor.fetchone()[0] or 0

    def _stats_repeaters(self, cursor, tables, stats):
        """Repeater contact counts."""
        if 'repeater_contacts' in tables:
            cursor.execute("SELECT COUNT(*) FROM repeater_contacts")
            stats['repeater_contacts'] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM repeater_contacts WHERE is_active = 1")
            stats['active_repeater_contacts'] = cursor.fetchone()[0]

    def _stats_caches(self, cursor, tables, stats):
        """Cache table entry counts."""
        cache_tables = [t for t in tables if 'cache' in t]
        stats['cache_tables'] = cache_tables
        stats['total_cache_entries'] = 0
        stats['active_cache_entries'] = 0

        for table in cache_tables:
            try:
                validate_sql_identifier(table)
            except ValueError:
                self.logger.warning(f"Rejecting invalid table name: {table!r}")
                raise
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            count = cursor.fetchone()[0]
            stats['total_cache_entries'] += count
            stats[f'{table}_count'] = count

            # Get active entries (not expired)
            cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE expires_at > datetime('now')")
            active_count = cursor.fetchone()[0]
            stats['active_cache_entries'] += active_count
            stats[f'{table}_active'] = active_count

    def _stats_messages(self, cursor, tables, stats, top_users_window, top_channels_window):
        """Message statistics, top users and top channels."""
        if 'message_stats' in tables:
            cursor.execute("SELECT COUNT(*) FROM message_stats")
            stats['total_messages'] = cursor.fetchone()[0]

            cursor.execute("""
                    SELECT COUNT(*) FROM message_stats
                    WHERE timestamp > strftime('%s', 'now', '-24 hours')
                """)
            stats['messages_24h'] = cursor.fetchone()[0]

            cursor.execute("""
                    SELECT COUNT(DISTINCT sender_id) FROM message_stats
                    WHERE timestamp > strftime('%s', 'now', '-24 hours')
                """)
            stats['unique_senders_24h'] = cursor.fetchone()[0]

            # Total unique users and channels
            cursor.execute("SELECT COUNT(DISTINCT sender_id) FROM message_stats")
            stats['unique_users_total'] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(DISTINCT channel) FROM message_stats WHERE channel IS NOT NULL")
            stats['unique_channels_total'] = cursor.fetchone()[0]

            # Top users (most frequent message senders) - filter by time window
            if top_users_window == '24h':
                time_filter = "WHERE timestamp > strftime('%s', 'now', '-24 hours')"
            elif top_users_window == '7d':
                time_filter = "WHERE timestamp > strftime('%s', 'now', '-7 days')"
            elif top_users_window == '30d':
                time_filter = "WHERE timestamp > strftime('%s', 'now', '-30 days')"
            else:  # 'all'
                time_filter = ""

            query = f"""
                    SELECT sender_id, COUNT(*) as count
                    FROM message_stats
                    {time_filter}
                    GROUP BY sender_id
                    ORDER BY count DESC
                    LIMIT 15
                """
            cursor.execute(query)
            stats['top_users'] = [{'user': row[0], 'count': row[1]} for row in cursor.fetchall()]

            # Top channels by message count - filter by time window
            if top_channels_window == '24h':
                time_filter = "AND timestamp > strftime('%s', 'now', '-24 hours')"
            elif top_channels_window == '7d':
                time_filter = "AND timestamp > strftime('%s', 'now', '-7 days')"
            elif top_channels_window == '30d':
                time_filter = "AND timestamp > strftime('%s', 'now', '-30 days')"
            else:  # 'all'
                time_filter = ""

            query = f"""
                    SELECT channel, COUNT(*) as message_count, COUNT(DISTINCT sender_id) as unique_users
                    FROM message_stats
                    WHERE channel IS NOT NULL {time_filter}
                    GROUP BY channel
                    ORDER BY message_count DESC
                    LIMIT 10
                """
            cursor.execute(query)
            stats['top_channels'] = [
                {'channel': row[0], 'messages': row[1], 'users': row[2]}
                for row in cursor.fetchall()
            ]

    def _stats_commands(self, cursor, tables, stats, top_commands_window):
        """Command statistics, reply rates and top commands."""
        if 'command_stats' in tables:
            cursor.execute("SELECT COUNT(*) FROM command_stats")
            stats['total_commands'] = cursor.fetchone()[0]

            cursor.execute("""
                    SELECT COUNT(*) FROM command_stats
                    WHERE timestamp > strftime('%s', 'now', '-24 hours')
                """)
            stats['commands_24h'] = cursor.fetchone()[0]

            # Top commands - filter by time window
            if top_commands_window == '24h':
                time_filter = "WHERE timestamp > strftime('%s', 'now', '-24 hours')"
            elif top_commands_window == '7d':
                time_filter = "WHERE timestamp > strftime('%s', 'now', '-7 days')"
            elif top_commands_window == '30d':
                time_filter = "WHERE timestamp > strftime('%s', 'now', '-30 days')"
            else:  # 'all'
                time_filter = ""

            query = f"""
                    SELECT command_name, COUNT(*) as count
                    FROM command_stats
                    {time_filter}
                    GROUP BY command_name
                    ORDER BY count DESC
                    LIMIT 15
                """
            cursor.execute(query)
            stats['top_commands'] = [{'command': row[0], 'count': row[1]} for row in cursor.fetchall()]

            # Bot reply rates (commands that got responses) - calculate for different time windows
            # 24 hour reply rate
            cursor.execute("""
                    SELECT COUNT(*) FROM command_stats
                    WHERE timestamp > strftime('%s', 'now', '-24 hours') AND response_sent = 1
                """)
            replied_24h = cursor.fetchone()[0]
            cursor.execute("""
                    SELECT COUNT(*) FROM command_stats
                    WHERE timestamp > strftime('%s', 'now', '-24 hours')
                """)
            total_24h = cursor.fetchone()[0]
            if total_24h > 0:
                stats['bot_reply_rate_24h'] = round((replied_24h / total_24h) * 100, 1)
            else:
                stats['bot_reply_rate_24h'] = 0

            # 7 day reply rate
            cursor.execute("""
                    SELECT COUNT(*) FROM command_stats
                    WHERE timestamp > strftime('%s', 'now', '-7 days') AND response_sent = 1
                """)
            replied_7d = cursor.fetchone()[0]
            cursor.execute("""
                    SELECT COUNT(*) FROM command_stats
                    WHERE timestamp > strftime('%s', 'now', '-7 days')
                """)
            total_7d = cursor.fetchone()[0]
            if total_7d > 0:
                stats['bot_reply_rate_7d'] = round((replied_7d / total_7d) * 100, 1)
            else:
                stats['bot_reply_rate_7d'] = 0

            # 30 day reply rate
            cursor.execute("""
                    SELECT COUNT(*) FROM command_stats
                    WHERE timestamp > strftime('%s', 'now', '-30 days') AND response_sent = 1
                """)
            replied_30d = cursor.fetchone()[0]
            cursor.execute("""
                    SELECT COUNT(*) FROM command_stats
                    WHERE timestamp > strftime('%s', 'now', '-30 days')
                """)
            total_30d = cursor.fetchone()[0]
            if total_30d > 0:
                stats['bot_reply_rate_30d'] = round((replied_30d / total_30d) * 100, 1)
            else:
                stats['bot_reply_rate_30d'] = 0

    def _stats_paths(self, cursor, tables, stats, top_paths_window):
        """Path statistics and top paths."""
        if 'path_stats' in tables:
            cursor.execute("""
                    SELECT sender_id, path_length, path_string, timestamp
                    FROM path_stats
                    ORDER BY path_length DESC
                    LIMIT 1
                """)
            longest_path = cursor.fetchone()
            if longest_path:
                stats['longest_path'] = {
                    'user': longest_path[0],
                    'path_length': longest_path[1],
                    'path_string': longest_path[2],
                    'timestamp': longest_path[3]
                }

            # Top paths (longest paths) - filter by time window
            if top_paths_window == '24h':
                time_filter = "WHERE timestamp > strftime('%s', 'now', '-24 hours')"
            elif top_paths_window == '7d':
                time_filter = "WHERE timestamp > strftime('%s', 'now', '-7 days')"
            elif top_paths_window == '30d':
                time_filter = "WHERE timestamp > strftime('%s', 'now', '-30 days')"
            else:  # 'all'
                time_filter = ""

            query = f"""
                    SELECT sender_id, path_length, path_string, timestamp
                    FROM path_stats
                    {time_filter}
                    ORDER BY path_length DESC
                    LIMIT 5
                """
            cursor.execute(query)
            stats['top_paths'] = [
                {
                    'user': row[0],
                    'path_length': row[1],
                    'path_string': row[2],
                    'timestamp': row[3]
                }
                for row in cursor.fetchall()
            ]

    def _stats_network_health(self, cursor, tables, stats):
        """Network health: average signal and SNR."""
        if 'complete_contact_tracking' in tables:
            cursor.execute("""
                    SELECT AVG(snr) FROM complete_contact_tracking
                    WHERE snr IS NOT NULL AND last_heard > datetime('now', 'localtime', '-24 hours')
                """)
            avg_snr = cursor.fetchone()[0]
            stats['avg_snr_24h'] = round(avg_snr, 1) if avg_snr else 0

            cursor.execute("""
                    SELECT AVG(signal_strength) FROM complete_contact_tracking
                    WHERE signal_strength IS NOT NULL AND last_heard > datetime('now', 'localtime', '-24 hours')
                """)
            avg_signal = cursor.fetchone()[0]
            stats['avg_signal_strength_24h'] = round(avg_signal, 1) if avg_signal else 0

    def _stats_geography(self, cursor, tables, stats):
        """Geographic distribution: currently tracked contacts heard in the last 30 days."""
        # Normalize country names to avoid duplicates (e.g., "United States" vs "United States of America")
        if 'complete_contact_tracking' in tables:
            cursor.execute("""
                    SELECT COUNT(DISTINCT
                        CASE
                            WHEN country IN ('United States', 'United States of America', 'US', 'USA')
                            THEN 'United States'
                            ELSE country
                        END
                    ) FROM complete_contact_tracking
                    WHERE country IS NOT NULL AND country != ''
                    AND last_heard > datetime('now', 'localtime', '-30 days')
                    AND is_currently_tracked = 1
                """)
            stats['countries'] = cursor.fetchone()[0]

            cursor.execute("""
                    SELECT COUNT(DISTINCT state) FROM complete_contact_tracking
                    WHERE state IS NOT NULL AND state != ''
                    AND last_heard > datetime('now', 'localtime', '-30 days')
                    AND is_currently_tracked = 1
                """)
            stats['states'] = cursor.fetchone()[0]

            cursor.execute("""
                    SELECT COUNT(DISTINCT city) FROM complete_contact_tracking
                    WHERE city IS NOT NULL AND city != ''
                    AND last_heard > datetime('now', 'localtime', '-30 days')
                    AND is_currently_tracked = 1
                """)
            stats['cities'] = cursor.fetchone()[0]

    def _get_database_info(self):
        """Get comprehensive database information for database page"""
        try:
            with self._db_connection() as conn:
                cursor = conn.cursor()

                # Get all tables
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                table_names = [row[0] for row in cursor.fetchall()]

                # Filter tables by ALLOWED_TABLES whitelist for security
                table_names = [
                    name for name in table_names
                    if name in self.ALLOWED_TABLES
                ]

                # Get table information
                tables = []
                total_records = 0

                for table_name in table_names:
                    try:
                        # Get record count
                        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
                        record_count = cursor.fetchone()[0]
                        total_records += record_count

                        # Get table size (approximate)
                        cursor.execute(f"PRAGMA table_info({table_name})")
                        columns = cursor.fetchall()

                        # Estimate size (rough calculation)
                        estimated_size = record_count * len(columns) * 50  # Rough estimate
                        size_str = f"{estimated_size:,} bytes" if estimated_size < 1024 else f"{estimated_size/1024:.1f} KB"

                        # Get table description based on name
                        description = self._get_table_description(table_name)

                        tables.append({
                            'name': table_name,
                            'record_count': record_count,
                            'size': size_str,
                            'description': description
                        })

                    except Exception as e:
                        self.logger.debug(f"Error getting info for table {table_name}: {e}")
                        tables.append({
                            'name': table_name,
                            'record_count': 0,
                            'size': 'Unknown',
                            'description': 'Error reading table'
                        })

                # Get database file size
                import os
                try:
                    db_size_bytes = os.path.getsize(self.db_path)
                    if db_size_bytes < 1024:
                        db_size = f"{db_size_bytes} bytes"
                    elif db_size_bytes < 1024 * 1024:
                        db_size = f"{db_size_bytes/1024:.1f} KB"
                    else:
                        db_size = f"{db_size_bytes/(1024*1024):.1f} MB"
                except:
                    db_size = "Unknown"

                return {
                    'total_tables': len(table_names),
                    'total_records': total_records,
                    'last_updated': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'db_size': db_size,
                    'tables': tables
                }

        except Exception as e:
            self.logger.error(f"Error getting database info: {e}")
            return {
                'total_tables': 0,
                'total_records': 0,
                'last_updated': 'Error',
                'db_size': 'Unknown',
                'tables': []
            }

    def _is_safe_table_name(self, table_name: str) -> bool:
        """Check if table name is in the ALLOWED_TABLES whitelist.

        Args:
            table_name: The table name to validate

        Returns:
            True if the table is in the allowed whitelist, False otherwise
        """
        if not table_name or not isinstance(table_name, str):
            return False
        return table_name in self.ALLOWED_TABLES

    def _get_table_description(self, table_name):
        """Get human-readable description for table"""
        descriptions = {
            'packet_stream': 'Real-time packet and command data stream',
            'complete_contact_tracking': 'Contact tracking and device information',
            'repeater_contacts': 'Repeater contact management',
            'message_stats': 'Message statistics and analytics',
            'command_stats': 'Command execution statistics',
            'path_stats': 'Network path statistics',
            'geocoding_cache': 'Geocoding service cache',
            'generic_cache': 'General purpose cache storage'
        }
        return descriptions.get(table_name, 'Database table')

    def _optimize_database(self):
        """Optimize database using VACUUM, ANALYZE, and REINDEX"""
        try:
            with self._db_connection() as conn:
                cursor = conn.cursor()

                # Get initial database size
                import os
                initial_size = os.path.getsize(self.db_path)

                # Perform VACUUM to reclaim unused space
                self.logger.info("Starting database VACUUM...")
                cursor.execute("VACUUM")
                vacuum_size = os.path.getsize(self.db_path)
                vacuum_saved = initial_size - vacuum_size

                # Perform ANALYZE to update table statistics
                self.logger.info("Starting database ANALYZE...")
                cursor.execute("ANALYZE")

                # Get all tables for REINDEX
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cursor.fetchall()]

                # Filter tables by ALLOWED_TABLES whitelist for security
                tables = [t for t in tables if t in self.ALLOWED_TABLES]

                # Perform REINDEX on all tables
                self.logger.info("Starting database REINDEX...")
                reindexed_tables = []
                for table in tables:
                    try:
                        cursor.execute(f"REINDEX {table}")
                        reindexed_tables.append(table)
                    except Exception as e:
                        self.logger.debug(f"Could not reindex table {table}: {e}")

                # Get final database size
                final_size = os.path.getsize(self.db_path)
                total_saved = initial_size - final_size

                # Format size information
                def format_size(size_bytes):
                    if size_bytes < 1024:
                        return f"{size_bytes} bytes"
                    elif size_bytes < 1024 * 1024:
                        return f"{size_bytes/1024:.1f} KB"
                    else:
                        return f"{size_bytes/(1024*1024):.1f} MB"

                return {
                    'success': True,
                    'vacuum_result': f"VACUUM completed - saved {format_size(vacuum_saved)}",
                    'analyze_result': f"ANALYZE completed - updated statistics for {len(tables)} tables",
                    'reindex_result': f"REINDEX completed - rebuilt indexes for {len(reindexed_tables)} tables",
                    'initial_size': format_size(initial_size),
                    'final_size': format_size(final_size),
                    'total_saved': format_size(total_saved),
                    'tables_processed': len(tables),
                    'tables_reindexed': len(reindexed_tables)
                }

        except Exception as e:
            self.logger.error(f"Error optimizing database: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _get_cache_data(self):
        """Get cache data"""
        try:
            with self._db_connection() as conn:
                cursor = conn.cursor()

                # Get cache statistics
                cursor.execute("SELECT COUNT(*) FROM adverts")
                total_adverts = cursor.fetchone()[0]

                cursor.execute("""
                SELECT COUNT(*) FROM adverts
                WHERE timestamp > datetime('now', '-1 hour')
            """)
                recent_adverts = cursor.fetchone()[0]

                cursor.execute("""
                SELECT COUNT(DISTINCT user_id) FROM adverts
                WHERE timestamp > datetime('now', '-24 hours')
            """)
                active_users = cursor.fetchone()[0]

                return {
                    'total_adverts': total_adverts,
                    'recent_adverts_1h': recent_adverts,
                    'active_users_24h': active_users,
                    'timestamp': time.time()
                }
        except Exception as e:
            self.logger.error(f"Error getting cache data: {e}")
            return {'error': str(e)}
