#!/usr/bin/env python3
"""
Feed command for the MeshCore Bot
Handles RSS and API feed subscription management
"""

import json
import re
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse
from .base_command import BaseCommand
from ..models import MeshMessage


class FeedCommand(BaseCommand):
    """Handles feed subscription management"""
    
    # Plugin metadata
    name = "feed"
    keywords = ['feed', 'feeds', 'rss', 'subscription', 'subscriptions']
    description = "Manage RSS and API feed subscriptions (usage: feed subscribe rss <url> <channel> [name])"
    category = "admin"
    requires_dm = True
    cooldown_seconds = 2
    requires_internet = True  # Requires internet access for RSS/API feed fetching
    
    def __init__(self, bot):
        super().__init__(bot)
        self.db_path = bot.db_manager.db_path
        self.feed_enabled = self.get_config_value('Feed_Command', 'enabled', fallback=True, value_type='bool')
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed (enabled, admin only)"""
        if not self.feed_enabled:
            return False
        if not self.requires_admin_access():
            return False
        return super().can_execute(message)
    
    def requires_admin_access(self) -> bool:
        """Feed command requires admin access"""
        return True
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the feed command"""
        content = message.content.strip()
        
        # Handle exclamation prefix
        if content.startswith('!'):
            content = content[1:].strip()
        
        # Parse command
        parts = content.split()
        if len(parts) < 2:
            return await self.send_response(message, self.get_help_text())
        
        subcommand = parts[1].lower()
        
        if subcommand == 'subscribe':
            return await self._handle_subscribe(message, parts[2:])
        elif subcommand == 'unsubscribe':
            return await self._handle_unsubscribe(message, parts[2:])
        elif subcommand == 'list':
            return await self._handle_list(message, parts[2:])
        elif subcommand == 'status':
            return await self._handle_status(message, parts[2:])
        elif subcommand == 'test':
            return await self._handle_test(message, parts[2:])
        elif subcommand == 'enable':
            return await self._handle_enable_disable(message, parts[2:], True)
        elif subcommand == 'disable':
            return await self._handle_enable_disable(message, parts[2:], False)
        elif subcommand == 'update':
            return await self._handle_update(message, parts[2:])
        else:
            return await self.send_response(message, self.get_help_text())
    
    def get_help_text(self) -> str:
        """Get help text for feed command"""
        return """Feed Command Usage:
feed subscribe <rss|api> <url> <channel> [name]
feed unsubscribe <id|url> <channel>
feed list [channel]
feed status <id>
feed test <url>
feed enable <id>
feed disable <id>
feed update <id> [interval_seconds]

Examples:
feed subscribe rss https://alerts.example.com/rss emergency "Emergency Alerts"
feed subscribe api https://api.example.com/alerts emergency "API Alerts" '{"headers": {"Authorization": "Bearer TOKEN"}}'
feed list
feed status 1"""
    
    async def _handle_subscribe(self, message: MeshMessage, args: List[str]) -> bool:
        """Handle feed subscribe command"""
        if len(args) < 3:
            return await self.send_response(message, "Usage: feed subscribe <rss|api> <url> <channel> [name] [api_config]")
        
        feed_type = args[0].lower()
        if feed_type not in ['rss', 'api']:
            return await self.send_response(message, "Feed type must be 'rss' or 'api'")
        
        feed_url = args[1]
        channel_name = args[2]
        feed_name = args[3] if len(args) > 3 else None
        api_config = args[4] if len(args) > 4 and feed_type == 'api' else None
        
        # Validate URL
        if not self._validate_url(feed_url):
            return await self.send_response(message, "Invalid URL format")
        
        # Validate channel exists
        channel_num = self.bot.channel_manager.get_channel_number(channel_name)
        if channel_num is None:
            return await self.send_response(message, f"Channel '{channel_name}' not found. Create it first or use a valid channel name.")
        
        # Parse API config if provided
        api_config_json = None
        if feed_type == 'api' and api_config:
            try:
                api_config_json = json.loads(api_config)
            except json.JSONDecodeError:
                return await self.send_response(message, "Invalid API config JSON")
        
        # Create subscription
        try:
            feed_id = self._create_subscription(
                feed_type=feed_type,
                feed_url=feed_url,
                channel_name=channel_name,
                feed_name=feed_name,
                api_config=api_config_json
            )
            
            response = f"Subscribed to {feed_type.upper()} feed"
            if feed_name:
                response += f" '{feed_name}'"
            response += f" -> channel: {channel_name} (ID: {feed_id})"
            return await self.send_response(message, response)
        
        except Exception as e:
            self.logger.error(f"Error creating subscription: {e}")
            return await self.send_response(message, f"Error creating subscription: {str(e)}")
    
    async def _handle_unsubscribe(self, message: MeshMessage, args: List[str]) -> bool:
        """Handle feed unsubscribe command"""
        if len(args) < 1:
            return await self.send_response(message, "Usage: feed unsubscribe <id|url> [channel]")
        
        identifier = args[0]
        channel_name = args[1] if len(args) > 1 else None
        
        try:
            # Try as ID first
            try:
                feed_id = int(identifier)
                success = self._delete_subscription_by_id(feed_id)
            except ValueError:
                # Try as URL
                if channel_name:
                    success = self._delete_subscription_by_url(identifier, channel_name)
                else:
                    return await self.send_response(message, "Channel name required when using URL")
            
            if success:
                return await self.send_response(message, f"Unsubscribed from feed (ID: {identifier})")
            else:
                return await self.send_response(message, "Feed subscription not found")
        
        except Exception as e:
            self.logger.error(f"Error unsubscribing: {e}")
            return await self.send_response(message, f"Error unsubscribing: {str(e)}")
    
    async def _handle_list(self, message: MeshMessage, args: List[str]) -> bool:
        """Handle feed list command"""
        channel_filter = args[0] if args else None
        
        try:
            feeds = self._get_subscriptions(channel_filter)
            
            if not feeds:
                response = "No feed subscriptions"
                if channel_filter:
                    response += f" for channel '{channel_filter}'"
                return await self.send_response(message, response)
            
            response = f"Feed Subscriptions ({len(feeds)}):\n"
            for feed in feeds[:10]:  # Limit to 10 for mesh message
                status = "enabled" if feed['enabled'] else "disabled"
                name = feed.get('feed_name') or feed['feed_url'][:30]
                response += f"{feed['id']}. {name} ({feed['feed_type']}) -> {feed['channel_name']} [{status}]\n"
            
            if len(feeds) > 10:
                response += f"({len(feeds) - 10} more...)"
            
            return await self.send_response(message, response)
        
        except Exception as e:
            self.logger.error(f"Error listing feeds: {e}")
            return await self.send_response(message, f"Error listing feeds: {str(e)}")
    
    async def _handle_status(self, message: MeshMessage, args: List[str]) -> bool:
        """Handle feed status command"""
        if not args:
            return await self.send_response(message, "Usage: feed status <id>")
        
        try:
            feed_id = int(args[0])
            feed = self._get_subscription_by_id(feed_id)
            
            if not feed:
                return await self.send_response(message, f"Feed subscription {feed_id} not found")
            
            status = "enabled" if feed['enabled'] else "disabled"
            last_check = feed.get('last_check_time') or "Never"
            last_item = feed.get('last_item_id') or "None"
            
            response = f"Feed {feed_id} Status:\n"
            response += f"Name: {feed.get('feed_name') or 'N/A'}\n"
            response += f"Type: {feed['feed_type']}\n"
            response += f"URL: {feed['feed_url']}\n"
            response += f"Channel: {feed['channel_name']}\n"
            response += f"Status: {status}\n"
            response += f"Interval: {feed.get('check_interval_seconds', 300)}s\n"
            response += f"Last check: {last_check}\n"
            response += f"Last item: {last_item[:30] if last_item != 'None' else 'None'}"
            
            return await self.send_response(message, response)
        
        except ValueError:
            return await self.send_response(message, "Invalid feed ID")
        except Exception as e:
            self.logger.error(f"Error getting feed status: {e}")
            return await self.send_response(message, f"Error getting feed status: {str(e)}")
    
    async def _handle_test(self, message: MeshMessage, args: List[str]) -> bool:
        """Handle feed test command"""
        if not args:
            return await self.send_response(message, "Usage: feed test <url>")
        
        feed_url = args[0]
        
        if not self._validate_url(feed_url):
            return await self.send_response(message, "Invalid URL format")
        
        # Test would require feed_manager to be available
        # For now, just validate URL
        return await self.send_response(message, f"URL validated: {feed_url}\n(Full test requires feed manager)")
    
    async def _handle_enable_disable(self, message: MeshMessage, args: List[str], enable: bool) -> bool:
        """Handle enable/disable command"""
        if not args:
            return await self.send_response(message, f"Usage: feed {'enable' if enable else 'disable'} <id>")
        
        try:
            feed_id = int(args[0])
            success = self._set_subscription_enabled(feed_id, enable)
            
            if success:
                status = "enabled" if enable else "disabled"
                return await self.send_response(message, f"Feed {feed_id} {status}")
            else:
                return await self.send_response(message, f"Feed subscription {feed_id} not found")
        
        except ValueError:
            return await self.send_response(message, "Invalid feed ID")
        except Exception as e:
            self.logger.error(f"Error setting feed status: {e}")
            return await self.send_response(message, f"Error: {str(e)}")
    
    async def _handle_update(self, message: MeshMessage, args: List[str]) -> bool:
        """Handle update command"""
        if not args:
            return await self.send_response(message, "Usage: feed update <id> [interval_seconds]")
        
        try:
            feed_id = int(args[0])
            interval = int(args[1]) if len(args) > 1 else None
            
            success = self._update_subscription(feed_id, interval)
            
            if success:
                response = f"Feed {feed_id} updated"
                if interval:
                    response += f" (interval: {interval}s)"
                return await self.send_response(message, response)
            else:
                return await self.send_response(message, f"Feed subscription {feed_id} not found")
        
        except ValueError:
            return await self.send_response(message, "Invalid feed ID or interval")
        except Exception as e:
            self.logger.error(f"Error updating feed: {e}")
            return await self.send_response(message, f"Error: {str(e)}")
    
    def _validate_url(self, url: str) -> bool:
        """Validate URL format"""
        try:
            result = urlparse(url)
            return all([result.scheme in ['http', 'https'], result.netloc])
        except Exception:
            return False
    
    def _create_subscription(self, feed_type: str, feed_url: str, channel_name: str,
                            feed_name: Optional[str] = None, api_config: Optional[Dict] = None) -> int:
        """Create a new feed subscription"""
        import sqlite3
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Get default check interval
            default_interval = self.bot.config.getint('Feed_Manager', 'default_check_interval_seconds', fallback=300)
            
            api_config_str = json.dumps(api_config) if api_config else None
            
            cursor.execute('''
                INSERT INTO feed_subscriptions 
                (feed_type, feed_url, channel_name, feed_name, check_interval_seconds, api_config)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (feed_type, feed_url, channel_name, feed_name, default_interval, api_config_str))
            
            conn.commit()
            return cursor.lastrowid
    
    def _delete_subscription_by_id(self, feed_id: int) -> bool:
        """Delete subscription by ID"""
        import sqlite3
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM feed_subscriptions WHERE id = ?', (feed_id,))
            conn.commit()
            return cursor.rowcount > 0
    
    def _delete_subscription_by_url(self, feed_url: str, channel_name: str) -> bool:
        """Delete subscription by URL and channel"""
        import sqlite3
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM feed_subscriptions 
                WHERE feed_url = ? AND channel_name = ?
            ''', (feed_url, channel_name))
            conn.commit()
            return cursor.rowcount > 0
    
    def _get_subscriptions(self, channel_filter: Optional[str] = None) -> List[Dict]:
        """Get all subscriptions, optionally filtered by channel"""
        import sqlite3
        
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            if channel_filter:
                cursor.execute('''
                    SELECT * FROM feed_subscriptions
                    WHERE channel_name = ?
                    ORDER BY id
                ''', (channel_filter,))
            else:
                cursor.execute('''
                    SELECT * FROM feed_subscriptions
                    ORDER BY id
                ''')
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def _get_subscription_by_id(self, feed_id: int) -> Optional[Dict]:
        """Get subscription by ID"""
        import sqlite3
        
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM feed_subscriptions WHERE id = ?', (feed_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def _set_subscription_enabled(self, feed_id: int, enabled: bool) -> bool:
        """Enable or disable a subscription"""
        import sqlite3
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE feed_subscriptions
                SET enabled = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (1 if enabled else 0, feed_id))
            conn.commit()
            return cursor.rowcount > 0
    
    def _update_subscription(self, feed_id: int, interval: Optional[int] = None) -> bool:
        """Update subscription settings"""
        import sqlite3
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            if interval:
                cursor.execute('''
                    UPDATE feed_subscriptions
                    SET check_interval_seconds = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (interval, feed_id))
            else:
                cursor.execute('''
                    UPDATE feed_subscriptions
                    SET updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (feed_id,))
            
            conn.commit()
            return cursor.rowcount > 0

