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

    def test_index_live_activity_controls(self, client):
        """Dashboard index page contains scroll buttons and type-filter checkboxes."""
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.data.decode()
        # Scroll buttons
        assert 'id="live-scroll-top"' in html
        assert 'id="live-scroll-bottom"' in html
        assert 'scrollLiveFeed' in html
        # Filter checkboxes with data-type attributes
        assert 'data-type="packet"' in html
        assert 'data-type="command"' in html
        assert 'data-type="message"' in html
        assert 'live-filter-cb' in html
        # [#channel] prefix logic present in JS
        assert 'applyFilters' in html

    def test_realtime(self, client):
        resp = client.get("/realtime")
        assert resp.status_code == 200

    def test_realtime_scroll_controls(self, client):
        """Realtime page has scroll buttons, type filters, and channel labels in messages."""
        resp = client.get("/realtime")
        assert resp.status_code == 200
        html = resp.data.decode()
        # Scroll buttons present for all three streams
        assert 'id="cmd-scroll-top"' in html
        assert 'id="cmd-scroll-bottom"' in html
        assert 'id="pkt-scroll-top"' in html
        assert 'id="pkt-scroll-bottom"' in html
        assert 'id="msg-scroll-top"' in html
        assert 'id="msg-scroll-bottom"' in html
        # scrollStream JS function present
        assert 'scrollStream' in html
        # Type filter checkboxes for each stream panel
        assert 'rt-filter-cb' in html
        assert 'id="rt-filter-command"' in html
        assert 'id="rt-filter-packet"' in html
        assert 'id="rt-filter-message"' in html
        assert 'id="command-card"' in html
        assert 'id="packet-card"' in html
        assert 'id="message-card"' in html
        # Live message helpers: strip duplicate bracket tags, per-channel accent
        assert 'stripLeadingChannelBracketTag' in html
        assert 'channelAccentStyles' in html

    def test_logs(self, client):
        resp = client.get("/logs")
        assert resp.status_code == 200

    def test_contacts(self, client):
        resp = client.get("/contacts")
        assert resp.status_code == 200

    def test_cache(self, client):
        resp = client.get("/cache")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/config#database")

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

    def test_api_stream_data_rejects_without_token_in_production(self, viewer):
        """When TESTING is False, requests without a valid stream token are rejected."""
        viewer.db_manager.set_metadata('internal.stream_token', 'secret-token')
        viewer.app.config['TESTING'] = False
        try:
            with viewer.app.test_client() as c:
                resp = c.post(
                    "/api/stream_data",
                    json={"type": "command", "data": {"cmd": "ping"}},
                    content_type="application/json",
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
            assert resp.status_code == 401
        finally:
            viewer.app.config['TESTING'] = True


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
            "db_backup_dir": "/tmp",  # /tmp always exists
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


# ---------------------------------------------------------------------------
# TASK-03: GET /api/connected_clients
# ---------------------------------------------------------------------------

class TestConnectedClientsApi:
    """Tests for GET /api/connected_clients endpoint."""

    def test_empty_when_no_clients(self, viewer):
        """Returns empty list when no clients connected."""
        viewer.connected_clients.clear()
        with viewer.app.test_client() as c:
            resp = c.get("/api/connected_clients")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == []

    def test_returns_client_list(self, viewer):
        """Returns list with client_id, connected_at, last_activity fields."""
        import time
        now = time.time()
        viewer.connected_clients["abcdef1234567890"] = {
            "connected_at": now - 60,
            "last_activity": now - 5,
            "subscribed_commands": False,
        }
        try:
            with viewer.app.test_client() as c:
                resp = c.get("/api/connected_clients")
            assert resp.status_code == 200
            data = resp.get_json()
            assert isinstance(data, list)
            assert len(data) == 1
            entry = data[0]
            assert "client_id" in entry
            assert "connected_at" in entry
            assert "last_activity" in entry
            # ID is truncated to first 8 chars + ellipsis
            assert entry["client_id"] == "abcdef12\u2026"
            assert abs(entry["connected_at"] - (now - 60)) < 1
            assert abs(entry["last_activity"] - (now - 5)) < 1
        finally:
            viewer.connected_clients.pop("abcdef1234567890", None)

    def test_multiple_clients(self, viewer):
        """Returns all connected clients."""
        import time
        now = time.time()
        viewer.connected_clients["aaa"] = {"connected_at": now, "last_activity": now}
        viewer.connected_clients["bbb"] = {"connected_at": now, "last_activity": now}
        try:
            with viewer.app.test_client() as c:
                resp = c.get("/api/connected_clients")
            assert resp.status_code == 200
            data = resp.get_json()
            ids = [entry["client_id"] for entry in data]
            assert "aaa" in ids
            assert "bbb" in ids
        finally:
            viewer.connected_clients.pop("aaa", None)
            viewer.connected_clients.pop("bbb", None)

    def test_short_id_not_truncated(self, viewer):
        """Client IDs of 8 chars or fewer are returned as-is."""
        import time
        now = time.time()
        viewer.connected_clients["short"] = {"connected_at": now, "last_activity": now}
        try:
            with viewer.app.test_client() as c:
                resp = c.get("/api/connected_clients")
            assert resp.status_code == 200
            data = resp.get_json()
            match = [entry for entry in data if entry["client_id"] == "short"]
            assert len(match) == 1
        finally:
            viewer.connected_clients.pop("short", None)

    def test_dashboard_contains_modal_and_link(self, viewer):
        """Dashboard page includes the connected-clients modal and clickable link."""
        with viewer.app.test_client() as c:
            resp = c.get("/")
        html = resp.data.decode()
        assert "connectedClientsModal" in html
        assert "connected-clients-table" in html
        assert "loadConnectedClients" in html


# ---------------------------------------------------------------------------
# TASK-04: DB backup dir validation on POST /api/config/maintenance
# ---------------------------------------------------------------------------

class TestDbBackupDirValidation:
    """Tests for backup directory validation in POST /api/config/maintenance."""

    def test_nonexistent_dir_returns_400(self, viewer):
        """Returns 400 with error message when backup_dir does not exist."""
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/config/maintenance",
                json={"db_backup_dir": "/nonexistent/path/that/does/not/exist"},
                content_type="application/json",
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data
        assert "/nonexistent/path/that/does/not/exist" in data["error"]

    def test_existing_dir_returns_200(self, viewer, tmp_path):
        """Returns 200 when backup_dir exists."""
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/config/maintenance",
                json={"db_backup_dir": str(tmp_path)},
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "db_backup_dir" in data["saved"]

    def test_empty_dir_skips_validation(self, viewer):
        """Empty string for db_backup_dir skips the isdir check and saves."""
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/config/maintenance",
                json={"db_backup_dir": ""},
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_other_fields_save_when_no_dir(self, viewer):
        """Other maintenance fields save normally when db_backup_dir is absent."""
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/config/maintenance",
                json={"db_backup_enabled": "true", "db_backup_schedule": "daily"},
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "db_backup_enabled" in data["saved"]
        assert "db_backup_schedule" in data["saved"]

    def test_error_message_contains_path(self, viewer):
        """Error message specifically mentions the invalid path."""
        bad_path = "/absolutely/does/not/exist/xyz"
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/config/maintenance",
                json={"db_backup_dir": bad_path},
                content_type="application/json",
            )
        assert resp.status_code == 400
        assert bad_path in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# TASK-06: POST /api/maintenance/backup_now
