"""Tests for ChannelManager pure logic (no meshcore device calls)."""

import hashlib
from unittest.mock import AsyncMock, Mock, patch

import pytest

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


# ---------------------------------------------------------------------------
# TestGetCachedChannels
# ---------------------------------------------------------------------------


class TestGetCachedChannels:
    """Tests for _get_cached_channels()."""

    def test_returns_channels_sorted_by_index(self, cm):
        cm._channels_cache = {
            2: {"channel_name": "third", "channel_idx": 2},
            0: {"channel_name": "first", "channel_idx": 0},
            1: {"channel_name": "second", "channel_idx": 1},
        }
        result = cm._get_cached_channels()
        assert [c["channel_name"] for c in result] == ["first", "second", "third"]

    def test_empty_cache_returns_empty_list(self, cm):
        cm._channels_cache = {}
        assert cm._get_cached_channels() == []

    def test_single_channel_in_cache(self, cm):
        cm._channels_cache = {0: {"channel_name": "solo", "channel_idx": 0}}
        result = cm._get_cached_channels()
        assert len(result) == 1
        assert result[0]["channel_name"] == "solo"


# ---------------------------------------------------------------------------
# TestFetchAllChannelsCacheLifecycle
# ---------------------------------------------------------------------------


class TestFetchAllChannelsCacheLifecycle:
    """Tests for fetch_all_channels() cache validity transitions."""

    @pytest.mark.asyncio
    async def test_cache_valid_returns_cached_without_device_call(self, cm):
        cm._channels_cache = {0: {"channel_name": "general", "channel_idx": 0}}
        cm._cache_valid = True
        cm.bot.connected = True
        channels = await cm.fetch_all_channels(force_refresh=False)
        assert len(channels) == 1
        assert channels[0]["channel_name"] == "general"

    @pytest.mark.asyncio
    async def test_device_not_connected_returns_empty_list(self, cm):
        cm.bot.connected = False
        channels = await cm.fetch_all_channels(force_refresh=True)
        assert channels == []

    @pytest.mark.asyncio
    async def test_force_refresh_when_disconnected_preserves_existing_cache(self, cm):
        """When device is not connected, the connectivity check fires before cache clear,
        so the existing cache is preserved (early return before clear)."""
        cm._channels_cache = {0: {"channel_name": "existing", "channel_idx": 0}}
        cm._cache_valid = True
        cm.bot.connected = False
        channels = await cm.fetch_all_channels(force_refresh=True)
        # Returns empty list (device not connected)
        assert channels == []
        # Cache not cleared because early return before clear_cache step
        assert 0 in cm._channels_cache

    @pytest.mark.asyncio
    async def test_successful_fetch_marks_cache_valid(self, cm):
        """After a successful fetch, _cache_valid should be True."""
        cm.bot.connected = True

        async def fake_fetch_single(idx):
            if idx == 0:
                return {"channel_name": "general", "channel_idx": 0, "channel_key_hex": ""}
            return None

        cm._fetch_single_channel = fake_fetch_single
        cm._store_channels_in_db = Mock()

        channels = await cm.fetch_all_channels(force_refresh=True)
        assert cm._cache_valid is True
        assert any(c["channel_name"] == "general" for c in channels)

    @pytest.mark.asyncio
    async def test_three_consecutive_timeouts_aborts_fetch(self, cm):
        """If first 3 channels all return None, the fetch should abort early."""
        cm.bot.connected = True
        call_count = 0

        async def always_none(idx):
            nonlocal call_count
            call_count += 1
            return None

        cm._fetch_single_channel = always_none
        cm._store_channels_in_db = Mock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            channels = await cm.fetch_all_channels(force_refresh=True)

        # Should have aborted before fetching all 8 channels
        assert call_count < cm.max_channels
        assert channels == []


# ---------------------------------------------------------------------------
# TestGetChannelByName
# ---------------------------------------------------------------------------


class TestGetChannelByName:
    """Tests for get_channel_number() with various cache states."""

    def test_returns_none_when_cache_empty(self, cm):
        cm._channels_cache = {}
        cm._cache_valid = True
        assert cm.get_channel_number("nonexistent") is None

    def test_exact_name_lookup(self, cm):
        """get_channel_number does a case-insensitive exact match (no # stripping)."""
        cm._channels_cache = {0: {"channel_name": "#general"}}
        cm._cache_valid = True
        assert cm.get_channel_number("#general") == 0
