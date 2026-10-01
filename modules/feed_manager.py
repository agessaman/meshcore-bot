#!/usr/bin/env python3
"""
Feed Manager for RSS and API feed subscriptions
Handles polling feeds and sending updates to channels
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp
import feedparser

from modules.feed_filter_eval import (
    get_nested_value,
    item_passes_filter_config,
    parse_microsoft_date,
)
from modules.feed_format import (
    apply_feed_field_function,
    feed_format_auto_base_value,
    feed_format_auto_slots,
    format_feed_message,
    format_relative_timestamp,
    sort_feed_items,
    truncate_to_budget,
)
from modules.feed_parse import (
    api_item_fields,
    feed_allow_private_urls,
    feed_max_parsed_items,
    feed_max_response_bytes,
    rss_entry_published,
)
from modules.security_utils import (
    SafeAiohttpResolver,
    SafeUrlPolicy,
    UnsafeUrlError,
    safe_aiohttp_request,
)


def _useful_feed_content_type(content_type: str, feed_type: str) -> bool:
    """Accept common feed/API media types while rejecting clearly unrelated bodies."""
    media_type = content_type.partition(';')[0].strip().lower()
    if not media_type:
        return True
    shared_fallbacks = {'text/plain', 'application/octet-stream'}
    if media_type in shared_fallbacks:
        return True
    if feed_type == 'rss':
        return (
            media_type in {
                'application/rss+xml',
                'application/atom+xml',
                'application/xml',
                'text/xml',
                'application/xhtml+xml',
                'text/html',
            }
            or media_type.endswith('+xml')
        )
    return media_type in {'application/json', 'text/json'} or media_type.endswith('+json')


class FeedManager:
    """Manages RSS and API feed subscriptions"""

    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self.db_path = bot.db_manager.db_path

        # Configuration (guard against missing [Feed_Manager] section for upgrade compatibility)
        if not bot.config.has_section('Feed_Manager'):
            self.enabled = False
            self.default_check_interval = 300
            self.max_items_per_check = 10
            self.max_posts_per_check = 10
            self.request_timeout = 30
            self.user_agent = 'MeshCoreBot/1.0 FeedManager'
            self.rate_limit_seconds = 5.0
            self.max_message_length = 130
            self.default_output_format = '{emoji} {body|truncate:100} - {date}\n{link|truncate:50}'
            self.default_send_interval = 2.0
            self.max_response_bytes = feed_max_response_bytes(bot.config)
            self.max_parsed_items = feed_max_parsed_items(bot.config)
            self.shorten_feed_urls = False
            self.allow_private_urls = feed_allow_private_urls(bot.config)
        else:
            self.enabled = bot.config.getboolean('Feed_Manager', 'feed_manager_enabled', fallback=False)
            self.default_check_interval = bot.config.getint('Feed_Manager', 'default_check_interval_seconds', fallback=300)
            # Clamped to >= 1: the scan window feeds a list slice, where 0 disables
            # posting entirely and negatives take Python's negative-slice meaning
            # (drop the newest N items) — neither is what a config typo intends.
            self.max_items_per_check = max(
                1, bot.config.getint('Feed_Manager', 'max_items_per_check', fallback=10)
            )
            # Max items actually posted per check. Defaults to max_items_per_check so existing
            # installs behave identically; raise max_items_per_check (the scan window) to reach
            # older passing items while this caps how many post per poll. Also clamped to >= 1,
            # since the post loop only checks the cap after sending an item.
            self.max_posts_per_check = max(
                1,
                bot.config.getint(
                    'Feed_Manager', 'max_posts_per_check', fallback=self.max_items_per_check
                ),
            )
            # Clamped to >= 1: aiohttp.ClientTimeout only arms a timer for
            # total > 0, so 0 or negative disables the timeout entirely and a
            # hung feed server holds its poll lock and semaphore slot forever.
            self.request_timeout = max(
                1, bot.config.getint('Feed_Manager', 'feed_request_timeout', fallback=30)
            )
            self.user_agent = bot.config.get('Feed_Manager', 'feed_user_agent', fallback='MeshCoreBot/1.0 FeedManager')
            self.rate_limit_seconds = bot.config.getfloat('Feed_Manager', 'feed_rate_limit_seconds', fallback=5.0)
            # Clamped: the final truncation is `message[:max_message_length - 3]`,
            # so anything under 4 becomes a negative slice that lengthens the
            # message instead of capping it.
            self.max_message_length = max(
                10, bot.config.getint('Feed_Manager', 'max_message_length', fallback=130)
            )
            self.default_output_format = bot.config.get('Feed_Manager', 'default_output_format', fallback='{emoji} {body|truncate:100} - {date}\n{link|truncate:50}')
            self.default_send_interval = bot.config.getfloat('Feed_Manager', 'default_message_send_interval_seconds', fallback=2.0)
            self.max_response_bytes = feed_max_response_bytes(bot.config)
            self.max_parsed_items = feed_max_parsed_items(bot.config)
            self.shorten_feed_urls = bot.config.getboolean(
                'Feed_Manager', 'shorten_urls', fallback=False
            )
            self.allow_private_urls = feed_allow_private_urls(bot.config)

        # Rate limiting per domain
        self._domain_last_request: dict[str, float] = {}
        self._domain_rate_locks: dict[str, asyncio.Lock] = {}
        self._feed_poll_locks: dict[int, asyncio.Lock] = {}

        # HTTP session
        self.session: Optional[aiohttp.ClientSession] = None
        self._url_policy = SafeUrlPolicy(allow_private=self.allow_private_urls)

        # Semaphore to limit concurrent requests
        self._request_semaphore = asyncio.Semaphore(5)

        # Serialize process_message_queue; lock is checked before acquiring to avoid coroutine pileup
        self._process_queue_lock: Optional[asyncio.Lock] = None
        # Persisted across runs so per-feed send intervals are respected without sleeping under the lock
        self._feed_last_send: dict[int, float] = {}

        self.logger.info("FeedManager initialized")

    async def initialize(self):
        """Initialize the feed manager (create HTTP session)"""
        if not self.enabled:
            self.logger.info("FeedManager is disabled in config")
            return

        # Don't create session here - create it lazily when needed
        # This avoids issues with using sessions across different event loops
        # The session will be created in the same event loop where it's used
        self.logger.info("FeedManager initialized (session will be created on first use)")

    async def stop(self):
        """Stop the feed manager (close HTTP session)"""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
        self.logger.info("FeedManager stopped")

    async def poll_all_feeds(self):
        """Poll all enabled feeds that are due for checking"""
        if not self.enabled:
            return

        try:
            # Get all enabled feeds
            feeds = self._get_enabled_feeds()

            if not feeds:
                return

            # Filter feeds that are due for checking
            current_time = time.time()
            feeds_to_check = []

            for feed in feeds:
                last_check = feed.get('last_check_time')
                if last_check:
                    try:
                        # Parse timestamp - handle both ISO format and SQLite format
                        if isinstance(last_check, str):
                            # Try ISO format first (with timezone)
                            try:
                                last_check_dt = datetime.fromisoformat(last_check.replace('Z', '+00:00'))
                            except ValueError:
                                # Try SQLite format (YYYY-MM-DD HH:MM:SS) - treat as UTC
                                try:
                                    last_check_dt = datetime.strptime(last_check, '%Y-%m-%d %H:%M:%S')
                                    last_check_dt = last_check_dt.replace(tzinfo=timezone.utc)
                                except ValueError:
                                    # Try with microseconds
                                    try:
                                        last_check_dt = datetime.strptime(last_check, '%Y-%m-%d %H:%M:%S.%f')
                                        last_check_dt = last_check_dt.replace(tzinfo=timezone.utc)
                                    except ValueError:
                                        raise ValueError(f"Unknown timestamp format: {last_check}")
                        else:
                            last_check_dt = datetime.fromtimestamp(last_check, tz=timezone.utc)

                        # Convert to timestamp
                        if last_check_dt.tzinfo:
                            last_check_ts = last_check_dt.timestamp()
                        else:
                            # Assume UTC if no timezone
                            last_check_ts = last_check_dt.replace(tzinfo=timezone.utc).timestamp()
                    except Exception as e:
                        self.logger.debug(f"Error parsing last_check_time for feed {feed['id']}: {e}")
                        last_check_ts = 0
                else:
                    last_check_ts = 0

                # Fall back for rows that predate interval validation: NULL would
                # raise a TypeError below and abort the poll cycle for *every*
                # feed, and <= 0 marks the feed permanently due.
                interval = feed.get('check_interval_seconds')
                if not isinstance(interval, (int, float)) or interval <= 0:
                    interval = self.default_check_interval

                if current_time - last_check_ts >= interval:
                    feeds_to_check.append(feed)

            if not feeds_to_check:
                self.logger.debug("No feeds due for checking at this time")
                return

            self.logger.info(f"Polling {len(feeds_to_check)} feed(s) that are due for checking")

            # Poll feeds in parallel (with semaphore limit)
            tasks = [self.poll_feed(feed) for feed in feeds_to_check]
            await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            self.logger.error(f"Error in poll_all_feeds: {e}")

    async def _ensure_session(self):
        """Ensure HTTP session exists in the current event loop"""
        if self.session is None or self.session.closed:
            # Create session in the current event loop context
            self.session = aiohttp.ClientSession(
                headers={'User-Agent': self.user_agent},
                connector=aiohttp.TCPConnector(
                    resolver=SafeAiohttpResolver(self._url_policy),
                    use_dns_cache=False,
                ),
            )
            self.logger.debug("Created FeedManager HTTP session in current event loop")

    async def _read_limited_response(
        self,
        response: aiohttp.ClientResponse,
        feed_type: str,
    ) -> bytes:
        """Read a decompressed response body without exceeding the configured cap."""
        content_type = response.headers.get('Content-Type', '')
        if not _useful_feed_content_type(content_type, feed_type):
            raise ValueError(f"Unexpected {feed_type.upper()} content type: {content_type}")

        declared_length = response.headers.get('Content-Length')
        if declared_length:
            try:
                content_length = int(declared_length)
            except ValueError:
                content_length = None
            if content_length is not None and content_length > self.max_response_bytes:
                raise ValueError(
                    f"Feed response exceeds {self.max_response_bytes} byte limit"
                )

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > self.max_response_bytes:
                raise ValueError(
                    f"Feed response exceeds {self.max_response_bytes} byte limit"
                )
            chunks.append(chunk)
        return b''.join(chunks)

    async def poll_feed(self, feed: dict[str, Any]):
        """Poll a single feed, serializing concurrent attempts for that feed."""
        feed_id = int(feed['id'])
        lock = self._feed_poll_locks.setdefault(feed_id, asyncio.Lock())
        async with lock:
            await self._poll_feed_locked(feed)

    async def _poll_feed_locked(self, feed: dict[str, Any]) -> None:
        """Poll a feed while its per-feed exclusion lock is held."""
        # Ensure session exists in current event loop
        await self._ensure_session()

        feed_id = feed['id']
        feed_type = feed['feed_type']
        feed_url = feed['feed_url']

        try:
            # Validate URL for SSRF protection
            try:
                await self._url_policy.validate_async(feed_url)
            except UnsafeUrlError:
                self.logger.error(f"Feed URL validation failed: {feed_url}")
                self._record_feed_error(feed_id, 'security', 'Invalid or unsafe URL')
                return

            self.logger.debug(f"Polling {feed_type} feed {feed_id}: {feed_url}")

            # Rate limit per domain
            host = self._normalized_host(feed_url)
            await self._wait_for_rate_limit(host)

            # Fetch feed data
            if feed_type == 'rss':
                new_items = await self.process_rss_feed(feed)
            elif feed_type == 'api':
                new_items = await self.process_api_feed(feed)
            else:
                self.logger.warning(f"Unknown feed type: {feed_type}")
                return

            # Process new items.
            # Examine up to max_items_per_check items (the scan window) and post the ones that
            # pass the filter, stopping once max_posts_per_check items have been queued. This
            # keeps filtered-out items from consuming the post budget, so a long back-catalog
            # with a restrictive filter (e.g. within_days) doesn't stall behind old items.
            if new_items:
                self.logger.info(f"Found {len(new_items)} new items for feed {feed_id}")
                filtered_count = 0
                posted_count = 0
                for item in new_items[:self.max_items_per_check]:
                    # Budget checked before sending, not after: checking after meant
                    # a cap of 0 still sent one item. __init__ clamps the config
                    # value, but the attribute is public and set directly in tests
                    # and by callers, so guard the loop itself.
                    if posted_count >= self.max_posts_per_check:
                        break
                    # Check if item passes filter conditions
                    if self._should_send_item(feed, item):
                        await self._send_feed_item(feed, item)
                        posted_count += 1
                    else:
                        filtered_count += 1
                        self.logger.debug(f"Filtered out item: {item.get('title', 'Untitled')[:50]}")

                if filtered_count > 0:
                    self.logger.debug(f"Filtered out {filtered_count} items for feed {feed_id}")
            else:
                self.logger.debug(f"No new items found for feed {feed_id}")

            # Always update last check time, even if no new items
            self._update_feed_last_check(feed_id)

        except Exception as e:
            self.logger.error(f"Error polling feed {feed_id}: {e}")
            self._record_feed_error(feed_id, 'network', str(e))

    async def process_rss_feed(self, feed: dict[str, Any]) -> list[dict[str, Any]]:
        """Process an RSS feed and return new items"""
        feed_url = feed['feed_url']
        last_item_id = feed.get('last_item_id')

        try:
            # Fetch RSS feed - use aiohttp's timeout directly
            # Create timeout object in the current async context
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)

            async with self._request_semaphore:
                try:
                    assert self.session is not None
                    response = await safe_aiohttp_request(
                        self.session,
                        "GET",
                        feed_url,
                        policy=self._url_policy,
                        timeout=timeout,
                    )
                    try:
                        if response.status != 200:
                            raise Exception(f"HTTP {response.status}")
                        content = await self._read_limited_response(response, 'rss')
                    finally:
                        response.release()
                except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
                    raise Exception(f"Request timeout after {self.request_timeout} seconds")

            # Parse RSS feed
            parsed = feedparser.parse(content)

            if parsed.bozo:
                self.logger.warning(f"RSS feed parsing warning: {parsed.bozo_exception}")

            # Extract items - collect ALL items first (don't break early if sorting is configured)
            all_items = []
            for entry in parsed.entries[:self.max_parsed_items]:
                # Get item ID (prefer guid, then link, then hash of title+link)
                item_id = entry.get('id') or entry.get('guid') or entry.get('link')
                if not item_id:
                    # Generate ID from title and link
                    item_id = hashlib.md5(
                        f"{entry.get('title', '')}{entry.get('link', '')}".encode()
                    ).hexdigest()

                all_items.append({
                    'id': item_id,
                    'title': entry.get('title', 'Untitled'),
                    'link': entry.get('link', ''),
                    'description': entry.get('description', ''),
                    'published': rss_entry_published(entry),
                })

            # Apply sorting if configured (before filtering, so we can properly track the last item)
            sort_config_str = feed.get('sort_config')
            if sort_config_str:
                try:
                    sort_config = json.loads(sort_config_str) if isinstance(sort_config_str, str) else sort_config_str
                    all_items = self._sort_items(all_items, sort_config)
                except (json.JSONDecodeError, TypeError, Exception) as e:
                    self.logger.warning(f"Error applying sort config for feed {feed['id']}: {e}")

            # Reverse to get oldest first (if no sort config)
            if not sort_config_str:
                all_items.reverse()

            # Now filter out items that have already been processed
            # Check against both last_item_id and the feed_activity table for robust deduplication
            items = []
            processed_item_ids = set()

            # Get all previously processed item IDs from feed_activity table
            if last_item_id:
                processed_item_ids.add(last_item_id)

            # Query database for all processed item IDs for this feed
            try:
                with self.bot.db_manager.connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        SELECT item_id FROM feed_activity WHERE feed_id = ?
                        UNION
                        SELECT item_id FROM feed_message_queue
                        WHERE feed_id = ? AND sent_at IS NULL
                          AND item_id IS NOT NULL AND trim(item_id) <> ''
                    ''', (feed['id'], feed['id']))
                    for row in cursor.fetchall():
                        processed_item_ids.add(row[0])
            except Exception as e:
                self.logger.warning(f"Error querying processed items for feed {feed['id']}: {e}")

            # Filter out already processed items
            for item in all_items:
                if item['id'] not in processed_item_ids:
                    items.append(item)
                else:
                    self.logger.debug(f"Skipping already processed item {item['id']} for feed {feed['id']}")

            # Update last_item_id if we have new items (use the last item from the sorted list)
            if items:
                # Use the last item from the original sorted list (all_items), not the filtered list
                # This ensures we track the most recent item even if it was already processed
                self._update_feed_last_item_id(feed['id'], all_items[-1]['id'])

            return items

        except Exception as e:
            self.logger.error(f"Error processing RSS feed: {e}")
            raise

    async def process_api_feed(self, feed: dict[str, Any]) -> list[dict[str, Any]]:
        """Process an API feed and return new items"""
        feed_url = feed['feed_url']
        api_config_str = feed.get('api_config', '{}')
        last_item_id = feed.get('last_item_id')

        try:
            # Parse API config
            api_config = json.loads(api_config_str) if api_config_str else {}

            method = api_config.get('method', 'GET').upper()
            headers = api_config.get('headers', {})
            params = api_config.get('params', {})
            body = api_config.get('body')
            parser_config = api_config.get('response_parser', {})

            # Make HTTP request - use aiohttp's timeout directly
            # Create timeout object in the current async context
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)

            async with self._request_semaphore:
                try:
                    assert self.session is not None
                    response = await safe_aiohttp_request(
                        self.session,
                        method,
                        feed_url,
                        policy=self._url_policy,
                        headers=headers,
                        params=params,
                        json=body if method == 'POST' else None,
                        timeout=timeout,
                    )
                    try:
                        if response.status != 200:
                            raise Exception(f"HTTP {response.status}")
                        content = await self._read_limited_response(response, 'api')
                        data = json.loads(content)
                    finally:
                        response.release()
                except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
                    raise Exception(f"Request timeout after {self.request_timeout} seconds")

            # Extract items using parser config
            items_path = parser_config.get('items_path', '')
            if items_path:
                # Navigate JSON path
                parts = items_path.split('.')
                items_data = data
                for part in parts:
                    items_data = items_data.get(part, [])
            else:
                # Assume data is a list
                items_data = data if isinstance(data, list) else [data]

            # Extract items
            id_field = parser_config.get('id_field', 'id')
            # title/description/timestamp/emoji fields are read by api_item_fields

            # Collect ALL items first (don't break early, as sorting may reorder them)
            all_items = []
            if not isinstance(items_data, list):
                items_data = [items_data]
            for item_data in items_data[:self.max_parsed_items]:
                if not isinstance(item_data, dict):
                    continue
                item_id = str(self._get_nested_value(item_data, id_field, ''))
                if not item_id:
                    continue

                all_items.append({'id': item_id, **api_item_fields(item_data, parser_config)})

            # Apply sorting if configured (before filtering, so we can properly track the last item)
            sort_config_str = feed.get('sort_config')
            if sort_config_str:
                try:
                    sort_config = json.loads(sort_config_str) if isinstance(sort_config_str, str) else sort_config_str
                    all_items = self._sort_items(all_items, sort_config)
                except (json.JSONDecodeError, TypeError, Exception) as e:
                    self.logger.warning(f"Error applying sort config for feed {feed['id']}: {e}")

            # Reverse to get oldest first (if no sort config)
            if not sort_config_str:
                all_items.reverse()

            # Now filter out items that have already been processed
            # Check against both last_item_id and the feed_activity table for robust deduplication
            items = []
            processed_item_ids = set()

            # Get all previously processed item IDs from feed_activity table
            if last_item_id:
                processed_item_ids.add(last_item_id)

            # Query database for all processed item IDs for this feed
            try:
                with self.bot.db_manager.connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        SELECT item_id FROM feed_activity WHERE feed_id = ?
                        UNION
                        SELECT item_id FROM feed_message_queue
                        WHERE feed_id = ? AND sent_at IS NULL
                          AND item_id IS NOT NULL AND trim(item_id) <> ''
                    ''', (feed['id'], feed['id']))
                    for row in cursor.fetchall():
                        processed_item_ids.add(row[0])
            except Exception as e:
                self.logger.warning(f"Error querying processed items for feed {feed['id']}: {e}")

            # Filter out already processed items
            for item in all_items:
                if item['id'] not in processed_item_ids:
                    items.append(item)
                else:
                    self.logger.debug(f"Skipping already processed item {item['id']} for feed {feed['id']}")

            # Update last_item_id if we have new items (use the last item from the sorted list)
            if items:
                # Use the last item from the original sorted list (all_items), not the filtered list
                # This ensures we track the most recent item even if it was already processed
                self._update_feed_last_item_id(feed['id'], all_items[-1]['id'])

            return items

        except Exception as e:
            self.logger.error(f"Error processing API feed: {e}")
            raise

    def _format_timestamp(self, published: Optional[datetime]) -> str:
        """Format a timestamp as a relative time string"""
        return format_relative_timestamp(published)

    @staticmethod
    def _feed_format_auto_slots(format_str: str) -> list[tuple[int, int, str]]:
        """Return (start, end, field_name) for each {field|auto} placeholder (left-to-right)."""
        return feed_format_auto_slots(format_str)

    @staticmethod
    def _truncate_to_budget(text: str, budget: int) -> str:
        """Fit text to at most budget characters; ellipsis when budget > 3 (same idea as truncate:N)."""
        return truncate_to_budget(text, budget)

    def _feed_format_auto_base_value(
        self,
        field_name: str,
        raw_data: Any,
        replacements: dict[str, str],
        link_original: str,
    ) -> str:
        """Full string for one field before |auto (long link, no shorten)."""
        return feed_format_auto_base_value(
            field_name, raw_data, replacements, link_original
        )

    def _apply_shortening(self, text: str, function: str) -> str:
        """Apply a shortening, parsing, or conditional function to text."""
        return apply_feed_field_function(
            text, function, config=self.bot.config, logger=self.logger
        )

    def _get_nested_value(self, data: Any, path: str, default: Any = '') -> Any:
        """Get a nested value from a dict/list using dot notation."""
        return get_nested_value(data, path, default)

    def _parse_microsoft_date(self, date_str: str) -> Optional[datetime]:
        """Parse Microsoft JSON date format: /Date(timestamp-offset)/"""
        return parse_microsoft_date(date_str)

    def _sort_items(self, items: list[dict[str, Any]], sort_config: dict) -> list[dict[str, Any]]:
        """Sort items based on sort configuration."""
        return sort_feed_items(
            items, sort_config, log_warning=self.logger.warning
        )

    def format_message(self, item: dict[str, Any], feed: dict[str, Any]) -> str:
        """Format a feed item as a message for the mesh using configurable format with placeholders.

        Supported placeholders and field functions are documented on
        modules.feed_format.format_feed_message.
        """
        format_str = feed.get('output_format') or self.default_output_format
        return format_feed_message(
            item,
            format_str,
            feed_name=feed.get('feed_name') or '',
            feed_id=feed.get('id'),
            max_message_length=self.max_message_length,
            shorten_feed_urls=self.shorten_feed_urls,
            config=self.bot.config,
            logger=self.logger,
        )

    def _queue_feed_message(
        self,
        feed: dict[str, Any],
        item: dict[str, Any],
        message: str,
    ) -> bool:
        """Queue a feed message, returning whether a new row was inserted."""
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO feed_message_queue
                    (feed_id, channel_name, message, item_id, item_title, priority)
                    VALUES (?, ?, ?, ?, ?, 0)
                    ON CONFLICT(feed_id, item_id)
                    WHERE item_id IS NOT NULL AND trim(item_id) <> ''
                    DO NOTHING
                ''', (
                    feed['id'],
                    feed['channel_name'],
                    message,
                    item.get('id', ''),
                    item.get('title', '')[:200]  # Limit title length
                ))
                conn.commit()
                inserted = cursor.rowcount == 1
                if inserted:
                    self.logger.debug(f"Queued feed message for {feed['channel_name']}: {item.get('title', '')[:50]}")
                else:
                    self.logger.debug(
                        f"Skipped duplicate queued item {item.get('id', '')!r} "
                        f"for feed {feed['id']}"
                    )
                return inserted
        except Exception as e:
            self.logger.error(f"Error queuing feed message: {e}")
            self._record_feed_error(feed['id'], 'queue', str(e))
            return False

    def _should_send_item(self, feed: dict[str, Any], item: dict[str, Any]) -> bool:
        """Check if an item should be sent based on filter configuration.

        See modules/feed_filter_eval.py and docs/FEEDS.md for operators.
        """
        def _warn(msg: str) -> None:
            self.logger.warning(f"{msg} (feed id {feed.get('id')})")

        return item_passes_filter_config(
            item,
            feed.get('filter_config'),
            log_warning=_warn,
        )

    async def _send_feed_item(self, feed: dict[str, Any], item: dict[str, Any]):
        """Queue a feed item message instead of sending immediately"""
        try:
            message = self.format_message(item, feed)
            # Queue the message instead of sending immediately
            self._queue_feed_message(feed, item, message)
        except Exception as e:
            self.logger.error(f"Error processing feed item: {e}")
            self._record_feed_error(feed['id'], 'other', str(e))

    @staticmethod
    def _normalized_host(url: str) -> str:
        """Return a stable host key independent of case, port, or trailing dot."""
        host = (urlparse(url).hostname or '').rstrip('.').lower()
        try:
            return host.encode('idna').decode('ascii')
        except UnicodeError:
            return host

    async def _wait_for_rate_limit(self, host: str):
        """Serialize and space requests to the same normalized host."""
        lock = self._domain_rate_locks.setdefault(host, asyncio.Lock())
        async with lock:
            last_request = self._domain_last_request.get(host)
            if last_request is not None:
                elapsed = time.monotonic() - last_request
                if elapsed < self.rate_limit_seconds:
                    await asyncio.sleep(self.rate_limit_seconds - elapsed)
            self._domain_last_request[host] = time.monotonic()

    def _get_enabled_feeds(self) -> list[dict[str, Any]]:
        """Get all enabled feed subscriptions from database"""
        try:
            with self.bot.db_manager.connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT * FROM feed_subscriptions
                    WHERE enabled = 1
                    ORDER BY last_check_time ASC NULLS FIRST
                ''')
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Error getting enabled feeds: {e}")
            return []

    def _update_feed_last_check(self, feed_id: int):
        """Update the last check time for a feed"""
        try:
            from datetime import datetime, timezone
            # Use Python's datetime to ensure proper timezone handling
            # Store in ISO format with timezone for JavaScript compatibility
            now = datetime.now(timezone.utc)
            now_str = now.isoformat()  # ISO format: 2025-12-05T12:34:56.789+00:00

            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE feed_subscriptions
                    SET last_check_time = ?,
                        updated_at = ?
                    WHERE id = ?
                ''', (now_str, now_str, feed_id))
                conn.commit()
                self.logger.debug(f"Updated last_check_time for feed {feed_id} to {now_str}")
        except Exception as e:
            self.logger.error(f"Error updating feed last check: {e}")

    def _update_feed_last_item_id(self, feed_id: int, item_id: str):
        """Update the last processed item ID for a feed"""
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE feed_subscriptions
                    SET last_item_id = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (item_id, feed_id))
                conn.commit()
        except Exception as e:
            self.logger.error(f"Error updating feed last item ID: {e}")

    def _record_feed_activity(self, feed_id: int, item_id: str, item_title: str):
        """Record that a feed item was processed"""
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO feed_activity (feed_id, item_id, item_title, message_sent)
                    VALUES (?, ?, ?, 1)
                ''', (feed_id, item_id, item_title[:200]))  # Limit title length
                conn.commit()
        except Exception as e:
            self.logger.error(f"Error recording feed activity: {e}")

    def _record_feed_error(self, feed_id: int, error_type: str, error_message: str):
        """Record a feed error"""
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO feed_errors (feed_id, error_type, error_message)
                    VALUES (?, ?, ?)
                ''', (feed_id, error_type, error_message[:500]))  # Limit message length
                conn.commit()
        except Exception as e:
            self.logger.error(f"Error recording feed error: {e}")

    async def process_message_queue(self):
        """Process queued feed messages and send them at configured intervals"""
        if self._process_queue_lock is None:
            self._process_queue_lock = asyncio.Lock()
        if self._process_queue_lock.locked():
            return  # previous run still in progress; skip this tick
        async with self._process_queue_lock:
            await self._process_message_queue_inner()

    async def _process_message_queue_inner(self):
        """Body of process_message_queue (runs under _process_queue_lock)."""
        try:
            # Get all unsent messages, ordered by priority and queue time
            db_path = str(self.db_path)  # Ensure string, not Path object
            with self.bot.db_manager.connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT q.id, q.feed_id, q.channel_name, q.message, q.item_id, q.item_title,
                           f.message_send_interval_seconds
                    FROM feed_message_queue q
                    JOIN feed_subscriptions f ON q.feed_id = f.id
                    WHERE q.sent_at IS NULL
                    ORDER BY q.priority DESC, q.queued_at ASC
                    LIMIT 100
                ''')
                messages = cursor.fetchall()

            if not messages:
                return

            for msg in messages:
                feed_id = msg['feed_id']
                channel_name = msg['channel_name']
                message_text = msg['message']
                queue_id = msg['id']
                item_id = msg['item_id']
                item_title = msg['item_title']

                # Get send interval for this feed (default if not set)
                send_interval = msg['message_send_interval_seconds'] or self.default_send_interval

                # Skip messages whose feed interval hasn't elapsed yet; the next
                # scheduler tick (2 s) will retry without blocking under the lock.
                if feed_id in self._feed_last_send:
                    elapsed = time.time() - self._feed_last_send[feed_id]
                    if elapsed < send_interval:
                        continue

                # Send the message
                try:
                    success = await self.bot.command_manager.send_channel_message(channel_name, message_text)

                    if success:
                        # Mark as sent
                        with self.bot.db_manager.connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute('''
                                UPDATE feed_message_queue
                                SET sent_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                            ''', (queue_id,))
                            conn.commit()

                        # Record activity
                        self._record_feed_activity(feed_id, item_id, item_title)
                        self.logger.debug(f"Sent queued feed message to {channel_name}: {item_title[:50]}")
                        self._feed_last_send[feed_id] = time.time()
                    else:
                        self.logger.warning(f"Failed to send queued feed message to channel {channel_name}")
                        self._record_feed_error(feed_id, 'channel', f"Failed to send to channel {channel_name}")
                        # Don't mark as sent, will retry later

                except Exception as e:
                    self.logger.error(f"Error sending queued feed message: {e}")
                    self._record_feed_error(feed_id, 'other', str(e))
                    # Don't mark as sent, will retry later

        except Exception as e:
            db_path = getattr(self, 'db_path', 'unknown')
            db_path_str = str(db_path) if db_path != 'unknown' else 'unknown'
            self.logger.exception(f"Error processing message queue: {e}")
            if db_path_str != 'unknown':
                path_obj = Path(db_path_str)
                self.logger.error(f"Database path: {db_path_str} (exists: {path_obj.exists()}, readable: {os.access(db_path_str, os.R_OK) if path_obj.exists() else False}, writable: {os.access(db_path_str, os.W_OK) if path_obj.exists() else False})")
                # Check parent directory permissions
                if path_obj.exists():
                    parent = path_obj.parent
                    self.logger.error(f"Parent directory: {parent} (exists: {parent.exists()}, writable: {os.access(str(parent), os.W_OK) if parent.exists() else False})")
            else:
                self.logger.error(f"Database path: {db_path_str}")
