"""Tests for modules.web_viewer.app — BotDataViewer Flask app."""

import json
import shutil
import sqlite3
import subprocess
import threading
import time
from configparser import ConfigParser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def cleanup_sqlite_connections(monkeypatch):
    """Track and close SQLite connections opened during each test.

    Some app code paths intentionally create ad-hoc connections for request-style
    operations; this fixture ensures any leaked handles are closed so Python 3.13
    ResourceWarning checks stay clean.
    """
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


@pytest.fixture
def viewer_with_db(tmp_path):
    """Create a BotDataViewer instance with a test database.

    The database starts empty so migrations create all tables with the correct schema.
    This ensures tests match production behavior where BotDataViewer runs migrations.
    """
    from modules.web_viewer.app import BotDataViewer

    config = ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "db_path", str(tmp_path / "meshcore_bot.db"))
    config.add_section("Web_Viewer")
    config.set("Web_Viewer", "host", "127.0.0.1")
    config.set("Web_Viewer", "port", "8080")
    config.set("Web_Viewer", "enabled", "false")
    config.set("Web_Viewer", "auto_start", "false")
    config.set("Web_Viewer", "debug", "false")
    config.set("Web_Viewer", "cors_allowed_origins", "*")
    config.set("Web_Viewer", "web_viewer_password", "")

    config_path = str(tmp_path / "config.ini")
    with open(config_path, "w") as f:
        config.write(f)

    db_path = str(tmp_path / "meshcore_bot.db")

    # Don't patch _setup_routes to get routes registered
    with patch.object(BotDataViewer, "_start_database_polling"), \
         patch.object(BotDataViewer, "_start_log_tailing"), \
         patch.object(BotDataViewer, "_start_cleanup_scheduler"), \
         patch.object(BotDataViewer, "_start_dashboard_refresher"), \
         patch.object(BotDataViewer, "_setup_socketio_handlers"), \
         patch("modules.web_viewer.app.RepeaterManager"):
        viewer = BotDataViewer(db_path=db_path, config_path=config_path)

    viewer.db_path = db_path
    viewer.config_path = config_path
    viewer.app.testing = True
    return viewer


@pytest.fixture
def mock_viewer(tmp_path):
    """Create a minimal BotDataViewer with mock bot."""
    from modules.web_viewer.app import BotDataViewer

    config = ConfigParser()
    config.add_section("Bot")
    config.add_section("Web_Viewer")
    config.set("Web_Viewer", "host", "127.0.0.1")
    config.set("Web_Viewer", "port", "8080")
    config.set("Web_Viewer", "enabled", "false")
    config.set("Web_Viewer", "auto_start", "false")
    config.set("Web_Viewer", "debug", "false")
    config.set("Web_Viewer", "cors_allowed_origins", "*")
    config.set("Web_Viewer", "web_viewer_password", "")

    config_path = str(tmp_path / "config.ini")
    with open(config_path, "w") as f:
        config.write(f)

    db_path = str(tmp_path / "meshcore_bot.db")

    # Create minimal database
    with sqlite3.connect(db_path, timeout=60) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_metadata (
                key TEXT PRIMARY KEY, value TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS packet_stream (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                data TEXT,
                type TEXT
            )
        """)
        conn.commit()

    # Don't patch _setup_routes to get routes registered
    with patch.object(BotDataViewer, "_start_database_polling"), \
         patch.object(BotDataViewer, "_start_log_tailing"), \
         patch.object(BotDataViewer, "_start_cleanup_scheduler"), \
         patch.object(BotDataViewer, "_start_dashboard_refresher"), \
         patch.object(BotDataViewer, "_setup_socketio_handlers"), \
         patch("modules.web_viewer.app.RepeaterManager"):
        viewer = BotDataViewer(db_path=db_path, config_path=config_path)

    viewer.db_path = db_path
    viewer.config_path = config_path
    viewer.app.testing = True
    return viewer


# ---------------------------------------------------------------------------
# ALLOWED_TABLES whitelist
# ---------------------------------------------------------------------------


class TestAllowedTables:
    def test_whitelist_contains_expected_tables(self):
        from modules.web_viewer.app import BotDataViewer

        expected_tables = {
            'geocoding_cache', 'generic_cache', 'bot_metadata',
            'packet_stream', 'message_stats', 'command_stats',
            'repeater_contacts', 'complete_contact_tracking', 'mesh_connections',
            'observed_paths', 'daily_stats', 'purging_log', 'greeter_rollout',
            'greeted_users', 'feed_subscriptions', 'feed_activity', 'feed_errors',
            'path_stats', 'unique_advert_packets', 'schema_version',
            'channel_operations', 'channels', 'feed_message_queue',
            'daily_rollup', 'dashboard_snapshot',
        }
        assert expected_tables == BotDataViewer.ALLOWED_TABLES


class TestIsSafeTableName:
    def test_valid_table_name_passes(self, mock_viewer):
        assert mock_viewer._is_safe_table_name('repeater_contacts') is True

    def test_invalid_table_name_fails(self, mock_viewer):
        assert mock_viewer._is_safe_table_name('repeater_contacts; DROP TABLE users;') is False

    def test_empty_name_fails(self, mock_viewer):
        assert mock_viewer._is_safe_table_name('') is False

    def test_underscore_allowed(self, mock_viewer):
        assert mock_viewer._is_safe_table_name('complete_contact_tracking') is True


# ---------------------------------------------------------------------------
# _get_database_info
# ---------------------------------------------------------------------------


class TestGetDatabaseInfo:
    def test_returns_allowed_tables_only(self, viewer_with_db):
        # Add a malicious table to the database
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE malicious_table (id INTEGER)")
            conn.commit()

        info = viewer_with_db._get_database_info()
        table_names = [t['name'] for t in info.get('tables', [])]

        assert 'malicious_table' not in table_names
        assert 'repeater_contacts' in table_names


class TestGetDatabaseStats:
    def test_filters_tables_by_whitelist(self, viewer_with_db):
        # Add a malicious table
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE malicious_table (id INTEGER)")
            cursor.execute("INSERT INTO malicious_table VALUES (1)")
            conn.commit()

        stats = viewer_with_db._get_database_stats()
        # Should not include stats for malicious table
        table_stats = stats.get('table_stats', {})
        assert 'malicious_table' not in table_stats


# ---------------------------------------------------------------------------
# api_export_contacts
# ---------------------------------------------------------------------------


class TestApiExportContacts:
    def test_export_json_default(self, viewer_with_db):
        # Add test data
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO complete_contact_tracking
                (public_key, name, role, device_type, latitude, longitude,
                 city, state, country, snr, first_heard, last_heard,
                 advert_count, is_starred)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "aa:bb:cc:dd:ee:ff:gg:hh",
                "Test Node",
                "client",
                "node",
                40.7128,
                -74.0060,
                "New York",
                "NY",
                "USA",
                -12.5,
                # last_heard/first_heard are stored as local ISO-text datetimes in production
                # (written via datetime.now()); the contacts time filter compares against
                # datetime('now', 'localtime', ...), so seed matching ISO text within the window.
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - 86400)),
                time.strftime('%Y-%m-%d %H:%M:%S'),
                5,
                0,
            ))
            conn.commit()

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/export/contacts')

            assert response.status_code == 200
            assert response.content_type == 'application/json'
            contacts = json.loads(response.data)
            assert isinstance(contacts, list)
            assert len(contacts) > 0

    def test_export_csv(self, viewer_with_db):
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO complete_contact_tracking
                (public_key, name, role, device_type)
                VALUES (?, ?, ?, ?)
            """, ("aa:bb", "Test Node", "client", "node"))
            conn.commit()

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/export/contacts?format=csv')

            assert response.status_code == 200
            # Flask adds charset=utf-8 automatically
            assert 'text/csv' in response.content_type
            csv_data = response.data.decode('utf-8')
            assert 'user_id' in csv_data
            assert 'Test Node' in csv_data

    def test_export_since_7d(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/export/contacts?since=7d')
            assert response.status_code == 200

    def test_export_since_invalid_defaults_to_30d(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/export/contacts?since=invalid')
            assert response.status_code == 200

    def test_export_since_all(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/export/contacts?since=all')
            assert response.status_code == 200


# ---------------------------------------------------------------------------
# api_export_paths
# ---------------------------------------------------------------------------


class TestApiExportPaths:
    def test_export_json_default(self, viewer_with_db):
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO observed_paths
                (packet_hash, path_hex, path_length, observation_count,
                 from_prefix, to_prefix, bytes_per_hop, packet_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "0102030405060708",
                "0102030405",
                5,
                10,
                "0102",
                "0304",
                1,
                "advert",
            ))
            conn.commit()

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/export/paths')

            assert response.status_code == 200
            assert response.content_type == 'application/json'
            paths = json.loads(response.data)
            assert isinstance(paths, list)

    def test_export_csv(self, viewer_with_db):
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO observed_paths
                (packet_hash, path_hex, path_length, observation_count,
                 from_prefix, to_prefix, bytes_per_hop, packet_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "0102030405060708",
                "0102030405",
                5,
                10,
                "0102",
                "0304",
                1,
                "advert",
            ))
            conn.commit()

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/export/paths?format=csv')

            assert response.status_code == 200
            # Flask adds charset=utf-8 automatically
            assert 'text/csv' in response.content_type
            csv_data = response.data.decode('utf-8')
            assert 'public_key' in csv_data

    def test_export_since_7d(self, viewer_with_db):
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO observed_paths
                (packet_hash, path_hex, path_length, observation_count,
                 from_prefix, to_prefix, bytes_per_hop, packet_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "0102030405060708",
                "01020304",
                4,
                5,
                "01",
                "02",
                1,
                "advert",
            ))
            conn.commit()

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/export/paths?since=7d')
            assert response.status_code == 200


