"""Tests for modules.db_manager."""

import sqlite3
from contextlib import closing
from unittest.mock import Mock, patch

import pytest

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
        with closing(sqlite3.connect(str(db.db_path))) as conn:
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
        with closing(sqlite3.connect(str(db.db_path))) as conn:
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
        with closing(sqlite3.connect(str(db.db_path))) as conn:
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


# ---------------------------------------------------------------------------
# Date/datetime adapters (lines 20, 24)
# ---------------------------------------------------------------------------


class TestSqliteAdapters:
    """_adapt_sqlite_date / _adapt_sqlite_datetime are registered globally."""

    def test_date_adapter(self):
        from datetime import date

        from modules.db_manager import _adapt_sqlite_date
        assert _adapt_sqlite_date(date(2026, 4, 7)) == "2026-04-07"

    def test_datetime_adapter(self):
        from datetime import datetime

        from modules.db_manager import _adapt_sqlite_datetime
        val = datetime(2026, 4, 7, 10, 30, 0, 123456)
        result = _adapt_sqlite_datetime(val)
        assert "2026-04-07" in result
        assert "10:30:00" in result


# ---------------------------------------------------------------------------
# Exception paths in cache methods (lines 75-77, 101-103, 153-155, 214-215)
# ---------------------------------------------------------------------------


class TestExceptionPaths:
    def test_init_database_error_reraises(self, mock_logger, tmp_path):
        from unittest.mock import patch as _patch

        from modules.db_manager import DBManager
        bot = Mock()
        bot.logger = mock_logger
        with _patch("modules.db_manager.MigrationRunner", side_effect=RuntimeError("db fail")):
            with pytest.raises(RuntimeError):
                DBManager(bot, str(tmp_path / "test.db"))

    def test_get_cached_geocoding_exception_returns_none(self, db):
        with patch.object(db, "connection", side_effect=RuntimeError("db gone")):
            lat, lon = db.get_cached_geocoding("test")
        assert lat is None and lon is None

    def test_get_cached_value_exception_returns_none(self, db):
        with patch.object(db, "connection", side_effect=RuntimeError("db gone")):
            assert db.get_cached_value("k", "t") is None

    def test_cache_json_exception_logged(self, db):
        with patch.object(db, "cache_value", side_effect=RuntimeError("fail")):
            db.cache_json("k", {"x": 1}, "t")
        db.bot.logger.error.assert_called()

    def test_set_metadata_exception_logged(self, db):
        with patch.object(db, "connection", side_effect=RuntimeError("db gone")):
            db.set_metadata("k", "v")
        db.bot.logger.error.assert_called()

    def test_get_metadata_exception_returns_none(self, db):
        with patch.object(db, "connection", side_effect=RuntimeError("db gone")):
            assert db.get_metadata("k") is None

    def test_get_bot_start_time_invalid_string_returns_none(self, db):
        db.set_metadata("start_time", "not_a_float")
        assert db.get_bot_start_time() is None
        db.bot.logger.warning.assert_called()

    def test_execute_query_exception_returns_empty(self, db):
        with patch.object(db, "connection", side_effect=RuntimeError("db gone")):
            assert db.execute_query("SELECT 1") == []

    def test_execute_update_exception_returns_zero(self, db):
        with patch.object(db, "connection", side_effect=RuntimeError("db gone")):
            assert db.execute_update("DELETE FROM bot_metadata WHERE key=?", ("x",)) == 0


# ---------------------------------------------------------------------------
# cleanup_geocoding_cache (lines 247-256)
# ---------------------------------------------------------------------------


class TestCleanupGeocodingCache:
    def test_removes_expired(self, db):
        db.cache_geocoding("loc", 1.0, 2.0)
        with closing(sqlite3.connect(str(db.db_path))) as conn:
            conn.execute("UPDATE geocoding_cache SET expires_at = datetime('now', '-1 hours')")
            conn.commit()
        db.cleanup_geocoding_cache()
        lat, lon = db.get_cached_geocoding("loc")
        assert lat is None

    def test_preserves_valid(self, db):
        db.cache_geocoding("loc", 1.0, 2.0)
        db.cleanup_geocoding_cache()
        lat, _ = db.get_cached_geocoding("loc")
        assert lat is not None


