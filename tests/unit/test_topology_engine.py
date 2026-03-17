#!/usr/bin/env python3
"""
Unit tests for probabilistic TopologyEngine.
"""

import json
from datetime import datetime, timedelta

import pytest

from modules.topology_engine import TopologyEngine
from tests.helpers import create_test_repeater


@pytest.mark.unit
def test_topology_engine_prefers_recent_candidate(mock_bot, populated_mesh_graph):
    mock_bot.mesh_graph = populated_mesh_graph
    mock_bot.config.set("Path_Command", "topology_engine_mode", "shadow")
    mock_bot.config.set("Path_Command", "topology_ghost_enabled", "true")
    mock_bot.config.set("Path_Command", "topology_ghost_min_confidence", "0.0")

    engine = TopologyEngine(mock_bot)
    old_dt = datetime.now() - timedelta(days=14)
    new_dt = datetime.now() - timedelta(hours=1)

    candidate_old = create_test_repeater("7e", "Old", public_key="7e" * 32, last_heard=old_dt, last_advert_timestamp=old_dt)
    candidate_new = create_test_repeater("7e", "New", public_key=("7e" * 31) + "7f", last_heard=new_dt, last_advert_timestamp=new_dt)

    chosen, confidence, method = engine.resolve_path_candidates(
        node_id="7e",
        path_context=["01", "7e", "86"],
        repeaters=[candidate_old, candidate_new],
        current_index=1,
    )

    assert chosen is not None
    assert chosen["public_key"] == candidate_new["public_key"]
    assert confidence > 0.0
    assert method in ("topology_viterbi", "topology_viterbi_ghost")


@pytest.mark.unit
def test_topology_engine_records_shadow_metrics(mock_bot, populated_mesh_graph):
    mock_bot.mesh_graph = populated_mesh_graph
    mock_bot.config.set("Path_Command", "topology_engine_mode", "shadow")
    mock_bot.config.set("Path_Command", "topology_shadow_sample_rate", "1.0")
    engine = TopologyEngine(mock_bot)

    # Ensure supplemental tables exist for direct engine writes in this unit test.
    mock_bot.db_manager.create_table(
        "topology_inference_shadow",
        """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path_hex TEXT NOT NULL,
        path_nodes_json TEXT NOT NULL,
        resolved_path_json TEXT NOT NULL,
        method TEXT NOT NULL,
        model_confidence REAL NOT NULL,
        legacy_method TEXT,
        legacy_confidence REAL,
        agreement INTEGER DEFAULT 0,
        packet_hash TEXT,
        bytes_per_hop INTEGER,
        backfill_key TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        """,
    )
    mock_bot.db_manager.create_table(
        "topology_model_metrics",
        """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        metric_date DATE NOT NULL,
        total_comparisons INTEGER DEFAULT 0,
        agreement_count INTEGER DEFAULT 0,
        disagreement_count INTEGER DEFAULT 0,
        non_collision_comparisons INTEGER DEFAULT 0,
        non_collision_agreement_count INTEGER DEFAULT 0,
        avg_legacy_confidence REAL,
        avg_model_confidence REAL,
        metadata_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(metric_date)
        """,
    )

    legacy_choice = {"public_key": "7e" * 32, "name": "Legacy"}
    model_choice = {"public_key": "7e" * 32, "name": "Model"}
    engine.record_shadow_comparison(
        path_nodes=["01", "7e", "86"],
        model_choice=model_choice,
        model_confidence=0.81,
        model_method="topology_viterbi",
        legacy_choice=legacy_choice,
        legacy_confidence=0.74,
        legacy_method="graph",
        non_collision=True,
    )

    rows = mock_bot.db_manager.execute_query("SELECT COUNT(*) AS n FROM topology_inference_shadow")
    assert rows[0]["n"] == 1
    metric_rows = mock_bot.db_manager.execute_query("SELECT total_comparisons, agreement_count FROM topology_model_metrics")
    assert metric_rows
    assert metric_rows[0]["total_comparisons"] >= 1
    assert metric_rows[0]["agreement_count"] >= 1


