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
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

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


@contextmanager
def _temp_config_option(config, section: str, option: str, value: str):
    """Set a config option for the body of a test, restoring prior state after.

    Patching ConfigParser.get is not a substitute: getboolean/getint delegate to
    it internally, so a stubbed get breaks every other option read too.
    """
    added_section = not config.has_section(section)
    if added_section:
        config.add_section(section)
    had_option = config.has_option(section, option)
    previous = config.get(section, option) if had_option else None
    config.set(section, option, value)
    try:
        yield
    finally:
        if added_section:
            config.remove_section(section)
        elif had_option:
            config.set(section, option, previous)
        else:
            config.remove_option(section, option)


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
        patch.object(BotDataViewer, "_start_dashboard_refresher", lambda self: None),
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
        patch.object(BotDataViewer, "_start_dashboard_refresher", lambda self: None),
    ):
        v = BotDataViewer(db_path=db_path, config_path=config_path)

    v.app.config["TESTING"] = True
    yield v


@pytest.fixture
def auth_client(auth_viewer):
    with auth_viewer.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def cleanup_sqlite_connections(monkeypatch):
    """Track and close SQLite connections opened during each test."""
    tracked_connections = []
    original_connect = sqlite3.connect

    def _tracked_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        tracked_connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", _tracked_connect)
    yield
    for conn in tracked_connections:
        try:
            conn.close()
        except sqlite3.Error:
            pass


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

    def test_index_loads_the_external_dashboard_script(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert 'js/dashboard.js' in resp.data.decode()

    def test_dashboard_does_not_subscribe_to_live_streams(self, client):
        """The live feed moved to /realtime; the dashboard must not re-add it.

        It opened three SocketIO subscriptions and re-rendered on every packet,
        duplicating a page that already exists, so the dashboard now costs one
        snapshot read per poll and nothing else.
        """
        html = client.get("/").data.decode()
        script = (
            Path(__file__).resolve().parents[1]
            / "modules" / "web_viewer" / "static" / "js" / "dashboard.js"
        ).read_text(encoding="utf-8")
        for marker in ("live-feed", "live-filter-cb", "live-scroll-top"):
            assert marker not in html, marker
        for marker in ("subscribe_packets", "subscribe_commands", "subscribe_messages"):
            assert marker not in script, marker

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

    def test_stats_page_removed(self, client):
        """The orphaned /stats stub page was folded into the dashboard."""
        resp = client.get("/stats")
        assert resp.status_code == 404

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
        assert "pagination" not in data

    def test_api_contacts_since_7d(self, client):
        resp = client.get("/api/contacts?since=7d")
        assert resp.status_code == 200

    def test_api_contacts_since_all(self, client):
        resp = client.get("/api/contacts?since=all")
        assert resp.status_code == 200

    def test_api_contacts_invalid_since_uses_default(self, client):
        resp = client.get("/api/contacts?since=forever")
        assert resp.status_code == 200

    def test_api_contacts_paginates_searches_and_sorts(self, client, viewer):
        contacts = [
            ("f001" * 16, "PerfContact Charlie"),
            ("f002" * 16, "PerfContact Alpha"),
            ("f003" * 16, "PerfContact Bravo"),
        ]
        for public_key, name in contacts:
            _insert_contact(viewer, public_key, name)

        first = client.get(
            "/api/contacts?since=all&page=1&page_size=2&search=PerfContact"
            "&sort=username&direction=asc"
        ).get_json()
        assert "filtered_stats" in first
        assert first["pagination"] == {
            "page": 1,
            "page_size": 2,
            "total_items": 3,
            "total_pages": 2,
            "has_previous": False,
            "has_next": True,
        }
        assert [row["username"] for row in first["tracking_data"]] == [
            "PerfContact Alpha",
            "PerfContact Bravo",
        ]

        second = client.get(
            "/api/contacts?since=all&page=2&page_size=2&search=PerfContact"
            "&sort=username&direction=asc"
        ).get_json()
        assert [row["username"] for row in second["tracking_data"]] == [
            "PerfContact Charlie"
        ]
        assert second["pagination"]["has_previous"] is True
        assert second["pagination"]["has_next"] is False

    def test_api_contacts_caps_page_size_and_normalizes_sort(self, client):
        data = client.get(
            "/api/contacts?page=not-a-number&page_size=9999&sort=not-a-column"
            "&direction=not-a-direction"
        ).get_json()
        assert data["pagination"]["page"] == 1
        assert data["pagination"]["page_size"] == 200

    def test_api_contacts_filters_and_sorts_by_path_bytes(self, client, viewer):
        rows = [
            ("d801" * 16, "PathBytes One", 1),
            ("d802" * 16, "PathBytes Three", 3),
            ("d803" * 16, "PathBytes Two", 2),
        ]
        for public_key, name, _bytes_per_hop in rows:
            _insert_contact(viewer, public_key, name)
        with closing(sqlite3.connect(viewer.db_path)) as conn:
            for public_key, _name, bytes_per_hop in rows:
                conn.execute(
                    """UPDATE complete_contact_tracking
                       SET out_bytes_per_hop = ?, role = ?, hop_count = ?, city = ?, is_starred = ?
                       WHERE public_key = ?""",
                    (
                        bytes_per_hop,
                        'repeater' if bytes_per_hop == 2 else 'companion',
                        2 if bytes_per_hop == 2 else 0,
                        'Test City' if bytes_per_hop == 2 else None,
                        1 if bytes_per_hop == 2 else 0,
                        public_key,
                    ),
                )
            conn.commit()

        filtered = client.get(
            "/api/contacts",
            query_string={
                "since": "all", "page": 1, "page_size": 10,
                "search": "PathBytes", "path_bytes": "2", "sort": "path_bytes",
                "direction": "asc",
            },
        ).get_json()
        assert [row["username"] for row in filtered["tracking_data"]] == ["PathBytes Two"]
        assert filtered["tracking_data"][0]["path_bytes_per_hop"] == 2

        combined_filters = client.get(
            "/api/contacts",
            query_string={
                "since": "all", "page": 1, "page_size": 10, "search": "PathBytes",
                "device_role": "repeater", "hop_filter": "2", "location_filter": "known",
                "starred": "yes",
            },
        ).get_json()
        assert [row["username"] for row in combined_filters["tracking_data"]] == ["PathBytes Two"]

        sorted_rows = client.get(
            "/api/contacts",
            query_string={
                "since": "all", "page": 1, "page_size": 10,
                "search": "PathBytes", "sort": "path_bytes", "direction": "desc",
            },
        ).get_json()["tracking_data"]
        assert [row["path_bytes_per_hop"] for row in sorted_rows] == [3, 2, 1]

    def test_api_contacts_path_bytes_ignores_invalid_observed_encodings(self, client, viewer):
        """NULL/invalid observed_paths.bytes_per_hop must not become 1-byte.

        Those rows should be ignored so the contact falls back to out_bytes_per_hop.
        """
        public_key = "d804" * 16
        _insert_contact(viewer, public_key, "PathBytes Fallback")
        with closing(sqlite3.connect(viewer.db_path)) as conn:
            conn.execute(
                """UPDATE complete_contact_tracking
                   SET out_bytes_per_hop = 3, last_heard = datetime('now', 'localtime')
                   WHERE public_key = ?""",
                (public_key,),
            )
            conn.execute(
                """INSERT INTO observed_paths
                   (public_key, from_prefix, to_prefix, path_hex, path_length,
                    bytes_per_hop, packet_type, first_seen, last_seen, observation_count)
                   VALUES (?, 'abcd', 'ef01', 'abcdef01', 4, NULL, 'advert',
                           datetime('now', 'localtime'), datetime('now', 'localtime'), 1)""",
                (public_key,),
            )
            conn.commit()

        rows = client.get(
            "/api/contacts",
            query_string={
                "since": "all", "page": 1, "page_size": 10,
                "search": "PathBytes Fallback", "sort": "path_bytes",
            },
        ).get_json()["tracking_data"]
        assert len(rows) == 1
        assert rows[0]["path_bytes_per_hop"] == 3

        filtered = client.get(
            "/api/contacts",
            query_string={
                "since": "all", "page": 1, "page_size": 10,
                "search": "PathBytes Fallback", "path_bytes": "3",
            },
        ).get_json()["tracking_data"]
        assert [row["username"] for row in filtered] == ["PathBytes Fallback"]

        as_one_byte = client.get(
            "/api/contacts",
            query_string={
                "since": "all", "page": 1, "page_size": 10,
                "search": "PathBytes Fallback", "path_bytes": "1",
            },
        ).get_json()["tracking_data"]
        assert as_one_byte == []

    @pytest.mark.parametrize(
        ("sort", "ascending_names"),
        [
            ("username", ["SortMatrix A", "SortMatrix B", "SortMatrix C"]),
            ("device_type", ["SortMatrix B", "SortMatrix C", "SortMatrix A"]),
            ("location", ["SortMatrix B", "SortMatrix C", "SortMatrix A"]),
            ("distance", ["SortMatrix A", "SortMatrix C", "SortMatrix B"]),
            ("snr", ["SortMatrix B", "SortMatrix C", "SortMatrix A"]),
            ("hop_count", ["SortMatrix B", "SortMatrix C", "SortMatrix A"]),
            ("first_heard", ["SortMatrix B", "SortMatrix C", "SortMatrix A"]),
            ("last_seen", ["SortMatrix C", "SortMatrix A", "SortMatrix B"]),
            ("advert_count", ["SortMatrix B", "SortMatrix C", "SortMatrix A"]),
        ],
    )
    @pytest.mark.parametrize("direction", ["asc", "desc"])
    def test_api_contacts_preserves_global_sort_semantics(
        self, client, viewer, monkeypatch, sort, ascending_names, direction
    ):
        with closing(sqlite3.connect(viewer.db_path)) as conn:
            conn.execute(
                "DELETE FROM complete_contact_tracking WHERE name LIKE 'SortMatrix %'"
            )
            conn.commit()

        rows = [
            (
                "d101" * 16, "SortMatrix A", "Zulu", "Zurich", 30.0, 3,
                "2026-01-03 00:00:00", "2026-02-02 00:00:00", 30, 0.1, 0.0,
            ),
            (
                "d102" * 16, "SortMatrix B", "Alpha", "Austin", 10.0, 1,
                "2026-01-01 00:00:00", "2026-02-03 00:00:00", 10, 2.0, 0.0,
            ),
            (
                "d103" * 16, "SortMatrix C", "Middle", "Boston", 20.0, 2,
                "2026-01-02 00:00:00", "2026-02-01 00:00:00", 20, 1.0, 0.0,
            ),
        ]
        for row in rows:
            _insert_contact(viewer, row[0], row[1])
        with closing(sqlite3.connect(viewer.db_path)) as conn:
            for row in rows:
                conn.execute(
                    """
                    UPDATE complete_contact_tracking
                    SET device_type = ?, city = ?, snr = ?, hop_count = ?,
                        first_heard = ?, last_heard = ?, advert_count = ?,
                        latitude = ?, longitude = ?
                    WHERE public_key = ?
                    """,
                    (*row[2:], row[0]),
                )
            conn.commit()

        original_getfloat = viewer.config.getfloat

        def getfloat(section, option, *, fallback=None):
            if section == "Bot" and option in {"bot_latitude", "bot_longitude"}:
                return 0.0
            return original_getfloat(section, option, fallback=fallback)

        monkeypatch.setattr(viewer.config, "getfloat", getfloat)
        data = client.get(
            "/api/contacts",
            query_string={
                "since": "all",
                "page": 1,
                "page_size": 10,
                "search": "SortMatrix",
                "sort": sort,
                "direction": direction,
            },
        ).get_json()
        expected = ascending_names if direction == "asc" else list(reversed(ascending_names))
        assert [row["username"] for row in data["tracking_data"]] == expected

    @pytest.mark.parametrize("sort", ["location", "distance", "snr", "hop_count"])
    def test_api_contacts_sorts_missing_values_as_zero_or_empty(
        self, client, viewer, monkeypatch, sort
    ):
        original_getfloat = viewer.config.getfloat

        def getfloat(section, option, *, fallback=None):
            if section == "Bot" and option in {"bot_latitude", "bot_longitude"}:
                return 0.0
            return original_getfloat(section, option, fallback=fallback)

        monkeypatch.setattr(viewer.config, "getfloat", getfloat)

        with closing(sqlite3.connect(viewer.db_path)) as conn:
            conn.execute(
                "DELETE FROM complete_contact_tracking WHERE name LIKE 'SortNull %'"
            )
            conn.commit()

        rows = [
            ("d201" * 16, "SortNull Missing", None, None, None, None),
            ("d202" * 16, "SortNull Low", "Alpha", 1.0, 1.0, 1),
            ("d203" * 16, "SortNull High", "Zulu", 2.0, 2.0, 2),
        ]
        for public_key, name, *_ in rows:
            _insert_contact(viewer, public_key, name)
        with closing(sqlite3.connect(viewer.db_path)) as conn:
            for public_key, _name, city, latitude, snr, hop_count in rows:
                conn.execute(
                    """
                    UPDATE complete_contact_tracking
                    SET city = ?, latitude = ?, longitude = ?, snr = ?, hop_count = ?
                    WHERE public_key = ?
                    """,
                    (city, latitude, latitude, snr, hop_count, public_key),
                )
            conn.commit()

        data = client.get(
            "/api/contacts",
            query_string={
                "since": "all",
                "page": 1,
                "page_size": 10,
                "search": "SortNull",
                "sort": sort,
                "direction": "asc",
            },
        ).get_json()
        assert [row["username"] for row in data["tracking_data"]][0] == "SortNull Missing"

    def test_api_contacts_empty_and_out_of_range_pages(self, client, viewer):
        empty = client.get(
            "/api/contacts",
            query_string={"page": 999, "page_size": 2, "search": "NoSuchContact"},
        ).get_json()
        assert empty["tracking_data"] == []
        assert empty["pagination"] == {
            "page": 1,
            "page_size": 2,
            "total_items": 0,
            "total_pages": 1,
            "has_previous": False,
            "has_next": False,
        }

        for index in range(3):
            _insert_contact(
                viewer,
                f"d30{index}" * 16,
                f"ClampMatrix {index}",
            )
        clamped = client.get(
            "/api/contacts",
            query_string={
                "since": "all",
                "page": 999,
                "page_size": 2,
                "search": "ClampMatrix",
                "sort": "username",
                "direction": "asc",
            },
        ).get_json()
        assert clamped["pagination"]["page"] == 2
        assert clamped["pagination"]["total_pages"] == 2
        assert [row["username"] for row in clamped["tracking_data"]] == [
            "ClampMatrix 2"
        ]

    def test_api_contacts_search_treats_sql_wildcards_literally(self, client, viewer):
        _insert_contact(viewer, "d401" * 16, "WildcardMatrix 100%")
        _insert_contact(viewer, "d402" * 16, "WildcardMatrix 100X")
        data = client.get(
            "/api/contacts",
            query_string={
                "since": "all",
                "page": 1,
                "page_size": 10,
                "search": "100%",
            },
        ).get_json()
        assert [row["username"] for row in data["tracking_data"]] == [
            "WildcardMatrix 100%"
        ]

    def test_api_contacts_filtered_stats_cover_all_matching_rows(self, client, viewer):
        rows = [
            ("d501" * 16, "StatsMatrix Companion", "Companion Device", "-1 hour", "-1 day"),
            ("d502" * 16, "StatsMatrix Repeater", "Repeater", "-2 days", "-2 days"),
            ("d503" * 16, "StatsMatrix Room", "Room Server", "-10 days", "-10 days"),
        ]
        for public_key, name, *_ in rows:
            _insert_contact(viewer, public_key, name)
        with closing(sqlite3.connect(viewer.db_path)) as conn:
            for public_key, _name, device_type, last_offset, first_offset in rows:
                conn.execute(
                    """
                    UPDATE complete_contact_tracking
                    SET device_type = ?,
                        last_heard = datetime('now', 'localtime', ?),
                        first_heard = datetime('now', 'localtime', ?)
                    WHERE public_key = ?
                    """,
                    (device_type, last_offset, first_offset, public_key),
                )
            conn.commit()

        data = client.get(
            "/api/contacts",
            query_string={
                "since": "30d",
                "page": 1,
                "page_size": 1,
                "search": "StatsMatrix",
            },
        ).get_json()
        assert len(data["tracking_data"]) == 1
        assert data["filtered_stats"] == {
            "contacts_24h": 1,
            "contacts_7d": 2,
            "contacts_total": 3,
            "new_companions": 1,
            "new_repeaters": 1,
            "new_room_servers": 0,
        }

    def test_api_contacts_scopes_path_query_to_visible_page(
        self, client, viewer, monkeypatch
    ):
        _insert_contact(viewer, "f101" * 16, "PerfScoped One")
        _insert_contact(viewer, "f102" * 16, "PerfScoped Two")
        statements = []
        original_get_connection = viewer._get_db_connection

        def traced_connection():
            conn = original_get_connection()
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(viewer, "_get_db_connection", traced_connection)
        data = client.get(
            "/api/contacts?since=all&page=1&page_size=1&search=PerfScoped"
        ).get_json()

        assert len(data["tracking_data"]) == 1
        recent_path_queries = [
            sql for sql in statements
            if "WITH recent_paths AS" in sql
        ]
        assert len(recent_path_queries) == 1
        assert "public_key IN (" in recent_path_queries[0]

    def test_contacts_page_uses_bounded_server_loading(self, client):
        html = client.get("/contacts").get_data(as_text=True)
        assert 'id="contacts-pagination"' in html
        assert 'id="contacts-page-previous"' in html
        assert 'id="contacts-page-next"' in html
        assert "page_size: String(this.pageSize)" in html
        assert "new AbortController()" in html

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

    def test_api_config_notifications_post(self, client, viewer):
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
            "allow_local_smtp": "true",
        }
        resp = client.post(
            "/api/config/notifications",
            json=payload,
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("success") is True
        assert viewer.db_manager.get_metadata("notif.allow_local_smtp") == "true"
        # Reset test state for subsequent notification-security tests.
        viewer.db_manager.set_metadata("notif.allow_local_smtp", "")

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

    def test_api_interrupted_operation_is_terminal_and_exposes_claim_time(
        self, client, viewer
    ):
        with sqlite3.connect(viewer.db_path) as conn:
            cursor = conn.execute(
                """INSERT INTO channel_operations
                   (operation_type, status, error_message, claimed_at, processed_at)
                   VALUES ('remove', 'interrupted', 'Verify device state',
                           CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
            )
            operation_id = cursor.lastrowid
            conn.commit()

        resp = client.get(f"/api/channel-operations/{operation_id}")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "interrupted"
        assert data["error_message"] == "Verify device state"
        assert data["claimed_at"] is not None

    def test_shared_operation_poller_recognizes_interrupted_as_terminal(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "modules/web_viewer/static/js/channel_operations.js"
        ).read_text(encoding="utf-8")

        assert source.count("result.status === 'interrupted'") == 1
        assert source.count("result2.status === 'interrupted'") == 1
        assert source.count("result3.status === 'interrupted'") == 1


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

    def test_notifications_test_rejects_private_smtp_host(self, client, viewer):
        """SSRF guard: RFC 6598 shared/CGN address (100.64.0.0/10) must be rejected."""
        viewer.db_manager.set_metadata('notif.smtp_host', '100.64.0.1')
        viewer.db_manager.set_metadata('notif.smtp_port', '587')
        viewer.db_manager.set_metadata('notif.smtp_security', 'starttls')
        viewer.db_manager.set_metadata('notif.from_email', 'bot@example.com')
        viewer.db_manager.set_metadata('notif.recipients', 'admin@example.com')
        resp = client.post("/api/config/notifications/test")
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'private' in data.get('error', '').lower() or 'reserved' in data.get('error', '').lower()
        # reset
        viewer.db_manager.set_metadata('notif.smtp_host', '')

    def test_notifications_test_rejects_loopback_smtp_host(self, client, viewer):
        """SSRF guard: SMTP host of localhost/loopback must be rejected."""
        viewer.db_manager.set_metadata('notif.smtp_host', '127.0.0.1')
        viewer.db_manager.set_metadata('notif.smtp_port', '25')
        viewer.db_manager.set_metadata('notif.smtp_security', 'none')
        viewer.db_manager.set_metadata('notif.from_email', 'bot@example.com')
        viewer.db_manager.set_metadata('notif.recipients', 'admin@example.com')
        resp = client.post("/api/config/notifications/test")
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'private' in data.get('error', '').lower() or 'reserved' in data.get('error', '').lower()
        # reset
        viewer.db_manager.set_metadata('notif.smtp_host', '')

    def test_notifications_test_allows_local_smtp_when_flag_set(self, client, viewer):
        """allow_local_smtp=true permits private-IP SMTP hosts (e.g. local Postfix)."""
        viewer.db_manager.set_metadata('notif.smtp_host', '127.0.0.1')
        viewer.db_manager.set_metadata('notif.smtp_port', '25')
        viewer.db_manager.set_metadata('notif.smtp_security', 'none')
        viewer.db_manager.set_metadata('notif.from_email', 'bot@example.com')
        viewer.db_manager.set_metadata('notif.recipients', 'admin@example.com')
        viewer.db_manager.set_metadata('notif.allow_local_smtp', 'true')
        resp = client.post("/api/config/notifications/test")
        # Must not be rejected with 400 for private address — may be 200 or 500 (send attempt)
        assert resp.status_code != 400 or 'private' not in (resp.get_json() or {}).get('error', '').lower()
        # reset
        viewer.db_manager.set_metadata('notif.smtp_host', '')
        viewer.db_manager.set_metadata('notif.allow_local_smtp', '')

    def test_notifications_get_includes_allow_local_smtp(self, client):
        """GET /api/config/notifications must return allow_local_smtp field."""
        resp = client.get("/api/config/notifications")
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'allow_local_smtp' in data


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
        with patch("modules.web_viewer.app.validate_external_url", return_value=True):
            resp = client.post(
                "/api/feeds/test",
                data=json.dumps({"url": "https://example.com/feed.rss"}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_post_feeds_test_honors_allow_private_urls_config(self, client, viewer):
        if not viewer.config.has_section("Feed_Command"):
            viewer.config.add_section("Feed_Command")
        had_option = viewer.config.has_option("Feed_Command", "allow_private_urls")
        previous = viewer.config.get("Feed_Command", "allow_private_urls", fallback=None)
        viewer.config.set("Feed_Command", "allow_private_urls", "true")

        with patch("modules.web_viewer.app.validate_external_url", return_value=True) as mock_validate:
            resp = client.post(
                "/api/feeds/test",
                data=json.dumps({"url": "http://127.0.0.1/feed.rss"}),
                content_type="application/json",
            )

        assert resp.status_code == 200
        assert mock_validate.call_args.kwargs.get("allow_private") is True
        # Reset test state for preview-security tests that expect strict default.
        if had_option and previous is not None:
            viewer.config.set("Feed_Command", "allow_private_urls", previous)
        elif viewer.config.has_option("Feed_Command", "allow_private_urls"):
            viewer.config.remove_option("Feed_Command", "allow_private_urls")

    def test_get_feed_activity(self, client):
        resp = client.get("/api/feeds/1/activity")
        assert resp.status_code in (200, 404, 500)

    def test_get_feed_errors(self, client):
        resp = client.get("/api/feeds/1/errors")
        assert resp.status_code in (200, 404, 500)

    def test_post_feed_refresh(self, client):
        resp = client.post("/api/feeds/1/refresh")
        assert resp.status_code in (200, 404, 500)

    # --- Preview formatting parity with feed_manager (Skye feedback) ---

    def test_preview_emoji_field_overrides_heuristic(self, viewer):
        item = {"title": "Road closed", "description": "", "emoji": "🔥"}
        result = viewer._format_feed_item(item, "{emoji}", feed_name="emergency alerts")
        assert result == "🔥"

    def test_preview_emoji_field_absent_falls_back(self, viewer):
        item = {"title": "Road closed", "description": ""}
        result = viewer._format_feed_item(item, "{emoji}", feed_name="emergency alerts")
        assert result == "🚨"

    def test_preview_truncate_hard_no_ellipsis(self, viewer):
        item = {"title": "Roadwork", "description": ""}
        result = viewer._format_feed_item(item, "{title|truncate_hard:4}", feed_name="x")
        assert result == "Road"

    def test_preview_substr_start_and_length(self, viewer):
        item = {"title": "Roadwork ahead", "description": ""}
        result = viewer._format_feed_item(item, "{title|substr:0,4}", feed_name="x")
        assert result == "Road"

    def test_preview_sanitize_strips_control_chars(self, viewer):
        item = {"title": "Hi\x00there", "description": ""}
        result = viewer._format_feed_item(item, "{title}", feed_name="x")
        assert "\x00" not in result
        assert "Hi" in result and "there" in result

    def test_preview_shorten_urls_honors_config(self, viewer, monkeypatch):
        if not viewer.config.has_section("Feed_Manager"):
            viewer.config.add_section("Feed_Manager")
        viewer.config.set("Feed_Manager", "shorten_urls", "true")
        item = {"title": "t", "description": "", "link": "https://example.com/long-path"}

        def _fake_shorten(url, config=None, logger=None):
            return "https://v.gd/ab"

        monkeypatch.setattr(
            "modules.feed_format.shorten_url_sync", _fake_shorten
        )
        result = viewer._format_feed_item(item, "{link}", feed_name="x")
        assert result == "https://v.gd/ab"

    # --- Reset feed errors (global + per-feed) ---

    def _seed_feed_errors(self, viewer):
        """Create two feeds each with one error; return their (real) feed ids."""
        import uuid

        conn = viewer._get_db_connection()
        try:
            cur = conn.cursor()
            ids = []
            for _ in range(2):
                url = f"http://example.com/{uuid.uuid4().hex}.xml"
                cur.execute(
                    "INSERT INTO feed_subscriptions (feed_type, feed_url, channel_name, enabled) "
                    "VALUES ('rss', ?, 'general', 1)",
                    (url,),
                )
                fid = cur.execute("SELECT last_insert_rowid()").fetchone()[0]
                cur.execute(
                    "INSERT INTO feed_errors (feed_id, error_type, error_message) VALUES (?, 'network', 'boom')",
                    (fid,),
                )
                ids.append(fid)
            conn.commit()
            return ids
        finally:
            conn.close()

    def _error_count(self, viewer, feed_id=None):
        conn = viewer._get_db_connection()
        try:
            cur = conn.cursor()
            if feed_id is None:
                cur.execute("SELECT COUNT(*) FROM feed_errors")
            else:
                cur.execute("SELECT COUNT(*) FROM feed_errors WHERE feed_id = ?", (feed_id,))
            return cur.fetchone()[0]
        finally:
            conn.close()

    def test_reset_single_feed_errors(self, client, viewer):
        fid1, fid2 = self._seed_feed_errors(viewer)
        assert self._error_count(viewer, fid1) > 0
        resp = client.post(f"/api/feeds/{fid1}/errors/reset")
        assert resp.status_code == 200
        assert json.loads(resp.data)["success"] is True
        # Target feed cleared, the other untouched
        assert self._error_count(viewer, fid1) == 0
        assert self._error_count(viewer, fid2) > 0

    def test_reset_all_feed_errors(self, client, viewer):
        self._seed_feed_errors(viewer)
        assert self._error_count(viewer) > 0
        resp = client.post("/api/feeds/errors/reset")
        assert resp.status_code == 200
        assert json.loads(resp.data)["success"] is True
        assert self._error_count(viewer) == 0


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
# TASK-01: Radio page — old firmware config card + reboot UI removed.
# The Node Settings card later reintroduced firmware settings (path hash mode)
# with new element ids; only the old card must stay absent.
# ===========================================================================

class TestRadioPageFirmwareRemoval:
    """Assert that the old firmware config card and reboot UI are absent from /radio (TASK-01)."""

    def test_radio_page_loads(self, client):
        resp = client.get("/radio")
        assert resp.status_code == 200

    def test_old_firmware_config_card_absent(self, client):
        resp = client.get("/radio")
        html = resp.data.decode()
        assert 'id="firmware-config"' not in html
        assert "readFirmwareBtn" not in html
        assert "writeFirmwareBtn" not in html
        assert "firmwareStatusAlert" not in html
        assert "firmwareLastRead" not in html
        assert "Firmware Configuration" not in html

    def test_node_settings_card_present(self, client):
        resp = client.get("/radio")
        html = resp.data.decode()
        assert 'id="node-settings-card"' in html
        assert 'id="nodePathHashMode"' in html
        # loop.detect is repeater/room-server CLI config, not a companion
        # setting — it must not appear on this page.
        assert 'nodeLoopDetect' not in html
        assert 'loop_detect' not in html
        assert 'id="nodeName"' in html
        assert 'id="sendAdvertFloodBtn"' in html
        assert 'id="nodeTelemBase"' in html
        assert 'id="nodeRxDelay"' in html

    def test_new_contacts_mode_is_config_managed(self, client):
        """manual_add_contacts is owned by [Bot] auto_manage_contacts — the
        panel shows the scheme read-only instead of offering a device toggle."""
        resp = client.get("/radio")
        html = resp.data.decode()
        assert 'id="nodeManualAdd"' not in html
        assert 'id="nodeNewContactsMode"' in html
        assert "auto_manage_contacts" in html

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
        _patch.object(BotDataViewer, "_start_dashboard_refresher", lambda self: None),
    ):
        v = BotDataViewer(db_path=db_path, config_path=config_path)

    v.app.config["TESTING"] = True
    v.app.config["SECRET_KEY"] = "test-secret"
    yield v
    with v._clients_lock:
        v.connected_clients.clear()


@pytest.fixture
def managed_socketio_clients(monkeypatch, socketio_viewer):
    """Track SocketIO test clients and always disconnect on teardown."""
    import flask_socketio

    created_clients = []
    original_client_cls = flask_socketio.SocketIOTestClient

    class ManagedSocketIOTestClient(original_client_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_clients.append(self)

    monkeypatch.setattr(flask_socketio, "SocketIOTestClient", ManagedSocketIOTestClient)
    yield
    for client in created_clients:
        try:
            if client.is_connected():
                client.disconnect()
        except Exception:
            pass
    with socketio_viewer._clients_lock:
        assert not socketio_viewer.connected_clients


@pytest.mark.usefixtures("managed_socketio_clients")
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

    def test_mesh_graph_is_not_built_at_startup(self, tmp_path):
        """Uses its own viewer: the shared one may already have decoded a path."""
        db_path = str(tmp_path / "startup.db")
        config_path = str(tmp_path / "config.ini")
        _write_config(Path(config_path), db_path)

        factory = Mock()
        with (
            patch("modules.mesh_graph.MeshGraph", factory),
            patch.object(BotDataViewer, "_setup_logging", _fake_setup_logging),
            patch.object(BotDataViewer, "_start_database_polling", lambda self: None),
            patch.object(BotDataViewer, "_start_log_tailing", lambda self: None),
            patch.object(BotDataViewer, "_start_cleanup_scheduler", lambda self: None),
            patch.object(BotDataViewer, "_start_dashboard_refresher", lambda self: None),
        ):
            v = BotDataViewer(db_path=db_path, config_path=config_path)

        assert v.mesh_graph is None
        factory.assert_not_called()

    def test_mesh_graph_is_lazy_singleton_and_read_only(self, viewer, monkeypatch):
        graph = object()
        factory = Mock(return_value=graph)
        monkeypatch.setattr("modules.mesh_graph.MeshGraph", factory)
        viewer.mesh_graph = None
        try:
            assert viewer._get_mesh_graph() is graph
            assert viewer._get_mesh_graph() is graph
            # capture=False keeps edge writes and the batch-writer thread in the
            # bot process, which owns capture.
            factory.assert_called_once_with(viewer._mesh_graph_bot, capture=False)
        finally:
            viewer.mesh_graph = None

    def test_path_decoding_passes_mesh_graph_to_shared_engine(self, viewer, monkeypatch):
        """Without the graph, decoding silently disagrees with the bot's `path` command."""
        graph = object()
        monkeypatch.setattr(
            "modules.mesh_graph.MeshGraph", Mock(return_value=graph)
        )
        decode = Mock(return_value=[])
        monkeypatch.setattr("modules.path_inference.decode_path_nodes", decode)
        viewer.mesh_graph = None
        try:
            viewer._decode_path_hex("aabb")
            assert decode.call_args.kwargs["mesh_graph"] is graph

            decode.reset_mock()
            viewer._resolve_path("aabb")
            assert decode.call_args.kwargs["mesh_graph"] is graph
        finally:
            viewer.mesh_graph = None

    def test_path_decoding_degrades_when_mesh_graph_fails_to_load(self, viewer, monkeypatch):
        monkeypatch.setattr(
            "modules.mesh_graph.MeshGraph", Mock(side_effect=RuntimeError("no table"))
        )
        decode = Mock(return_value=[])
        monkeypatch.setattr("modules.path_inference.decode_path_nodes", decode)
        viewer.mesh_graph = None
        try:
            viewer._decode_path_hex("aabb")
            assert decode.call_args.kwargs["mesh_graph"] is None
        finally:
            viewer.mesh_graph = None

    def test_missing_migrations_fail_at_startup_not_in_a_request(self, tmp_path):
        """RepeaterManager is lazy now, so the viewer must validate up front.

        Otherwise a migration problem first surfaces as a 500 from whichever
        request happens to need the manager.
        """
        db_path = str(tmp_path / "unmigrated.db")
        config_path = str(tmp_path / "config.ini")
        _write_config(Path(config_path), db_path)

        boom = Mock(side_effect=RuntimeError("Missing repeater/graph database tables: purging_log"))
        with (
            patch("modules.web_viewer.app.validate_repeater_tables", boom),
            patch.object(BotDataViewer, "_setup_logging", _fake_setup_logging),
            patch.object(BotDataViewer, "_start_database_polling", lambda self: None),
            patch.object(BotDataViewer, "_start_log_tailing", lambda self: None),
            patch.object(BotDataViewer, "_start_cleanup_scheduler", lambda self: None),
            patch.object(BotDataViewer, "_start_dashboard_refresher", lambda self: None),
            pytest.raises(RuntimeError, match="Missing repeater/graph database tables"),
        ):
            BotDataViewer(db_path=db_path, config_path=config_path)

    def test_repeater_manager_is_lazy_and_singleton(self, viewer, monkeypatch):
        manager = object()
        factory = Mock(return_value=manager)
        monkeypatch.setattr("modules.web_viewer.app.RepeaterManager", factory)
        viewer.repeater_manager = None
        try:
            assert viewer._get_repeater_manager() is manager
            assert viewer._get_repeater_manager() is manager
            factory.assert_called_once_with(viewer._repeater_manager_bot)
        finally:
            viewer.repeater_manager = None

    def test_live_stream_polling_is_idle_without_relevant_subscribers(self, viewer):
        with viewer._clients_lock:
            original_clients = dict(viewer.connected_clients)
            viewer.connected_clients.clear()
            viewer.connected_clients["mesh-only"] = {"subscribed_mesh": True}
        try:
            assert viewer._has_live_stream_subscribers() is False
            with viewer._clients_lock:
                viewer.connected_clients["packet-client"] = {
                    "subscribed_packets": True
                }
            assert viewer._has_live_stream_subscribers() is True
        finally:
            with viewer._clients_lock:
                viewer.connected_clients.clear()
                viewer.connected_clients.update(original_clients)

    def test_viewer_journal_mode_is_initialized_once(self, viewer, monkeypatch):
        statements = []
        real_connect = sqlite3.connect

        class TracingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                statements.append(sql)
                return super().execute(sql, parameters)

        def tracing_connect(*args, **kwargs):
            kwargs["factory"] = TracingConnection
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", tracing_connect)
        viewer.db_manager._journal_mode_initialized.discard("Web_Viewer")
        for _ in range(3):
            with closing(viewer._get_db_connection()):
                pass

        journal_statements = [
            sql for sql in statements if sql.upper().startswith("PRAGMA JOURNAL_MODE=")
        ]
        foreign_key_statements = [
            sql for sql in statements if sql.upper().startswith("PRAGMA FOREIGN_KEYS=")
        ]
        assert journal_statements == ["PRAGMA journal_mode=WAL"]
        assert len(foreign_key_statements) == 3

    def test_viewer_connections_use_the_shared_dbmanager_implementation(self, viewer):
        """The viewer must not carry its own copy of the pragma logic."""
        conn = Mock()
        with patch.object(viewer.db_manager, "_apply_sqlite_pragmas") as shared:
            viewer._configure_db_connection(conn)
        shared.assert_called_once_with(conn, for_web_viewer=True)

        # The DBManager it delegates to must own the same file every viewer
        # connection opens, or its journal-mode bookkeeping would be tracking
        # a different database.
        assert str(viewer.db_manager.db_path) == str(viewer.db_path)

    def test_rollback_journal_mode_is_applied_to_every_connection(self, viewer):
        """DELETE/TRUNCATE/PERSIST/MEMORY/OFF are connection-local, not persistent.

        Caching them like WAL would leave every connection after the first
        silently running SQLite's default mode instead of the configured one.
        """
        applied = []
        conn = Mock()
        conn.execute.side_effect = lambda sql: applied.append(sql)
        viewer.db_manager._journal_mode_initialized.discard("Web_Viewer")

        with _temp_config_option(
            viewer.config, "Web_Viewer", "sqlite_journal_mode", "MEMORY"
        ):
            viewer._configure_db_connection(conn)
            viewer._configure_db_connection(conn)

        assert [s for s in applied if s.upper().startswith("PRAGMA JOURNAL_MODE=")] == [
            "PRAGMA journal_mode=MEMORY",
            "PRAGMA journal_mode=MEMORY",
        ]
        # A non-persistent mode must not consume the WAL-only fast path.
        assert "Web_Viewer" not in viewer.db_manager._journal_mode_initialized

    def test_invalid_journal_mode_warns_once_and_falls_back(self, viewer):
        """The mode is read per connection now, so the warning must not repeat."""
        viewer.db_manager._journal_mode_warned.discard("Web_Viewer")
        viewer.db_manager._journal_mode_initialized.discard("Web_Viewer")
        applied = []
        conn = Mock()
        conn.execute.side_effect = lambda sql: applied.append(sql)
        fake_logger = Mock()
        try:
            with (
                _temp_config_option(
                    viewer.config, "Web_Viewer", "sqlite_journal_mode", "BOGUS"
                ),
                patch.object(viewer.db_manager, "logger", fake_logger),
            ):
                for _ in range(3):
                    viewer._configure_db_connection(conn)
        finally:
            viewer.db_manager._journal_mode_warned.discard("Web_Viewer")

        assert fake_logger.warning.call_count == 1
        assert "BOGUS" in str(fake_logger.warning.call_args)
        # Falls back to WAL rather than sending an invalid pragma to SQLite.
        assert [s for s in applied if s.upper().startswith("PRAGMA JOURNAL_MODE=")] == [
            "PRAGMA journal_mode=WAL"
        ]


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
        assert 'id="health-clients-link"' in html


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

    def test_returns_503_when_no_maintenance_runner(self, viewer):
        """Returns 503 when the viewer's maintenance runner is unavailable."""
        with patch.object(viewer, '_maintenance_runner', None):
            with viewer.app.test_client() as c:
                resp = c.post("/api/maintenance/backup_now")
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["success"] is False
        assert "error" in data  # Error details sanitized in 5xx responses

    def test_returns_200_on_successful_backup(self, viewer, tmp_path):
        """The standalone viewer performs a real SQLite backup."""
        backup_dir = tmp_path / "backups"
        viewer.db_manager.set_metadata('maint.db_backup_dir', str(backup_dir))

        with viewer.app.test_client() as c:
            resp = c.post("/api/maintenance/backup_now")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert Path(data["path"]).is_file()
        assert Path(data["path"]).parent == backup_dir

    def test_returns_500_on_backup_error(self, viewer):
        """Returns 500 with success=False when backup writes an error outcome."""
        def fake_run_db_backup():
            viewer.db_manager.set_metadata('maint.status.db_backup_path', '')
            viewer.db_manager.set_metadata(
                'maint.status.db_backup_outcome', 'error: cannot create dir'
            )

        with patch.object(viewer._maintenance_runner, 'run_db_backup', fake_run_db_backup):
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
        assert "sqlite" in resp.get_json()["error"].lower()

    def test_valid_sqlite_restore_is_staged_without_replacing_active_db(self, viewer, tmp_path):
        """Returns 202 and stages a valid MeshCore backup for service restart."""
        import sqlite3 as _sql
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        viewer.db_manager.set_metadata('maint.db_backup_dir', str(backup_dir))
        backup = backup_dir / "backup.db"
        with _sql.connect(viewer.db_path) as source_conn:
            with _sql.connect(str(backup)) as destination_conn:
                source_conn.backup(destination_conn)
        # Patch db_path to a temp destination and verify the request does not create it.
        dest = tmp_path / "restored.db"
        with patch.object(viewer, "db_path", str(dest)):
            with viewer.app.test_client() as c:
                resp = c.post("/api/maintenance/restore",
                              json={"db_file": str(backup)},
                              content_type="application/json")
        assert resp.status_code == 202
        data = resp.get_json()
        assert data["success"] is True
        assert data["requires_restart"] is True
        assert data["pending_path"] == str(tmp_path / ".restored.db.restore-pending")
        assert not dest.exists()
        assert Path(data["pending_path"]).exists()
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

    @pytest.fixture()
    def bot_integration_tiny_queue(self, tmp_path):
        """BotIntegration with packet_stream_write_queue_max=2 for bounded-queue tests."""
        import configparser as _cp
        from unittest.mock import MagicMock

        db_path = str(tmp_path / "test_tiny.db")
        cfg = _cp.ConfigParser()
        cfg["Connection"] = {"connection_type": "serial", "serial_port": "/dev/null"}
        cfg["Bot"] = {"bot_name": "Test", "db_path": db_path, "prefix_bytes": "1"}
        cfg["Web_Viewer"] = {"packet_stream_write_queue_max": "2"}

        bot = MagicMock()
        bot.logger = logging.getLogger("test_integration_tiny")
        bot.logger.addHandler(logging.NullHandler())
        bot.config = cfg
        bot.bot_root = str(tmp_path)

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

    def test_default_write_queue_maxsize(self, bot_integration):
        assert bot_integration._write_queue_maxsize == 1000
        assert bot_integration._write_queue.maxsize == 1000

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

    def test_bounded_queue_retry_flush_frees_space(self, bot_integration_tiny_queue, monkeypatch):
        """When the queue is full, insert triggers a flush so the row can be queued."""
        from modules.web_viewer.integration import BotIntegration

        bi = bot_integration_tiny_queue
        assert bi._write_queue_maxsize == 2
        bi._drain_stop.set()
        if bi._drain_thread:
            bi._drain_thread.join(timeout=2.0)
        monkeypatch.setattr(BotIntegration, "_WRITE_QUEUE_PUT_TIMEOUT_SEC", 0.15)
        bi._insert_packet_stream_row('{"n":0}', 'packet')
        bi._insert_packet_stream_row('{"n":1}', 'packet')
        assert bi._write_queue.qsize() == 2
        bi._insert_packet_stream_row('{"n":2}', 'packet')
        assert bi._write_queue.qsize() == 1

    def test_flush_failure_requeues_rows(self, bot_integration):
        """Rows are put back on the queue if SQLite flush fails after retries."""
        bot_integration._drain_stop.set()
        if bot_integration._drain_thread:
            bot_integration._drain_thread.join(timeout=2.0)
        bot_integration._insert_packet_stream_row('{"x":1}', 'packet')
        bot_integration._insert_packet_stream_row('{"x":2}', 'packet')
        assert bot_integration._write_queue.qsize() == 2

        with patch.object(sqlite3, "connect", side_effect=sqlite3.OperationalError("database is locked")):
            bot_integration._flush_write_queue()

        assert bot_integration._write_queue.qsize() == 2


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

@pytest.mark.usefixtures("managed_socketio_clients")
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

    def test_subscribe_packets_does_not_emit_status_ack(self, socketio_viewer):
        """subscribe_packets is silent — the navbar indicator already reflects socket state."""
        from flask_socketio import SocketIOTestClient

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_packets")

        received = sio_client.get_received()
        status_events = [e for e in received if e["name"] == "status"]
        assert status_events == []

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


@pytest.mark.usefixtures("managed_socketio_clients")
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

    def test_subscribe_messages_does_not_emit_status_ack(self, socketio_viewer):
        """subscribe_messages is silent — the navbar indicator already reflects socket state."""
        from flask_socketio import SocketIOTestClient

        sio_client = SocketIOTestClient(socketio_viewer.app, socketio_viewer.socketio)
        sio_client.emit("subscribe_messages")

        received = sio_client.get_received()
        status_events = [e for e in received if e["name"] == "status"]
        assert status_events == []

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
            patch.object(BotDataViewer, "_start_dashboard_refresher", lambda self: None),
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


class TestWebViewerLoggingRespectsLogFile:
    """Web viewer file logging follows [Logging] log_file like the main bot."""

    def _write_config(
        self,
        config_dir: Path,
        log_file: str | None,
        log_level: str = "INFO",
    ) -> str:
        cfg = configparser.ConfigParser()
        cfg["Connection"] = {"connection_type": "serial", "serial_port": "/dev/ttyUSB0"}
        cfg["Bot"] = {"bot_name": "TestBot", "db_path": "bot.db", "prefix_bytes": "1"}
        cfg["Channels"] = {"monitor_channels": "general"}
        cfg["Path_Command"] = {"max_hops": "5", "timeout": "30"}
        if log_file is not None:
            cfg["Logging"] = {
                "log_file": log_file,
                "log_level": log_level,
            }
        config_path = str(config_dir / "config.ini")
        with open(config_path, "w") as fh:
            cfg.write(fh)
        return config_path

    def _make_viewer(self, config_path: str) -> BotDataViewer:
        with (
            patch.object(BotDataViewer, "_start_database_polling", lambda self: None),
            patch.object(BotDataViewer, "_start_log_tailing", lambda self: None),
            patch.object(BotDataViewer, "_start_cleanup_scheduler", lambda self: None),
            patch.object(BotDataViewer, "_start_dashboard_refresher", lambda self: None),
        ):
            return BotDataViewer(config_path=config_path)

    def test_empty_log_file_is_console_only(self, tmp_path: Path) -> None:
        from logging.handlers import RotatingFileHandler

        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        config_path = self._write_config(config_dir, log_file="")
        v = self._make_viewer(config_path)
        assert not any(isinstance(h, RotatingFileHandler) for h in v.logger.handlers)
        assert not (config_dir / "logs").exists()
        assert not (config_dir / "web_viewer.log").exists()
        assert not (tmp_path / "logs").exists()

    def test_log_file_writes_beside_bot_log(self, tmp_path: Path) -> None:
        from logging.handlers import RotatingFileHandler

        config_dir = tmp_path / "cfg"
        log_dir = tmp_path / "varlog"
        config_dir.mkdir()
        log_dir.mkdir()
        bot_log = log_dir / "meshcore_bot.log"
        config_path = self._write_config(config_dir, log_file=str(bot_log))
        v = self._make_viewer(config_path)
        assert any(isinstance(h, RotatingFileHandler) for h in v.logger.handlers)
        assert (log_dir / "web_viewer.log").exists()
        assert not (config_dir / "logs").exists()
        assert not (tmp_path / "logs").exists()

    def test_handlers_respect_configured_log_level(self, tmp_path: Path) -> None:
        import logging

        config_dir = tmp_path / "cfg"
        log_dir = tmp_path / "varlog"
        config_dir.mkdir()
        log_dir.mkdir()
        config_path = self._write_config(
            config_dir,
            log_file=str(log_dir / "meshcore_bot.log"),
            log_level="WARNING",
        )

        viewer = self._make_viewer(config_path)

        assert viewer.logger.level == logging.WARNING
        assert viewer.logger.handlers
        assert all(
            handler.level == logging.WARNING
            for handler in viewer.logger.handlers
        )

    def test_debug_file_logging_keeps_info_floor_for_journal(
        self, tmp_path: Path
    ) -> None:
        import logging
        from logging.handlers import RotatingFileHandler

        config_dir = tmp_path / "cfg"
        log_dir = tmp_path / "varlog"
        config_dir.mkdir()
        log_dir.mkdir()
        config_path = self._write_config(
            config_dir,
            log_file=str(log_dir / "meshcore_bot.log"),
            log_level="DEBUG",
        )

        viewer = self._make_viewer(config_path)
        file_handlers = [
            handler
            for handler in viewer.logger.handlers
            if isinstance(handler, RotatingFileHandler)
        ]
        console_handlers = [
            handler
            for handler in viewer.logger.handlers
            if not isinstance(handler, RotatingFileHandler)
        ]

        assert viewer.logger.level == logging.DEBUG
        assert [handler.level for handler in file_handlers] == [logging.DEBUG]
        assert [handler.level for handler in console_handlers] == [logging.INFO]


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


# ===========================================================================
# Security: Feed preview/test SSRF prevention (GAP W2)
# ===========================================================================


class TestFeedPreviewResponseLimits:
    def test_chunked_body_over_cap_is_streamed_closed_and_rejected(self, viewer):
        response = Mock()
        response.headers = {"Content-Type": "application/rss+xml"}
        response.raise_for_status = Mock()
        response.close = Mock()

        def chunks(*_args, **_kwargs):
            yield b"x" * (1024 * 1024)
            yield b"y" * (1024 * 1024 + 1)

        response.iter_content = Mock(side_effect=chunks)
        session_context = MagicMock()
        session_context.__enter__.return_value = Mock()
        with (
            patch("modules.web_viewer.app.SafeUrlPolicy.validate", return_value=True),
            patch(
                "modules.web_viewer.app.create_safe_requests_session",
                return_value=session_context,
            ),
            patch(
                "modules.web_viewer.app.safe_requests_request",
                return_value=response,
            ) as request_mock,
            pytest.raises(ValueError, match="exceeds .* byte limit"),
        ):
            viewer._preview_feed_items(
                "https://example.com/feed.xml", "rss", "{title}"
            )

        assert request_mock.call_args.kwargs["stream"] is True
        response.close.assert_called_once()

    def test_content_length_over_cap_is_rejected_without_reading(self, viewer):
        response = Mock()
        response.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(2 * 1024 * 1024 + 1),
        }
        response.raise_for_status = Mock()
        response.close = Mock()
        response.iter_content = Mock()
        session_context = MagicMock()
        session_context.__enter__.return_value = Mock()
        with (
            patch("modules.web_viewer.app.SafeUrlPolicy.validate", return_value=True),
            patch(
                "modules.web_viewer.app.create_safe_requests_session",
                return_value=session_context,
            ),
            patch(
                "modules.web_viewer.app.safe_requests_request",
                return_value=response,
            ),
            pytest.raises(ValueError, match="exceeds .* byte limit"),
        ):
            viewer._preview_feed_items(
                "https://example.com/feed.json",
                "api",
                "{title}",
                api_config={},
            )

        response.iter_content.assert_not_called()
        response.close.assert_called_once()


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

    def test_safe_url_policy_called_for_preview(self, client):
        """The strict feed URL policy is invoked before preview fetches."""
        with patch("modules.web_viewer.app.SafeUrlPolicy.validate", return_value=False) as validate:
            resp = client.post(
                "/api/feeds/preview",
                json={"feed_url": "http://192.168.1.1/feed.rss", "feed_type": "rss"},
                content_type="application/json",
            )
        validate.assert_called_once()
        assert resp.status_code == 400
