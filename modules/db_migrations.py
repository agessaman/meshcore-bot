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


def _m0013_observed_paths_advert_covering_index(cursor: sqlite3.Cursor) -> None:
    """Covering index for the contacts-page recent-advert-paths window query.

    Lets the ROW_NUMBER() OVER (PARTITION BY public_key ORDER BY last_seen DESC)
    scan in the contacts API run as an ordered index scan over advert rows,
    avoiding a full materialization + temp B-tree sort of observed_paths.
    """
    if not _table_exists(cursor, "observed_paths"):
        return
    cursor.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_observed_paths_advert_pk_seen
            ON observed_paths(public_key, last_seen DESC, path_hex, path_length,
                              bytes_per_hop, observation_count)
            WHERE packet_type = 'advert';
        """
    )


def _m0014_observed_paths_multibyte_covering_index(cursor: sqlite3.Cursor) -> None:
    """Partial covering index for the mesh-graph multibyte evidence query.

    /api/mesh/edges?evidence=multibyte reads every multi-byte path row
    (bytes_per_hop >= 2, roughly a fifth of observed_paths); without an index
    that is a full scan of the widest table in the database (~2s at 700k
    rows). This partial index covers the query's columns, so it never touches
    the table, and last_seen in slot 2 keeps the optional days filter indexed.
    """
    if not _table_exists(cursor, "observed_paths"):
        return
    cursor.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_observed_paths_multibyte
            ON observed_paths(bytes_per_hop, last_seen, path_hex,
                              observation_count, first_seen)
            WHERE bytes_per_hop >= 2;
        """
    )


def _m0015_channel_operations_claimed_at(cursor: sqlite3.Cursor) -> None:
    """Record when a queued hardware/config operation is durably claimed.

    A ``processing`` row is deliberately not auto-requeued: after a process
    crash the device may already have applied the operation, so retrying it
    automatically could execute a non-idempotent command twice.  ``claimed_at``
    gives operators enough information to diagnose and explicitly resolve such
    an ambiguous operation.
    """
    if _table_exists(cursor, "channel_operations"):
        _add_column(cursor, "channel_operations", "claimed_at", "TIMESTAMP")


def _m0016_channel_operations_claim_owner(cursor: sqlite3.Cursor) -> None:
    """Persist enough local-process identity to recover only provably dead claims."""
    if not _table_exists(cursor, "channel_operations"):
        return
    _add_column(cursor, "channel_operations", "claim_owner_host", "TEXT")
    _add_column(cursor, "channel_operations", "claim_owner_pid", "INTEGER")
    _add_column(cursor, "channel_operations", "claim_owner_boot_id", "TEXT")