# ---------------------------------------------------------------------------
# api_mesh_edges (evidence modes)
# ---------------------------------------------------------------------------


def _seed_observed_path(db_path, path_hex, bytes_per_hop, observation_count=1,
                        last_seen=None, packet_type='advert'):
    """Insert an observed_paths row; from/to prefixes derived from the path."""
    hex_chars = bytes_per_hop * 2
    hops = [path_hex[i:i + hex_chars] for i in range(0, len(path_hex), hex_chars)]
    ts = last_seen or time.strftime('%Y-%m-%dT%H:%M:%S')
    with sqlite3.connect(db_path, timeout=60) as conn:
        conn.execute("""
            INSERT INTO observed_paths
            (from_prefix, to_prefix, path_hex, path_length, bytes_per_hop,
             packet_type, observation_count, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (hops[0], hops[-1], path_hex, len(path_hex) // 2, bytes_per_hop,
              packet_type, observation_count, ts, ts))
        conn.commit()


def _insert_neighbor_link(conn, self_key, neighbor_key, *, last_seen,
                          observation_count=1):
    """Insert a confirmed zero-hop link, the strongest evidence class."""
    conn.execute(
        """
        INSERT INTO neighbor_links
            (self_public_key, neighbor_public_key, first_seen, last_seen,
             observation_count, snr_sum, snr_count, best_snr, last_snr,
             last_status, scopes)
        VALUES (?, ?, ?, ?, ?, 5.0, 1, 5.0, 5.0, 'responded', '')
        """,
        (self_key, neighbor_key, last_seen, last_seen, observation_count),
    )


class TestApiMeshEdgesEvidence:
    def test_nodes_days_filter_is_applied_in_database(self, viewer_with_db):
        recent = time.strftime('%Y-%m-%dT%H:%M:%S')
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            conn.executemany(
                """
                INSERT INTO complete_contact_tracking
                    (public_key, name, role, last_heard, latitude, longitude)
                VALUES (?, ?, 'repeater', ?, 45.0, -122.0)
                """,
                [
                    ('aa' * 32, 'Recent', recent),
                    ('bb' * 32, 'Old', '2020-01-01T00:00:00'),
                ],
            )
            conn.commit()

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/mesh/nodes?days=7')
            assert response.status_code == 200
            names = {node['name'] for node in json.loads(response.data)['nodes']}
            assert names == {'Recent'}

    def test_default_mode_tags_evidence_by_key_length(self, viewer_with_db):
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            conn.execute("""
                INSERT INTO mesh_connections (from_prefix, to_prefix, observation_count)
                VALUES ('aa', 'bb', 5), ('aabb', 'ccdd', 3)
            """)
            conn.commit()

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/mesh/edges')
            assert response.status_code == 200
            edges = {(e['from_prefix'], e['to_prefix']): e
                     for e in json.loads(response.data)['edges']}
            assert edges[('aa', 'bb')]['evidence'] == 'singlebyte'
            assert edges[('aabb', 'ccdd')]['evidence'] == 'multibyte'

    def test_multibyte_mode_derives_consecutive_pairs(self, viewer_with_db):
        # 2-byte path with 3 hops -> two directed edges; 1-byte row excluded
        _seed_observed_path(viewer_with_db.db_path, 'aaaabbbbcccc', 2, observation_count=2)
        _seed_observed_path(viewer_with_db.db_path, 'ddee', 1, observation_count=9)

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/mesh/edges?evidence=multibyte')
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['evidence'] == 'multibyte'
            edges = {(e['from_prefix'], e['to_prefix']): e for e in data['edges']}
            assert set(edges) == {('aaaa', 'bbbb'), ('bbbb', 'cccc')}
            edge = edges[('aaaa', 'bbbb')]
            assert edge['observation_count'] == 2
            assert edge['path_count'] == 1
            assert edge['evidence'] == 'multibyte'
            assert edge['avg_hop_position'] == 1
            assert edges[('bbbb', 'cccc')]['avg_hop_position'] == 2

    def test_multibyte_mode_coalesces_unique_lower_resolution(self, viewer_with_db):
        # One 3-byte edge plus a 2-byte observation of the same link:
        # unique prefix match, so the 2-byte counts merge into the 3-byte edge
        _seed_observed_path(viewer_with_db.db_path, 'aaaa11bbbb22', 3, observation_count=4)
        _seed_observed_path(viewer_with_db.db_path, 'aaaabbbb', 2, observation_count=3)

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/mesh/edges?evidence=multibyte')
            data = json.loads(response.data)
            edges = {(e['from_prefix'], e['to_prefix']): e for e in data['edges']}
            assert set(edges) == {('aaaa11', 'bbbb22')}
            assert edges[('aaaa11', 'bbbb22')]['observation_count'] == 7
            assert edges[('aaaa11', 'bbbb22')]['path_count'] == 2

    def test_multibyte_mode_keeps_ambiguous_resolutions_separate(self, viewer_with_db):
        # Two distinct 3-byte edges share the same 4-char truncation:
        # the 2-byte observation is ambiguous and must stay its own edge
        _seed_observed_path(viewer_with_db.db_path, 'aaaa11bbbb22', 3)
        _seed_observed_path(viewer_with_db.db_path, 'aaaa33bbbb44', 3)
        _seed_observed_path(viewer_with_db.db_path, 'aaaabbbb', 2)

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/mesh/edges?evidence=multibyte')
            data = json.loads(response.data)
            keys = {(e['from_prefix'], e['to_prefix']) for e in data['edges']}
            assert keys == {('aaaa11', 'bbbb22'), ('aaaa33', 'bbbb44'), ('aaaa', 'bbbb')}

    def test_multibyte_mode_min_observations_applied_after_merge(self, viewer_with_db):
        _seed_observed_path(viewer_with_db.db_path, 'aaaa11bbbb22', 3, observation_count=2)
        _seed_observed_path(viewer_with_db.db_path, 'aaaabbbb', 2, observation_count=2)
        _seed_observed_path(viewer_with_db.db_path, 'cccc55dddd66', 3, observation_count=1)

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/mesh/edges?evidence=multibyte&min_observations=4')
            data = json.loads(response.data)
            keys = {(e['from_prefix'], e['to_prefix']) for e in data['edges']}
            # merged edge has 4 observations and survives; the other has 1
            assert keys == {('aaaa11', 'bbbb22')}

    def test_multibyte_mode_days_filter(self, viewer_with_db):
        _seed_observed_path(viewer_with_db.db_path, 'aaaa11bbbb22', 3,
                            last_seen='2020-01-01T00:00:00')
        _seed_observed_path(viewer_with_db.db_path, 'cccc55dddd66', 3)

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/mesh/edges?evidence=multibyte&days=7')
            data = json.loads(response.data)
            keys = {(e['from_prefix'], e['to_prefix']) for e in data['edges']}
            assert keys == {('cccc55', 'dddd66')}

    def test_multibyte_days_preserves_lifetime_cross_resolution_merge(self, viewer_with_db):
        # The selected window controls edge visibility, not the lifetime
        # identity/count aggregation. A recent 2-byte observation must still
        # resolve to the unique older 3-byte edge.
        _seed_observed_path(
            viewer_with_db.db_path,
            'aaaa11bbbb22',
            3,
            observation_count=4,
            last_seen='2020-01-01T00:00:00',
        )
        _seed_observed_path(
            viewer_with_db.db_path,
            'aaaabbbb',
            2,
            observation_count=3,
        )

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/mesh/edges?evidence=multibyte&days=7')
            data = json.loads(response.data)
            edges = {(e['from_prefix'], e['to_prefix']): e for e in data['edges']}
            assert set(edges) == {('aaaa11', 'bbbb22')}
            assert edges[('aaaa11', 'bbbb22')]['observation_count'] == 7
            assert edges[('aaaa11', 'bbbb22')]['path_count'] == 2

    def test_window_preserves_lifetime_prefix_resolution(self, viewer_with_db):
        old = '2020-01-01T00:00:00'
        recent = time.strftime('%Y-%m-%dT%H:%M:%S')
        _seed_observed_path(
            viewer_with_db.db_path,
            'aaaa11bbbb22',
            3,
            last_seen=old,
        )
        _seed_observed_path(
            viewer_with_db.db_path,
            'ccccdddd',
            2,
            last_seen=recent,
        )
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            conn.executemany(
                """
                INSERT INTO mesh_connections
                    (from_prefix, to_prefix, observation_count, last_seen)
                VALUES (?, ?, 1, ?)
                """,
                [
                    ('aaaa11', 'bbbb22', old),
                    ('cccc', 'dddd', recent),
                ],
            )
            conn.commit()

        with viewer_with_db.app.test_client() as client:
            for evidence in ('all', 'multibyte'):
                response = client.get(
                    f'/api/mesh/edges?evidence={evidence}&days=7'
                )
                data = json.loads(response.data)
                assert data['prefix_hex_chars'] == 6
                assert {
                    (edge['from_prefix'], edge['to_prefix'])
                    for edge in data['edges']
                } == {('cccc', 'dddd')}

    def test_multibyte_days_uses_exact_timestamp_cutoff(
        self, viewer_with_db, monkeypatch
    ):
        fixed_now = datetime(2026, 7, 29, 15, 0, 0)

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return fixed_now
                return fixed_now.replace(tzinfo=timezone.utc).astimezone(tz)

        monkeypatch.setattr('modules.web_viewer.app.datetime', FixedDateTime)
        cutoff = fixed_now - timedelta(days=7)
        _seed_observed_path(
            viewer_with_db.db_path,
            'aaaa11bbbb22',
            3,
            last_seen=(cutoff - timedelta(seconds=1)).isoformat(),
        )
        _seed_observed_path(
            viewer_with_db.db_path,
            'cccc55dddd66',
            3,
            last_seen=cutoff.isoformat(),
        )

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/mesh/edges?evidence=multibyte&days=7')
            data = json.loads(response.data)
            assert {
                (edge['from_prefix'], edge['to_prefix'])
                for edge in data['edges']
            } == {('cccc55', 'dddd66')}

    def test_neighbor_label_matches_on_full_public_keys(self, viewer_with_db):
        """MeshGraph keeps some 1-byte edges but fills in the keys discovery gave it.

        add_edge refuses to promote a 1-byte edge with no public key (several
        nodes still share it), so a 3-byte prefix comparison alone would leave a
        confirmed neighbour labelled 'singlebyte'.
        """
        self_key, neighbor_key = 'aa' * 32, 'bb' * 32
        recent = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            conn.execute(
                """
                INSERT INTO mesh_connections
                    (from_prefix, to_prefix, from_public_key, to_public_key,
                     observation_count, last_seen)
                VALUES ('aa', 'bb', ?, ?, 5, ?)
                """,
                (self_key, neighbor_key, recent),
            )
            _insert_neighbor_link(conn, self_key, neighbor_key, last_seen=recent)
            conn.commit()

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/mesh/edges')
            edges = {(e['from_prefix'], e['to_prefix']): e
                     for e in json.loads(response.data)['edges']}
            assert edges[('aa', 'bb')]['evidence'] == 'neighbors'

    def test_stale_neighbor_evidence_does_not_relabel_a_recent_edge(
        self, viewer_with_db
    ):
        """neighbor_links is never pruned, so a link last heard years ago must not
        claim a recent path-derived edge is a current direct neighbour."""
        self_key, neighbor_key = 'aa' * 32, 'bb' * 32
        recent = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            conn.execute(
                """
                INSERT INTO mesh_connections
                    (from_prefix, to_prefix, observation_count, last_seen)
                VALUES (?, ?, 5, ?)
                """,
                (self_key[:6], neighbor_key[:6], recent),
            )
            _insert_neighbor_link(conn, self_key, neighbor_key,
                                  last_seen='2020-01-01T00:00:00+00:00')
            conn.commit()

        key = (self_key[:6], neighbor_key[:6])
        with viewer_with_db.app.test_client() as client:
            windowed = json.loads(
                client.get('/api/mesh/edges?days=7').data
            )['edges']
            assert {(e['from_prefix'], e['to_prefix']): e
                    for e in windowed}[key]['evidence'] == 'multibyte'

            # Unwindowed, the lifetime evidence still stands.
            lifetime = json.loads(client.get('/api/mesh/edges').data)['edges']
            assert {(e['from_prefix'], e['to_prefix']): e
                    for e in lifetime}[key]['evidence'] == 'neighbors'

    def test_stats_include_multibyte_edge_count(self, viewer_with_db):
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            conn.execute("""
                INSERT INTO mesh_connections (from_prefix, to_prefix, observation_count)
                VALUES ('aa', 'bb', 5), ('aabb', 'ccdd', 3), ('aabb11', 'ccdd22', 1)
            """)
            conn.commit()

        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/mesh/stats')
            assert response.status_code == 200
            stats = json.loads(response.data)
            assert stats['total_edges'] == 3
            assert stats['multibyte_edges'] == 2


class TestMultibyteMeshAggregateCache:
    def test_reuses_lifetime_aggregate_across_filtered_requests(
        self, viewer_with_db
    ):
        _seed_observed_path(
            viewer_with_db.db_path,
            'aaaa11bbbb22',
            3,
            observation_count=5,
        )

        with patch.object(
            viewer_with_db,
            '_compute_multibyte_evidence_edges',
            wraps=viewer_with_db._compute_multibyte_evidence_edges,
        ) as compute:
            with viewer_with_db.app.test_client() as client:
                first = client.get('/api/mesh/edges?evidence=multibyte')
                second = client.get(
                    '/api/mesh/edges?evidence=multibyte&min_observations=4&days=7'
                )

        assert first.status_code == 200
        assert second.status_code == 200
        assert compute.call_count == 1

    def test_recomputes_after_cache_window(self, viewer_with_db):
        with patch.object(
            viewer_with_db,
            '_compute_multibyte_evidence_edges',
            return_value=[],
        ) as compute:
            viewer_with_db._aggregate_multibyte_evidence_edges()
            viewer_with_db._multibyte_graph_cache_created_at -= (
                viewer_with_db._mesh_graph_cache_seconds + 1
            )
            viewer_with_db._aggregate_multibyte_evidence_edges()

        assert compute.call_count == 2

    def test_forced_refresh_bypasses_warm_cache(self, viewer_with_db):
        _seed_observed_path(
            viewer_with_db.db_path,
            'aaaa11bbbb22',
            3,
        )
        with patch.object(
            viewer_with_db,
            '_compute_multibyte_evidence_edges',
            wraps=viewer_with_db._compute_multibyte_evidence_edges,
        ) as compute:
            with viewer_with_db.app.test_client() as client:
                first = client.get('/api/mesh/edges?evidence=multibyte')
                _seed_observed_path(
                    viewer_with_db.db_path,
                    'cccc55dddd66',
                    3,
                )
                cached = client.get('/api/mesh/edges?evidence=multibyte')
                refreshed = client.get(
                    '/api/mesh/edges?evidence=multibyte&refresh=1'
                )

        assert {
            edge['from_prefix'] for edge in first.get_json()['edges']
        } == {'aaaa11'}
        assert {
            edge['from_prefix'] for edge in cached.get_json()['edges']
        } == {'aaaa11'}
        assert {
            edge['from_prefix'] for edge in refreshed.get_json()['edges']
        } == {'aaaa11', 'cccc55'}
        assert compute.call_count == 2

    def test_concurrent_cold_requests_share_one_computation(
        self, viewer_with_db
    ):
        started = threading.Event()
        release = threading.Event()
        computed = [{'from_prefix': 'aaaa', 'to_prefix': 'bbbb'}]

        def slow_compute():
            started.set()
            assert release.wait(timeout=2)
            return computed

        results = []
        with patch.object(
            viewer_with_db,
            '_compute_multibyte_evidence_edges',
            side_effect=slow_compute,
        ) as compute:
            first = threading.Thread(
                target=lambda: results.append(
                    viewer_with_db._aggregate_multibyte_evidence_edges()
                )
            )
            second = threading.Thread(
                target=lambda: results.append(
                    viewer_with_db._aggregate_multibyte_evidence_edges()
                )
            )
            first.start()
            assert started.wait(timeout=2)
            second.start()
            release.set()
            first.join(timeout=2)
            second.join(timeout=2)

        assert not first.is_alive()
        assert not second.is_alive()
        assert compute.call_count == 1
        assert results == [computed, computed]

    def test_concurrent_refresh_serves_stale_cache(self, viewer_with_db):
        stale = [{'from_prefix': 'old', 'to_prefix': 'edge'}]
        fresh = [{'from_prefix': 'new', 'to_prefix': 'edge'}]
        viewer_with_db._multibyte_graph_cache_edges = stale
        viewer_with_db._multibyte_graph_cache_created_at = 0
        started = threading.Event()
        release = threading.Event()

        def slow_compute():
            started.set()
            assert release.wait(timeout=2)
            return fresh

        refreshed = []
        with patch.object(
            viewer_with_db,
            '_compute_multibyte_evidence_edges',
            side_effect=slow_compute,
        ) as compute:
            worker = threading.Thread(
                target=lambda: refreshed.append(
                    viewer_with_db._aggregate_multibyte_evidence_edges()
                )
            )
            worker.start()
            assert started.wait(timeout=2)
            concurrent = viewer_with_db._aggregate_multibyte_evidence_edges()
            release.set()
            worker.join(timeout=2)

        assert not worker.is_alive()
        assert concurrent is stale
        assert refreshed == [fresh]
        assert compute.call_count == 1

    def test_concurrent_cold_failure_is_shared(self, viewer_with_db):
        started = threading.Event()
        release = threading.Event()
        errors = []

        def failing_compute():
            started.set()
            assert release.wait(timeout=2)
            raise sqlite3.OperationalError('temporary failure')

        def aggregate():
            try:
                viewer_with_db._aggregate_multibyte_evidence_edges()
            except Exception as exc:
                errors.append(exc)

        with patch.object(
            viewer_with_db,
            '_compute_multibyte_evidence_edges',
            side_effect=failing_compute,
        ) as compute:
            first = threading.Thread(target=aggregate)
            second = threading.Thread(target=aggregate)
            first.start()
            assert started.wait(timeout=2)
            second.start()
            release.set()
            first.join(timeout=2)
            second.join(timeout=2)

        assert not first.is_alive()
        assert not second.is_alive()
        assert compute.call_count == 1
        assert len(errors) == 2
        assert any(isinstance(exc, sqlite3.OperationalError) for exc in errors)
        assert any(isinstance(exc, RuntimeError) for exc in errors)

    def test_stale_failure_uses_retry_backoff(self, viewer_with_db):
        stale = [{'from_prefix': 'old', 'to_prefix': 'edge'}]
        viewer_with_db._multibyte_graph_cache_edges = stale
        viewer_with_db._multibyte_graph_cache_created_at = 0

        with patch.object(
            viewer_with_db,
            '_compute_multibyte_evidence_edges',
            side_effect=sqlite3.OperationalError('temporary failure'),
        ) as compute:
            first = viewer_with_db._aggregate_multibyte_evidence_edges()
            with pytest.raises(RuntimeError, match='retry suppressed'):
                viewer_with_db._aggregate_multibyte_evidence_edges(
                    force_refresh=True
                )
            viewer_with_db._multibyte_graph_cache_failure_at -= (
                viewer_with_db._multibyte_graph_cache_retry_seconds + 1
            )
            third = viewer_with_db._aggregate_multibyte_evidence_edges()

        assert first is stale
        assert third is stale
        assert compute.call_count == 2
        assert viewer_with_db._multibyte_graph_cache_failure == (
            'OperationalError',
            'temporary failure',
        )

    def test_forced_failure_returns_500_while_normal_request_serves_stale(
        self, viewer_with_db
    ):
        viewer_with_db._multibyte_graph_cache_edges = []
        viewer_with_db._multibyte_graph_cache_created_at = 0

        with patch.object(
            viewer_with_db,
            '_compute_multibyte_evidence_edges',
            side_effect=sqlite3.OperationalError('temporary failure'),
        ) as compute:
            with viewer_with_db.app.test_client() as client:
                forced = client.get(
                    '/api/mesh/edges?evidence=multibyte&refresh=1'
                )
                normal = client.get('/api/mesh/edges?evidence=multibyte')

        assert forced.status_code == 500
        assert forced.get_json()['error'] == 'An internal error occurred'
        assert normal.status_code == 200
        assert normal.get_json()['edges'] == []
        assert compute.call_count == 1


def test_mesh_template_coalesces_live_refreshes():
    source = (
        Path(__file__).parents[1]
        / 'modules'
        / 'web_viewer'
        / 'templates'
        / 'mesh.html'
    ).read_text(encoding='utf-8')

    assert 'const MESH_LIVE_REFRESH_MS = 30000;' in source
    assert 'const MESH_LIVE_REFRESH_RETRY_MS = 6000;' in source
    assert "document.addEventListener('visibilitychange'" in source
    assert 'scheduleMeshLiveRefresh(reloadNodes);' in source
    assert "socket.on('mesh_edge_updated', () => onMeshUpdate(false));" in source
    assert 'while (pendingMeshLoad)' in source
    assert "fetchMeshJson(edgesUrl, 'edges')" in source
    assert "loadData({ skipRender: true }).then" not in source


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js not installed')
def test_mesh_refresh_coordinator_handles_bursts_visibility_and_failures():
    source = (
        Path(__file__).parents[1]
        / 'modules'
        / 'web_viewer'
        / 'templates'
        / 'mesh.html'
    ).read_text(encoding='utf-8')

    def extract(start, end):
        return source[source.index(start):source.index(end)]

    coordinator = '\n'.join([
        extract(
            '    function normalizeMeshLoadOptions',
            '    function mergeMeshLoadOptions',
        ),
        extract(
            '    function mergeMeshLoadOptions',
            '    // Load nodes and edges.',
        ),
        extract('    function loadData', '    async function drainMeshLoads'),
        extract(
            '    async function drainMeshLoads',
            '    async function performMeshLoad',
        ),
        extract(
            '    async function refreshData',
            '    function scheduleMeshLiveRefresh',
        ),
        extract(
            '    function scheduleMeshLiveRefresh',
            '    async function runScheduledMeshRefresh',
        ),
        extract(
            '    async function runScheduledMeshRefresh',
            '    function exportView',
        ),
    ])

    script = f"""
    let meshLoadInFlight = null;
    let pendingMeshLoad = null;
    let meshLiveRefreshTimer = null;
    let meshLiveRefreshPending = false;
    let meshLiveRefreshNeedsNodes = false;
    let meshLiveRefreshRunning = false;
    let meshLiveRefreshRetryCount = 0;
    let meshLiveRefreshActiveReloadsNodes = false;
    const MESH_LIVE_REFRESH_MS = 10;
    const MESH_LIVE_REFRESH_RETRY_MS = 10;
    const MESH_LIVE_REFRESH_MAX_RETRIES = 2;
    let currentView = 'graph';
    let document = {{hidden: false}};
    let loadCount = 0;
    let statsCount = 0;
    let concurrent = 0;
    let maxConcurrent = 0;
    let forcedLoads = 0;
    let blockFirst = false;
    let failBlockedFirst = false;
    let firstStartedResolve;
    let firstReleaseResolve;
    let firstStarted = new Promise(resolve => firstStartedResolve = resolve);
    let firstRelease = new Promise(resolve => firstReleaseResolve = resolve);

    async function performMeshLoad(options) {{
        loadCount++;
        if (options.forceRefresh) forcedLoads++;
        concurrent++;
        maxConcurrent = Math.max(maxConcurrent, concurrent);
        if (blockFirst && loadCount === 1) {{
            firstStartedResolve();
            await firstRelease;
            concurrent--;
            if (failBlockedFirst) {{
                throw new Error('expected first-load failure');
            }}
            return;
        }}
        await new Promise(resolve => setTimeout(resolve, 2));
        concurrent--;
    }}
    async function loadStats() {{ statsCount++; }}
    function applyFilters() {{}}

    const SETTLE_TIMEOUT_MS = 2000;

    function coordinatorIdle() {{
        return !meshLiveRefreshPending
            && !meshLiveRefreshRunning
            && meshLiveRefreshTimer === null
            && meshLoadInFlight === null
            && pendingMeshLoad === null;
    }}

    // Wait for the coordinator to come to rest instead of sleeping a fixed span.
    // A 10ms timer on a loaded CI runner can fire tens of ms late, so a fixed wait
    // samples the state machine mid-retry and reports a half-finished count as a
    // logic failure. Two consecutive idle observations are required so the gap
    // between a load settling and its retry being armed is not read as rest.
    async function waitForIdle(label) {{
        const deadline = Date.now() + SETTLE_TIMEOUT_MS;
        let idleStreak = 0;
        while (idleStreak < 2) {{
            idleStreak = coordinatorIdle() ? idleStreak + 1 : 0;
            if (idleStreak < 2 && Date.now() > deadline) {{
                throw new Error(
                    `timed out after ${{SETTLE_TIMEOUT_MS}}ms waiting for ${{label}} to settle`
                );
            }}
            await new Promise(resolve => setTimeout(resolve, 1));
        }}
    }}

    {coordinator}

    (async () => {{
        for (let i = 0; i < 100; i++) scheduleMeshLiveRefresh(false);
        await waitForIdle('burst coalescing');
        const burst = {{loadCount, statsCount, maxConcurrent}};

        loadCount = 0;
        statsCount = 0;
        maxConcurrent = 0;
        document.hidden = true;
        scheduleMeshLiveRefresh(false);
        await new Promise(resolve => setTimeout(resolve, 20));
        const hiddenLoads = loadCount;
        document.hidden = false;
        await runScheduledMeshRefresh();
        const visibleLoads = loadCount;

        loadCount = 0;
        maxConcurrent = 0;
        blockFirst = true;
        failBlockedFirst = true;
        firstStarted = new Promise(resolve => firstStartedResolve = resolve);
        firstRelease = new Promise(resolve => firstReleaseResolve = resolve);
        const first = loadData({{reloadNodes: true}});
        await firstStarted;
        const trailing = loadData({{reloadNodes: false}});
        firstReleaseResolve();
        await Promise.all([first, trailing]);
        const failureQueue = {{
            loadCount,
            maxConcurrent,
            pending: pendingMeshLoad !== null
        }};

        loadCount = 0;
        statsCount = 0;
        forcedLoads = 0;
        blockFirst = true;
        failBlockedFirst = true;
        firstStarted = new Promise(resolve => firstStartedResolve = resolve);
        firstRelease = new Promise(resolve => firstReleaseResolve = resolve);
        scheduleMeshLiveRefresh(true, 10);
        await firstStarted;
        firstReleaseResolve();
        await waitForIdle('scheduled retry');
        const scheduledRetry = {{
            loadCount,
            statsCount,
            forcedLoads,
            pending: meshLiveRefreshPending,
            retries: meshLiveRefreshRetryCount
        }};

        loadCount = 0;
        statsCount = 0;
        forcedLoads = 0;
        blockFirst = false;
        failBlockedFirst = false;
        scheduleMeshLiveRefresh(false, 20);
        await refreshData();
        await waitForIdle('manual refresh absorbing the timer');
        const manualAbsorbsTimer = {{
            loadCount,
            statsCount,
            forcedLoads,
            pending: meshLiveRefreshPending
        }};

        loadCount = 0;
        statsCount = 0;
        forcedLoads = 0;
        blockFirst = true;
        failBlockedFirst = true;
        firstStarted = new Promise(resolve => firstStartedResolve = resolve);
        firstRelease = new Promise(resolve => firstReleaseResolve = resolve);
        scheduleMeshLiveRefresh(false, 20);
        const failedManualPromise = refreshData().catch(() => {{}});
        await firstStarted;
        firstReleaseResolve();
        await failedManualPromise;
        await waitForIdle('failed manual refresh retry');
        const failedManualRetries = {{
            loadCount,
            forcedLoads,
            pending: meshLiveRefreshPending,
            retries: meshLiveRefreshRetryCount
        }};

        loadCount = 0;
        statsCount = 0;
        forcedLoads = 0;
        blockFirst = true;
        failBlockedFirst = false;
        firstStarted = new Promise(resolve => firstStartedResolve = resolve);
        firstRelease = new Promise(resolve => firstReleaseResolve = resolve);
        scheduleMeshLiveRefresh(true, 0);
        await firstStarted;
        const activeManualPromise = refreshData();
        firstReleaseResolve();
        await activeManualPromise;
        await waitForIdle('manual refresh reusing the active load');
        const manualReusesActiveRefresh = {{
            loadCount,
            forcedLoads,
            pending: meshLiveRefreshPending
        }};

        console.log(JSON.stringify({{
            burst,
            hiddenLoads,
            visibleLoads,
            failureQueue,
            scheduledRetry,
            manualAbsorbsTimer,
            failedManualRetries,
            manualReusesActiveRefresh
        }}));
    }})().catch(error => {{
        console.error(error);
        process.exitCode = 1;
    }});
    """

    completed = subprocess.run(
        [shutil.which('node'), '-'],
        input=script,
        text=True,
        capture_output=True,
        check=True,
        # Generous: the script now waits on coordinator state rather than the clock,
        # so a genuine stall reports which phase failed to settle instead of dying
        # here with no detail.
        timeout=60,
    )
    result = json.loads(completed.stdout.strip())

    assert result['burst'] == {
        'loadCount': 1,
        'statsCount': 1,
        'maxConcurrent': 1,
    }
    assert result['hiddenLoads'] == 0
    assert result['visibleLoads'] == 1
    assert result['failureQueue'] == {
        'loadCount': 2,
        'maxConcurrent': 1,
        'pending': False,
    }
    assert result['scheduledRetry'] == {
        'loadCount': 2,
        'statsCount': 2,
        'forcedLoads': 2,
        'pending': False,
        'retries': 0,
    }
    assert result['manualAbsorbsTimer'] == {
        'loadCount': 1,
        'statsCount': 1,
        'forcedLoads': 1,
        'pending': False,
    }
    assert result['failedManualRetries'] == {
        'loadCount': 2,
        'forcedLoads': 2,
        'pending': False,
        'retries': 0,
    }
    assert result['manualReusesActiveRefresh'] == {
        'loadCount': 1,
        'forcedLoads': 1,
        'pending': False,
    }


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js not installed')
def test_mesh_fetch_rejects_http_errors_and_invalid_payloads():
    source = (
        Path(__file__).parents[1]
        / 'modules'
        / 'web_viewer'
        / 'templates'
        / 'mesh.html'
    ).read_text(encoding='utf-8')
    helper = source[
        source.index('    function isValidMeshNode'):
        source.index('    // Load statistics')
    ]
    script = f"""
    {helper}
    (async () => {{
        global.fetch = async () => ({{
            ok: false,
            status: 500,
            json: async () => ({{error: 'temporary failure'}})
        }});
        let httpError = '';
        try {{
            await fetchMeshJson('/api/mesh/edges', 'edges');
        }} catch (error) {{
            httpError = error.message;
        }}

        global.fetch = async () => ({{
            ok: true,
            status: 200,
            json: async () => ({{edges: null}})
        }});
        let shapeError = '';
        try {{
            await fetchMeshJson('/api/mesh/edges', 'edges');
        }} catch (error) {{
            shapeError = error.message;
        }}

        global.fetch = async () => ({{
            ok: true,
            status: 200,
            json: async () => ({{edges: [null]}})
        }});
        let nestedShapeError = '';
        try {{
            await fetchMeshJson('/api/mesh/edges', 'edges');
        }} catch (error) {{
            nestedShapeError = error.message;
        }}

        global.fetch = async () => ({{
            ok: true,
            status: 200,
            json: async () => ({{nodes: [{{}}]}})
        }});
        let emptyNodeError = '';
        try {{
            await fetchMeshJson('/api/mesh/nodes', 'nodes');
        }} catch (error) {{
            emptyNodeError = error.message;
        }}

        global.fetch = async () => ({{
            ok: true,
            status: 200,
            json: async () => ({{nodes: [{{
                public_key: 'aabb',
                prefix: 'aa',
                name: 'Node',
                latitude: '1.0',
                longitude: 2.0
            }}]}})
        }});
        let typedNodeError = '';
        try {{
            await fetchMeshJson('/api/mesh/nodes', 'nodes');
        }} catch (error) {{
            typedNodeError = error.message;
        }}
        console.log(JSON.stringify({{
            httpError,
            shapeError,
            nestedShapeError,
            emptyNodeError,
            typedNodeError
        }}));
    }})().catch(error => {{
        console.error(error);
        process.exitCode = 1;
    }});
    """
    completed = subprocess.run(
        [shutil.which('node'), '-'],
        input=script,
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )
    result = json.loads(completed.stdout.strip())
    assert result['httpError'] == (
        'Mesh request failed (500): temporary failure'
    )
    assert result['shapeError'] == (
        'Mesh response from /api/mesh/edges is missing edges'
    )
    assert result['nestedShapeError'] == (
        'Mesh response from /api/mesh/edges contains invalid edges'
    )
    assert result['emptyNodeError'] == (
        'Mesh response from /api/mesh/nodes contains invalid nodes'
    )
    assert result['typedNodeError'] == (
        'Mesh response from /api/mesh/nodes contains invalid nodes'
    )


# ---------------------------------------------------------------------------
# api_geocode_contact
# ---------------------------------------------------------------------------


class TestApiGeocodeContact:
    def test_geocode_contact_not_found(self, mock_viewer):
        with mock_viewer.app.test_client() as client:
            response = client.post(
                '/api/geocode-contact',
                data=json.dumps({'public_key': 'not:found'}),
                content_type='application/json'
            )

            assert response.status_code == 404
            data = json.loads(response.data)
            assert data['error'] == 'Contact not found'

    def test_geocode_contact_no_coordinates(self, mock_viewer):
        # Add contact without coordinates
        with sqlite3.connect(mock_viewer.db_path, timeout=60) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO complete_contact_tracking
                (public_key, name, role, latitude, longitude)
                VALUES (?, ?, ?, NULL, NULL)
            """, ("aa:bb", "No Coordinates", "client"))
            conn.commit()

        with mock_viewer.app.test_client() as client:
            response = client.post(
                '/api/geocode-contact',
                data=json.dumps({'public_key': 'aa:bb'}),
                content_type='application/json'
            )

            assert response.status_code == 400
            data = json.loads(response.data)
            assert data['error'] == 'Contact does not have valid coordinates'


