"""Feed subscription storage, preview and formatting for the web viewer.

Mixed into BotDataViewer, which supplies ``config``, ``logger``,
``_get_db_connection`` and ``_db_connection``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any

from modules.feed_filter_eval import item_passes_filter_config
from modules.feed_format import format_feed_message, sort_feed_items
from modules.feed_manager import _useful_feed_content_type
from modules.feed_parse import (
    api_item_fields,
    feed_allow_private_urls,
    feed_max_parsed_items,
    feed_max_response_bytes,
    rss_entry_published,
)
from modules.security_utils import SafeUrlPolicy, create_safe_requests_session, safe_requests_request


def _validate_feed_interval(raw: Any) -> int:
    """Coerce a feed poll interval, rejecting values that break the poller.

    feed_manager compares ``current_time - last_check >= interval``: anything
    <= 0 leaves every feed permanently due and hammers the source URL, and a
    None (JSON ``null``) raises a TypeError that aborts the whole poll cycle for
    every feed, not just this one.
    """
    try:
        interval = int(raw)
    except (TypeError, ValueError):
        raise ValueError("check_interval_seconds must be a positive integer")
    if interval <= 0:
        raise ValueError("check_interval_seconds must be a positive integer")
    return interval


def _read_limited_requests_response(
    response: Any,
    *,
    max_bytes: int,
    feed_type: str,
) -> bytes:
    """Read a streamed, decompressed requests response under a hard byte cap."""
    response.raise_for_status()
    content_type = response.headers.get('Content-Type', '')
    if not _useful_feed_content_type(content_type, feed_type):
        raise ValueError(f"Unexpected {feed_type.upper()} content type: {content_type}")

    declared_length = response.headers.get('Content-Length')
    if declared_length:
        try:
            content_length = int(declared_length)
        except ValueError:
            content_length = None
        if content_length is not None and content_length > max_bytes:
            raise ValueError(f"Feed response exceeds {max_bytes} byte limit")

    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024, decode_unicode=False):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"Feed response exceeds {max_bytes} byte limit")
        chunks.append(chunk)
    return b''.join(chunks)


class FeedSubscriptionsMixin:
    """Feed subscription CRUD, activity/error queries, preview and item formatting."""

    _db_connection: Any
    _get_db_connection: Any
    config: Any
    logger: Any

    def _get_feed_subscriptions(self, channel_filter=None):
        """Get all feed subscriptions, optionally filtered by channel"""
        try:
            with self._db_connection() as conn:
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
                feeds = []
                for row in rows:
                    feed = dict(row)
                    # Get feed count for this channel
                    cursor.execute('''
                    SELECT COUNT(*) FROM feed_activity
                    WHERE feed_id = ?
                ''', (feed['id'],))
                    feed['item_count'] = cursor.fetchone()[0]

                    # Get error count
                    cursor.execute('''
                    SELECT COUNT(*) FROM feed_errors
                    WHERE feed_id = ? AND resolved_at IS NULL
                ''', (feed['id'],))
                    feed['error_count'] = cursor.fetchone()[0]

                    feeds.append(feed)

                return {'feeds': feeds, 'total': len(feeds)}
        except Exception as e:
            self.logger.error(f"Error getting feed subscriptions: {e}")
            return {'feeds': [], 'total': 0, 'error': str(e)}

    def _get_feed_subscription(self, feed_id):
        """Get a single feed subscription by ID"""
        try:
            with self._db_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM feed_subscriptions WHERE id = ?', (feed_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            self.logger.error(f"Error getting feed subscription: {e}")
            return None

    def _create_feed_subscription(self, data):
        """Create a new feed subscription"""
        import json
        conn = None
        try:
            feed_type = data.get('feed_type')
            feed_url = data.get('feed_url')
            channel_name = data.get('channel_name')
            feed_name = data.get('feed_name')
            check_interval = _validate_feed_interval(data.get('check_interval_seconds', 300))
            api_config = data.get('api_config')
            output_format = data.get('output_format')
            message_send_interval = data.get('message_send_interval_seconds')
            filter_config = data.get('filter_config')
            sort_config = data.get('sort_config')

            if not all([feed_type, feed_url, channel_name]):
                raise ValueError("feed_type, feed_url, and channel_name are required")

            conn = self._get_db_connection()
            cursor = conn.cursor()

            api_config_str = json.dumps(api_config) if api_config else None
            filter_config_str = json.dumps(filter_config) if filter_config else None
            sort_config_str = json.dumps(sort_config) if sort_config else None

            cursor.execute('''
                INSERT INTO feed_subscriptions
                (feed_type, feed_url, channel_name, feed_name, check_interval_seconds, api_config, output_format, message_send_interval_seconds, filter_config, sort_config)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (feed_type, feed_url, channel_name, feed_name, check_interval, api_config_str, output_format, message_send_interval, filter_config_str, sort_config_str))

            conn.commit()
            return cursor.lastrowid
        except Exception:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    def _update_feed_subscription(self, feed_id, data):
        """Update a feed subscription"""
        import json
        conn = None
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()

            updates = []
            params: list[Any] = []

            if 'channel_name' in data:
                channel_name = str(data['channel_name']).strip() if data['channel_name'] is not None else ''
                if not channel_name:
                    raise ValueError("channel_name cannot be empty")
                updates.append('channel_name = ?')
                params.append(channel_name)

            if 'feed_name' in data:
                updates.append('feed_name = ?')
                params.append(data['feed_name'])

            if 'check_interval_seconds' in data:
                updates.append('check_interval_seconds = ?')
                params.append(_validate_feed_interval(data['check_interval_seconds']))

            if 'enabled' in data:
                updates.append('enabled = ?')
                params.append(1 if data['enabled'] else 0)

            if 'api_config' in data:
                updates.append('api_config = ?')
                params.append(json.dumps(data['api_config']) if data['api_config'] else None)

            if 'output_format' in data:
                updates.append('output_format = ?')
                params.append(data['output_format'] if data['output_format'] else None)

            if 'message_send_interval_seconds' in data:
                updates.append('message_send_interval_seconds = ?')
                params.append(float(data['message_send_interval_seconds']) if data['message_send_interval_seconds'] else None)

            if 'filter_config' in data:
                updates.append('filter_config = ?')
                params.append(json.dumps(data['filter_config']) if data['filter_config'] else None)

            if 'sort_config' in data:
                updates.append('sort_config = ?')
                params.append(json.dumps(data['sort_config']) if data['sort_config'] else None)

            if not updates:
                return True  # Nothing to update

            updates.append('updated_at = CURRENT_TIMESTAMP')
            params.append(feed_id)

            query = f'UPDATE feed_subscriptions SET {", ".join(updates)} WHERE id = ?'
            cursor.execute(query, params)
            conn.commit()

            return cursor.rowcount > 0
        except Exception:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    def _delete_feed_subscription(self, feed_id):
        """Delete a feed subscription"""
        conn = None
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM feed_subscriptions WHERE id = ?', (feed_id,))
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    def _reset_feed_errors(self, feed_id=None):
        """Clear recorded feed errors. Pass a feed_id to clear one feed, or None for all.

        Returns the number of error rows deleted.
        """
        conn = None
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            if feed_id is None:
                cursor.execute('DELETE FROM feed_errors')
            else:
                cursor.execute('DELETE FROM feed_errors WHERE feed_id = ?', (feed_id,))
            conn.commit()
            return cursor.rowcount
        except Exception:
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    def _get_feed_activity(self, feed_id, limit=50):
        """Get activity log for a feed"""
        try:
            with self._db_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('''
                SELECT * FROM feed_activity
                WHERE feed_id = ?
                ORDER BY processed_at DESC
                LIMIT ?
            ''', (feed_id, limit))
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Error getting feed activity: {e}")
            return []

    def _get_feed_errors(self, feed_id, limit=20):
        """Get error history for a feed"""
        try:
            with self._db_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('''
                SELECT * FROM feed_errors
                WHERE feed_id = ?
                ORDER BY occurred_at DESC
                LIMIT ?
            ''', (feed_id, limit))
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Error getting feed errors: {e}")
            return []

    def _get_feed_statistics(self):
        """Get aggregate feed statistics"""
        try:
            with self._db_connection() as conn:
                cursor = conn.cursor()

                stats = {}

                # Total subscriptions
                cursor.execute('SELECT COUNT(*) FROM feed_subscriptions')
                stats['total_subscriptions'] = cursor.fetchone()[0]

                # Enabled subscriptions
                cursor.execute('SELECT COUNT(*) FROM feed_subscriptions WHERE enabled = 1')
                stats['enabled_subscriptions'] = cursor.fetchone()[0]

                # Items processed in last 24h
                cursor.execute('''
                SELECT COUNT(*) FROM feed_activity
                WHERE processed_at > datetime('now', '-24 hours')
            ''')
                stats['items_24h'] = cursor.fetchone()[0]

                # Items processed in last 7d
                cursor.execute('''
                SELECT COUNT(*) FROM feed_activity
                WHERE processed_at > datetime('now', '-7 days')
            ''')
                stats['items_7d'] = cursor.fetchone()[0]

                # Error count
                cursor.execute('''
                SELECT COUNT(*) FROM feed_errors
                WHERE resolved_at IS NULL
            ''')
                stats['active_errors'] = cursor.fetchone()[0]

                # Most active channels
                cursor.execute('''
                SELECT channel_name, COUNT(*) as feed_count
                FROM feed_subscriptions
                WHERE enabled = 1
                GROUP BY channel_name
                ORDER BY feed_count DESC
                LIMIT 10
            ''')
                stats['top_channels'] = [{'channel': row[0], 'count': row[1]} for row in cursor.fetchall()]

                return stats
        except Exception as e:
            self.logger.error(f"Error getting feed statistics: {e}")
            return {'error': str(e)}

    def _get_feeds_by_channel(self, channel_idx):
        """Get all feeds for a specific channel index"""
        # First get channel name from index
        # This would require channel_manager access
        # For now, return empty list
        return []

    def _preview_feed_items(self, feed_url: str, feed_type: str, output_format: str, api_config: dict[str, Any] | None = None, filter_config: dict[str, Any] | None = None, sort_config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Preview feed items with custom output format (standalone, doesn't require bot)"""

        import feedparser

        try:
            items = []

            # Validate URL for SSRF protection
            url_policy = SafeUrlPolicy(allow_private=feed_allow_private_urls(self.config))
            if not url_policy.validate(feed_url):
                raise ValueError("Invalid or unsafe feed URL")
            max_response_bytes = feed_max_response_bytes(self.config)
            max_parsed_items = feed_max_parsed_items(self.config)
            preview_parse_limit = min(20, max_parsed_items)

            if feed_type == 'rss':
                # Fetch RSS feed
                with create_safe_requests_session(url_policy) as http_session:
                    response = safe_requests_request(
                        http_session,
                        'GET',
                        feed_url,
                        policy=url_policy,
                        timeout=30,
                        headers={'User-Agent': 'MeshCoreBot/1.0 FeedManager'},
                        stream=True,
                    )
                    with closing(response):
                        content = _read_limited_requests_response(
                            response,
                            max_bytes=max_response_bytes,
                            feed_type='rss',
                        )
                parsed = feedparser.parse(content)

                # Get items (we'll filter and limit later)
                for entry in parsed.entries[:preview_parse_limit]:
                    items.append({
                        'title': entry.get('title', 'Untitled'),
                        'description': entry.get('description', ''),
                        'link': entry.get('link', ''),
                        'published': rss_entry_published(entry),
                    })

            elif feed_type == 'api':
                # Fetch API feed
                if api_config is None:
                    raise ValueError("api_config is required for API feed type")
                method = api_config.get('method', 'GET').upper()
                headers = api_config.get('headers', {})
                params = api_config.get('params', {})
                body = api_config.get('body')
                parser_config = api_config.get('response_parser', {})

                with create_safe_requests_session(url_policy) as http_session:
                    response = safe_requests_request(
                        http_session,
                        method,
                        feed_url,
                        policy=url_policy,
                        headers=headers,
                        params=params,
                        json=body if method == 'POST' else None,
                        timeout=30,
                        stream=True,
                    )
                    with closing(response):
                        content = _read_limited_requests_response(
                            response,
                            max_bytes=max_response_bytes,
                            feed_type='api',
                        )

                # Try to parse JSON, handle cases where response might be a string
                try:
                    data = json.loads(content)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # If JSON parsing fails, try to get text and see if it's an error message
                    text_snippet = content[:200].decode('utf-8', errors='replace')
                    raise Exception(f"API returned non-JSON response: {text_snippet[:200]}")

                # Check if response is an error message (string)
                if isinstance(data, str):
                    raise Exception(f"API returned error message: {data[:200]}")

                # Ensure data is a dict or list
                if not isinstance(data, (dict, list)):
                    raise Exception(f"API response is not a valid JSON object or array: {type(data).__name__} - {str(data)[:200]}")

                # Extract items using parser config
                items_path = parser_config.get('items_path', '')
                items_data: dict[Any, Any] | list[Any]
                if items_path:
                    parts = items_path.split('.')
                    items_data = data
                    for part in parts:
                        if isinstance(items_data, dict):
                            items_data = items_data.get(part, [])
                        else:
                            raise Exception(f"Cannot navigate path '{items_path}': expected dict at '{part}', got {type(items_data).__name__}")
                else:
                    # If no items_path, data should be a list or we wrap it
                    if isinstance(data, list):
                        items_data = data
                    elif isinstance(data, dict):
                        # If it's a dict, try to find common array fields
                        _found = data.get('items') or data.get('data') or data.get('results')
                        items_data = _found if _found is not None else [data]
                    else:
                        items_data = [data]

                # Ensure items_data is a list
                if not isinstance(items_data, list):
                    items_data = [items_data]

                for item_data in items_data[:preview_parse_limit]:
                    # Ensure item_data is a dict
                    if not isinstance(item_data, dict):
                        # If it's not a dict, try to convert or skip
                        if isinstance(item_data, str):
                            # If it's a string, create a simple dict
                            item_data = {'title': item_data, 'description': item_data}
                        else:
                            # Try to convert to dict or skip
                            continue

                    items.append(api_item_fields(item_data, parser_config))

            # Apply sorting if configured
            if sort_config:
                items = sort_feed_items(items, sort_config, log_warning=self.logger.warning)

            # Apply filter if configured
            if filter_config:
                items = [item for item in items if item_passes_filter_config(item, filter_config)]

            # Limit to first 3 items after filtering
            items = items[:3]

            # Format items using output format (shared with FeedManager)
            formatted_items = []
            for item in items:
                formatted = self._format_feed_item(item, output_format, feed_name='')
                formatted_items.append({
                    'original': item,
                    'formatted': formatted
                })

            return formatted_items

        except Exception as e:
            self.logger.error(f"Error previewing feed: {e}")
            raise

    def _format_feed_item(self, item: dict[str, Any], format_str: str, feed_name: str = '') -> str:
        """Format a feed item using the shared feed formatter (parity with FeedManager)."""
        try:
            max_length = self.config.getint(
                'Feed_Manager', 'max_message_length', fallback=130
            )
        except Exception:
            max_length = 130
        try:
            shorten_feed_urls = (
                self.config.getboolean('Feed_Manager', 'shorten_urls', fallback=False)
                if self.config.has_section('Feed_Manager')
                else False
            )
        except ValueError:
            shorten_feed_urls = False

        return format_feed_message(
            item,
            format_str,
            feed_name=feed_name or '',
            max_message_length=max_length,
            shorten_feed_urls=shorten_feed_urls,
            config=self.config,
            logger=self.logger,
        )
