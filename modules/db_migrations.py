#!/usr/bin/env python3
"""
Database migration versioning for MeshCore Bot.

Migrations are numbered functions applied exactly once and recorded in the
``schema_version`` table.  New installs run all migrations in order;
upgraded installs skip any already-applied version.

Adding a migration
------------------
1. Write a function ``_mNNNN_short_description(cursor)`` below.
2. Append it to ``MIGRATIONS`` as ``(NNNN, "short description", _mNNNN_...)``.

Never modify or remove an existing migration — add a new one instead.
"""

import logging
import sqlite3
from typing import Callable

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _column_exists(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
    """Return True if *column* already exists in *table*."""
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def _add_column(
    cursor: sqlite3.Cursor, table: str, column: str, definition: str
) -> None:
    """Add *column* to *table* if it does not already exist."""
    if not _column_exists(cursor, table, column):
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# ---------------------------------------------------------------------------
# Individual migrations
# ---------------------------------------------------------------------------


def _m0001_initial_schema(cursor: sqlite3.Cursor) -> None:
    """Create all base tables.  No-op for tables that already exist."""
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS geocoding_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT UNIQUE NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        );

        CREATE TABLE IF NOT EXISTS generic_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key TEXT UNIQUE NOT NULL,
            cache_value TEXT NOT NULL,
            cache_type TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bot_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS feed_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_type TEXT NOT NULL,
            feed_url TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            feed_name TEXT,
            last_item_id TEXT,
            last_check_time TIMESTAMP,
            check_interval_seconds INTEGER DEFAULT 300,
            enabled BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            api_config TEXT,
            rss_config TEXT,
            UNIQUE(feed_url, channel_name)
        );

        CREATE TABLE IF NOT EXISTS feed_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            item_title TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            message_sent BOOLEAN DEFAULT 1,
            FOREIGN KEY (feed_id) REFERENCES feed_subscriptions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS feed_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_id INTEGER NOT NULL,
            error_type TEXT NOT NULL,
            error_message TEXT,
            occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP,
            FOREIGN KEY (feed_id) REFERENCES feed_subscriptions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS channels (
            channel_idx INTEGER PRIMARY KEY,
            channel_name TEXT NOT NULL,
            channel_type TEXT,
            channel_key_hex TEXT,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(channel_idx)
        );

        CREATE TABLE IF NOT EXISTS channel_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_type TEXT NOT NULL,
            channel_idx INTEGER,
            channel_name TEXT,
            channel_key_hex TEXT,
            status TEXT DEFAULT 'pending',
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS feed_message_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_id INTEGER NOT NULL,
            channel_name TEXT NOT NULL,
            message TEXT NOT NULL,
            queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP,
            FOREIGN KEY (feed_id) REFERENCES feed_subscriptions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_geocoding_query     ON geocoding_cache(query);
        CREATE INDEX IF NOT EXISTS idx_geocoding_expires   ON geocoding_cache(expires_at);
        CREATE INDEX IF NOT EXISTS idx_generic_key         ON generic_cache(cache_key);
        CREATE INDEX IF NOT EXISTS idx_generic_type        ON generic_cache(cache_type);
        CREATE INDEX IF NOT EXISTS idx_generic_expires     ON generic_cache(expires_at);
        CREATE INDEX IF NOT EXISTS idx_feed_sub_enabled    ON feed_subscriptions(enabled);
        CREATE INDEX IF NOT EXISTS idx_feed_sub_type       ON feed_subscriptions(feed_type);
        CREATE INDEX IF NOT EXISTS idx_feed_sub_last_check ON feed_subscriptions(last_check_time);
        CREATE INDEX IF NOT EXISTS idx_feed_act_feed_id    ON feed_activity(feed_id);
        CREATE INDEX IF NOT EXISTS idx_feed_act_proc_at    ON feed_activity(processed_at);
        CREATE INDEX IF NOT EXISTS idx_feed_err_feed_id    ON feed_errors(feed_id);
        CREATE INDEX IF NOT EXISTS idx_feed_err_occur_at   ON feed_errors(occurred_at);
        CREATE INDEX IF NOT EXISTS idx_feed_err_resolved   ON feed_errors(resolved_at);
        CREATE INDEX IF NOT EXISTS idx_channels_name       ON channels(channel_name);
        CREATE INDEX IF NOT EXISTS idx_chan_ops_status      ON channel_operations(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_fmq_feed_id         ON feed_message_queue(feed_id);
        CREATE INDEX IF NOT EXISTS idx_fmq_sent_at         ON feed_message_queue(sent_at);
    """)


def _m0002_feed_subscriptions_output_format(cursor: sqlite3.Cursor) -> None:
    """Add output_format and message_send_interval_seconds to feed_subscriptions."""
    _add_column(cursor, "feed_subscriptions", "output_format", "TEXT")
    _add_column(
        cursor,
        "feed_subscriptions",
        "message_send_interval_seconds",
        "REAL DEFAULT 2.0",
    )


def _m0003_feed_subscriptions_filter_sort(cursor: sqlite3.Cursor) -> None:
    """Add filter_config and sort_config to feed_subscriptions."""
    _add_column(cursor, "feed_subscriptions", "filter_config", "TEXT")
    _add_column(cursor, "feed_subscriptions", "sort_config", "TEXT")


def _m0004_channel_operations_result_processed(cursor: sqlite3.Cursor) -> None:
    """Add result_data and processed_at to channel_operations."""
    _add_column(cursor, "channel_operations", "result_data", "TEXT")
    _add_column(cursor, "channel_operations", "processed_at", "TIMESTAMP")


def _m0005_feed_message_queue_item_fields(cursor: sqlite3.Cursor) -> None:
    """Add item_id, item_title, and priority to feed_message_queue."""
    _add_column(cursor, "feed_message_queue", "item_id", "TEXT")
    _add_column(cursor, "feed_message_queue", "item_title", "TEXT")
    _add_column(cursor, "feed_message_queue", "priority", "INTEGER DEFAULT 0")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_fmq_priority "
        "ON feed_message_queue(priority DESC, queued_at ASC)"
    )


def _m0006_channel_operations_payload_data(cursor: sqlite3.Cursor) -> None:
    """Add payload_data to channel_operations for firmware config read/write operations."""
    _add_column(cursor, "channel_operations", "payload_data", "TEXT")


# ---------------------------------------------------------------------------
# Migration registry — append new entries here, never remove or reorder.
# ---------------------------------------------------------------------------

MigrationEntry = tuple[int, str, Callable[[sqlite3.Cursor], None]]

MIGRATIONS: list[MigrationEntry] = [
    (1, "initial schema", _m0001_initial_schema),
    (2, "feed_subscriptions: output_format, message_send_interval_seconds", _m0002_feed_subscriptions_output_format),
    (3, "feed_subscriptions: filter_config, sort_config", _m0003_feed_subscriptions_filter_sort),
    (4, "channel_operations: result_data, processed_at", _m0004_channel_operations_result_processed),
    (5, "feed_message_queue: item_id, item_title, priority", _m0005_feed_message_queue_item_fields),
    (6, "channel_operations: payload_data", _m0006_channel_operations_payload_data),
]


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------


class MigrationRunner:
    """Apply pending numbered migrations to a SQLite connection.

    Usage::

        with db_manager.connection() as conn:
            runner = MigrationRunner(conn, logger)
            runner.run()
            conn.commit()
    """

    def __init__(self, conn: sqlite3.Connection, logger: logging.Logger) -> None:
        self.conn = conn
        self.logger = logger

    def _ensure_version_table(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version     INTEGER NOT NULL,
                description TEXT,
                applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    def _current_version(self) -> int:
        cursor = self.conn.execute("SELECT MAX(version) FROM schema_version")
        row = cursor.fetchone()
        return row[0] if row[0] is not None else 0

    def _apply(self, version: int, description: str, fn: Callable[[sqlite3.Cursor], None]) -> None:
        cursor = self.conn.cursor()
        fn(cursor)
        cursor.execute(
            "INSERT INTO schema_version (version, description) VALUES (?, ?)",
            (version, description),
        )
        self.logger.info(f"DB migration {version:04d} applied: {description}")

    def run(self) -> None:
        """Apply all pending migrations in version order."""
        self._ensure_version_table()
        current = self._current_version()
        pending = [(v, d, f) for v, d, f in MIGRATIONS if v > current]
        if not pending:
            self.logger.debug("Database schema is up to date")
            return
        for version, description, fn in pending:
            self._apply(version, description, fn)
        self.logger.info(
            f"Database migrations complete: {len(pending)} applied, "
            f"schema now at version {pending[-1][0]}"
        )