# ---------------------------------------------------------------------------
# api_delete_contact
# ---------------------------------------------------------------------------


class TestApiDeleteContact:
    def test_delete_contact_not_found(self, mock_viewer):
        with mock_viewer.app.test_client() as client:
            response = client.post(
                '/api/delete-contact',
                data=json.dumps({'public_key': 'not:found'}),
                content_type='application/json'
            )

            assert response.status_code == 404
            data = json.loads(response.data)
            assert data['error'] == 'Contact not found'

    def test_delete_contact_success(self, viewer_with_db):
        # Add test contact first
        with sqlite3.connect(viewer_with_db.db_path, timeout=60) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO complete_contact_tracking
                (public_key, name, role, device_type)
                VALUES (?, ?, ?, ?)
            """, ("aa:bb:cc:dd:ee:ff:gg:hh", "Test Node", "client", "node"))
            conn.commit()

        with viewer_with_db.app.test_client() as client:
            response = client.post(
                '/api/delete-contact',
                data=json.dumps({'public_key': 'aa:bb:cc:dd:ee:ff:gg:hh'}),
                content_type='application/json'
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['success'] is True
            assert 'deleted_counts' in data


# ---------------------------------------------------------------------------
# api_decode_path
# ---------------------------------------------------------------------------


class TestApiDecodePath:
    def test_decode_path_success(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post(
                '/api/decode-path',
                data=json.dumps({'path_hex': '0102030405'}),
                content_type='application/json'
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['success'] is True
            assert 'path' in data

    def test_decode_path_no_path_hex(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post(
                '/api/decode-path',
                data=json.dumps({'invalid': 'key'}),
                content_type='application/json'
            )

            assert response.status_code == 400
            data = json.loads(response.data)
            assert data['error'] == 'path_hex is required'

    def test_decode_path_empty_path_hex(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post(
                '/api/decode-path',
                data=json.dumps({'path_hex': ''}),
                content_type='application/json'
            )

            assert response.status_code == 400
            data = json.loads(response.data)
            assert data['error'] == 'path_hex cannot be empty'


# ---------------------------------------------------------------------------
# api_resolve_path
# ---------------------------------------------------------------------------


class TestApiResolvePath:
    def test_resolve_path_success(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post(
                '/api/mesh/resolve-path',
                data=json.dumps({'path': '0102030405'}),
                content_type='application/json'
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            # Response should contain path resolution result
            assert 'node_ids' in data or 'repeaters' in data


# ---------------------------------------------------------------------------
# api_contacts_purge_preview
# ---------------------------------------------------------------------------


class TestApiContactsPurgePreview:
    def test_purge_preview_empty(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/contacts/purge-preview?days=30')

            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'count' in data


# ---------------------------------------------------------------------------
# api_feeds
# ---------------------------------------------------------------------------


class TestApiFeeds:
    def test_feeds_success(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/feeds')

            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'feeds' in data


# ---------------------------------------------------------------------------
# api_create_feed / api_update_feed / api_delete_feed
# ---------------------------------------------------------------------------


class TestApiFeedCrud:
    def test_create_feed_success(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post(
                '/api/feeds',
                data=json.dumps({
                    'feed_type': 'rss',
                    'feed_url': 'https://example.com/feed.xml',
                    'channel_name': 'general',
                    'feed_name': 'Test Feed',
                    'output_format': '{title}',
                    'check_interval_seconds': 300,
                }),
                content_type='application/json'
            )

            assert response.status_code == 200
            data = json.loads(response.data)
            assert data.get('success') is True
            # Store id for subsequent tests
            if 'id' in data:
                self._feed_id = data['id']

    def test_update_feed_success(self, viewer_with_db):
        # First create a feed
        with viewer_with_db.app.test_client() as client:
            create_response = client.post(
                '/api/feeds',
                data=json.dumps({
                    'channel': 0,
                    'feed_url': 'https://example.com/feed.xml',
                    'format': '{title}',
                    'feed_name': 'Test Feed',
                    'enabled': True
                }),
                content_type='application/json'
            )
            feed_data = json.loads(create_response.data)

        # Update the feed
        feed_id = feed_data.get('feed_id')
        if feed_id:
            with viewer_with_db.app.test_client() as client:
                response = client.put(
                    f'/api/feeds/{feed_id}',
                    data=json.dumps({
                        'feed_name': 'Updated Feed Name',
                        'feed_url': 'https://example.com/updated.xml'
                    }),
                    content_type='application/json'
                )

                assert response.status_code == 200
                data = json.loads(response.data)
                assert data.get('success') is True

    def test_update_feed_channel_name_persists(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            create_response = client.post(
                '/api/feeds',
                data=json.dumps({
                    'feed_type': 'rss',
                    'feed_url': 'https://example.com/feed.xml',
                    'channel_name': 'general',
                    'feed_name': 'Test Feed',
                    'check_interval_seconds': 300,
                }),
                content_type='application/json'
            )
            assert create_response.status_code == 200
            create_data = json.loads(create_response.data)
            feed_id = create_data.get('id')
            assert feed_id is not None

            update_response = client.put(
                f'/api/feeds/{feed_id}',
                data=json.dumps({
                    'channel_name': 'alerts'
                }),
                content_type='application/json'
            )
            assert update_response.status_code == 200
            update_data = json.loads(update_response.data)
            assert update_data.get('success') is True

            get_response = client.get(f'/api/feeds/{feed_id}')
            assert get_response.status_code == 200
            updated_feed = json.loads(get_response.data)
            assert updated_feed.get('channel_name') == 'alerts'

    def test_update_feed_channel_name_empty_rejected(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            create_response = client.post(
                '/api/feeds',
                data=json.dumps({
                    'feed_type': 'rss',
                    'feed_url': 'https://example.com/feed.xml',
                    'channel_name': 'general',
                    'feed_name': 'Test Feed',
                    'check_interval_seconds': 300,
                }),
                content_type='application/json'
            )
            assert create_response.status_code == 200
            feed_id = json.loads(create_response.data).get('id')
            assert feed_id is not None

            update_response = client.put(
                f'/api/feeds/{feed_id}',
                data=json.dumps({'channel_name': '   '}),
                content_type='application/json'
            )
            assert update_response.status_code == 500
            error_data = json.loads(update_response.data)
            assert error_data.get('error') == 'An internal error occurred'

    def test_delete_feed_success(self, viewer_with_db):
        # First create a feed
        with viewer_with_db.app.test_client() as client:
            create_response = client.post(
                '/api/feeds',
                data=json.dumps({
                    'channel': 0,
                    'feed_url': 'https://example.com/feed.xml',
                    'format': '{title}',
                    'feed_name': 'Test Feed',
                    'enabled': True
                }),
                content_type='application/json'
            )
            feed_data = json.loads(create_response.data)

        # Delete the feed
        feed_id = feed_data.get('feed_id')
        if feed_id:
            with viewer_with_db.app.test_client() as client:
                response = client.delete(f'/api/feeds/{feed_id}')

                assert response.status_code == 200
                data = json.loads(response.data)
                assert data.get('success') is True


# ---------------------------------------------------------------------------
# SocketIO handlers
# ---------------------------------------------------------------------------

# Note: SocketIO handlers are defined inside _setup_socketio_handlers method
# and use Flask-SocketIO's request context. Unit tests are complex due to
# nested function definitions and context dependencies.
# These tests verify handler registration, not internal logic.


class TestSocketIOHandlers:
    def test_socketio_handlers_are_registered(self, mock_viewer):
        # Verify that SocketIO handlers were registered during initialization
        assert hasattr(mock_viewer, 'socketio')
        assert mock_viewer.socketio is not None


# ---------------------------------------------------------------------------
# _setup_routes (route definitions)
# ---------------------------------------------------------------------------


class TestRouteDefinitions:
    def test_routes_are_defined(self, viewer_with_db):
        # Check that routes exist by testing client
        with viewer_with_db.app.test_client() as client:
            # Index page
            response = client.get('/')
            assert response.status_code == 200

            # Realtime page
            response = client.get('/realtime')
            assert response.status_code == 200

            # Logs page
            response = client.get('/logs')
            assert response.status_code == 200

            # Contacts page
            response = client.get('/contacts')
            assert response.status_code == 200

            # Greeter page
            response = client.get('/greeter')
            assert response.status_code == 200

            # Feeds page
            response = client.get('/feeds')
            assert response.status_code == 200

            # Radio page
            response = client.get('/radio')
            assert response.status_code == 200

            # Config page
            response = client.get('/config')
            assert response.status_code == 200

    def test_multibyte_routes_disabled_by_default(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/multibyte-rollout')
            assert response.status_code == 404

            response = client.get('/api/multibyte-rollout')
            assert response.status_code == 404

    def test_multibyte_routes_enabled_via_flag(self, viewer_with_db):
        viewer_with_db.multibyte_monitor_enabled = True
        with viewer_with_db.app.test_client() as client:
            response = client.get('/multibyte-rollout')
            assert response.status_code == 200

            response = client.get('/api/multibyte-rollout')
            assert response.status_code == 200


# ---------------------------------------------------------------------------
# api_config_notifications
# ---------------------------------------------------------------------------


class TestApiConfigNotifications:
    def test_get_notifications_empty(self, mock_viewer):
        with mock_viewer.app.test_client() as client:
            response = client.get('/api/config/notifications')
            assert response.status_code == 200
            data = json.loads(response.data)
            # Should have defaults
            assert 'smtp_port' in data
            assert 'smtp_security' in data

    def test_post_notifications(self, mock_viewer):
        with mock_viewer.app.test_client() as client:
            response = client.post(
                '/api/config/notifications',
                data=json.dumps({
                    'smtp_host': 'smtp.example.com',
                    'smtp_port': '587',
                    'smtp_security': 'starttls'
                }),
                content_type='application/json'
            )
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['success'] is True
            assert 'saved' in data


# ---------------------------------------------------------------------------
# api_stats
# ---------------------------------------------------------------------------


class TestApiStats:
    def test_api_stats_success(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/stats')
            assert response.status_code == 200
            data = json.loads(response.data)
            # Response contains table stats and other metadata
            assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# api_connected_clients
# ---------------------------------------------------------------------------


class TestApiConnectedClients:
    def test_api_connected_clients(self, mock_viewer):
        with mock_viewer.app.test_client() as client:
            response = client.get('/api/connected_clients')
            assert response.status_code == 200
            data = json.loads(response.data)
            # Returns list of client dicts with 'client_id', 'connected_at', etc.
            assert isinstance(data, list)


# ---------------------------------------------------------------------------
# api_contacts
# ---------------------------------------------------------------------------


class TestApiContacts:
    def test_api_contacts(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/contacts')
            assert response.status_code == 200
            data = json.loads(response.data)
            # Returns dict with 'tracking_data' and 'server_stats'
            assert 'tracking_data' in data
            assert 'server_stats' in data

    def test_contact_badge_multibyte_chunks_are_cached_and_invalidated(
        self, viewer_with_db
    ):
        with viewer_with_db._with_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO observed_paths
                    (public_key, from_prefix, to_prefix, path_hex, path_length,
                     bytes_per_hop, packet_type)
                VALUES (?, ?, ?, ?, ?, ?, 'advert')
                """,
                ("aa" * 32, "aabb", "ccdd", "aabbccdd", 4, 2),
            )
            conn.commit()
            cursor = conn.cursor()
            first = viewer_with_db._get_cached_contact_multibyte_hop_chunks(cursor)
            second = viewer_with_db._get_cached_contact_multibyte_hop_chunks(cursor)
            assert first is second
            assert {"aabb", "ccdd"}.issubset(first)

            conn.execute(
                """
                INSERT INTO observed_paths
                    (public_key, from_prefix, to_prefix, path_hex, path_length,
                     bytes_per_hop, packet_type)
                VALUES (?, ?, ?, ?, ?, ?, 'advert')
                """,
                ("bb" * 32, "112233", "445566", "112233445566", 6, 3),
            )
            conn.commit()
            third = viewer_with_db._get_cached_contact_multibyte_hop_chunks(cursor)
            assert third is not first
            assert {"112233", "445566"}.issubset(third)


