#!/usr/bin/env python3
"""
Stats command for the MeshCore Bot
Provides comprehensive statistics about bot usage, messages, and activity
"""

import time
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from .base_command import BaseCommand
from ..models import MeshMessage


class StatsCommand(BaseCommand):
    """Handles the stats command with comprehensive data collection"""
    
    # Plugin metadata
    name = "stats"
    keywords = ['stats']
    description = "Show statistics for past 24 hours. Use 'stats messages', 'stats channels', or 'stats paths' for specific stats."
    category = "analytics"
    
    def __init__(self, bot):
        super().__init__(bot)
        self._load_config()
        self._init_stats_tables()
    
    def _load_config(self):
        """Load configuration settings for stats command"""
        self.stats_enabled = self.bot.config.getboolean('Stats', 'stats_enabled', fallback=True)
        self.data_retention_days = self.bot.config.getint('Stats', 'data_retention_days', fallback=7)
        self.auto_cleanup = self.bot.config.getboolean('Stats', 'auto_cleanup', fallback=True)
        self.track_all_messages = self.bot.config.getboolean('Stats', 'track_all_messages', fallback=True)
        self.track_command_details = self.bot.config.getboolean('Stats', 'track_command_details', fallback=True)
        self.anonymize_users = self.bot.config.getboolean('Stats', 'anonymize_users', fallback=False)
    
    def _init_stats_tables(self):
        """Initialize database tables for stats tracking"""
        try:
            with sqlite3.connect(self.bot.db_manager.db_path) as conn:
                cursor = conn.cursor()
                
                # Create message_stats table for tracking all messages
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS message_stats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp INTEGER NOT NULL,
                        sender_id TEXT NOT NULL,
                        channel TEXT,
                        content TEXT NOT NULL,
                        is_dm BOOLEAN NOT NULL,
                        hops INTEGER,
                        snr REAL,
                        rssi INTEGER,
                        path TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Create command_stats table for tracking bot commands
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS command_stats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp INTEGER NOT NULL,
                        sender_id TEXT NOT NULL,
                        command_name TEXT NOT NULL,
                        channel TEXT,
                        is_dm BOOLEAN NOT NULL,
                        response_sent BOOLEAN NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Create path_stats table for tracking longest paths
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS path_stats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp INTEGER NOT NULL,
                        sender_id TEXT NOT NULL,
                        channel TEXT,
                        path_length INTEGER NOT NULL,
                        path_string TEXT NOT NULL,
                        hops INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Create indexes for better performance
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_message_timestamp ON message_stats(timestamp)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_message_sender ON message_stats(sender_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_message_channel ON message_stats(channel)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_command_timestamp ON command_stats(timestamp)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_command_sender ON command_stats(sender_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_command_name ON command_stats(command_name)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_path_timestamp ON path_stats(timestamp)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_path_length ON path_stats(path_length)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_path_sender ON path_stats(sender_id)')
                
                conn.commit()
                self.logger.info("Stats tables initialized successfully")
                
        except Exception as e:
            self.logger.error(f"Failed to initialize stats tables: {e}")
            raise
    
    def record_message(self, message: MeshMessage):
        """Record a message in the stats database"""
        if not self.stats_enabled or not self.track_all_messages:
            return
            
        try:
            # Anonymize user if configured
            sender_id = message.sender_id or 'unknown'
            if self.anonymize_users and sender_id != 'unknown':
                # Create a simple hash-based anonymization
                import hashlib
                sender_id = f"user_{hashlib.md5(sender_id.encode()).hexdigest()[:8]}"
            
            with sqlite3.connect(self.bot.db_manager.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO message_stats 
                    (timestamp, sender_id, channel, content, is_dm, hops, snr, rssi, path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    message.timestamp or int(time.time()),
                    sender_id,
                    message.channel,
                    message.content,
                    message.is_dm,
                    message.hops,
                    message.snr,
                    message.rssi,
                    message.path
                ))
                conn.commit()
        except Exception as e:
            self.logger.error(f"Error recording message stats: {e}")
    
    def record_command(self, message: MeshMessage, command_name: str, response_sent: bool = True):
        """Record a command execution in the stats database"""
        if not self.stats_enabled or not self.track_command_details:
            return
            
        try:
            # Anonymize user if configured
            sender_id = message.sender_id or 'unknown'
            if self.anonymize_users and sender_id != 'unknown':
                # Create a simple hash-based anonymization
                import hashlib
                sender_id = f"user_{hashlib.md5(sender_id.encode()).hexdigest()[:8]}"
            
            with sqlite3.connect(self.bot.db_manager.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO command_stats 
                    (timestamp, sender_id, command_name, channel, is_dm, response_sent)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    message.timestamp or int(time.time()),
                    sender_id,
                    command_name,
                    message.channel,
                    message.is_dm,
                    response_sent
                ))
                conn.commit()
        except Exception as e:
            self.logger.error(f"Error recording command stats: {e}")
    
    def record_path_stats(self, message: MeshMessage):
        """Record path statistics for longest path tracking"""
        if not self.stats_enabled or not self.track_all_messages:
            return
            
        # Only record if we have meaningful path data
        if not message.hops or message.hops <= 0 or not message.path:
            return
        
        # Only record paths that contain actual node IDs (hex characters or comma-separated)
        # Skip descriptive paths like "Routed through X hops"
        if not self._is_valid_path_format(message.path):
            return
            
        try:
            # Anonymize user if configured
            sender_id = message.sender_id or 'unknown'
            if self.anonymize_users and sender_id != 'unknown':
                import hashlib
                sender_id = f"user_{hashlib.md5(sender_id.encode()).hexdigest()[:8]}"
            
            # Format the path string properly (e.g., "75,24,1d,5f,bd")
            path_string = self._format_path_for_display(message.path)
            
            with sqlite3.connect(self.bot.db_manager.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO path_stats 
                    (timestamp, sender_id, channel, path_length, path_string, hops)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    message.timestamp or int(time.time()),
                    sender_id,
                    message.channel,
                    message.hops,  # Use hops as path length
                    path_string,
                    message.hops
                ))
                conn.commit()
        except Exception as e:
            self.logger.error(f"Error recording path stats: {e}")
    
    def _is_valid_path_format(self, path: str) -> bool:
        """Check if path contains actual node IDs rather than descriptive text"""
        if not path:
            return False
        
        # If path contains spaces and common descriptive words, it's likely descriptive text
        descriptive_words = ['routed', 'through', 'hops', 'direct', 'unknown', 'path']
        path_lower = path.lower()
        
        if any(word in path_lower for word in descriptive_words):
            return False
        
        # If path contains only hex characters and commas, it's valid
        if all(c in '0123456789abcdefABCDEF,' for c in path):
            return True
        
        # If path is a single hex string without separators, it's valid
        if all(c in '0123456789abcdefABCDEF' for c in path) and len(path) >= 2:
            return True
        
        return False
    
    def _format_path_for_display(self, path: str) -> str:
        """Format path string for display (e.g., '75,24,1d,5f,bd')"""
        if not path:
            return "Direct"
        
        # If path already contains commas, it's likely already formatted
        if ',' in path:
            return path
        
        # If path contains descriptive text (like "Routed through X hops"), 
        # extract just the numeric part or return as-is
        if ' ' in path and not all(c in '0123456789abcdefABCDEF' for c in path.replace(' ', '')):
            # This looks like descriptive text, return as-is
            return path
        
        # If path is a hex string without separators, add commas every 2 characters
        # But only if it looks like a hex string (all hex characters)
        if len(path) > 2 and ',' not in path and all(c in '0123456789abcdefABCDEF' for c in path):
            # Split into 2-character chunks and join with commas
            formatted = ','.join([path[i:i+2] for i in range(0, len(path), 2)])
            return formatted
        
        # If it's already a single node ID or short path, return as-is
        return path
    
    def get_help_text(self) -> str:
        return "Show 24-hour bot statistics. Commands: 'stats' (basic), 'stats messages' (bot users), 'stats channels' (channel activity), 'stats paths' (longest paths)"
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the stats command"""
        if not self.stats_enabled:
            await self.send_response(message, "Stats command is disabled")
            return False
            
        try:
            # Perform automatic cleanup if enabled
            if self.auto_cleanup:
                self.cleanup_old_stats(self.data_retention_days)
            
            # Parse command arguments
            content = message.content.strip()
            if content.startswith('!'):
                content = content[1:].strip()
            
            parts = content.split()
            if len(parts) > 1:
                subcommand = parts[1].lower()
                if subcommand in ['messages', 'message']:
                    response = await self._get_bot_user_leaderboard()
                elif subcommand in ['channels', 'channel']:
                    response = await self._get_channel_leaderboard()
                elif subcommand in ['paths', 'path']:
                    response = await self._get_path_leaderboard()
                else:
                    response = f"Unknown: {subcommand}. Use 'stats', 'stats messages', 'stats channels', or 'stats paths'"
            else:
                response = await self._get_basic_stats()
            
            # Record this command execution
            self.record_command(message, 'stats', True)
            
            return await self.send_response(message, response)
            
        except Exception as e:
            self.logger.error(f"Error executing stats command: {e}")
            await self.send_response(message, f"Error getting stats: {e}")
            return False
    
    async def _get_basic_stats(self) -> str:
        """Get basic bot statistics"""
        try:
            # Get time window (24 hours ago)
            now = int(time.time())
            day_ago = now - (24 * 60 * 60)
            
            with sqlite3.connect(self.bot.db_manager.db_path) as conn:
                cursor = conn.cursor()
                
                # Bot commands received
                cursor.execute('''
                    SELECT COUNT(*) FROM command_stats 
                    WHERE timestamp >= ?
                ''', (day_ago,))
                commands_received = cursor.fetchone()[0]
                
                # Bot replies sent
                cursor.execute('''
                    SELECT COUNT(*) FROM command_stats 
                    WHERE timestamp >= ? AND response_sent = 1
                ''', (day_ago,))
                bot_replies = cursor.fetchone()[0]
                
                # Top command
                cursor.execute('''
                    SELECT command_name, COUNT(*) as count 
                    FROM command_stats 
                    WHERE timestamp >= ?
                    GROUP BY command_name 
                    ORDER BY count DESC 
                    LIMIT 1
                ''', (day_ago,))
                top_command_result = cursor.fetchone()
                top_command = f"{top_command_result[0]} ({top_command_result[1]})" if top_command_result else "None"
                
                # Top user
                cursor.execute('''
                    SELECT sender_id, COUNT(*) as count 
                    FROM command_stats 
                    WHERE timestamp >= ?
                    GROUP BY sender_id 
                    ORDER BY count DESC 
                    LIMIT 1
                ''', (day_ago,))
                top_user_result = cursor.fetchone()
                top_user = f"{top_user_result[0]} ({top_user_result[1]})" if top_user_result else "None"
                
                response = f"""Bot Stats (24h):
