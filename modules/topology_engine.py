#!/usr/bin/env python3
"""
Probabilistic topology engine for shadow/cutover path resolution.

This engine supplements the legacy mesh graph heuristics with a lightweight
Viterbi-style decode across candidate repeater states. It is intentionally
additive: legacy tables and APIs remain authoritative unless mode='new'.
"""

import json
import math
import random
import sqlite3
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple


class TopologyEngine:
    """Shadow-capable probabilistic resolver for prefix-collision paths."""

    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self.db = bot.db_manager
        self.prefix_hex_chars = max(2, getattr(bot, "prefix_hex_chars", 2))

        cfg = bot.config
        self.mode = cfg.get("Path_Command", "topology_engine_mode", fallback="legacy").lower()
        self.shadow_sample_rate = max(
            0.0, min(1.0, cfg.getfloat("Path_Command", "topology_shadow_sample_rate", fallback=1.0))
        )
        self.ghost_enabled = cfg.getboolean("Path_Command", "topology_ghost_enabled", fallback=True)
        self.ghost_threshold = max(
            0.0, min(1.0, cfg.getfloat("Path_Command", "topology_ghost_min_confidence", fallback=0.35))
        )
        self.max_candidates_per_prefix = max(
            1, cfg.getint("Path_Command", "topology_max_candidates_per_prefix", fallback=12)
        )
        self.min_edge_observations = max(
            1, cfg.getint("Path_Command", "min_edge_observations", fallback=3)
        )

    def ingest_path_observation(
        self,
        path_nodes: List[str],
        packet_hash: Optional[str] = None,
        bytes_per_hop: Optional[int] = None,
    ) -> None:
        """Capture normalized path observations for offline/topology analysis."""
        if not path_nodes:
            return
        try:
            path_hex = "".join(path_nodes).lower()
            self.db.execute_update(
                """
                INSERT INTO topology_inference_shadow
                (path_hex, path_nodes_json, resolved_path_json, method, model_confidence, packet_hash, bytes_per_hop)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    path_hex,
                    json.dumps(path_nodes),
                    json.dumps([]),
                    "shadow_ingest",
                    0.0,
                    packet_hash,
                    bytes_per_hop,
                ),
            )
        except Exception as e:
            self.logger.debug(f"Topology engine ingest_path_observation failed: {e}")

    def resolve_path_candidates(
        self,
        node_id: str,
        path_context: List[str],
        repeaters: List[Dict[str, Any]],
        current_index: int,
    ) -> Tuple[Optional[Dict[str, Any]], float, Optional[str]]:
        """Resolve the best repeater candidate for a colliding node via Viterbi."""
        if not repeaters or current_index < 0 or current_index >= len(path_context):
            return None, 0.0, None

        state_sets: List[List[Dict[str, Any]]] = []
        for i, path_node in enumerate(path_context):
            if i == current_index:
                candidates = self._normalize_repeaters(repeaters, path_node)
            else:
                candidates = self._query_candidates_for_prefix(path_node)

            if not candidates:
                candidates = [self._ghost_state(path_node)]
            state_sets.append(candidates)

        best_sequence, best_score = self._viterbi_decode(state_sets, path_context)
        if not best_sequence:
            return None, 0.0, None

        selected = best_sequence[current_index]
        confidence = self._score_to_confidence(best_score, path_length=len(path_context))
        if selected.get("is_ghost"):
            if self.ghost_enabled:
                self._record_ghost_node(selected, path_context)
                return None, confidence, "topology_viterbi_ghost"
            return None, 0.0, None

        selected_pk = selected.get("public_key")
        resolved = None
        for candidate in repeaters:
            if candidate.get("public_key") == selected_pk:
                resolved = candidate
                break

        if not resolved and repeaters:
            # Best-effort fallback if candidate mapping fails.
            resolved = max(repeaters, key=lambda r: self._recency_score(r))

        if confidence < self.ghost_threshold and self.ghost_enabled:
            # Too uncertain: treat as ghost hypothesis in shadow mode.
            self._record_ghost_node(self._ghost_state(node_id), path_context)
            return None, confidence, "topology_viterbi_ghost"

        return resolved, confidence, "topology_viterbi"

    def select_for_hop(
        self,
        repeaters: List[Dict[str, Any]],
        node_id: str,
        path_context: List[str],
    ) -> Dict[str, Any]:
        """Command-facing topology selection wrapper with stable return shape."""
        if not repeaters or not path_context:
            return {
                "repeater": None,
                "confidence": 0.0,
                "method": None,
                "is_topology_guess": False,
            }
        try:
            current_index = path_context.index(node_id) if node_id in path_context else -1
            if current_index < 0:
                return {
                    "repeater": None,
                    "confidence": 0.0,
                    "method": None,
                    "is_topology_guess": False,
                }
            repeater, confidence, method = self.resolve_path_candidates(
                node_id=node_id,
                path_context=path_context,
                repeaters=repeaters,
                current_index=current_index,
            )
            return {
                "repeater": repeater,
                "confidence": confidence,
                "method": method,
                "is_topology_guess": bool(method and method.startswith("topology")),
            }
        except Exception as e:
            self.logger.debug(f"Topology engine select_for_hop failed: {e}")
            return {
                "repeater": None,
                "confidence": 0.0,
                "method": None,
                "is_topology_guess": False,
            }

    def maybe_record_shadow_comparison(
        self,
        topology_mode: str,
        path_nodes: List[str],
        model_result: Optional[Dict[str, Any]],
        legacy_choice: Optional[Dict[str, Any]],
        legacy_confidence: float,
        legacy_method: Optional[str],
        non_collision: bool = False,
        packet_hash: Optional[str] = None,
        bytes_per_hop: Optional[int] = None,
    ) -> bool:
        """Record shadow telemetry only when the engine is in shadow mode."""
        if (topology_mode or "").lower() != "shadow":
            return False
        model_result = model_result or {}
        return self.record_shadow_comparison(
            path_nodes=path_nodes,
            model_choice=model_result.get("repeater"),
            model_confidence=float(model_result.get("confidence") or 0.0),
            model_method=model_result.get("method"),
            legacy_choice=legacy_choice,
            legacy_confidence=float(legacy_confidence or 0.0),
            legacy_method=legacy_method,
            packet_hash=packet_hash,
            bytes_per_hop=bytes_per_hop,
            non_collision=non_collision,
        )

    def record_shadow_comparison(
        self,
        path_nodes: List[str],
        model_choice: Optional[Dict[str, Any]],
        model_confidence: float,
        model_method: Optional[str],
        legacy_choice: Optional[Dict[str, Any]],
        legacy_confidence: float,
        legacy_method: Optional[str],
        packet_hash: Optional[str] = None,
        bytes_per_hop: Optional[int] = None,
        non_collision: bool = False,
        backfill_key: Optional[str] = None,
    ) -> bool:
        """Persist legacy-vs-new comparison rows and update daily metrics."""
        if random.random() > self.shadow_sample_rate:
            return False

        model_pk = (model_choice or {}).get("public_key")
        legacy_pk = (legacy_choice or {}).get("public_key")
        agreement = 1 if model_pk and legacy_pk and model_pk == legacy_pk else 0
        metric_date = datetime.now().strftime("%Y-%m-%d")

        resolved_path_json = json.dumps(
            {
                "model_public_key": model_pk,
                "legacy_public_key": legacy_pk,
                "model_name": (model_choice or {}).get("name"),
                "legacy_name": (legacy_choice or {}).get("name"),
            }
        )
        path_hex = "".join(path_nodes).lower() if path_nodes else ""

        try:
            if backfill_key:
                existing_backfill = self.db.execute_query(
                    "SELECT id FROM topology_inference_shadow WHERE backfill_key = ? LIMIT 1",
                    (backfill_key,),
                )
                if existing_backfill:
                    # Idempotent backfill: do not duplicate rows or re-roll metrics.
                    return False

            self.db.execute_update(
                """
                INSERT INTO topology_inference_shadow
                (path_hex, path_nodes_json, resolved_path_json, method, model_confidence, legacy_method, legacy_confidence, agreement, packet_hash, bytes_per_hop, backfill_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    path_hex,
                    json.dumps(path_nodes),
                    resolved_path_json,
                    model_method or "topology_viterbi",
                    float(model_confidence or 0.0),
                    legacy_method,
                    float(legacy_confidence or 0.0),
                    agreement,
                    packet_hash,
                    bytes_per_hop,
                    backfill_key,
                ),
            )

            existing = self.db.execute_query(
                "SELECT * FROM topology_model_metrics WHERE metric_date = ? LIMIT 1",
                (metric_date,),
            )
            if existing:
                row = existing[0]
                total = int(row.get("total_comparisons", 0)) + 1
                agree_count = int(row.get("agreement_count", 0)) + agreement
                disagree_count = int(row.get("disagreement_count", 0)) + (1 - agreement)
                nc_total = int(row.get("non_collision_comparisons", 0)) + (1 if non_collision else 0)
                nc_agree = int(row.get("non_collision_agreement_count", 0)) + (1 if non_collision and agreement else 0)
                self.db.execute_update(
                    """
                    UPDATE topology_model_metrics
                    SET total_comparisons = ?,
                        agreement_count = ?,
                        disagreement_count = ?,
                        non_collision_comparisons = ?,
                        non_collision_agreement_count = ?,
                        avg_legacy_confidence = ?,
                        avg_model_confidence = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE metric_date = ?
                    """,
                    (
                        total,
                        agree_count,
                        disagree_count,
                        nc_total,
                        nc_agree,
                        self._rolling_avg(float(row.get("avg_legacy_confidence") or 0.0), float(legacy_confidence or 0.0), total),
                        self._rolling_avg(float(row.get("avg_model_confidence") or 0.0), float(model_confidence or 0.0), total),
                        metric_date,
                    ),
                )
            else:
                self.db.execute_update(
                    """
                    INSERT INTO topology_model_metrics
                    (metric_date, total_comparisons, agreement_count, disagreement_count,
                     non_collision_comparisons, non_collision_agreement_count,
                     avg_legacy_confidence, avg_model_confidence, metadata_json)
                    VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        metric_date,
                        agreement,
                        1 - agreement,
                        1 if non_collision else 0,
                        1 if non_collision and agreement else 0,
                        float(legacy_confidence or 0.0),
                        float(model_confidence or 0.0),
                        json.dumps({}),
                    ),
                )
            return True
        except Exception as e:
            self.logger.debug(f"Topology shadow comparison write failed: {e}")
            return False

    def get_shadow_diagnostics(self, days: int = 7) -> Dict[str, Any]:
        """Return summarized shadow diagnostics for optional API/debug use."""
        days = max(1, min(90, int(days or 7)))
        metrics = self.db.execute_query(
            """
            SELECT metric_date, total_comparisons, agreement_count, disagreement_count,
                   non_collision_comparisons, non_collision_agreement_count,
                   avg_legacy_confidence, avg_model_confidence
            FROM topology_model_metrics
            WHERE metric_date >= date('now', ?)
            ORDER BY metric_date DESC
            """,
            (f"-{days} days",),
        )
        recent = self.db.execute_query(
            """
            SELECT path_hex, method, model_confidence, legacy_method, legacy_confidence, agreement, created_at
            FROM topology_inference_shadow
            WHERE created_at >= datetime('now', ?)
            ORDER BY created_at DESC
            LIMIT 100
            """,
            (f"-{days} days",),
        )
        ghosts = self.db.execute_query(
            """
            SELECT ghost_id, prefix, evidence_count, confidence_tier, model_confidence, first_seen, last_seen
            FROM topology_ghost_nodes
            ORDER BY last_seen DESC
            LIMIT 100
            """
        )
        return {"metrics": metrics, "recent_comparisons": recent, "ghost_nodes": ghosts}

    def get_model_graph(
        self,
        days: int = 7,
        min_confidence: float = 0.0,
        include_ghost: bool = False,
    ) -> Dict[str, Any]:
        """Build a model-derived mesh graph payload from shadow comparison rows."""
        days = max(1, min(90, int(days or 7)))
        min_confidence = max(0.0, min(1.0, float(min_confidence or 0.0)))
        model_rows = self.db.execute_query(
            """
            SELECT path_nodes_json, method, model_confidence, created_at
            FROM topology_inference_shadow
            WHERE created_at >= datetime('now', ?)
              AND method != 'shadow_ingest'
              AND model_confidence >= ?
            ORDER BY created_at DESC
            LIMIT 5000
            """,
            (f"-{days} days", min_confidence),
        )

        edge_acc: Dict[str, Dict[str, Any]] = {}
        node_prefixes: set = set()
        prefix_hex_chars = self.prefix_hex_chars

        for row in model_rows:
            raw_nodes = row.get("path_nodes_json")
            created_at = row.get("created_at")
            method = (row.get("method") or "topology_viterbi").strip() or "topology_viterbi"
            confidence = float(row.get("model_confidence") or 0.0)
            if confidence < min_confidence:
                continue

            path_nodes: List[str] = []
            if raw_nodes:
                try:
                    parsed = json.loads(raw_nodes)
                    if isinstance(parsed, list):
                        path_nodes = [str(v).lower() for v in parsed if v is not None]
                except (TypeError, ValueError, json.JSONDecodeError):
                    path_nodes = []
            if len(path_nodes) < 2:
                continue

            # Track prefix length seen in model rows so frontend node/edge prefixes stay aligned.
            for node in path_nodes:
                if not node:
                    continue
                prefix_hex_chars = max(prefix_hex_chars, len(node))
                node_prefixes.add(node)

            for i in range(len(path_nodes) - 1):
                from_prefix = (path_nodes[i] or "").lower()
                to_prefix = (path_nodes[i + 1] or "").lower()
                if not from_prefix or not to_prefix:
                    continue
                if not include_ghost and (from_prefix.startswith("ghost:") or to_prefix.startswith("ghost:")):
                    continue
                edge_key = f"{from_prefix}->{to_prefix}"
                current = edge_acc.get(edge_key)
                if not current:
                    current = {
                        "from_prefix": from_prefix,
                        "to_prefix": to_prefix,
                        "observation_count": 0,
                        "first_seen": created_at,
                        "last_seen": created_at,
                        "model_confidence_sum": 0.0,
                        "evidence_count": 0,
                        "source_method_counts": {},
                    }
                    edge_acc[edge_key] = current

                current["observation_count"] += 1
                current["evidence_count"] += 1
                current["model_confidence_sum"] += confidence
                if created_at:
                    if not current.get("first_seen") or str(created_at) < str(current.get("first_seen")):
                        current["first_seen"] = created_at
                    if not current.get("last_seen") or str(created_at) > str(current.get("last_seen")):
                        current["last_seen"] = created_at
                method_counts = current["source_method_counts"]
                method_counts[method] = int(method_counts.get(method, 0)) + 1

        edges: List[Dict[str, Any]] = []
        for edge in edge_acc.values():
            evidence_count = int(edge.get("evidence_count") or 0)
            avg_conf = (float(edge.get("model_confidence_sum") or 0.0) / evidence_count) if evidence_count > 0 else 0.0
            method_counts = edge.get("source_method_counts") or {}
            dominant_method = max(method_counts.items(), key=lambda kv: kv[1])[0] if method_counts else "topology_viterbi"
            edges.append(
                {
                    "from_prefix": edge["from_prefix"],
                    "to_prefix": edge["to_prefix"],
                    "from_public_key": None,
                    "to_public_key": None,
                    "observation_count": int(edge.get("observation_count") or 0),
                    "first_seen": edge.get("first_seen"),
                    "last_seen": edge.get("last_seen"),
                    "avg_hop_position": None,
                    "geographic_distance": None,
                    "model_confidence": round(avg_conf, 4),
                    "evidence_count": evidence_count,
                    "source_method": dominant_method,
                }
            )

        # Resolve node metadata (name/location/last seen) from contact tracking when possible.
        nodes: List[Dict[str, Any]] = []
        if node_prefixes:
            try:
                conn = sqlite3.connect(self.db.db_path, timeout=60)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                for prefix in sorted(node_prefixes):
                    if not prefix:
                        continue
                    cursor.execute(
                        """
                        SELECT public_key, name, latitude, longitude, role, is_starred, last_heard, last_advert_timestamp
                        FROM complete_contact_tracking
                        WHERE public_key LIKE ?
                          AND role IN ('repeater', 'roomserver')
                        ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC, is_starred DESC
                        LIMIT 1
                        """,
                        (f"{prefix}%",),
                    )
                    row = cursor.fetchone()
                    if not row:
                        continue
                    if row["latitude"] in (None, 0) or row["longitude"] in (None, 0):
                        continue
                    nodes.append(
                        {
                            "public_key": row["public_key"],
                            "prefix": prefix.lower(),
                            "name": row["name"] or f"Node {prefix.upper()}",
                            "latitude": float(row["latitude"]),
                            "longitude": float(row["longitude"]),
                            "role": row["role"] or "repeater",
                            "is_starred": bool(row["is_starred"]),
                            "last_heard": row["last_heard"],
                            "last_advert_timestamp": row["last_advert_timestamp"],
                        }
                    )
                conn.close()
            except Exception as e:
                self.logger.debug(f"Topology model graph node hydration failed: {e}")

        return {
            "nodes": nodes,
            "edges": edges,
            "prefix_hex_chars": max(2, prefix_hex_chars),
            "days": days,
            "min_confidence": min_confidence,
            "include_ghost": include_ghost,
        }

    def run_confidence_backfill(
        self,
        days: int = 7,
        limit: Optional[int] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Replay observed paths and persist model-vs-legacy confidence rows."""
        days = max(1, min(90, int(days or 7)))
        limit_clause = ""
        params: Tuple[Any, ...]
        if limit is not None:
            safe_limit = max(1, min(500000, int(limit)))
            limit_clause = " LIMIT ?"
            params = (f"-{days} days", safe_limit)
        else:
            params = (f"-{days} days",)

        count_query = """
            SELECT COUNT(*) AS total_rows
            FROM observed_paths
            WHERE last_seen >= datetime('now', ?)
        """
        total_rows = int((self.db.execute_query(count_query, (f"-{days} days",)) or [{"total_rows": 0}])[0].get("total_rows", 0))
        if limit is not None:
            total_rows = min(total_rows, max(1, min(500000, int(limit))))

        select_query = f"""
            SELECT path_hex, path_length, bytes_per_hop, packet_hash, observation_count, last_seen
            FROM observed_paths
            WHERE last_seen >= datetime('now', ?)
            ORDER BY last_seen ASC
            {limit_clause}
        """
        rows = self.db.execute_query(select_query, params)

        processed = 0
        skipped = 0
        errors = 0
        comparisons_written = 0
        start_ts = datetime.now().isoformat()

        if progress_callback:
            progress_callback(
                {
                    "status": "running",
                    "days": days,
                    "total": total_rows,
                    "processed": 0,
                    "rows_skipped": 0,
                    "errors": 0,
                    "comparisons_written": 0,
                    "started_at": start_ts,
                }
            )

        # Force full replay coverage independent of runtime shadow sampling.
        original_sample_rate = self.shadow_sample_rate
        self.shadow_sample_rate = 1.0
        try:
            for row in rows:
                processed += 1
                try:
                    path_nodes = self._parse_observed_path_nodes(row)
                    if len(path_nodes) < 2:
                        skipped += 1
                        continue

                    wrote_for_row = False
                    for current_index, node_id in enumerate(path_nodes):
                        repeaters = self._query_candidates_for_prefix(node_id)
                        if not repeaters:
                            continue

                        legacy_choice, legacy_confidence, legacy_method, non_collision = self._legacy_select_candidate(
                            path_context=path_nodes,
                            repeaters=repeaters,
                            current_index=current_index,
                        )
                        model_choice, model_confidence, model_method = self.resolve_path_candidates(
                            node_id=node_id,
                            path_context=path_nodes,
                            repeaters=repeaters,
                            current_index=current_index,
                        )

                        # If both methods fail to pick anything, skip this node.
                        if not legacy_choice and not model_choice:
                            continue

                        backfill_key = self._build_backfill_key(
                            path_nodes=path_nodes,
                            packet_hash=row.get("packet_hash"),
                            current_index=current_index,
                        )
                        wrote = self.record_shadow_comparison(
                            path_nodes=path_nodes,
                            model_choice=model_choice,
                            model_confidence=model_confidence,
                            model_method=model_method,
                            legacy_choice=legacy_choice,
                            legacy_confidence=legacy_confidence,
                            legacy_method=legacy_method,
                            packet_hash=row.get("packet_hash"),
                            bytes_per_hop=row.get("bytes_per_hop"),
                            non_collision=non_collision,
                            backfill_key=backfill_key,
                        )
                        if wrote:
                            comparisons_written += 1
                            wrote_for_row = True

                    if not wrote_for_row:
                        skipped += 1
                except Exception:
                    errors += 1

                if progress_callback and processed % 100 == 0:
                    progress_callback(
                        {
                            "status": "running",
                            "days": days,
                            "total": total_rows,
                            "processed": processed,
                            "rows_skipped": skipped,
                            "errors": errors,
                            "comparisons_written": comparisons_written,
                            "started_at": start_ts,
                        }
                    )
        finally:
            self.shadow_sample_rate = original_sample_rate

        result = {
            "status": "completed",
            "days": days,
            "total": total_rows,
            "processed": processed,
            "rows_skipped": skipped,
            "errors": errors,
            "comparisons_written": comparisons_written,
            "started_at": start_ts,
            "completed_at": datetime.now().isoformat(),
        }
        if progress_callback:
            progress_callback(result)
        return result

    def _build_backfill_key(
        self,
        path_nodes: List[str],
        packet_hash: Optional[str],
        current_index: int,
    ) -> str:
        path_hex = "".join(path_nodes).lower()
        packet_component = (packet_hash or "nohash").lower()
        return f"obs_replay:{packet_component}:{path_hex}:{current_index}"

    def _parse_observed_path_nodes(self, row: Dict[str, Any]) -> List[str]:
        path_hex = str(row.get("path_hex") or "").strip().lower()
        if not path_hex:
            return []
        bytes_per_hop = row.get("bytes_per_hop")
        path_length = row.get("path_length")

        hop_hex_chars = 0
        if bytes_per_hop:
            try:
                hop_hex_chars = int(bytes_per_hop) * 2
            except (TypeError, ValueError):
                hop_hex_chars = 0
        if hop_hex_chars <= 0 and path_length:
            try:
                path_length = int(path_length)
                if path_length > 0 and len(path_hex) % path_length == 0:
                    hop_hex_chars = len(path_hex) // path_length
            except (TypeError, ValueError):
                hop_hex_chars = 0
        if hop_hex_chars <= 0:
            hop_hex_chars = self.prefix_hex_chars
        hop_hex_chars = max(2, hop_hex_chars)

        if len(path_hex) < hop_hex_chars:
            return []
        if len(path_hex) % hop_hex_chars != 0:
            # Fallback for malformed rows: parse by default configured prefix width.
            hop_hex_chars = self.prefix_hex_chars
            if hop_hex_chars <= 0 or len(path_hex) % hop_hex_chars != 0:
                return []
        return [path_hex[i : i + hop_hex_chars] for i in range(0, len(path_hex), hop_hex_chars)]

    def _legacy_select_candidate(
        self,
        path_context: List[str],
        repeaters: List[Dict[str, Any]],
        current_index: int,
    ) -> Tuple[Optional[Dict[str, Any]], float, str, bool]:
        if not repeaters:
            return None, 0.0, "legacy_none", False
        if len(repeaters) == 1:
            return repeaters[0], 1.0, "legacy_single", True

        # Legacy fallback prioritizes recency for colliding prefixes.
        selected = max(repeaters, key=lambda r: self._recency_score(r))
        confidence = max(0.0, min(1.0, self._recency_score(selected)))

        # If graph context exists, nudge confidence upward when adjacent edges are observed.
        mesh_graph = getattr(self.bot, "mesh_graph", None)
        if mesh_graph and current_index > 0:
            prev_prefix = (path_context[current_index - 1] or "").lower()[: self.prefix_hex_chars]
            cur_prefix = (selected.get("public_key") or selected.get("prefix") or "").lower()[: self.prefix_hex_chars]
            graph_score = mesh_graph.get_candidate_score(
                candidate_prefix=cur_prefix,
                prev_prefix=prev_prefix,
                next_prefix=None,
                min_observations=self.min_edge_observations,
                hop_position=None,
                use_bidirectional=True,
                use_hop_position=False,
            )
            confidence = max(confidence, max(0.0, min(1.0, float(graph_score or 0.0))))

        return selected, confidence, "legacy_recency_graph", False

    def _normalize_repeaters(self, repeaters: List[Dict[str, Any]], node_id: str) -> List[Dict[str, Any]]:
        node_prefix = (node_id or "").lower()
        states: List[Dict[str, Any]] = []
        for r in repeaters:
            pk = (r.get("public_key") or "").lower()
            if not pk:
                continue
            if node_prefix and not pk.startswith(node_prefix):
                continue
            states.append(
                {
                    "public_key": pk,
                    "prefix": pk[: max(len(node_prefix), self.prefix_hex_chars)],
                    "name": r.get("name"),
                    "last_heard": r.get("last_heard") or r.get("last_seen"),
                    "last_advert_timestamp": r.get("last_advert_timestamp"),
                    "hop_count": r.get("hop_count"),
                    "is_starred": bool(r.get("is_starred", False)),
                    "snr": r.get("snr"),
                    "latitude": r.get("latitude"),
                    "longitude": r.get("longitude"),
                }
            )
        return states[: self.max_candidates_per_prefix]

    def _query_candidates_for_prefix(self, prefix: str) -> List[Dict[str, Any]]:
        prefix = (prefix or "").lower()
        if not prefix:
            return []
        results = self.db.execute_query(
            """
            SELECT public_key, name, last_heard, last_advert_timestamp, hop_count, is_starred, snr, latitude, longitude
            FROM complete_contact_tracking
            WHERE public_key LIKE ?
              AND role IN ('repeater', 'roomserver')
            ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
            LIMIT ?
            """,
            (f"{prefix}%", self.max_candidates_per_prefix),
        )
        states: List[Dict[str, Any]] = []
        for row in results:
            pk = (row.get("public_key") or "").lower()
            if not pk:
                continue
            states.append(
                {
                    "public_key": pk,
                    "prefix": pk[: max(len(prefix), self.prefix_hex_chars)],
                    "name": row.get("name"),
                    "last_heard": row.get("last_heard"),
                    "last_advert_timestamp": row.get("last_advert_timestamp"),
                    "hop_count": row.get("hop_count"),
                    "is_starred": bool(row.get("is_starred", 0)),
                    "snr": row.get("snr"),
                    "latitude": row.get("latitude"),
                    "longitude": row.get("longitude"),
                }
            )
        return states

    def _viterbi_decode(
        self,
        state_sets: List[List[Dict[str, Any]]],
        path_context: List[str],
    ) -> Tuple[List[Dict[str, Any]], float]:
        """Compute best state path with log-space Viterbi."""
        if not state_sets:
            return [], 0.0

        epsilon = 1e-6
        dp: List[List[float]] = []
        parent: List[List[int]] = []

        first_scores = []
        first_parent = []
        for st in state_sets[0]:
            emission = max(epsilon, self._emission_score(st))
            first_scores.append(math.log(emission))
            first_parent.append(-1)
        dp.append(first_scores)
        parent.append(first_parent)

        for i in range(1, len(state_sets)):
            cur_scores = []
            cur_parent = []
            prev_states = state_sets[i - 1]
            cur_states = state_sets[i]
            for cur_idx, cur_st in enumerate(cur_states):
                emission = max(epsilon, self._emission_score(cur_st))
                best_prev_idx = -1
                best_val = -1e18
                for prev_idx, prev_st in enumerate(prev_states):
                    transition = max(
                        epsilon,
                        self._transition_score(prev_st, cur_st, path_context[i - 1], path_context[i]),
                    )
                    val = dp[i - 1][prev_idx] + math.log(transition) + math.log(emission)
                    if val > best_val:
                        best_val = val
                        best_prev_idx = prev_idx
                cur_scores.append(best_val)
                cur_parent.append(best_prev_idx)
            dp.append(cur_scores)
            parent.append(cur_parent)

        if not dp[-1]:
            return [], 0.0

        best_last_idx = max(range(len(dp[-1])), key=lambda idx: dp[-1][idx])
        best_log_score = dp[-1][best_last_idx]
        sequence: List[Dict[str, Any]] = [state_sets[-1][best_last_idx]]
        cursor = best_last_idx
        for i in range(len(state_sets) - 1, 0, -1):
            cursor = parent[i][cursor]
            if cursor < 0:
                break
            sequence.append(state_sets[i - 1][cursor])
        sequence.reverse()
        if len(sequence) != len(state_sets):
            return [], 0.0
        return sequence, best_log_score

    def _emission_score(self, state: Dict[str, Any]) -> float:
        if state.get("is_ghost"):
            return 0.25
        recency = self._recency_score(state)
        zero_hop = 0.12 if state.get("hop_count") == 0 else 0.0
        snr_bonus = 0.08 if state.get("snr") is not None else 0.0
        starred = 0.06 if state.get("is_starred") else 0.0
        return max(0.01, min(0.99, 0.35 + 0.45 * recency + zero_hop + snr_bonus + starred))

    def _transition_score(
        self,
        prev_state: Dict[str, Any],
        cur_state: Dict[str, Any],
        prev_node: str,
        cur_node: str,
    ) -> float:
        prev_is_ghost = prev_state.get("is_ghost")
        cur_is_ghost = cur_state.get("is_ghost")
        if prev_is_ghost and cur_is_ghost:
            return 0.25
        if prev_is_ghost or cur_is_ghost:
            return 0.4

        mesh_graph = getattr(self.bot, "mesh_graph", None)
        if not mesh_graph:
            return 0.5

        prev_prefix = (prev_state.get("public_key") or prev_node or "").lower()[: self.prefix_hex_chars]
        cur_prefix = (cur_state.get("public_key") or cur_node or "").lower()[: self.prefix_hex_chars]
        graph_score = mesh_graph.get_candidate_score(
            candidate_prefix=cur_prefix,
            prev_prefix=prev_prefix,
            next_prefix=None,
            min_observations=self.min_edge_observations,
            hop_position=None,
            use_bidirectional=True,
            use_hop_position=False,
        )
        return max(0.01, min(0.99, 0.2 + 0.75 * graph_score))

    def _recency_score(self, state: Dict[str, Any]) -> float:
        recent = state.get("last_advert_timestamp") or state.get("last_heard")
        if not recent:
            return 0.0
        try:
            if isinstance(recent, str):
                dt = datetime.fromisoformat(recent.replace("Z", "+00:00"))
            else:
                dt = recent
            age_hours = max(0.0, (datetime.now(dt.tzinfo) - dt).total_seconds() / 3600.0) if getattr(dt, "tzinfo", None) else max(0.0, (datetime.now() - dt).total_seconds() / 3600.0)
            return math.exp(-age_hours / 12.0)
        except Exception:
            return 0.0

    def _score_to_confidence(self, log_score: float, path_length: Optional[int] = None) -> float:
        """Convert log probability into bounded confidence with path-length normalization."""
        steps = max(1, int(path_length or 1))
        # Normalize by number of transitions so confidence remains comparable across path lengths.
        normalized_score = log_score / float(steps)
        return max(0.0, min(1.0, 1.0 / (1.0 + math.exp(-2.0 * normalized_score))))

    def _ghost_state(self, prefix: str) -> Dict[str, Any]:
        return {
            "is_ghost": True,
            "public_key": f"ghost:{prefix.lower()}",
            "prefix": prefix.lower(),
            "name": "Ghost Node",
        }

    def _record_ghost_node(self, ghost_state: Dict[str, Any], path_context: List[str]) -> None:
        if not self.ghost_enabled:
            return
        prefix = (ghost_state.get("prefix") or "").lower()
        if not prefix:
            return
        ghost_id = f"ghost_{prefix}"
        neighbors = {
            "path_context": path_context,
            "previous": path_context[:-1],
            "next": path_context[1:],
        }
        try:
            existing = self.db.execute_query(
                "SELECT id, evidence_count FROM topology_ghost_nodes WHERE ghost_id = ? LIMIT 1",
                (ghost_id,),
            )
            if existing:
                evidence_count = int(existing[0].get("evidence_count", 1)) + 1
                tier = self._ghost_tier(evidence_count)
                self.db.execute_update(
                    """
                    UPDATE topology_ghost_nodes
                    SET inferred_neighbors_json = ?, evidence_count = ?, confidence_tier = ?, model_confidence = ?, last_seen = CURRENT_TIMESTAMP
                    WHERE ghost_id = ?
                    """,
                    (json.dumps(neighbors), evidence_count, tier, min(0.99, 0.2 + evidence_count * 0.05), ghost_id),
                )
            else:
                self.db.execute_update(
                    """
                    INSERT INTO topology_ghost_nodes
                    (ghost_id, prefix, inferred_neighbors_json, evidence_count, confidence_tier, model_confidence)
                    VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (ghost_id, prefix, json.dumps(neighbors), self._ghost_tier(1), 0.25),
                )
        except Exception as e:
            self.logger.debug(f"Topology ghost write failed: {e}")

    def _ghost_tier(self, evidence_count: int) -> str:
        if evidence_count >= 40:
            return "confirmed"
        if evidence_count >= 20:
            return "likely"
        if evidence_count >= 8:
            return "possible"
        return "noise"

    @staticmethod
    def _rolling_avg(previous_avg: float, new_value: float, n: int) -> float:
        if n <= 1:
            return float(new_value)
        return ((previous_avg * (n - 1)) + new_value) / float(n)
