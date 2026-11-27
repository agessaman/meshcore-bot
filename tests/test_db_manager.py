"""Tests for modules.db_manager."""

import configparser
import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest

from modules.db_manager import DBManager


@pytest.fixture
def db(mock_logger, tmp_path):
    """File-based DBManager for testing. _init_database() auto-creates core tables."""
    bot = Mock()
    bot.logger = mock_logger
    return DBManager(bot, str(tmp_path / "test.db"))


class TestDatabaseInitialization:
    def test_missing_parent_logs_path_diagnostics(self, mock_logger, tmp_path):
        bot = Mock()
        bot.logger = mock_logger
        db_path = tmp_path / "missing" / "test.db"

        with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
            DBManager(bot, str(db_path))

        logged = " ".join(
            str(arg)
            for call in mock_logger.error.call_args_list
            for arg in call.args
        )
        assert str(db_path) in logged
        assert "parent=" in logged
        assert "exists=False" in logged

    def test_journal_mode_is_initialized_once_per_manager(
        self, mock_logger, monkeypatch, tmp_path
    ):
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
        bot = Mock()
        bot.logger = mock_logger
        bot.config = configparser.ConfigParser()
        bot.config["Bot"] = {"sqlite_journal_mode": "WAL"}

        manager = DBManager(bot, str(tmp_path / "journal-once.db"))
        manager.get_metadata("missing-one")
        manager.get_metadata("missing-two")

        journal_statements = [
            sql for sql in statements if sql.upper().startswith("PRAGMA JOURNAL_MODE=")
        ]
        foreign_key_statements = [
            sql for sql in statements if sql.upper().startswith("PRAGMA FOREIGN_KEYS=")
        ]
        assert journal_statements == ["PRAGMA journal_mode=WAL"]
        assert len(foreign_key_statements) == 3

    def test_journal_mode_retries_after_database_lock(self, db):
        db._journal_mode_initialized.clear()
        locked_connection = Mock()

        def locked_execute(sql):
            if sql.upper().startswith("PRAGMA JOURNAL_MODE="):
                raise sqlite3.OperationalError("database is locked")
            return Mock()

        locked_connection.execute.side_effect = locked_execute
        db._apply_sqlite_pragmas(locked_connection)
        assert "Bot" not in db._journal_mode_initialized

        available_connection = Mock()
        db._apply_sqlite_pragmas(available_connection)
        assert "Bot" in db._journal_mode_initialized
        assert any(
            call.args[0].upper().startswith("PRAGMA JOURNAL_MODE=")
            for call in available_connection.execute.call_args_list
        )

    def test_config_sections_do_not_starve_each_other(self, db):
        """[Bot] and [Web_Viewer] are read by different callers against one file.

        A single shared flag would let whichever ran first suppress the other's
        journal-mode setup entirely.
        """
        db._journal_mode_initialized.clear()

        bot_conn = Mock()
        db._apply_sqlite_pragmas(bot_conn, for_web_viewer=False)
        viewer_conn = Mock()
        db._apply_sqlite_pragmas(viewer_conn, for_web_viewer=True)

        def journal_pragmas(conn):
            return [
                c.args[0] for c in conn.execute.call_args_list
                if c.args[0].upper().startswith("PRAGMA JOURNAL_MODE=")
            ]

        assert journal_pragmas(bot_conn) == ["PRAGMA journal_mode=WAL"]
        assert journal_pragmas(viewer_conn) == ["PRAGMA journal_mode=WAL"]
        assert db._journal_mode_initialized == {"Bot", "Web_Viewer"}

        # ...and each section is still only initialized once.
        again = Mock()
        db._apply_sqlite_pragmas(again, for_web_viewer=True)
        assert journal_pragmas(again) == []

    def test_invalid_journal_mode_warns_once_per_section(self, mock_logger, tmp_path):
        bot = Mock()
        bot.logger = mock_logger
        bot.config = configparser.ConfigParser()
        bot.config["Bot"] = {"sqlite_journal_mode": "NOT_A_MODE"}
        manager = DBManager(bot, str(tmp_path / "bad-mode.db"))
        # Construction runs migrations, which already consumed the one warning
        # and initialized the mode for [Bot].
        manager._journal_mode_warned.clear()
        manager._journal_mode_initialized.clear()
        mock_logger.warning.reset_mock()

        applied = []
        conn = Mock()
        conn.execute.side_effect = lambda sql: applied.append(sql)
        for _ in range(3):
            manager._apply_sqlite_pragmas(conn)

        assert mock_logger.warning.call_count == 1
        assert "NOT_A_MODE" in str(mock_logger.warning.call_args)
        assert [s for s in applied if s.upper().startswith("PRAGMA JOURNAL_MODE=")] == [
            "PRAGMA journal_mode=WAL"
        ]

    def test_rollback_journal_mode_is_applied_to_every_connection(
        self, mock_logger, tmp_path
    ):
        """Only WAL persists in the file header; the rollback modes are per-connection.

        Caching a rollback mode after the first connection would leave every
        later connection silently running SQLite's default DELETE.
        """
        bot = Mock()
        bot.logger = mock_logger
        bot.config = configparser.ConfigParser()
        bot.config["Bot"] = {"sqlite_journal_mode": "TRUNCATE"}
        manager = DBManager(bot, str(tmp_path / "rollback-mode.db"))

        applied = []
        conn = Mock()
        conn.execute.side_effect = lambda sql: applied.append(sql)
        manager._apply_sqlite_pragmas(conn)
        manager._apply_sqlite_pragmas(conn)

        assert [s for s in applied if s.upper().startswith("PRAGMA JOURNAL_MODE=")] == [
            "PRAGMA journal_mode=TRUNCATE",
            "PRAGMA journal_mode=TRUNCATE",
        ]
        # A non-persistent mode must not consume the WAL-only fast path.
        assert "Bot" not in manager._journal_mode_initialized


