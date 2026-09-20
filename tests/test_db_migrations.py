"""Tests for modules.db_migrations — MigrationRunner and migration functions."""

import logging
import sqlite3

import pytest

from modules.db_migrations import (
    MIGRATIONS,
    MigrationRunner,
    _add_column,
    _column_exists,
    _m0017_feed_queue_item_uniqueness,
    _m0020_mesh_connections_last_seen_index,
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


class TestMeshConnectionIndexMigration:
    def test_reuses_legacy_equivalent_index(self, conn):
        conn.execute(
            "CREATE TABLE mesh_connections (from_prefix TEXT, to_prefix TEXT, last_seen TEXT)"
        )
        conn.execute("CREATE INDEX idx_last_seen ON mesh_connections(last_seen)")

        _m0020_mesh_connections_last_seen_index(conn.cursor())

        indexes = conn.execute('PRAGMA index_list("mesh_connections")').fetchall()
        last_seen_indexes = []
        for index in indexes:
            columns = conn.execute(f'PRAGMA index_info("{index[1]}")').fetchall()
            if [column[2] for column in columns] == ["last_seen"]:
                last_seen_indexes.append(index[1])
        assert last_seen_indexes == ["idx_last_seen"]


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
        for (version, description), (expected_v, expected_d, _) in zip(rows, MIGRATIONS, strict=False):
            assert version == expected_v
            assert description == expected_d

    def test_run_is_idempotent(self, runner, conn):
        runner.run()
        runner.run()  # second call should not re-apply
        cursor = conn.execute("SELECT COUNT(*) FROM schema_version")
        assert cursor.fetchone()[0] == len(MIGRATIONS)

    def test_non_contiguous_history_applies_missing_versions(self, conn, logger):
        """If schema_version has gaps, runner should apply missing versions."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL,
                description TEXT,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("INSERT INTO schema_version (version, description) VALUES (1, 'initial schema')")
        conn.execute("INSERT INTO schema_version (version, description) VALUES (3, 'feed_subscriptions: filter_config, sort_config')")

        # Seed minimal tables so migrations that ALTER them have something to work with.
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

        applied = {row[0] for row in conn.execute("SELECT version FROM schema_version").fetchall()}
        assert set(range(1, len(MIGRATIONS) + 1)).issubset(applied)

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

    def test_applied_versions_empty_on_empty_table(self, runner, conn):
        runner._ensure_version_table()
        assert runner._applied_versions() == set()


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

    def test_feed_queue_unique_index_deduplicates_valid_ids(self, conn):
        conn.execute(
            """
            CREATE TABLE feed_message_queue (
                id INTEGER PRIMARY KEY,
                feed_id INTEGER NOT NULL,
                item_id TEXT,
                sent_at TIMESTAMP
            )
            """
        )
        conn.executemany(
            "INSERT INTO feed_message_queue VALUES (?, ?, ?, ?)",
            [
                (1, 7, "same", None),
                (2, 7, "same", "2026-01-01"),
                (3, 7, "same", None),
                (4, 7, "", None),
                (5, 7, "", None),
                (6, 7, None, None),
                (7, 7, None, None),
            ],
        )

        _m0017_feed_queue_item_uniqueness(conn.cursor())

        rows = conn.execute(
            "SELECT id, item_id FROM feed_message_queue ORDER BY id"
        ).fetchall()
        assert rows == [(2, "same"), (4, ""), (5, ""), (6, None), (7, None)]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO feed_message_queue (feed_id, item_id) VALUES (7, 'same')"
            )
        conn.execute("INSERT INTO feed_message_queue (feed_id, item_id) VALUES (7, '')")

    def test_foreign_key_cascade_works_when_enabled(self, runner, conn):
        conn.execute("PRAGMA foreign_keys=ON")
        runner.run()

        # Create a subscription and activity, then delete subscription.
        sub_id = conn.execute(
            "INSERT INTO feed_subscriptions (feed_type, feed_url, channel_name) VALUES (?, ?, ?)",
            ("rss", "https://example.com/feed.xml", "chan"),
        ).lastrowid
        conn.execute(
            "INSERT INTO feed_activity (feed_id, item_id, item_title) VALUES (?, ?, ?)",
            (sub_id, "item-1", "title"),
        )
        conn.commit()

        conn.execute("DELETE FROM feed_subscriptions WHERE id = ?", (sub_id,))
        conn.commit()

        remaining = conn.execute("SELECT COUNT(*) FROM feed_activity WHERE feed_id = ?", (sub_id,)).fetchone()[0]
        assert remaining == 0

    def test_repeater_tables_created_by_migrations(self, runner, conn):
        runner.run()
        for table in [
            "repeater_contacts",
            "complete_contact_tracking",
            "daily_stats",
            "unique_advert_packets",
            "purging_log",
            "mesh_connections",
            "observed_paths",
        ]:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            assert cur.fetchone() is not None

    def test_repeater_indexes_created(self, runner, conn):
        runner.run()
        # Spot-check a few key indexes (not exhaustive).
        for idx in [
            "idx_public_key",
            "idx_complete_public_key",
            "idx_unique_advert_hash",
            "idx_from_prefix",
            "idx_observed_paths_endpoints",
        ]:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (idx,),
            )
            assert cur.fetchone() is not None

    def test_mesh_connections_has_table_specific_last_seen_index(self, runner, conn):
        runner.run()
        row = conn.execute(
            """
            SELECT tbl_name, sql
            FROM sqlite_master
            WHERE type = 'index' AND name = 'idx_mesh_connections_last_seen'
            """
        ).fetchone()
        assert row is not None
        assert row[0] == "mesh_connections"
        assert "last_seen" in row[1]

    def test_purging_log_has_details_column_after_full_migration(self, runner, conn):
        runner.run()
        cursor = conn.cursor()
        assert _column_exists(cursor, "purging_log", "details") is True

    def test_migration_adds_purging_log_details_for_legacy_schema(self, conn, logger):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL,
                description TEXT,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for version, description, _ in MIGRATIONS:
            if version <= 11:
                conn.execute(
                    "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                    (version, description),
                )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS purging_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                action TEXT NOT NULL,
                public_key TEXT NOT NULL,
                name TEXT NOT NULL,
                reason TEXT
            )
        """)
        conn.commit()

        runner = MigrationRunner(conn, logger)
        runner.run()

        cursor = conn.cursor()
        assert _column_exists(cursor, "purging_log", "details") is True


