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
import re
import sqlite3
from typing import Callable

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

VALID_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Allowed column definition pattern: type keyword(s) optionally followed by
# DEFAULT and a literal value.  This prevents SQL injection through the
# definition parameter of _add_column().
_VALID_COL_DEF = re.compile(
    r"^[A-Z]+(?:\s+[A-Z]+)*"                      # type name, e.g. "TEXT", "INTEGER", "BOOLEAN"
    r"(?:\s+DEFAULT\s+(?:'[^']*'|[0-9.]+|NULL|CURRENT_TIMESTAMP))?"  # optional DEFAULT clause
    r"$",
    re.IGNORECASE,
)


def _validate_ident(name: str, kind: str) -> None:
    if not VALID_IDENT.match(name):
        raise ValueError(f"Invalid {kind} identifier: {name!r}")


def _table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    _validate_ident(table, "table")
    cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cursor.fetchone() is not None


def _column_exists(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
    """Return True if *column* already exists in *table*."""
    _validate_ident(table, "table")
    _validate_ident(column, "column")
    cursor.execute(f'PRAGMA table_info("{table}")')
    return any(row[1] == column for row in cursor.fetchall())


def _validate_col_definition(definition: str) -> None:
    """Ensure *definition* matches a safe SQLite column definition pattern."""
    if not _VALID_COL_DEF.match(definition.strip()):
        raise ValueError(f"Invalid column definition: {definition!r}")


def _add_column(
    cursor: sqlite3.Cursor, table: str, column: str, definition: str
) -> None:
    """Add *column* to *table* if it does not already exist."""
    _validate_ident(table, "table")
    _validate_ident(column, "column")
    _validate_col_definition(definition)
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


# NOTE: Higher-numbered migrations can safely depend on tables created by other
# subsystems (e.g., repeater manager) by checking for table existence and then
# applying idempotent ALTER/CREATE INDEX statements.


def _m0007_packet_stream_table(cursor: sqlite3.Cursor) -> None:
    """Create packet_stream table and indexes (shared DB with web viewer)."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS packet_stream (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            data TEXT NOT NULL,
            type TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_packet_stream_timestamp ON packet_stream(timestamp)"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_packet_stream_type ON packet_stream(type)")


def _m0008_repeater_tables_optional_columns(cursor: sqlite3.Cursor) -> None:
    """Bring repeater/graph tables up to date if they exist."""
    # repeater_contacts: add location columns only if table exists
    if _table_exists(cursor, "repeater_contacts"):
        for column_name, column_def in [
            ("latitude", "REAL"),
            ("longitude", "REAL"),
            ("city", "TEXT"),
            ("state", "TEXT"),
            ("country", "TEXT"),
        ]:
            _add_column(cursor, "repeater_contacts", column_name, column_def)

    # complete_contact_tracking: path columns + is_starred and out_bytes_per_hop
    if _table_exists(cursor, "complete_contact_tracking"):
        for column_name, column_def in [
            ("out_path", "TEXT"),
            ("out_path_len", "INTEGER"),
            ("snr", "REAL"),
            ("is_starred", "BOOLEAN DEFAULT 0"),
            ("out_bytes_per_hop", "INTEGER"),
        ]:
            _add_column(cursor, "complete_contact_tracking", column_name, column_def)

    # observed_paths: packet_hash + bytes_per_hop
    if _table_exists(cursor, "observed_paths"):
        for column_name, column_def in [
            ("packet_hash", "TEXT"),
            ("bytes_per_hop", "INTEGER"),
        ]:
            _add_column(cursor, "observed_paths", column_name, column_def)

    # mesh_connections: graph/viewer columns
    if _table_exists(cursor, "mesh_connections"):
        for column_name, column_def in [
            ("from_public_key", "TEXT"),
            ("to_public_key", "TEXT"),
            ("avg_hop_position", "REAL"),
            ("geographic_distance", "REAL"),
        ]:
            _add_column(cursor, "mesh_connections", column_name, column_def)


def _m0009_repeater_optional_indexes(cursor: sqlite3.Cursor) -> None:
    """Create optional indexes for repeater/graph tables if they exist."""
    if _table_exists(cursor, "unique_advert_packets"):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_unique_advert_date_pubkey ON unique_advert_packets(date, public_key)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_unique_advert_hash ON unique_advert_packets(packet_hash)"
        )

    if _table_exists(cursor, "mesh_connections"):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_from_prefix ON mesh_connections(from_prefix)"
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_to_prefix ON mesh_connections(to_prefix)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_last_seen ON mesh_connections(last_seen)")

    if _table_exists(cursor, "observed_paths"):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_observed_paths_public_key ON observed_paths(public_key, packet_type)"
        )
        if _column_exists(cursor, "observed_paths", "packet_hash"):
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_observed_paths_packet_hash ON observed_paths(packet_hash) WHERE packet_hash IS NOT NULL"
            )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_observed_paths_advert_unique ON observed_paths(public_key, path_hex, packet_type) WHERE public_key IS NOT NULL"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_observed_paths_endpoints ON observed_paths(from_prefix, to_prefix, packet_type)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_observed_paths_message_unique ON observed_paths(from_prefix, to_prefix, path_hex, packet_type) WHERE public_key IS NULL"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_observed_paths_last_seen ON observed_paths(last_seen)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_observed_paths_type_seen ON observed_paths(packet_type, last_seen)"
        )


def _m0010_create_repeater_and_graph_tables(cursor: sqlite3.Cursor) -> None:
    """Create repeater/graph tables used by the web viewer and repeater manager."""
    cursor.executescript(
        """
        CREATE TABLE IF NOT EXISTS repeater_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            device_type TEXT NOT NULL,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            contact_data TEXT,
            latitude REAL,
            longitude REAL,
            city TEXT,
            state TEXT,
            country TEXT,
            is_active BOOLEAN DEFAULT 1,
            purge_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS complete_contact_tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_key TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            device_type TEXT,
            first_heard TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_heard TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            advert_count INTEGER DEFAULT 1,
            latitude REAL,
            longitude REAL,
            city TEXT,
            state TEXT,
            country TEXT,
            raw_advert_data TEXT,
            signal_strength REAL,
            snr REAL,
            hop_count INTEGER,
            is_currently_tracked BOOLEAN DEFAULT 0,
            last_advert_timestamp TIMESTAMP,
            location_accuracy REAL,
            contact_source TEXT DEFAULT 'advertisement',
            out_path TEXT,
            out_path_len INTEGER,
            out_bytes_per_hop INTEGER,
            is_starred INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL,
            public_key TEXT NOT NULL,
            advert_count INTEGER DEFAULT 1,
            first_advert_time TIMESTAMP,
            last_advert_time TIMESTAMP,
            UNIQUE(date, public_key)
        );

        CREATE TABLE IF NOT EXISTS unique_advert_packets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL,
            public_key TEXT NOT NULL,
            packet_hash TEXT NOT NULL,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, public_key, packet_hash)
        );

        CREATE TABLE IF NOT EXISTS purging_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            action TEXT NOT NULL,
            public_key TEXT NOT NULL,
            name TEXT NOT NULL,
            reason TEXT
        );

        CREATE TABLE IF NOT EXISTS mesh_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_prefix TEXT NOT NULL,
            to_prefix TEXT NOT NULL,
            from_public_key TEXT,
            to_public_key TEXT,
            observation_count INTEGER DEFAULT 1,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            avg_hop_position REAL,
            geographic_distance REAL,
            UNIQUE(from_prefix, to_prefix)
        );

        CREATE TABLE IF NOT EXISTS observed_paths (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_key TEXT,
            packet_hash TEXT,
            from_prefix TEXT NOT NULL,
            to_prefix TEXT NOT NULL,
            path_hex TEXT NOT NULL,
            path_length INTEGER NOT NULL,
            bytes_per_hop INTEGER,
            packet_type TEXT NOT NULL,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            observation_count INTEGER DEFAULT 1
        );
        """
    )


def _m0011_repeater_and_graph_indexes(cursor: sqlite3.Cursor) -> None:
    """Create indexes for repeater/graph tables (safe to run repeatedly)."""
    cursor.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_public_key ON repeater_contacts(public_key);
        CREATE INDEX IF NOT EXISTS idx_device_type ON repeater_contacts(device_type);
        CREATE INDEX IF NOT EXISTS idx_last_seen ON repeater_contacts(last_seen);
        CREATE INDEX IF NOT EXISTS idx_is_active ON repeater_contacts(is_active);

        CREATE INDEX IF NOT EXISTS idx_complete_public_key ON complete_contact_tracking(public_key);
        CREATE INDEX IF NOT EXISTS idx_complete_role ON complete_contact_tracking(role);
        CREATE INDEX IF NOT EXISTS idx_complete_last_heard ON complete_contact_tracking(last_heard);
        CREATE INDEX IF NOT EXISTS idx_complete_currently_tracked ON complete_contact_tracking(is_currently_tracked);
        CREATE INDEX IF NOT EXISTS idx_complete_location ON complete_contact_tracking(latitude, longitude);
        CREATE INDEX IF NOT EXISTS idx_complete_role_tracked ON complete_contact_tracking(role, is_currently_tracked);

        CREATE INDEX IF NOT EXISTS idx_unique_advert_date_pubkey ON unique_advert_packets(date, public_key);
        CREATE INDEX IF NOT EXISTS idx_unique_advert_hash ON unique_advert_packets(packet_hash);

        CREATE INDEX IF NOT EXISTS idx_from_prefix ON mesh_connections(from_prefix);
        CREATE INDEX IF NOT EXISTS idx_to_prefix ON mesh_connections(to_prefix);
        CREATE INDEX IF NOT EXISTS idx_last_seen ON mesh_connections(last_seen);

        CREATE INDEX IF NOT EXISTS idx_observed_paths_public_key ON observed_paths(public_key, packet_type);
        CREATE INDEX IF NOT EXISTS idx_observed_paths_packet_hash ON observed_paths(packet_hash) WHERE packet_hash IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_observed_paths_advert_unique ON observed_paths(public_key, path_hex, packet_type) WHERE public_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_observed_paths_endpoints ON observed_paths(from_prefix, to_prefix, packet_type);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_observed_paths_message_unique ON observed_paths(from_prefix, to_prefix, path_hex, packet_type) WHERE public_key IS NULL;
        CREATE INDEX IF NOT EXISTS idx_observed_paths_last_seen ON observed_paths(last_seen);
        CREATE INDEX IF NOT EXISTS idx_observed_paths_type_seen ON observed_paths(packet_type, last_seen);
        """
    )


