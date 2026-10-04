"""modules.feed_parse: the parsing and config pieces FeedManager and the viewer share."""

import configparser
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from modules import feed_parse as fp


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, None),
        ("", None),
        (1700000000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)),
        ("2024-05-06T07:08:09Z", datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc)),
        # fromisoformat (3.11+) accepts these before the UTC-tagging fallback is
        # reached, so they come back naive. Pinned as current behavior.
        ("2024-05-06 07:08:09", datetime(2024, 5, 6, 7, 8, 9)),
        ("2024-05-06", datetime(2024, 5, 6)),
        ("not a date", None),
        (["list"], None),
    ],
)
def test_parse_item_timestamp(value, expected):
    assert fp.parse_item_timestamp(value) == expected


def test_parse_item_timestamp_microsoft_format():
    assert fp.parse_item_timestamp("/Date(1700000000000)/") == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)


def test_rss_entry_published():
    assert fp.rss_entry_published(SimpleNamespace(published_parsed=(2024, 1, 2, 3, 4, 5, 0, 0, 0))) == datetime(
        2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc
    )
    assert fp.rss_entry_published(SimpleNamespace()) is None
    assert fp.rss_entry_published(SimpleNamespace(published_parsed=None)) is None


def test_api_item_fields_reads_nested_and_custom_fields():
    item = {"t": {"name": "Hello"}, "body": 5, "link": "http://x", "when": "2024-05-06"}
    got = fp.api_item_fields(item, {"title_field": "t.name", "description_field": "body", "timestamp_field": "when"})
    assert got == {
        "title": "Hello", "emoji": "", "link": "http://x", "description": "5",
        "published": datetime(2024, 5, 6), "raw": item,
    }
    assert fp.api_item_fields({}, {})["title"] == "Untitled"


def _config(**sections):
    cfg = configparser.ConfigParser()
    for name, values in sections.items():
        cfg[name] = values
    return cfg


def test_config_helpers_defaults_and_clamps():
    empty = _config()
    assert fp.feed_allow_private_urls(empty) is False
    assert fp.feed_max_response_bytes(empty) == fp.DEFAULT_MAX_FEED_RESPONSE_BYTES
    assert fp.feed_max_parsed_items(empty) == fp.DEFAULT_MAX_PARSED_FEED_ITEMS
    cfg = _config(Feed_Manager={"max_response_bytes": "10", "max_parsed_items": "0"})
    assert fp.feed_max_response_bytes(cfg) == 1024
    assert fp.feed_max_parsed_items(cfg) == 1


def test_allow_private_falls_back_to_feed_command():
    assert fp.feed_allow_private_urls(_config(Feed_Command={"allow_private_urls": "true"})) is True
    assert fp.feed_allow_private_urls(_config(Feed_Command={"allow_private_urls": "maybe"})) is False
    cfg = _config(Feed_Command={"allow_private_urls": "true"}, Feed_Manager={"allow_private_urls": "false"})
    assert fp.feed_allow_private_urls(cfg) is False
    assert fp.feed_allow_private_urls(_config(Feed_Command={"allow_private_urls": "true"}, Feed_Manager={})) is True


def test_feed_manager_still_exports_the_size_defaults():
    from modules import feed_manager

    assert feed_manager.DEFAULT_MAX_FEED_RESPONSE_BYTES == fp.DEFAULT_MAX_FEED_RESPONSE_BYTES
    assert feed_manager.DEFAULT_MAX_PARSED_FEED_ITEMS == fp.DEFAULT_MAX_PARSED_FEED_ITEMS
