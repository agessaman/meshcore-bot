#!/usr/bin/env python3
"""
Unit tests for PathCommand multi-byte path support: routing_info usage and comma/prefix parsing.
"""


import pytest

from modules.commands.path_command import PathCommand
from modules.models import MeshMessage


@pytest.mark.unit
class TestPathCommandDecodePathMultibyte:
    """Test _decode_path parsing: comma-separated inference and parse_path_string fallback."""

    @pytest.fixture
    def path_command(self, mock_bot):
        """Create a PathCommand instance."""
        return PathCommand(mock_bot)

    @pytest.mark.asyncio
    async def test_comma_separated_two_byte_infers_four_char_nodes(self, path_command, mock_bot):
        """path 0102,5f7e -> 2 nodes (0102, 5F7E) even when prefix_hex_chars=2 would give 4 nodes for continuous."""
        mock_bot.prefix_hex_chars = 2
        captured = []

        async def capture_lookup(node_ids, lookup_func=None):
            captured.append(node_ids)
            return {nid: {'found': True, 'name': nid} for nid in node_ids}

        path_command._lookup_repeater_names = capture_lookup
        await path_command._decode_path("0102,5f7e")
        assert len(captured) == 1
        assert captured[0] == ['0102', '5F7E']

    @pytest.mark.asyncio
    async def test_comma_separated_one_byte_two_nodes(self, path_command, mock_bot):
        """path 01,5f -> 2 nodes (01, 5F)."""
        mock_bot.prefix_hex_chars = 2
        captured = []

        async def capture_lookup(node_ids, lookup_func=None):
            captured.append(node_ids)
            return {nid: {'found': True, 'name': nid} for nid in node_ids}

        path_command._lookup_repeater_names = capture_lookup
        await path_command._decode_path("01,5f")
        assert len(captured) == 1
        assert captured[0] == ['01', '5F']

    @pytest.mark.asyncio
    async def test_continuous_hex_uses_prefix_hex_chars(self, path_command, mock_bot):
        """path 01025f7e: prefix_hex_chars=2 -> 4 nodes (1-byte); prefix_hex_chars=4 -> 2 nodes (2-byte)."""
        captured = []

        async def capture_lookup(node_ids, lookup_func=None):
            captured.append(list(node_ids))
            return {nid: {'found': True, 'name': nid} for nid in node_ids}

        path_command._lookup_repeater_names = capture_lookup

        mock_bot.prefix_hex_chars = 2  # 2 hex chars per node = 1 byte per hop
        captured.clear()
        await path_command._decode_path("01025f7e")
        assert len(captured) == 1
        assert captured[0] == ['01', '02', '5F', '7E']

        mock_bot.prefix_hex_chars = 4  # 4 hex chars per node = 2 bytes per hop
        captured.clear()
        await path_command._decode_path("01025f7e")
        assert len(captured) == 1
        assert captured[0] == ['0102', '5F7E']

    @pytest.mark.asyncio
    async def test_strips_hop_count_suffix(self, path_command, mock_bot):
        """path 01,5f (2 hops) -> 2 nodes."""
        mock_bot.prefix_hex_chars = 2
        captured = []

        async def capture_lookup(node_ids, lookup_func=None):
            captured.append(node_ids)
            return {nid: {'found': True, 'name': nid} for nid in node_ids}

        path_command._lookup_repeater_names = capture_lookup
        await path_command._decode_path("01,5f (2 hops)")
        assert len(captured) == 1
        assert captured[0] == ['01', '5F']


@pytest.mark.unit
class TestPathCommandExtractPathUsesRoutingInfo:
    """Test _extract_path_from_recent_messages prefers routing_info.path_nodes when present."""

    @pytest.fixture
    def path_command(self, mock_bot):
        """Create a PathCommand instance."""
        return PathCommand(mock_bot)

    @pytest.mark.asyncio
    async def test_uses_routing_info_path_nodes_when_present(self, path_command, mock_bot):
        """When message has routing_info with path_nodes, use them directly (no _decode_path)."""
        path_command._current_message = MeshMessage(
            content="path",
            path="0102,5f7e (2 hops via FLOOD)",
            routing_info={
                'path_length': 2,
                'path_nodes': ['0102', '5f7e'],
                'path_hex': '01025f7e',
                'bytes_per_hop': 2,
                'route_type': 'FLOOD',
            },
        )
        captured = []

        async def capture_lookup(node_ids, lookup_func=None):
            captured.append(list(node_ids))
            return {nid: {'found': True, 'name': nid} for nid in node_ids}

        path_command._lookup_repeater_names = capture_lookup
        decode_path_called = []

        async def track_decode(path_input):
            decode_path_called.append(path_input)
            return "decode_path_result"

        path_command._decode_path = track_decode
        path_command.translate = lambda key, **kwargs: f"msg:{kwargs.get('node_id', kwargs.get('name', key))}"

        result = await path_command._extract_path_from_recent_messages()

        assert len(captured) == 1
        assert captured[0] == ['0102', '5F7E']
        assert len(decode_path_called) == 0, "Should not call _decode_path when routing_info.path_nodes present"
        assert '0102' in result and '5F7E' in result

    @pytest.mark.asyncio
    async def test_direct_connection_when_routing_info_path_length_zero(self, path_command, mock_bot):
        """When routing_info.path_length is 0, return direct connection message."""
        path_command._current_message = MeshMessage(
            content="path",
            path="Direct via FLOOD",
            routing_info={'path_length': 0, 'path_nodes': [], 'route_type': 'FLOOD'},
        )
        path_command.translate = lambda key, **kwargs: "Direct connection" if "direct" in key.lower() else key
        result = await path_command._extract_path_from_recent_messages()
        assert "direct" in result.lower()
