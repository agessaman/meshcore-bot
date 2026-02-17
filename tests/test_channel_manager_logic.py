"""Tests for ChannelManager pure logic (no meshcore device calls)."""

import hashlib

import pytest
from unittest.mock import Mock

from modules.channel_manager import ChannelManager


@pytest.fixture
def cm(mock_logger):
    """ChannelManager with mock bot for pure logic tests."""
    bot = Mock()
    bot.logger = mock_logger
    bot.db_manager = Mock()
    bot.db_manager.db_path = "/dev/null"
    bot.connected = False
    bot.meshcore = Mock()
    bot.meshcore.channels = {}
    return ChannelManager(bot, max_channels=8)


class TestGenerateHashtagKey:
    """Tests for generate_hashtag_key() static method."""

    def test_deterministic(self):
        key1 = ChannelManager.generate_hashtag_key("general")
        key2 = ChannelManager.generate_hashtag_key("general")
        assert key1 == key2
        assert len(key1) == 16

    def test_prepends_hash_if_missing(self):
        key_without = ChannelManager.generate_hashtag_key("general")
        key_with = ChannelManager.generate_hashtag_key("#general")
        assert key_without == key_with

    def test_known_value(self):
        """Verify against independently computed SHA256."""
        expected = hashlib.sha256(b"#longfast").digest()[:16]
        result = ChannelManager.generate_hashtag_key("#LongFast")
        assert result == expected


class TestChannelNameLookup:
    """Tests for get_channel_name()."""

    def test_cached_channel_name(self, cm):
        cm._channels_cache = {0: {"channel_name": "general"}}
        cm._cache_valid = True
        assert cm.get_channel_name(0) == "general"

    def test_not_cached_returns_fallback(self, cm):
        cm._channels_cache = {}
        cm._cache_valid = True
        result = cm.get_channel_name(99)
        assert "99" in result


class TestChannelNumberLookup:
    """Tests for get_channel_number()."""

    def test_found_by_name(self, cm):
        cm._channels_cache = {0: {"channel_name": "general"}, 1: {"channel_name": "test"}}
        cm._cache_valid = True
        assert cm.get_channel_number("test") == 1

    def test_case_insensitive(self, cm):
        cm._channels_cache = {0: {"channel_name": "General"}}
        cm._cache_valid = True
        assert cm.get_channel_number("general") == 0


class TestCacheManagement:
    """Tests for cache invalidation."""

    def test_invalidate_cache(self, cm):
        cm._cache_valid = True
        cm.invalidate_cache()
        assert cm._cache_valid is False
