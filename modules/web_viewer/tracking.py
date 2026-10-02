"""Contact tracking data for the web viewer: the contacts table, contact detail and distances."""

from __future__ import annotations

from typing import Any


class ContactTrackingMixin:
    """Mixed into BotDataViewer."""

    _bucket_hop_chunks: Any
    _compute_path_encoding_badge: Any
    _db_connection: Any
    _get_cached_contact_multibyte_hop_chunks: Any
    config: Any
    logger: Any

    def _get_tracking_data(
        self,
        since='30d',
        include_detail=False,
        page: int | None = None,
        page_size: int | None = None,
        search: str = '',
        path_bytes: str = '',
        device_role: str = '',
        hop_filter: str = '',
        location_filter: str = '',
        starred: str = '',
        sort: str = 'last_seen',
        direction: str = 'desc',
    ):
        """Get contact tracking data. since: 24h, 7d, 30d, 90d, or all (heard in that window).

        include_detail=False (the interactive /api/contacts list) omits per-contact ``all_paths``
        and ``raw_advert_data`` to keep the payload small; the UI fetches those on demand via
        /api/contact-detail.  The interactive route supplies ``page`` and ``page_size`` so path
        enrichment is limited to visible contacts. include_detail=True (the export endpoint)
        keeps the legacy full-result behavior and full fields.
        """
        try:
            with self._db_connection() as conn:
                cursor = conn.cursor()

                # Get bot location from config
                bot_lat = self.config.getfloat('Bot', 'bot_latitude', fallback=None)
                bot_lon = self.config.getfloat('Bot', 'bot_longitude', fallback=None)

                where_clause, where_params, path_bytes_expression = self._tracking_where(
                    since, search, path_bytes, device_role, hop_filter, location_filter, starred, include_detail
                )

                pagination = None
                filtered_stats = None
                if page is not None and page_size is not None and not include_detail:
                    page_size = max(1, min(200, int(page_size)))
                    page = max(1, int(page))
                    cursor.execute(
                        """
                    SELECT
                        COUNT(*) AS total_items,
                        SUM(CASE WHEN c.last_heard >= datetime('now', 'localtime', '-24 hours') THEN 1 ELSE 0 END) AS contacts_24h,
                        SUM(CASE WHEN c.last_heard >= datetime('now', 'localtime', '-7 days') THEN 1 ELSE 0 END) AS contacts_7d,
                        SUM(CASE WHEN c.first_heard >= datetime('now', 'localtime', '-7 days')
                                  AND LOWER(COALESCE(c.device_type, '')) LIKE '%companion%' THEN 1 ELSE 0 END) AS new_companions,
                        SUM(CASE WHEN c.first_heard >= datetime('now', 'localtime', '-7 days')
                                  AND LOWER(COALESCE(c.device_type, '')) LIKE '%repeater%' THEN 1 ELSE 0 END) AS new_repeaters,
                        SUM(CASE WHEN c.first_heard >= datetime('now', 'localtime', '-7 days')
                                  AND (LOWER(COALESCE(c.device_type, '')) LIKE '%room%'
                                       OR LOWER(COALESCE(c.device_type, '')) LIKE '%server%') THEN 1 ELSE 0 END) AS new_room_servers
                    FROM complete_contact_tracking c
                    """ + where_clause,
                        tuple(where_params),
                    )
                    aggregate = cursor.fetchone()
                    total_items = int(aggregate['total_items'] or 0)
                    total_pages = max(1, (total_items + page_size - 1) // page_size)
                    page = min(page, total_pages)
                    pagination = {
                        'page': page,
                        'page_size': page_size,
                        'total_items': total_items,
                        'total_pages': total_pages,
                        'has_previous': page > 1,
                        'has_next': page < total_pages,
                    }
                    filtered_stats = {
                        'contacts_24h': int(aggregate['contacts_24h'] or 0),
                        'contacts_7d': int(aggregate['contacts_7d'] or 0),
                        'contacts_total': total_items,
                        'new_companions': int(aggregate['new_companions'] or 0),
                        'new_repeaters': int(aggregate['new_repeaters'] or 0),
                        'new_room_servers': int(aggregate['new_room_servers'] or 0),
                    }

                # Fetch contacts directly (no join/group-by). The recent paths per contact are
                # loaded in a second query below and assembled in Python. This avoids materializing
                # a window-function CTE over all of observed_paths and grouping by every contact
                # column (incl. the raw_advert_data blob) on every request. last_advert_timestamp is
                # the per-contact value, so it matches the old MAX(...) over a single contact's rows.
                detail_cols = "c.raw_advert_data," if include_detail else ""
                sort_expressions = {
                    'username': "LOWER(COALESCE(c.name, ''))",
                    'device_type': "LOWER(COALESCE(c.device_type, ''))",
                    'location': (
                        "LOWER(CASE "
                        "WHEN c.city IS NOT NULL AND c.city != '' AND c.state IS NOT NULL AND c.state != '' "
                        "THEN c.city || ', ' || c.state "
                        "WHEN c.city IS NOT NULL AND c.city != '' THEN c.city "
                        "WHEN c.latitude IS NOT NULL AND c.longitude IS NOT NULL "
                        "AND c.latitude != 0 AND c.longitude != 0 THEN printf('%s, %s', c.latitude, c.longitude) "
                        "ELSE '' END)"
                    ),
                    'snr': 'COALESCE(c.snr, 0)',
                    'hop_count': 'COALESCE(c.hop_count, 0)',
                    'path_bytes': path_bytes_expression,
                    'first_heard': "COALESCE(c.first_heard, '')",
                    'last_seen': "COALESCE(c.last_heard, '')",
                    'advert_count': 'COALESCE(c.advert_count, 0)',
                }
                sort = sort if sort in (*sort_expressions.keys(), 'distance') else 'last_seen'
                direction = 'asc' if direction == 'asc' else 'desc'
                if sort == 'distance':
                    if bot_lat is None or bot_lon is None:
                        sort_expression = '0'
                    else:
                        conn.create_function('contacts_distance_km', 2, lambda lat, lon: (
                            self._calculate_distance(bot_lat, bot_lon, lat, lon)
                            if lat is not None and lon is not None else 0
                        ))
                        sort_expression = 'contacts_distance_km(c.latitude, c.longitude)'
                else:
                    sort_expression = sort_expressions[sort]

                query_params = list(where_params)
                limit_clause = ''
                if pagination is not None:
                    limit_clause = ' LIMIT ? OFFSET ?'
                    query_params.extend([page_size, (page - 1) * page_size])

                cursor.execute("""
                SELECT
                    c.public_key, c.name, c.role, c.device_type,
                    c.latitude, c.longitude, c.city, c.state, c.country,
                    c.snr, c.hop_count, c.first_heard, c.last_heard,
                    c.advert_count, c.is_currently_tracked,
                    """ + detail_cols + """
                    c.signal_strength,
                    c.is_starred, c.out_path, c.out_path_len, c.out_bytes_per_hop,
                    """ + path_bytes_expression + """ AS path_bytes_per_hop,
                    c.last_advert_timestamp as last_message
                FROM complete_contact_tracking c
                """ + where_clause + """
                ORDER BY """ + sort_expression + f" {direction.upper()}, c.public_key ASC" + limit_clause,
                    tuple(query_params),
                )

                main_rows = cursor.fetchall()

                paths_by_key = {}
                path_rows = []
                if main_rows:
                    path_params: list[Any] = []
                    page_key_clause = ''
                    if pagination is not None:
                        # The interactive list enriches only the visible page.  At most 200 keys are
                        # supplied, staying comfortably below SQLite's parameter limit and turning
                        # the former all-history window scan into targeted index lookups.
                        page_keys = [row['public_key'] for row in main_rows]
                        placeholders = ','.join('?' for _ in page_keys)
                        page_key_clause = f' AND public_key IN ({placeholders})'
                        path_params.extend(page_keys)
                    cursor.execute("""
                    WITH recent_paths AS (
                        SELECT public_key, path_hex, path_length, bytes_per_hop,
                               observation_count, last_seen,
                               ROW_NUMBER() OVER (PARTITION BY public_key ORDER BY last_seen DESC) as rn
                        FROM observed_paths
                        WHERE packet_type = 'advert' AND public_key IS NOT NULL
                    """ + page_key_clause + """
                    )
                    SELECT public_key, path_hex, path_length, bytes_per_hop, observation_count, last_seen
                    FROM recent_paths WHERE rn <= 50
                    ORDER BY public_key, last_seen DESC
                """, tuple(path_params))
                    path_rows = cursor.fetchall()

                for prow in path_rows:
                    if not prow['path_hex']:  # Skip empty paths
                        continue
                    bph = None
                    if prow['bytes_per_hop'] is not None:
                        try:
                            bph = int(prow['bytes_per_hop'])
                            if bph not in (1, 2, 3):
                                bph = 1
                        except (TypeError, ValueError):
                            bph = 1
                    paths_by_key.setdefault(prow['public_key'], []).append({
                        'path_hex': prow['path_hex'],
                        'path_length': int(prow['path_length']) if prow['path_length'] is not None else 0,
                        'bytes_per_hop': bph,
                        'observation_count': int(prow['observation_count']) if prow['observation_count'] is not None else 1,
                        'last_seen': prow['last_seen'] if prow['last_seen'] is not None else None
                    })

                multibyte_hop_chunks = self._get_cached_contact_multibyte_hop_chunks(cursor)
                chunk_buckets = self._bucket_hop_chunks(multibyte_hop_chunks)

                tracking = []
                self._tracking_entries(main_rows, paths_by_key, chunk_buckets, bot_lat, bot_lon, include_detail, tracking)

                # Get server statistics for daily tracking using direct database queries
                server_stats = {}
                self._tracking_server_stats(cursor, server_stats)

                result = {
                    'tracking_data': tracking,
                    'server_stats': server_stats
                }
                if pagination is not None:
                    result['pagination'] = pagination
                    result['filtered_stats'] = filtered_stats
                return result
        except Exception as e:
            self.logger.error(f"Error getting tracking data: {e}")
            return {'error': str(e)}

    def _tracking_where(
        self, since, search, path_bytes, device_role, hop_filter, location_filter, starred, include_detail
    ):
        """WHERE clause and parameters for the contact filters, and the path-bytes SQL expression the sort reuses."""
        # Filter by last_heard (default: last 30 days). last_heard is stored as ISO-text
        # datetime in LOCAL time (e.g. '2026-06-16 09:03:49.606966', written by datetime.now()),
        # so the cutoff must also be local: datetime('now', 'localtime', ...). Using bare
        # datetime('now', ...) computes the cutoff in UTC and shaves the local UTC offset off
        # the window (e.g. a "24h" filter only returns ~17h of data in US/Pacific).
        datetime_offsets = {
            '24h': "'-24 hours'",
            '7d':  "'-7 days'",
            '30d': "'-30 days'",
            '90d': "'-90 days'",
        }
        where_parts = []
        where_params: list[Any] = []
        # A node can have more than one observed advert path.  Treat its byte class as
        # the widest path encoding seen for it, with the contact's current out-path as a
        # fallback for databases that have not retained an observed path yet.  This gives
        # the list one stable, sortable value instead of placing the same node in several
        # byte buckets.  Only count rows with a known 1/2/3 encoding so NULL/invalid
        # observations do not collapse to "1-byte" and block the out-path fallback.
        path_bytes_expression = """COALESCE((
                SELECT MAX(op.bytes_per_hop)
                FROM observed_paths op
                WHERE op.public_key = c.public_key
                  AND op.packet_type = 'advert'
                  AND op.path_hex IS NOT NULL AND op.path_hex != ''
                  AND op.bytes_per_hop IN (1, 2, 3)
            ), CASE WHEN c.out_bytes_per_hop IN (1, 2, 3)
                     THEN c.out_bytes_per_hop ELSE 0 END)"""
        if since in datetime_offsets:
            where_parts.append(
                f"c.last_heard >= datetime('now', 'localtime', {datetime_offsets[since]})"
            )

        search = (search or '').strip().lower()[:100]
        if search and not include_detail:
            # Match the former client-side behavior: public keys are prefix-only,
            # while names, roles, device types, and locations match anywhere.
            escaped = search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            where_parts.append(
                "("
                        "LOWER(COALESCE(c.public_key, '')) LIKE ? ESCAPE '\\' OR "
                        "LOWER(COALESCE(c.name, '')) LIKE ? ESCAPE '\\' OR "
                        "LOWER(COALESCE(c.role, '')) LIKE ? ESCAPE '\\' OR "
                        "LOWER(COALESCE(c.device_type, '')) LIKE ? ESCAPE '\\' OR "
                        "LOWER(COALESCE(c.city, '')) LIKE ? ESCAPE '\\' OR "
                        "LOWER(COALESCE(c.state, '')) LIKE ? ESCAPE '\\' OR "
                        "LOWER(COALESCE(c.country, '')) LIKE ? ESCAPE '\\'"
                        ")"
            )
            where_params.extend([f'{escaped}%'] + [f'%{escaped}%'] * 6)

        path_bytes = str(path_bytes or '').strip()
        if path_bytes in ('1', '2', '3'):
            where_parts.append(f'{path_bytes_expression} = ?')
            where_params.append(int(path_bytes))
        elif path_bytes == 'unknown':
            where_parts.append(f'{path_bytes_expression} = 0')

        device_role = str(device_role or '').strip().lower()
        if device_role in ('companion', 'repeater', 'roomserver', 'sensor'):
            where_parts.append("LOWER(COALESCE(c.role, '')) = ?")
            where_params.append(device_role)
        elif device_role == 'other':
            where_parts.append("LOWER(COALESCE(c.role, '')) NOT IN ('companion', 'repeater', 'roomserver', 'sensor')")

        if hop_filter in ('0', '1', '2', '3'):
            where_parts.append(
                'COALESCE(c.hop_count, 0) = ?' if hop_filter == '0'
                else 'COALESCE(c.hop_count, 0) >= ?'
            )
            where_params.append(int(hop_filter))

        has_location_expression = (
            "((c.city IS NOT NULL AND c.city != '') OR "
                    "(c.state IS NOT NULL AND c.state != '') OR "
                    "(c.country IS NOT NULL AND c.country != '') OR "
                    "(c.latitude IS NOT NULL AND c.longitude IS NOT NULL "
                    "AND c.latitude != 0 AND c.longitude != 0))"
        )
        if location_filter == 'known':
            where_parts.append(has_location_expression)
        elif location_filter == 'unknown':
            where_parts.append(f'NOT {has_location_expression}')

        if starred == 'yes':
            where_parts.append('COALESCE(c.is_starred, 0) = 1')
        elif starred == 'no':
            where_parts.append('COALESCE(c.is_starred, 0) = 0')

        where_clause = (' WHERE ' + ' AND '.join(where_parts)) if where_parts else ''
        return where_clause, where_params, path_bytes_expression

    def _tracking_entries(self, main_rows, paths_by_key, chunk_buckets, bot_lat, bot_lon, include_detail, tracking):
        """Append one response entry per contact row to *tracking*."""
        for row in main_rows:
            # Calculate distance if both bot and contact have coordinates
            distance = None
            if (bot_lat is not None and bot_lon is not None and
                row['latitude'] is not None and row['longitude'] is not None):
                distance = self._calculate_distance(bot_lat, bot_lon, row['latitude'], row['longitude'])

            # Recent paths for this contact (grouped from the second query above). The full
            # path objects are NOT sent in the list payload (they were ~70% of its size and
            # are only used in the per-contact modal); the UI fetches them on demand via
            # /api/contact-detail. The list only needs the count and the badge.
            all_paths = paths_by_key.get(row['public_key'], [])
            paths_count = len(all_paths)

            # Preserve the legacy total_messages value: it was COUNT(*) over the LEFT-JOINed
            # path rows, i.e. the number of paths, or 1 when a contact had no paths.
            total_messages = max(1, paths_count)

            path_encoding_badge = self._compute_path_encoding_badge(
                row, all_paths, chunk_buckets
            )

            # The badge/tooltip decodes out_path (the "primary" path) using out_bytes_per_hop.
            # The contact column can be stale (e.g. left at 1 while the primary path is a 3-byte
            # path), which makes a multi-byte path render as twice/three-times as many 1-byte
            # hops. Index the encoding on the primary observed path itself, which carries the
            # authoritative bytes_per_hop, falling back to the contact column when unmatched.
            out_path_val = row['out_path'] if row['out_path'] is not None else ''
            out_bytes_per_hop_val = row['out_bytes_per_hop'] if row['out_bytes_per_hop'] is not None else None
            if out_path_val:
                primary_path = next((p for p in all_paths if p['path_hex'] == out_path_val), None)
                if primary_path and primary_path.get('bytes_per_hop') in (1, 2, 3):
                    out_bytes_per_hop_val = primary_path['bytes_per_hop']

            entry = {
                'user_id': row['public_key'],
                'username': row['name'],
                'role': row['role'],
                'device_type': row['device_type'],
                'latitude': row['latitude'],
                'longitude': row['longitude'],
                'city': row['city'],
                'state': row['state'],
                'country': row['country'],
                'snr': row['snr'],
                'hop_count': row['hop_count'],
                'first_heard': row['first_heard'],
                'last_seen': row['last_heard'],
                'advert_count': row['advert_count'],
                'is_currently_tracked': row['is_currently_tracked'],
                'signal_strength': row['signal_strength'],
                'total_messages': total_messages,
                'last_message': row['last_message'],
                'distance': distance,
                'is_starred': bool(row['is_starred'] if row['is_starred'] is not None else 0),
                'out_path': out_path_val,
                'out_path_len': row['out_path_len'] if row['out_path_len'] is not None else -1,
                'out_bytes_per_hop': out_bytes_per_hop_val,
                'path_bytes_per_hop': int(row['path_bytes_per_hop'] or 0),
                'paths_count': paths_count,
                'path_encoding_badge': path_encoding_badge,
            }
            if include_detail:
                # Full fidelity for the export endpoint (size-tolerant, infrequent download).
                raw_advert_data = row['raw_advert_data']
                raw_advert_data_parsed = None
                if raw_advert_data:
                    try:
                        import json
                        raw_advert_data_parsed = json.loads(raw_advert_data)
                    except Exception:
                        raw_advert_data_parsed = None
                entry['all_paths'] = all_paths
                entry['raw_advert_data'] = raw_advert_data
                entry['raw_advert_data_parsed'] = raw_advert_data_parsed
            tracking.append(entry)

    def _tracking_server_stats(self, cursor, server_stats):
        """Fill *server_stats* with advert counts, node counts and the daily per-role series."""
        try:
            # Check if daily_stats table exists
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_stats'")
            if cursor.fetchone():
                # 24h: Last 24 hours of advertisements
                cursor.execute("""
                        SELECT SUM(advert_count) FROM daily_stats
                        WHERE date >= date('now', 'localtime', '-1 day')
                    """)
                server_stats['advertisements_24h'] = cursor.fetchone()[0] or 0

                # 7d: Previous 6 days (excluding today)
                cursor.execute("""
                        SELECT SUM(advert_count) FROM daily_stats
                        WHERE date >= date('now', 'localtime', '-7 days') AND date < date('now', 'localtime')
                    """)
                server_stats['advertisements_7d'] = cursor.fetchone()[0] or 0

                # All: Everything
                cursor.execute("""
                        SELECT SUM(advert_count) FROM daily_stats
                    """)
                server_stats['total_advertisements'] = cursor.fetchone()[0] or 0

                # Nodes per day statistics
                # Calculate today's unique nodes from complete_contact_tracking
                # (last_heard in last 24 hours) since daily_stats might not have today's data yet
                cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM complete_contact_tracking
                        WHERE last_heard >= datetime('now', 'localtime', '-24 hours')
                    """)
                server_stats['nodes_24h'] = cursor.fetchone()[0] or 0

                # Get today's unique nodes by role for the stacked chart
                cursor.execute("""
                        SELECT role, COUNT(DISTINCT public_key) as count
                        FROM complete_contact_tracking
                        WHERE last_heard >= datetime('now', 'localtime', '-24 hours')
                        AND role IS NOT NULL AND role != ''
                        GROUP BY role
                    """)
                today_by_role = {}
                for row in cursor.fetchall():
                    role = row[0].lower() if row[0] else 'unknown'
                    count = row[1]
                    today_by_role[role] = count

                server_stats['nodes_24h_by_role'] = {
                    'companion': today_by_role.get('companion', 0),
                    'repeater': today_by_role.get('repeater', 0),
                    'roomserver': today_by_role.get('roomserver', 0),
                    'sensor': today_by_role.get('sensor', 0),
                    'other': sum(v for k, v in today_by_role.items() if k not in ['companion', 'repeater', 'roomserver', 'sensor'])
                }

                cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats
                        WHERE date >= date('now', 'localtime', '-7 days') AND date < date('now', 'localtime')
                    """)
                server_stats['nodes_7d'] = cursor.fetchone()[0] or 0

                # Calculate day-over-day and period-over-period comparisons
                # Today vs 7 days ago (single day comparison)
                cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats
                        WHERE date = date('now', 'localtime', '-7 days')
                    """)
                result = cursor.fetchone()
                server_stats['nodes_7d_ago'] = result[0] if result and result[0] else 0

                # Last 7 days vs previous 7 days (days 8-14 ago)
                cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats
                        WHERE date >= date('now', 'localtime', '-14 days') AND date < date('now', 'localtime', '-7 days')
                    """)
                result = cursor.fetchone()
                server_stats['nodes_prev_7d'] = result[0] if result and result[0] else 0

                # Last 30 days vs previous 30 days (days 31-60 ago)
                cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats
                        WHERE date >= date('now', 'localtime', '-60 days') AND date < date('now', 'localtime', '-30 days')
                    """)
                result = cursor.fetchone()
                server_stats['nodes_prev_30d'] = result[0] if result and result[0] else 0

                # Also get current period totals for comparison
                cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats
                        WHERE date >= date('now', 'localtime', '-7 days')
                    """)
                server_stats['nodes_7d'] = cursor.fetchone()[0] or 0

                cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats
                        WHERE date >= date('now', 'localtime', '-30 days')
                    """)
                server_stats['nodes_30d'] = cursor.fetchone()[0] or 0

                cursor.execute("""
                        SELECT COUNT(DISTINCT public_key) FROM daily_stats
                    """)
                server_stats['nodes_all'] = cursor.fetchone()[0] or 0

                # Get daily unique node counts by role for the last 30 days for the stacked graph
                # Join daily_stats with complete_contact_tracking to get role information
                # This gives us accurate historical daily counts by role
                cursor.execute("""
                        SELECT ds.date, c.role, COUNT(DISTINCT ds.public_key) as daily_count
                        FROM daily_stats ds
                        LEFT JOIN complete_contact_tracking c ON ds.public_key = c.public_key
                        WHERE ds.date >= date('now', 'localtime', '-30 days') AND ds.date <= date('now', 'localtime')
                        AND (c.role IS NOT NULL AND c.role != '')
                        GROUP BY ds.date, c.role
                        ORDER BY ds.date ASC, c.role ASC
                    """)
                daily_data_by_role = cursor.fetchall()

                # Organize data by date and role
                daily_by_role = {}
                for row in daily_data_by_role:
                    date_str = row[0]
                    role = (row[1] or 'unknown').lower()
                    count = row[2]

                    if date_str not in daily_by_role:
                        daily_by_role[date_str] = {}
                    daily_by_role[date_str][role] = count

                # Convert to array format with all roles for each date
                server_stats['daily_nodes_30d_by_role'] = []
                for date_str in sorted(daily_by_role.keys()):
                    roles_data = daily_by_role[date_str]
                    server_stats['daily_nodes_30d_by_role'].append({
                        'date': date_str,
                        'companion': roles_data.get('companion', 0),
                        'repeater': roles_data.get('repeater', 0),
                        'roomserver': roles_data.get('roomserver', 0),
                        'sensor': roles_data.get('sensor', 0),
                        'other': sum(v for k, v in roles_data.items() if k not in ['companion', 'repeater', 'roomserver', 'sensor'])
                    })

                # Also keep the total count for backward compatibility
                cursor.execute("""
                        SELECT date, COUNT(DISTINCT public_key) as daily_count
                        FROM daily_stats
                        WHERE date >= date('now', 'localtime', '-30 days') AND date <= date('now', 'localtime')
                        GROUP BY date
                        ORDER BY date ASC
                    """)
                daily_data = cursor.fetchall()
                server_stats['daily_nodes_30d'] = [
                    {'date': row[0], 'count': row[1]}
                    for row in daily_data
                ]

        except Exception as e:
            self.logger.debug(f"Could not get server stats: {e}")

    def _get_contact_detail(self, public_key: str) -> dict:
        """Per-contact detail loaded on demand by the contacts UI modals.

        Returns the recent advert paths (same shape as the old list ``all_paths``) and the raw
        advertisement data. Both are excluded from the /api/contacts list payload — they were
        ~85% of its size and are only needed when a single contact is opened.
        """
        try:
            with self._db_connection() as conn:
                cursor = conn.cursor()

                # Recent advert paths (most-recent 50). The idx_observed_paths_advert_pk_seen covering
                # index serves WHERE public_key=? AND packet_type='advert' ORDER BY last_seen DESC directly.
                all_paths: list[dict[str, Any]] = []
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='observed_paths'")
                if cursor.fetchone():
                    cursor.execute("""
                    SELECT path_hex, path_length, bytes_per_hop, observation_count, last_seen
                    FROM observed_paths
                    WHERE packet_type = 'advert' AND public_key = ?
                    ORDER BY last_seen DESC
                    LIMIT 50
                """, (public_key,))
                    for prow in cursor.fetchall():
                        if not prow['path_hex']:
                            continue
                        bph = None
                        if prow['bytes_per_hop'] is not None:
                            try:
                                bph = int(prow['bytes_per_hop'])
                                if bph not in (1, 2, 3):
                                    bph = 1
                            except (TypeError, ValueError):
                                bph = 1
                        all_paths.append({
                            'path_hex': prow['path_hex'],
                            'path_length': int(prow['path_length']) if prow['path_length'] is not None else 0,
                            'bytes_per_hop': bph,
                            'observation_count': int(prow['observation_count']) if prow['observation_count'] is not None else 1,
                            'last_seen': prow['last_seen'] if prow['last_seen'] is not None else None,
                        })

                # Raw advertisement data for the "Advertisement Data" modal.
                raw_advert_data = None
                raw_advert_data_parsed = None
                cursor.execute("""
                SELECT raw_advert_data FROM complete_contact_tracking
                WHERE public_key = ? ORDER BY last_heard DESC LIMIT 1
            """, (public_key,))
                rad_row = cursor.fetchone()
                if rad_row and rad_row['raw_advert_data']:
                    raw_advert_data = rad_row['raw_advert_data']
                    try:
                        import json
                        raw_advert_data_parsed = json.loads(raw_advert_data)
                    except Exception:
                        raw_advert_data_parsed = None

                return {
                    'all_paths': all_paths,
                    'raw_advert_data': raw_advert_data,
                    'raw_advert_data_parsed': raw_advert_data_parsed,
                }
        except Exception as e:
            self.logger.error(f"Error getting contact detail: {e}")
            return {'error': str(e)}

    def _calculate_distance(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two points using Haversine formula"""
        import math

        # Convert latitude and longitude from degrees to radians
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

        # Haversine formula
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        c = 2 * math.asin(math.sqrt(a))

        # Radius of earth in kilometers
        r = 6371

        return c * r