# ---------------------------------------------------------------------------

class TestBackupNowRoute:
    """Tests for POST /api/maintenance/backup_now endpoint."""

    def test_returns_503_when_no_scheduler(self, viewer):
        """Returns 503 when bot/scheduler is not attached."""
        # viewer fixture has no bot attached
        with viewer.app.test_client() as c:
            resp = c.post("/api/maintenance/backup_now")
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["success"] is False
        assert "error" in data  # Error details sanitized in 5xx responses

    def test_returns_200_on_successful_backup(self, viewer, tmp_path):
        """Returns 200 with success=True and path when backup succeeds."""
        from unittest.mock import MagicMock, patch

        mock_scheduler = MagicMock()

        def fake_run_db_backup():
            # Simulate what run_db_backup writes to metadata
            viewer.db_manager.set_metadata(
                'maint.status.db_backup_path', str(tmp_path / "test.db")
            )
            viewer.db_manager.set_metadata('maint.status.db_backup_outcome', 'ok')

        mock_scheduler.run_db_backup = fake_run_db_backup
        mock_bot = MagicMock()
        mock_bot.scheduler = mock_scheduler

        with patch.object(viewer, 'bot', mock_bot, create=True):
            with viewer.app.test_client() as c:
                resp = c.post("/api/maintenance/backup_now")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "test.db" in data["path"]

    def test_returns_500_on_backup_error(self, viewer):
        """Returns 500 with success=False when backup writes an error outcome."""
        from unittest.mock import MagicMock, patch

        mock_scheduler = MagicMock()

        def fake_run_db_backup():
            viewer.db_manager.set_metadata('maint.status.db_backup_path', '')
            viewer.db_manager.set_metadata(
                'maint.status.db_backup_outcome', 'error: cannot create dir'
            )

        mock_scheduler.run_db_backup = fake_run_db_backup
        mock_bot = MagicMock()
        mock_bot.scheduler = mock_scheduler

        with patch.object(viewer, 'bot', mock_bot, create=True):
            with viewer.app.test_client() as c:
                resp = c.post("/api/maintenance/backup_now")

        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False

    def test_config_page_contains_backup_now_button(self, viewer):
        """Config page HTML includes the Backup Now button."""
        with viewer.app.test_client() as c:
            resp = c.get("/config")
        html = resp.data.decode()
        assert "backup-now-btn" in html
        assert "backup_now" in html


# ---------------------------------------------------------------------------
# TASK-07: POST /api/maintenance/restore + GET /api/maintenance/list_backups
# ---------------------------------------------------------------------------