# ---------------------------------------------------------------------------
# api_channel_*
# ---------------------------------------------------------------------------


class TestApiChannels:
    def test_api_channels(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/channels')
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'channels' in data


# ---------------------------------------------------------------------------
# api_radio_status
# ---------------------------------------------------------------------------


class TestApiRadioStatus:
    def test_api_radio_status(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/radio/status')
            assert response.status_code == 200
            data = json.loads(response.data)
            # Response has 'connected' and 'status_known'
            assert 'connected' in data
            assert 'status_known' in data


# ---------------------------------------------------------------------------
# api_radio_params / api_radio_advert / firmware config
# ---------------------------------------------------------------------------


def _queued_operation(viewer, op_id):
    with viewer.db_manager.connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT operation_type, payload_data, status FROM channel_operations WHERE id = ?",
            (op_id,),
        ).fetchone()
    return row


class TestApiRadioParams:
    def test_read_queues_operation(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/radio/params')
            assert response.status_code == 200
            data = json.loads(response.data)
            row = _queued_operation(viewer_with_db, data['operation_id'])
            assert row['operation_type'] == 'radio_params_read'
            assert row['status'] == 'pending'

    def test_write_node_settings_queues_operation(self, viewer_with_db):
        payload = {
            'name': 'TestBot',
            'lat': 47.6,
            'lon': -122.3,
            'adv_loc_policy': 1,
            'multi_acks': 1,
            'telemetry_mode_base': 2,
            'telemetry_mode_loc': 0,
            'telemetry_mode_env': 1,
            'rx_delay': 2.5,
            'airtime_factor': 1.0,
        }
        with viewer_with_db.app.test_client() as client:
            response = client.post('/api/radio/params', json=payload)
            assert response.status_code == 200
            data = json.loads(response.data)
            row = _queued_operation(viewer_with_db, data['operation_id'])
            assert row['operation_type'] == 'radio_params_write'
            assert json.loads(row['payload_data']) == payload

    def test_write_rejects_unknown_only_fields(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post('/api/radio/params', json={'bogus': 1})
            assert response.status_code == 400

    def test_write_rejects_long_name(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post('/api/radio/params', json={'name': 'x' * 33})
            assert response.status_code == 400

    def test_write_rejects_lat_without_lon(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post('/api/radio/params', json={'lat': 47.6})
            assert response.status_code == 400

    def test_write_rejects_out_of_range_lat(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post('/api/radio/params', json={'lat': 91.0, 'lon': 0.0})
            assert response.status_code == 400

    def test_write_rejects_bad_adv_loc_policy(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post('/api/radio/params', json={'adv_loc_policy': 2})
            assert response.status_code == 400

    def test_write_rejects_manual_add_contacts(self, viewer_with_db):
        """manual_add_contacts is owned by [Bot] auto_manage_contacts, not this API."""
        with viewer_with_db.app.test_client() as client:
            response = client.post('/api/radio/params', json={'manual_add_contacts': True})
            assert response.status_code == 400

    def test_write_drops_manual_add_contacts_from_mixed_payload(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post(
                '/api/radio/params',
                json={'manual_add_contacts': True, 'multi_acks': 1},
            )
            assert response.status_code == 200
            data = json.loads(response.data)
            row = _queued_operation(viewer_with_db, data['operation_id'])
            assert json.loads(row['payload_data']) == {'multi_acks': 1}

    def test_write_rejects_bad_multi_acks(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post('/api/radio/params', json={'multi_acks': 4})
            assert response.status_code == 400

    def test_write_rejects_bad_telemetry_mode(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post('/api/radio/params', json={'telemetry_mode_base': 3})
            assert response.status_code == 400

    def test_write_rejects_rx_delay_without_airtime_factor(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post('/api/radio/params', json={'rx_delay': 1.0})
            assert response.status_code == 400

    def test_write_rejects_out_of_range_tuning(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post(
                '/api/radio/params', json={'rx_delay': 21.0, 'airtime_factor': 1.0}
            )
            assert response.status_code == 400


class TestApiRadioAdvert:
    def test_advert_queues_operation(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post('/api/radio/advert', json={'flood': True})
            assert response.status_code == 200
            data = json.loads(response.data)
            row = _queued_operation(viewer_with_db, data['operation_id'])
            assert row['operation_type'] == 'radio_advert'
            assert json.loads(row['payload_data']) == {'flood': True}

    def test_advert_defaults_to_zero_hop(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post('/api/radio/advert', json={})
            assert response.status_code == 200
            data = json.loads(response.data)
            row = _queued_operation(viewer_with_db, data['operation_id'])
            assert json.loads(row['payload_data']) == {'flood': False}

    def test_advert_rejects_non_bool_flood(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post('/api/radio/advert', json={'flood': 'yes'})
            assert response.status_code == 400


class TestApiFirmwareConfig:
    def test_write_rejects_loop_detect(self, viewer_with_db):
        """loop.detect is repeater/room-server CLI config, not a companion setting."""
        with viewer_with_db.app.test_client() as client:
            response = client.post(
                '/api/radio/firmware/config/write', json={'loop_detect': 'on'}
            )
            assert response.status_code == 400

    def test_write_rejects_out_of_range_path_hash_mode(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post(
                '/api/radio/firmware/config/write', json={'path_hash_mode': 3}
            )
            assert response.status_code == 400

    def test_write_accepts_valid_path_hash_mode(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post(
                '/api/radio/firmware/config/write', json={'path_hash_mode': 1}
            )
            assert response.status_code == 200
            data = json.loads(response.data)
            row = _queued_operation(viewer_with_db, data['operation_id'])
            assert row['operation_type'] == 'firmware_write'
            assert json.loads(row['payload_data']) == {'path_hash_mode': 1}


# ---------------------------------------------------------------------------
# api_explorer
# ---------------------------------------------------------------------------


class TestApiExplorer:
    def test_page_loads(self, mock_viewer):
        with mock_viewer.app.test_client() as client:
            response = client.get('/api-explorer')
            assert response.status_code == 200

    def test_contains_section_headings(self, mock_viewer):
        with mock_viewer.app.test_client() as client:
            response = client.get('/api-explorer')
            body = response.data.decode()
            assert 'System' in body
            assert 'Contacts' in body
            assert 'Feeds' in body

    def test_contains_known_endpoints(self, mock_viewer):
        with mock_viewer.app.test_client() as client:
            response = client.get('/api-explorer')
            body = response.data.decode()
            assert '/api/health' in body
            assert '/api/contacts' in body
            assert '/api/mesh/nodes' in body

    def test_contains_curl_buttons(self, mock_viewer):
        with mock_viewer.app.test_client() as client:
            response = client.get('/api-explorer')
            body = response.data.decode()
            assert 'curl-btn' in body


# ---------------------------------------------------------------------------
# admin_config (resolved config.ini viewer)
# ---------------------------------------------------------------------------


class TestAdminConfig:
    def test_page_loads(self, mock_viewer):
        with mock_viewer.app.test_client() as client:
            response = client.get('/admin/config')
            assert response.status_code == 200

    def test_redacts_password_like_keys(self, mock_viewer):
        with mock_viewer.app.test_client() as client:
            response = client.get('/admin/config')
            body = response.data.decode()
            assert 'web_viewer_password' in body
            assert '●●●●●●' in body

    def test_percent_in_values_does_not_500(self, mock_viewer):
        """Literal % in ini values (e.g. humidity % RH) must not use ConfigParser interpolation."""
        # Values loaded from ini bypass set() interpolation checks; merge like a real config file.
        mock_viewer.config.read_string(
            '[Bot]\n'
            'wx_status_template = {humidity_pct:.0f} % RH) | {pressure_hpa:.0f} hPa\n'
        )
        with mock_viewer.app.test_client() as client:
            response = client.get('/admin/config')
        assert response.status_code == 200
        assert '% RH' in response.data.decode()


# ---------------------------------------------------------------------------
# error_handler
# ---------------------------------------------------------------------------


class TestErrorHandler500:
    def test_api_path_returns_json_error(self, mock_viewer):
        """500 on /api/ path returns JSON with 'error' key."""
        @mock_viewer.app.route('/api/test-500-trigger')
        def _boom():
            raise RuntimeError("test 500")

        # PROPAGATE_EXCEPTIONS must be False so the 500 handler fires instead of re-raising
        mock_viewer.app.config['PROPAGATE_EXCEPTIONS'] = False
        try:
            with mock_viewer.app.test_client() as client:
                response = client.get('/api/test-500-trigger',
                                      headers={'Accept': 'application/json'})
                assert response.status_code == 500
                data = json.loads(response.data)
                assert 'error' in data
        finally:
            mock_viewer.app.config['PROPAGATE_EXCEPTIONS'] = True

    def test_browser_path_returns_html(self, mock_viewer):
        """500 on non-API path returns HTML page."""
        @mock_viewer.app.route('/test-500-html-trigger')
        def _boom_html():
            raise RuntimeError("test 500 html")

        mock_viewer.app.config['PROPAGATE_EXCEPTIONS'] = False
        try:
            with mock_viewer.app.test_client() as client:
                response = client.get('/test-500-html-trigger',
                                      headers={'Accept': 'text/html'})
                assert response.status_code == 500
                assert b'Internal Server Error' in response.data
        finally:
            mock_viewer.app.config['PROPAGATE_EXCEPTIONS'] = True


# ---------------------------------------------------------------------------
# /api/maintenance/status
# ---------------------------------------------------------------------------


class TestApiMaintenanceStatus:
    def test_returns_all_status_keys(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/maintenance/status')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'data_retention_ran_at' in data
        assert 'nightly_email_ran_at' in data
        assert 'db_backup_ran_at' in data
        assert 'log_rotation_applied_at' in data

    def test_empty_string_for_unset_keys(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/maintenance/status')
        data = json.loads(response.data)
        # Nothing written to DB yet — all values should be empty strings
        assert all(v == '' for v in data.values())


# ---------------------------------------------------------------------------
# /api/admin/zombie-recover
# ---------------------------------------------------------------------------


class TestApiZombieRecover:
    def test_clears_zombie_metadata(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post(
                '/api/admin/zombie-recover',
                headers={'X-Requested-With': 'XMLHttpRequest'},
            )
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        # Verify metadata was cleared
        assert viewer_with_db.db_manager.get_metadata('bot.radio_zombie') == 'false'


# ---------------------------------------------------------------------------
# /api/admin/radio-offline-clear
# ---------------------------------------------------------------------------


class TestApiRadioOfflineClear:
    def test_clears_radio_offline_metadata(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.post(
                '/api/admin/radio-offline-clear',
                headers={'X-Requested-With': 'XMLHttpRequest'},
            )
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert viewer_with_db.db_manager.get_metadata('bot.radio_offline') == 'false'


# ---------------------------------------------------------------------------
# /mesh page
# ---------------------------------------------------------------------------


class TestMeshPage:
    def test_mesh_page_loads_200(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/mesh')
        assert response.status_code == 200

    def test_mesh_page_with_prefix_bytes_config(self, viewer_with_db):
        viewer_with_db.config.set('Bot', 'prefix_bytes', '2')
        with viewer_with_db.app.test_client() as client:
            response = client.get('/mesh')
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------


class TestApiHealth:
    def test_returns_healthy_by_default(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/health')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['status'] == 'healthy'
        assert 'connected_clients' in data
        assert 'version' in data
        assert data['radio_zombie'] is False

    def test_returns_degraded_when_zombie(self, viewer_with_db):
        viewer_with_db.db_manager.set_metadata('bot.radio_zombie', 'true')
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/health')
        data = json.loads(response.data)
        assert data['status'] == 'degraded'
        assert data['radio_zombie'] is True


# ---------------------------------------------------------------------------
# /api/banner-status
# ---------------------------------------------------------------------------


class TestApiBannerStatus:
    def test_returns_all_banner_keys(self, viewer_with_db):
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/banner-status')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'radio_zombie' in data
        assert 'radio_offline' in data
        assert 'bot_initializing' in data

    def test_reflects_zombie_state(self, viewer_with_db):
        viewer_with_db.db_manager.set_metadata('bot.radio_zombie', 'true')
        with viewer_with_db.app.test_client() as client:
            response = client.get('/api/banner-status')
        data = json.loads(response.data)
        assert data['radio_zombie'] is True


# ---------------------------------------------------------------------------
# Favicon / static asset routes
# ---------------------------------------------------------------------------


class TestFaviconRoutes:
    """Favicon routes call send_from_directory — patch it to avoid fs dependency."""

    def _check_route(self, viewer_with_db, path):
        from unittest.mock import patch as _patch
        with viewer_with_db.app.test_client() as client:
            with _patch("modules.web_viewer.app.send_from_directory",
                        return_value=viewer_with_db.app.response_class("ok", status=200)):
                response = client.get(path)
        assert response.status_code == 200

    def test_apple_touch_icon(self, viewer_with_db):
        self._check_route(viewer_with_db, '/apple-touch-icon.png')

    def test_favicon_32x32(self, viewer_with_db):
        self._check_route(viewer_with_db, '/favicon-32x32.png')

    def test_favicon_16x16(self, viewer_with_db):
        self._check_route(viewer_with_db, '/favicon-16x16.png')

    def test_site_webmanifest(self, viewer_with_db):
        self._check_route(viewer_with_db, '/site.webmanifest')

    def test_favicon_ico(self, viewer_with_db):
        self._check_route(viewer_with_db, '/favicon.ico')
