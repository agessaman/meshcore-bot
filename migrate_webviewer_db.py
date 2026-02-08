#!/usr/bin/env python3
"""
One-off migration: copy packet_stream from a separate web viewer database
(e.g. bot_data.db) into the main bot database (e.g. meshcore_bot.db).

Use this when switching from split databases to the shared database so you
don't lose packet stream history. Run with bot and web viewer stopped.

Usage:
  python3 migrate_webviewer_db.py <source_db> <target_db>

Example:
  python3 migrate_webviewer_db.py bot_data.db meshcore_bot.db
"""

import sqlite3
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python3 migrate_webviewer_db.py <source_db> <target_db>", file=sys.stderr)
        print("Example: python3 migrate_webviewer_db.py bot_data.db meshcore_bot.db", file=sys.stderr)
        return 1

    source_path = sys.argv[1]
    target_path = sys.argv[2]

    if source_path == target_path:
        print("Source and target must be different files.", file=sys.stderr)
        return 1

    try:
        conn = sqlite3.connect(target_path, timeout=30.0)
        conn.execute("ATTACH DATABASE ? AS src", (source_path,))

        # Ensure packet_stream exists in target (same schema as web viewer)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS packet_stream (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                data TEXT NOT NULL,
                type TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_packet_stream_timestamp ON packet_stream(timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_packet_stream_type ON packet_stream(type)"
        )

        # Check that source has packet_stream
        cur = conn.execute(
            "SELECT name FROM src.sqlite_master WHERE type='table' AND name='packet_stream'"
        )
        if cur.fetchone() is None:
            conn.execute("DETACH DATABASE src")
            conn.close()
            print(f"Source database has no packet_stream table; nothing to migrate.", file=sys.stderr)
            return 0

        # Copy rows from source; skip ids that already exist in target (INSERT OR IGNORE)
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO main.packet_stream (id, timestamp, data, type)
            SELECT id, timestamp, data, type FROM src.packet_stream
            """
        )
        inserted = conn.total_changes - before
        conn.commit()

        conn.execute("DETACH DATABASE src")
        conn.close()

        print(f"Migrated {inserted} packet_stream row(s) from {source_path} to {target_path}.")
        return 0

    except sqlite3.OperationalError as e:
        print(f"Database error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"File error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