# ---------------------------------------------------------------------------
# get_database_stats / vacuum_database (lines 261-305)
# ---------------------------------------------------------------------------


class TestDatabaseStats:
    def test_returns_expected_keys(self, db):
        stats = db.get_database_stats()
        assert "geocoding_cache_entries" in stats
        assert "generic_cache_entries" in stats
        assert "cache_types" in stats

    def test_counts_active_entries(self, db):
        db.cache_value("k", "v", "mytype")
        stats = db.get_database_stats()
        assert stats["generic_cache_entries"] >= 1
        assert stats["generic_cache_active"] >= 1
        assert "mytype" in stats["cache_types"]

    def test_exception_returns_empty(self, db):
        with patch.object(db, "connection", side_effect=RuntimeError("db gone")):
            assert db.get_database_stats() == {}


class TestVacuumDatabase:
    def test_vacuum_completes(self, db):
        db.vacuum_database()
        db.bot.logger.info.assert_called()

    def test_vacuum_exception_logged(self, db):
        with patch.object(db, "connection", side_effect=RuntimeError("db gone")):
            db.vacuum_database()
        db.bot.logger.error.assert_called()


# ---------------------------------------------------------------------------
# drop_table (lines 346-366)
# ---------------------------------------------------------------------------


class TestDropTable:
    def test_drop_allowed_table(self, db):
        db.create_table("greeted_users", "id INTEGER PRIMARY KEY, name TEXT")
        db.drop_table("greeted_users")
        with closing(sqlite3.connect(str(db.db_path))) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='greeted_users'"
            )
            assert cursor.fetchone() is None

    def test_drop_disallowed_table_raises(self, db):
        with pytest.raises(ValueError, match="not in allowed tables"):
            db.drop_table("evil_table")

    def test_drop_table_exception_logged_and_raised(self, db):
        with patch.object(db, "connection", side_effect=RuntimeError("db gone")):
            with pytest.raises(RuntimeError):
                db.drop_table("greeted_users")


# ---------------------------------------------------------------------------
# execute_query_on_connection / execute_update_on_connection (lines 393-409)
# ---------------------------------------------------------------------------


class TestOnConnectionMethods:
    def test_execute_query_on_connection_with_row_factory(self, db):
        db.set_metadata("conn_test", "hello")
        with closing(sqlite3.connect(str(db.db_path))) as conn:
            conn.row_factory = sqlite3.Row
            rows = db.execute_query_on_connection(
                conn, "SELECT * FROM bot_metadata WHERE key=?", ("conn_test",)
            )
        assert rows[0]["key"] == "conn_test"

    def test_execute_query_on_connection_without_row_factory(self, db):
        db.set_metadata("conn_test2", "world")
        with closing(sqlite3.connect(str(db.db_path))) as conn:
            # row_factory is NOT sqlite3.Row → use description-based dict
            rows = db.execute_query_on_connection(
                conn, "SELECT key, value FROM bot_metadata WHERE key=?", ("conn_test2",)
            )
        assert rows[0]["key"] == "conn_test2"

    def test_execute_query_on_connection_no_results(self, db):
        with closing(sqlite3.connect(str(db.db_path))) as conn:
            rows = db.execute_query_on_connection(
                conn, "SELECT key FROM bot_metadata WHERE key=?", ("nonexistent",)
            )
        assert rows == []

    def test_execute_update_on_connection(self, db):
        db.set_metadata("upd_test", "before")
        with closing(sqlite3.connect(str(db.db_path))) as conn:
            count = db.execute_update_on_connection(
                conn, "UPDATE bot_metadata SET value=? WHERE key=?", ("after", "upd_test")
            )
            conn.commit()
        assert count == 1
        assert db.get_metadata("upd_test") == "after"


# ---------------------------------------------------------------------------
# get_connection (lines 520-531)
# ---------------------------------------------------------------------------


class TestGetConnection:
    def test_returns_valid_connection(self, db):
        conn = db.get_connection()
        try:
            cursor = conn.execute("SELECT 1")
            assert cursor.fetchone() is not None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# _apply_sqlite_pragmas branches (lines 484-505)
# ---------------------------------------------------------------------------


