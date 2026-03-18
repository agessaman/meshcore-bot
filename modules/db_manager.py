#!/usr/bin/env python3
"""
Generalized Database Manager
Provides common database operations and table management for the MeshCore Bot
"""

import json
import re
import sqlite3
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Optional

from .db_migrations import MigrationRunner


class DBManager:
    """Generalized database manager for common operations.

    Handles database initialization, schema management, caching, and metadata storage.
    Enforces a table whitelist for security.
    """

    # Whitelist of allowed tables for security
    ALLOWED_TABLES = {
        'geocoding_cache',
        'generic_cache',
        'bot_metadata',
        'packet_stream',
        'message_stats',
        'greeted_users',
        'repeater_contacts',
        'complete_contact_tracking',  # Repeater manager
        'daily_stats',  # Repeater manager
        'unique_advert_packets',  # Repeater manager - unique packet tracking
        'purging_log',  # Repeater manager
        'mesh_connections',  # Mesh graph for path validation
        'observed_paths',  # Repeater manager - observed paths from adverts and messages
    }

    def __init__(self, bot: Any, db_path: str = "meshcore_bot.db"):
        self.bot = bot
        self.logger = bot.logger
        self.db_path = db_path
        self._init_database()

    def _init_database(self) -> None:
        """Initialize the database by running all pending numbered migrations.

        On a fresh install this creates all tables and indexes (migration 0001).
        On an existing installation it applies only the migrations that have not
        yet been recorded in the ``schema_version`` table.
        """
        try:
            with self.connection() as conn:
                runner = MigrationRunner(conn, self.logger)
                runner.run()
                conn.commit()
                self.logger.info("Database manager initialized successfully")

        except Exception as e:
            self.logger.error(f"Failed to initialize database: {e}")
            raise

    # Geocoding cache methods
    def get_cached_geocoding(self, query: str) -> tuple[Optional[float], Optional[float]]:
        """Get cached geocoding result for a query.

        Args:
            query: The geocoding query string.

        Returns:
            Tuple[Optional[float], Optional[float]]: A tuple containing (latitude, longitude)
            if found and valid, otherwise (None, None).
        """
        try:
            with self.connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT latitude, longitude FROM geocoding_cache
                    WHERE query = ? AND expires_at > datetime('now')
                ''', (query,))
                result = cursor.fetchone()
                if result:
                    return result[0], result[1]
                return None, None
        except Exception as e:
            self.logger.error(f"Error getting cached geocoding: {e}")
            return None, None

    def cache_geocoding(self, query: str, latitude: float, longitude: float, cache_hours: int = 720) -> None:
        """Cache geocoding result for future use.

        Args:
            query: The geocoding query string.
            latitude: Latitude coordinate.
            longitude: Longitude coordinate.
            cache_hours: Expiration time in hours (default: 720 hours / 30 days).
        """
        try:
            # Validate cache_hours to prevent SQL injection
            if not isinstance(cache_hours, int) or cache_hours < 1 or cache_hours > 87600:  # Max 10 years
                raise ValueError(f"cache_hours must be an integer between 1 and 87600, got: {cache_hours}")

            with self.connection() as conn:
                cursor = conn.cursor()
                # Use parameter binding instead of string formatting
                cursor.execute('''
                    INSERT OR REPLACE INTO geocoding_cache
                    (query, latitude, longitude, expires_at)
                    VALUES (?, ?, ?, datetime('now', '+' || ? || ' hours'))
                ''', (query, latitude, longitude, cache_hours))
                conn.commit()
        except Exception as e:
            self.logger.error(f"Error caching geocoding: {e}")

    # Generic cache methods
    def get_cached_value(self, cache_key: str, cache_type: str) -> Optional[str]:
        """Get cached value for a key and type.

        Args:
            cache_key: Unique key for the cached item.
            cache_type: Category or type identifier for the cache.

        Returns:
            Optional[str]: Cached string value if found and valid, None otherwise.
        """
        try:
            with self.connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT cache_value FROM generic_cache
                    WHERE cache_key = ? AND cache_type = ? AND expires_at > datetime('now')
                ''', (cache_key, cache_type))
                result = cursor.fetchone()
                if result:
                    return result[0]
                return None
        except Exception as e:
            self.logger.error(f"Error getting cached value: {e}")
            return None

    def cache_value(self, cache_key: str, cache_value: str, cache_type: str, cache_hours: int = 24) -> None:
        """Cache a value for future use.

        Args:
            cache_key: Unique key for the cached item.
            cache_value: String value to cache.
            cache_type: Category or type identifier.
            cache_hours: Expiration time in hours (default: 24 hours).
        """
        try:
            # Validate cache_hours to prevent SQL injection
            if not isinstance(cache_hours, int) or cache_hours < 1 or cache_hours > 87600:  # Max 10 years
                raise ValueError(f"cache_hours must be an integer between 1 and 87600, got: {cache_hours}")

            with self.connection() as conn:
                cursor = conn.cursor()
                # Use parameter binding instead of string formatting
                cursor.execute('''
                    INSERT OR REPLACE INTO generic_cache
                    (cache_key, cache_value, cache_type, expires_at)
                    VALUES (?, ?, ?, datetime('now', '+' || ? || ' hours'))
                ''', (cache_key, cache_value, cache_type, cache_hours))
                conn.commit()
        except Exception as e:
            self.logger.error(f"Error caching value: {e}")

    def get_cached_json(self, cache_key: str, cache_type: str) -> Optional[dict]:
        """Get cached JSON value for a key and type.

        Args:
            cache_key: Unique key for the cached item.
            cache_type: Category or type identifier.

        Returns:
            Optional[Dict]: Parsed JSON dictionary if found and valid, None otherwise.
        """
        cached_value = self.get_cached_value(cache_key, cache_type)
        if cached_value:
            try:
                return json.loads(cached_value)
            except json.JSONDecodeError:
                self.logger.warning(f"Failed to decode cached JSON for {cache_key}")
                return None
        return None

    def cache_json(self, cache_key: str, cache_value: dict, cache_type: str, cache_hours: int = 720) -> None:
        """Cache a JSON value for future use.

        Args:
            cache_key: Unique key for the cached item.
            cache_value: Dictionary to serialize and cache.
            cache_type: Category or type identifier.
            cache_hours: Expiration time in hours (default: 720 hours / 30 days).
        """
        try:
            json_str = json.dumps(cache_value)
            self.cache_value(cache_key, json_str, cache_type, cache_hours)
        except Exception as e:
            self.logger.error(f"Error caching JSON value: {e}")

    # Cache cleanup methods
    def cleanup_expired_cache(self) -> None:
        """Remove expired cache entries from all cache tables.

        Deletes rows from geocoding_cache and generic_cache where the
        expiration timestamp has passed.
        """
        try:
            with self.connection() as conn:
                cursor = conn.cursor()

                # Clean up geocoding cache
                cursor.execute("DELETE FROM geocoding_cache WHERE expires_at < datetime('now')")
                geocoding_deleted = cursor.rowcount

                # Clean up generic cache
                cursor.execute("DELETE FROM generic_cache WHERE expires_at < datetime('now')")
                generic_deleted = cursor.rowcount

                conn.commit()

                total_deleted = geocoding_deleted + generic_deleted
                if total_deleted > 0:
                    self.logger.info(f"Cleaned up {total_deleted} expired cache entries ({geocoding_deleted} geocoding, {generic_deleted} generic)")

        except Exception as e:
            self.logger.error(f"Error cleaning up expired cache: {e}")

    def cleanup_geocoding_cache(self) -> None:
        """Remove expired geocoding cache entries"""
        try:
            with self.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM geocoding_cache WHERE expires_at < datetime('now')")
                deleted_count = cursor.rowcount
                conn.commit()
                if deleted_count > 0:
                    self.logger.info(f"Cleaned up {deleted_count} expired geocoding cache entries")
        except Exception as e:
            self.logger.error(f"Error cleaning up geocoding cache: {e}")

    # Database maintenance methods
    def get_database_stats(self) -> dict[str, Any]:
        """Get database statistics"""
        try:
            with self.connection() as conn:
                cursor = conn.cursor()

                stats = {}

                # Geocoding cache stats
                cursor.execute('SELECT COUNT(*) FROM geocoding_cache')
                stats['geocoding_cache_entries'] = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM geocoding_cache WHERE expires_at > datetime('now')")
                stats['geocoding_cache_active'] = cursor.fetchone()[0]

                # Generic cache stats
                cursor.execute('SELECT COUNT(*) FROM generic_cache')
                stats['generic_cache_entries'] = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM generic_cache WHERE expires_at > datetime('now')")
                stats['generic_cache_active'] = cursor.fetchone()[0]

                # Cache type breakdown
                cursor.execute('''
                    SELECT cache_type, COUNT(*) FROM generic_cache
                    WHERE expires_at > datetime('now')
                    GROUP BY cache_type
                ''')
                stats['cache_types'] = dict(cursor.fetchall())

                return stats

        except Exception as e:
            self.logger.error(f"Error getting database stats: {e}")
            return {}

    def vacuum_database(self) -> None:
        """Optimize database by reclaiming unused space.

        Executes the VACUUM command to rebuild the database file and reduce size.
        """
        try:
            with self.connection() as conn:
                conn.execute("VACUUM")
                self.logger.info("Database vacuum completed")
        except Exception as e:
            self.logger.error(f"Error vacuuming database: {e}")

    # Table management methods
    def create_table(self, table_name: str, schema: str) -> None:
        """Create a custom table with the given schema.

        Args:
            table_name: Name of the table to create (must be whitelist-protected).
            schema: SQL schema definition for the table columns.

        Raises:
            ValueError: If table_name is not in the allowed whitelist.
        """
        try:
            # Validate table name against whitelist
            if table_name not in self.ALLOWED_TABLES:
                raise ValueError(f"Table name '{table_name}' not in allowed tables whitelist")

            # Additional validation: ensure table name follows safe naming convention
            if not re.match(r'^[a-z_][a-z0-9_]*$', table_name):
                raise ValueError(f"Invalid table name format: {table_name}")

            with self.connection() as conn:
                cursor = conn.cursor()
                # Table names cannot be parameterized, but we've validated against whitelist
                cursor.execute(f'CREATE TABLE IF NOT EXISTS {table_name} ({schema})')
                conn.commit()
                self.logger.info(f"Created table: {table_name}")
        except Exception as e:
            self.logger.error(f"Error creating table {table_name}: {e}")
            raise

    def drop_table(self, table_name: str) -> None:
        """Drop a table.

        Args:
            table_name: Name of the table to drop (must be whitelist-protected).

        Raises:
            ValueError: If table_name is not in the allowed whitelist.
        """
        try:
            # Validate table name against whitelist
            if table_name not in self.ALLOWED_TABLES:
                raise ValueError(f"Table name '{table_name}' not in allowed tables whitelist")

            # Additional validation: ensure table name follows safe naming convention
            if not re.match(r'^[a-z_][a-z0-9_]*$', table_name):
                raise ValueError(f"Invalid table name format: {table_name}")

            # Extra safety: log critical action
            self.logger.warning(f"CRITICAL: Dropping table '{table_name}'")

            with self.connection() as conn:
                cursor = conn.cursor()
                # Table names cannot be parameterized, but we've validated against whitelist
                cursor.execute(f'DROP TABLE IF EXISTS {table_name}')
                conn.commit()
                self.logger.info(f"Dropped table: {table_name}")
        except Exception as e:
            self.logger.error(f"Error dropping table {table_name}: {e}")
            raise

    def execute_query(self, query: str, params: tuple = ()) -> list[dict]:
        """Execute a custom query and return results as list of dictionaries"""
        try:
            with self.connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(query, params)
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Error executing query: {e}")
            return []

    def execute_update(self, query: str, params: tuple = ()) -> int:
        """Execute an update/insert/delete query and return number of affected rows"""
        try:
            with self.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            self.logger.error(f"Error executing update: {e}")
            return 0

    def execute_query_on_connection(self, conn: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict]:
        """Execute a query on an existing connection. Caller owns the connection."""
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        if conn.row_factory is sqlite3.Row:
            return [dict(row) for row in rows]
        desc = cursor.description
        if not desc:
            return []
        return [dict(zip([c[0] for c in desc], row)) for row in rows]

    def execute_update_on_connection(self, conn: sqlite3.Connection, query: str, params: tuple = ()) -> int:
        """Execute an update/insert/delete on an existing connection. Caller must commit."""
        cursor = conn.cursor()
        cursor.execute(query, params)
        return cursor.rowcount

    # Bot metadata methods
    def set_metadata(self, key: str, value: str) -> None:
        """Set a metadata value for the bot.

        Args:
            key: Metadata key name.
            value: Metadata string value.
        """
        try:
            with self.connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO bot_metadata (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                ''', (key, value))
                conn.commit()
        except Exception as e:
            self.logger.error(f"Error setting metadata {key}: {e}")

    def get_metadata(self, key: str) -> Optional[str]:
        """Get a metadata value for the bot.

        Args:
            key: Metadata key to retrieve.

        Returns:
            Optional[str]: Value string if found, None otherwise.
        """
        try:
            with self.connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT value FROM bot_metadata WHERE key = ?', (key,))
                result = cursor.fetchone()
                if result:
                    return result[0]
                return None
        except Exception as e:
            self.logger.error(f"Error getting metadata {key}: {e}")
            return None

    def get_bot_start_time(self) -> Optional[float]:
        """Get bot start time from metadata"""
        start_time_str = self.get_metadata('start_time')
        if start_time_str:
            try:
                return float(start_time_str)
            except ValueError:
                self.logger.warning(f"Invalid start_time in metadata: {start_time_str}")
                return None
        return None

    def set_bot_start_time(self, start_time: float) -> None:
        """Set bot start time in metadata"""
        self.set_metadata('start_time', str(start_time))

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that yields a configured connection and closes it on exit.
        Use this instead of get_connection() in with-statements to avoid leaking file descriptors.
        """
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def get_connection(self) -> sqlite3.Connection:
        """Get a database connection with proper configuration.
        Caller must close the connection (e.g. conn.close() in finally).
        Prefer connection() when using a with-statement so the connection is closed automatically.

        Returns:
            sqlite3.Connection with row factory and timeout configured
        """
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def set_system_health(self, health_data: dict[str, Any]) -> None:
        """Store system health data in metadata"""
        try:
            import json
            health_json = json.dumps(health_data)
            self.set_metadata('system_health', health_json)
        except Exception as e:
            self.logger.error(f"Error storing system health: {e}")

    def get_system_health(self) -> Optional[dict[str, Any]]:
        """Get system health data from metadata"""
        try:
            import json
            health_json = self.get_metadata('system_health')
            if health_json:
                return json.loads(health_json)
            return None
        except Exception as e:
            self.logger.error(f"Error getting system health: {e}")
            return None


class AsyncDBManager:
    """Async database manager using aiosqlite for non-blocking DB access.

    Provides the same interface as ``DBManager`` for the most common operations
    but uses ``aiosqlite`` so async callers do not block the event loop.

    Usage in async code::

        async with self.bot.async_db_manager.connection() as conn:
            await conn.execute('SELECT ...')

        value = await self.bot.async_db_manager.get_metadata('key')
    """

    def __init__(self, db_path: str, logger: Any) -> None:
        self.db_path = db_path
        self.logger = logger

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[Any, None]:
        """Async context manager yielding an aiosqlite connection."""
        try:
            import aiosqlite
        except ImportError:
            raise RuntimeError("aiosqlite is required for AsyncDBManager. Run: pip install aiosqlite")
        async with aiosqlite.connect(self.db_path, timeout=30.0) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    async def get_metadata(self, key: str) -> Optional[str]:
        """Async version of DBManager.get_metadata."""
        try:
            async with self.connection() as conn:
                async with conn.execute(
                    'SELECT value FROM bot_metadata WHERE key = ?', (key,)
                ) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else None
        except Exception as e:
            self.logger.error(f"AsyncDBManager: error getting metadata {key}: {e}")
            return None

    async def set_metadata(self, key: str, value: str) -> None:
        """Async version of DBManager.set_metadata."""
        try:
            async with self.connection() as conn:
                await conn.execute(
                    '''INSERT OR REPLACE INTO bot_metadata (key, value, updated_at)
                       VALUES (?, ?, CURRENT_TIMESTAMP)''',
                    (key, value),
                )
                await conn.commit()
        except Exception as e:
            self.logger.error(f"AsyncDBManager: error setting metadata {key}: {e}")

    async def execute_query(self, query: str, params: tuple = ()) -> list[dict]:
        """Execute a SELECT query and return results as list of dicts."""
        try:
            async with self.connection() as conn:
                async with conn.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error(f"AsyncDBManager: error executing query: {e}")
            return []

    async def execute_update(self, query: str, params: tuple = ()) -> int:
        """Execute an INSERT/UPDATE/DELETE query and return affected row count."""
        try:
            async with self.connection() as conn:
                async with conn.execute(query, params) as cursor:
                    await conn.commit()
                    return cursor.rowcount
        except Exception as e:
            self.logger.error(f"AsyncDBManager: error executing update: {e}")
            return 0

    async def get_cached_value(self, cache_key: str, cache_type: str) -> Optional[str]:
        """Async version of DBManager.get_cached_value."""
        try:
            async with self.connection() as conn:
                async with conn.execute(
                    '''SELECT cache_value FROM generic_cache
                       WHERE cache_key = ? AND cache_type = ? AND expires_at > datetime('now')''',
                    (cache_key, cache_type),
                ) as cursor:
                    row = await cursor.fetchone()
                    return row[0] if row else None
        except Exception as e:
            self.logger.error(f"AsyncDBManager: error getting cached value: {e}")
            return None

    async def cache_value(
        self, cache_key: str, cache_value: str, cache_type: str, cache_hours: int = 24
    ) -> None:
        """Async version of DBManager.cache_value."""
        try:
            if not isinstance(cache_hours, int) or cache_hours < 1 or cache_hours > 87600:
                raise ValueError(f"cache_hours must be 1–87600, got: {cache_hours}")
            async with self.connection() as conn:
                await conn.execute(
                    '''INSERT OR REPLACE INTO generic_cache
                       (cache_key, cache_value, cache_type, expires_at)
                       VALUES (?, ?, ?, datetime('now', '+' || ? || ' hours'))''',
                    (cache_key, cache_value, cache_type, cache_hours),
                )
                await conn.commit()
        except Exception as e:
            self.logger.error(f"AsyncDBManager: error caching value: {e}")
