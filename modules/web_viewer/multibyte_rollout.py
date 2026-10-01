"""Multi-byte path rollout statistics for the web viewer: hop chunks, path encoding badges and relay metrics."""

from __future__ import annotations

import threading
from typing import Any


class MultibyteRolloutMixin:
    """Mixed into BotDataViewer."""

    _contacts_badge_cache: Any
    _contacts_badge_cache_lock: Any
    _db_connection: Any
    logger: Any

    @staticmethod
    def _chunks_from_multibyte_path_hex(path_hex: str, bytes_per_hop: int) -> list[str]:
        """Split path hex into per-hop segments for 2- or 3-byte hop encoding."""
        if not path_hex or bytes_per_hop not in (2, 3):
            return []
        step = bytes_per_hop * 2
        out: list[str] = []
        for i in range(0, len(path_hex), step):
            seg = path_hex[i : i + step]
            if len(seg) == step:
                out.append(seg.lower())
        return out

    def _collect_multibyte_hop_chunks(
        self, cursor, recent_days: int | None = None
    ) -> set[str]:
        """Hop prefixes from multibyte paths in observed_paths (for repeater/room pubkey matching).

        If ``recent_days`` is set (e.g. 7), only paths whose ``last_seen`` falls within that
        window are used. Default (None) keeps full history — used by the contacts API badge.
        Dashboard 7d stats pass ``recent_days=7`` so percentages match the chart title.
        """
        chunks: set[str] = set()
        try:
            extra = ""
            if recent_days is not None:
                d = max(1, min(int(recent_days), 366))
                extra = f" AND date(last_seen) >= date('now', 'localtime', '-{d} days')"
            # DISTINCT collapses the many duplicate (path_hex, bytes_per_hop) advert rows in SQL,
            # so Python only splits each distinct path once instead of every observation.
            cursor.execute(
                f"""
                SELECT DISTINCT path_hex, bytes_per_hop FROM observed_paths
                WHERE bytes_per_hop >= 2 AND bytes_per_hop <= 3
                AND path_hex IS NOT NULL AND length(path_hex) > 0
                {extra}
                """
            )
            for row in cursor.fetchall():
                ph = row["path_hex"]
                bph = row["bytes_per_hop"]
                try:
                    bph_i = int(bph) if bph is not None else 0
                except (TypeError, ValueError):
                    bph_i = 0
                for c in self._chunks_from_multibyte_path_hex(ph, bph_i):
                    if len(c) in (4, 6):
                        chunks.add(c)
        except Exception as e:
            self.logger.debug(f"Could not load multibyte hop chunks: {e}")
        return chunks

    def _get_cached_contact_multibyte_hop_chunks(
        self, cursor, recent_days: int | None = None
    ) -> set[str]:
        """Return contact badge evidence without rebuilding it per page request.

        Memoized per ``recent_days`` window: the contacts list wants all-time
        evidence (None) while the dashboard wants the 7-day set, and both are
        rebuilt often enough that sharing one slot would thrash the cache.
        """
        try:
            # last_seen changes when an existing path is observed again; count changes on
            # inserts and retention deletes.  Together they cheaply invalidate the cache while
            # using the multibyte covering index rather than the wide observed_paths table.
            cursor.execute(
                """
                SELECT MAX(last_seen), COUNT(*) FROM observed_paths
                WHERE bytes_per_hop >= 2 AND bytes_per_hop <= 3
                """
            )
            signature_row = cursor.fetchone()
            signature = tuple(signature_row) if signature_row else (None, 0)
            if recent_days is not None:
                # A windowed set also changes when the window itself slides, which
                # leaves the row fingerprint untouched — pin it to the local date.
                cursor.execute("SELECT date('now', 'localtime')")
                signature = (*signature, cursor.fetchone()[0])
        except Exception as e:
            self.logger.debug(f"Could not fingerprint multibyte hop chunks: {e}")
            return self._collect_multibyte_hop_chunks(cursor, recent_days=recent_days)

        lock = getattr(self, '_contacts_badge_cache_lock', None)
        if lock is None:
            lock = self._contacts_badge_cache_lock = threading.Lock()
        with lock:
            cache = getattr(self, '_contacts_badge_cache', None)
            if cache is None:
                cache = self._contacts_badge_cache = {}
            cached = cache.get(recent_days)
            if cached is not None and cached[0] == signature:
                return cached[1]
            chunks = self._collect_multibyte_hop_chunks(cursor, recent_days=recent_days)
            cache[recent_days] = (signature, chunks)
            return chunks

    @staticmethod
    def _bucket_hop_chunks(multibyte_hop_chunks: set[str]) -> dict[int, set[str]]:
        """Bucket hop-prefix chunks by length (4 or 6) for O(1) prefix matching.

        Build this once per request and reuse across the per-contact badge loop instead of
        scanning the whole chunk set for every contact.
        """
        return {
            4: {c for c in multibyte_hop_chunks if len(c) == 4},
            6: {c for c in multibyte_hop_chunks if len(c) == 6},
        }

    def _compute_path_encoding_badge(
        self,
        row: Any,
        all_paths: list[dict[str, Any]],
        chunk_buckets: dict[int, set[str]],
    ) -> str | None:
        """Return 'multibyte', 'one_byte', or None for contacts path-encoding badge.

        ``chunk_buckets`` is the length-bucketed form of the multibyte hop chunks
        (see _bucket_hop_chunks).
        """
        pk = row["public_key"] or ""
        role = (row["role"] or "").lower()
        obph_raw = row["out_bytes_per_hop"]
        obph: int | None
        try:
            obph = int(obph_raw) if obph_raw is not None else None
        except (TypeError, ValueError):
            obph = None
        if obph is not None and obph not in (1, 2, 3):
            obph = None

        out_path_len = row["out_path_len"]
        if out_path_len is None:
            out_path_len = -1
        try:
            out_path_len = int(out_path_len)
        except (TypeError, ValueError):
            out_path_len = -1

        advert_count = row["advert_count"] or 0

        def norm_bph(b: Any) -> int:
            if b is None:
                return 1
            try:
                i = int(b)
                return i if i in (1, 2, 3) else 1
            except (TypeError, ValueError):
                return 1

        # Multibyte evidence
        if obph in (2, 3):
            return "multibyte"
        for p in all_paths:
            if norm_bph(p.get("bytes_per_hop")) in (2, 3):
                return "multibyte"
        if role in ("repeater", "roomserver") and pk:
            pk_low = pk.lower()
            # Chunks are fixed-length (4 or 6), so startswith reduces to prefix-equality lookups.
            if pk_low[:4] in chunk_buckets[4] or pk_low[:6] in chunk_buckets[6]:
                return "multibyte"

        # One-byte: positive signal and no multibyte observation
        has_signal = bool(
            advert_count > 0 or len(all_paths) > 0 or out_path_len >= 0
        )
        if not has_signal:
            return None

        if obph is not None and obph != 1:
            return None
        for p in all_paths:
            if norm_bph(p.get("bytes_per_hop")) != 1:
                return None

        return "one_byte"

    def _contact_has_multibyte_path_evidence(
        self,
        public_key: str,
        role: str | None,
        out_bytes_per_hop: Any,
        multibyte_advert_public_keys: set[str],
        chunk_buckets: dict[int, set[str]],
    ) -> bool:
        """Multibyte detection for dashboard 7d stats (observed_paths scoped by date in SQL).

        ``chunk_buckets`` is the length-bucketed form of the multibyte hop chunks
        (see _bucket_hop_chunks) — build it once per request, not per contact.
        """
        pk = public_key or ""
        role_l = (role or "").lower()
        obph: int | None
        try:
            obph = int(out_bytes_per_hop) if out_bytes_per_hop is not None else None
        except (TypeError, ValueError):
            obph = None
        if obph is not None and obph not in (1, 2, 3):
            obph = None

        if obph in (2, 3):
            return True
        if pk and pk in multibyte_advert_public_keys:
            return True
        if role_l in ("repeater", "roomserver") and pk:
            # Chunks are fixed-length (4 or 6), so startswith reduces to prefix-equality
            # lookups — O(1) per contact instead of a scan of the whole chunk set.
            pk_low = pk.lower()
            if pk_low[:4] in chunk_buckets[4] or pk_low[:6] in chunk_buckets[6]:
                return True
        return False

    def _compute_single_byte_relay_metrics(
        self, cursor, path_time_cond: str
    ) -> dict[str, dict]:
        """Per-1-byte-prefix relay metrics derived from single-byte advert paths.

        Senders who also appear in multibyte advert paths in the same window are excluded —
        their single-byte paths are parallel/legacy routes that do not need this relay upgraded.

        Returns: {hex_prefix_2chars: {relay_score, unique_sources, unique_dests}}
        """
        try:
            cursor.execute(
                f"""
                SELECT DISTINCT public_key FROM observed_paths
                WHERE bytes_per_hop IN (2,3) AND packet_type = 'advert'
                AND public_key IS NOT NULL {path_time_cond}
                """
            )
            mb_senders: set[str] = {r['public_key'] for r in cursor.fetchall()}

            cursor.execute(
                f"""
                SELECT path_hex, public_key, to_prefix, observation_count
                FROM observed_paths
                WHERE bytes_per_hop = 1 AND packet_type = 'advert'
                AND public_key IS NOT NULL {path_time_cond}
                """
            )

            metrics: dict[str, dict] = {}
            for row in cursor.fetchall():
                if row['public_key'] in mb_senders:
                    continue
                ph = (row['path_hex'] or '').lower()
                if not ph:
                    continue
                obs = row['observation_count'] or 1
                for i in range(0, len(ph) - 1, 2):
                    chunk = ph[i:i + 2]
                    if len(chunk) != 2:
                        continue
                    if chunk not in metrics:
                        metrics[chunk] = {'relay_score': 0, 'sources': set(), 'to_prefixes': set()}
                    metrics[chunk]['relay_score'] += obs
                    metrics[chunk]['sources'].add(row['public_key'])
                    if row['to_prefix']:
                        metrics[chunk]['to_prefixes'].add(row['to_prefix'])

            return {
                pfx: {
                    'relay_score':    v['relay_score'],
                    'unique_sources': len(v['sources']),
                    'unique_dests':   len(v['to_prefixes']),
                }
                for pfx, v in metrics.items()
            }
        except Exception as e:
            self.logger.debug(f"Could not compute single-byte relay metrics: {e}")
            return {}

    def _compute_legacy_degree(
        self, cursor, unupgraded_pks: set[str], mc_since_cond: str
    ) -> dict[str, int]:
        """Count mesh_connections edges where both endpoints are unupgraded (single_byte) nodes.

        Uses from_public_key / to_public_key for precise matching — no 1-byte collision ambiguity.
        Returns: {public_key: edge_count}
        """
        try:
            cursor.execute(
                f"""
                SELECT from_public_key, to_public_key
                FROM mesh_connections
                WHERE from_public_key IS NOT NULL AND to_public_key IS NOT NULL
                {mc_since_cond}
                """
            )
            degree: dict[str, int] = {}
            for row in cursor.fetchall():
                fk, tk = row['from_public_key'], row['to_public_key']
                if fk in unupgraded_pks and tk in unupgraded_pks:
                    degree[fk] = degree.get(fk, 0) + 1
                    degree[tk] = degree.get(tk, 0) + 1
            return degree
        except Exception as e:
            self.logger.debug(f"Could not compute legacy degree: {e}")
            return {}

    def _get_multibyte_rollout_data(self, since: str = '30d', node_type: str = 'all') -> dict:
        """Return multibyte hash rollout analytics for repeaters and/or room servers.

        since: 24h | 7d | 30d | 90d | all — window applied to last_heard in complete_contact_tracking.
        node_type: all | repeater | roomserver — filters which relay node types are included.
        The daily_trend array always covers the last 30 days regardless of ``since``.
        """
        try:
            with self._db_connection() as conn:
                cursor = conn.cursor()

                # Role filter based on node_type
                if node_type == 'repeater':
                    role_filter = "c.role = 'repeater'"
                elif node_type == 'roomserver':
                    role_filter = "c.role = 'roomserver'"
                else:
                    role_filter = "c.role IN ('repeater', 'roomserver')"

                # last_heard is stored as a Python datetime serialised to ISO text by sqlite3
                # (e.g. '2026-05-17 10:30:00.123456').  Use SQLite's datetime() so the comparison
                # is ISO-text vs ISO-text, which sorts correctly lexicographically.
                datetime_offsets = {
                    '24h': "'-24 hours'",
                    '7d':  "'-7 days'",
                    '30d': "'-30 days'",
                    '90d': "'-90 days'",
                }
                if since in datetime_offsets:
                    where_clause = (
                        f"WHERE {role_filter}"
                        f" AND c.last_heard >= datetime('now', 'localtime', {datetime_offsets[since]})"
                    )
                else:
                    where_clause = f"WHERE {role_filter}"

                # Check for observed_paths table
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='observed_paths'"
                )
                has_observed_paths = cursor.fetchone() is not None

                # Collect multibyte hop chunks for prefix matching, scoped to the same window as the
                # contact filter so the rollout numbers are consistent with the dashboard's 7d stats.
                _since_to_days = {'24h': 1, '7d': 7, '30d': 30, '90d': 90}
                _chunk_recent_days = _since_to_days.get(since)  # None → all-time for 'all'
                multibyte_hop_chunks: set[str] = set()
                if has_observed_paths:
                    multibyte_hop_chunks = self._collect_multibyte_hop_chunks(
                        cursor, recent_days=_chunk_recent_days
                    )
                chunk_buckets = self._bucket_hop_chunks(multibyte_hop_chunks)

                # Per-repeater path traffic totals (advert paths only)
                path_traffic: dict[str, dict] = {}
                if has_observed_paths:
                    cursor.execute(
                        """
                    SELECT public_key,
                        SUM(observation_count) as total_traffic,
                        SUM(CASE WHEN bytes_per_hop IN (2,3) THEN observation_count ELSE 0 END) as mb_traffic,
                        MAX(last_seen) as latest_path_seen
                    FROM observed_paths
                    WHERE public_key IS NOT NULL AND packet_type = 'advert'
                    GROUP BY public_key
                    """
                    )
                    for pt_row in cursor.fetchall():
                        path_traffic[pt_row['public_key']] = {
                            'total_traffic': pt_row['total_traffic'] or 0,
                            'mb_traffic':    pt_row['mb_traffic'] or 0,
                            'latest_path_seen': pt_row['latest_path_seen'],
                        }

                # Scope the path history to the same window so badge evidence matches the since filter.
                path_time_cond = ""
                if since in datetime_offsets:
                    path_time_cond = f"AND last_seen >= datetime('now', 'localtime', {datetime_offsets[since]})"

                # Fetch the relay contacts directly (no join/group-by); their recent advert paths are
                # loaded separately below and assembled in Python. This mirrors _get_tracking_data and
                # keeps the planner on the idx_observed_paths_advert_pk_seen covering index for the path
                # scan instead of building a runtime automatic index for a LEFT JOIN + GROUP_CONCAT.
                cursor.execute(
                    f"""
                SELECT
                    c.public_key, c.name, c.role, c.device_type,
                    c.latitude, c.longitude, c.city, c.state, c.country,
                    c.first_heard, c.last_heard,
                    c.advert_count, c.out_bytes_per_hop, c.out_path_len
                FROM complete_contact_tracking c
                {where_clause}
                ORDER BY c.last_heard DESC
                """
                )
                main_rows = cursor.fetchall()

                # Recent advert paths per contact (most-recent 50), grouped in Python by public_key.
                # With idx_observed_paths_advert_pk_seen this runs as an ordered covering index scan.
                # We don't filter to the relay keys here: the loop only looks up paths for contacts in
                # main_rows, and joining the key set pushes the planner off the covering index.
                paths_by_key: dict[str, list[dict]] = {}
                if has_observed_paths:
                    cursor.execute(
                        f"""
                    WITH recent_paths AS (
                        SELECT public_key, path_hex, bytes_per_hop, observation_count, last_seen,
                               ROW_NUMBER() OVER (PARTITION BY public_key ORDER BY last_seen DESC) as rn
                        FROM observed_paths
                        WHERE packet_type = 'advert' AND public_key IS NOT NULL
                        {path_time_cond}
                    )
                    SELECT public_key, path_hex, bytes_per_hop, observation_count, last_seen
                    FROM recent_paths WHERE rn <= 50
                    ORDER BY public_key, last_seen DESC
                    """
                    )
                    for prow in cursor.fetchall():
                        ph = prow['path_hex']
                        if not ph:
                            continue
                        bph = 1
                        if prow['bytes_per_hop'] is not None:
                            try:
                                bph = int(prow['bytes_per_hop'])
                                if bph not in (1, 2, 3):
                                    bph = 1
                            except (TypeError, ValueError):
                                bph = 1
                        paths_by_key.setdefault(prow['public_key'], []).append({
                            'path_hex': ph,
                            'bytes_per_hop': bph,
                            'observation_count': int(prow['observation_count']) if prow['observation_count'] is not None else 1,
                            'last_seen': prow['last_seen'] if prow['last_seen'] is not None else None,
                        })

                # Classify each repeater
                status_counts: dict[str, int] = {
                    'multibyte_direct': 0,
                    'multibyte_relayed': 0,
                    'single_byte': 0,
                    'unknown': 0,
                }
                all_repeaters: list[dict] = []

                for row in main_rows:
                    pk = row['public_key']

                    # Recent advert paths for this contact (grouped from the path query above)
                    all_paths = paths_by_key.get(pk, [])

                    badge = self._compute_path_encoding_badge(row, all_paths, chunk_buckets)

                    obph_raw = row['out_bytes_per_hop']
                    try:
                        obph: int | None = int(obph_raw) if obph_raw is not None else None
                    except (TypeError, ValueError):
                        obph = None

                    if badge == 'multibyte':
                        status = 'multibyte_direct' if obph in (2, 3) else 'multibyte_relayed'
                    elif badge == 'one_byte':
                        status = 'single_byte'
                    else:
                        status = 'unknown'

                    status_counts[status] += 1

                    loc_parts = [p for p in [row['city'], row['state'], row['country']] if p]

                    pt = path_traffic.get(pk or '', {})
                    all_repeaters.append({
                        'public_key':      pk,
                        'name':            row['name'],
                        'role':            row['role'],
                        'device_type':     row['device_type'],
                        'status':          status,
                        'out_bytes_per_hop': obph,
                        'advert_count':    row['advert_count'] or 0,
                        'total_traffic':   pt.get('total_traffic', 0),
                        'last_seen':       row['last_heard'],
                        'first_heard':     row['first_heard'],
                        'location':        ', '.join(loc_parts),
                        'latitude':        row['latitude'],
                        'longitude':       row['longitude'],
                    })

                # ── Priority scoring ──────────────────────────────────────────────────
                unupgraded_pks: set[str] = set()
                # Build prefix → all nodes (any status) for complete prefix_peers reporting.
                # Confirmed-multibyte nodes sharing a 1-byte prefix may be the actual relay
                # in those paths, so surfacing them in the tooltip is important.
                all_by_prefix: dict[str, list[dict]] = {}
                for r in all_repeaters:
                    if r['public_key']:
                        pfx = r['public_key'][:2].lower()
                        all_by_prefix.setdefault(pfx, []).append(r)
                    if r['status'] == 'single_byte' and r['public_key']:
                        unupgraded_pks.add(r['public_key'])

                sb_metrics: dict[str, dict] = {}
                if has_observed_paths and unupgraded_pks:
                    sb_metrics = self._compute_single_byte_relay_metrics(cursor, path_time_cond)

                legacy_degree: dict[str, int] = {}
                try:
                    cursor.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='mesh_connections'"
                    )
                    if cursor.fetchone() is not None and unupgraded_pks:
                        mc_since_cond = ""
                        if since in datetime_offsets:
                            mc_since_cond = (
                                f"AND last_seen >= datetime('now', 'localtime', {datetime_offsets[since]})"
                            )
                        legacy_degree = self._compute_legacy_degree(
                            cursor, unupgraded_pks, mc_since_cond
                        )
                except Exception as e:
                    self.logger.debug(f"Legacy degree skipped: {e}")

                for r in all_repeaters:
                    if r['status'] == 'single_byte' and r['public_key']:
                        pfx = r['public_key'][:2].lower()
                        m = sb_metrics.get(pfx, {})
                        r['relay_score']    = m.get('relay_score', 0)
                        r['unique_sources'] = m.get('unique_sources', 0)
                        r['unique_dests']   = m.get('unique_dests', 0)
                        r['legacy_degree']  = legacy_degree.get(r['public_key'], 0)
                        # prefix_peers: ALL other nodes (any status) sharing this 1-byte prefix.
                        # Confirmed-multibyte peers explain inflated scores on low-traffic nodes.
                        r['prefix_peers']   = [
                            {'public_key': r2['public_key'], 'name': r2['name'], 'status': r2['status']}
                            for r2 in all_by_prefix.get(pfx, [])
                            if r2['public_key'] != r['public_key']
                        ]
                    else:
                        r['relay_score']    = 0
                        r['unique_sources'] = 0
                        r['unique_dests']   = 0
                        r['legacy_degree']  = 0
                        r['prefix_peers']   = []

                # ── Name-based suppression ────────────────────────────────────────────
                # A single_byte record that shares a name with a confirmed-multibyte
                # record is almost certainly the same physical device — the single_byte
                # entry reflects stale traffic from before the firmware upgrade.
                # Suppress it so it doesn't appear as an upgrade target or inflate counts.
                multibyte_names: set[str] = {
                    r['name'].strip().lower()
                    for r in all_repeaters
                    if r['status'] in ('multibyte_direct', 'multibyte_relayed') and r['name']
                }
                if multibyte_names:
                    kept: list[dict] = []
                    for r in all_repeaters:
                        if (r['status'] == 'single_byte'
                                and r['name']
                                and r['name'].strip().lower() in multibyte_names):
                            status_counts['single_byte'] -= 1
                        else:
                            kept.append(r)
                    all_repeaters = kept

                # Priority list: single_byte nodes ranked by relay_score desc
                priority_list = sorted(
                    [r for r in all_repeaters if r['status'] == 'single_byte'],
                    key=lambda r: (-(r['relay_score'] or 0), -(r['unique_sources'] or 0)),
                )

                # Summary
                total = len(all_repeaters)
                mb_total = status_counts['multibyte_direct'] + status_counts['multibyte_relayed']
                adoption_pct = round(mb_total / total * 100, 1) if total > 0 else 0.0

                # Overall path observation traffic breakdown (advert paths, within the since window,
                # for nodes matching the node_type filter)
                total_path_obs = multibyte_path_obs = single_byte_path_obs = 0
                traffic_mb_pct = 0.0
                if has_observed_paths:
                    # observed_paths.last_seen is an ISO timestamp → use datetime() comparison
                    if node_type == 'repeater':
                        obs_role_filter = "c.role = 'repeater'"
                    elif node_type == 'roomserver':
                        obs_role_filter = "c.role = 'roomserver'"
                    else:
                        obs_role_filter = "c.role IN ('repeater', 'roomserver')"

                    obs_time_cond = ""
                    if since in datetime_offsets:
                        obs_time_cond = f" AND op.last_seen >= datetime('now', 'localtime', {datetime_offsets[since]})"

                    cursor.execute(
                        f"""
                    SELECT op.bytes_per_hop, SUM(op.observation_count) as n
                    FROM observed_paths op
                    JOIN complete_contact_tracking c ON c.public_key = op.public_key
                    WHERE op.packet_type = 'advert' AND {obs_role_filter}{obs_time_cond}
                    GROUP BY op.bytes_per_hop
                    """
                    )
                    for tr in cursor.fetchall():
                        n = tr['n'] or 0
                        total_path_obs += n
                        bph = tr['bytes_per_hop'] or 1
                        try:
                            bph = int(bph)
                        except (TypeError, ValueError):
                            bph = 1
                        if bph in (2, 3):
                            multibyte_path_obs += n
                        else:
                            single_byte_path_obs += n

                    if total_path_obs > 0:
                        traffic_mb_pct = round(multibyte_path_obs / total_path_obs * 100, 1)

                # Daily trend: last 30 days, filtered by node_type
                daily_trend: list[dict] = []
                if has_observed_paths:
                    if node_type == 'repeater':
                        trend_role_filter = "c.role = 'repeater'"
                    elif node_type == 'roomserver':
                        trend_role_filter = "c.role = 'roomserver'"
                    else:
                        trend_role_filter = "c.role IN ('repeater', 'roomserver')"

                    cursor.execute(
                        f"""
                    SELECT date(op.last_seen) as obs_date,
                        SUM(CASE WHEN op.bytes_per_hop IN (2,3)
                                 THEN op.observation_count ELSE 0 END) as mb_obs,
                        SUM(CASE WHEN op.bytes_per_hop = 1 OR op.bytes_per_hop IS NULL
                                 THEN op.observation_count ELSE 0 END) as sb_obs
                    FROM observed_paths op
                    JOIN complete_contact_tracking c ON c.public_key = op.public_key
                    WHERE {trend_role_filter}
                      AND op.last_seen >= datetime('now', 'localtime', '-30 days')
                      AND op.packet_type = 'advert'
                    GROUP BY date(op.last_seen)
                    ORDER BY obs_date
                    """
                    )
                    for tr in cursor.fetchall():
                        daily_trend.append({
                            'date':        tr['obs_date'],
                            'multibyte':   tr['mb_obs'] or 0,
                            'single_byte': tr['sb_obs'] or 0,
                        })

                return {
                    'since': since,
                    'node_type': node_type,
                    'summary': {
                        'total_repeaters':      total,
                        'multibyte_direct':     status_counts['multibyte_direct'],
                        'multibyte_relayed':    status_counts['multibyte_relayed'],
                        'single_byte':          status_counts['single_byte'],
                        'unknown':              status_counts['unknown'],
                        'adoption_pct':         adoption_pct,
                        'total_path_obs':       total_path_obs,
                        'multibyte_path_obs':   multibyte_path_obs,
                        'single_byte_path_obs': single_byte_path_obs,
                        'traffic_multibyte_pct': traffic_mb_pct,
                    },
                    'priority_list': priority_list,
                    'all_repeaters': all_repeaters,
                    'daily_trend':   daily_trend,
                }

        except Exception as e:
            self.logger.error(f"Error getting multibyte rollout data: {e}")
            return {
                'since': since,
                'error': str(e),
                'summary': {
                    'total_repeaters': 0, 'multibyte_direct': 0,
                    'multibyte_relayed': 0, 'single_byte': 0, 'unknown': 0,
                    'adoption_pct': 0.0, 'total_path_obs': 0,
                    'multibyte_path_obs': 0, 'single_byte_path_obs': 0,
                    'traffic_multibyte_pct': 0.0,
                },
                'priority_list': [],
                'all_repeaters': [],
                'daily_trend': [],
            }
