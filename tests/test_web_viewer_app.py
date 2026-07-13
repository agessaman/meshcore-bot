"""Tests for modules.web_viewer.app — BotDataViewer Flask app."""

import json
import sqlite3
import time
from configparser import ConfigParser
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


class TestApiMeshEdgesEvidence:
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
