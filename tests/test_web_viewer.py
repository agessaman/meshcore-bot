#!/usr/bin/env python3
"""Tests for modules/web_viewer/app.py — BotDataViewer Flask routes and API endpoints.

Uses Flask's built-in test client.  Background threads (database polling, log
tailing, cleanup scheduler) are patched to no-ops so the fixture is fast and
side-effect free.
"""

import configparser
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from modules.web_viewer.app import BotDataViewer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(path: Path, db_path: str) -> None:
    cfg = configparser.ConfigParser()
    cfg["Connection"] = {"connection_type": "serial", "serial_port": "/dev/ttyUSB0"}
    cfg["Bot"] = {"bot_name": "TestBot", "db_path": db_path, "prefix_bytes": "1"}
    cfg["Channels"] = {"monitor_channels": "general"}
    cfg["Path_Command"] = {
        "graph_capture_enabled": "false",
        "graph_write_strategy": "immediate",
    }
    with open(path, "w") as f:
        cfg.write(f)


def _fake_setup_logging(self: BotDataViewer) -> None:
    """Replace file-based logging with an in-memory logger for tests."""
    self.logger = logging.getLogger("test_web_viewer")
    self.logger.setLevel(logging.DEBUG)
    if not self.logger.handlers:
        self.logger.addHandler(logging.NullHandler())
    self.logger.propagate = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def viewer(tmp_path_factory):
    """Create a BotDataViewer with a real temp SQLite DB and Flask test client.

    Background threads are suppressed.  The fixture is module-scoped so the
    expensive DB initialisation only runs once per test module.
    """
    tmp = tmp_path_factory.mktemp("web_viewer")
    db_path = str(tmp / "test.db")
    config_path = str(tmp / "config.ini")
    _write_config(Path(config_path), db_path)

    with (
        patch.object(BotDataViewer, "_setup_logging", _fake_setup_logging),
        patch.object(BotDataViewer, "_start_database_polling", lambda self: None),
        patch.object(BotDataViewer, "_start_log_tailing", lambda self: None),
        patch.object(BotDataViewer, "_start_cleanup_scheduler", lambda self: None),
    ):
        v = BotDataViewer(db_path=db_path, config_path=config_path)

    v.app.config["TESTING"] = True
    v.app.config["WTF_CSRF_ENABLED"] = False
    yield v


@pytest.fixture
def client(viewer):
    """Flask test client with an application context."""
    with viewer.app.test_client() as c:
        yield c


@pytest.fixture
def auth_viewer(tmp_path_factory):
    """BotDataViewer with password authentication enabled."""
    tmp = tmp_path_factory.mktemp("web_viewer_auth")
    db_path = str(tmp / "test.db")
    config_path = str(tmp / "config.ini")

    cfg = configparser.ConfigParser()
    cfg["Connection"] = {"connection_type": "serial", "serial_port": "/dev/ttyUSB0"}
    cfg["Bot"] = {"bot_name": "TestBot", "db_path": db_path, "prefix_bytes": "1"}
    cfg["Web_Viewer"] = {"web_viewer_password": "secret123"}
    cfg["Path_Command"] = {
        "graph_capture_enabled": "false",
        "graph_write_strategy": "immediate",
    }
    with open(config_path, "w") as f:
        cfg.write(f)

    with (
        patch.object(BotDataViewer, "_setup_logging", _fake_setup_logging),
        patch.object(BotDataViewer, "_start_database_polling", lambda self: None),
        patch.object(BotDataViewer, "_start_log_tailing", lambda self: None),
        patch.object(BotDataViewer, "_start_cleanup_scheduler", lambda self: None),
    ):
        v = BotDataViewer(db_path=db_path, config_path=config_path)

    v.app.config["TESTING"] = True
    yield v


@pytest.fixture
def auth_client(auth_viewer):
    with auth_viewer.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Helper: insert a contact row so contact-related routes have data
# ---------------------------------------------------------------------------