class TestWriteTuning:
    """synchronous level and the WAL keeper connection (fewer SD-card writes)."""

    @staticmethod
    def _manager(mock_logger, tmp_path, **bot_keys):
        bot = Mock()
        bot.logger = mock_logger
        bot.config = configparser.ConfigParser()
        bot.config["Bot"] = bot_keys
        return DBManager(bot, str(tmp_path / "tuning.db"))

    @staticmethod
    def _synchronous(manager, for_web_viewer=False):
        with closing(sqlite3.connect(manager.db_path)) as conn:
            manager._apply_sqlite_pragmas(conn, for_web_viewer=for_web_viewer)
            return conn.execute("PRAGMA synchronous").fetchone()[0]

    def test_wal_connections_use_normal(self, mock_logger, tmp_path):
        manager = self._manager(mock_logger, tmp_path)
        assert manager.sqlite_synchronous() == "NORMAL"
        assert self._synchronous(manager) == 1  # NORMAL
        manager.close()

    def test_rollback_journal_keeps_full(self, mock_logger, tmp_path):
        manager = self._manager(mock_logger, tmp_path, sqlite_journal_mode="TRUNCATE")
        assert manager.sqlite_synchronous() == "FULL"
        assert self._synchronous(manager) == 2  # FULL

    def test_configured_level_wins(self, mock_logger, tmp_path):
        manager = self._manager(mock_logger, tmp_path, sqlite_synchronous="full")
        assert manager.sqlite_synchronous() == "FULL"
        assert self._synchronous(manager) == 2
        manager.close()

    def test_invalid_level_warns_once_and_uses_default(self, mock_logger, tmp_path):
        manager = self._manager(mock_logger, tmp_path, sqlite_synchronous="sometimes")
        mock_logger.warning.reset_mock()
        assert manager.sqlite_synchronous() == "NORMAL"
        assert manager.sqlite_synchronous() == "NORMAL"
        warnings = [c for c in mock_logger.warning.call_args_list if "sqlite_synchronous" in str(c)]
        assert len(warnings) <= 1
        manager.close()

    def test_keeper_keeps_wal_after_a_connection_closes(self, mock_logger, tmp_path):
        """Without the keeper, closing the only connection checkpoints and deletes the WAL."""
        manager = self._manager(mock_logger, tmp_path)
        manager.set_metadata("k", "v")
        wal = tmp_path / "tuning.db-wal"
        assert manager._wal_keeper is not None
        assert wal.exists()
        assert manager.get_metadata("k") == "v"

        manager.close()
        assert manager._wal_keeper is None
        # The keeper was the last connection, so its close checkpointed the WAL away.
        assert not wal.exists()
        with closing(sqlite3.connect(manager.db_path)) as conn:
            assert conn.execute(
                "SELECT value FROM bot_metadata WHERE key = 'k'"
            ).fetchone()[0] == "v"

    def test_close_is_idempotent_and_keeper_reopens(self, mock_logger, tmp_path):
        manager = self._manager(mock_logger, tmp_path)
        manager.close()
        manager.close()
        manager.set_metadata("k", "v")
        assert manager._wal_keeper is not None
        manager.close()

    def test_no_keeper_for_rollback_journal(self, mock_logger, tmp_path):
        manager = self._manager(mock_logger, tmp_path, sqlite_journal_mode="DELETE")
        manager.set_metadata("k", "v")
        assert manager._wal_keeper is None

    def test_keeper_does_not_block_writers_or_checkpoints(self, mock_logger, tmp_path):
        manager = self._manager(mock_logger, tmp_path)
        for i in range(50):
            manager.set_metadata(f"k{i}", "x" * 500)
        with manager.connection() as conn:
            busy, _log, _ckpt = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert busy == 0
        manager.close()


class TestAsyncSynchronous:
    def test_async_manager_applies_level(self, tmp_path, mock_logger):
        import asyncio

        pytest.importorskip("aiosqlite")
        from modules.db_manager import AsyncDBManager

        async def level(manager):
            async with manager.connection() as conn:
                async with conn.execute("PRAGMA synchronous") as cur:
                    return (await cur.fetchone())[0]

        path = str(tmp_path / "async.db")
        assert asyncio.run(level(AsyncDBManager(path, mock_logger, synchronous="NORMAL"))) == 1
        # An unknown level is ignored rather than interpolated into SQL.
        assert AsyncDBManager(path, mock_logger, synchronous="NORMAL; DROP").synchronous is None


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
