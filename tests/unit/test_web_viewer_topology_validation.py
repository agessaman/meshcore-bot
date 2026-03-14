#!/usr/bin/env python3
"""
Tests for shadow-only topology validation web viewer page.
"""

from pathlib import Path

import pytest

from modules.web_viewer.app import BotDataViewer


def _write_config(path: Path, db_path: Path, mode: str) -> None:
    path.write_text(
        "\n".join(
            [
                "[Bot]",
                "bot_name = TestBot",
                f"db_path = {db_path}",
                "",
                "[Path_Command]",
                f"topology_engine_mode = {mode}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _build_viewer(tmp_path: Path, monkeypatch, mode: str) -> BotDataViewer:
    db_path = tmp_path / f"viewer_{mode}.db"
    config_path = tmp_path / f"viewer_{mode}.ini"
    _write_config(config_path, db_path, mode)

    monkeypatch.setattr(BotDataViewer, "_start_database_polling", lambda self: None)
    monkeypatch.setattr(BotDataViewer, "_start_cleanup_scheduler", lambda self: None)
    monkeypatch.setattr(BotDataViewer, "_setup_socketio_handlers", lambda self: None)

    viewer = BotDataViewer(config_path=str(config_path))
    viewer.app.testing = True
    return viewer


def _build_viewer_with_explicit_paths(tmp_path: Path, monkeypatch, config_path: Path) -> BotDataViewer:
    monkeypatch.setattr(BotDataViewer, "_start_database_polling", lambda self: None)
    monkeypatch.setattr(BotDataViewer, "_start_cleanup_scheduler", lambda self: None)
    monkeypatch.setattr(BotDataViewer, "_setup_socketio_handlers", lambda self: None)
    viewer = BotDataViewer(config_path=str(config_path))
    viewer.app.testing = True
    return viewer


@pytest.mark.unit
def test_topology_validation_route_shadow_gated(tmp_path, monkeypatch):
    legacy_viewer = _build_viewer(tmp_path, monkeypatch, "legacy")
    legacy_client = legacy_viewer.app.test_client()
    legacy_resp = legacy_client.get("/topology-validation")
    assert legacy_resp.status_code == 404

    shadow_viewer = _build_viewer(tmp_path, monkeypatch, "shadow")
    shadow_client = shadow_viewer.app.test_client()
    shadow_resp = shadow_client.get("/topology-validation")
    assert shadow_resp.status_code == 200
    assert b"Topology Validation (Shadow Mode)" in shadow_resp.data


@pytest.mark.unit
def test_topology_validation_nav_link_only_in_shadow(tmp_path, monkeypatch):
    legacy_viewer = _build_viewer(tmp_path, monkeypatch, "legacy")
    legacy_client = legacy_viewer.app.test_client()
    legacy_home = legacy_client.get("/")
    assert legacy_home.status_code == 200
    assert b"/topology-validation" not in legacy_home.data

    shadow_viewer = _build_viewer(tmp_path, monkeypatch, "shadow")
    shadow_client = shadow_viewer.app.test_client()
    shadow_home = shadow_client.get("/")
    assert shadow_home.status_code == 200
    assert b"/topology-validation" in shadow_home.data


@pytest.mark.unit
def test_topology_validation_api_data_flow(tmp_path, monkeypatch):
    viewer = _build_viewer(tmp_path, monkeypatch, "shadow")
    client = viewer.app.test_client()

    viewer.db_manager.execute_update(
        """
        INSERT INTO topology_inference_shadow
        (path_hex, path_nodes_json, resolved_path_json, method, model_confidence, legacy_method, legacy_confidence, agreement, packet_hash, bytes_per_hop)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "017e86",
            '["01","7e","86"]',
            '{"model_public_key":"7e"}',
            "topology_viterbi",
            0.82,
            "graph",
            0.74,
            1,
            "abc123",
            1,
        ),
    )
    viewer.db_manager.execute_update(
        """
        INSERT INTO topology_model_metrics
        (metric_date, total_comparisons, agreement_count, disagreement_count,
         non_collision_comparisons, non_collision_agreement_count, avg_legacy_confidence, avg_model_confidence, metadata_json)
        VALUES (date('now'), 10, 8, 2, 6, 5, 0.7, 0.8, '{}')
        """,
        (),
    )
    viewer.db_manager.execute_update(
        """
        INSERT INTO topology_ghost_nodes
        (ghost_id, prefix, inferred_neighbors_json, evidence_count, confidence_tier, model_confidence)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("ghost_7e", "7e", '{"path_context":["01","7e","86"]}', 12, "possible", 0.44),
    )

    shadow_resp = client.get("/api/mesh/topology-shadow?days=7")
    assert shadow_resp.status_code == 200
    shadow_json = shadow_resp.get_json()
    assert "comparisons" in shadow_json
    assert len(shadow_json["comparisons"]) >= 1

    metrics_resp = client.get("/api/mesh/topology-metrics?days=7")
    assert metrics_resp.status_code == 200
    metrics_json = metrics_resp.get_json()
    assert "metrics" in metrics_json
    assert len(metrics_json["metrics"]) >= 1

    ghost_resp = client.get("/api/mesh/topology-ghosts?limit=10")
    assert ghost_resp.status_code == 200
    ghost_json = ghost_resp.get_json()
    assert "ghost_nodes" in ghost_json
    assert len(ghost_json["ghost_nodes"]) >= 1


@pytest.mark.unit
def test_topology_shadow_api_excludes_ingest_by_default(tmp_path, monkeypatch):
    viewer = _build_viewer(tmp_path, monkeypatch, "shadow")
    client = viewer.app.test_client()

    viewer.db_manager.execute_update(
        """
        INSERT INTO topology_inference_shadow
        (path_hex, path_nodes_json, resolved_path_json, method, model_confidence, legacy_method, legacy_confidence, agreement, packet_hash, bytes_per_hop)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("017e86", '["01","7e","86"]', "{}", "shadow_ingest", 0.0, None, 0.0, 0, "ing123", 1),
    )
    viewer.db_manager.execute_update(
        """
        INSERT INTO topology_inference_shadow
        (path_hex, path_nodes_json, resolved_path_json, method, model_confidence, legacy_method, legacy_confidence, agreement, packet_hash, bytes_per_hop)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("017e86", '["01","7e","86"]', '{"model_public_key":"7e"}', "topology_viterbi", 0.8, "graph", 0.7, 1, "cmp123", 1),
    )

    # Default should hide ingest rows.
    default_resp = client.get("/api/mesh/topology-shadow?days=7")
    assert default_resp.status_code == 200
    default_rows = default_resp.get_json()["comparisons"]
    assert all(r["method"] != "shadow_ingest" for r in default_rows)

    # Explicit include should show ingest rows.
    include_resp = client.get("/api/mesh/topology-shadow?days=7&include_ingest=1")
    assert include_resp.status_code == 200
    include_rows = include_resp.get_json()["comparisons"]
    assert any(r["method"] == "shadow_ingest" for r in include_rows)


@pytest.mark.unit
def test_topology_validation_respects_local_config_override(tmp_path, monkeypatch):
    # Main config says legacy.
    db_path = tmp_path / "viewer_local_override.db"
    config_path = tmp_path / "viewer_local_override.ini"
    _write_config(config_path, db_path, "legacy")

    # Build a local override at project-root-relative path used by viewer.
    # We monkeypatch bot_root to tmp_path via instance attribute after init path resolution behavior.
    local_dir = tmp_path / "local"
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "config.ini").write_text(
        "\n".join(
            [
                "[Path_Command]",
                "topology_engine_mode = shadow",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # Temporarily point bot_root resolution to tmp_path by monkeypatching class __init__ side effect.
    original_init = BotDataViewer.__init__

    def patched_init(self, db_path="meshcore_bot.db", repeater_db_path=None, config_path="config.ini"):
        original_init(self, db_path=db_path, repeater_db_path=repeater_db_path, config_path=config_path)
        self.bot_root = tmp_path
        self.config = self._load_config(config_path)

    monkeypatch.setattr(BotDataViewer, "__init__", patched_init)
    viewer = _build_viewer_with_explicit_paths(tmp_path, monkeypatch, config_path)
    client = viewer.app.test_client()
    resp = client.get("/topology-validation")
    assert resp.status_code == 200


@pytest.mark.unit
def test_mesh_page_shadow_toggle_visibility(tmp_path, monkeypatch):
    legacy_viewer = _build_viewer(tmp_path, monkeypatch, "legacy")
    legacy_client = legacy_viewer.app.test_client()
    legacy_mesh = legacy_client.get("/mesh")
    assert legacy_mesh.status_code == 200
    assert b"btn-data-mode-legacy" not in legacy_mesh.data

    shadow_viewer = _build_viewer(tmp_path, monkeypatch, "shadow")
    shadow_client = shadow_viewer.app.test_client()
    shadow_mesh = shadow_client.get("/mesh")
    assert shadow_mesh.status_code == 200
    assert b"btn-data-mode-legacy" in shadow_mesh.data
    assert b"btn-data-mode-new" in shadow_mesh.data
    assert b"btn-data-mode-overlay" in shadow_mesh.data


@pytest.mark.unit
def test_model_graph_api_mode_gating_and_payload_shape(tmp_path, monkeypatch):
    legacy_viewer = _build_viewer(tmp_path, monkeypatch, "legacy")
    legacy_client = legacy_viewer.app.test_client()
    legacy_resp = legacy_client.get("/api/mesh/model-graph")
    assert legacy_resp.status_code == 404

    shadow_viewer = _build_viewer(tmp_path, monkeypatch, "shadow")
    shadow_client = shadow_viewer.app.test_client()

    shadow_viewer.db_manager.execute_update(
        """
        INSERT INTO complete_contact_tracking
        (public_key, name, role, latitude, longitude, is_starred, last_heard, last_advert_timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "0101010101010101010101010101010101010101010101010101010101010101",
            "Node 01",
            "repeater",
            47.6062,
            -122.3321,
            0,
            "2026-03-10T00:00:00",
            "2026-03-10T00:00:00",
        ),
    )
    shadow_viewer.db_manager.execute_update(
        """
        INSERT INTO complete_contact_tracking
        (public_key, name, role, latitude, longitude, is_starred, last_heard, last_advert_timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e",
            "Node 7e",
            "repeater",
            47.6200,
            -122.3493,
            1,
            "2026-03-10T00:00:00",
            "2026-03-10T00:00:00",
        ),
    )
    shadow_viewer.db_manager.execute_update(
        """
        INSERT INTO topology_inference_shadow
        (path_hex, path_nodes_json, resolved_path_json, method, model_confidence, legacy_method, legacy_confidence, agreement, packet_hash, bytes_per_hop)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "017e",
            '["01","7e"]',
            '{"model_public_key":"7e"}',
            "topology_viterbi",
            0.83,
            "graph",
            0.71,
            1,
            "pkt-1",
            1,
        ),
    )

    shadow_resp = shadow_client.get("/api/mesh/model-graph?days=30")
    assert shadow_resp.status_code == 200
    payload = shadow_resp.get_json()
    assert "edges" in payload
    assert "nodes" in payload
    assert payload.get("topology_mode") == "shadow"
    assert payload.get("prefix_hex_chars") in (2, 4, 6)
    assert len(payload["edges"]) >= 1
    sample_edge = payload["edges"][0]
    assert "model_confidence" in sample_edge
    assert "evidence_count" in sample_edge
    assert "source_method" in sample_edge


@pytest.mark.unit
def test_topology_validation_has_backfill_controls_in_shadow(tmp_path, monkeypatch):
    shadow_viewer = _build_viewer(tmp_path, monkeypatch, "shadow")
    shadow_client = shadow_viewer.app.test_client()
    resp = shadow_client.get("/topology-validation")
    assert resp.status_code == 200
    assert b"backfill-start-btn" in resp.data
    assert b"backfill-days" in resp.data
    assert b"backfill-limit" in resp.data


@pytest.mark.unit
def test_topology_backfill_api_start_and_status(tmp_path, monkeypatch):
    viewer = _build_viewer(tmp_path, monkeypatch, "shadow")
    client = viewer.app.test_client()

    def fake_run_backfill(days=7, limit=None, progress_callback=None):
        if progress_callback:
            progress_callback(
                {
                    "status": "running",
                    "days": days,
                    "total": 10,
                    "processed": 5,
                    "rows_skipped": 1,
                    "errors": 0,
                    "comparisons_written": 4,
                    "started_at": "2026-03-13T00:00:00",
                }
            )
        return {
            "status": "completed",
            "days": days,
            "total": 10,
            "processed": 10,
            "rows_skipped": 1,
            "errors": 0,
            "comparisons_written": 9,
            "started_at": "2026-03-13T00:00:00",
            "completed_at": "2026-03-13T00:00:01",
        }

    monkeypatch.setattr(viewer.topology_engine, "run_confidence_backfill", fake_run_backfill)
    monkeypatch.setattr(
        viewer,
        "_start_topology_backfill_thread",
        lambda job_id, days, limit: viewer._run_topology_backfill_job(job_id, days, limit),
    )

    start_resp = client.post("/api/mesh/topology-backfill/start", json={"days": 14, "limit": 500})
    assert start_resp.status_code == 200
    start_json = start_resp.get_json()
    assert start_json["ok"] is True
    assert start_json["state"]["status"] == "completed"
    assert start_json["state"]["comparisons_written"] == 9

    status_resp = client.get("/api/mesh/topology-backfill/status")
    assert status_resp.status_code == 200
    status_json = status_resp.get_json()
    assert status_json["state"]["status"] == "completed"
    assert status_json["state"]["days"] == 14
    assert status_json["state"]["limit"] == 500


@pytest.mark.unit
def test_topology_backfill_api_rejects_concurrent_start(tmp_path, monkeypatch):
    viewer = _build_viewer(tmp_path, monkeypatch, "shadow")
    client = viewer.app.test_client()

    viewer._update_topology_backfill_state({"status": "running", "job_id": "running-job"})
    resp = client.post("/api/mesh/topology-backfill/start", json={"days": 7})
    assert resp.status_code == 409
    payload = resp.get_json()
    assert "already running" in payload["error"]