def _insert_contact(viewer: BotDataViewer, public_key: str = "aabbccdd" * 8,
                    name: str = "TestNode") -> str:
    with closing(sqlite3.connect(viewer.db_path)) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO complete_contact_tracking
               (public_key, name, role, device_type, is_starred, is_currently_tracked)
               VALUES (?, ?, 'companion', 'device', 0, 1)""",
            (public_key, name),
        )
        conn.commit()
    return public_key


# ===========================================================================
# Page routes (HTML)
# ===========================================================================

class TestPageRoutes:

    def test_index(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_realtime(self, client):
        resp = client.get("/realtime")
        assert resp.status_code == 200

    def test_logs(self, client):
        resp = client.get("/logs")
        assert resp.status_code == 200

    def test_contacts(self, client):
        resp = client.get("/contacts")
        assert resp.status_code == 200

    def test_cache(self, client):
        resp = client.get("/cache")
        assert resp.status_code == 200

    def test_stats(self, client):
        resp = client.get("/stats")
        assert resp.status_code == 200

    def test_greeter(self, client):
        resp = client.get("/greeter")
        assert resp.status_code == 200

    def test_feeds(self, client):
        resp = client.get("/feeds")
        assert resp.status_code == 200

    def test_radio(self, client):
        resp = client.get("/radio")
        assert resp.status_code == 200

    def test_config(self, client):
        resp = client.get("/config")
        assert resp.status_code == 200

    def test_mesh(self, client):
        resp = client.get("/mesh")
        assert resp.status_code == 200


# ===========================================================================
# Health routes
# ===========================================================================

class TestHealthRoutes:

    def test_api_health_status(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "healthy"
        assert "connected_clients" in data
        assert "timestamp" in data
        assert data["version"] == "modern_2.0"

    def test_api_health_client_count(self, client):
        resp = client.get("/api/health")
        data = resp.get_json()
        assert isinstance(data["connected_clients"], int)
        assert data["connected_clients"] >= 0

    def test_api_system_health_returns_json(self, client):
        resp = client.get("/api/system-health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "status" in data or "error" in data


# ===========================================================================
# Radio routes
# ===========================================================================

class TestRadioRoutes:

    def test_radio_status_returns_json(self, client):
        resp = client.get("/api/radio/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "status_known" in data

    def test_radio_status_unknown_when_no_metadata(self, client, viewer):
        # Ensure key is absent
        viewer.db_manager.set_metadata("radio_connected", None) if hasattr(
            viewer.db_manager, "set_metadata"
        ) else None
        resp = client.get("/api/radio/status")
        assert resp.status_code == 200

    def test_radio_reboot_queues_operation(self, client):
        resp = client.post("/api/radio/reboot")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "operation_id" in data

    def test_radio_connect_action(self, client):
        resp = client.post(
            "/api/radio/connect",
            json={"action": "connect"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    def test_radio_disconnect_action(self, client):
        resp = client.post(
            "/api/radio/connect",
            json={"action": "disconnect"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    def test_radio_connect_invalid_action(self, client):
        resp = client.post(
            "/api/radio/connect",
            json={"action": "explode"},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_radio_connect_missing_action(self, client):
        resp = client.post(
            "/api/radio/connect",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400


# ===========================================================================
# Contact routes
# ===========================================================================

class TestContactRoutes:

    def test_api_contacts_default(self, client):
        resp = client.get("/api/contacts")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_api_contacts_since_7d(self, client):
        resp = client.get("/api/contacts?since=7d")
        assert resp.status_code == 200

    def test_api_contacts_since_all(self, client):
        resp = client.get("/api/contacts?since=all")
        assert resp.status_code == 200

    def test_api_contacts_invalid_since_uses_default(self, client):
        resp = client.get("/api/contacts?since=forever")
        assert resp.status_code == 200

    def test_toggle_star_missing_public_key(self, client):
        resp = client.post(
            "/api/toggle-star-contact",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_toggle_star_unknown_contact(self, client):
        resp = client.post(
            "/api/toggle-star-contact",
            json={"public_key": "0" * 64},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_toggle_star_known_contact(self, client, viewer):
        pk = _insert_contact(viewer, "1122334455667788" * 4, "StarNode")
        resp = client.post(
            "/api/toggle-star-contact",
            json={"public_key": pk},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "is_starred" in data

    def test_toggle_star_toggles_value(self, client, viewer):
        pk = _insert_contact(viewer, "aabbccdd11223344" * 4, "ToggleNode")
        # First call: star
        r1 = client.post("/api/toggle-star-contact", json={"public_key": pk},
                         content_type="application/json")
        starred = r1.get_json()["is_starred"]
        # Second call: unstar
        r2 = client.post("/api/toggle-star-contact", json={"public_key": pk},
                         content_type="application/json")
        unstarred = r2.get_json()["is_starred"]
        assert starred != unstarred

    def test_purge_preview_returns_json(self, client):
        resp = client.get("/api/contacts/purge-preview?days=30")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, (dict, list))

    def test_purge_contacts_post(self, client):
        resp = client.post(
            "/api/contacts/purge",
            json={"days": 365},
            content_type="application/json",
        )
        # Should return 200 (even if no contacts to purge)
        assert resp.status_code == 200


# ===========================================================================
# Export routes
# ===========================================================================

class TestExportRoutes:

    def test_export_contacts_json(self, client):
        resp = client.get("/api/export/contacts?format=json")
        assert resp.status_code == 200
        assert resp.content_type.startswith("application/json")

    def test_export_contacts_csv(self, client):
        resp = client.get("/api/export/contacts?format=csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.content_type
        assert b"user_id" in resp.data  # CSV header

    def test_export_contacts_since_7d(self, client):
        resp = client.get("/api/export/contacts?format=json&since=7d")
        assert resp.status_code == 200

    def test_export_contacts_default_format_is_json(self, client):
        resp = client.get("/api/export/contacts")
        assert resp.status_code == 200

    def test_export_paths_json(self, client):
        resp = client.get("/api/export/paths?format=json")
        assert resp.status_code == 200

    def test_export_paths_csv(self, client):
        resp = client.get("/api/export/paths?format=csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.content_type

    def test_export_paths_invalid_since_uses_default(self, client):
        resp = client.get("/api/export/paths?since=bogus")
        assert resp.status_code == 200


# ===========================================================================
# Decode path
# ===========================================================================

class TestDecodePathRoute:

    def test_missing_path_hex_returns_400(self, client):
        resp = client.post(
            "/api/decode-path",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_empty_path_hex_returns_400(self, client):
        resp = client.post(
            "/api/decode-path",
            json={"path_hex": ""},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_valid_path_hex_returns_200(self, client):
        resp = client.post(
            "/api/decode-path",
            json={"path_hex": "7e,01"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "path" in data

    def test_path_hex_with_bytes_per_hop(self, client):
        resp = client.post(
            "/api/decode-path",
            json={"path_hex": "7e01", "bytes_per_hop": 1},
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_invalid_bytes_per_hop_ignored(self, client):
        resp = client.post(
            "/api/decode-path",
            json={"path_hex": "7e", "bytes_per_hop": 99},
            content_type="application/json",
        )
        assert resp.status_code == 200


# ===========================================================================
# Database / cache / stats routes
# ===========================================================================

class TestDatabaseRoutes:

    def test_api_database_returns_json(self, client):
        resp = client.get("/api/database")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_api_optimize_database(self, client):
        resp = client.post("/api/optimize-database")
        assert resp.status_code == 200

    def test_api_cache_returns_json(self, client):
        resp = client.get("/api/cache")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_api_stats_returns_json(self, client):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_api_stats_with_window_params(self, client):
        resp = client.get("/api/stats?top_users_window=7d&top_commands_window=30d")
        assert resp.status_code == 200


# ===========================================================================
# Mesh routes
# ===========================================================================

class TestMeshRoutes:

    def test_api_mesh_nodes_returns_json(self, client):
        resp = client.get("/api/mesh/nodes")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "nodes" in data or isinstance(data, (list, dict))

    def test_api_mesh_edges_returns_json(self, client):
        resp = client.get("/api/mesh/edges")
        assert resp.status_code == 200

    def test_api_mesh_stats_returns_json(self, client):
        resp = client.get("/api/mesh/stats")
        assert resp.status_code == 200

    def test_api_mesh_resolve_path_missing_body(self, client):
        resp = client.post(
            "/api/mesh/resolve-path",
            json={},
            content_type="application/json",
        )
        # Should return 400 or 200 with error key — not a 500
        assert resp.status_code in (200, 400)

    def test_api_mesh_resolve_path_valid(self, client):
        resp = client.post(
            "/api/mesh/resolve-path",
            json={"path": "7e,01"},
            content_type="application/json",
        )
        assert resp.status_code == 200


# ===========================================================================
# Config / notification routes
# ===========================================================================

class TestConfigRoutes:

    def test_api_config_notifications_get(self, client):
        resp = client.get("/api/config/notifications")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "smtp_port" in data
        assert "smtp_security" in data

    def test_api_config_notifications_post(self, client):
        payload = {
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
            "smtp_security": "starttls",
            "smtp_user": "user@example.com",
            "smtp_password": "pass",
            "from_name": "Bot",
            "from_email": "bot@example.com",
            "recipients": "admin@example.com",
            "nightly_enabled": "true",
        }
        resp = client.post(
            "/api/config/notifications",
            json=payload,
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("success") is True

    def test_api_config_logging_get(self, client):
        resp = client.get("/api/config/logging")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_api_config_maintenance_get(self, client):
        resp = client.get("/api/config/maintenance")
        assert resp.status_code == 200

    def test_api_maintenance_status(self, client):
        resp = client.get("/api/maintenance/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)


# ===========================================================================
# Channel operations
# ===========================================================================

class TestChannelRoutes:

    def test_api_channels_get(self, client):
        resp = client.get("/api/channels")
        assert resp.status_code == 200

    def test_api_channel_stats(self, client):
        resp = client.get("/api/channels/stats")
        assert resp.status_code == 200

    def test_api_channels_validate_missing_name(self, client):
        resp = client.post(
            "/api/channels/validate",
            json={},
            content_type="application/json",
        )
        assert resp.status_code in (200, 400)

    def test_api_channel_operation_status_not_found(self, client):
        resp = client.get("/api/channel-operations/99999")
        assert resp.status_code in (200, 404)


# ===========================================================================
# Feeds routes
# ===========================================================================

class TestFeedRoutes:

    def test_api_feeds_get(self, client):
        resp = client.get("/api/feeds")
        assert resp.status_code == 200
        data = resp.get_json()
        # Returns {'feeds': [...], 'total': N} or a plain list
        assert isinstance(data, (dict, list))

    def test_api_feeds_stats(self, client):
        resp = client.get("/api/feeds/stats")
        assert resp.status_code == 200

    def test_api_feeds_default_format(self, client):
        resp = client.get("/api/feeds/default-format")
        assert resp.status_code == 200

    def test_api_feed_not_found(self, client):
        resp = client.get("/api/feeds/99999")
        assert resp.status_code in (200, 404)

    def test_api_feed_delete_not_found(self, client):
        resp = client.delete("/api/feeds/99999")
        assert resp.status_code in (200, 404)


# ===========================================================================
# Authentication (password-protected viewer)
# ===========================================================================

class TestAuthRoutes:

    def test_login_page_get(self, auth_client):
        resp = auth_client.get("/login")
        assert resp.status_code == 200

    def test_unauthenticated_index_redirects_to_login(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"]

    def test_unauthenticated_api_returns_401(self, auth_client):
        resp = auth_client.get("/api/health")
        assert resp.status_code == 401

    def test_login_wrong_password(self, auth_client):
        resp = auth_client.post(
            "/login",
            data={"password": "wrongpass"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert b"Invalid" in resp.data

    def test_login_correct_password_redirects(self, auth_client):
        resp = auth_client.post(
            "/login",
            data={"password": "secret123"},
            follow_redirects=False,
        )
        assert resp.status_code == 302

    def test_authenticated_can_access_index(self, auth_client):
        auth_client.post("/login", data={"password": "secret123"})
        resp = auth_client.get("/")
        assert resp.status_code == 200

    def test_logout_clears_session(self, auth_client):
        auth_client.post("/login", data={"password": "secret123"})
        resp = auth_client.get("/logout", follow_redirects=False)
        assert resp.status_code == 302
        # After logout, index should redirect to login again
        resp2 = auth_client.get("/")
        assert resp2.status_code == 302

    def test_login_no_password_configured_redirects_to_index(self, client):
        """When no password is set, /login should redirect to /."""
        resp = client.get("/login", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")


# ===========================================================================
# Open-access routes (no auth required even with password enabled)
# ===========================================================================

class TestOpenRoutes:

    def test_favicon_ico(self, client):
        resp = client.get("/favicon.ico")
        assert resp.status_code in (200, 404)  # 404 if static file absent

    def test_favicon_32(self, client):
        resp = client.get("/favicon-32x32.png")
        assert resp.status_code in (200, 404)

    def test_favicon_16(self, client):
        resp = client.get("/favicon-16x16.png")
        assert resp.status_code in (200, 404)

    def test_apple_touch_icon(self, client):
        resp = client.get("/apple-touch-icon.png")
        assert resp.status_code in (200, 404)

    def test_site_webmanifest(self, client):
        resp = client.get("/site.webmanifest")
        assert resp.status_code in (200, 404)

    def test_favicon_not_blocked_by_auth(self, auth_client):
        resp = auth_client.get("/favicon.ico")
        # Auth exempt — should NOT be 302/401
        assert resp.status_code in (200, 404)


# ===========================================================================
# Recent commands / stream
# ===========================================================================

class TestStreamRoutes:

    def test_api_recent_commands(self, client):
        resp = client.get("/api/recent_commands")
        assert resp.status_code == 200

    def test_api_stream_data_post(self, client):
        payload = {"type": "command", "data": {"cmd": "ping"}}
        resp = client.post(
            "/api/stream_data",
            json=payload,
            content_type="application/json",
        )
        assert resp.status_code == 200


# ===========================================================================
# Greeter routes
# ===========================================================================

class TestGreeterRoutes:

    def test_api_greeter_get(self, client):
        resp = client.get("/api/greeter")
        assert resp.status_code == 200

    def test_api_greeter_end_rollout(self, client):
        resp = client.post(
            "/api/greeter/end-rollout",
            json={"public_key": "a" * 64},
            content_type="application/json",
        )
        assert resp.status_code in (200, 400, 404, 500)

    def test_api_greeter_ungreet(self, client):
        resp = client.post(
            "/api/greeter/ungreet",
            json={"public_key": "a" * 64},
            content_type="application/json",
        )
        assert resp.status_code in (200, 400, 404, 500)


# ===========================================================================
# Delete contact
# ===========================================================================

class TestDeleteContact:

    def test_delete_contact_missing_key(self, client):
        resp = client.post(
            "/api/delete-contact",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_delete_contact_unknown_key(self, client):
        resp = client.post(
            "/api/delete-contact",
            json={"public_key": "0" * 64},
            content_type="application/json",
        )
        assert resp.status_code in (200, 404)

    def test_delete_contact_existing(self, client, viewer):
        pk = _insert_contact(viewer, "deadbeef" * 8, "DeleteMe")
        resp = client.post(
            "/api/delete-contact",
            json={"public_key": pk},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("success") is True


# ===========================================================================
# Geocode contact
# ===========================================================================

class TestGeocodeContact:

    def test_geocode_missing_public_key(self, client):
        resp = client.post(
            "/api/geocode-contact",
            json={},
            content_type="application/json",
        )
        assert resp.status_code in (200, 400)

    def test_geocode_unknown_contact(self, client):
        resp = client.post(
            "/api/geocode-contact",
            json={"public_key": "0" * 64},
            content_type="application/json",
        )
        assert resp.status_code in (200, 404)


# ===========================================================================
# Version info helper (unit test, no HTTP)
# ===========================================================================

class TestVersionInfo:

    def test_version_info_structure(self, viewer):
        info = viewer._version_info
        assert isinstance(info, dict)
        assert set(info.keys()) >= {"tag", "branch", "commit", "date"}

    def test_version_info_returns_something(self, viewer):
        # At least one field is populated in a git repo
        info = viewer._version_info
        assert any(v is not None for v in info.values())


# ===========================================================================
# Config loading helper (unit test)
# ===========================================================================

class TestConfigLoading:

    def test_load_config_nonexistent_returns_empty(self, viewer):
        cfg = viewer._load_config("/nonexistent/config.ini")
        assert isinstance(cfg, configparser.ConfigParser)

    def test_load_config_reads_values(self, viewer, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("cfg_load")
        p = tmp / "cfg.ini"
        db = str(tmp / "db.db")
        _write_config(p, db)
        cfg = viewer._load_config(str(p))
        assert cfg.get("Bot", "bot_name") == "TestBot"


# ===========================================================================
# Config logging API
# ===========================================================================

class TestConfigLoggingRoutes:

    def test_get_logging_returns_defaults(self, client):
        resp = client.get("/api/config/logging")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "log_max_bytes" in data
        assert "log_backup_count" in data

    def test_get_logging_default_values_populated(self, client):
        resp = client.get("/api/config/logging")
        data = json.loads(resp.data)
        # Defaults should be non-empty strings
        assert data["log_max_bytes"] != ""
        assert data["log_backup_count"] != ""

    def test_post_logging_saves_fields(self, client):
        resp = client.post(
            "/api/config/logging",
            data=json.dumps({"log_max_bytes": "10485760", "log_backup_count": "5"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert set(data["saved"]) == {"log_max_bytes", "log_backup_count"}

    def test_post_logging_ignores_unknown_fields(self, client):
        resp = client.post(
            "/api/config/logging",
            data=json.dumps({"unknown_field": "value"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["saved"] == []

    def test_post_logging_empty_body(self, client):
        resp = client.post("/api/config/logging", content_type="application/json")
        assert resp.status_code == 200


# ===========================================================================
# Config maintenance API
# ===========================================================================

class TestConfigMaintenanceRoutes:

    def test_get_maintenance_returns_defaults(self, client):
        resp = client.get("/api/config/maintenance")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "db_backup_enabled" in data
        assert "db_backup_schedule" in data
        assert "db_backup_time" in data

    def test_post_maintenance_saves_backup_settings(self, client):
        payload = {
            "db_backup_enabled": "true",
            "db_backup_schedule": "weekly",
            "db_backup_time": "03:00",
            "db_backup_retention_count": "14",
            "db_backup_dir": "/tmp/backups",
            "email_attach_log": "true",
        }
        resp = client.post(
            "/api/config/maintenance",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        assert len(data["saved"]) == 6

    def test_post_maintenance_empty_body(self, client):
        resp = client.post("/api/config/maintenance", content_type="application/json")
        assert resp.status_code == 200

    def test_get_maintenance_status(self, client):
        resp = client.get("/api/maintenance/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "data_retention_ran_at" in data
        assert "nightly_email_ran_at" in data
        assert "db_backup_ran_at" in data


# ===========================================================================
# Config notifications API
# ===========================================================================

class TestConfigNotificationsRoutes:

    def test_get_notifications_returns_200(self, client):
        resp = client.get("/api/config/notifications")
        assert resp.status_code == 200

    def test_post_notifications_test_returns_result(self, client):
        resp = client.post(
            "/api/config/notifications/test",
            data=json.dumps({"type": "email"}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 400, 500)


# ===========================================================================
# Feed management API
# ===========================================================================

class TestFeedManagementRoutes:

    def test_get_feeds_returns_200(self, client):
        resp = client.get("/api/feeds")
        assert resp.status_code == 200

    def test_get_feed_stats(self, client):
        resp = client.get("/api/feeds/stats")
        assert resp.status_code == 200

    def test_get_feed_detail_not_found(self, client):
        resp = client.get("/api/feeds/99999")
        assert resp.status_code in (404, 200)

    def test_post_feeds_missing_data(self, client):
        resp = client.post(
            "/api/feeds",
            data=json.dumps({}),
            content_type="application/json",
        )
        # Should return 400 or 500 — invalid/empty feed
        assert resp.status_code in (200, 400, 500)

    def test_delete_feed_not_found(self, client):
        resp = client.delete("/api/feeds/99999")
        assert resp.status_code in (200, 404, 500)

    def test_get_default_format(self, client):
        resp = client.get("/api/feeds/default-format")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "default_format" in data

    def test_post_feeds_preview_missing_url(self, client):
        resp = client.post(
            "/api/feeds/preview",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_post_feeds_test_missing_url(self, client):
        resp = client.post(
            "/api/feeds/test",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_post_feeds_test_invalid_url(self, client):
        resp = client.post(
            "/api/feeds/test",
            data=json.dumps({"url": "not-a-url"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_post_feeds_test_valid_url_accepted(self, client):
        resp = client.post(
            "/api/feeds/test",
            data=json.dumps({"url": "https://example.com/feed.rss"}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_get_feed_activity(self, client):
        resp = client.get("/api/feeds/1/activity")
        assert resp.status_code in (200, 404, 500)

    def test_get_feed_errors(self, client):
        resp = client.get("/api/feeds/1/errors")
        assert resp.status_code in (200, 404, 500)

    def test_post_feed_refresh(self, client):
        resp = client.post("/api/feeds/1/refresh")
        assert resp.status_code in (200, 404, 500)


# ===========================================================================
# Channel management API
# ===========================================================================

class TestChannelManagementRoutes:

    def test_get_channels_returns_dict(self, client):
        resp = client.get("/api/channels")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "channels" in data

    def test_post_channel_missing_name(self, client):
        resp = client.post(
            "/api/channels",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_delete_channel_not_found(self, client):
        resp = client.delete("/api/channels/99")
        assert resp.status_code in (200, 400, 404, 500)

    def test_get_channel_operation_not_found(self, client):
        resp = client.get("/api/channel-operations/99999")
        assert resp.status_code in (200, 404, 500)

    def test_post_channel_validate_missing_data(self, client):
        resp = client.post(
            "/api/channels/validate",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 400, 500)

    def test_get_channel_stats(self, client):
        resp = client.get("/api/channels/stats")
        assert resp.status_code == 200

    def test_get_channel_feeds(self, client):
        resp = client.get("/api/channels/0/feeds")
        assert resp.status_code in (200, 404, 500)


# ===========================================================================
# Optimize database API
# ===========================================================================

class TestDatabaseOptimizeRoute:

    def test_post_optimize_database(self, client):
        resp = client.post("/api/optimize-database")
        assert resp.status_code in (200, 500)


# ===========================================================================
# Purge contacts API
# ===========================================================================

class TestPurgeContactsRoutes:

    def test_get_purge_preview(self, client):
        resp = client.get("/api/contacts/purge-preview")
        assert resp.status_code in (200, 500)

    def test_post_purge_empty_body(self, client):
        resp = client.post(
            "/api/contacts/purge",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 400, 500)


# ===========================================================================
# Mesh graph API routes
# ===========================================================================

class TestMeshGraphRoutes:

    def test_get_mesh_nodes_returns_200(self, client):
        resp = client.get("/api/mesh/nodes")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "nodes" in data

    def test_get_mesh_nodes_with_prefix_param(self, client):
        resp = client.get("/api/mesh/nodes?prefix_hex_chars=4")
        assert resp.status_code == 200

    def test_get_mesh_edges_returns_200(self, client):
        resp = client.get("/api/mesh/edges")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "edges" in data

    def test_get_mesh_edges_with_filter_params(self, client):
        resp = client.get("/api/mesh/edges?min_observations=2&days=7")
        assert resp.status_code == 200

    def test_get_mesh_stats_returns_200(self, client):
        resp = client.get("/api/mesh/stats")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "node_count" in data
        assert "total_edges" in data

    def test_post_resolve_path_missing_body(self, client):
        resp = client.post("/api/mesh/resolve-path", content_type="application/json")
        assert resp.status_code in (400, 500)

    def test_post_resolve_path_missing_path_field(self, client):
        resp = client.post(
            "/api/mesh/resolve-path",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_post_resolve_path_with_hex(self, client):
        resp = client.post(
            "/api/mesh/resolve-path",
            data=json.dumps({"path": "aabbccdd", "prefix_hex_chars": 2}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 400, 500)


# ===========================================================================
# Radio API routes
# ===========================================================================

class TestRadioApiRoutes:

    def test_get_radio_status_returns_200(self, client):
        resp = client.get("/api/radio/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "connected" in data or "error" in data

    def test_post_radio_reboot_queues_operation(self, client):
        resp = client.post("/api/radio/reboot")
        assert resp.status_code in (200, 500)
        if resp.status_code == 200:
            data = json.loads(resp.data)
            assert data.get("success") is True

    def test_post_radio_connect_missing_action(self, client):
        resp = client.post(
            "/api/radio/connect",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_post_radio_connect_invalid_action(self, client):
        resp = client.post(
            "/api/radio/connect",
            data=json.dumps({"action": "restart"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_post_radio_connect_valid(self, client):
        resp = client.post(
            "/api/radio/connect",
            data=json.dumps({"action": "connect"}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 500)

    def test_post_radio_disconnect_valid(self, client):
        resp = client.post(
            "/api/radio/connect",
            data=json.dumps({"action": "disconnect"}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 500)


# ===========================================================================
# Greeter API route
# ===========================================================================

class TestGreeterRoute:

    def test_get_greeter_returns_200(self, client):
        resp = client.get("/api/greeter")
        assert resp.status_code == 200

    def test_post_greeter_end_rollout(self, client):
        resp = client.post("/api/greeter/end-rollout", content_type="application/json")
        assert resp.status_code in (200, 400, 404, 500)

    def test_post_greeter_ungreet(self, client):
        resp = client.post(
            "/api/greeter/ungreet",
            data=json.dumps({"sender_id": "TestUser"}),
            content_type="application/json",
        )
        assert resp.status_code in (200, 400, 404, 500)


# ===========================================================================
# Stream data and recent commands
# ===========================================================================

class TestStreamAndCommandRoutes:

    def test_get_recent_commands_returns_200(self, client):
        resp = client.get("/api/recent_commands")
        assert resp.status_code == 200

    def test_post_stream_data_empty_body(self, client):
        resp = client.post("/api/stream_data", content_type="application/json")
        assert resp.status_code in (200, 400, 500)


# ===========================================================================
# Rate limiter stats API
# ===========================================================================

class TestRateLimiterStatsRoute:

    def test_get_rate_limiter_stats_returns_200(self, client):
        resp = client.get("/api/stats/rate_limiters")
        assert resp.status_code == 200

    def test_rate_limiter_stats_returns_dict(self, client):
        resp = client.get("/api/stats/rate_limiters")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_rate_limiter_stats_no_bot(self, viewer):
        """When viewer has no bot attribute the endpoint returns an empty dict."""
        # Standalone viewer has no .bot — just use the test client directly
        with viewer.app.test_client() as c:
            resp = c.get("/api/stats/rate_limiters")
        assert resp.status_code == 200
        assert resp.get_json() == {}


# ---------------------------------------------------------------------------
# Werkzeug WebSocket compatibility patch
# ---------------------------------------------------------------------------

class TestWerkzeugWebSocketFix:
    """_apply_werkzeug_websocket_fix patches SimpleWebSocketWSGI.__call__ so
    that Werkzeug's write() before start_response assertion is never raised
    when a WebSocket session ends normally."""

    def test_patch_is_applied_at_module_import(self):
        """SimpleWebSocketWSGI.__call__ should be our patched wrapper after
        importing app.py (which calls _apply_werkzeug_websocket_fix at import
        time)."""
        from engineio.async_drivers import _websocket_wsgi
        # The patch wraps __call__; the closure name reflects the patch.
        assert _websocket_wsgi.SimpleWebSocketWSGI.__call__.__name__ == '_patched_call'

    def test_patch_calls_start_response_after_handler(self):
        """After the underlying __call__ returns, the patch must invoke
        start_response so that status_set is not None when Werkzeug's
        write(b'') runs."""
        from engineio.async_drivers import _websocket_wsgi

        sr_calls = []

        def fake_start_response(status, headers, exc_info=None):
            sr_calls.append((status, headers))
            return lambda data: None

        # Build a minimal mock SimpleWebSocketWSGI instance where __call__
        # returns [] (as _websocket_handler does on teardown).
        from unittest.mock import MagicMock
        from unittest.mock import patch as mock_patch

        ws_instance = MagicMock(spec=_websocket_wsgi.SimpleWebSocketWSGI)

        # Temporarily restore a fake "original" __call__ that returns []
        with mock_patch.object(
            _websocket_wsgi.SimpleWebSocketWSGI,
            '__call__',
            new=_websocket_wsgi.SimpleWebSocketWSGI.__call__,
        ):
            # The real patched __call__ is already in place; call it with a
            # mock "inner" that returns [] without calling start_response.
            from modules.web_viewer.app import _apply_werkzeug_websocket_fix

            captured = {}

            def mock_orig(self, environ, start_response):
                captured['sr_called'] = False
                return []

            orig = _websocket_wsgi.SimpleWebSocketWSGI.__call__
            _websocket_wsgi.SimpleWebSocketWSGI.__call__ = mock_orig
            try:
                _apply_werkzeug_websocket_fix()
                result = _websocket_wsgi.SimpleWebSocketWSGI.__call__(
                    ws_instance, {}, fake_start_response
                )
            finally:
                _websocket_wsgi.SimpleWebSocketWSGI.__call__ = orig

        assert result == []
        # start_response must have been called by the patch
        assert len(sr_calls) == 1
        assert sr_calls[0][0] == '200 OK'

    def test_patch_tolerates_start_response_already_called(self):
        """If start_response was already called (e.g. error path), the patch
        must not propagate the 'Headers already set' AssertionError."""
        from unittest.mock import MagicMock

        from engineio.async_drivers import _websocket_wsgi

        from modules.web_viewer.app import _apply_werkzeug_websocket_fix

        call_count = [0]

        def raises_on_second(status, headers, exc_info=None):
            call_count[0] += 1
            if call_count[0] > 1:
                raise AssertionError("Headers already set")
            return lambda data: None

        ws_instance = MagicMock(spec=_websocket_wsgi.SimpleWebSocketWSGI)

        def mock_orig_already_called(self, environ, start_response):
            start_response('500 INTERNAL SERVER ERROR', [])
            return []

        orig = _websocket_wsgi.SimpleWebSocketWSGI.__call__
        _websocket_wsgi.SimpleWebSocketWSGI.__call__ = mock_orig_already_called
        try:
            _apply_werkzeug_websocket_fix()
            # Must not raise even though start_response throws on second call
            result = _websocket_wsgi.SimpleWebSocketWSGI.__call__(
                ws_instance, {}, raises_on_second
            )
        finally:
            _websocket_wsgi.SimpleWebSocketWSGI.__call__ = orig

        assert result == []

    def test_patch_is_idempotent(self):
        """Calling _apply_werkzeug_websocket_fix() twice must not double-wrap
        and must leave the patched callable working correctly."""
        from engineio.async_drivers import _websocket_wsgi

        from modules.web_viewer.app import _apply_werkzeug_websocket_fix

        sr_calls = []

        def fake_sr(status, headers, exc_info=None):
            sr_calls.append(status)
            return lambda data: None

        # Apply a second time
        _apply_werkzeug_websocket_fix()

        from unittest.mock import MagicMock
        ws_instance = MagicMock(spec=_websocket_wsgi.SimpleWebSocketWSGI)

        # Temporarily replace the inner with a simple stub
        def stub_orig(self, environ, start_response):
            return []

        orig = _websocket_wsgi.SimpleWebSocketWSGI.__call__
        _websocket_wsgi.SimpleWebSocketWSGI.__call__ = stub_orig
        try:
            _apply_werkzeug_websocket_fix()
            _websocket_wsgi.SimpleWebSocketWSGI.__call__(
                ws_instance, {}, fake_sr
            )
        finally:
            _websocket_wsgi.SimpleWebSocketWSGI.__call__ = orig

        # Exactly one start_response call regardless of how many times patch applied
        assert len(sr_calls) == 1

    def test_patch_handles_missing_engineio(self):
        """_apply_werkzeug_websocket_fix must not raise if engineio is absent."""
        import sys
        from unittest.mock import patch as mock_patch

        from modules.web_viewer.app import _apply_werkzeug_websocket_fix

        with mock_patch.dict(sys.modules, {'engineio.async_drivers._websocket_wsgi': None}):
            # Should be a no-op, not raise
            _apply_werkzeug_websocket_fix()


# ===========================================================================
# TASK-01: Radio page — firmware config + reboot UI removed
# ===========================================================================

class TestRadioPageFirmwareRemoval:
    """Assert that firmware config and reboot UI are absent from /radio (TASK-01)."""

    def test_radio_page_loads(self, client):
        resp = client.get("/radio")
        assert resp.status_code == 200

    def test_firmware_config_card_absent(self, client):
        resp = client.get("/radio")
        html = resp.data.decode()
        assert 'id="firmware-config"' not in html
        assert "readFirmwareConfig" not in html
        assert "writeFirmwareConfig" not in html
        assert "readFirmwareBtn" not in html
        assert "writeFirmwareBtn" not in html
        assert "firmwareStatusAlert" not in html
        assert "firmwareLastRead" not in html
        assert "Firmware Configuration" not in html

    def test_reboot_ui_absent(self, client):
        resp = client.get("/radio")
        html = resp.data.decode()
        assert "rebootRadioBtn" not in html
        assert "rebootConfirmModal" not in html
        assert "confirmRebootBtn" not in html
        assert "confirmReboot" not in html
        assert "rebootRadio" not in html
        assert "handleReboot" not in html

    def test_connect_section_present(self, client):
        """Connect/disconnect button must still be present after removal."""
        resp = client.get("/radio")
        html = resp.data.decode()
        assert "connectToggleBtn" in html
        assert "handleConnectToggle" in html


# ===========================================================================
# TASK-02: subscribe_commands history replay (BUG-023)
# ===========================================================================

def _insert_packet_stream_rows(db_path: str, rows: list) -> None:
    """Insert rows into packet_stream for testing. Each row: (timestamp, data_json, type)."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS packet_stream"
            " (id INTEGER PRIMARY KEY, timestamp REAL, data TEXT, type TEXT)"
        )
        for i, (ts, data_json, row_type) in enumerate(rows):
            conn.execute(
                "INSERT INTO packet_stream (timestamp, data, type) VALUES (?, ?, ?)",
                (ts, data_json, row_type),
            )
        conn.commit()


