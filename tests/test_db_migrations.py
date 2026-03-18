"""Tests for modules.db_migrations — MigrationRunner and migration functions."""

import logging
import sqlite3

import pytest

from modules.db_migrations import (
    MIGRATIONS,
    MigrationRunner,
    _add_column,
    _column_exists,
)


@pytest.fixture
def conn():
    """In-memory SQLite connection for migration tests."""
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def logger():
    return logging.getLogger("test_migrations")


@pytest.fixture
def runner(conn, logger):
    return MigrationRunner(conn, logger)


# ---------------------------------------------------------------------------
# TestColumnHelpers
# ---------------------------------------------------------------------------


class TestColumnHelpers:
    def test_column_exists_returns_false_for_missing(self, conn):
        conn.execute("CREATE TABLE t (a TEXT)")
        cursor = conn.cursor()
        assert _column_exists(cursor, "t", "b") is False

    def test_column_exists_returns_true_for_existing(self, conn):
        conn.execute("CREATE TABLE t (a TEXT)")
        cursor = conn.cursor()
        assert _column_exists(cursor, "t", "a") is True

    def test_add_column_adds_new_column(self, conn):
        conn.execute("CREATE TABLE t (a TEXT)")
        cursor = conn.cursor()
        _add_column(cursor, "t", "b", "INTEGER DEFAULT 0")
        assert _column_exists(cursor, "t", "b") is True

    def test_add_column_is_idempotent(self, conn):
        conn.execute("CREATE TABLE t (a TEXT)")
        cursor = conn.cursor()
        _add_column(cursor, "t", "a", "TEXT")  # already exists — must not raise
        assert _column_exists(cursor, "t", "a") is True


# ---------------------------------------------------------------------------
# TestMigrationRunner
# ---------------------------------------------------------------------------


class TestMigrationRunner:
    def test_schema_version_table_created_on_run(self, runner, conn):
        runner.run()
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'")
        assert cursor.fetchone() is not None

    def test_all_migrations_applied_on_fresh_db(self, runner, conn):
        runner.run()
        cursor = conn.execute("SELECT MAX(version) FROM schema_version")
        assert cursor.fetchone()[0] == len(MIGRATIONS)

    def test_migrations_recorded_with_descriptions(self, runner, conn):
        runner.run()
        rows = conn.execute("SELECT version, description FROM schema_version ORDER BY version").fetchall()
        assert len(rows) == len(MIGRATIONS)
        for (version, description), (expected_v, expected_d, _) in zip(rows, MIGRATIONS):
            assert version == expected_v
            assert description == expected_d

    def test_run_is_idempotent(self, runner, conn):
        runner.run()
        runner.run()  # second call should not re-apply
        cursor = conn.execute("SELECT COUNT(*) FROM schema_version")
        assert cursor.fetchone()[0] == len(MIGRATIONS)

    def test_partial_history_applies_only_pending(self, conn, logger):
        """Simulate a DB that already had migration 1 applied."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL,
                description TEXT,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("INSERT INTO schema_version (version, description) VALUES (1, 'initial schema')")
        conn.commit()

        # Migration 1 creates feed_subscriptions (among others); inject it manually
        # so later migrations can find the table.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feed_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_url TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                UNIQUE(feed_url, channel_name)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_type TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feed_message_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id INTEGER NOT NULL,
                channel_name TEXT NOT NULL,
                message TEXT NOT NULL,
                queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP
            )
        """)
        conn.commit()

        runner = MigrationRunner(conn, logger)
        runner.run()

        # All migrations after 1 should now be applied
        cursor = conn.execute("SELECT MAX(version) FROM schema_version")
        assert cursor.fetchone()[0] == len(MIGRATIONS)

        # Columns from later migrations should exist
        cursor2 = conn.cursor()
        assert _column_exists(cursor2, "feed_subscriptions", "output_format") is True
        assert _column_exists(cursor2, "feed_message_queue", "priority") is True

    def test_current_version_zero_on_empty_table(self, runner, conn):
        runner._ensure_version_table()
        assert runner._current_version() == 0


# ---------------------------------------------------------------------------
# TestSchema — spot-check key tables and columns after full migration
# ---------------------------------------------------------------------------


class TestSchema:
    def test_geocoding_cache_table_exists(self, runner, conn):
        runner.run()
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='geocoding_cache'")
        assert cursor.fetchone() is not None

    def test_feed_subscriptions_has_output_format(self, runner, conn):
        runner.run()
        cursor = conn.cursor()
        assert _column_exists(cursor, "feed_subscriptions", "output_format") is True

    def test_feed_subscriptions_has_filter_config(self, runner, conn):
        runner.run()
        cursor = conn.cursor()
        assert _column_exists(cursor, "feed_subscriptions", "filter_config") is True

    def test_channel_operations_has_result_data(self, runner, conn):
        runner.run()
        cursor = conn.cursor()
        assert _column_exists(cursor, "channel_operations", "result_data") is True

    def test_channel_operations_has_processed_at(self, runner, conn):
        runner.run()
        cursor = conn.cursor()
        assert _column_exists(cursor, "channel_operations", "processed_at") is True

    def test_feed_message_queue_has_priority(self, runner, conn):
        runner.run()
        cursor = conn.cursor()
        assert _column_exists(cursor, "feed_message_queue", "priority") is True

    def test_feed_message_queue_has_item_id(self, runner, conn):
        runner.run()
        cursor = conn.cursor()
        assert _column_exists(cursor, "feed_message_queue", "item_id") is True