class TestRestoreRoute:
    """Tests for the DB restore endpoint."""

    def test_missing_db_file_returns_400(self, viewer):
        """Returns 400 when db_file is absent."""
        with viewer.app.test_client() as c:
            resp = c.post("/api/maintenance/restore", json={},
                          content_type="application/json")
        assert resp.status_code == 400
        assert "db_file" in resp.get_json()["error"]

    def test_nonexistent_file_returns_400(self, viewer, tmp_path):
        """Returns 400 when db_file path does not exist."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        viewer.db_manager.set_metadata('maint.db_backup_dir', str(backup_dir))
        with viewer.app.test_client() as c:
            resp = c.post("/api/maintenance/restore",
                          json={"db_file": str(backup_dir / "missing.db")},
                          content_type="application/json")
        assert resp.status_code == 400
        assert "not found" in resp.get_json()["error"].lower()

    def test_path_traversal_returns_403(self, viewer, tmp_path):
        """Returns 403 when db_file is outside the backup directory."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        viewer.db_manager.set_metadata('maint.db_backup_dir', str(backup_dir))
        outside = tmp_path / "outside.db"
        outside.write_bytes(b"SQLite format 3\x00")
        with viewer.app.test_client() as c:
            resp = c.post("/api/maintenance/restore",
                          json={"db_file": str(outside)},
                          content_type="application/json")
        assert resp.status_code == 403

    def test_no_backup_dir_returns_400(self, viewer):
        """Returns 400 when no backup directory is configured."""
        viewer.db_manager.set_metadata('maint.db_backup_dir', '')
        with viewer.app.test_client() as c:
            resp = c.post("/api/maintenance/restore",
                          json={"db_file": "/some/file.db"},
                          content_type="application/json")
        assert resp.status_code == 400
        assert "backup directory" in resp.get_json()["error"].lower()

    def test_non_sqlite_file_returns_400(self, viewer, tmp_path):
        """Returns 400 when the file is not a valid SQLite database."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        viewer.db_manager.set_metadata('maint.db_backup_dir', str(backup_dir))
        bad = backup_dir / "bad.db"
        bad.write_bytes(b"not a sqlite file!!")
        with viewer.app.test_client() as c:
            resp = c.post("/api/maintenance/restore",
                          json={"db_file": str(bad)},
                          content_type="application/json")
        assert resp.status_code == 400
        assert "valid SQLite" in resp.get_json()["error"]

    def test_valid_sqlite_restore_returns_200(self, viewer, tmp_path):
        """Returns 200 with warning when a valid SQLite backup is restored."""
        import sqlite3 as _sql
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        viewer.db_manager.set_metadata('maint.db_backup_dir', str(backup_dir))
        backup = backup_dir / "backup.db"
        conn = _sql.connect(str(backup))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()
        # Patch db_path to a temp destination so the real test DB is not overwritten
        dest = str(tmp_path / "restored.db")
        with patch.object(viewer, "db_path", dest):
            with viewer.app.test_client() as c:
                resp = c.post("/api/maintenance/restore",
                              json={"db_file": str(backup)},
                              content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "warning" in data
        assert "Restart" in data["warning"]

    def test_list_backups_empty_when_no_dir(self, viewer):
        """list_backups returns empty list when backup dir not configured."""
        viewer.db_manager.set_metadata('maint.db_backup_dir', '')
        with viewer.app.test_client() as c:
            resp = c.get("/api/maintenance/list_backups")
        assert resp.status_code == 200
        assert resp.get_json()["backups"] == []

    def test_list_backups_returns_files(self, viewer, tmp_path):
        """list_backups returns matching .db files from the backup directory."""
        import sqlite3 as _sql
        db_stem = Path(viewer.db_path).stem
        for i in range(2):
            f = tmp_path / f"{db_stem}_2026010{i}T000000.db"
            conn = _sql.connect(str(f))
            conn.execute("CREATE TABLE t (id INTEGER)")
            conn.commit()
            conn.close()
        viewer.db_manager.set_metadata('maint.db_backup_dir', str(tmp_path))
        with viewer.app.test_client() as c:
            resp = c.get("/api/maintenance/list_backups")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["backups"]) == 2
        assert all("path" in b and "size_mb" in b for b in data["backups"])

    def test_config_page_contains_restore_modal(self, viewer):
        """Config page HTML includes the restore modal and button."""
        with viewer.app.test_client() as c:
            resp = c.get("/config")
        html = resp.data.decode()
        assert "restoreModal" in html
        assert "restore-btn" in html
        assert "restore-db-path" in html


# ---------------------------------------------------------------------------
# TASK-08: Database purge by age
# ---------------------------------------------------------------------------

class TestPurgeRoute:
    """Tests for POST /api/maintenance/purge."""

    def test_keep_all_returns_empty_deleted(self, viewer):
        """keep_days='all' returns 200 with empty deleted dict."""
        with viewer.app.test_client() as c:
            resp = c.post("/api/maintenance/purge",
                          json={"keep_days": "all"},
                          content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["deleted"] == {}

    def test_invalid_keep_days_returns_400(self, viewer):
        """keep_days=3 (not in valid set) returns 400."""
        with viewer.app.test_client() as c:
            resp = c.post("/api/maintenance/purge",
                          json={"keep_days": 3},
                          content_type="application/json")
        assert resp.status_code == 400
        assert "keep_days" in resp.get_json()["error"]

    def test_non_integer_keep_days_returns_400(self, viewer):
        """keep_days='invalid' returns 400."""
        with viewer.app.test_client() as c:
            resp = c.post("/api/maintenance/purge",
                          json={"keep_days": "invalid"},
                          content_type="application/json")
        assert resp.status_code == 400

    def test_valid_keep_days_returns_deleted_counts(self, viewer):
        """keep_days=30 returns 200 with per-table deleted counts."""
        with viewer.app.test_client() as c:
            resp = c.post("/api/maintenance/purge",
                          json={"keep_days": 30},
                          content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "deleted" in data
        # All six purgeable tables should be present in the response
        expected_tables = {
            "packet_stream", "message_stats", "complete_contact_tracking",
            "purging_log", "mesh_connections", "daily_stats",
        }
        assert expected_tables == set(data["deleted"].keys())

    def test_purge_subset_tables_only_purges_those(self, viewer):
        """tables=[packet_stream] returns deleted counts only for that table."""
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/maintenance/purge",
                json={"keep_days": 30, "tables": ["packet_stream"]},
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert set(data["deleted"].keys()) == {"packet_stream"}

    def test_purge_empty_tables_list_returns_400(self, viewer):
        """tables=[] returns 400."""
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/maintenance/purge",
                json={"keep_days": 30, "tables": []},
                content_type="application/json",
            )
        assert resp.status_code == 400
        assert "tables" in resp.get_json()["error"].lower()

    def test_purge_invalid_table_name_returns_400(self, viewer):
        """Unknown table name in tables returns 400."""
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/maintenance/purge",
                json={"keep_days": 30, "tables": ["not_a_table"]},
                content_type="application/json",
            )
        assert resp.status_code == 400
        assert "Invalid" in resp.get_json()["error"] or "invalid" in resp.get_json()["error"].lower()

    def test_all_valid_keep_days_values_accepted(self, viewer):
        """All documented valid keep_days values (1,7,14,30,60,90) return 200."""
        for days in [1, 7, 14, 30, 60, 90]:
            with viewer.app.test_client() as c:
                resp = c.post("/api/maintenance/purge",
                              json={"keep_days": days},
                              content_type="application/json")
            assert resp.status_code == 200, f"Failed for keep_days={days}"

    def test_old_rows_deleted_recent_rows_kept(self, viewer):
        """Rows older than cutoff are deleted; recent rows are kept."""
        import sqlite3 as _sql
        import time as _time

        old_ts = _time.time() - 40 * 86400    # 40 days ago
        new_ts = _time.time() - 1 * 86400     # 1 day ago

        # Insert one old and one new row into packet_stream
        with _sql.connect(viewer.db_path) as conn:
            conn.execute(
                "INSERT INTO packet_stream (timestamp, data, type) VALUES (?, ?, ?)",
                (old_ts, '{"test": "old"}', "test"),
            )
            conn.execute(
                "INSERT INTO packet_stream (timestamp, data, type) VALUES (?, ?, ?)",
                (new_ts, '{"test": "new"}', "test"),
            )
            conn.commit()

        with viewer.app.test_client() as c:
            resp = c.post("/api/maintenance/purge",
                          json={"keep_days": 30},
                          content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        # At least the one old packet_stream row should be deleted
        assert data["deleted"]["packet_stream"] >= 1

        # Recent row must still be present
        with _sql.connect(viewer.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM packet_stream WHERE data = ?",
                ('{"test": "new"}',),
            ).fetchone()
        assert row[0] == 1

    def test_config_page_contains_purge_card(self, viewer):
        """Config page HTML includes the purge card and confirmation modal."""
        with viewer.app.test_client() as c:
            resp = c.get("/config")
        html = resp.data.decode()
        assert "purgeModal" in html
        assert "purge-keep-days" in html
        assert "purge-confirm-btn" in html


# ---------------------------------------------------------------------------
# TASK-09: BotIntegration write queue (batch packet_stream inserts)
# ---------------------------------------------------------------------------

class TestBotIntegrationQueue:
    """Tests for BotIntegration's batched packet_stream write queue."""

    @pytest.fixture()
    def bot_integration(self, tmp_path):
        """Create a BotIntegration with a real temp SQLite DB and a fake bot."""
        import configparser as _cp
        from unittest.mock import MagicMock

        db_path = str(tmp_path / "test.db")
        cfg = _cp.ConfigParser()
        cfg["Connection"] = {"connection_type": "serial", "serial_port": "/dev/null"}
        cfg["Bot"] = {"bot_name": "Test", "db_path": db_path, "prefix_bytes": "1"}

        bot = MagicMock()
        bot.logger = logging.getLogger("test_integration")
        bot.logger.addHandler(logging.NullHandler())
        bot.config = cfg
        bot.bot_root = str(tmp_path)

        # Ensure schema exists (packet_stream is migration-owned).
        from modules.db_manager import DBManager

        class MinimalBot:
            def __init__(self, logger, config):
                self.logger = logger
                self.config = config

        bot.db_manager = DBManager(MinimalBot(bot.logger, cfg), db_path)

        from modules.web_viewer.integration import BotIntegration
        bi = BotIntegration(bot)
        yield bi
        bi.shutdown()

    def test_insert_queues_row_without_db_open(self, bot_integration):
        """_insert_packet_stream_row puts item in queue, does not open DB immediately."""
        # Queue should start empty
        assert bot_integration._write_queue.empty()
        bot_integration._insert_packet_stream_row('{"x":1}', 'packet')
        assert bot_integration._write_queue.qsize() == 1

    def test_flush_writes_row_to_db(self, bot_integration, tmp_path):
        """_flush_write_queue inserts queued rows into packet_stream."""
        bot_integration._insert_packet_stream_row('{"test":"flush"}', 'packet')
        bot_integration._flush_write_queue()

        db_path = bot_integration._get_web_viewer_db_path()
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT data, type FROM packet_stream WHERE type='packet'"
            ).fetchall()
        assert any(r[0] == '{"test":"flush"}' for r in rows)

    def test_flush_batches_multiple_rows(self, bot_integration):
        """_flush_write_queue inserts multiple queued rows in one transaction."""
        for i in range(5):
            bot_integration._insert_packet_stream_row(f'{{"n":{i}}}', 'command')
        assert bot_integration._write_queue.qsize() == 5
        bot_integration._flush_write_queue()
        assert bot_integration._write_queue.empty()

        db_path = bot_integration._get_web_viewer_db_path()
        with sqlite3.connect(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM packet_stream WHERE type='command'"
            ).fetchone()[0]
        assert count == 5

    def test_flush_empty_queue_is_noop(self, bot_integration):
        """_flush_write_queue on empty queue does not raise."""
        assert bot_integration._write_queue.empty()
        bot_integration._flush_write_queue()  # should not raise

    def test_shutdown_flushes_remaining_rows(self, bot_integration):
        """shutdown() flushes rows that were queued but not yet drained."""
        # Stop the drain thread early so rows stay in queue
        bot_integration._drain_stop.set()
        if bot_integration._drain_thread:
            bot_integration._drain_thread.join(timeout=2.0)

        bot_integration._insert_packet_stream_row('{"shutdown":"test"}', 'message')
        assert not bot_integration._write_queue.empty()

        # shutdown() must flush the remaining row
        bot_integration.shutdown()

        db_path = bot_integration._get_web_viewer_db_path()
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT data FROM packet_stream WHERE type='message'"
            ).fetchall()
        assert any(r[0] == '{"shutdown":"test"}' for r in rows)

    def test_drain_thread_is_running(self, bot_integration):
        """Drain thread is alive after construction."""
        assert bot_integration._drain_thread is not None
        assert bot_integration._drain_thread.is_alive()


