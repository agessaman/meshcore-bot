"""Mesh graph evidence for the web viewer: multi-byte path edges and neighbor-confirmed edges."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple


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
    logger: Any

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
        transmitted, they received, they transmitted, and we received.
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
                first_seen,
                last_seen
            FROM neighbor_links
        '''
        try:
            with self._with_db_connection() as conn:
                rows = conn.execute(query).fetchall()
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
            mean_snr = (row['snr_sum'] / snr_count) if snr_count else None
            for from_key, to_key in ((self_key, neighbor_key), (neighbor_key, self_key)):
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
                    'snr': mean_snr,
                    'best_snr': row['best_snr'],
                    'last_snr': row['last_snr'],
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
