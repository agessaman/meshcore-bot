#!/usr/bin/env python3
"""
Greeter command for the MeshCore Bot
Greets users on their first public channel message with mesh information
"""

import sqlite3
import time
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from .base_command import BaseCommand
from ..models import MeshMessage


class GreeterCommand(BaseCommand):
    """Handles greeting new users on public channels"""
    
    # Plugin metadata
    name = "greeter"
    keywords = []  # No keywords - this command is triggered automatically
    description = "Greets users on their first public channel message (once globally by default, or per-channel if configured)"
    category = "system"
    
    def __init__(self, bot: Any):
        """Initialize the greeter command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self._init_greeter_tables()
        self._load_config()
        
        # Track pending greetings (for dead air delay)
        self.pending_greetings = {}  # key: (sender_id, channel), value: asyncio.Task
        
        # Auto-backfill if enabled
        if self.enabled and self.auto_backfill:
            self.logger.info("Auto-backfill enabled - backfilling greeted users from historical data")
            result = self.backfill_greeted_users(lookback_days=self.backfill_lookback_days)
            if result['success']:
                self.logger.info(f"Auto-backfill completed: {result['marked_count']} users marked")
            else:
                self.logger.warning(f"Auto-backfill failed: {result.get('error', 'Unknown error')}")
        
        # Check for existing rollout and mark active users if needed
        self._check_rollout_period()
        
        # Auto-start rollout if enabled, rollout_days > 0, and no active rollout exists
        if self.enabled and self.rollout_days > 0:
            try:
                with self.bot.db_manager.get_connection() as conn:
                    cursor = conn.cursor()
                    # Check for active rollout (more robust check)
                    cursor.execute('''
                        SELECT id, rollout_started_at, rollout_days, rollout_completed,
                               datetime(rollout_started_at, '+' || rollout_days || ' days') as end_date,
                               datetime('now') as current_time
                        FROM greeter_rollout
                        WHERE rollout_completed = 0
                        ORDER BY rollout_started_at DESC
                        LIMIT 1
                    ''')
                    active_rollout = cursor.fetchone()
                    
                    if active_rollout:
                        # Verify the rollout is actually still active (not expired)
                        rollout_id, started_at_str, rollout_days, completed, end_date_str, current_time_str = active_rollout
                        end_date = datetime.fromisoformat(end_date_str)
                        current_time = datetime.fromisoformat(current_time_str)
                        
                        if current_time < end_date:
                            # Rollout is still active - don't start a new one
                            remaining = (end_date - current_time).total_seconds() / 86400
                            self.logger.info(f"Active rollout found (ID: {rollout_id}, {remaining:.1f} days remaining) - not starting new rollout")
                        else:
                            # Rollout expired but not marked as completed - mark it and start new one
                            self.logger.warning(f"Found expired rollout (ID: {rollout_id}) - marking as completed and starting new one")
                            cursor.execute('''
                                UPDATE greeter_rollout
                                SET rollout_completed = 1
                                WHERE id = ?
                            ''', (rollout_id,))
                            conn.commit()
                            self.logger.info(f"Auto-starting greeter rollout for {self.rollout_days} days")
                            self.start_rollout(backfill_first=self.auto_backfill)
                    else:
                        # No active rollout - check if one was recently completed to prevent immediate restart
                        cursor.execute('''
                            SELECT id, rollout_started_at, rollout_days
                            FROM greeter_rollout
                            WHERE rollout_completed = 1
                            ORDER BY rollout_started_at DESC
                            LIMIT 1
                        ''')
                        recent_rollout = cursor.fetchone()
                        
                        if recent_rollout:
                            recent_id, recent_started_at_str, recent_rollout_days = recent_rollout
                            recent_started_at = datetime.fromisoformat(recent_started_at_str)
                            # Calculate when this rollout would have ended
                            recent_end_date = recent_started_at + timedelta(days=recent_rollout_days)
                            cursor.execute("SELECT datetime('now')")
                            current_time = datetime.fromisoformat(cursor.fetchone()[0])
                            
                            # If rollout ended less than 1 day ago, don't auto-start a new one
                            # (prevents restart loops if there's a bug)
                            if current_time < recent_end_date + timedelta(days=1):
                                days_since_end = (current_time - recent_end_date).total_seconds() / 86400
                                self.logger.info(f"Recent rollout completed {days_since_end:.1f} days ago (ID: {recent_id}) - skipping auto-start to prevent restart loop")
                                return
                        
                        # No active rollout and no recent completed rollout - start one automatically
                        self.logger.info(f"Auto-starting greeter rollout for {self.rollout_days} days")
                        self.start_rollout(backfill_first=self.auto_backfill)
            except Exception as e:
                self.logger.error(f"Error checking for existing rollout: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
    
    def _decode_escape_sequences(self, text: str) -> str:
        """Decode escape sequences in config strings.
        
        Processes common escape sequences like \\n (newline), \\t (tab), \\\\ (backslash).
        This allows users to add newlines in greeting messages using \n (single backslash).
        
        Behavior:
        - \n in config file → newline character
        - \\n in config file → literal backslash + n
        
        Args:
            text: The text string to process.
            
        Returns:
            str: The text with escape sequences decoded.
        """
        # Replace escape sequences
        # Order matters: \\ must be processed first to avoid double-processing
        # This preserves literal backslashes (\\n becomes \n, not a newline)
        text = text.replace('\\\\', '\x00')  # Temporary placeholder for backslash
        text = text.replace('\\n', '\n')     # Newline
        text = text.replace('\\t', '\t')     # Tab
        text = text.replace('\\r', '\r')     # Carriage return
        text = text.replace('\x00', '\\')    # Restore backslash
        return text
    
    def _load_config(self) -> None:
        """Load configuration for greeter command."""
        self.enabled = self.get_config_value('Greeter_Command', 'enabled', fallback=False, value_type='bool')
        self.greeting_message = self.get_config_value('Greeter_Command', 'greeting_message', 
                                                      fallback='Welcome to the mesh, {sender}!')
        # Decode escape sequences (e.g., \\n for newlines)
        self.greeting_message = self._decode_escape_sequences(self.greeting_message)
        
        self.rollout_days = self.get_config_value('Greeter_Command', 'rollout_days', fallback=7, value_type='int')
        self.include_mesh_info = self.get_config_value('Greeter_Command', 'include_mesh_info', 
                                                       fallback=True, value_type='bool')
        self.mesh_info_format = self.get_config_value('Greeter_Command', 'mesh_info_format',
                                                      fallback='\n\nMesh Info: {total_contacts} contacts, {repeaters} repeaters')
        # Decode escape sequences (e.g., \n for newlines)
        self.mesh_info_format = self._decode_escape_sequences(self.mesh_info_format)
        
        # Log configuration for debugging
        self.logger.debug(f"Greeter config loaded: include_mesh_info={self.include_mesh_info}, "
                         f"mesh_info_format={repr(self.mesh_info_format)}")
        
        self.per_channel_greetings = self.get_config_value('Greeter_Command', 'per_channel_greetings',
                                                           fallback=False, value_type='bool')
        self.auto_backfill = self.get_config_value('Greeter_Command', 'auto_backfill', 
                                                   fallback=False, value_type='bool')
        self.backfill_lookback_days = self.get_config_value('Greeter_Command', 'backfill_lookback_days',
                                                            fallback=None, value_type='int')
        # Convert 0 to None (all time)
        if self.backfill_lookback_days == 0:
            self.backfill_lookback_days = None
        
        # Note: allowed_channels is now loaded by BaseCommand from config
        # Keep greeter_channels for backward compatibility and case-insensitive matching
        channels_str = self.get_config_value('Greeter_Command', 'channels', fallback='')
        if channels_str:
            # Store both original and lowercase versions for case-insensitive matching
            self.greeter_channels = [ch.strip() for ch in channels_str.split(',') if ch.strip()]
            self.greeter_channels_lower = [ch.lower() for ch in self.greeter_channels]
        else:
            # Fall back to monitor_channels if not specified
            self.greeter_channels = None
            self.greeter_channels_lower = None
        
        # Load channel-specific greeting messages
        # Format: channel_name:greeting_message,channel_name2:greeting_message2
        # Example: Public:Welcome to Public, {sender}!|general:Welcome to general, {sender}!
        channel_greetings_str = self.get_config_value('Greeter_Command', 'channel_greetings', fallback='')
        self.channel_greetings = {}
        if channel_greetings_str:
            for entry in channel_greetings_str.split(','):
                entry = entry.strip()
                if ':' in entry:
                    channel_name, greeting = entry.split(':', 1)
                    channel_name = channel_name.strip()
                    greeting = greeting.strip()
                    # Decode escape sequences (e.g., \\n for newlines)
                    greeting = self._decode_escape_sequences(greeting)
                    # Store both original and lowercase channel name for case-insensitive matching
                    self.channel_greetings[channel_name.lower()] = {
                        'channel': channel_name,
                        'greeting': greeting
                    }
        
        # Parse multi-part greetings (pipe-separated)
        # If greeting_message contains '|', split it into multiple parts
        if '|' in self.greeting_message:
            self.greeting_parts = [part.strip() for part in self.greeting_message.split('|') if part.strip()]
        else:
            self.greeting_parts = [self.greeting_message]
        
        # Dead air delay settings
        self.dead_air_delay_seconds = self.get_config_value('Greeter_Command', 'dead_air_delay_seconds',
                                                           fallback=0, value_type='int')
        self.defer_to_human_greeting = self.get_config_value('Greeter_Command', 'defer_to_human_greeting',
                                                            fallback=False, value_type='bool')
        self.levenshtein_distance = self.get_config_value('Greeter_Command', 'levenshtein_distance',
                                                          fallback=0, value_type='int')
    
    def _init_greeter_tables(self) -> None:
        """Initialize database tables for greeter tracking."""
        try:
            with self.bot.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                
                # Create greeted_users table for tracking who has been greeted
                # channel can be NULL for global greetings (default behavior)
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS greeted_users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        sender_id TEXT NOT NULL,
                        channel TEXT,
                        greeted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        rollout_marked BOOLEAN DEFAULT 0,
                        UNIQUE(sender_id, channel)
                    )
                ''')
                
                # Create indexes for better performance
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_greeted_sender ON greeted_users(sender_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_greeted_channel ON greeted_users(channel)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_greeted_at ON greeted_users(greeted_at)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_greeted_sender_channel ON greeted_users(sender_id, channel)')
                
                # Create greeter_rollout table to track rollout period
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS greeter_rollout (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        rollout_started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        rollout_days INTEGER NOT NULL,
                        rollout_completed BOOLEAN DEFAULT 0,
                        active_users_marked INTEGER DEFAULT 0
                    )
                ''')
                
                conn.commit()
                
                # Clean up any existing duplicates (in case they existed before UNIQUE constraint)
                self._cleanup_duplicate_greetings()
                
                self.logger.info("Greeter tables initialized successfully")
                
        except Exception as e:
            self.logger.error(f"Failed to initialize greeter tables: {e}")
            raise
    
    def _check_rollout_period(self) -> None:
        """Check if we're in a rollout period and mark active users if needed."""
        if not self.enabled:
            return
        
        try:
            with self.bot.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                
                # Check if there's an active rollout
                cursor.execute('''
                    SELECT id, rollout_started_at, rollout_days, rollout_completed
                    FROM greeter_rollout
                    WHERE rollout_completed = 0
                    ORDER BY rollout_started_at DESC
                    LIMIT 1
                ''')
                
                rollout = cursor.fetchone()
                
                if rollout:
                    rollout_id, started_at_str, rollout_days, completed = rollout
                    # Use SQLite's datetime functions to handle timezone correctly
                    cursor.execute('''
                        SELECT datetime(rollout_started_at, '+' || rollout_days || ' days') as end_date,
                               datetime('now') as current_time
                        FROM greeter_rollout
                        WHERE id = ?
                    ''', (rollout_id,))
                    time_result = cursor.fetchone()
                    
                    if time_result:
                        end_date_str, current_time_str = time_result
                        end_date = datetime.fromisoformat(end_date_str)
                        current_time = datetime.fromisoformat(current_time_str)
                        
                        if current_time < end_date:
                            # Still in rollout period - mark active users
                            remaining = (end_date - current_time).total_seconds() / 86400
                            self.logger.info(f"Greeter rollout active: marking active users (ends {end_date}, {remaining:.1f} days remaining)")
                            self._mark_active_users_as_greeted(rollout_id)
                        else:
                            # Rollout period ended - mark as completed
                            days_over = (current_time - end_date).total_seconds() / 86400
                            cursor.execute('''
                                UPDATE greeter_rollout
                                SET rollout_completed = 1
                                WHERE id = ?
                            ''', (rollout_id,))
                            conn.commit()
                            self.logger.info(f"Greeter rollout period completed (ended {end_date}, {days_over:.1f} days ago) - will check for auto-restart")
                        
        except Exception as e:
            self.logger.error(f"Error checking rollout period: {e}")
    
    def _mark_active_users_as_greeted(self, rollout_id: int) -> None:
        """Mark all users who have posted on public channels during rollout period as greeted.
        
        Args:
            rollout_id: The ID of the active rollout.
        """
        try:
            with self.bot.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                
                # Get rollout start date
                cursor.execute('''
                    SELECT rollout_started_at FROM greeter_rollout WHERE id = ?
                ''', (rollout_id,))
                result = cursor.fetchone()
                if not result:
                    return
                
                rollout_start = datetime.fromisoformat(result[0])
                
                # Find all users who posted on public channels since rollout started
                # Only get messages that are NOT DMs (is_dm = 0) and have a channel
                cursor.execute('''
                    SELECT DISTINCT sender_id, channel
                    FROM message_stats
                    WHERE is_dm = 0
                      AND channel IS NOT NULL
                      AND channel != ''
                      AND timestamp >= ?
                ''', (int(rollout_start.timestamp()),))
                
                active_users = cursor.fetchall()
                marked_count = 0
                
                for sender_id, channel in active_users:
                    # Mark based on per_channel_greetings setting
                    # If per_channel_greetings is False, mark globally (channel = NULL)
                    # If per_channel_greetings is True, mark per channel
                    if self.per_channel_greetings:
                        mark_channel = channel
                        # Check if already greeted on this channel
                        cursor.execute('''
                            SELECT id FROM greeted_users
                            WHERE sender_id = ? AND channel = ?
                        ''', (sender_id, mark_channel))
                    else:
                        mark_channel = None
                        # Check if already greeted globally
                        cursor.execute('''
                            SELECT id FROM greeted_users
                            WHERE sender_id = ? AND channel IS NULL
                        ''', (sender_id,))
                    
                    if not cursor.fetchone():
                        # Mark as greeted with rollout flag
                        cursor.execute('''
                            INSERT OR IGNORE INTO greeted_users
                            (sender_id, channel, rollout_marked, greeted_at)
                            VALUES (?, ?, 1, ?)
                        ''', (sender_id, mark_channel, rollout_start.isoformat()))
                        marked_count += 1
                
                # Update rollout record
                cursor.execute('''
                    UPDATE greeter_rollout
                    SET active_users_marked = active_users_marked + ?
                    WHERE id = ?
                ''', (marked_count, rollout_id))
                
                conn.commit()
                
                if marked_count > 0:
                    self.logger.info(f"Marked {marked_count} active users as greeted during rollout")
                    
        except Exception as e:
            self.logger.error(f"Error marking active users as greeted: {e}")
    
    def backfill_greeted_users(self, lookback_days: Optional[int] = None) -> Dict[str, Any]:
        """Backfill greeted_users table from historical message_stats data.
        
        This allows marking all users who have posted on public channels in the past,
        which can shorten or eliminate the rollout period.
        
        Args:
            lookback_days: Number of days to look back (None = all time).
            
        Returns:
            Dict[str, Any]: Dictionary with backfill results (marked_count, total_users, etc.)
        """
        if not self.enabled:
            self.logger.warning("Greeter is disabled - cannot backfill")
            return {'success': False, 'error': 'Greeter is disabled'}
        
        try:
            with self.bot.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                
                # Check if message_stats table exists
                cursor.execute('''
                    SELECT name FROM sqlite_master 
                    WHERE type='table' AND name='message_stats'
                ''')
                if not cursor.fetchone():
                    return {
                        'success': False,
                        'error': 'message_stats table does not exist',
                        'marked_count': 0
                    }
                
                # Build query to find all users who posted on public channels
                if lookback_days:
                    cutoff_timestamp = int(time.time()) - (lookback_days * 24 * 60 * 60)
                    cursor.execute('''
                        SELECT DISTINCT sender_id, channel
                        FROM message_stats
                        WHERE is_dm = 0
                          AND channel IS NOT NULL
                          AND channel != ''
                          AND timestamp >= ?
                    ''', (cutoff_timestamp,))
                else:
                    # All time
                    cursor.execute('''
                        SELECT DISTINCT sender_id, channel
                        FROM message_stats
                        WHERE is_dm = 0
                          AND channel IS NOT NULL
                          AND channel != ''
                    ''')
                
                historical_users = cursor.fetchall()
                marked_count = 0
                skipped_count = 0
                
                for sender_id, channel in historical_users:
                    # Mark based on per_channel_greetings setting
                    if self.per_channel_greetings:
                        mark_channel = channel
                        # Check if already greeted on this channel
                        cursor.execute('''
                            SELECT id FROM greeted_users
                            WHERE sender_id = ? AND channel = ?
                        ''', (sender_id, mark_channel))
                    else:
                        mark_channel = None
                        # Check if already greeted globally
                        cursor.execute('''
                            SELECT id FROM greeted_users
                            WHERE sender_id = ? AND channel IS NULL
                        ''', (sender_id,))
                    
                    if not cursor.fetchone():
                        # Mark as greeted with backfill flag (use current time as greeted_at)
                        cursor.execute('''
                            INSERT OR IGNORE INTO greeted_users
                            (sender_id, channel, rollout_marked, greeted_at)
                            VALUES (?, ?, 1, datetime('now'))
                        ''', (sender_id, mark_channel))
                        marked_count += 1
                    else:
                        skipped_count += 1
                
                conn.commit()
                
                result = {
                    'success': True,
                    'marked_count': marked_count,
                    'skipped_count': skipped_count,
                    'total_users_found': len(historical_users),
                    'lookback_days': lookback_days
                }
                
                self.logger.info(f"Backfilled {marked_count} users from historical message_stats data "
                               f"({skipped_count} already marked, {len(historical_users)} total found)")
                
                return result
                
        except Exception as e:
            self.logger.error(f"Error backfilling greeted users: {e}")
            return {
                'success': False,
                'error': str(e),
                'marked_count': 0
            }
    
    def start_rollout(self, days: Optional[int] = None, backfill_first: bool = True) -> bool:
        """Start a rollout period where all active users are marked as greeted.
        
        Args:
            days: Number of days for rollout period (uses config default if None).
            backfill_first: If True, backfill from historical data before starting rollout.
            
        Returns:
            bool: True if rollout started successfully.
        """
        if not self.enabled:
            self.logger.warning("Greeter is disabled - cannot start rollout")
            return False
        
        try:
            # Backfill from historical data first if requested
            if backfill_first:
                self.logger.info("Backfilling from historical data before starting rollout...")
                backfill_result = self.backfill_greeted_users(lookback_days=None)  # All time
                if backfill_result['success']:
                    self.logger.info(f"Backfilled {backfill_result['marked_count']} users from history")
            
            rollout_days = days or self.rollout_days
            
            with self.bot.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                
                # Check if there's already an active rollout
                cursor.execute('''
                    SELECT id FROM greeter_rollout
                    WHERE rollout_completed = 0
                ''')
                
                if cursor.fetchone():
                    self.logger.warning("Rollout already in progress")
                    return False
                
                # Start new rollout
                cursor.execute('''
                    INSERT INTO greeter_rollout (rollout_days)
                    VALUES (?)
                ''', (rollout_days,))
                
                rollout_id = cursor.lastrowid
                conn.commit()
                
                # Mark active users immediately
                self._mark_active_users_as_greeted(rollout_id)
                
                self.logger.info(f"Started greeter rollout for {rollout_days} days (ID: {rollout_id})")
                return True
                
        except Exception as e:
            self.logger.error(f"Error starting rollout: {e}")
            return False
    
    def _levenshtein_distance(self, s1: str, s2: str) -> int:
        """Calculate Levenshtein distance between two strings.
        
        Args:
            s1: First string.
            s2: Second string.
            
        Returns:
            int: Levenshtein distance (number of edits needed).
        """
        if len(s1) < len(s2):
            return self._levenshtein_distance(s2, s1)
        
        if len(s2) == 0:
            return len(s1)
        
        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row
        
        return previous_row[-1]
    
    def _find_similar_greeted_user(self, sender_id: str, channel: str) -> Optional[str]:
        """Find if a user with a similar name has been greeted.
        
        Args:
            sender_id: The user's ID to check.
            channel: The channel name (used only if per_channel_greetings is True).
            
        Returns:
            Optional[str]: The greeted sender_id if a similar one is found, None otherwise.
        """
        if self.levenshtein_distance <= 0:
            return None
        
        try:
            with self.bot.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                
                if self.per_channel_greetings:
                    # Per-channel mode: check greeted users on this specific channel
                    cursor.execute('''
                        SELECT DISTINCT sender_id FROM greeted_users
                        WHERE channel = ?
                    ''', (channel,))
                else:
                    # Global mode: check all greeted users (channel = NULL)
                    cursor.execute('''
                        SELECT DISTINCT sender_id FROM greeted_users
                        WHERE channel IS NULL
                    ''')
                
                greeted_users = cursor.fetchall()
                
                # Check each greeted user for similarity
                for (greeted_id,) in greeted_users:
                    distance = self._levenshtein_distance(sender_id.lower(), greeted_id.lower())
                    if distance <= self.levenshtein_distance:
                        self.logger.debug(f"Found similar user: {greeted_id} (distance: {distance} from {sender_id})")
                        return greeted_id
                
                return None
        except Exception as e:
            self.logger.error(f"Error checking for similar greeted users: {e}")
            return None
    
    def has_been_greeted(self, sender_id: str, channel: str) -> bool:
        """Check if a user has been greeted.
        
        Args:
            sender_id: The user's ID.
            channel: The channel name (used only if per_channel_greetings is True).
            
        Returns:
            bool: True if user has been greeted (globally or on this channel), False otherwise.
        """
        try:
            with self.bot.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                
                if self.per_channel_greetings:
                    # Per-channel mode: check if greeted on this specific channel
                    cursor.execute('''
                        SELECT id FROM greeted_users
                        WHERE sender_id = ? AND channel = ?
                    ''', (sender_id, channel))
                else:
                    # Global mode: check if greeted at all (channel = NULL)
                    cursor.execute('''
                        SELECT id FROM greeted_users
                        WHERE sender_id = ? AND channel IS NULL
                    ''', (sender_id,))
                
                if cursor.fetchone() is not None:
                    return True
                
                # If exact match not found and Levenshtein distance is enabled, check for similar names
                if self.levenshtein_distance > 0:
                    similar_user = self._find_similar_greeted_user(sender_id, channel)
                    if similar_user:
                        self.logger.info(f"User {sender_id} matches previously greeted user {similar_user} (Levenshtein distance enabled)")
                        return True
                
                return False
        except Exception as e:
            self.logger.error(f"Error checking if user has been greeted: {e}")
            return False
    
    def mark_as_greeted(self, sender_id: str, channel: str) -> bool:
        """Mark a user as greeted atomically.
        
        Uses INSERT OR IGNORE with UNIQUE constraint to handle race conditions.
        
        Args:
            sender_id: The user's ID.
            channel: The channel name (stored only if per_channel_greetings is True).
            
        Returns:
            bool: True if user was marked (or already marked), False on error.
        """
        try:
            self.logger.debug(f"Marking {sender_id} as greeted (channel: {channel})")
            
            with self.bot.db_manager.get_connection() as conn:
                # Use WAL mode for better concurrency (if not already enabled)
                # This helps with race conditions
                conn.execute('PRAGMA journal_mode=WAL')
                
                cursor = conn.cursor()
                
                # Check if user is already greeted first to avoid unnecessary inserts
                # This also helps us detect and handle any existing duplicates
                if self.per_channel_greetings:
                    cursor.execute('''
                        SELECT id, greeted_at FROM greeted_users
                        WHERE sender_id = ? AND channel = ?
                        ORDER BY greeted_at ASC
                        LIMIT 1
                    ''', (sender_id, channel))
                    existing = cursor.fetchone()
                    
                    if existing:
                        # User already greeted - check if there are duplicates
                        cursor.execute('''
                            SELECT COUNT(*) FROM greeted_users
                            WHERE sender_id = ? AND channel = ?
                        ''', (sender_id, channel))
                        count = cursor.fetchone()[0]
                        if count > 1:
                            # Duplicates exist - clean them up, keeping the earliest (first) greeting
                            cursor.execute('''
                                SELECT id FROM greeted_users
                                WHERE sender_id = ? AND channel = ?
                                ORDER BY greeted_at ASC
                            ''', (sender_id, channel))
                            all_ids = [row[0] for row in cursor.fetchall()]
                            if len(all_ids) > 1:
                                # Delete all but the first (earliest)
                                placeholders = ','.join(['?'] * (len(all_ids) - 1))
                                cursor.execute(f'''
                                    DELETE FROM greeted_users
                                    WHERE id IN ({placeholders})
                                ''', all_ids[1:])
                                conn.commit()
                                self.logger.debug(f"Cleaned up {len(all_ids) - 1} duplicate greeting entries for {sender_id} on {channel}, kept earliest")
                        self.logger.debug(f"User {sender_id} already greeted on channel {channel}")
                        return True
                    
                    # User not greeted yet - insert
                    try:
                        cursor.execute('''
                            INSERT INTO greeted_users (sender_id, channel)
                            VALUES (?, ?)
                        ''', (sender_id, channel))
                        conn.commit()
                        self.logger.info(f"✅ Saved: Marked {sender_id} as greeted on channel {channel}")
                        return True
                    except sqlite3.IntegrityError:
                        # Race condition - another process inserted it between our check and insert
                        # This is fine, the user is now greeted
                        conn.rollback()
                        self.logger.debug(f"User {sender_id} was marked as greeted by another process (race condition)")
                        return True
                else:
                    # Global mode: store NULL for channel (greeted once globally)
                    cursor.execute('''
                        SELECT id, greeted_at FROM greeted_users
                        WHERE sender_id = ? AND channel IS NULL
                        ORDER BY greeted_at ASC
                        LIMIT 1
                    ''', (sender_id,))
                    existing = cursor.fetchone()
                    
                    if existing:
                        # User already greeted - check if there are duplicates
                        cursor.execute('''
                            SELECT COUNT(*) FROM greeted_users
                            WHERE sender_id = ? AND channel IS NULL
                        ''', (sender_id,))
                        count = cursor.fetchone()[0]
                        if count > 1:
                            # Duplicates exist - clean them up, keeping the earliest (first) greeting
                            cursor.execute('''
                                SELECT id FROM greeted_users
                                WHERE sender_id = ? AND channel IS NULL
                                ORDER BY greeted_at ASC
                            ''', (sender_id,))
                            all_ids = [row[0] for row in cursor.fetchall()]
                            if len(all_ids) > 1:
                                # Delete all but the first (earliest)
                                placeholders = ','.join(['?'] * (len(all_ids) - 1))
                                cursor.execute(f'''
                                    DELETE FROM greeted_users
                                    WHERE id IN ({placeholders})
                                ''', all_ids[1:])
                                conn.commit()
                                self.logger.debug(f"Cleaned up {len(all_ids) - 1} duplicate greeting entries for {sender_id} (global), kept earliest")
                        self.logger.debug(f"User {sender_id} already greeted globally")
                        return True
                    
                    # User not greeted yet - insert
                    try:
                        cursor.execute('''
                            INSERT INTO greeted_users (sender_id, channel)
                            VALUES (?, NULL)
                        ''', (sender_id,))
                        conn.commit()
                        self.logger.info(f"✅ Saved: Marked {sender_id} as greeted globally (all channels)")
                        return True
                    except sqlite3.IntegrityError:
                        # Race condition - another process inserted it between our check and insert
                        # This is fine, the user is now greeted
                        conn.rollback()
                        self.logger.debug(f"User {sender_id} was marked as greeted by another process (race condition)")
                        return True
                        
        except sqlite3.IntegrityError as e:
            # UNIQUE constraint violation - should not happen with INSERT OR IGNORE
            # but handle it gracefully if it does (means user already marked)
            self.logger.debug(f"User {sender_id} already marked as greeted (integrity check: {e})")
            return True
        except Exception as e:
            self.logger.error(f"❌ Error marking user as greeted: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False
    
    def get_greeted_users_count(self) -> int:
        """Get count of users who have been greeted.
        
        Returns:
            int: The total count of greeted users.
        """
        try:
            with self.bot.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM greeted_users')
                count = cursor.fetchone()[0]
                return count
        except Exception as e:
            self.logger.error(f"Error getting greeted users count: {e}")
            return 0
    
    def _cleanup_duplicate_greetings(self) -> None:
        """Remove duplicate entries from greeted_users table."""
        try:
            with self.bot.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                
                # Find duplicates - count how many exist per (sender_id, channel)
                cursor.execute('''
                    SELECT sender_id, channel, COUNT(*) as count
                    FROM greeted_users
                    GROUP BY sender_id, channel
                    HAVING COUNT(*) > 1
                ''')
                duplicates = cursor.fetchall()
                
                if duplicates:
                    self.logger.info(f"Found {len(duplicates)} duplicate greeting entries, cleaning up...")
                    
                    # For each duplicate, keep only the earliest (first) entry
                    for sender_id, channel, count in duplicates:
                        # Get all IDs for this (sender_id, channel) combination, ordered by earliest first
                        if channel:
                            cursor.execute('''
                                SELECT id, greeted_at
                                FROM greeted_users
                                WHERE sender_id = ? AND channel = ?
                                ORDER BY greeted_at ASC
                            ''', (sender_id, channel))
                        else:
                            cursor.execute('''
                                SELECT id, greeted_at
                                FROM greeted_users
                                WHERE sender_id = ? AND channel IS NULL
                                ORDER BY greeted_at ASC
                            ''', (sender_id,))
                        
                        rows = cursor.fetchall()
                        if len(rows) > 1:
                            # Keep the first (earliest) one, delete the rest
                            keep_id = rows[0][0]
                            delete_ids = [row[0] for row in rows[1:]]
                            
                            # Delete duplicates
                            placeholders = ','.join(['?'] * len(delete_ids))
                            cursor.execute(f'''
                                DELETE FROM greeted_users
                                WHERE id IN ({placeholders})
                            ''', delete_ids)
                            
                            self.logger.debug(f"Kept earliest greeting record {keep_id} for {sender_id} (channel: {channel or 'global'}), deleted {len(delete_ids)} duplicates")
                    
                    conn.commit()
                    self.logger.info(f"Cleaned up duplicate greeting entries")
                else:
                    self.logger.debug("No duplicate greeting entries found")
                    
        except Exception as e:
            self.logger.error(f"Error cleaning up duplicate greetings: {e}")
            # Don't raise - allow initialization to continue even if cleanup fails
    
    def get_recent_greeted_users(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent greeted users.
        
        Args:
            limit: Maximum number of users to return.
            
        Returns:
            List[Dict[str, Any]]: A list of dictionaries containing greeted user info.
        """
        try:
            with self.bot.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT sender_id, channel, MIN(greeted_at) as greeted_at, 
                           MAX(rollout_marked) as rollout_marked
                    FROM greeted_users
                    GROUP BY sender_id, channel
                    ORDER BY MIN(greeted_at) DESC
                    LIMIT ?
                ''', (limit,))
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Error getting recent greeted users: {e}")
            return []
    
    async def _get_mesh_info(self) -> Dict[str, Any]:
        """Get mesh network information for greeting.
        
        Returns:
            Dict[str, Any]: A dictionary containing mesh statistics.
        """
        info = {
            'total_contacts': 0,
            'repeaters': 0,
            'companions': 0,
            'recent_activity_24h': 0
        }
        
        try:
            # Get contact statistics from repeater manager if available
            if hasattr(self.bot, 'repeater_manager'):
                try:
                    stats = await self.bot.repeater_manager.get_contact_statistics()
                    if stats:
                        info['total_contacts'] = stats.get('total_heard', 0)
                        info['repeaters'] = stats.get('by_role', {}).get('repeater', 0)
                        info['companions'] = stats.get('by_role', {}).get('companion', 0)
                        info['recent_activity_24h'] = stats.get('recent_activity', 0)
                except Exception as e:
                    self.logger.debug(f"Error getting stats from repeater_manager: {e}")
            
            # Fallback to device contacts if repeater manager stats not available
            if info['total_contacts'] == 0 and hasattr(self.bot, 'meshcore') and hasattr(self.bot.meshcore, 'contacts'):
                info['total_contacts'] = len(self.bot.meshcore.contacts)
                
                # Count repeaters and companions
                if hasattr(self.bot, 'repeater_manager'):
                    for contact_data in self.bot.meshcore.contacts.values():
                        if self.bot.repeater_manager._is_repeater_device(contact_data):
                            info['repeaters'] += 1
                        else:
                            info['companions'] += 1
            
            # Get recent activity from message_stats if available
            if info['recent_activity_24h'] == 0:
                try:
                    with self.bot.db_manager.get_connection() as conn:
                        cursor = conn.cursor()
                        # Check if message_stats table exists
                        cursor.execute('''
                            SELECT name FROM sqlite_master 
                            WHERE type='table' AND name='message_stats'
                        ''')
                        if cursor.fetchone():
                            cutoff_time = int(time.time()) - (24 * 60 * 60)
                            cursor.execute('''
                                SELECT COUNT(DISTINCT sender_id)
                                FROM message_stats
                                WHERE timestamp >= ? AND is_dm = 0
                            ''', (cutoff_time,))
                            result = cursor.fetchone()
                            if result:
                                info['recent_activity_24h'] = result[0]
                except Exception:
                    pass
                    
        except Exception as e:
            self.logger.debug(f"Error getting mesh info: {e}")
        
        return info
    
    def _get_greeting_for_channel(self, channel: str) -> str:
        """Get greeting message for a specific channel.
        
        Args:
            channel: Channel name.
            
        Returns:
            str: Greeting message template for the channel, or default if not specified.
        """
        if channel and channel.lower() in self.channel_greetings:
            return self.channel_greetings[channel.lower()]['greeting']
        return self.greeting_message
    
    async def _format_greeting_parts(self, sender_id: str, channel: Optional[str] = None, mesh_info: Optional[Dict[str, Any]] = None) -> List[str]:
        """Format greeting message parts with mesh information.
        
        Args:
            sender_id: The user's ID.
            channel: Channel name (for channel-specific greetings).
            mesh_info: Optional mesh info dict (will be fetched if None).
        
        Returns:
            List[str]: List of greeting message strings (for multi-part greetings).
        """
        if mesh_info is None:
            mesh_info = await self._get_mesh_info()
        
        # Get channel-specific greeting if available, otherwise use default
        greeting_template = self._get_greeting_for_channel(channel) if channel else self.greeting_message
        
        # Parse multi-part greetings (pipe-separated)
        if '|' in greeting_template:
            greeting_parts = [part.strip() for part in greeting_template.split('|') if part.strip()]
        else:
            greeting_parts = [greeting_template]
        
        # Format each greeting part
        formatted_parts = []
        for part in greeting_parts:
            formatted_part = part.format(sender=sender_id)
            formatted_parts.append(formatted_part)
        
        # Add mesh info to the last part if enabled
        if self.include_mesh_info:
            self.logger.debug(f"Including mesh info. Format: {repr(self.mesh_info_format)}, Mesh info: {mesh_info}")
            try:
                mesh_info_text = self.mesh_info_format.format(
                    total_contacts=mesh_info.get('total_contacts', 0),
                    repeaters=mesh_info.get('repeaters', 0),
                    companions=mesh_info.get('companions', 0),
                    recent_activity_24h=mesh_info.get('recent_activity_24h', 0)
                )
                self.logger.debug(f"Formatted mesh info text: {repr(mesh_info_text)}")
                # Append mesh info to the last greeting part
                if formatted_parts:
                    formatted_parts[-1] += mesh_info_text
                else:
                    formatted_parts.append(mesh_info_text)
            except (KeyError, ValueError) as e:
                self.logger.warning(f"Error formatting mesh info: {e}. Format string: {repr(self.mesh_info_format)}, Mesh info keys: {list(mesh_info.keys())}")
                # Continue without mesh info rather than failing the entire greeting
            except Exception as e:
                self.logger.error(f"Unexpected error formatting mesh info: {e}", exc_info=True)
                # Continue without mesh info rather than failing the entire greeting
        else:
            self.logger.debug("Mesh info not included (include_mesh_info is False)")
        
        return formatted_parts
    
    def matches_keyword(self, message: MeshMessage) -> bool:
        """Greeter doesn't match keywords - it's triggered automatically.
        
        Args:
            message: The message to check.
            
        Returns:
            bool: Always False.
        """
        return False
    
    def matches_custom_syntax(self, message: MeshMessage) -> bool:
        """Greeter doesn't match custom syntax.
        
        Args:
            message: The message to check.
            
        Returns:
            bool: Always False.
        """
        return False
    
    def _is_rollout_active(self) -> bool:
        """Check if there's an active rollout period.
        
        Returns:
            bool: True if a rollout is active, False otherwise.
        """
        try:
            with self.bot.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                # Use SQLite's datetime functions to calculate end date and compare with current time
                # This handles timezone issues automatically since both are in UTC
                cursor.execute('''
                    SELECT id, rollout_started_at, rollout_days,
                           datetime(rollout_started_at, '+' || rollout_days || ' days') as end_date,
                           datetime('now') as current_time
                    FROM greeter_rollout
                    WHERE rollout_completed = 0
                    ORDER BY rollout_started_at DESC
                    LIMIT 1
                ''')
                rollout = cursor.fetchone()
                
                if rollout:
                    rollout_id, started_at_str, rollout_days, end_date_str, current_time_str = rollout
                    
                    # Parse for logging (both are in UTC from SQLite)
                    started_at = datetime.fromisoformat(started_at_str)
                    end_date = datetime.fromisoformat(end_date_str)
                    current_time = datetime.fromisoformat(current_time_str)
                    
                    if current_time < end_date:
                        remaining = (end_date - current_time).total_seconds() / 86400  # days
                        self.logger.debug(f"Rollout active: {remaining:.1f} days remaining (started {started_at}, ends {end_date})")
                        return True
                    else:
                        # Rollout period ended - mark as completed
                        days_over = (current_time - end_date).total_seconds() / 86400
                        cursor.execute('''
                            UPDATE greeter_rollout
                            SET rollout_completed = 1
                            WHERE id = ?
                        ''', (rollout_id,))
                        conn.commit()
                        self.logger.info(f"Greeter rollout period completed (ended {end_date}, {days_over:.1f} days ago)")
                        return False
                
                self.logger.debug("No active rollout found")
                return False
        except Exception as e:
            self.logger.error(f"Error checking rollout status: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False
    
    def _check_human_greeting(self, new_user_id: str, channel: str, since_timestamp: int) -> bool:
        """Check if a human has greeted the new user.
        
        Args:
            new_user_id: The new user's ID to check for.
            channel: The channel to check.
            since_timestamp: Only check messages after this timestamp.
            
        Returns:
            bool: True if a human has mentioned the new user, False otherwise.
        """
        if not self.defer_to_human_greeting:
            return False
        
        try:
            with self.bot.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                
                # Check if message_stats table exists
                cursor.execute('''
                    SELECT name FROM sqlite_master 
                    WHERE type='table' AND name='message_stats'
                ''')
                if not cursor.fetchone():
                    return False
                
                # Get recent messages from this channel since the new user posted
                cursor.execute('''
                    SELECT sender_id, content
                    FROM message_stats
                    WHERE channel = ?
                      AND timestamp >= ?
                      AND is_dm = 0
                      AND sender_id != ?
                    ORDER BY timestamp DESC
                ''', (channel, since_timestamp, new_user_id))
                
                messages = cursor.fetchall()
                
                # Check if any message contains the new user's name
                new_user_id_lower = new_user_id.lower()
                for sender_id, content in messages:
                    if content and new_user_id_lower in content.lower():
                        # Also check with Levenshtein distance if enabled
                        if self.levenshtein_distance > 0:
                            # Check if any word in the message is within Levenshtein distance
                            words = content.lower().split()
                            for word in words:
                                # Remove common punctuation
                                word = word.strip('.,!?;:()[]{}@')
                                distance = self._levenshtein_distance(new_user_id_lower, word)
                                if distance <= self.levenshtein_distance:
                                    self.logger.info(f"Human greeting detected: {sender_id} mentioned {new_user_id} in channel {channel}")
                                    return True
                        else:
                            # Simple substring match
                            self.logger.info(f"Human greeting detected: {sender_id} mentioned {new_user_id} in channel {channel}")
                            return True
                
                return False
        except Exception as e:
            self.logger.error(f"Error checking for human greeting: {e}")
            return False
    
    def _cancel_pending_greeting(self, sender_id: str, channel: str) -> None:
        """Cancel a pending greeting if it exists.
        
        Args:
            sender_id: The user's ID.
            channel: The channel name.
        """
        key = (sender_id, channel)
        if key in self.pending_greetings:
            task = self.pending_greetings[key]
            if not task.done():
                task.cancel()
                self.logger.info(f"Cancelled pending greeting for {sender_id} on {channel}")
            del self.pending_greetings[key]
    
    async def _send_delayed_greeting(self, message: MeshMessage) -> None:
        """Send a greeting after the dead air delay.
        
        Args:
            message: The original message that triggered the greeting.
        """
        key = (message.sender_id, message.channel)
        original_timestamp = message.timestamp or int(time.time())
        
        try:
            # Wait for the dead air delay
            if self.dead_air_delay_seconds > 0:
                self.logger.debug(f"Waiting {self.dead_air_delay_seconds} seconds before greeting {message.sender_id} on {message.channel}")
                await asyncio.sleep(self.dead_air_delay_seconds)
            
            # Check if greeting was cancelled (user was already greeted or human responded)
            if key not in self.pending_greetings:
                self.logger.debug(f"Greeting for {message.sender_id} on {message.channel} was cancelled")
                return
            
            # Check if we should still greet (user might have been greeted by another process)
            if self.has_been_greeted(message.sender_id, message.channel):
                self.logger.debug(f"User {message.sender_id} already greeted on {message.channel} - skipping")
                if key in self.pending_greetings:
                    del self.pending_greetings[key]
                return
            
            # If defer to human greeting is enabled, check if a human has greeted the user
            # Check messages from the original timestamp onwards (during the delay period)
            if self.defer_to_human_greeting and self.dead_air_delay_seconds > 0:
                if self._check_human_greeting(message.sender_id, message.channel, original_timestamp):
                    self.logger.info(f"Deferring to human greeting for {message.sender_id} on {message.channel}")
                    # Mark as greeted so we don't greet them later
                    self.mark_as_greeted(message.sender_id, message.channel)
                    if key in self.pending_greetings:
                        del self.pending_greetings[key]
                    return
            
            # Send the greeting
            await self._send_greeting(message)
            
            # Clean up
            if key in self.pending_greetings:
                del self.pending_greetings[key]
                
        except asyncio.CancelledError:
            self.logger.debug(f"Delayed greeting for {message.sender_id} on {message.channel} was cancelled")
            # Clean up on cancellation
            if key in self.pending_greetings:
                del self.pending_greetings[key]
        except Exception as e:
            self.logger.error(f"Error in delayed greeting for {message.sender_id}: {e}")
            if key in self.pending_greetings:
                del self.pending_greetings[key]
    
    async def _send_greeting(self, message: MeshMessage) -> bool:
        """Actually send the greeting message.
        
        Args:
            message: The message that triggered the greeting.
            
        Returns:
            bool: True if greeting was sent successfully.
        """
        try:
            # Format greeting parts (may be single or multi-part)
            # Pass channel name for channel-specific greetings
            greeting_parts = await self._format_greeting_parts(message.sender_id, message.channel)
            
            # Send greeting(s)
            mode_str = "per-channel" if self.per_channel_greetings else "global"
            self.logger.info(f"Greeting {message.sender_id} on channel {message.channel} ({mode_str} mode, {len(greeting_parts)} part(s))")
            
            # Log database verification
            total_greeted = self.get_greeted_users_count()
            self.logger.debug(f"Database verification: {total_greeted} total user(s) marked as greeted")
            
            # Send all greeting parts
            success = True
            for i, greeting_part in enumerate(greeting_parts):
                if i > 0:
                    # Wait for bot TX rate limiter between multi-part messages
                    # This ensures we respect the bot's rate limiting configuration
                    # Note: User rate limiter is not checked here since greeter is an automated bot response
                    await self.bot.bot_tx_rate_limiter.wait_for_tx()
                    
                    # Additional delay to ensure proper spacing (use configured rate limit)
                    rate_limit = self.bot.config.getfloat('Bot', 'bot_tx_rate_limit_seconds', fallback=1.0)
                    # Use a conservative sleep time to avoid rate limiting
                    sleep_time = max(rate_limit + 0.5, 1.0)  # At least 1 second, or rate_limit + 0.5 seconds
                    await asyncio.sleep(sleep_time)
                
                # Skip user rate limiter for greeter messages since they're automated bot responses
                result = await self.send_response(message, greeting_part, skip_user_rate_limit=True)
                if not result:
                    success = False
                    self.logger.warning(f"Failed to send greeting part {i+1} of {len(greeting_parts)}")
            
            return success
        except Exception as e:
            self.logger.error(f"Error sending greeting: {e}")
            return False
    
    def should_execute(self, message: MeshMessage) -> bool:
        """Check if greeter should execute for this message.
        
        Args:
            message: The message to check.
            
        Returns:
            bool: True if the greeter should execute, False otherwise.
        """
        if not self.enabled:
            return False
        
        # Only greet on public channels
        if message.is_dm:
            return False
        
        # Must have a channel name
        if not message.channel:
            return False
        
        # Check channel access using standardized method (with case-insensitive fallback)
        # First try standardized method (case-sensitive)
        if not self.is_channel_allowed(message):
            # If standardized check fails, try case-insensitive matching for backward compatibility
            if self.greeter_channels is not None:
                # Use greeter-specific channels if configured (case-insensitive matching)
                if message.channel and message.channel.lower() not in self.greeter_channels_lower:
                    return False
            else:
                # Fall back to general monitor_channels setting (case-insensitive matching)
                monitor_channels_lower = [ch.lower() for ch in self.bot.command_manager.monitor_channels]
                if message.channel and message.channel.lower() not in monitor_channels_lower:
                    return False
        
        # Check if we're in an active rollout period
        rollout_active = self._is_rollout_active()
        if rollout_active:
            # During rollout, mark user as greeted but don't actually greet them
            # Check if already greeted first to avoid misleading logs
            if not self.has_been_greeted(message.sender_id, message.channel):
                self.logger.info(f"🔄 Rollout active: Marking {message.sender_id} as greeted on {message.channel} (no greeting sent)")
            else:
                self.logger.debug(f"🔄 Rollout active: {message.sender_id} already greeted on {message.channel} (skipping)")
            self.mark_as_greeted(message.sender_id, message.channel)
            return False
        else:
            self.logger.debug(f"Rollout not active - proceeding with greeting check for {message.sender_id}")
        
        # Check if user has already been greeted (globally or per-channel, depending on config)
        if self.has_been_greeted(message.sender_id, message.channel):
            return False
        
        return True
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the greeter command.
        
        Args:
            message: The message triggering the greeting.
            
        Returns:
            bool: True if executed successfully, False otherwise.
        """
        try:
            # Double-check we should greet (race condition protection)
            if not self.should_execute(message):
                return False
            
            # Mark as greeted BEFORE scheduling greeting (to prevent duplicate greetings)
            # This ensures we don't greet the same user twice even if there's a delay
            # mark_as_greeted uses atomic INSERT OR IGNORE to handle race conditions
            marked = self.mark_as_greeted(message.sender_id, message.channel)
            if not marked:
                self.logger.warning(f"Failed to mark {message.sender_id} as greeted - aborting greeting")
                return False
            
            # Final verification: Double-check that user hasn't been greeted by another process
            # This is a last-ditch check to catch any race conditions
            # The INSERT OR IGNORE in mark_as_greeted() should have handled this, but we verify
            if self.has_been_greeted(message.sender_id, message.channel):
                # User is marked - verify this is a fresh mark (not an old one)
                # If the record was just created (within last 5 seconds), we likely created it
                # If it's older, another process may have created it first
                try:
                    with self.bot.db_manager.get_connection() as conn:
                        cursor = conn.cursor()
                        if self.per_channel_greetings:
                            cursor.execute('''
                                SELECT datetime(greeted_at) as greeted_at, datetime('now') as now
                                FROM greeted_users
                                WHERE sender_id = ? AND channel = ?
                            ''', (message.sender_id, message.channel))
                        else:
                            cursor.execute('''
                                SELECT datetime(greeted_at) as greeted_at, datetime('now') as now
                                FROM greeted_users
                                WHERE sender_id = ? AND channel IS NULL
                            ''', (message.sender_id,))
                        result = cursor.fetchone()
                        if result:
                            greeted_at_str, now_str = result
                            greeted_at = datetime.fromisoformat(greeted_at_str)
                            now = datetime.fromisoformat(now_str)
                            seconds_ago = (now - greeted_at).total_seconds()
                            
                            # If marked more than 5 seconds ago, likely another process did it first
                            # (our mark_as_greeted should have just run, so it should be very recent)
                            if seconds_ago > 5:
                                self.logger.info(f"User {message.sender_id} was already greeted {seconds_ago:.1f}s ago by another process - aborting duplicate greeting")
                                return False
                            else:
                                self.logger.debug(f"User {message.sender_id} marked {seconds_ago:.1f}s ago - proceeding with greeting")
                except Exception as e:
                    # If check fails, proceed anyway (better to greet than miss a greeting)
                    self.logger.debug(f"Could not verify greeting timestamp (proceeding anyway): {e}")
            
            # Check if dead air delay is enabled
            if self.dead_air_delay_seconds > 0:
                # Schedule delayed greeting
                key = (message.sender_id, message.channel)
                
                # Cancel any existing pending greeting for this user/channel
                if key in self.pending_greetings:
                    self._cancel_pending_greeting(message.sender_id, message.channel)
                
                # Schedule new delayed greeting
                task = asyncio.create_task(self._send_delayed_greeting(message))
                self.pending_greetings[key] = task
                self.logger.info(f"Scheduled delayed greeting for {message.sender_id} on {message.channel} (delay: {self.dead_air_delay_seconds}s)")
                return True
            else:
                # Send greeting immediately (original behavior)
                return await self._send_greeting(message)
            
        except Exception as e:
            self.logger.error(f"Error executing greeter command: {e}")
            return False
    
    def check_message_for_human_greeting(self, message: MeshMessage) -> None:
        """Check if an incoming message should cancel a pending greeting.
        
        Args:
            message: The incoming message to check.
        """
        if not self.defer_to_human_greeting or not self.dead_air_delay_seconds > 0:
            return
        
        if message.is_dm or not message.channel:
            return
        
        # Check all pending greetings for this channel
        keys_to_cancel = []
        for (sender_id, channel), task in list(self.pending_greetings.items()):
            if channel == message.channel and sender_id != message.sender_id:
                # Check if this message mentions the pending user
                if message.content and sender_id.lower() in message.content.lower():
                    # Also check with Levenshtein distance if enabled
                    should_cancel = False
                    if self.levenshtein_distance > 0:
                        words = message.content.lower().split()
                        for word in words:
                            word = word.strip('.,!?;:()[]{}@')
                            distance = self._levenshtein_distance(sender_id.lower(), word)
                            if distance <= self.levenshtein_distance:
                                should_cancel = True
                                break
                    else:
                        should_cancel = True
                    
                    if should_cancel:
                        self.logger.info(f"Human greeting detected in real-time: {message.sender_id} mentioned {sender_id} - cancelling pending greeting")
                        keys_to_cancel.append((sender_id, channel))
        
        # Cancel the pending greetings
        for key in keys_to_cancel:
            self._cancel_pending_greeting(key[0], key[1])
            # Mark as greeted so we don't greet them later
            self.mark_as_greeted(key[0], key[1])
    
    def get_help_text(self) -> str:
        """Get help text for the greeter command.
        
        Returns:
            str: The help text for this command.
        """
        mode = "per-channel" if self.per_channel_greetings else "global (once total)"
        return f"Greeter automatically welcomes new users on public channels ({mode} mode). Configure in [Greeter_Command] section."