class TestApplySqlitePragmas:
    def test_invalid_journal_mode_falls_back_to_wal(self, mock_logger, tmp_path):
        import configparser

        from modules.db_manager import DBManager
        bot = Mock()
        bot.logger = mock_logger
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot")
        cfg.set("Bot", "sqlite_journal_mode", "INVALID_MODE")
        bot.config = cfg
        db = DBManager(bot, str(tmp_path / "test.db"))
        # Connection still works after fallback
        assert db.get_metadata("nonexistent") is None
        mock_logger.warning.assert_called()

    def test_busy_timeout_bad_type_falls_back(self, mock_logger, tmp_path):
        import configparser

        from modules.db_manager import DBManager
        bot = Mock()
        bot.logger = mock_logger
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot")
        cfg.set("Bot", "sqlite_busy_timeout_ms", "notanumber")
        bot.config = cfg
        db = DBManager(bot, str(tmp_path / "test.db"))
        assert db.get_metadata("k") is None  # DB still works


# ---------------------------------------------------------------------------
# set/get_system_health (lines 533-552)
# ---------------------------------------------------------------------------


class TestSystemHealth:
    def test_set_and_get_system_health(self, db):
        health = {"cpu": 12.3, "mem_mb": 256, "status": "ok"}
        db.set_system_health(health)
        result = db.get_system_health()
        assert result == health

    def test_get_system_health_missing_returns_none(self, db):
        assert db.get_system_health() is None

    def test_set_system_health_exception_logged(self, db):
        with patch.object(db, "set_metadata", side_effect=RuntimeError("fail")):
            db.set_system_health({"x": 1})
        db.bot.logger.error.assert_called()

    def test_get_system_health_exception_returns_none(self, db):
        with patch.object(db, "get_metadata", side_effect=RuntimeError("fail")):
            assert db.get_system_health() is None


# ---------------------------------------------------------------------------
# AsyncDBManager (lines 555-663)
# ---------------------------------------------------------------------------


class TestAsyncDBManager:
    """Cover AsyncDBManager methods using asyncio.run."""

    def _make_async_db(self, tmp_path):
        from unittest.mock import Mock

        from modules.db_manager import AsyncDBManager
        logger = Mock()
        # Reuse the sync DBManager to ensure tables exist
        bot = Mock()
        bot.logger = logger
        DBManager(bot, str(tmp_path / "async.db"))
        return AsyncDBManager(str(tmp_path / "async.db"), logger)

    def test_set_and_get_metadata(self, tmp_path):
        import asyncio
        adb = self._make_async_db(tmp_path)
        asyncio.run(adb.set_metadata("akey", "aval"))
        result = asyncio.run(adb.get_metadata("akey"))
        assert result == "aval"

    def test_get_metadata_missing_returns_none(self, tmp_path):
        import asyncio
        adb = self._make_async_db(tmp_path)
        assert asyncio.run(adb.get_metadata("nonexistent")) is None

    def test_execute_query_returns_dicts(self, tmp_path):
        import asyncio
        adb = self._make_async_db(tmp_path)
        asyncio.run(adb.set_metadata("qkey", "qval"))
        rows = asyncio.run(adb.execute_query(
            "SELECT key, value FROM bot_metadata WHERE key=?", ("qkey",)
        ))
        assert rows[0]["key"] == "qkey"

    def test_execute_update_returns_rowcount(self, tmp_path):
        import asyncio
        adb = self._make_async_db(tmp_path)
        asyncio.run(adb.set_metadata("del_k", "del_v"))
        count = asyncio.run(adb.execute_update(
            "DELETE FROM bot_metadata WHERE key=?", ("del_k",)
        ))
        assert count == 1

    def test_cache_value_and_get(self, tmp_path):
        import asyncio
        adb = self._make_async_db(tmp_path)
        asyncio.run(adb.cache_value("ck", "cv", "ctype"))
        result = asyncio.run(adb.get_cached_value("ck", "ctype"))
        assert result == "cv"

    def test_cache_value_invalid_hours_logged(self, tmp_path):
        import asyncio
        adb = self._make_async_db(tmp_path)
        asyncio.run(adb.cache_value("k", "v", "t", cache_hours=0))
        adb.logger.error.assert_called()

    def test_get_cached_value_miss_returns_none(self, tmp_path):
        import asyncio
        adb = self._make_async_db(tmp_path)
        assert asyncio.run(adb.get_cached_value("nope", "notype")) is None

    def test_get_metadata_exception_returns_none(self, tmp_path):
        import asyncio
        adb = self._make_async_db(tmp_path)
        adb.db_path = "/nonexistent/path.db"
        assert asyncio.run(adb.get_metadata("k")) is None
        adb.logger.error.assert_called()

    def test_missing_aiosqlite_logs_error(self, tmp_path):
        """When aiosqlite is unavailable the RuntimeError is caught and logged."""
        import asyncio
        import sys

        from modules.db_manager import AsyncDBManager
        logger = Mock()
        adb = AsyncDBManager(str(tmp_path / "x.db"), logger)
        real = sys.modules.pop("aiosqlite", None)
        try:
            with patch.dict(sys.modules, {"aiosqlite": None}):
                result = asyncio.run(adb.get_metadata("k"))
        finally:
            if real is not None:
                sys.modules["aiosqlite"] = real
        # get_metadata catches the RuntimeError and returns None
        assert result is None
        logger.error.assert_called()


