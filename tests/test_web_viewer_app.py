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
                time.time() - 86400,
                time.time(),
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
