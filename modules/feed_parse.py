"""Feed parsing pieces shared by the bot's FeedManager and the web viewer's preview.

Only what both sides do identically lives here. Where they diverge (how a
dict API response without ``items_path`` is unwrapped, string items, items
without an id) each side keeps its own code.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from typing import Any, Optional

from modules.feed_filter_eval import get_nested_value, parse_microsoft_date

DEFAULT_MAX_FEED_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_PARSED_FEED_ITEMS = 500

_FALLBACK_TIMESTAMP_FORMATS = ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d')


def parse_item_timestamp(ts_value: Any) -> Optional[datetime]:
    """An API item's timestamp as an aware datetime, or None.

    Accepts epoch seconds, Microsoft ``/Date(...)/`` strings, ISO 8601 (``Z``
    allowed) and three plain formats, the last read as UTC.
    """
    if not ts_value:
        return None
    published = None
    try:
        if isinstance(ts_value, (int, float)):
            published = datetime.fromtimestamp(ts_value, tz=timezone.utc)
        elif isinstance(ts_value, str):
            if ts_value.startswith('/Date('):
                published = parse_microsoft_date(ts_value)
            else:
                try:
                    published = datetime.fromisoformat(ts_value.replace('Z', '+00:00'))
                except ValueError:
                    for fmt in _FALLBACK_TIMESTAMP_FORMATS:
                        try:
                            published = datetime.strptime(ts_value, fmt)
                            if published.tzinfo is None:
                                published = published.replace(tzinfo=timezone.utc)
                            break
                        except ValueError:
                            continue
    except Exception:
        pass
    return published


def rss_entry_published(entry: Any) -> Optional[datetime]:
    """A feedparser entry's ``published_parsed`` as an aware UTC datetime, or None."""
    if hasattr(entry, 'published_parsed') and entry.published_parsed:
        with contextlib.suppress(Exception):
            pt = entry.published_parsed
            return datetime(pt[0], pt[1], pt[2], pt[3], pt[4], pt[5], tzinfo=timezone.utc)
    return None


def api_item_fields(item_data: dict[str, Any], parser_config: dict[str, Any]) -> dict[str, Any]:
    """Title, emoji, description, link, published and raw for one API item dict."""
    title_field = parser_config.get('title_field', 'title')
    description_field = parser_config.get('description_field', 'description')
    timestamp_field = parser_config.get('timestamp_field', 'created_at')
    emoji_field = parser_config.get('emoji_field', 'emoji')
    published = parse_item_timestamp(get_nested_value(item_data, timestamp_field)) if timestamp_field else None
    description = ''
    if description_field:
        desc_value = get_nested_value(item_data, description_field)
        if desc_value:
            description = str(desc_value)
    return {
        'title': get_nested_value(item_data, title_field, 'Untitled'),
        'emoji': get_nested_value(item_data, emoji_field, ''),
        'link': item_data.get('link', ''),
        'description': description,
        'published': published,
        'raw': item_data,
    }


def feed_allow_private_urls(config: Any) -> bool:
    """[Feed_Manager] allow_private_urls, defaulting to [Feed_Command]'s value (else False)."""
    feed_command_allow_private = False
    if config.has_section('Feed_Command'):
        try:
            feed_command_allow_private = config.getboolean('Feed_Command', 'allow_private_urls', fallback=False)
        except ValueError:
            feed_command_allow_private = False
    if not config.has_section('Feed_Manager'):
        return feed_command_allow_private
    return config.getboolean('Feed_Manager', 'allow_private_urls', fallback=feed_command_allow_private)


def feed_max_response_bytes(config: Any) -> int:
    """[Feed_Manager] max_response_bytes, at least 1 KiB."""
    if not config.has_section('Feed_Manager'):
        return DEFAULT_MAX_FEED_RESPONSE_BYTES
    return max(1024, config.getint('Feed_Manager', 'max_response_bytes', fallback=DEFAULT_MAX_FEED_RESPONSE_BYTES))


def feed_max_parsed_items(config: Any) -> int:
    """[Feed_Manager] max_parsed_items, at least 1."""
    if not config.has_section('Feed_Manager'):
        return DEFAULT_MAX_PARSED_FEED_ITEMS
    return max(1, config.getint('Feed_Manager', 'max_parsed_items', fallback=DEFAULT_MAX_PARSED_FEED_ITEMS))