# ---------------------------------------------------------------------------
# TestNeighborTables (migration 22)
# ---------------------------------------------------------------------------


class TestNeighborTables:
    """Zero-hop neighbor discovery tables (see modules/neighbors_discovery.py)."""

    def test_neighbor_tables_created(self, runner, conn):
        runner.run()
        for table in ["neighbor_links", "neighbor_observations"]:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            assert cur.fetchone() is not None

    def test_neighbor_links_keeps_full_public_keys(self, runner, conn):
        """Full 32-byte keys, not prefixes — that is the point of this evidence."""
        runner.run()
        cursor = conn.cursor()
        for column in ["self_public_key", "neighbor_public_key", "best_snr",
                       "last_snr", "snr_sum", "snr_count", "observation_count",
                       "first_seen", "last_seen", "last_status", "scopes"]:
            assert _column_exists(cursor, "neighbor_links", column) is True

    def test_neighbor_observations_columns(self, runner, conn):
        runner.run()
        cursor = conn.cursor()
        for column in ["observed_at", "self_public_key", "neighbor_public_key",
                       "snr", "heard_secs_ago", "scopes", "status"]:
            assert _column_exists(cursor, "neighbor_observations", column) is True

    def test_neighbor_indexes_are_table_qualified(self, runner, conn):
        """SQLite index names are database-global (see migration 20)."""
        runner.run()
        for idx, table in [
            ("idx_neighbor_links_last_seen", "neighbor_links"),
            ("idx_neighbor_links_neighbor", "neighbor_links"),
            ("idx_neighbor_observations_observed_at", "neighbor_observations"),
            ("idx_neighbor_observations_neighbor", "neighbor_observations"),
        ]:
            row = conn.execute(
                "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
                (idx,),
            ).fetchone()
            assert row is not None, f"missing index {idx}"
            assert row[0] == table

    def test_neighbor_links_is_unique_per_directed_pair(self, runner, conn):
        runner.run()
        conn.execute(
            "INSERT INTO neighbor_links (self_public_key, neighbor_public_key) VALUES ('a', 'b')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO neighbor_links (self_public_key, neighbor_public_key) VALUES ('a', 'b')"
            )

    def test_migration_is_idempotent(self, conn, logger):
        MigrationRunner(conn, logger).run()
        MigrationRunner(conn, logger).run()
        applied = conn.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 22"
        ).fetchone()[0]
        assert applied == 1

    def test_neighbor_tables_are_writable_by_the_db_manager(self, runner, conn):
        """DBManager.ALLOWED_TABLES gates create/drop/retention helpers."""
        from modules.db_manager import DBManager

        runner.run()
        assert "neighbor_links" in DBManager.ALLOWED_TABLES
        assert "neighbor_observations" in DBManager.ALLOWED_TABLES


class TestObservedPathsZeroHopSignal:
    def test_snr_rssi_columns_added(self, runner, conn):
        runner.run()
        cursor = conn.cursor()
        assert _column_exists(cursor, "observed_paths", "snr") is True
        assert _column_exists(cursor, "observed_paths", "rssi") is True

    def test_migration_is_idempotent(self, conn, logger):
        MigrationRunner(conn, logger).run()
        MigrationRunner(conn, logger).run()
        applied = conn.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = 23"
        ).fetchone()[0]
        assert applied == 1
