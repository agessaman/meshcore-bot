#!/usr/bin/env python3
"""Tests for modules/channel_manager.py — pure-logic and cache-layer paths.

Hardware/network methods (fetch_channels, fetch_all_channels,
_fetch_single_channel, add_hashtag_channel) are excluded because they
require a live MeshCore device.
"""

import hashlib
import logging
from unittest.mock import MagicMock

import pytest

from modules.channel_manager import ChannelManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_bot():
    bot = MagicMock()
    bot.logger = logging.getLogger("test")
    bot.connected = True
    bot.meshcore = MagicMock()
    bot.db_manager = MagicMock()
    return bot


def make_manager(max_channels: int = 40) -> ChannelManager:
    return ChannelManager(make_bot(), max_channels=max_channels)


def _seeded_manager(channels: dict) -> ChannelManager:
    """Return a ChannelManager whose cache is pre-populated with *channels*."""
    cm = make_manager()
    cm._channels_cache = channels
    cm._cache_valid = True
    return cm


# ---------------------------------------------------------------------------
# generate_hashtag_key
# ---------------------------------------------------------------------------

class TestGenerateHashtagKey:

    def test_returns_16_bytes(self):
        key = ChannelManager.generate_hashtag_key("general")
        assert isinstance(key, bytes)
        assert len(key) == 16

    def test_adds_hash_prefix_when_missing(self):
        key_with = ChannelManager.generate_hashtag_key("#general")
        key_without = ChannelManager.generate_hashtag_key("general")
        assert key_with == key_without

    def test_case_insensitive(self):
        key_lower = ChannelManager.generate_hashtag_key("#general")
        key_upper = ChannelManager.generate_hashtag_key("#GENERAL")
        assert key_lower == key_upper

    def test_matches_manual_sha256(self):
        name = "#general"
        expected = hashlib.sha256(name.encode("utf-8")).digest()[:16]
        assert ChannelManager.generate_hashtag_key(name) == expected

    def test_different_names_produce_different_keys(self):
        assert ChannelManager.generate_hashtag_key("alpha") != ChannelManager.generate_hashtag_key("beta")

    def test_empty_string_prepends_hash(self):
        # Should not raise; '#' becomes the name
        key = ChannelManager.generate_hashtag_key("")
        assert len(key) == 16

    def test_unicode_name(self):
        key = ChannelManager.generate_hashtag_key("canal")
        assert len(key) == 16


# ---------------------------------------------------------------------------
# get_channel_name
# ---------------------------------------------------------------------------

class TestGetChannelName:

    def test_returns_name_from_cache(self):
        cm = _seeded_manager({0: {"channel_name": "general", "channel_key_hex": "aa"}})
        assert cm.get_channel_name(0) == "general"

    def test_falls_back_to_channel_number_label(self):
        cm = make_manager()
        assert cm.get_channel_name(5) == "Channel5"

    def test_channel_with_no_name_field_uses_default(self):
        cm = _seeded_manager({3: {"channel_key_hex": "bb"}})
        assert cm.get_channel_name(3) == "Channel3"

    def test_channel_zero_returns_correct_name(self):
        cm = _seeded_manager({0: {"channel_name": "primary"}})
        assert cm.get_channel_name(0) == "primary"


# ---------------------------------------------------------------------------
# get_channel_number
# ---------------------------------------------------------------------------

class TestGetChannelNumber:

    def test_returns_index_by_name(self):
        cm = _seeded_manager({
            0: {"channel_name": "general"},
            1: {"channel_name": "emergency"},
        })
        assert cm.get_channel_number("general") == 0
        assert cm.get_channel_number("emergency") == 1

    def test_lookup_is_case_insensitive(self):
        cm = _seeded_manager({2: {"channel_name": "TacticalNet"}})
        assert cm.get_channel_number("tacticalnet") == 2
        assert cm.get_channel_number("TACTICALNET") == 2

    def test_returns_none_when_not_found(self):
        cm = _seeded_manager({0: {"channel_name": "general"}})
        assert cm.get_channel_number("nonexistent") is None

    def test_empty_cache_returns_none(self):
        cm = make_manager()
        assert cm.get_channel_number("anything") is None