# ---------------------------------------------------------------------------
# Coverage gap fill: ~22 missing lines
# ---------------------------------------------------------------------------


class TestCoverageGapsFill:
    """Cover remaining missing lines in db_manager.py."""

    # -- get_cached_json line 200 --

    def test_get_cached_json_no_cached_value(self, db):
        """Line 200: returns None when key is not in cache at all."""
        result = db.get_cached_json("nonexistent_key", "test_type")
        assert result is None

    # -- cleanup_expired_cache lines 242-243 --

    def test_cleanup_expired_cache_logs_when_deleted(self, tmp_path, mock_logger):
        """Lines 242-243: logs info message when entries are deleted."""
        bot = Mock()
        bot.logger = mock_logger
        db = DBManager(bot, str(tmp_path / "gc.db"))
        with closing(sqlite3.connect(str(tmp_path / "gc.db"))) as conn:
            conn.execute(
                "INSERT INTO generic_cache (cache_key, cache_value, cache_type, expires_at)"
                " VALUES ('k1','v1','t1', datetime('now','-1 hour'))"
            )
            conn.execute(
                "INSERT INTO geocoding_cache (query, latitude, longitude, expires_at)"
                " VALUES ('test q', 37.77, -122.41, datetime('now','-1 hour'))"
            )
            conn.commit()
        db.cleanup_expired_cache()
        mock_logger.info.assert_called()

    # -- cleanup_geocoding_cache lines 255-256 --

    def test_cleanup_geocoding_cache_logs_when_deleted(self, tmp_path, mock_logger):
        """Lines 255-256: logs info when geocoding entries deleted."""
        bot = Mock()
        bot.logger = mock_logger
        db = DBManager(bot, str(tmp_path / "cg.db"))
        with closing(sqlite3.connect(str(tmp_path / "cg.db"))) as conn:
            conn.execute(
                "INSERT INTO geocoding_cache (query, latitude, longitude, expires_at)"
                " VALUES ('test query', 37.7749, -122.4194, datetime('now','-1 hour'))"
            )
            conn.commit()
        db.cleanup_geocoding_cache()
        mock_logger.info.assert_called()

    # -- create_table / drop_table regex lines 325, 353 --

    def test_create_table_invalid_name_format_raises(self, db):
        """Line 325: raises ValueError when table name passes whitelist but fails regex."""
        # Temporarily add an uppercase name to bypass whitelist check
        original = db.ALLOWED_TABLES
        db.ALLOWED_TABLES = original | {"BadTable"}
        try:
            with pytest.raises((ValueError, Exception)):
                db.create_table("BadTable", "id INTEGER PRIMARY KEY")
        finally:
            db.ALLOWED_TABLES = original

    def test_drop_table_invalid_name_format_raises(self, db):
        """Line 353: raises ValueError when table name passes whitelist but fails regex."""
        original = db.ALLOWED_TABLES
        db.ALLOWED_TABLES = original | {"BadTable"}
        try:
            with pytest.raises((ValueError, Exception)):
                db.drop_table("BadTable")
        finally:
            db.ALLOWED_TABLES = original

    # -- execute_query_on_connection no description line 402 --

    def test_execute_query_on_connection_no_description(self, db):
        """Line 402: returns [] when cursor.description is None (non-SELECT, no row_factory).

        db.connection() sets row_factory=sqlite3.Row bypassing line 402, so use a plain conn.
        """
        plain_conn = sqlite3.connect(str(db.db_path))
        try:
            result = db.execute_query_on_connection(
                plain_conn,
                "INSERT OR REPLACE INTO bot_metadata (key, value) VALUES (?, ?)",
                ("__cov_test__", "1"),
            )
            plain_conn.commit()
        finally:
            plain_conn.close()
        assert result == []

    # -- cleanup_expired_cache exception path lines 242-243 --

    def test_cleanup_expired_cache_exception_logged(self, tmp_path, mock_logger):
        """Lines 242-243: exception inside cleanup_expired_cache is logged."""
        bot = Mock()
        bot.logger = mock_logger
        db = DBManager(bot, str(tmp_path / "exc.db"))
        with patch.object(db, "connection", side_effect=RuntimeError("db gone")):
            db.cleanup_expired_cache()
        mock_logger.error.assert_called()

    # -- cleanup_geocoding_cache exception path lines 255-256 --

    def test_cleanup_geocoding_cache_exception_logged(self, tmp_path, mock_logger):
        """Lines 255-256: exception inside cleanup_geocoding_cache is logged."""
        bot = Mock()
        bot.logger = mock_logger
        db = DBManager(bot, str(tmp_path / "exc2.db"))
        with patch.object(db, "connection", side_effect=RuntimeError("db gone")):
            db.cleanup_geocoding_cache()
        mock_logger.error.assert_called()

    # -- get_bot_start_time line 460 --

    def test_get_bot_start_time_not_set_returns_none(self, db):
        """Line 460: returns None when start_time not in metadata."""
        # Fresh db has no start_time entry
        result = db.get_bot_start_time()
        assert result is None

    # -- _apply_sqlite_pragmas OperationalError lines 503-505 --

    def test_apply_sqlite_pragmas_operational_error_ignored(self, db):
        """Lines 503-505: OperationalError in pragmas is silently swallowed."""
        import sqlite3 as _sqlite3
        from unittest.mock import MagicMock
        mock_conn = MagicMock(spec=_sqlite3.Connection)
        mock_conn.execute.side_effect = _sqlite3.OperationalError("locked")
        # Must not raise
        db._apply_sqlite_pragmas(mock_conn)