def _m0017_feed_queue_item_uniqueness(cursor: sqlite3.Cursor) -> None:
    """Deduplicate identifiable queue items and prevent future duplicates.

    Blank and NULL item IDs deliberately remain unconstrained: without a stable
    provider identifier they cannot safely be treated as the same feed item.
    For duplicate valid IDs, retain a sent row when one exists (otherwise the
    oldest queued row) so migration does not resurrect already-delivered work.
    """
    if not _table_exists(cursor, "feed_message_queue"):
        return
    cursor.execute(
        """
        DELETE FROM feed_message_queue
        WHERE item_id IS NOT NULL AND trim(item_id) <> ''
          AND id NOT IN (
              SELECT COALESCE(
                         MIN(CASE WHEN sent_at IS NOT NULL THEN id END),
                         MIN(id)
                     )
              FROM feed_message_queue
              WHERE item_id IS NOT NULL AND trim(item_id) <> ''
              GROUP BY feed_id, item_id
          )
        """
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fmq_feed_item_unique
        ON feed_message_queue(feed_id, item_id)
        WHERE item_id IS NOT NULL AND trim(item_id) <> ''
        """
    )


def _m0018_dashboard_rollup_tables(cursor: sqlite3.Cursor) -> None:
    """Durable dashboard storage: per-day rollups plus a current-state snapshot.

    ``daily_rollup`` holds one row per local date for the metrics whose raw
    sources are pruned long before the dashboard's 30-day window (message_stats
    and friends at 7 days, packet_stream at 3).  Signal metrics are stored as
    sums and counts rather than means so an arbitrary window re-aggregates
    correctly — averaging daily averages weights a quiet day the same as a busy
    one.  Every numeric column is nullable on purpose: NULL means "no source
    data for this day", which the UI must render as a gap, while 0 means "the
    source was present and the count really was zero".

    ``dashboard_snapshot`` is a single row of JSON.  The current-state half of
    the dashboard is ~40 heterogeneous scalars and small arrays that are read
    together and never queried by field, so columnizing it would cost a
    migration per new tile for no query benefit.
    """
    cursor.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_rollup (
            date                     TEXT PRIMARY KEY,
            messages_total INTEGER, messages_dm INTEGER, messages_channel INTEGER,
            unique_senders INTEGER, unique_channels INTEGER,
            commands_total INTEGER, commands_replied INTEGER, unique_command_users INTEGER,
            path_obs_total INTEGER, path_len_max INTEGER, path_len_sum INTEGER,
            snr_sum REAL, snr_count INTEGER, rssi_sum REAL, rssi_count INTEGER,
            hops_sum INTEGER, hops_count INTEGER,
            packets_total INTEGER, packets_flood INTEGER, packets_direct INTEGER,
            packets_multibyte INTEGER,
            adverts_total INTEGER, nodes_active INTEGER, nodes_new INTEGER,
            adverts_from_multibyte INTEGER, adverts_from_singlebyte INTEGER,
            contacts_known INTEGER, contacts_tracked INTEGER,
            sources_present INTEGER NOT NULL DEFAULT 0,
            is_backfilled   INTEGER NOT NULL DEFAULT 0,
            is_final        INTEGER NOT NULL DEFAULT 0,
            computed_at     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_daily_rollup_computed ON daily_rollup(computed_at);

        CREATE TABLE IF NOT EXISTS dashboard_snapshot (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            generated_at REAL NOT NULL,
            duration_ms  INTEGER,
            schema_rev   INTEGER NOT NULL DEFAULT 1,
            payload      TEXT NOT NULL
        );
        """
    )


def _m0019_packet_stream_denorm_dims(cursor: sqlite3.Cursor) -> None:
    """Denormalize the four packet dimensions the dashboard aggregates on.

    Counting packets by bytes_per_hop or route type via
    ``json_extract(data, '$.…')`` is a full scan that parses every row's JSON:
    ~3s for bytes_per_hop and ~6s for route_type_name on a 178k-row table.
    The writer already holds these values as parsed fields, so storing them as
    columns removes the parse entirely.

    Deliberately no bulk UPDATE here.  packet_stream rows average ~1KB, so
    widening 178k of them rewrites ~180MB into the WAL inside MigrationRunner's
    transaction — a multi-minute stall blocking bot startup on an SD-card Pi.
    The dashboard refresher backfills a bounded batch per tick instead, and the
    table's 3-day retention makes the gap self-healing regardless.
    """
    if not _table_exists(cursor, "packet_stream"):
        return
    _add_column(cursor, "packet_stream", "route_type_name", "TEXT")
    _add_column(cursor, "packet_stream", "payload_type_name", "TEXT")
    _add_column(cursor, "packet_stream", "path_len", "INTEGER")
    _add_column(cursor, "packet_stream", "bytes_per_hop", "INTEGER")
    cursor.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_packet_stream_dims
            ON packet_stream(timestamp, bytes_per_hop, route_type_name, payload_type_name)
            WHERE type = 'packet';

        -- Worklist for the refresher's incremental backfill.  Without it, the
        -- "any rows left to dimension?" probe is a full table scan that costs
        -- ~4.6s on a 180MB packet_stream — and it costs that on every tick
        -- *after* the backlog is empty, because finding nothing still means
        -- reading everything.  The index shrinks toward empty as rows are
        -- dimensioned, so the probe converges to a no-op.  It must carry
        -- ``type`` as well: non-packet rows keep a NULL route type forever and
        -- would otherwise leave the index permanently non-empty.
        CREATE INDEX IF NOT EXISTS idx_packet_stream_undimensioned
            ON packet_stream(type, id)
            WHERE route_type_name IS NULL;
        """
    )


def _m0020_mesh_connections_last_seen_index(cursor: sqlite3.Cursor) -> None:
    """Add the table-specific time index used by graph windows and retention.

    Older migrations attempted to create ``idx_last_seen`` for several tables.
    SQLite index names are database-global, so the first table claimed that
    name and ``mesh_connections`` was left without its intended index.
    """
    if not _table_exists(cursor, "mesh_connections"):
        return

    # Some older databases already have the generic ``idx_last_seen`` attached
    # to this table.  Keep that usable index instead of building an identical
    # second B-tree (which costs both storage and write amplification).
    cursor.execute('PRAGMA index_list("mesh_connections")')
    for index_row in cursor.fetchall():
        index_name = index_row[1]
        is_partial = bool(index_row[4]) if len(index_row) > 4 else False
        cursor.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
            (index_name,),
        )
        indexed_columns = [row[0] for row in cursor.fetchall()]
        if not is_partial and indexed_columns == ["last_seen"]:
            return

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mesh_connections_last_seen
            ON mesh_connections(last_seen)
        """
    )