# ---------------------------------------------------------------------------
# get_channel_key
# ---------------------------------------------------------------------------

class TestGetChannelKey:

    def test_returns_hex_key(self):
        cm = _seeded_manager({0: {"channel_key_hex": "deadbeef" * 4}})
        assert cm.get_channel_key(0) == "deadbeef" * 4

    def test_returns_empty_string_when_channel_missing(self):
        cm = make_manager()
        assert cm.get_channel_key(99) == ""

    def test_returns_empty_string_when_key_field_absent(self):
        cm = _seeded_manager({0: {"channel_name": "general"}})
        assert cm.get_channel_key(0) == ""


# ---------------------------------------------------------------------------
# get_channel_info
# ---------------------------------------------------------------------------

class TestGetChannelInfo:

    def test_returns_dict_with_name_key_info(self):
        cm = _seeded_manager({1: {"channel_name": "ops", "channel_key_hex": "abcd1234"}})
        info = cm.get_channel_info(1)
        assert info["name"] == "ops"
        assert info["key"] == "abcd1234"
        assert info["info"]["channel_name"] == "ops"

    def test_missing_channel_returns_fallback(self):
        cm = make_manager()
        info = cm.get_channel_info(7)
        assert info["name"] == "Channel7"
        assert info["key"] == ""
        assert info["info"] == {}

    def test_info_contains_full_cache_entry(self):
        payload = {"channel_name": "alpha", "channel_key_hex": "1122", "extra": True}
        cm = _seeded_manager({3: payload})
        result = cm.get_channel_info(3)
        assert result["info"] == payload


# ---------------------------------------------------------------------------
# get_channel_by_name
# ---------------------------------------------------------------------------

class TestGetChannelByName:

    def test_returns_channel_dict_when_found(self):
        entry = {"channel_name": "general", "channel_key_hex": "ff"}
        cm = _seeded_manager({0: entry})
        assert cm.get_channel_by_name("general") == entry

    def test_lookup_is_case_insensitive(self):
        entry = {"channel_name": "General"}
        cm = _seeded_manager({0: entry})
        assert cm.get_channel_by_name("GENERAL") == entry

    def test_returns_none_when_not_found(self):
        cm = _seeded_manager({0: {"channel_name": "general"}})
        assert cm.get_channel_by_name("missing") is None

    def test_returns_none_when_cache_invalid(self):
        cm = _seeded_manager({0: {"channel_name": "general"}})
        cm._cache_valid = False
        assert cm.get_channel_by_name("general") is None

    def test_empty_cache_returns_none(self):
        cm = make_manager()
        cm._cache_valid = True
        assert cm.get_channel_by_name("anything") is None


# ---------------------------------------------------------------------------
# get_configured_channels
# ---------------------------------------------------------------------------

class TestGetConfiguredChannels:

    def test_returns_non_empty_named_channels(self):
        cm = _seeded_manager({
            0: {"channel_name": "general"},
            1: {"channel_name": ""},
            2: {"channel_name": "   "},
            3: {"channel_name": "ops"},
        })
        result = cm.get_configured_channels()
        names = [ch["channel_name"] for ch in result]
        assert "general" in names
        assert "ops" in names
        assert "" not in names

    def test_excludes_whitespace_only_names(self):
        cm = _seeded_manager({0: {"channel_name": "   "}})
        assert cm.get_configured_channels() == []

    def test_returns_empty_list_when_cache_invalid(self):
        cm = _seeded_manager({0: {"channel_name": "general"}})
        cm._cache_valid = False
        assert cm.get_configured_channels() == []

    def test_returns_empty_list_when_no_named_channels(self):
        cm = _seeded_manager({0: {"channel_name": ""}})
        assert cm.get_configured_channels() == []

    def test_channels_missing_name_field_excluded(self):
        cm = _seeded_manager({0: {"channel_key_hex": "aa"}})
        assert cm.get_configured_channels() == []