Cmds: {commands_received} | Replies: {bot_replies}
Top cmd: {top_command}
Top user: {top_user}"""
                
                return response
                
        except Exception as e:
            self.logger.error(f"Error getting basic stats: {e}")
            return f"Error getting stats: {e}"
    
    async def _get_bot_user_leaderboard(self) -> str:
        """Get leaderboard for bot users (people who triggered bot responses)"""
        try:
            # Get time window (24 hours ago)
            now = int(time.time())
            day_ago = now - (24 * 60 * 60)
            
            with sqlite3.connect(self.bot.db_manager.db_path) as conn:
                cursor = conn.cursor()
                
                # Top bot users (people who triggered commands)
                cursor.execute('''
                    SELECT sender_id, COUNT(*) as count 
                    FROM command_stats 
                    WHERE timestamp >= ?
                    GROUP BY sender_id 
                    ORDER BY count DESC 
                    LIMIT 5
                ''', (day_ago,))
                top_users = cursor.fetchall()
                
                # Build response
                response = "Bot Users (24h):\n"
                
                if top_users:
                    for i, (user, count) in enumerate(top_users, 1):
                        display_user = user[:12] + "..." if len(user) > 15 else user
                        response += f"{i}. {display_user}: {count}\n"
                else:
                    response += "No bot users\n"
                
                return response
                
        except Exception as e:
            self.logger.error(f"Error getting bot user leaderboard: {e}")
            return f"Error getting bot user stats: {e}"
    
    async def _get_channel_leaderboard(self) -> str:
        """Get leaderboard for channel message activity"""
        try:
            # Get time window (24 hours ago)
            now = int(time.time())
            day_ago = now - (24 * 60 * 60)
            
            with sqlite3.connect(self.bot.db_manager.db_path) as conn:
                cursor = conn.cursor()
                
                # Top channels by message count with unique user counts
                cursor.execute('''
                    SELECT channel, COUNT(*) as message_count, COUNT(DISTINCT sender_id) as unique_users
                    FROM message_stats 
                    WHERE timestamp >= ? AND channel IS NOT NULL
                    GROUP BY channel 
                    ORDER BY message_count DESC 
                    LIMIT 5
                ''', (day_ago,))
                top_channels = cursor.fetchall()
                
                # Build compact response
                response = "Top Channels (24h)\n"
                
                if top_channels:
                    for i, (channel, msg_count, unique_users) in enumerate(top_channels, 1):
                        display_channel = channel[:12] + "..." if len(channel) > 15 else channel
                        # Handle singular/plural for messages and users
                        msg_text = "msg" if msg_count == 1 else "msgs"
                        user_text = "user" if unique_users == 1 else "users"
                        response += f"{i}. {display_channel}: {msg_count} {msg_text} | {unique_users} {user_text}\n"
                    # Remove trailing newline
                    response = response.rstrip('\n')
                else:
                    response += "No channel activity"
                
                return response
                
        except Exception as e:
            self.logger.error(f"Error getting channel leaderboard: {e}")
            return f"Error getting channel stats: {e}"
    
    async def _get_path_leaderboard(self) -> str:
        """Get leaderboard for longest paths seen"""
        try:
            # Get time window (24 hours ago)
            now = int(time.time())
            day_ago = now - (24 * 60 * 60)
            
            with sqlite3.connect(self.bot.db_manager.db_path) as conn:
                cursor = conn.cursor()
                
                # Top longest paths (one per user, more results with compact format)
                cursor.execute('''
                    SELECT sender_id, path_length, path_string 
                    FROM path_stats p1
                    WHERE timestamp >= ? 
                    AND path_length = (
                        SELECT MAX(path_length) 
                        FROM path_stats p2 
                        WHERE p2.sender_id = p1.sender_id 
                        AND p2.timestamp >= ?
                    )
                    GROUP BY sender_id
                    ORDER BY path_length DESC 
                    LIMIT 8
                ''', (day_ago, day_ago))
                longest_paths = cursor.fetchall()
                
                # Build compact response with length checking
                response = ""
                max_length = 130  # Safe length for mesh network
                
                if longest_paths:
                    for i, (sender, path_len, path_str) in enumerate(longest_paths, 1):
                        # Truncate sender name to fit more data
                        display_sender = sender[:8] + "..." if len(sender) > 11 else sender
                        # Compact format: "1 Gundam 56,1c,98,1a,aa,cd,5f"
                        new_line = f"{i} {display_sender} {path_str}\n"
                        
                        # Check if adding this line would exceed the limit
                        if len(response + new_line) > max_length:
                            break
                        
                        response += new_line
                else:
                    response = "No path data\n"
                
                return response
                
        except Exception as e:
            self.logger.error(f"Error getting path leaderboard: {e}")
            return f"Error getting path stats: {e}"
    
    def cleanup_old_stats(self, days_to_keep: int = 7):
        """Clean up old stats data to prevent database bloat"""
        try:
            cutoff_time = int(time.time()) - (days_to_keep * 24 * 60 * 60)
            
            with sqlite3.connect(self.bot.db_manager.db_path) as conn:
                cursor = conn.cursor()
                
                # Clean up old message stats
                cursor.execute('DELETE FROM message_stats WHERE timestamp < ?', (cutoff_time,))
                messages_deleted = cursor.rowcount
                
                # Clean up old command stats
                cursor.execute('DELETE FROM command_stats WHERE timestamp < ?', (cutoff_time,))
                commands_deleted = cursor.rowcount
                
                # Clean up old path stats
                cursor.execute('DELETE FROM path_stats WHERE timestamp < ?', (cutoff_time,))
                paths_deleted = cursor.rowcount
                
                conn.commit()
                
                total_deleted = messages_deleted + commands_deleted + paths_deleted
                if total_deleted > 0:
                    self.logger.info(f"Cleaned up {total_deleted} old stats entries ({messages_deleted} messages, {commands_deleted} commands, {paths_deleted} paths)")
                
        except Exception as e:
            self.logger.error(f"Error cleaning up old stats: {e}")
    
    def get_stats_summary(self) -> Dict[str, Any]:
        """Get a summary of all stats data"""
        try:
            with sqlite3.connect(self.bot.db_manager.db_path) as conn:
                cursor = conn.cursor()
                
                # Total messages
                cursor.execute('SELECT COUNT(*) FROM message_stats')
                total_messages = cursor.fetchone()[0]
                
                # Total commands
                cursor.execute('SELECT COUNT(*) FROM command_stats')
                total_commands = cursor.fetchone()[0]
                
                # Unique users
                cursor.execute('SELECT COUNT(DISTINCT sender_id) FROM message_stats')
                unique_users = cursor.fetchone()[0]
                
                # Unique channels
                cursor.execute('SELECT COUNT(DISTINCT channel) FROM message_stats WHERE channel IS NOT NULL')
                unique_channels = cursor.fetchone()[0]
                
                return {
                    'total_messages': total_messages,
                    'total_commands': total_commands,
                    'unique_users': unique_users,
                    'unique_channels': unique_channels
                }
                
        except Exception as e:
            self.logger.error(f"Error getting stats summary: {e}")
            return {}