def _m0012_purging_log_details_column(cursor: sqlite3.Cursor) -> None:
    """Add details column for newer purging log entries."""
    if _table_exists(cursor, "purging_log"):
        _add_column(cursor, "purging_log", "details", "TEXT")


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
    (7, "packet_stream table for web viewer", _m0007_packet_stream_table),
    (8, "optional repeater/graph columns", _m0008_repeater_tables_optional_columns),
    (9, "optional repeater/graph indexes", _m0009_repeater_optional_indexes),
    (10, "create repeater/graph tables", _m0010_create_repeater_and_graph_tables),
    (11, "repeater/graph indexes", _m0011_repeater_and_graph_indexes),
    (12, "purging_log: add details column", _m0012_purging_log_details_column),
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
        # Legacy DBs may have been created without a uniqueness constraint.
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_schema_version_version ON schema_version(version)"
        )

    def _applied_versions(self) -> set[int]:
        cursor = self.conn.execute("SELECT version FROM schema_version")
        return {int(row[0]) for row in cursor.fetchall() if row and row[0] is not None}

    def _validate_versions(self, applied: set[int]) -> None:
        known = {v for v, _, _ in MIGRATIONS}
        unknown = sorted(v for v in applied if v not in known)
        if unknown:
            raise RuntimeError(
                "Database schema is newer or inconsistent with this codebase. "
                f"Unknown applied migration version(s): {unknown}. "
                "Upgrade the bot to a newer version that includes these migrations."
            )

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
        applied = self._applied_versions()
        self._validate_versions(applied)
        pending = [(v, d, f) for v, d, f in MIGRATIONS if v not in applied]
        pending.sort(key=lambda x: x[0])
        if not pending:
            self.logger.debug("Database schema is up to date")
            return

        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for version, description, fn in pending:
                self._apply(version, description, fn)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        self.logger.info(
            f"Database migrations complete: {len(pending)} applied, "
            f"schema now at version {pending[-1][0]}"
        )