# ===========================================================================
# PUT /api/channels/<idx> — update channel endpoint
# ===========================================================================

class TestUpdateChannelRoute:
    """Tests for PUT /api/channels/<channel_idx>."""

    def test_update_channel_no_body_returns_400(self, viewer):
        with viewer.app.test_client() as c:
            resp = c.put(
                "/api/channels/0",
                json={},
                content_type="application/json",
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_update_channel_with_body_returns_200(self, viewer):
        with viewer.app.test_client() as c:
            resp = c.put(
                "/api/channels/0",
                json={"name": "#newname"},
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    def test_update_channel_index_5_returns_200(self, viewer):
        with viewer.app.test_client() as c:
            resp = c.put(
                "/api/channels/5",
                json={"name": "#updated"},
                content_type="application/json",
            )
        assert resp.status_code == 200


# ===========================================================================
# POST /api/channels — create channel validation branches
# ===========================================================================

class TestCreateChannelValidation:
    """Additional POST /api/channels validation paths not covered elsewhere."""

    def test_empty_name_returns_400(self, viewer):
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/channels",
                json={"name": "   "},
                content_type="application/json",
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_custom_channel_without_key_returns_400(self, viewer):
        """Non-hashtag channel name without a key must be rejected."""
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/channels",
                json={"name": "mychannel"},
                content_type="application/json",
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "key" in data["error"].lower()

    def test_channel_key_wrong_length_returns_400(self, viewer):
        """A key shorter than 32 hex chars must be rejected."""
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/channels",
                json={"name": "mychan", "channel_key": "deadbeef"},
                content_type="application/json",
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "32" in data["error"]

    def test_channel_key_non_hex_returns_400(self, viewer):
        """A 32-char key containing non-hex characters must be rejected."""
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/channels",
                json={"name": "mychan", "channel_key": "Z" * 32},
                content_type="application/json",
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "hexadecimal" in data["error"].lower()


# ===========================================================================
# POST /api/channels/validate — with a valid name
# ===========================================================================

class TestChannelValidateRoute:
    """Tests for POST /api/channels/validate."""

    def test_validate_missing_name_returns_400(self, viewer):
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/channels/validate",
                json={},
                content_type="application/json",
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_validate_known_channel_name(self, viewer):
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/channels/validate",
                json={"name": "#general"},
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "exists" in data

    def test_validate_nonexistent_channel_exists_false(self, viewer):
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/channels/validate",
                json={"name": "#channel_that_does_not_exist_xyz"},
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["exists"] is False
        assert data["channel_num"] is None


# ===========================================================================
# POST /api/stream_data — additional type branches
# ===========================================================================

class TestStreamDataTypes:
    """POST /api/stream_data with all supported type values."""

    def test_packet_type_returns_success(self, viewer):
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/stream_data",
                json={"type": "packet", "data": {"raw": "aabbcc"}},
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_mesh_edge_type_returns_success(self, viewer):
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/stream_data",
                json={"type": "mesh_edge", "data": {"from": "aa", "to": "bb"}},
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_mesh_node_type_returns_success(self, viewer):
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/stream_data",
                json={"type": "mesh_node", "data": {"node_id": "cc"}},
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_unknown_type_returns_400(self, viewer):
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/stream_data",
                json={"type": "unknown_type", "data": {}},
                content_type="application/json",
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_missing_body_returns_400(self, viewer):
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/stream_data",
                json={},
                content_type="application/json",
            )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_command_type_returns_success(self, viewer):
        """Verify the already-exercised command type still returns success."""
        with viewer.app.test_client() as c:
            resp = c.post(
                "/api/stream_data",
                json={"type": "command", "data": {"cmd": "ping"}},
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"


# ===========================================================================
# GET /api/maintenance/status — detailed field verification
# ===========================================================================

class TestMaintenanceStatusFields:
    """Verify all expected keys are present in GET /api/maintenance/status."""

    def test_status_contains_all_keys(self, viewer):
        with viewer.app.test_client() as c:
            resp = c.get("/api/maintenance/status")
        assert resp.status_code == 200
        data = resp.get_json()
        expected = {
            "data_retention_ran_at",
            "data_retention_outcome",
            "nightly_email_ran_at",
            "nightly_email_outcome",
            "db_backup_ran_at",
            "db_backup_outcome",
            "db_backup_path",
            "log_rotation_applied_at",
        }
        assert expected == set(data.keys())

    def test_status_values_are_strings(self, viewer):
        """All values in the status response must be strings (empty or populated)."""
        with viewer.app.test_client() as c:
            resp = c.get("/api/maintenance/status")
        data = resp.get_json()
        for key, value in data.items():
            assert isinstance(value, str), f"Key {key!r} has non-string value: {value!r}"

    def test_status_written_metadata_is_reflected(self, viewer):
        """Metadata written via set_metadata appears in status response."""
        viewer.db_manager.set_metadata(
            "maint.status.db_backup_ran_at", "2026-01-01T02:00:00"
        )
        with viewer.app.test_client() as c:
            resp = c.get("/api/maintenance/status")
        data = resp.get_json()
        assert data["db_backup_ran_at"] == "2026-01-01T02:00:00"


# ===========================================================================
# T1-A: subscribe_packets and subscribe_messages history replay
# ===========================================================================

class TestSubscribePacketsHistoryReplay:
    """subscribe_packets must replay last 50 packet/command/routing rows on connect (T1-A)."""

    def test_subscribe_packets_replays_packet_history(self, socketio_viewer):
        """Packet-type history rows are emitted as packet_data events on subscribe."""
        import json as _json
        import time as _time

        from flask_socketio import SocketIOTestClient

        now = _time.time()
        rows = [
            (now - 10 + i, _json.dumps({"seq": i, "type": "rf_data"}), "packet")
            for i in range(3)
        ]
        _insert_packet_stream_rows(socketio_viewer.db_path, rows)

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_packets")

        received = sio_client.get_received()
        packet_events = [e for e in received if e["name"] == "packet_data"]
        assert len(packet_events) == 3
        seq_values = [e["args"][0]["seq"] for e in packet_events]
        assert seq_values == [0, 1, 2]

    def test_subscribe_packets_replays_command_as_command_data(self, socketio_viewer):
        """Command-type rows in packet_stream are replayed as command_data events."""
        import json as _json
        import time as _time

        from flask_socketio import SocketIOTestClient

        now = _time.time()
        rows = [
            (now - 5, _json.dumps({"command": "ping", "seq": 0}), "command"),
        ]
        _insert_packet_stream_rows(socketio_viewer.db_path, rows)

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_packets")

        received = sio_client.get_received()
        command_events = [e for e in received if e["name"] == "command_data"]
        assert len(command_events) == 1

    def test_subscribe_packets_excludes_message_type(self, socketio_viewer):
        """Message-type rows are NOT included in subscribe_packets replay."""
        import json as _json
        import time as _time

        from flask_socketio import SocketIOTestClient

        now = _time.time()
        rows = [
            (now - 5, _json.dumps({"content": "hello", "seq": 0}), "message"),
        ]
        _insert_packet_stream_rows(socketio_viewer.db_path, rows)

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_packets")

        received = sio_client.get_received()
        packet_events = [e for e in received if e["name"] == "packet_data"]
        message_events = [e for e in received if e["name"] == "message_data"]
        assert len(packet_events) == 0
        assert len(message_events) == 0

    def test_subscribe_packets_empty_history(self, socketio_viewer):
        """subscribe_packets with no history emits only status, no packet_data."""
        from flask_socketio import SocketIOTestClient

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_packets")

        received = sio_client.get_received()
        packet_events = [e for e in received if e["name"] == "packet_data"]
        assert len(packet_events) == 0

    def test_subscribe_packets_sets_subscription_flag(self, socketio_viewer):
        """subscribed_packets flag is set to True after subscribe event."""
        from flask_socketio import SocketIOTestClient

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_packets")

        with socketio_viewer._clients_lock:
            flags = [
                v.get("subscribed_packets", False)
                for v in socketio_viewer.connected_clients.values()
            ]
        assert any(flags), "At least one client should have subscribed_packets=True"

    def test_subscribe_packets_emits_status_ack(self, socketio_viewer):
        """subscribe_packets always emits a status acknowledgement."""
        from flask_socketio import SocketIOTestClient

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_packets")

        received = sio_client.get_received()
        status_events = [e for e in received if e["name"] == "status"]
        assert len(status_events) >= 1

    def test_subscribe_packets_routing_type_replayed(self, socketio_viewer):
        """Routing-type rows are replayed as packet_data events."""
        import json as _json
        import time as _time

        from flask_socketio import SocketIOTestClient

        now = _time.time()
        rows = [
            (now - 5, _json.dumps({"route": "aa->bb", "seq": 0}), "routing"),
        ]
        _insert_packet_stream_rows(socketio_viewer.db_path, rows)

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_packets")

        received = sio_client.get_received()
        packet_events = [e for e in received if e["name"] == "packet_data"]
        assert len(packet_events) == 1

    def test_subscribe_packets_chronological_order(self, socketio_viewer):
        """Replayed rows are in chronological order (oldest first)."""
        import json as _json
        import time as _time

        from flask_socketio import SocketIOTestClient

        now = _time.time()
        rows = [
            (now - 20, _json.dumps({"seq": 0}), "packet"),
            (now - 10, _json.dumps({"seq": 1}), "packet"),
            (now - 5, _json.dumps({"seq": 2}), "packet"),
        ]
        _insert_packet_stream_rows(socketio_viewer.db_path, rows)

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_packets")

        received = sio_client.get_received()
        packet_events = [e for e in received if e["name"] == "packet_data"]
        seq_values = [e["args"][0]["seq"] for e in packet_events]
        assert seq_values == [0, 1, 2]


class TestSubscribeMessagesHistoryReplay:
    """subscribe_messages must replay last 50 message rows on connect (T1-A)."""

    def test_subscribe_messages_replays_history(self, socketio_viewer):
        """Message-type history rows are emitted as message_data events on subscribe."""
        import json as _json
        import time as _time

        from flask_socketio import SocketIOTestClient

        now = _time.time()
        rows = [
            (now - 10 + i, _json.dumps({"content": f"hello{i}", "seq": i}), "message")
            for i in range(4)
        ]
        _insert_packet_stream_rows(socketio_viewer.db_path, rows)

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_messages")

        received = sio_client.get_received()
        message_events = [e for e in received if e["name"] == "message_data"]
        assert len(message_events) == 4
        seq_values = [e["args"][0]["seq"] for e in message_events]
        assert seq_values == [0, 1, 2, 3]

    def test_subscribe_messages_excludes_packet_type(self, socketio_viewer):
        """Packet-type rows are NOT replayed on subscribe_messages."""
        import json as _json
        import time as _time

        from flask_socketio import SocketIOTestClient

        now = _time.time()
        rows = [
            (now - 5, _json.dumps({"seq": 0}), "packet"),
        ]
        _insert_packet_stream_rows(socketio_viewer.db_path, rows)

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_messages")

        received = sio_client.get_received()
        message_events = [e for e in received if e["name"] == "message_data"]
        assert len(message_events) == 0

    def test_subscribe_messages_excludes_command_type(self, socketio_viewer):
        """Command-type rows are NOT replayed on subscribe_messages."""
        import json as _json
        import time as _time

        from flask_socketio import SocketIOTestClient

        now = _time.time()
        rows = [
            (now - 5, _json.dumps({"command": "ping"}), "command"),
        ]
        _insert_packet_stream_rows(socketio_viewer.db_path, rows)

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_messages")

        received = sio_client.get_received()
        message_events = [e for e in received if e["name"] == "message_data"]
        assert len(message_events) == 0

    def test_subscribe_messages_empty_history(self, socketio_viewer):
        """subscribe_messages with no history emits only status, no message_data."""
        from flask_socketio import SocketIOTestClient

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_messages")

        received = sio_client.get_received()
        message_events = [e for e in received if e["name"] == "message_data"]
        assert len(message_events) == 0

    def test_subscribe_messages_sets_subscription_flag(self, socketio_viewer):
        """subscribed_messages flag is set to True after subscribe event."""
        from flask_socketio import SocketIOTestClient

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_messages")

        with socketio_viewer._clients_lock:
            flags = [
                v.get("subscribed_messages", False)
                for v in socketio_viewer.connected_clients.values()
            ]
        assert any(flags), "At least one client should have subscribed_messages=True"

    def test_subscribe_messages_emits_status_ack(self, socketio_viewer):
        """subscribe_messages always emits a status acknowledgement."""
        from flask_socketio import SocketIOTestClient

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_messages")

        received = sio_client.get_received()
        status_events = [e for e in received if e["name"] == "status"]
        assert len(status_events) >= 1

    def test_subscribe_messages_chronological_order(self, socketio_viewer):
        """Replayed message rows are in chronological order."""
        import json as _json
        import time as _time

        from flask_socketio import SocketIOTestClient

        now = _time.time()
        rows = [
            (now - 30, _json.dumps({"seq": 0}), "message"),
            (now - 15, _json.dumps({"seq": 1}), "message"),
            (now - 5, _json.dumps({"seq": 2}), "message"),
        ]
        _insert_packet_stream_rows(socketio_viewer.db_path, rows)

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_messages")

        received = sio_client.get_received()
        message_events = [e for e in received if e["name"] == "message_data"]
        seq_values = [e["args"][0]["seq"] for e in message_events]
        assert seq_values == [0, 1, 2]

    def test_subscribe_messages_limit_50(self, socketio_viewer):
        """Only up to 50 message rows are replayed."""
        import json as _json
        import time as _time

        from flask_socketio import SocketIOTestClient

        now = _time.time()
        rows = [
            (now - (100 - i), _json.dumps({"seq": i}), "message")
            for i in range(60)
        ]
        _insert_packet_stream_rows(socketio_viewer.db_path, rows)

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_messages")

        received = sio_client.get_received()
        message_events = [e for e in received if e["name"] == "message_data"]
        assert len(message_events) <= 50


# ===========================================================================
# TASK-16: db_path resolved relative to config file directory (BUG-023 regression)
# ===========================================================================

class TestDbPathResolutionFromConfigDir:
    """BotDataViewer must resolve db_path relative to the config file's parent directory,
    matching core.py's bot_root = Path(config_file).parent.resolve(), not relative to
    the hardcoded code root (2 dirs above app.py).  This was the root cause of the blank
    realtime monitor when config.ini lived outside the project tree."""

    def _make_viewer(self, tmp_path: Path, db_rel: str = "meshcore_bot.db") -> BotDataViewer:
        """Create a BotDataViewer whose config lives in tmp_path with a relative db_path."""
        config_dir = tmp_path / "deployment"
        config_dir.mkdir()
        cfg = configparser.ConfigParser()
        cfg["Connection"] = {"connection_type": "serial", "serial_port": "/dev/ttyUSB0"}
        cfg["Bot"] = {"bot_name": "TestBot", "db_path": db_rel, "prefix_bytes": "1"}
        cfg["Channels"] = {"monitor_channels": "general"}
        cfg["Path_Command"] = {"max_hops": "5", "timeout": "30"}
        config_path = str(config_dir / "config.ini")
        with open(config_path, "w") as fh:
            cfg.write(fh)
        with (
            patch.object(BotDataViewer, "_start_database_polling", lambda self: None),
            patch.object(BotDataViewer, "_start_log_tailing", lambda self: None),
            patch.object(BotDataViewer, "_start_cleanup_scheduler", lambda self: None),
        ):
            return BotDataViewer(config_path=config_path)

    def test_relative_db_path_resolved_to_config_dir(self, tmp_path: Path) -> None:
        """Relative [Bot] db_path is joined to the config file's parent, not the code root."""
        v = self._make_viewer(tmp_path, db_rel="meshcore_bot.db")
        expected = str((tmp_path / "deployment" / "meshcore_bot.db").resolve())
        assert v.db_path == expected

    def test_absolute_db_path_unchanged(self, tmp_path: Path) -> None:
        """Absolute db_path is kept as-is regardless of config location."""
        abs_dir = tmp_path / "absolute"
        abs_dir.mkdir()
        abs_db = str(abs_dir / "bot.db")
        v = self._make_viewer(tmp_path, db_rel=abs_db)
        assert v.db_path == abs_db

    def test_db_path_differs_from_code_root_when_config_elsewhere(self, tmp_path: Path) -> None:
        """When config.ini is not in the code root, the resolved db_path must NOT point
        inside the code root (the old, broken behaviour)."""
        v = self._make_viewer(tmp_path, db_rel="bot.db")
        code_root = Path(
            __file__  # tests/test_web_viewer.py
        ).parent.parent.resolve()  # project root
        # The resolved db_path must be under tmp_path/deployment, not under the code root
        assert not v.db_path.startswith(str(code_root)), (
            f"db_path {v.db_path!r} still resolves inside the code root {code_root} — "
            "the config_base fix is not working"
        )

    def test_startup_logs_db_path_at_info(self, tmp_path: Path) -> None:
        """BotDataViewer logs the resolved database path at INFO level on startup.

        _setup_logging() calls handlers.clear() before adding its own handlers, so
        any handler attached before __init__ is removed.  We patch _setup_logging to
        add a capture handler immediately after the original runs so we see log records
        emitted during the rest of __init__.
        """
        import logging as _logging

        logged: list[str] = []

        class _Capture(_logging.Handler):
            def emit(self, record: _logging.LogRecord) -> None:
                logged.append(record.getMessage())

        original_setup_logging = BotDataViewer._setup_logging
        capture_handler = _Capture(level=_logging.INFO)

        def patched_setup_logging(viewer_self: BotDataViewer) -> None:  # type: ignore[type-arg]
            original_setup_logging(viewer_self)
            viewer_self.logger.addHandler(capture_handler)

        with patch.object(BotDataViewer, "_setup_logging", patched_setup_logging):
            self._make_viewer(tmp_path)

        assert any("Using database:" in msg for msg in logged), (
            f"Expected 'Using database:' INFO log at startup; got: {logged}"
        )


# ===========================================================================
# Security: Restore endpoint path traversal prevention (GAP W1)
# ===========================================================================


class TestRestoreEndpointSecurity:
    """POST /api/maintenance/restore must reject path traversal attacks.

    Covers GAP W1: validate_safe_path() added before shutil.copy2().
    Dangerous system directories and traversal patterns must return 400.
    """

    def test_etc_passwd_path_rejected(self, client):
        """/etc/passwd supplied as db_file must be blocked."""
        resp = client.post(
            "/api/maintenance/restore",
            json={"db_file": "/etc/passwd"},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_etc_shadow_path_rejected(self, client):
        """/etc/shadow must be blocked (credential file)."""
        resp = client.post(
            "/api/maintenance/restore",
            json={"db_file": "/etc/shadow"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_proc_self_environ_rejected(self, client):
        """/proc/self/environ must be blocked (process secrets)."""
        resp = client.post(
            "/api/maintenance/restore",
            json={"db_file": "/proc/self/environ"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_empty_db_file_rejected(self, client):
        """Empty db_file must return 400."""
        resp = client.post(
            "/api/maintenance/restore",
            json={"db_file": ""},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_db_file_key_rejected(self, client):
        """Missing db_file key must return 400."""
        resp = client.post(
            "/api/maintenance/restore",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_validate_safe_path_called_for_restore(self, client):
        """validate_safe_path is invoked — not bypassed — in the restore handler."""
        with patch("modules.web_viewer.app.validate_safe_path", return_value=None) as mock_vsp:
            resp = client.post(
                "/api/maintenance/restore",
                json={"db_file": "/tmp/test.db"},
                content_type="application/json",
            )
        mock_vsp.assert_called_once()
        assert resp.status_code == 400  # None returned → path rejected


# ===========================================================================
# Security: Feed preview/test SSRF prevention (GAP W2)
# ===========================================================================


class TestFeedPreviewSecurity:
    """POST /api/feeds/preview and /api/feeds/test must reject SSRF-able URLs.

    Covers GAP W2: validate_external_url() added before any outbound fetch.
    Private IPs, loopback, and file:// schemes must return 400.
    """

    def test_metadata_endpoint_blocked_in_preview(self, client):
        """169.254.169.254 (cloud metadata) must be blocked in feed preview."""
        resp = client.post(
            "/api/feeds/preview",
            json={"feed_url": "http://169.254.169.254/latest/meta-data/", "feed_type": "rss"},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "error" in data

    def test_loopback_blocked_in_preview(self, client):
        """127.0.0.1 loopback must be blocked in feed preview."""
        resp = client.post(
            "/api/feeds/preview",
            json={"feed_url": "http://127.0.0.1/internal", "feed_type": "rss"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_file_scheme_blocked_in_preview(self, client):
        """file:// scheme must be blocked in feed preview."""
        resp = client.post(
            "/api/feeds/preview",
            json={"feed_url": "file:///etc/passwd", "feed_type": "rss"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_private_network_blocked_in_preview(self, client):
        """10.x.x.x (private RFC1918) must be blocked in feed preview."""
        resp = client.post(
            "/api/feeds/preview",
            json={"feed_url": "http://10.0.0.1/feed.xml", "feed_type": "rss"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_feed_url_rejected(self, client):
        """Missing feed_url key must return 400."""
        resp = client.post(
            "/api/feeds/preview",
            json={"feed_type": "rss"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_metadata_endpoint_blocked_in_test_route(self, client):
        """169.254.169.254 must be blocked in /api/feeds/test as well."""
        resp = client.post(
            "/api/feeds/test",
            json={"url": "http://169.254.169.254/metadata"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_loopback_blocked_in_test_route(self, client):
        """127.0.0.1 must be blocked in /api/feeds/test."""
        resp = client.post(
            "/api/feeds/test",
            json={"url": "http://127.0.0.1/internal"},
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_validate_external_url_called_for_preview(self, client):
        """validate_external_url is invoked — not bypassed — in the preview handler."""
        with patch("modules.web_viewer.app.validate_external_url", return_value=False) as mock_veu:
            resp = client.post(
                "/api/feeds/preview",
                json={"feed_url": "http://192.168.1.1/feed.rss", "feed_type": "rss"},
                content_type="application/json",
            )
        mock_veu.assert_called()
        assert resp.status_code == 400


class TestRadioDebugConfig:
    """Tests for GET/POST /api/config/radio-debug endpoints."""

    def test_get_returns_200_and_shape(self, client):
        resp = client.get("/api/config/radio-debug")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "meta" in data
        assert "config_ini" in data
        assert "enabled" in data["meta"]
        assert "enabled" in data["config_ini"]

    def test_get_reflects_metadata(self, client, viewer):
        viewer.db_manager.set_metadata("radio.debug", "true")
        resp = client.get("/api/config/radio-debug")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["meta"]["enabled"] == "true"
        # cleanup
        viewer.db_manager.set_metadata("radio.debug", "false")

    def test_post_save_enabled(self, client, viewer):
        resp = client.post(
            "/api/config/radio-debug",
            json={"enabled": "true"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert viewer.db_manager.get_metadata("radio.debug") == "true"
        # cleanup
        viewer.db_manager.set_metadata("radio.debug", "false")

    def test_post_save_disabled(self, client, viewer):
        viewer.db_manager.set_metadata("radio.debug", "true")
        resp = client.post(
            "/api/config/radio-debug",
            json={"enabled": "false"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert viewer.db_manager.get_metadata("radio.debug") == "false"

    def test_post_reconnect_queues_operation(self, client, viewer):
        resp = client.post(
            "/api/config/radio-debug",
            json={"enabled": "true", "reconnect": "true"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["op_id"] is not None
        # cleanup
        viewer.db_manager.set_metadata("radio.debug", "false")

    def test_post_no_reconnect_returns_no_op_id(self, client):
        resp = client.post(
            "/api/config/radio-debug",
            json={"enabled": "false", "reconnect": "false"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["op_id"] is None