def _m0021_daily_rollup_packet_type_encoding(cursor: sqlite3.Cursor) -> None:
    """Record the per-payload-type multibyte split on each rollup day.

    ``packet_stream`` is pruned at three days, so the dashboard's 30-day
    encoding trend cannot be recomputed from it after the fact — the split has
    to be written as each day is rolled up, the same accumulate-forward shape
    the advert share already uses.

    One JSON column rather than a count pair per type: the payload-type
    vocabulary belongs to the firmware, not to us, so a type appearing on the
    mesh should not need a schema migration to be charted.
    """
    if not _table_exists(cursor, "daily_rollup"):
        return
    _add_column(cursor, "daily_rollup", "packet_type_encoding", "TEXT")


def _m0022_neighbor_tables(cursor: sqlite3.Cursor) -> None:
    """Tables for zero-hop neighbor discovery (see modules/neighbors_discovery.py).

    A discover response is the strongest link evidence this codebase has: a
    confirmed direct RF reception between two *full* 32-byte public keys, with a
    measured SNR. Every other edge source is weaker — ``observed_paths`` carries
    1–3 byte prefixes and no keys, and ``complete_contact_tracking.hop_count``
    over-claims zero-hop. So these keep the full keys rather than prefixes, and
    stay separate from ``mesh_connections``, which cannot represent provenance
    (its ``confirmed_2byte`` flag is memory-only and never persisted).

    Two tables because they answer different questions: ``neighbor_links`` is the
    current adjacency the mesh graph reads, ``neighbor_observations`` is the
    per-cycle history a signal trend needs. SNR is stored as sum+count rather
    than a mean, matching ``daily_rollup``, so any window re-aggregates exactly.

    Index names are database-global in SQLite (see the note on migration 20), so
    every name here is table-qualified.
    """
    cursor.executescript(
        """
        CREATE TABLE IF NOT EXISTS neighbor_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            self_public_key TEXT NOT NULL,
            neighbor_public_key TEXT NOT NULL,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            observation_count INTEGER DEFAULT 1,
            snr_sum REAL DEFAULT 0,
            snr_count INTEGER DEFAULT 0,
            best_snr REAL,
            last_snr REAL,
            last_status TEXT,
            scopes TEXT,
            UNIQUE(self_public_key, neighbor_public_key)
        );

        CREATE TABLE IF NOT EXISTS neighbor_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TIMESTAMP NOT NULL,
            self_public_key TEXT NOT NULL,
            neighbor_public_key TEXT NOT NULL,
            snr REAL,
            heard_secs_ago INTEGER,
            scopes TEXT,
            status TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_neighbor_links_last_seen
            ON neighbor_links(last_seen);
        CREATE INDEX IF NOT EXISTS idx_neighbor_links_neighbor
            ON neighbor_links(neighbor_public_key);
        CREATE INDEX IF NOT EXISTS idx_neighbor_observations_observed_at
            ON neighbor_observations(observed_at);
        CREATE INDEX IF NOT EXISTS idx_neighbor_observations_neighbor
            ON neighbor_observations(neighbor_public_key, observed_at);
        """
    )