@pytest.mark.unit
def test_topology_engine_backfill_replays_observed_paths(mock_bot, populated_mesh_graph):
    mock_bot.mesh_graph = populated_mesh_graph
    mock_bot.config.set("Path_Command", "topology_engine_mode", "shadow")
    mock_bot.config.set("Path_Command", "topology_shadow_sample_rate", "0.0")
    mock_bot.config.set("Path_Command", "topology_ghost_enabled", "false")
    engine = TopologyEngine(mock_bot)

    mock_bot.db_manager.create_table(
        "observed_paths",
        """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_key TEXT,
        packet_hash TEXT,
        from_prefix TEXT NOT NULL,
        to_prefix TEXT NOT NULL,
        path_hex TEXT NOT NULL,
        path_length INTEGER NOT NULL,
        bytes_per_hop INTEGER,
        packet_type TEXT NOT NULL,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        observation_count INTEGER DEFAULT 1
        """,
    )
    mock_bot.db_manager.create_table(
        "topology_inference_shadow",
        """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path_hex TEXT NOT NULL,
        path_nodes_json TEXT NOT NULL,
        resolved_path_json TEXT NOT NULL,
        method TEXT NOT NULL,
        model_confidence REAL NOT NULL,
        legacy_method TEXT,
        legacy_confidence REAL,
        agreement INTEGER DEFAULT 0,
        packet_hash TEXT,
        bytes_per_hop INTEGER,
        backfill_key TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        """,
    )
    mock_bot.db_manager.create_table(
        "topology_model_metrics",
        """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        metric_date DATE NOT NULL,
        total_comparisons INTEGER DEFAULT 0,
        agreement_count INTEGER DEFAULT 0,
        disagreement_count INTEGER DEFAULT 0,
        non_collision_comparisons INTEGER DEFAULT 0,
        non_collision_agreement_count INTEGER DEFAULT 0,
        avg_legacy_confidence REAL,
        avg_model_confidence REAL,
        metadata_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(metric_date)
        """,
    )

    # Candidate nodes for a simple 3-hop path (with prefix collision on 7e).
    mock_bot.db_manager.execute_update(
        """
        INSERT INTO complete_contact_tracking
        (public_key, name, role, last_heard, last_advert_timestamp, latitude, longitude, is_starred)
        VALUES (?, ?, 'repeater', ?, ?, ?, ?, ?)
        """,
        ("01" * 32, "Node 01", datetime.now().isoformat(), datetime.now().isoformat(), 47.60, -122.33, 0),
    )
    mock_bot.db_manager.execute_update(
        """
        INSERT INTO complete_contact_tracking
        (public_key, name, role, last_heard, last_advert_timestamp, latitude, longitude, is_starred)
        VALUES (?, ?, 'repeater', ?, ?, ?, ?, ?)
        """,
        ("7e" * 32, "Node 7e A", datetime.now().isoformat(), datetime.now().isoformat(), 47.61, -122.30, 0),
    )
    mock_bot.db_manager.execute_update(
        """
        INSERT INTO complete_contact_tracking
        (public_key, name, role, last_heard, last_advert_timestamp, latitude, longitude, is_starred)
        VALUES (?, ?, 'repeater', ?, ?, ?, ?, ?)
        """,
        ("7e" + ("7f" * 31), "Node 7e B", datetime.now().isoformat(), datetime.now().isoformat(), 47.62, -122.29, 1),
    )
    mock_bot.db_manager.execute_update(
        """
        INSERT INTO complete_contact_tracking
        (public_key, name, role, last_heard, last_advert_timestamp, latitude, longitude, is_starred)
        VALUES (?, ?, 'repeater', ?, ?, ?, ?, ?)
        """,
        ("86" * 32, "Node 86", datetime.now().isoformat(), datetime.now().isoformat(), 47.63, -122.28, 0),
    )

    mock_bot.db_manager.execute_update(
        """
        INSERT INTO observed_paths
        (public_key, packet_hash, from_prefix, to_prefix, path_hex, path_length, bytes_per_hop, packet_type, first_seen, last_seen, observation_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None,
            "pkt-backfill-1",
            "01",
            "86",
            "017e86",
            3,
            1,
            "advert",
            datetime.now().isoformat(),
            datetime.now().isoformat(),
            1,
        ),
    )

    progress_events = []
    result = engine.run_confidence_backfill(days=30, progress_callback=lambda update: progress_events.append(update))

    assert result["status"] == "completed"
    assert result["processed"] >= 1
    assert result["comparisons_written"] >= 1
    assert progress_events
    shadow_rows = mock_bot.db_manager.execute_query("SELECT COUNT(*) AS n FROM topology_inference_shadow WHERE method != 'shadow_ingest'")
    assert shadow_rows[0]["n"] >= 1

    first_count = shadow_rows[0]["n"]
    second_run = engine.run_confidence_backfill(days=30)
    shadow_rows_after = mock_bot.db_manager.execute_query("SELECT COUNT(*) AS n FROM topology_inference_shadow WHERE method != 'shadow_ingest'")
    assert second_run["comparisons_written"] == 0
    assert shadow_rows_after[0]["n"] == first_count


@pytest.mark.unit
def test_topology_engine_select_for_hop_returns_stable_shape(mock_bot, populated_mesh_graph):
    mock_bot.mesh_graph = populated_mesh_graph
    engine = TopologyEngine(mock_bot)
    candidate = create_test_repeater("7e", "Candidate", public_key="7e" * 32)
    result = engine.select_for_hop(repeaters=[candidate], node_id="7e", path_context=["01", "7e", "86"])
    assert "repeater" in result
    assert "confidence" in result
    assert "method" in result
    assert "is_topology_guess" in result
    assert isinstance(result["is_topology_guess"], bool)


@pytest.mark.unit
def test_topology_engine_maybe_record_shadow_comparison_gated_by_mode(mock_bot, populated_mesh_graph):
    mock_bot.mesh_graph = populated_mesh_graph
    mock_bot.config.set("Path_Command", "topology_shadow_sample_rate", "1.0")
    engine = TopologyEngine(mock_bot)
    mock_bot.db_manager.create_table(
        "topology_inference_shadow",
        """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path_hex TEXT NOT NULL,
        path_nodes_json TEXT NOT NULL,
        resolved_path_json TEXT NOT NULL,
        method TEXT NOT NULL,
        model_confidence REAL NOT NULL,
        legacy_method TEXT,
        legacy_confidence REAL,
        agreement INTEGER DEFAULT 0,
        packet_hash TEXT,
        bytes_per_hop INTEGER,
        backfill_key TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        """,
    )
    mock_bot.db_manager.create_table(
        "topology_model_metrics",
        """
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        metric_date DATE NOT NULL,
        total_comparisons INTEGER DEFAULT 0,
        agreement_count INTEGER DEFAULT 0,
        disagreement_count INTEGER DEFAULT 0,
        non_collision_comparisons INTEGER DEFAULT 0,
        non_collision_agreement_count INTEGER DEFAULT 0,
        avg_legacy_confidence REAL,
        avg_model_confidence REAL,
        metadata_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(metric_date)
        """,
    )
    model_choice = create_test_repeater("7e", "Model", public_key="7e" * 32)
    legacy_choice = create_test_repeater("7e", "Legacy", public_key=("7e" * 31) + "7f")

    wrote_legacy_mode = engine.maybe_record_shadow_comparison(
        topology_mode="legacy",
        path_nodes=["01", "7e", "86"],
        model_result={"repeater": model_choice, "confidence": 0.9, "method": "topology_viterbi"},
        legacy_choice=legacy_choice,
        legacy_confidence=0.7,
        legacy_method="graph",
    )
    wrote_shadow_mode = engine.maybe_record_shadow_comparison(
        topology_mode="shadow",
        path_nodes=["01", "7e", "86"],
        model_result={"repeater": model_choice, "confidence": 0.9, "method": "topology_viterbi"},
        legacy_choice=legacy_choice,
        legacy_confidence=0.7,
        legacy_method="graph",
    )
    rows = mock_bot.db_manager.execute_query("SELECT COUNT(*) AS n FROM topology_inference_shadow")
    assert wrote_legacy_mode is False
    assert wrote_shadow_mode is True
    assert rows[0]["n"] == 1


@pytest.mark.unit
def test_topology_engine_confidence_normalized_by_path_length(mock_bot):
    engine = TopologyEngine(mock_bot)
    # Same per-hop quality should yield similar confidence across different path lengths.
    short_conf = engine._score_to_confidence(log_score=-2.0, path_length=2)
    long_conf = engine._score_to_confidence(log_score=-6.0, path_length=6)
    assert abs(short_conf - long_conf) < 0.05


@pytest.mark.unit
def test_topology_engine_anchor_prior_not_applied_without_origin_context(mock_bot, populated_mesh_graph):
    mock_bot.mesh_graph = populated_mesh_graph
    mock_bot.config.set("Path_Command", "topology_advert_anchor_enabled", "true")
    engine = TopologyEngine(mock_bot)

    older_dt = datetime.now() - timedelta(days=10)
    newer_dt = datetime.now() - timedelta(minutes=10)
    candidate_old = create_test_repeater("7e", "Old", public_key="7e" * 32, last_heard=older_dt, last_advert_timestamp=older_dt)
    candidate_new = create_test_repeater("7e", "New", public_key=("7e" * 31) + "7f", last_heard=newer_dt, last_advert_timestamp=newer_dt)

    chosen, _, _ = engine.resolve_path_candidates(
        node_id="7e",
        path_context=["7e"],
        repeaters=[candidate_old, candidate_new],
        current_index=0,
        resolution_context=None,
    )
    debug = engine._consume_last_anchor_debug()

    assert chosen is not None
    assert chosen["public_key"] == candidate_new["public_key"]
    assert debug["applied"] is False
    assert debug["adjustment_total"] == 0.0


@pytest.mark.unit
def test_topology_engine_anchor_prior_applies_and_is_bounded(mock_bot, populated_mesh_graph):
    mock_bot.mesh_graph = populated_mesh_graph
    mock_bot.config.set("Path_Command", "topology_advert_anchor_enabled", "true")
    mock_bot.config.set("Path_Command", "topology_advert_anchor_weight", "1.0")
    mock_bot.config.set("Path_Command", "topology_advert_anchor_max_adjustment", "0.09")
    engine = TopologyEngine(mock_bot)

    recent_dt = datetime.now() - timedelta(minutes=5)
    origin_pk = "7e" * 32
    candidate_origin = create_test_repeater("7e", "Origin", public_key=origin_pk, last_heard=recent_dt, last_advert_timestamp=recent_dt)
    candidate_other = create_test_repeater("7e", "Other", public_key=("7e" * 31) + "7f", last_heard=recent_dt, last_advert_timestamp=recent_dt)

    chosen, _, _ = engine.resolve_path_candidates(
        node_id="7e",
        path_context=["7e"],
        repeaters=[candidate_origin, candidate_other],
        current_index=0,
        resolution_context={
            "origin_public_key": origin_pk,
            "origin_packet_type": "advert",
            "origin_location": None,
        },
    )
    debug = engine._consume_last_anchor_debug()

    assert chosen is not None
    assert debug["applied"] is True
    assert abs(debug["adjustment_total"]) <= 0.18 + 1e-9


@pytest.mark.unit
def test_topology_engine_anchor_prior_is_soft_not_hard_lock(mock_bot, populated_mesh_graph):
    mock_bot.mesh_graph = populated_mesh_graph
    mock_bot.config.set("Path_Command", "topology_advert_anchor_enabled", "true")
    mock_bot.config.set("Path_Command", "topology_advert_anchor_weight", "1.0")
    mock_bot.config.set("Path_Command", "topology_advert_anchor_max_adjustment", "0.09")
    engine = TopologyEngine(mock_bot)

    old_dt = datetime.now() - timedelta(days=12)
    new_dt = datetime.now() - timedelta(minutes=2)
    origin_pk = "7e" * 32
    candidate_origin = create_test_repeater("7e", "Origin", public_key=origin_pk, last_heard=old_dt, last_advert_timestamp=old_dt)
    candidate_new = create_test_repeater("7e", "Recent", public_key=("7e" * 31) + "7f", last_heard=new_dt, last_advert_timestamp=new_dt)

    chosen, _, _ = engine.resolve_path_candidates(
        node_id="7e",
        path_context=["7e"],
        repeaters=[candidate_origin, candidate_new],
        current_index=0,
        resolution_context={
            "origin_public_key": origin_pk,
            "origin_packet_type": "advert",
            "origin_location": None,
        },
    )

    assert chosen is not None
    assert chosen["public_key"] == candidate_new["public_key"]


@pytest.mark.unit
def test_topology_engine_anchor_context_is_advert_only(mock_bot):
    mock_bot.config.set("Path_Command", "topology_advert_anchor_enabled", "true")
    mock_bot.db_manager.execute_update(
        """
        INSERT INTO complete_contact_tracking
        (public_key, name, role, last_heard, last_advert_timestamp, latitude, longitude, is_starred)
        VALUES (?, ?, 'repeater', ?, ?, ?, ?, ?)
        """,
        (
            "01" * 32,
            "Origin",
            datetime.now().isoformat(),
            datetime.now().isoformat(),
            47.61,
            -122.33,
            0,
        ),
    )
    engine = TopologyEngine(mock_bot)

    advert_context = engine._build_resolution_context(origin_public_key="01" * 32, origin_packet_type="advert")
    message_context = engine._build_resolution_context(origin_public_key="01" * 32, origin_packet_type="message")

    assert advert_context is not None
    assert advert_context["origin_public_key"] == ("01" * 32)
    assert message_context is None


@pytest.mark.unit
def test_topology_engine_diagnostics_include_anchor_metrics(mock_bot):
    engine = TopologyEngine(mock_bot)
    rows = [
        {
            "method": "topology_viterbi",
            "agreement": 1,
            "resolved_path_json": json.dumps(
                {
                    "origin_packet_type": "advert",
                    "origin_public_key": "01" * 32,
                    "anchor_prior_applied": True,
                    "anchor_prior_adjustment": 0.04,
                }
            ),
        },
        {
            "method": "topology_viterbi_ghost",
            "agreement": 0,
            "resolved_path_json": json.dumps(
                {
                    "origin_packet_type": "message",
                    "origin_public_key": None,
                    "anchor_prior_applied": False,
                    "anchor_prior_adjustment": 0.0,
                }
            ),
        },
    ]
    diagnostics = engine._compute_anchor_diagnostics(rows)

    assert diagnostics["total_rows"] == 2
    assert diagnostics["anchored_rows"] == 1
    assert diagnostics["anchor_prior_applied_count"] == 1
    assert diagnostics["average_anchor_adjustment"] > 0.0