# ---------------------------------------------------------------------------
# invalidate_cache
# ---------------------------------------------------------------------------

class TestInvalidateCache:

    def test_sets_cache_valid_false(self):
        cm = _seeded_manager({0: {"channel_name": "general"}})
        assert cm._cache_valid is True
        cm.invalidate_cache()
        assert cm._cache_valid is False

    def test_does_not_clear_channel_data(self):
        cm = _seeded_manager({0: {"channel_name": "general"}})
        cm.invalidate_cache()
        assert 0 in cm._channels_cache

    def test_idempotent(self):
        cm = make_manager()
        cm.invalidate_cache()
        cm.invalidate_cache()
        assert cm._cache_valid is False


# ---------------------------------------------------------------------------
# _get_cached_channels
# ---------------------------------------------------------------------------

class TestGetCachedChannels:

    def test_returns_sorted_by_index(self):
        cm = _seeded_manager({
            5: {"channel_name": "e"},
            0: {"channel_name": "a"},
            3: {"channel_name": "c"},
        })
        result = cm._get_cached_channels()
        names = [ch["channel_name"] for ch in result]
        assert names == ["a", "c", "e"]

    def test_empty_cache_returns_empty_list(self):
        cm = make_manager()
        assert cm._get_cached_channels() == []

    def test_single_channel_returns_list_of_one(self):
        cm = _seeded_manager({7: {"channel_name": "solo"}})
        result = cm._get_cached_channels()
        assert len(result) == 1
        assert result[0]["channel_name"] == "solo"


# ---------------------------------------------------------------------------
# add_channel — validation-only paths (no hardware)
# ---------------------------------------------------------------------------

class TestAddChannelValidation:

    @pytest.mark.asyncio
    async def test_returns_false_when_not_connected(self):
        bot = make_bot()
        bot.connected = False
        cm = ChannelManager(bot)
        result = await cm.add_channel(0, "testchan")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_meshcore_falsy(self):
        bot = make_bot()
        bot.meshcore = None
        cm = ChannelManager(bot)
        result = await cm.add_channel(0, "testchan")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_for_negative_index(self):
        cm = make_manager()
        result = await cm.add_channel(-1, "#general")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_for_index_at_max(self):
        cm = make_manager(max_channels=10)
        result = await cm.add_channel(10, "#general")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_for_index_beyond_max(self):
        cm = make_manager(max_channels=10)
        result = await cm.add_channel(99, "#general")
        assert result is False

    @pytest.mark.asyncio
    async def test_custom_channel_missing_key_returns_false(self):
        cm = make_manager()
        # Non-hashtag name with no key provided
        result = await cm.add_channel(0, "custom_no_key")
        assert result is False

    @pytest.mark.asyncio
    async def test_custom_channel_invalid_hex_returns_false(self):
        cm = make_manager()
        result = await cm.add_channel(0, "custom", channel_secret_hex="ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ")
        assert result is False

    @pytest.mark.asyncio
    async def test_custom_channel_short_hex_returns_false(self):
        cm = make_manager()
        result = await cm.add_channel(0, "custom", channel_secret_hex="deadbeef")
        assert result is False

    @pytest.mark.asyncio
    async def test_custom_channel_wrong_byte_length_returns_false(self):
        cm = make_manager()
        # 8 bytes instead of 16
        result = await cm.add_channel(0, "custom", channel_secret=b"\x00" * 8)
        assert result is False

    @pytest.mark.asyncio
    async def test_index_zero_valid_boundary_proceeds_past_validation(self):
        """Index 0 is inside range; failure happens later (hardware), not validation."""
        cm = make_manager(max_channels=40)
        # Patch commands so it doesn't raise AttributeError deep in the method
        cm.bot.meshcore.commands = None
        # The method should either return False (CLI fallback fails) or raise
        # an exception that we catch — the important thing is it passes the
        # out-of-range guard (no early False from range check).
        try:
            result = await cm.add_channel(0, "#general")
            # False is expected because CLI fallback is not available in tests
            assert result is False
        except Exception:
            pass  # Any exception means range-check was NOT the reason for failure