def _m0023_observed_paths_zero_hop_signal(cursor: sqlite3.Cursor) -> None:
    """SNR/RSSI on observed_paths for direct (zero-hop) advert rows.

    Empty-path adverts are a confirmed last-hop-is-originator measurement, so
    the figure belongs on the path row rather than on complete_contact_tracking
    (whose hop_count over-claims zero-hop). Multi-hop rows leave these NULL.
    """
    if not _table_exists(cursor, "observed_paths"):
        return
    _add_column(cursor, "observed_paths", "snr", "REAL")
    _add_column(cursor, "observed_paths", "rssi", "REAL")

def _m0024_region_scope_tables(cursor: sqlite3.Cursor) -> None:
    """Storage for regional flood-scope observation and the warnings it drives.

    ``region_scope_daily`` is a per-day, per-channel tally of how each channel
    message's flood scope was classified.  It is written for every channel
    message the bot hears, independent of whether warnings are enabled, because
    the whole point is letting an operator see how much unscoped traffic there
    actually is *before* deciding to spend airtime telling anyone about it.
    One row per channel per local date keeps that free.

    ``region_warning_events`` holds one row per warning decision that reached
    the send stage — actually sent, suppressed by a failed send, or withheld
    because the feature is in dry-run.  Suppressions by cooldown or daily cap
    are deliberately *not* rows: they are the common case and would bury the
    log.  The cooldown and cap windows are derived from this table rather than
    from memory so they survive a restart, which is why dry-run rows are
    written too — a dry run has to consume the same budget it is previewing.
    """
    cursor.executescript(
        """
        CREATE TABLE IF NOT EXISTS region_scope_daily (
            date          TEXT NOT NULL,
            channel       TEXT NOT NULL,
            scoped_count  INTEGER NOT NULL DEFAULT 0,
            global_count  INTEGER NOT NULL DEFAULT 0,
            unknown_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date, channel)
        );

        CREATE TABLE IF NOT EXISTS region_warning_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TIMESTAMP NOT NULL,
            sender_id     TEXT NOT NULL,
            sender_pubkey TEXT,
            channel       TEXT,
            delivery      TEXT NOT NULL,
            action        TEXT NOT NULL,
            detail        TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_region_warning_events_created_at
            ON region_warning_events(created_at);
        CREATE INDEX IF NOT EXISTS idx_region_warning_events_sender
            ON region_warning_events(sender_id, created_at);
        """
    )


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
    (13, "observed_paths: advert covering index for contacts page", _m0013_observed_paths_advert_covering_index),
    (14, "observed_paths: multibyte covering index for mesh graph", _m0014_observed_paths_multibyte_covering_index),
    (15, "channel_operations: claimed_at", _m0015_channel_operations_claimed_at),
    (16, "channel_operations: claim owner identity", _m0016_channel_operations_claim_owner),
    (17, "feed_message_queue: unique identifiable items", _m0017_feed_queue_item_uniqueness),
    (18, "dashboard rollup and snapshot tables", _m0018_dashboard_rollup_tables),
    (19, "packet_stream: denormalized packet dimensions", _m0019_packet_stream_denorm_dims),
    (20, "mesh_connections: table-specific last_seen index", _m0020_mesh_connections_last_seen_index),
    (21, "daily_rollup: per-payload-type multibyte split", _m0021_daily_rollup_packet_type_encoding),
    (22, "neighbor discovery tables", _m0022_neighbor_tables),
    (23, "observed_paths: snr/rssi for zero-hop adverts", _m0023_observed_paths_zero_hop_signal),
    (24, "regional flood scope tallies and warning events", _m0024_region_scope_tables),
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
