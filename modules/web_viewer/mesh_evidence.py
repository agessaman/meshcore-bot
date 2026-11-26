"""Mesh graph evidence for the web viewer: multi-byte path edges and neighbor-confirmed edges."""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple


def _parse_utc(value: Any) -> datetime | None:
    """Parse a ``neighbor_links`` timestamp (UTC ISO, with or without offset)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _valid_coordinates(latitude: Any, longitude: Any) -> bool:
    """Same rule the repeater node query applies: present, finite, in range, nonzero."""
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    if lat == 0 or lon == 0:
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


class NeighborEvidenceKeys(NamedTuple):
    """Directed edge identities that zero-hop neighbor discovery has confirmed.

    A ``mesh_connections`` edge counts as neighbor-confirmed if it matches on
    either key space; see BotDataViewer._neighbor_evidence_edge_keys.
    """

    prefixes: set[tuple[str, str]]
    public_keys: set[tuple[str, str]]


class MeshEvidenceMixin:
    """Mixed into BotDataViewer."""

    NEIGHBOR_PREFIX_HEX_CHARS: Any
    _mesh_graph_cache_seconds: Any
    _multibyte_graph_cache_computing: Any
    _multibyte_graph_cache_condition: Any
    _multibyte_graph_cache_created_at: Any
    _multibyte_graph_cache_edges: Any
    _multibyte_graph_cache_failure: Any
    _multibyte_graph_cache_failure_at: Any
    _multibyte_graph_cache_retry_seconds: Any
    _with_db_connection: Any
    config: Any
    logger: Any

    def _local_radio_nodes(
        self,
        conn: Any,
        prefix_hex_chars: int,
        days: int | None = None,
    ) -> list[dict[str, Any]]:
        """The bot's own radio as mesh nodes, one per key that ran discovery.

        Every neighbor-evidence edge has the local radio on one end, but the
        radio is a companion, so it never appears among the repeater and room
        server contacts the node list is built from. Without this, the graph
        drops every confirmed-neighbor edge for want of an endpoint.

        The key comes from ``neighbor_links.self_public_key``: the viewer runs in
        its own process and has no other record of the device's identity. More
        than one key means the radio was replaced; only the most recent one is
        given the configured name and position, since those describe the
        station as it is now.

        Position prefers the radio's own contact row, then ``[Bot]
        bot_latitude``/``bot_longitude``. A node with neither is still returned,
        with null coordinates: the graph view lays nodes out by force, not by
        position, and the map view skips it.
        """
        try:
            rows = conn.execute(
                '''
                SELECT self_public_key, MAX(last_seen) AS last_seen
                FROM neighbor_links
                GROUP BY self_public_key
                ORDER BY last_seen DESC
                '''
            ).fetchall()
        except Exception as exc:
            # A pre-migration database has no neighbor_links table.
            self.logger.debug(f"Local radio node unavailable: {exc}")
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days is not None else None
        nodes: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            public_key = (row['self_public_key'] or '').lower()
            if not public_key:
                continue
            last_seen = row['last_seen']
            if cutoff is not None:
                seen = _parse_utc(last_seen)
                if seen is None or seen < cutoff:
                    continue
            is_current = index == 0
            contact = self._local_radio_contact(conn, public_key)

            latitude = longitude = None
            if contact and _valid_coordinates(contact['latitude'], contact['longitude']):
                latitude, longitude = float(contact['latitude']), float(contact['longitude'])
            elif is_current:
                latitude, longitude = self._configured_bot_position()

            prefix = public_key[:prefix_hex_chars]
            name = (contact['name'] if contact else '') or ''
            if not name and is_current:
                name = (self.config.get('Bot', 'bot_name', fallback='') or '').strip()
            if not name:
                name = f"Local radio {prefix.upper()}"

            nodes.append({
                'public_key': public_key,
                'prefix': prefix,
                'name': name,
                'latitude': latitude,
                'longitude': longitude,
                'role': 'local',
                'is_local': True,
                'is_starred': False,
                'last_heard': last_seen,
                'last_advert_timestamp': None,
            })
        return nodes

    @staticmethod
    def _local_radio_contact(conn: Any, public_key: str) -> Any:
        """The radio's own contact row, if another node's advert put it there."""
        try:
            return conn.execute(
                '''
                SELECT name, latitude, longitude
                FROM complete_contact_tracking
                WHERE LOWER(public_key) = ?
                LIMIT 1
                ''',
                (public_key,),
            ).fetchone()
        except Exception:
            return None

    def _configured_bot_position(self) -> tuple[float | None, float | None]:
        """``[Bot] bot_latitude``/``bot_longitude``, or (None, None) if unusable."""
        try:
            latitude = self.config.getfloat('Bot', 'bot_latitude', fallback=None)
            longitude = self.config.getfloat('Bot', 'bot_longitude', fallback=None)
        except (TypeError, ValueError):
            return None, None
        if _valid_coordinates(latitude, longitude):
            return float(latitude), float(longitude)
        return None, None

    def _derive_multibyte_evidence_edges(
        self,
        days: int | None = None,
        min_observations: int | None = None,
        *,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Return lifetime-derived multi-byte edges filtered for the API view."""
        all_edges = self._aggregate_multibyte_evidence_edges(
            force_refresh=force_refresh
        )
        return self._filter_multibyte_evidence_edges(
            all_edges,
            days=days,
            min_observations=min_observations,
        )

    def _derive_multibyte_evidence_graph(
        self,
        days: int | None = None,
        min_observations: int | None = None,
        *,
        force_refresh: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return filtered edges plus the lifetime graph's prefix resolution."""
        all_edges = self._aggregate_multibyte_evidence_edges(
            force_refresh=force_refresh
        )
        prefix_hex_chars = max(
            (len(edge['from_prefix']) for edge in all_edges),
            default=2,
        )
        return (
            self._filter_multibyte_evidence_edges(
                all_edges,
                days=days,
                min_observations=min_observations,
            ),
            max(2, prefix_hex_chars),
        )

    @staticmethod
    def _filter_multibyte_evidence_edges(
        edges: list[dict[str, Any]],
        days: int | None,
        min_observations: int | None,
    ) -> list[dict[str, Any]]:
        """Apply view filters without changing lifetime edge identity or counts."""
        cutoff_naive = datetime.now() - timedelta(days=days) if days is not None else None
        cutoff_utc = (
            datetime.now(timezone.utc) - timedelta(days=days)
            if days is not None
            else None
        )
        result = []
        for edge in edges:
            if cutoff_naive is not None and edge['last_seen']:
                try:
                    last_seen = datetime.fromisoformat(
                        str(edge['last_seen']).replace('Z', '+00:00')
                    )
                except (TypeError, ValueError):
                    # Preserve the historical behavior for malformed timestamps:
                    # they remain visible rather than silently losing graph data.
                    last_seen = None
                if last_seen is not None:
                    if last_seen.tzinfo is None:
                        if last_seen < cutoff_naive:
                            continue
                    elif cutoff_utc is not None and last_seen.astimezone(timezone.utc) < cutoff_utc:
                        continue
            if (
                min_observations is not None
                and edge['observation_count'] < min_observations
            ):
                continue
            result.append(edge)
        return result

    def _aggregate_multibyte_evidence_edges(
        self, *, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        """Return a bounded-age, single-flight lifetime multi-byte aggregate."""
        def previous_failure(message: str) -> RuntimeError:
            failure = self._multibyte_graph_cache_failure
            if failure is None:
                return RuntimeError(message)
            failure_type, failure_message = failure
            return RuntimeError(
                f"{message}: {failure_type}: {failure_message}"
            )

        now = time.monotonic()
        with self._multibyte_graph_cache_condition:
            cached = self._multibyte_graph_cache_edges
            cache_age = now - self._multibyte_graph_cache_created_at
            failure_age = now - self._multibyte_graph_cache_failure_at
            retry_suppressed = (
                self._multibyte_graph_cache_failure is not None
                and failure_age < self._multibyte_graph_cache_retry_seconds
            )
            if retry_suppressed:
                if cached is not None and not force_refresh:
                    return cached
                raise previous_failure(
                    "Multi-byte mesh aggregation retry suppressed after failure"
                )
            if (
                not force_refresh
                and cached is not None
                and cache_age < self._mesh_graph_cache_seconds
            ):
                return cached

            if self._multibyte_graph_cache_computing:
                # Prefer a slightly stale result to making concurrent clients
                # duplicate the same expensive SQLite aggregation.
                if cached is not None and not force_refresh:
                    return cached
                while self._multibyte_graph_cache_computing:
                    self._multibyte_graph_cache_condition.wait()
                cached = self._multibyte_graph_cache_edges
                if self._multibyte_graph_cache_failure is not None:
                    if cached is not None and not force_refresh:
                        return cached
                    raise previous_failure(
                        "Concurrent multi-byte mesh aggregation failed"
                    )
                if cached is not None:
                    return cached

            self._multibyte_graph_cache_computing = True
            stale = cached

        started_at = time.monotonic()
        try:
            computed = self._compute_multibyte_evidence_edges()
        except Exception as exc:
            self.logger.warning(
                "Multi-byte mesh aggregation failed%s",
                "; serving cached data"
                if stale is not None and not force_refresh
                else "",
                exc_info=True,
            )
            with self._multibyte_graph_cache_condition:
                self._multibyte_graph_cache_failure = (
                    type(exc).__name__,
                    str(exc),
                )
                self._multibyte_graph_cache_failure_at = time.monotonic()
                self._multibyte_graph_cache_computing = False
                self._multibyte_graph_cache_condition.notify_all()
            if stale is not None and not force_refresh:
                return stale
            raise

        elapsed = time.monotonic() - started_at
        with self._multibyte_graph_cache_condition:
            self._multibyte_graph_cache_edges = computed
            self._multibyte_graph_cache_created_at = time.monotonic()
            self._multibyte_graph_cache_failure = None
            self._multibyte_graph_cache_failure_at = 0.0
            self._multibyte_graph_cache_computing = False
            self._multibyte_graph_cache_condition.notify_all()

        self.logger.debug(
            "Computed %d multi-byte mesh edges in %.3fs",
            len(computed),
            elapsed,
        )
        return computed

    def _compute_multibyte_evidence_edges(self) -> list[dict[str, Any]]:
        """Derive mesh edges purely from multi-byte path evidence.

        Splits each observed_paths row with bytes_per_hop >= 2 into consecutive
        hop pairs and aggregates per directed pair. Unlike mesh_connections, this
        never mixes in single-byte observations, so edge identity is unambiguous
        (up to 2/3-byte prefix collisions, which are rare).

        Edges observed at 2-byte resolution are coalesced into a 3-byte edge when
        exactly one 3-byte edge prefix-matches both endpoints — the same
        unique-match rule MeshGraph.add_edge applies at write time.

        Returns edge dicts matching the /api/mesh/edges schema, plus:
          path_count — number of distinct observed paths crossing the edge
          evidence   — always 'multibyte'
        """
        # Split paths and aggregate directed hop pairs in SQLite. This preserves
        # lifetime counts and cross-resolution coalescing while avoiding one
        # Python row/dict/list per observed path (hundreds of thousands on busy
        # meshes). The selected timeframe is applied only after coalescing,
        # matching the historical client-side filter semantics.
        query = '''
            WITH RECURSIVE edge_parts(
                path_hex, step, observation_count, first_seen, last_seen,
                hop_position, from_prefix, to_prefix, next_offset
            ) AS (
                SELECT
                    LOWER(path_hex),
                    bytes_per_hop * 2,
                    CASE
                        WHEN observation_count IS NULL OR observation_count = 0 THEN 1
                        ELSE observation_count
                    END,
                    first_seen,
                    last_seen,
                    1,
                    SUBSTR(LOWER(path_hex), 1, bytes_per_hop * 2),
                    SUBSTR(LOWER(path_hex), bytes_per_hop * 2 + 1, bytes_per_hop * 2),
                    bytes_per_hop * 4 + 1
                FROM observed_paths
                WHERE bytes_per_hop >= 2
                  AND path_hex IS NOT NULL
                  AND LENGTH(path_hex) > 0
                  AND LENGTH(path_hex) % (bytes_per_hop * 2) = 0
                  AND LENGTH(path_hex) >= bytes_per_hop * 4

                UNION ALL

                SELECT
                    path_hex,
                    step,
                    observation_count,
                    first_seen,
                    last_seen,
                    hop_position + 1,
                    to_prefix,
                    SUBSTR(path_hex, next_offset, step),
                    next_offset + step
                FROM edge_parts
                WHERE LENGTH(path_hex) >= next_offset + step - 1
            )
            SELECT
                from_prefix,
                to_prefix,
                SUM(observation_count) AS observation_count,
                COUNT(*) AS path_count,
                MIN(first_seen) AS first_seen,
                MAX(last_seen) AS last_seen,
                SUM(hop_position * observation_count) AS hop_position_sum
            FROM edge_parts
            GROUP BY from_prefix, to_prefix
        '''

        with self._with_db_connection() as conn:
            rows = conn.execute(query).fetchall()

        edges: dict[tuple[str, str], dict[str, Any]] = {
            (row['from_prefix'], row['to_prefix']): {
                'observation_count': row['observation_count'],
                'path_count': row['path_count'],
                'first_seen': row['first_seen'],
                'last_seen': row['last_seen'],
                'hop_position_sum': row['hop_position_sum'],
            }
            for row in rows
        }

        # Coalesce 2-byte edges into a 3-byte edge when exactly one matches.
        # (Hops within a path share one resolution, so keys are homogeneous.)
        by_truncated_key: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for key in edges:
            if len(key[0]) == 6:
                by_truncated_key.setdefault((key[0][:4], key[1][:4]), []).append(key)
        for key in [k for k in edges if len(k[0]) == 4]:
            candidates = by_truncated_key.get(key, [])
            if len(candidates) == 1:
                target = edges[candidates[0]]
                source = edges.pop(key)
                target['observation_count'] += source['observation_count']
                target['path_count'] += source['path_count']
                target['hop_position_sum'] += source['hop_position_sum']
                if source['first_seen'] and (target['first_seen'] is None or source['first_seen'] < target['first_seen']):
                    target['first_seen'] = source['first_seen']
                if source['last_seen'] and (target['last_seen'] is None or source['last_seen'] > target['last_seen']):
                    target['last_seen'] = source['last_seen']

        result = []
        for (from_prefix, to_prefix), agg in edges.items():
            result.append({
                'from_prefix': from_prefix,
                'to_prefix': to_prefix,
                'from_public_key': None,
                'to_public_key': None,
                'observation_count': agg['observation_count'],
                'path_count': agg['path_count'],
                'first_seen': agg['first_seen'],
                'last_seen': agg['last_seen'],
                'avg_hop_position': agg['hop_position_sum'] / agg['observation_count'],
                'geographic_distance': None,
                'evidence': 'multibyte',
            })
        result.sort(key=lambda e: e['last_seen'] or '', reverse=True)
        return result

    def _neighbor_evidence_edge_keys(
        self,
        days: int | None = None,
    ) -> NeighborEvidenceKeys:
        """Directed pairs that confirmed zero-hop discovery has proven.

        Used to upgrade the evidence label in the combined view, where the edge
        itself comes from ``mesh_connections`` and so has lost its provenance.
        Two key spaces are returned because a ``mesh_connections`` edge can be
        matched by either:

        * ``prefixes`` — 3-byte prefix pairs, matching edges the graph stores at
          the same resolution neighbor discovery feeds it.
        * ``public_keys`` — full-key pairs, for edges the graph deliberately keeps
          at a *shorter* prefix (see ``MeshGraph.add_edge``: a 1-byte edge with no
          public key is not promoted, so several nodes keep sharing it) while
          still filling in the public keys discovery supplied. Truncating our
          keys down to 2 chars instead would be wrong — it would relabel every
          other node sharing that byte.

        ``days`` windows the evidence the same way the caller windows its edges.
        ``neighbor_links`` is never pruned, so without it a link last seen years
        ago would keep labelling a recent path-derived edge a current neighbor.
        """
        try:
            edges = self._derive_neighbor_evidence_graph(days=days)[0]
        except Exception as exc:
            # A pre-migration-22 database simply has no neighbor evidence.
            self.logger.debug(f"Neighbor evidence keys unavailable: {exc}")
            return NeighborEvidenceKeys(set(), set())

        # Both directions are already emitted per link, so no reversing here.
        prefixes = {
            (edge['from_prefix'], edge['to_prefix'])
            for edge in edges
            if edge['from_prefix'] and edge['to_prefix']
        }
        public_keys = {
            (edge['from_public_key'], edge['to_public_key'])
            for edge in edges
            if edge['from_public_key'] and edge['to_public_key']
        }
        return NeighborEvidenceKeys(prefixes, public_keys)

    def _compute_neighbor_evidence_edges(self) -> list[dict[str, Any]]:
        """Derive mesh edges from confirmed zero-hop neighbor discovery.

        This is the strongest evidence class in the database: each row is a
        direct RF reception between two *full* public keys with a measured SNR,
        recorded by modules/neighbors_discovery.py. Two differences from the
        multi-byte path derivation are worth noting:

        * ``from_public_key``/``to_public_key`` are populated. Path-derived edges
          cannot fill these in, because a path carries prefixes only.
        * ``snr``/``best_snr`` are real measurements. Unlike the dashboard's
          one-hop panel, which withholds SNR unless two sources agree because
          ``complete_contact_tracking.hop_count`` over-claims zero-hop, a
          discover response *is* the authoritative first-party measurement.

        Both directions are emitted per link: a discover response proves we
        transmitted, they received, they transmitted, and we received. Each
        direction carries its own measurement: neighbor-to-us is how well we
        heard the response (``snr``), us-to-neighbor is how well they heard the
        request (``snr_in``, echoed in the response). Links recorded before the
        inbound figure was kept have none for that direction, so it carries no
        SNR rather than borrowing the other direction's.
        """
        chars = self.NEIGHBOR_PREFIX_HEX_CHARS
        query = '''
            SELECT
                self_public_key,
                neighbor_public_key,
                observation_count,
                snr_sum,
                snr_count,
                best_snr,
                last_snr,
                {inbound},
                first_seen,
                last_seen
            FROM neighbor_links
        '''
        try:
            with self._with_db_connection() as conn:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(neighbor_links)")
                }
                # Before migration 25 there are no inbound columns; select NULLs.
                inbound = ', '.join(
                    name if name in columns else f'NULL AS {name}'
                    for name in ('snr_in_sum', 'snr_in_count', 'best_snr_in', 'last_snr_in')
                )
                rows = conn.execute(query.format(inbound=inbound)).fetchall()
        except Exception as exc:
            self.logger.debug(f"Neighbor evidence edges unavailable: {exc}")
            return []

        edges: list[dict[str, Any]] = []
        for row in rows:
            self_key = (row['self_public_key'] or '').lower()
            neighbor_key = (row['neighbor_public_key'] or '').lower()
            if not self_key or not neighbor_key:
                continue
            snr_count = row['snr_count'] or 0
            heard_by_us = {
                'snr': (row['snr_sum'] / snr_count) if snr_count else None,
                'best_snr': row['best_snr'],
                'last_snr': row['last_snr'],
            }
            snr_in_count = row['snr_in_count'] or 0
            heard_by_them = {
                'snr': (row['snr_in_sum'] / snr_in_count) if snr_in_count else None,
                'best_snr': row['best_snr_in'] if snr_in_count else None,
                'last_snr': row['last_snr_in'] if snr_in_count else None,
            }
            # An edge from A to B is measured where B receives.
            for from_key, to_key, signal in (
                (self_key, neighbor_key, heard_by_them),
                (neighbor_key, self_key, heard_by_us),
            ):
                edges.append({
                    'from_prefix': from_key[:chars],
                    'to_prefix': to_key[:chars],
                    'from_public_key': from_key,
                    'to_public_key': to_key,
                    'observation_count': row['observation_count'] or 1,
                    'first_seen': row['first_seen'],
                    'last_seen': row['last_seen'],
                    # A direct link is by definition the first hop of any path
                    # that crosses it.
                    'avg_hop_position': 1.0,
                    'geographic_distance': None,
                    'snr': signal['snr'],
                    'best_snr': signal['best_snr'],
                    'last_snr': signal['last_snr'],
                    'evidence': 'neighbors',
                })

        edges.sort(key=lambda e: e['last_seen'] or '', reverse=True)
        return edges

    def _derive_neighbor_evidence_graph(
        self,
        days: int | None = None,
        min_observations: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Filtered neighbor-evidence edges plus their prefix resolution.

        Reuses the multi-byte view filter: it only touches ``last_seen`` and
        ``observation_count`` (handling both naive and aware timestamps), which
        is exactly the filtering these edges need.
        """
        all_edges = self._compute_neighbor_evidence_edges()
        filtered = self._filter_multibyte_evidence_edges(
            all_edges, days=days, min_observations=min_observations
        )
        return filtered, self.NEIGHBOR_PREFIX_HEX_CHARS
