#!/usr/bin/env python3
"""
Feed Manager for RSS and API feed subscriptions
Handles polling feeds and sending updates to channels
"""

import asyncio
import aiohttp
import json
import time
import hashlib
import html
import re
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path
import sqlite3
import feedparser
from urllib.parse import urlparse


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
            self.request_timeout = 30
            self.user_agent = 'MeshCoreBot/1.0 FeedManager'
            self.rate_limit_seconds = 5.0
            self.max_message_length = 130
            self.default_output_format = '{emoji} {body|truncate:100} - {date}\n{link|truncate:50}'
            self.default_send_interval = 2.0
        else:
            self.enabled = bot.config.getboolean('Feed_Manager', 'feed_manager_enabled', fallback=False)
            self.default_check_interval = bot.config.getint('Feed_Manager', 'default_check_interval_seconds', fallback=300)
            self.max_items_per_check = bot.config.getint('Feed_Manager', 'max_items_per_check', fallback=10)
            self.request_timeout = bot.config.getint('Feed_Manager', 'feed_request_timeout', fallback=30)
            self.user_agent = bot.config.get('Feed_Manager', 'feed_user_agent', fallback='MeshCoreBot/1.0 FeedManager')
            self.rate_limit_seconds = bot.config.getfloat('Feed_Manager', 'feed_rate_limit_seconds', fallback=5.0)
            self.max_message_length = bot.config.getint('Feed_Manager', 'max_message_length', fallback=130)
            self.default_output_format = bot.config.get('Feed_Manager', 'default_output_format', fallback='{emoji} {body|truncate:100} - {date}\n{link|truncate:50}')
            self.default_send_interval = bot.config.getfloat('Feed_Manager', 'default_message_send_interval_seconds', fallback=2.0)
        
        # Rate limiting per domain
        self._domain_last_request: Dict[str, float] = {}
        
        # HTTP session
        self.session: Optional[aiohttp.ClientSession] = None
        
        # Semaphore to limit concurrent requests
        self._request_semaphore = asyncio.Semaphore(5)
        
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
                
                interval = feed.get('check_interval_seconds', self.default_check_interval)
                
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
                headers={'User-Agent': self.user_agent}
            )
            self.logger.debug("Created FeedManager HTTP session in current event loop")
    
    async def poll_feed(self, feed: Dict[str, Any]):
        """Poll a single feed and process new items"""
        # Ensure session exists in current event loop
        await self._ensure_session()
        
        feed_id = feed['id']
        feed_type = feed['feed_type']
        feed_url = feed['feed_url']
        channel_name = feed['channel_name']
        
        try:
            self.logger.debug(f"Polling {feed_type} feed {feed_id}: {feed_url}")
            
            # Rate limit per domain
            domain = urlparse(feed_url).netloc
            await self._wait_for_rate_limit(domain)
            
            # Fetch feed data
            if feed_type == 'rss':
                new_items = await self.process_rss_feed(feed)
            elif feed_type == 'api':
                new_items = await self.process_api_feed(feed)
            else:
                self.logger.warning(f"Unknown feed type: {feed_type}")
                return
            
            # Process new items
            if new_items:
                self.logger.info(f"Found {len(new_items)} new items for feed {feed_id}")
                filtered_count = 0
                for item in new_items[:self.max_items_per_check]:
                    # Check if item passes filter conditions
                    if self._should_send_item(feed, item):
                        await self._send_feed_item(feed, item)
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
    
    async def process_rss_feed(self, feed: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Process an RSS feed and return new items"""
        feed_url = feed['feed_url']
        last_item_id = feed.get('last_item_id')
        
        try:
            # Fetch RSS feed - use aiohttp's timeout directly
            # Create timeout object in the current async context
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            
            async with self._request_semaphore:
                try:
                    async with self.session.get(feed_url, timeout=timeout) as response:
                        if response.status != 200:
                            raise Exception(f"HTTP {response.status}")
                        content = await response.text()
                except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
                    raise Exception(f"Request timeout after {self.request_timeout} seconds")
            
            # Parse RSS feed
            parsed = feedparser.parse(content)
            
            if parsed.bozo:
                self.logger.warning(f"RSS feed parsing warning: {parsed.bozo_exception}")
            
            # Extract items - collect ALL items first (don't break early if sorting is configured)
            all_items = []
            for entry in parsed.entries:
                # Get item ID (prefer guid, then link, then hash of title+link)
                item_id = entry.get('id') or entry.get('guid') or entry.get('link')
                if not item_id:
                    # Generate ID from title and link
                    item_id = hashlib.md5(
                        f"{entry.get('title', '')}{entry.get('link', '')}".encode()
                    ).hexdigest()
                
                # Parse published date
                published = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    try:
                        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    except Exception:
                        pass
                
                all_items.append({
                    'id': item_id,
                    'title': entry.get('title', 'Untitled'),
                    'link': entry.get('link', ''),
                    'description': entry.get('description', ''),
                    'published': published
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
                with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        SELECT DISTINCT item_id FROM feed_activity
                        WHERE feed_id = ?
                    ''', (feed['id'],))
                    for row in cursor.fetchall():
                        processed_item_ids.add(row[0])
            except Exception as e:
                self.logger.debug(f"Error querying processed items for feed {feed['id']}: {e}")
            
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
    
    async def process_api_feed(self, feed: Dict[str, Any]) -> List[Dict[str, Any]]:
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
                    if method == 'POST':
                        async with self.session.post(feed_url, headers=headers, params=params, json=body, timeout=timeout) as response:
                            if response.status != 200:
                                raise Exception(f"HTTP {response.status}")
                            data = await response.json()
                    else:
                        async with self.session.get(feed_url, headers=headers, params=params, timeout=timeout) as response:
                            if response.status != 200:
                                raise Exception(f"HTTP {response.status}")
                            data = await response.json()
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
            title_field = parser_config.get('title_field', 'title')
            description_field = parser_config.get('description_field', 'description')  # New: allow custom description field
            timestamp_field = parser_config.get('timestamp_field', 'created_at')
            
            # Collect ALL items first (don't break early, as sorting may reorder them)
            all_items = []
            for item_data in items_data:
                item_id = str(self._get_nested_value(item_data, id_field, ''))
                if not item_id:
                    continue
                
                # Parse timestamp if available - support nested paths
                published = None
                if timestamp_field:
                    ts_value = self._get_nested_value(item_data, timestamp_field)
                    if ts_value:
                        try:
                            if isinstance(ts_value, (int, float)):
                                published = datetime.fromtimestamp(ts_value, tz=timezone.utc)
                            elif isinstance(ts_value, str):
                                # Try Microsoft date format first
                                if ts_value.startswith('/Date('):
                                    published = self._parse_microsoft_date(ts_value)
                                else:
                                    # Try ISO format
                                    try:
                                        published = datetime.fromisoformat(ts_value.replace('Z', '+00:00'))
                                    except ValueError:
                                        # Try common formats
                                        for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d']:
                                            try:
                                                published = datetime.strptime(ts_value, fmt)
                                                if published.tzinfo is None:
                                                    published = published.replace(tzinfo=timezone.utc)
                                                break
                                            except ValueError:
                                                continue
                        except Exception:
                            pass
                
                # Get description - support nested paths
                description = ''
                if description_field:
                    desc_value = self._get_nested_value(item_data, description_field)
                    if desc_value:
                        description = str(desc_value)
                
                all_items.append({
                    'id': item_id,
                    'title': self._get_nested_value(item_data, title_field, 'Untitled'),
                    'link': item_data.get('link', ''),
                    'description': description,
                    'published': published,
                    'raw': item_data  # Store full raw response for field access
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
                with sqlite3.connect(str(self.db_path), timeout=30.0) as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        SELECT DISTINCT item_id FROM feed_activity
                        WHERE feed_id = ?
                    ''', (feed['id'],))
                    for row in cursor.fetchall():
                        processed_item_ids.add(row[0])
            except Exception as e:
                self.logger.debug(f"Error querying processed items for feed {feed['id']}: {e}")
            
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
        if not published:
            return ""
        
        try:
            if published.tzinfo:
                now = datetime.now(timezone.utc)
            else:
                now = datetime.now()
            
            diff = now - published
            minutes = int(diff.total_seconds() / 60)
            
            if minutes < 1:
                return "now"
            elif minutes < 60:
                return f"{minutes}m ago"
            elif minutes < 1440:
                hours = minutes // 60
                mins = minutes % 60
                return f"{hours}h {mins}m ago"
            else:
                days = minutes // 1440
                return f"{days}d ago"
        except Exception:
            return ""
    
    def _apply_shortening(self, text: str, function: str) -> str:
        """Apply a shortening, parsing, or conditional function to text
        
        Supported functions:
        - truncate:N - truncate to N characters
        - word_wrap:N - wrap at N characters, breaking at word boundaries
        - first_words:N - take first N words
        - regex:pattern - extract using regex pattern (uses first capture group, or whole match)
        - regex:pattern:group - extract specific capture group (0 = whole match, 1 = first group, etc.)
        - if_regex:pattern:then:else - if pattern matches, return "then", else return "else"
        """
        if not text:
            return ""
        
        if function.startswith('truncate:'):
            try:
                max_len = int(function.split(':', 1)[1])
                if len(text) <= max_len:
                    return text
                return text[:max_len] + "..."
            except (ValueError, IndexError):
                return text
        
        elif function.startswith('word_wrap:'):
            try:
                max_len = int(function.split(':', 1)[1])
                if len(text) <= max_len:
                    return text
                # Find last space before max_len
                truncated = text[:max_len]
                last_space = truncated.rfind(' ')
                if last_space > max_len * 0.7:  # Only use word boundary if it's not too short
                    return truncated[:last_space] + "..."
                return truncated + "..."
            except (ValueError, IndexError):
                return text
        
        elif function.startswith('first_words:'):
            try:
                num_words = int(function.split(':', 1)[1])
                words = text.split()
                if len(words) <= num_words:
                    return text
                return ' '.join(words[:num_words]) + "..."
            except (ValueError, IndexError):
                return text
        
        elif function.startswith('regex:'):
            try:
                # Parse regex pattern and optional group number
                # Format: regex:pattern:group or regex:pattern
                # Need to handle patterns that contain colons, so split from the right
                remaining = function[6:]  # Skip 'regex:' prefix
                
                # Try to find the last colon that's followed by a number (the group number)
                # Look for pattern like :N at the end
                last_colon_idx = remaining.rfind(':')
                pattern = remaining
                group_num = None
                
                if last_colon_idx > 0:
                    # Check if what's after the last colon is a number
                    potential_group = remaining[last_colon_idx + 1:]
                    if potential_group.isdigit():
                        pattern = remaining[:last_colon_idx]
                        group_num = int(potential_group)
                
                if not pattern:
                    return text
                
                # Apply regex
                match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
                if match:
                    if group_num is not None:
                        # Use specified group (0 = whole match, 1 = first group, etc.)
                        if 0 <= group_num <= len(match.groups()):
                            return match.group(group_num) if group_num > 0 else match.group(0)
                    else:
                        # Use first capture group if available, otherwise whole match
                        if match.groups():
                            return match.group(1)
                        else:
                            return match.group(0)
                return ""  # No match found
            except (ValueError, IndexError, re.error) as e:
                self.logger.debug(f"Error applying regex function: {e}")
                return text
        
        elif function.startswith('if_regex:'):
            try:
                # Parse: if_regex:pattern:then:else
                # Split by ':' but need to handle regex patterns that contain ':'
                # Use a smarter split that respects the structure
                parts = function[9:].split(':', 2)  # Skip 'if_regex:' prefix, split into [pattern, then, else]
                if len(parts) < 3:
                    return text
                
                pattern = parts[0]
                then_value = parts[1]
                else_value = parts[2]
                
                if not pattern:
                    return text
                
                # Check if pattern matches
                match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
                if match:
                    return then_value
                else:
                    return else_value
            except (ValueError, IndexError, re.error) as e:
                self.logger.debug(f"Error applying if_regex function: {e}")
                return text
        
        elif function.startswith('switch:'):
            try:
                # Parse: switch:value1:result1:value2:result2:...:default
                # Example: switch:highest:🔴:high:🟠:medium:🟡:low:⚪:⚪
                # This checks if text exactly matches value1, returns result1, etc., or default
                parts = function[7:].split(':')  # Skip 'switch:' prefix
                if len(parts) < 2:
                    return text
                
                # Pairs of value:result, last one is default
                text_lower = text.lower().strip()
                for i in range(0, len(parts) - 1, 2):
                    if i + 1 < len(parts):
                        value = parts[i].lower()
                        result = parts[i + 1]
                        if text_lower == value:
                            return result
                
                # Return last part as default if no match
                return parts[-1] if parts else text
            except (ValueError, IndexError) as e:
                self.logger.debug(f"Error applying switch function: {e}")
                return text
        
        elif function.startswith('regex_cond:'):
            try:
                # Parse: regex_cond:extract_pattern:check_pattern:then:group
                # This extracts text using extract_pattern, then checks if it matches check_pattern
                # If check_pattern matches, return "then", else return the extracted text
                # Example: regex_cond:Northbound\s*\n([^\n]+):No restrictions:👍:1
                # This extracts text after "Northbound\n" up to next newline, checks if it's "No restrictions",
                # if yes returns "👍", else returns the extracted text
                parts = function[11:].split(':', 3)  # Skip 'regex_cond:' prefix
                if len(parts) < 4:
                    return text
                
                extract_pattern = parts[0]
                check_pattern = parts[1]
                then_value = parts[2]
                else_group = int(parts[3]) if parts[3].isdigit() else 1
                
                if not extract_pattern:
                    return text
                
                # Extract using extract_pattern
                match = re.search(extract_pattern, text, re.IGNORECASE | re.DOTALL)
                if match:
                    # Get the captured group
                    if match.groups():
                        extracted = match.group(else_group) if else_group <= len(match.groups()) else match.group(1)
                        # Strip whitespace from extracted text
                        extracted = extracted.strip()
                    else:
                        extracted = match.group(0).strip()
                    
                    # Check if extracted text matches check_pattern (exact match or contains)
                    if check_pattern:
                        # Try exact match first, then substring match
                        if extracted.lower() == check_pattern.lower() or re.search(check_pattern, extracted, re.IGNORECASE):
                            return then_value
                    
                    return extracted
                return ""  # No match found
            except (ValueError, IndexError, re.error) as e:
                self.logger.debug(f"Error applying regex_cond function: {e}")
                return text
        
        return text
    
    def _get_nested_value(self, data: Any, path: str, default: Any = '') -> Any:
        """Get a nested value from a dict/list using dot notation (e.g., 'raw.Priority' or 'raw.StartRoadwayLocation.RoadName')"""
        if not path or not data:
            return default
        
        parts = path.split('.')
        value = data
        
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list):
                try:
                    idx = int(part)
                    if 0 <= idx < len(value):
                        value = value[idx]
                    else:
                        return default
                except (ValueError, TypeError):
                    return default
            else:
                return default
            
            if value is None:
                return default
        
        return value if value is not None else default
    
    def _parse_microsoft_date(self, date_str: str) -> Optional[datetime]:
        """Parse Microsoft JSON date format: /Date(timestamp-offset)/"""
        if not date_str or not isinstance(date_str, str):
            return None
        
        # Match /Date(timestamp-offset)/ format
        match = re.match(r'/Date\((\d+)([+-]\d+)?\)/', date_str)
        if match:
            timestamp_ms = int(match.group(1))
            offset_str = match.group(2) if match.group(2) else '+0000'
            
            # Convert milliseconds to seconds
            timestamp = timestamp_ms / 1000.0
            
            # Parse offset (format: +0800 or -0800)
            try:
                offset_hours = int(offset_str[:3])
                offset_mins = int(offset_str[3:5])
                offset_seconds = (offset_hours * 3600) + (offset_mins * 60)
                if offset_str[0] == '-':
                    offset_seconds = -offset_seconds
                
                # Create timezone-aware datetime
                tz = timezone.utc
                if offset_seconds != 0:
                    from datetime import timedelta
                    tz = timezone(timedelta(seconds=offset_seconds))
                
                return datetime.fromtimestamp(timestamp, tz=tz)
            except (ValueError, IndexError):
                # Fallback to UTC if offset parsing fails
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        
        return None
    
    def _sort_items(self, items: List[Dict[str, Any]], sort_config: dict) -> List[Dict[str, Any]]:
        """Sort items based on sort configuration
        
        Sort config format:
        {
            "field": "raw.LastUpdatedTime",  # Field path to sort by
            "order": "desc"  # "asc" or "desc"
        }
        """
        if not sort_config or not items:
            return items
        
        field_path = sort_config.get('field')
        order = sort_config.get('order', 'desc').lower()
        
        if not field_path:
            return items
        
        def get_sort_value(item):
            """Get the sort value for an item"""
            # Try raw data first
            raw_data = item.get('raw', {})
            value = self._get_nested_value(raw_data, field_path, '')
            
            if not value and field_path.startswith('raw.'):
                value = self._get_nested_value(raw_data, field_path[4:], '')
            
            if not value:
                value = self._get_nested_value(item, field_path, '')
            
            # Handle Microsoft date format
            if isinstance(value, str) and value.startswith('/Date('):
                dt = self._parse_microsoft_date(value)
                if dt:
                    return dt.timestamp()
            
            # Handle datetime objects
            if isinstance(value, datetime):
                return value.timestamp()
            
            # Handle numeric values
            if isinstance(value, (int, float)):
                return float(value)
            
            # Handle string timestamps
            if isinstance(value, str):
                # Try to parse as ISO format
                try:
                    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    return dt.timestamp()
                except ValueError:
                    pass
                
                # Try common date formats
                for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d']:
                    try:
                        dt = datetime.strptime(value, fmt)
                        return dt.timestamp()
                    except ValueError:
                        continue
            
            # For strings, use lexicographic comparison
            return str(value)
        
        # Sort items
        try:
            sorted_items = sorted(items, key=get_sort_value, reverse=(order == 'desc'))
            return sorted_items
        except Exception as e:
            self.logger.warning(f"Error sorting items: {e}")
            return items
    
    def format_message(self, item: Dict[str, Any], feed: Dict[str, Any]) -> str:
        """Format a feed item as a message for the mesh using configurable format with placeholders
        
        Supported placeholders:
        - {title} - item title
        - {body} - item description/body
        - {date} - relative time (e.g., "5m ago")
        - {link} - item link URL
        - {emoji} - emoji based on feed type
        - {raw.field} - access any field from raw API response (e.g., {raw.Priority}, {raw.StartRoadwayLocation.RoadName})
        
        Supported shortening functions:
        - {field|truncate:N} - truncate to N characters
        - {field|word_wrap:N} - wrap at N characters
        - {field|first_words:N} - take first N words
        - {field|regex:pattern} - extract using regex (first group or whole match)
        - {field|regex:pattern:group} - extract specific capture group
        - {field|if_regex:pattern:then:else} - if pattern matches, return "then", else "else"
        - {field|switch:value1:result1:value2:result2:...:default} - exact match switch (e.g., switch:highest:🔴:high:🟠:medium:🟡:⚪)
        - {field|regex_cond:extract_pattern:check_pattern:then:group} - extract text, check if it matches check_pattern, return "then" if match else extracted text
        """
        
        # Get format string from feed config or use default
        format_str = feed.get('output_format') or self.default_output_format
        
        # Extract field values
        title = item.get('title', 'Untitled')
        body = item.get('description', '') or item.get('body', '')
        # Clean HTML from body if present
        if body:
            body = html.unescape(body)
            # Convert line break tags to newlines before stripping other HTML
            # Handle <br>, <br/>, <br />, <BR>, etc.
            body = re.sub(r'<br\s*/?>', '\n', body, flags=re.IGNORECASE)
            # Convert paragraph tags to newlines (with spacing)
            body = re.sub(r'</p>', '\n\n', body, flags=re.IGNORECASE)
            body = re.sub(r'<p[^>]*>', '', body, flags=re.IGNORECASE)
            # Remove remaining HTML tags
            body = re.sub(r'<[^>]+>', '', body)
            # Clean up whitespace (preserve intentional line breaks)
            # Replace multiple newlines with double newline, then normalize spaces within lines
            body = re.sub(r'\n\s*\n\s*\n+', '\n\n', body)  # Multiple newlines -> double newline
            lines = body.split('\n')
            body = '\n'.join(' '.join(line.split()) for line in lines)  # Normalize spaces per line
            body = body.strip()
        
        link = item.get('link', '')
        published = item.get('published')
        date_str = self._format_timestamp(published)
        
        # Choose emoji based on feed type or content
        emoji = "📢"
        feed_name = feed.get('feed_name', '').lower()
        if 'emergency' in feed_name or 'alert' in feed_name:
            emoji = "🚨"
        elif 'warning' in feed_name:
            emoji = "⚠️"
        elif 'info' in feed_name or 'news' in feed_name:
            emoji = "ℹ️"
        
        # Build replacement dictionary
        replacements = {
            'title': title,
            'body': body,
            'date': date_str,
            'link': link,
            'emoji': emoji
        }
        
        # Get raw API data if available
        raw_data = item.get('raw', {})
        
        # Process format string with placeholders and functions
        # Pattern: {field|function} or {field} or {raw.field.path}
        def replace_placeholder(match):
            full_match = match.group(0)
            content = match.group(1)  # Content inside {}
            
            if '|' in content:
                field_name, function = content.split('|', 1)
                field_name = field_name.strip()
                function = function.strip()
                
                # Check if it's a raw field access
                if field_name.startswith('raw.'):
                    value = str(self._get_nested_value(raw_data, field_name[4:], ''))
                else:
                    value = replacements.get(field_name, '')
                
                return self._apply_shortening(value, function)
            else:
                field_name = content.strip()
                
                # Check if it's a raw field access
                if field_name.startswith('raw.'):
                    value = self._get_nested_value(raw_data, field_name[4:], '')
                    # Convert to string, handling None and complex types
                    if value is None:
                        return ''
                    elif isinstance(value, (dict, list)):
                        # For complex types, convert to JSON string
                        try:
                            return json.dumps(value)
                        except Exception:
                            return str(value)
                    else:
                        return str(value)
                else:
                    return replacements.get(field_name, '')
        
        # Replace all placeholders
        message = re.sub(r'\{([^}]+)\}', replace_placeholder, format_str)
        
        # Final truncation if message is too long
        if len(message) > self.max_message_length:
            # Try to preserve structure by truncating at newline if possible
            lines = message.split('\n')
            if len(lines) > 1:
                # Truncate last line
                total_length = sum(len(line) + 1 for line in lines[:-1])  # +1 for newline
                remaining = self.max_message_length - total_length - 3  # -3 for "..."
                if remaining > 20:
                    lines[-1] = lines[-1][:remaining] + "..."
                    message = '\n'.join(lines)
                else:
                    # Just truncate everything
                    message = message[:self.max_message_length - 3] + "..."
            else:
                message = message[:self.max_message_length - 3] + "..."
        
        return message
    
    def _queue_feed_message(self, feed: Dict[str, Any], item: Dict[str, Any], message: str):
        """Queue a feed message for later sending"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO feed_message_queue 
                    (feed_id, channel_name, message, item_id, item_title, priority)
                    VALUES (?, ?, ?, ?, ?, 0)
                ''', (
                    feed['id'],
                    feed['channel_name'],
                    message,
                    item.get('id', ''),
                    item.get('title', '')[:200]  # Limit title length
                ))
                conn.commit()
                self.logger.debug(f"Queued feed message for {feed['channel_name']}: {item.get('title', '')[:50]}")
        except Exception as e:
            self.logger.error(f"Error queuing feed message: {e}")
            self._record_feed_error(feed['id'], 'queue', str(e))
    
    def _should_send_item(self, feed: Dict[str, Any], item: Dict[str, Any]) -> bool:
        """Check if an item should be sent based on filter configuration
        
        Filter config format:
        {
            "conditions": [
                {"field": "Priority", "operator": "in", "values": ["highest", "high"]},
                {"field": "EventStatus", "operator": "equals", "value": "open"},
                {"field": "EventCategory", "operator": "not_equals", "value": "Maintenance"},
                {"field": "raw.Priority", "operator": "matches", "pattern": "^(highest|high)$"}
            ],
            "logic": "AND"  # or "OR"
        }
        
        Supported operators:
        - equals: exact match
        - not_equals: not exact match
        - in: value is in list
        - not_in: value is not in list
        - matches: regex match
        - not_matches: regex doesn't match
        - contains: substring match
        - not_contains: substring doesn't match
        """
        filter_config_str = feed.get('filter_config')
        if not filter_config_str:
            # No filter configured, send all items
            return True
        
        try:
            filter_config = json.loads(filter_config_str) if isinstance(filter_config_str, str) else filter_config_str
        except (json.JSONDecodeError, TypeError):
            self.logger.warning(f"Invalid filter_config for feed {feed['id']}, sending all items")
            return True
        
        conditions = filter_config.get('conditions', [])
        if not conditions:
            # Empty conditions, send all items
            return True
        
        logic = filter_config.get('logic', 'AND').upper()
        
        # Get raw data for field access
        raw_data = item.get('raw', {})
        
        # Evaluate each condition
        results = []
        for condition in conditions:
            field_path = condition.get('field')
            operator = condition.get('operator', 'equals')
            
            if not field_path:
                # Invalid condition, skip it
                continue
            
            # Get field value using nested access
            field_value = self._get_nested_value(raw_data, field_path, '')
            if not field_value and field_path.startswith('raw.'):
                # Try without 'raw.' prefix
                field_value = self._get_nested_value(raw_data, field_path[4:], '')
            
            # If still not found, try top-level item fields
            if not field_value:
                field_value = self._get_nested_value(item, field_path, '')
            
            # Convert to string for comparison
            field_value_str = str(field_value).lower() if field_value is not None else ''
            
            # Evaluate condition
            result = False
            if operator == 'equals':
                compare_value = str(condition.get('value', '')).lower()
                result = field_value_str == compare_value
            elif operator == 'not_equals':
                compare_value = str(condition.get('value', '')).lower()
                result = field_value_str != compare_value
            elif operator == 'in':
                values = [str(v).lower() for v in condition.get('values', [])]
                result = field_value_str in values
            elif operator == 'not_in':
                values = [str(v).lower() for v in condition.get('values', [])]
                result = field_value_str not in values
            elif operator == 'matches':
                pattern = condition.get('pattern', '')
                if pattern:
                    try:
                        result = bool(re.search(pattern, str(field_value), re.IGNORECASE))
                    except re.error:
                        result = False
            elif operator == 'not_matches':
                pattern = condition.get('pattern', '')
                if pattern:
                    try:
                        result = not bool(re.search(pattern, str(field_value), re.IGNORECASE))
                    except re.error:
                        result = True
            elif operator == 'contains':
                compare_value = str(condition.get('value', '')).lower()
                result = compare_value in field_value_str
            elif operator == 'not_contains':
                compare_value = str(condition.get('value', '')).lower()
                result = compare_value not in field_value_str
            else:
                self.logger.warning(f"Unknown filter operator: {operator}")
                result = True  # Default to allowing if operator is unknown
            
            results.append(result)
        
        # Apply logic (AND or OR)
        if logic == 'OR':
            return any(results)
        else:  # AND (default)
            return all(results)
    
    async def _send_feed_item(self, feed: Dict[str, Any], item: Dict[str, Any]):
        """Queue a feed item message instead of sending immediately"""
        try:
            message = self.format_message(item, feed)
            # Queue the message instead of sending immediately
            self._queue_feed_message(feed, item, message)
        except Exception as e:
            self.logger.error(f"Error processing feed item: {e}")
            self._record_feed_error(feed['id'], 'other', str(e))
    
    async def _wait_for_rate_limit(self, domain: str):
        """Wait if needed to respect rate limits"""
        if domain in self._domain_last_request:
            last_request = self._domain_last_request[domain]
            elapsed = time.time() - last_request
            if elapsed < self.rate_limit_seconds:
                wait_time = self.rate_limit_seconds - elapsed
                await asyncio.sleep(wait_time)
        
        self._domain_last_request[domain] = time.time()
    
    def _get_enabled_feeds(self) -> List[Dict[str, Any]]:
        """Get all enabled feed subscriptions from database"""
        try:
            with sqlite3.connect(self.db_path) as conn:
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
            
            with sqlite3.connect(self.db_path) as conn:
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
            with sqlite3.connect(self.db_path) as conn:
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
            with sqlite3.connect(self.db_path) as conn:
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
            with sqlite3.connect(self.db_path) as conn:
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
        try:
            # Get all unsent messages, ordered by priority and queue time
            db_path = str(self.db_path)  # Ensure string, not Path object
            with sqlite3.connect(db_path, timeout=30.0) as conn:
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
            
            # Group messages by feed to respect per-feed send intervals
            feed_last_send: Dict[int, float] = {}
            
            for msg in messages:
                feed_id = msg['feed_id']
                channel_name = msg['channel_name']
                message_text = msg['message']
                queue_id = msg['id']
                item_id = msg['item_id']
                item_title = msg['item_title']
                
                # Get send interval for this feed (default if not set)
                send_interval = msg['message_send_interval_seconds'] or self.default_send_interval
                
                # Check if we need to wait before sending this feed's message
                if feed_id in feed_last_send:
                    elapsed = time.time() - feed_last_send[feed_id]
                    if elapsed < send_interval:
                        wait_time = send_interval - elapsed
                        await asyncio.sleep(wait_time)
                
                # Send the message
                try:
                    success = await self.bot.command_manager.send_channel_message(channel_name, message_text)
                    
                    if success:
                        # Mark as sent
                        with sqlite3.connect(db_path, timeout=30.0) as conn:
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
                        feed_last_send[feed_id] = time.time()
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
            self.logger.error(f"Error processing message queue: {e}")
            if db_path_str != 'unknown':
                path_obj = Path(db_path_str)
                self.logger.error(f"Database path: {db_path_str} (exists: {path_obj.exists()}, readable: {os.access(db_path_str, os.R_OK) if path_obj.exists() else False}, writable: {os.access(db_path_str, os.W_OK) if path_obj.exists() else False})")
                # Check parent directory permissions
                if path_obj.exists():
                    parent = path_obj.parent
                    self.logger.error(f"Parent directory: {parent} (exists: {parent.exists()}, writable: {os.access(str(parent), os.W_OK) if parent.exists() else False})")
            else:
                self.logger.error(f"Database path: {db_path_str}")

