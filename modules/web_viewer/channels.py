"""Channel listing, statistics and add/remove operations for the web viewer."""

from __future__ import annotations

import sqlite3
from typing import Any


class ChannelAdminMixin:
    """Mixed into BotDataViewer."""

    _db_connection: Any
    _get_db_connection: Any
    config: Any
    logger: Any

    def _get_channels(self):
        """Get all configured channels from database plus additional decode-only channels"""
        try:
            with self._db_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute('''
                SELECT channel_idx, channel_name, channel_type, channel_key_hex, last_updated
                FROM channels
                ORDER BY channel_idx
            ''')

                rows = cursor.fetchall()
                channels = []
                existing_names = set()

                for row in rows:
                    name = row['channel_name']
                    channels.append({
                        'channel_idx': row['channel_idx'],
                        'index': row['channel_idx'],  # Alias for compatibility
                        'name': name,
                        'channel_name': name,  # Alias for compatibility
                        'type': row['channel_type'] or 'hashtag',
                        'key_hex': row['channel_key_hex'],
                        'last_updated': row['last_updated']
                    })
                    # Track names for deduplication (normalize to lowercase with #)
                    normalized = name.lower() if name.startswith('#') else f'#{name.lower()}'
                    existing_names.add(normalized)

                # Add additional decode-only hashtag channels from config
                additional_channels = self._get_additional_decode_channels()
                for channel_name in additional_channels:
                    # Normalize name
                    normalized = channel_name.lower() if channel_name.startswith('#') else f'#{channel_name.lower()}'
                    if normalized not in existing_names:
                        channels.append({
                            'channel_idx': None,  # Not a real radio channel
                            'index': None,
                            'name': normalized,
                            'channel_name': normalized,
                            'type': 'hashtag',
                            'key_hex': None,  # Key will be derived client-side
                            'last_updated': None,
                            'decode_only': True  # Flag to indicate this is decode-only
                        })
                        existing_names.add(normalized)

                return channels
        except Exception as e:
            self.logger.error(f"Error getting channels: {e}")
            return []

    def _get_additional_decode_channels(self):
        """Get additional hashtag channels to decode from config"""
        channels = set()  # Use set for automatic deduplication

        try:
            # 1. Get channels from decode_hashtag_channels in [Web_Viewer]
            if self.config and self.config.has_option('Web_Viewer', 'decode_hashtag_channels'):
                channels_str = self.config.get('Web_Viewer', 'decode_hashtag_channels', fallback='')
                if channels_str:
                    for c in channels_str.split(','):
                        c = c.strip().lower()
                        if c:
                            # Remove # prefix if present for normalization
                            if c.startswith('#'):
                                c = c[1:]
                            channels.add(c)

            # 2. Import channels from [Channels_List] section
            if self.config and self.config.has_section('Channels_List'):
                for key in self.config.options('Channels_List'):
                    # Handle categorized channels like "sports.sounders" -> "sounders"
                    if '.' in key:
                        channel_name = key.split('.')[-1]  # Get part after last dot
                    else:
                        channel_name = key

                    channel_name = channel_name.strip().lower()
                    if channel_name:
                        channels.add(channel_name)
        except Exception as e:
            self.logger.error(f"Error reading decode channels config: {e}")

        return list(channels)

    def _get_channel_number(self, channel_name):
        """Get channel number from channel name"""
        # This would use channel_manager
        # For now, return None
        return None

    def _get_lowest_available_channel_index(self):
        """Get the lowest available channel index (0 to max_channels-1)"""
        try:
            channels = self._get_channels()
            used_indices = {c['channel_idx'] for c in channels}

            # Get max_channels from config (default 40)
            max_channels = self.config.getint('Bot', 'max_channels', fallback=40)

            # Find the lowest available index
            for i in range(max_channels):
                if i not in used_indices:
                    return i

            # All channels are used
            return None
        except Exception as e:
            self.logger.error(f"Error getting lowest available channel index: {e}")
            return None

    def _get_channel_statistics(self):
        """Get channel statistics"""
        try:
            with self._db_connection() as conn:
                cursor = conn.cursor()

                # Get feed count per channel
                cursor.execute('''
                SELECT channel_name, COUNT(*) as feed_count
                FROM feed_subscriptions
                WHERE enabled = 1
                GROUP BY channel_name
            ''')

                channel_feeds = {row[0]: row[1] for row in cursor.fetchall()}

                # Get max_channels from config (default 40)
                max_channels = self.config.getint('Bot', 'max_channels', fallback=40)

                return {
                    'channels_with_feeds': len(channel_feeds),
                    'channel_feed_counts': channel_feeds,
                    'max_channels': max_channels
                }
        except Exception as e:
            self.logger.error(f"Error getting channel statistics: {e}")
            return {'error': str(e)}

    def _add_channel_for_web(self, channel_idx, channel_name, channel_key_hex=None):
        """
        Add a channel by queuing it in the database for the bot to process

        Args:
            channel_idx: Channel index (0-39)
            channel_name: Channel name (with or without # prefix)
            channel_key_hex: Optional hex key for custom channels (32 chars)

        Returns:
            dict with 'success' and optional 'error' key
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()

            # Insert operation into queue
            cursor.execute('''
                INSERT INTO channel_operations
                (operation_type, channel_idx, channel_name, channel_key_hex, status)
                VALUES (?, ?, ?, ?, 'pending')
            ''', ('add', channel_idx, channel_name, channel_key_hex))

            operation_id = cursor.lastrowid
            conn.commit()
            conn.close()

            self.logger.info(f"Queued channel add operation: {channel_name} at index {channel_idx} (operation_id: {operation_id})")

            # Return immediately with operation_id - let frontend poll for status
            return {
                'success': True,
                'pending': True,
                'operation_id': operation_id,
                'message': 'Channel operation queued successfully'
            }

        except Exception as e:
            self.logger.error(f"Error in _add_channel_for_web: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def _remove_channel_for_web(self, channel_idx):
        """
        Remove a channel by queuing it in the database for the bot to process

        Args:
            channel_idx: Channel index to remove

        Returns:
            dict with 'success' and optional 'error' key
        """
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()

            # Insert operation into queue
            cursor.execute('''
                INSERT INTO channel_operations
                (operation_type, channel_idx, status)
                VALUES (?, ?, 'pending')
            ''', ('remove', channel_idx))

            operation_id = cursor.lastrowid
            conn.commit()
            conn.close()

            self.logger.info(f"Queued channel remove operation: index {channel_idx} (operation_id: {operation_id})")

            # Return immediately with operation_id - let frontend poll for status
            return {
                'success': True,
                'pending': True,
                'operation_id': operation_id,
                'message': 'Channel operation queued successfully'
            }

        except Exception as e:
            self.logger.error(f"Error in _remove_channel_for_web: {e}")
            return {
                'success': False,
                'error': str(e)
            }