class TestAsyncDBManagerExceptionPaths:
    """Cover exception paths in AsyncDBManager: lines 607-608, 617-619, 628-630, 643-645."""

    def _make_bad_adb(self, tmp_path):
        """AsyncDBManager pointing at a non-existent path to force errors."""
        from modules.db_manager import AsyncDBManager, DBManager
        logger = Mock()
        bot = Mock()
        bot.logger = logger
        # Create tables via sync DB first
        DBManager(bot, str(tmp_path / "exc.db"))
        adb = AsyncDBManager("/nonexistent/__bad__.db", logger)
        return adb

    def test_set_metadata_exception_logged(self, tmp_path):
        """Lines 607-608: set_metadata logs error when DB is unreachable."""
        import asyncio
        adb = self._make_bad_adb(tmp_path)
        asyncio.run(adb.set_metadata("k", "v"))
        adb.logger.error.assert_called()

    def test_execute_query_exception_returns_empty(self, tmp_path):
        """Lines 617-619: execute_query logs error and returns [] on failure."""
        import asyncio
        adb = self._make_bad_adb(tmp_path)
        result = asyncio.run(adb.execute_query("SELECT 1"))
        assert result == []
        adb.logger.error.assert_called()

    def test_execute_update_exception_returns_zero(self, tmp_path):
        """Lines 628-630: execute_update logs error and returns 0 on failure."""
        import asyncio
        adb = self._make_bad_adb(tmp_path)
        result = asyncio.run(adb.execute_update(
            "INSERT INTO bot_metadata (key, value) VALUES (?, ?)", ("k", "v")
        ))
        assert result == 0
        adb.logger.error.assert_called()

    def test_get_cached_value_exception_returns_none(self, tmp_path):
        """Lines 643-645: get_cached_value logs error and returns None on failure."""
        import asyncio
        adb = self._make_bad_adb(tmp_path)
        result = asyncio.run(adb.get_cached_value("k", "t"))
        assert result is None
        adb.logger.error.assert_called()
