"""Tests for modules.db_manager."""

import sqlite3
import json

import pytest
from unittest.mock import Mock

from modules.db_manager import DBManager


@pytest.fixture
def db(mock_logger, tmp_path):
    """File-based DBManager for testing. _init_database() auto-creates core tables."""
    bot = Mock()
    bot.logger = mock_logger
    return DBManager(bot, str(tmp_path / "test.db"))


class TestGeocoding:
    """Tests for geocoding cache."""

    def test_cache_and_retrieve_geocoding(self, db):
        db.cache_geocoding("Seattle, WA", 47.6062, -122.3321)
        lat, lon = db.get_cached_geocoding("Seattle, WA")
        assert abs(lat - 47.6062) < 0.001
        assert abs(lon - (-122.3321)) < 0.001

    def test_get_cached_geocoding_miss(self, db):
        lat, lon = db.get_cached_geocoding("Nonexistent City")
        assert lat is None
        assert lon is None

    def test_cache_geocoding_overwrites_existing(self, db):
        db.cache_geocoding("Test", 10.0, 20.0)
        db.cache_geocoding("Test", 30.0, 40.0)
        lat, lon = db.get_cached_geocoding("Test")
        assert abs(lat - 30.0) < 0.001
        assert abs(lon - 40.0) < 0.001

    def test_cache_geocoding_invalid_hours_logged(self, db):
        """Invalid cache_hours is caught and logged, not raised."""
        db.cache_geocoding("Test", 10.0, 20.0, cache_hours=0)
        db.bot.logger.error.assert_called()
        # Verify it did not store anything
        lat, lon = db.get_cached_geocoding("Test")
        assert lat is None


class TestGenericCache:
    """Tests for generic cache."""

    def test_cache_and_retrieve_value(self, db):
        db.cache_value("weather_key", "sunny", "weather")
        result = db.get_cached_value("weather_key", "weather")
        assert result == "sunny"

    def test_get_cached_value_miss(self, db):
        assert db.get_cached_value("nonexistent", "any") is None

    def test_different_keys_stored_independently(self, db):
        db.cache_value("key_a", "value_a", "weather")
        db.cache_value("key_b", "value_b", "weather")
        assert db.get_cached_value("key_a", "weather") == "value_a"
        assert db.get_cached_value("key_b", "weather") == "value_b"

    def test_cache_json_round_trip(self, db):
        data = {"temp": 72, "conditions": "clear", "nested": {"wind": 5}}
        db.cache_json("forecast", data, "weather")
        result = db.get_cached_json("forecast", "weather")
        assert result == data

    def test_get_cached_json_invalid_json(self, db):
        """Manually insert invalid JSON; get_cached_json returns None."""
        with sqlite3.connect(str(db.db_path)) as conn:
            conn.execute(
                "INSERT INTO generic_cache (cache_key, cache_value, cache_type, expires_at) "
                "VALUES (?, ?, ?, datetime('now', '+24 hours'))",
                ("bad_json", "not{valid}json", "test"),
            )
            conn.commit()
        assert db.get_cached_json("bad_json", "test") is None


class TestCacheCleanup:
    """Tests for cache expiry cleanup."""

    def test_cleanup_expired_deletes_old(self, db):
        db.cache_value("old_key", "old_val", "test")
        # Manually set expires_at to the past
        with sqlite3.connect(str(db.db_path)) as conn:
            conn.execute(
                "UPDATE generic_cache SET expires_at = datetime('now', '-1 hours') "
                "WHERE cache_key = 'old_key'"
            )
            conn.commit()
        db.cleanup_expired_cache()
        assert db.get_cached_value("old_key", "test") is None

    def test_cleanup_expired_preserves_valid(self, db):
        db.cache_value("fresh_key", "fresh_val", "test", cache_hours=720)
        db.cleanup_expired_cache()
        assert db.get_cached_value("fresh_key", "test") == "fresh_val"


class TestTableManagement:
    """Tests for table creation whitelist."""

    def test_create_table_allowed(self, db):
        db.create_table(
            "greeted_users",
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL",
        )
        with sqlite3.connect(str(db.db_path)) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='greeted_users'"
            )
            assert cursor.fetchone() is not None

    def test_create_table_disallowed_raises(self, db):
        with pytest.raises(ValueError, match="not in allowed tables"):
            db.create_table("not_allowed", "id INTEGER PRIMARY KEY")

    def test_create_table_sql_injection_name_raises(self, db):
        with pytest.raises(ValueError):
            db.create_table("DROP TABLE users; --", "id INTEGER PRIMARY KEY")


class TestExecuteQuery:
    """Tests for raw query execution."""

    def test_execute_query_returns_dicts(self, db):
        db.set_metadata("test_key", "test_value")
        rows = db.execute_query("SELECT * FROM bot_metadata WHERE key = ?", ("test_key",))
        assert len(rows) == 1
        assert rows[0]["key"] == "test_key"
        assert rows[0]["value"] == "test_value"

    def test_execute_update_returns_rowcount(self, db):
        db.set_metadata("del_key", "del_value")
        count = db.execute_update(
            "DELETE FROM bot_metadata WHERE key = ?", ("del_key",)
        )
        assert count == 1


class TestMetadata:
    """Tests for bot metadata storage."""

    def test_set_and_get_metadata(self, db):
        db.set_metadata("version", "1.2.3")
        assert db.get_metadata("version") == "1.2.3"

    def test_get_metadata_miss(self, db):
        assert db.get_metadata("nonexistent") is None

    def test_bot_start_time_round_trip(self, db):
        ts = 1234567890.5
        db.set_bot_start_time(ts)
        assert db.get_bot_start_time() == ts


class TestCacheHoursValidation:
    """Tests for cache_hours boundary validation."""

    def test_boundary_values(self, db):
        # Valid boundaries
        db.cache_value("k1", "v1", "t", cache_hours=1)
        assert db.get_cached_value("k1", "t") == "v1"

        db.cache_value("k2", "v2", "t", cache_hours=87600)
        assert db.get_cached_value("k2", "t") == "v2"

        # Invalid boundaries — caught and logged, not stored
        db.cache_value("k3", "v3", "t", cache_hours=0)
        db.bot.logger.error.assert_called()
        assert db.get_cached_value("k3", "t") is None

        db.bot.logger.error.reset_mock()
        db.cache_value("k4", "v4", "t", cache_hours=87601)
        db.bot.logger.error.assert_called()
        assert db.get_cached_value("k4", "t") is None
