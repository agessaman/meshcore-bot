#!/usr/bin/env python3
"""Unit tests for PathCommand repeater lookup (recency filter and prefix matching)."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from modules.commands.path_command import PathCommand
from modules.utils import public_key_has_prefix
from tests.helpers import create_test_repeater


@pytest.mark.unit
class TestPublicKeyHasPrefix:
    def test_case_insensitive_match(self):
        assert public_key_has_prefix("155eabcd", "155E") is True
        assert public_key_has_prefix("155EABCD", "155e") is True

    def test_empty_inputs(self):
        assert public_key_has_prefix("", "155E") is False
        assert public_key_has_prefix("155eabcd", "") is False


@pytest.mark.unit
class TestPathCommandLookupRecency:
    @pytest.fixture
    def path_command(self, mock_bot):
        cmd = PathCommand(mock_bot)
        cmd.graph_based_validation = False
        cmd.geographic_guessing_enabled = False
        return cmd

    @pytest.mark.asyncio
    async def test_single_stale_repeater_resolves(self, path_command):
        stale = datetime.now() - timedelta(days=30)

        def lookup(node_id):
            return [
                create_test_repeater(
                    prefix="15",
                    name="Stale Repeater",
                    public_key="155e" + "ab" * 31,
                    last_heard=stale,
                    last_advert_timestamp=stale,
                )
            ]

        result = await path_command._lookup_repeater_names(["155E"], lookup_func=lookup)

        assert result["155E"]["found"] is True
        assert result["155E"]["collision"] is False
        assert result["155E"]["name"] == "Stale Repeater"

    @pytest.mark.asyncio
    async def test_multiple_stale_repeaters_remain_unknown(self, path_command):
        stale = datetime.now() - timedelta(days=30)

        def lookup(node_id):
            return [
                create_test_repeater(
                    prefix="15",
                    name="Stale A",
                    public_key="155e" + "11" * 31,
                    last_heard=stale,
                    last_advert_timestamp=stale,
                ),
                create_test_repeater(
                    prefix="15",
                    name="Stale B",
                    public_key="155e" + "22" * 31,
                    last_heard=stale,
                    last_advert_timestamp=stale,
                ),
            ]

        result = await path_command._lookup_repeater_names(["155E"], lookup_func=lookup)

        assert result["155E"]["found"] is False

    @pytest.mark.asyncio
    async def test_case_insensitive_repeater_manager_match(self, mock_bot):
        mock_bot.repeater_manager.get_repeater_devices = AsyncMock(
            return_value=[
                {
                    "public_key": "155e" + "ab" * 31,
                    "name": "Repeater 155E",
                    "device_type": "repeater",
                    "last_heard": datetime.now() - timedelta(days=30),
                    "last_advert_timestamp": datetime.now() - timedelta(days=30),
                    "is_currently_tracked": False,
                    "latitude": 47.6,
                    "longitude": -122.3,
                    "city": "Seattle",
                    "state": "WA",
                    "country": "USA",
                    "advert_count": 1,
                    "signal_strength": None,
                    "snr": None,
                    "hop_count": 0,
                    "role": "repeater",
                    "is_starred": 0,
                }
            ]
        )
        mock_bot.meshcore = Mock()
        mock_bot.meshcore.contacts = {}

        path_command = PathCommand(mock_bot)
        path_command.graph_based_validation = False
        path_command.geographic_guessing_enabled = False

        result = await path_command._lookup_repeater_names(["155E"])

        assert result["155E"]["found"] is True
        assert result["155E"]["name"] == "Repeater 155E"
