#!/usr/bin/env python3
"""
Generalized Database Manager
Provides common database operations and table management for the MeshCore Bot
"""

import sqlite3
import json
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path


class DBManager:
    """Generalized database manager for common operations"""
    
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
        'purging_log',  # Repeater manager
    }
    
    def __init__(self, bot, db_path: str = "meshcore_bot.db"):
        self.bot = bot
        self.logger = bot.logger
        self.db_path = db_path
        self._init_database()
    
    def _init_database(self):
        """Initialize the SQLite database with required tables"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                cursor = conn.cursor()
                
                # Create geocoding_cache table for weather command optimization
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS geocoding_cache (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        query TEXT UNIQUE NOT NULL,
                        latitude REAL NOT NULL,
                        longitude REAL NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP NOT NULL
                    )
                ''')
                
                # Create generic cache table for other caching needs
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS generic_cache (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        cache_key TEXT UNIQUE NOT NULL,
                        cache_value TEXT NOT NULL,
                        cache_type TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP NOT NULL
                    )
                ''')
                
                # Create bot_metadata table for bot configuration and state
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS bot_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Create feed_subscriptions table for RSS/API feed subscriptions
                cursor.execute('''
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
                        output_format TEXT,
                        message_send_interval_seconds REAL DEFAULT 2.0,
                        UNIQUE(feed_url, channel_name)
                    )
                ''')
                
                # Add new columns if they don't exist (for existing databases)
                try:
                    cursor.execute('ALTER TABLE feed_subscriptions ADD COLUMN output_format TEXT')
                except sqlite3.OperationalError:
                    pass  # Column already exists
                try:
                    cursor.execute('ALTER TABLE feed_subscriptions ADD COLUMN message_send_interval_seconds REAL DEFAULT 2.0')
                except sqlite3.OperationalError:
                    pass  # Column already exists
                try:
                    cursor.execute('ALTER TABLE feed_subscriptions ADD COLUMN filter_config TEXT')
                except sqlite3.OperationalError:
                    pass  # Column already exists
                try:
                    cursor.execute('ALTER TABLE feed_subscriptions ADD COLUMN sort_config TEXT')
                except sqlite3.OperationalError:
                    pass  # Column already exists
                
                # Create feed_activity table for tracking processed items
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS feed_activity (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        feed_id INTEGER NOT NULL,
                        item_id TEXT NOT NULL,
                        item_title TEXT,
                        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        message_sent BOOLEAN DEFAULT 1,
                        FOREIGN KEY (feed_id) REFERENCES feed_subscriptions(id) ON DELETE CASCADE
                    )
                ''')
                
                # Create feed_errors table for tracking feed errors
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS feed_errors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        feed_id INTEGER NOT NULL,
                        error_type TEXT NOT NULL,
                        error_message TEXT,
                        occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        resolved_at TIMESTAMP,
                        FOREIGN KEY (feed_id) REFERENCES feed_subscriptions(id) ON DELETE CASCADE
                    )
                ''')
                
                # Create channels table for storing channel information
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS channels (
                        channel_idx INTEGER PRIMARY KEY,
                        channel_name TEXT NOT NULL,
                        channel_type TEXT,
                        channel_key_hex TEXT,
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(channel_idx)
                    )
                ''')
                
                # Create channel_operations queue table for web viewer -> bot communication
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS channel_operations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        operation_type TEXT NOT NULL,
                        channel_idx INTEGER,
                        channel_name TEXT,
                        channel_key_hex TEXT,
                        status TEXT DEFAULT 'pending',
                        error_message TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        processed_at TIMESTAMP,
                        result_data TEXT
                    )
                ''')
                
                # Create feed_message_queue table for queuing feed messages
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS feed_message_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        feed_id INTEGER NOT NULL,
                        channel_name TEXT NOT NULL,
                        message TEXT NOT NULL,
                        item_id TEXT,
                        item_title TEXT,
                        priority INTEGER DEFAULT 0,
                        queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        sent_at TIMESTAMP,
                        FOREIGN KEY (feed_id) REFERENCES feed_subscriptions(id) ON DELETE CASCADE
                    )
                ''')
                
                # Create indexes for better performance
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_geocoding_query ON geocoding_cache(query)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_geocoding_expires ON geocoding_cache(expires_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_generic_key ON generic_cache(cache_key)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_generic_type ON generic_cache(cache_type)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_generic_expires ON generic_cache(expires_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_feed_subscriptions_enabled ON feed_subscriptions(enabled)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_feed_subscriptions_type ON feed_subscriptions(feed_type)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_feed_subscriptions_last_check ON feed_subscriptions(last_check_time)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_feed_activity_feed_id ON feed_activity(feed_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_feed_activity_processed_at ON feed_activity(processed_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_feed_errors_feed_id ON feed_errors(feed_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_feed_errors_occurred_at ON feed_errors(occurred_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_feed_errors_resolved ON feed_errors(resolved_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_channels_name ON channels(channel_name)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_channel_ops_status ON channel_operations(status, created_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_feed_message_queue_feed_id ON feed_message_queue(feed_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_feed_message_queue_sent ON feed_message_queue(sent_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_feed_message_queue_priority ON feed_message_queue(priority DESC, queued_at ASC)')
                
                conn.commit()
                self.logger.info("Database manager initialized successfully")
                
        except Exception as e:
            self.logger.error(f"Failed to initialize database: {e}")
            raise
    
    # Geocoding cache methods
    def get_cached_geocoding(self, query: str) -> Tuple[Optional[float], Optional[float]]:
        """Get cached geocoding result for a query"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
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
    
    def cache_geocoding(self, query: str, latitude: float, longitude: float, cache_hours: int = 720):
        """Cache geocoding result for future use (default: 30 days)"""
        try:
            # Validate cache_hours to prevent SQL injection
            if not isinstance(cache_hours, int) or cache_hours < 1 or cache_hours > 87600:  # Max 10 years
                raise ValueError(f"cache_hours must be an integer between 1 and 87600, got: {cache_hours}")
            
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
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
        """Get cached value for a key and type"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
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
    
    def cache_value(self, cache_key: str, cache_value: str, cache_type: str, cache_hours: int = 24):
        """Cache a value for future use"""
        try:
            # Validate cache_hours to prevent SQL injection
            if not isinstance(cache_hours, int) or cache_hours < 1 or cache_hours > 87600:  # Max 10 years
                raise ValueError(f"cache_hours must be an integer between 1 and 87600, got: {cache_hours}")
            
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
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
    
    def get_cached_json(self, cache_key: str, cache_type: str) -> Optional[Dict]:
        """Get cached JSON value for a key and type"""
        cached_value = self.get_cached_value(cache_key, cache_type)
        if cached_value:
            try:
                return json.loads(cached_value)
            except json.JSONDecodeError:
                self.logger.warning(f"Failed to decode cached JSON for {cache_key}")
                return None
        return None
    
    def cache_json(self, cache_key: str, cache_value: Dict, cache_type: str, cache_hours: int = 720):
        """Cache a JSON value for future use (default: 30 days for geolocation)"""
        try:
            json_str = json.dumps(cache_value)
            self.cache_value(cache_key, json_str, cache_type, cache_hours)
        except Exception as e:
            self.logger.error(f"Error caching JSON value: {e}")
    
    # Cache cleanup methods
    def cleanup_expired_cache(self):
        """Remove expired cache entries from all cache tables"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
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
    
    def cleanup_geocoding_cache(self):
        """Remove expired geocoding cache entries"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM geocoding_cache WHERE expires_at < datetime('now')")
                deleted_count = cursor.rowcount
                if deleted_count > 0:
                    self.logger.info(f"Cleaned up {deleted_count} expired geocoding cache entries")
        except Exception as e:
            self.logger.error(f"Error cleaning up geocoding cache: {e}")
    
    # Database maintenance methods
    def get_database_stats(self) -> Dict[str, Any]:
        """Get database statistics"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
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
    
    def vacuum_database(self):
        """Optimize database by reclaiming unused space"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                conn.execute("VACUUM")
                self.logger.info("Database vacuum completed")
        except Exception as e:
            self.logger.error(f"Error vacuuming database: {e}")
    
    # Table management methods
    def create_table(self, table_name: str, schema: str):
        """Create a custom table with the given schema (whitelist-protected)"""
        try:
            # Validate table name against whitelist
            if table_name not in self.ALLOWED_TABLES:
                raise ValueError(f"Table name '{table_name}' not in allowed tables whitelist")
            
            # Additional validation: ensure table name follows safe naming convention
            if not re.match(r'^[a-z_][a-z0-9_]*$', table_name):
                raise ValueError(f"Invalid table name format: {table_name}")
            
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                cursor = conn.cursor()
                # Table names cannot be parameterized, but we've validated against whitelist
                cursor.execute(f'CREATE TABLE IF NOT EXISTS {table_name} ({schema})')
                conn.commit()
                self.logger.info(f"Created table: {table_name}")
        except Exception as e:
            self.logger.error(f"Error creating table {table_name}: {e}")
            raise
    
    def drop_table(self, table_name: str):
        """Drop a table (whitelist-protected, use with extreme caution)"""
        try:
            # Validate table name against whitelist
            if table_name not in self.ALLOWED_TABLES:
                raise ValueError(f"Table name '{table_name}' not in allowed tables whitelist")
            
            # Additional validation: ensure table name follows safe naming convention
            if not re.match(r'^[a-z_][a-z0-9_]*$', table_name):
                raise ValueError(f"Invalid table name format: {table_name}")
            
            # Extra safety: log critical action
            self.logger.warning(f"CRITICAL: Dropping table '{table_name}'")
            
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                cursor = conn.cursor()
                # Table names cannot be parameterized, but we've validated against whitelist
                cursor.execute(f'DROP TABLE IF EXISTS {table_name}')
                conn.commit()
                self.logger.info(f"Dropped table: {table_name}")
        except Exception as e:
            self.logger.error(f"Error dropping table {table_name}: {e}")
            raise
    
    def execute_query(self, query: str, params: Tuple = ()) -> List[Dict]:
        """Execute a custom query and return results as list of dictionaries"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(query, params)
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Error executing query: {e}")
            return []
    
    def execute_update(self, query: str, params: Tuple = ()) -> int:
        """Execute an update/insert/delete query and return number of affected rows"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            self.logger.error(f"Error executing update: {e}")
            return 0
    
    # Bot metadata methods
    def set_metadata(self, key: str, value: str):
        """Set a metadata value for the bot"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO bot_metadata (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                ''', (key, value))
                conn.commit()
        except Exception as e:
            self.logger.error(f"Error setting metadata {key}: {e}")
    
    def get_metadata(self, key: str) -> Optional[str]:
        """Get a metadata value for the bot"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
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
    
    def set_bot_start_time(self, start_time: float):
        """Set bot start time in metadata"""
        self.set_metadata('start_time', str(start_time))