@pytest.fixture
def socketio_viewer(tmp_path_factory):
    """Isolated viewer fixture for SocketIO event tests."""
    from unittest.mock import patch as _patch
    tmp = tmp_path_factory.mktemp("sio_viewer")
    db_path = str(tmp / "sio_test.db")
    config_path = str(tmp / "config.ini")
    _write_config(Path(config_path), db_path)

    with (
        _patch.object(BotDataViewer, "_setup_logging", _fake_setup_logging),
        _patch.object(BotDataViewer, "_start_database_polling", lambda self: None),
        _patch.object(BotDataViewer, "_start_log_tailing", lambda self: None),
        _patch.object(BotDataViewer, "_start_cleanup_scheduler", lambda self: None),
    ):
        v = BotDataViewer(db_path=db_path, config_path=config_path)

    v.app.config["TESTING"] = True
    v.app.config["SECRET_KEY"] = "test-secret"
    return v


class TestSubscribeCommandsHistoryReplay:
    """subscribe_commands must replay last 50 command rows on connect (TASK-02 / BUG-023)."""

    def test_subscribe_commands_replays_history(self, socketio_viewer):
        """History rows are emitted as command_data events on subscribe."""
        import json as _json
        import time as _time

        from flask_socketio import SocketIOTestClient

        now = _time.time()
        # Give each row a distinct timestamp so ORDER BY is deterministic
        rows = [
            (now - 50 + i, _json.dumps({"cmd": "ping", "seq": i}), "command")
            for i in range(5)
        ]
        _insert_packet_stream_rows(socketio_viewer.db_path, rows)

        sio_client = SocketIOTestClient(
            socketio_viewer.app, socketio_viewer.socketio
        )
        sio_client.emit("subscribe_commands")

        received = sio_client.get_received()
        command_events = [e for e in received if e["name"] == "command_data"]
        assert len(command_events) == 5
        seq_values = [e["args"][0]["seq"] for e in command_events]
        assert seq_values == list(range(5))  # replayed in chronological order

    def test_subscribe_commands_sets_subscription_flag(self, socketio_viewer):
        """subscribed_commands flag is set to True after subscribe event."""
        from flask_socketio import SocketIOTestClient

        sio_client = SocketIOTestClient(
            socketio_viewer.app, socketio_viewer.socketio
        )
        sio_client.emit("subscribe_commands")

        with socketio_viewer._clients_lock:
            flags = [
                info.get("subscribed_commands", False)
                for info in socketio_viewer.connected_clients.values()
            ]
        assert any(flags), "At least one client should have subscribed_commands=True"

    def test_subscribe_commands_empty_history(self, socketio_viewer):
        """subscribe_commands with no history emits only status event, no command_data."""
        from flask_socketio import SocketIOTestClient

        sio_client = SocketIOTestClient(
            socketio_viewer.app, socketio_viewer.socketio
        )
        sio_client.emit("subscribe_commands")

        received = sio_client.get_received()
        command_events = [e for e in received if e["name"] == "command_data"]
        assert len(command_events) == 0

    def test_subscribe_commands_only_replays_command_type(self, socketio_viewer):
        """Only rows with type='command' are replayed — not packets or messages."""
        import json as _json
        import time as _time

        from flask_socketio import SocketIOTestClient

        now = _time.time()
        rows = [
            (now - 5, _json.dumps({"t": "cmd"}), "command"),
            (now - 4, _json.dumps({"t": "pkt"}), "packet"),
            (now - 3, _json.dumps({"t": "msg"}), "message"),
        ]
        _insert_packet_stream_rows(socketio_viewer.db_path, rows)

        sio_client = SocketIOTestClient(
            socketio_viewer.app, socketio_viewer.socketio
        )
        sio_client.emit("subscribe_commands")

        received = sio_client.get_received()
        command_events = [e for e in received if e["name"] == "command_data"]
        assert len(command_events) == 1
        assert command_events[0]["args"][0]["t"] == "cmd"

    def test_polling_thread_last_timestamp_is_recent(self, socketio_viewer):
        """_start_database_polling initializes last_timestamp ~5 min back, not epoch 0."""
        import inspect

        # Extract poll_database source from _start_database_polling closure
        src = inspect.getsource(socketio_viewer._start_database_polling)
        # The source should reference time.time() - 300, not "= 0"
        assert "time() - 300" in src or "_time.time() - 300" in src, (
            "last_timestamp must be initialized to time.time()-300, not 0"
        )
