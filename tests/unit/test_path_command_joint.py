#!/usr/bin/env python3
"""
Unit tests for the PathCommand under [Path_Command] path_inference_mode = joint.

In joint mode the whole path is resolved at once and ``repeater_info`` is keyed by
hop index rather than node id. Keying by node id is what made the per-hop path
show one repeater at both halves of a repeated 1-byte prefix, and count one leg
of the route twice in {path_distance}.
"""

import pytest

from modules.commands.path_command import PathCommand

BOT = (47.60, -122.33)
SENDER = (47.10, -122.40)

# Two repeaters collide on the 1-byte prefix AA; one middle hop on BB.
AA_SOUTH = ('AA-south', 'aa11', 47.30, -122.35)
AA_NORTH = ('AA-north', 'aa33', 47.45, -122.30)
BB_MID = ('BB-mid', 'bb11', 47.20, -122.38)


def repeater(name, key_prefix, lat, lon):
    return {
        'name': name,
        'public_key': key_prefix.lower() + '0' * (64 - len(key_prefix)),
        'device_type': 'repeater',
        'role': 'repeater',
        'last_heard': '2026-10-26T12:00:00',
        'last_seen': '2026-10-26T12:00:00',
        'last_advert_timestamp': '2026-10-26T12:00:00',
        'is_active': True,
        'latitude': lat,
        'longitude': lon,
        'advert_count': 5,
        'is_starred': False,
    }


def lookup_from(*nodes):
    """A lookup_func answering by prefix from the given repeater tuples."""
    rows = [repeater(*node) for node in nodes]

    def _lookup(node_id):
        prefix = node_id.lower()
        return [row for row in rows if row['public_key'].startswith(prefix)]

    return _lookup


@pytest.fixture
def path_command(mock_bot):
    for section in ('Bot', 'Path_Command'):
        if not mock_bot.config.has_section(section):
            mock_bot.config.add_section(section)
    # A Mock() mesh_graph would answer get_edge with another Mock.
    mock_bot.mesh_graph = None
    mock_bot.meshcore.contacts = {}

    cmd = PathCommand(mock_bot)
    cmd.path_inference_mode = 'joint'
    cmd.geographic_guessing_enabled = True
    cmd.bot_latitude, cmd.bot_longitude = BOT
    cmd._get_sender_location = lambda message=None: SENDER
    return cmd


def names_by_hop(repeater_info, count):
    return [repeater_info.get(index, {}).get('name') for index in range(count)]


def _render_line(key, **kwargs):
    """Minimal translate: render the hop lines _format_path_response asks for."""
    if key == 'commands.path.truncation':
        return '...'
    if key.startswith('commands.path.node_'):
        return f"{kwargs.get('node_id', '')}:{kwargs.get('name', '')}"
    return key


@pytest.mark.unit
class TestModeSelection:
    """The flag is read from config and can be flipped on the instance."""

    def test_defaults_to_greedy(self, mock_bot):
        mock_bot.config.remove_option('Path_Command', 'path_inference_mode')
        assert PathCommand(mock_bot).path_inference_mode == 'greedy'

    def test_joint_is_read_from_config(self, mock_bot):
        if not mock_bot.config.has_section('Path_Command'):
            mock_bot.config.add_section('Path_Command')
        mock_bot.config.set('Path_Command', 'path_inference_mode', 'joint')
        assert PathCommand(mock_bot).path_inference_mode == 'joint'

    def test_unknown_mode_falls_back_to_greedy(self, mock_bot):
        if not mock_bot.config.has_section('Path_Command'):
            mock_bot.config.add_section('Path_Command')
        mock_bot.config.set('Path_Command', 'path_inference_mode', 'sideways')
        assert PathCommand(mock_bot).path_inference_mode == 'greedy'

    def test_the_instance_attribute_reaches_the_engine_config(self, path_command):
        assert path_command._inference_config().path_inference_mode == 'joint'
        path_command.path_inference_mode = 'greedy'
        assert path_command._inference_config().path_inference_mode == 'greedy'


@pytest.mark.unit
class TestHopEntry:
    """The readers cope with either keying."""

    def test_hop_index_wins_when_present(self, path_command):
        info = {0: {'name': 'by-index'}, 'AA': {'name': 'by-node-id'}}
        assert path_command._hop_entry(info, 0, 'AA')['name'] == 'by-index'

    def test_falls_back_to_the_node_id(self, path_command):
        info = {'AA': {'name': 'by-node-id'}}
        assert path_command._hop_entry(info, 0, 'AA')['name'] == 'by-node-id'

    def test_missing_hop_is_empty(self, path_command):
        assert path_command._hop_entry({}, 2, 'ZZ') == {}


@pytest.mark.unit
class TestJointResolution:
    """A repeated prefix is two hops, and gets two nodes."""

    async def test_keyed_by_hop_index(self, path_command):
        info = await path_command._lookup_repeater_names(
            ['AA', 'BB', 'AA'], lookup_func=lookup_from(AA_SOUTH, AA_NORTH, BB_MID),
        )
        assert sorted(info) == [0, 1, 2]

    async def test_repeated_prefix_resolves_to_two_repeaters(self, path_command):
        info = await path_command._lookup_repeater_names(
            ['AA', 'BB', 'AA'], lookup_func=lookup_from(AA_SOUTH, AA_NORTH, BB_MID),
        )
        names = names_by_hop(info, 3)
        assert names[1] == 'BB-mid'
        assert names[0] != names[2]
        assert set(names) == {'AA-south', 'AA-north', 'BB-mid'}

    async def test_the_reply_shows_both_hops(self, path_command):
        path_command.translate = _render_line
        node_ids = ['AA', 'BB', 'AA']
        info = await path_command._lookup_repeater_names(
            node_ids, lookup_func=lookup_from(AA_SOUTH, AA_NORTH, BB_MID),
        )
        reply = path_command._format_path_response(node_ids, info)
        assert 'AA-south' in reply
        assert 'AA-north' in reply

    async def test_a_resolved_collision_carries_its_confidence(self, path_command):
        info = await path_command._lookup_repeater_names(
            ['AA', 'BB', 'AA'], lookup_func=lookup_from(AA_SOUTH, AA_NORTH, BB_MID),
        )
        assert info[0]['joint_guess'] is True
        assert 0.0 < info[0]['confidence'] <= 1.0

    async def test_a_lone_candidate_is_not_a_guess(self, path_command):
        info = await path_command._lookup_repeater_names(
            ['BB'], lookup_func=lookup_from(BB_MID),
        )
        assert info[0]['found'] is True
        assert 'joint_guess' not in info[0]
        assert 'confidence' not in info[0]

    async def test_distance_counts_each_hop_once(self, path_command):
        node_ids = ['AA', 'BB', 'AA']
        info = await path_command._lookup_repeater_names(
            node_ids, lookup_func=lookup_from(AA_SOUTH, AA_NORTH, BB_MID),
        )
        joint_km = path_command._calculate_path_distance_km(node_ids, info)

        path_command.path_inference_mode = 'greedy'
        greedy_info = await path_command._lookup_repeater_names(
            node_ids, lookup_func=lookup_from(AA_SOUTH, AA_NORTH, BB_MID),
        )
        greedy_km = path_command._calculate_path_distance_km(node_ids, greedy_info)

        assert joint_km is not None
        assert greedy_km is not None
        assert joint_km != pytest.approx(greedy_km)

    async def test_coordinates_are_carried_for_the_distance(self, path_command):
        info = await path_command._lookup_repeater_names(
            ['BB'], lookup_func=lookup_from(BB_MID),
        )
        assert (info[0]['latitude'], info[0]['longitude']) == (BB_MID[2], BB_MID[3])


@pytest.mark.unit
class TestJointRejections:
    """What a hop looks like when no node fits it."""

    async def test_colliding_prefix_with_no_fit_reports_a_collision(self, path_command):
        # Both candidates for CC sit on another continent, so neither is placeable.
        far_one = ('CC-far1', 'cc11', 39.70, -104.99)
        far_two = ('CC-far2', 'cc22', 39.75, -105.02)
        info = await path_command._lookup_repeater_names(
            ['BB', 'CC'], lookup_func=lookup_from(BB_MID, far_one, far_two),
        )
        assert info[1]['collision'] is True
        assert info[1]['matches'] == 2
        assert info[1]['node_id'] == 'CC'

    async def test_a_rejected_lone_candidate_is_not_named(self, path_command):
        far = ('CC-far', 'cc11', 39.70, -104.99)
        info = await path_command._lookup_repeater_names(
            ['BB', 'CC'], lookup_func=lookup_from(BB_MID, far),
        )
        assert info[1] == {'found': False, 'node_id': 'CC'}

    async def test_a_collision_blocks_the_distance(self, path_command):
        far_one = ('CC-far1', 'cc11', 39.70, -104.99)
        far_two = ('CC-far2', 'cc22', 39.75, -105.02)
        node_ids = ['BB', 'CC']
        info = await path_command._lookup_repeater_names(
            node_ids, lookup_func=lookup_from(BB_MID, far_one, far_two),
        )
        assert path_command._calculate_path_distance_km(node_ids, info) is None

    async def test_a_prefix_with_no_candidate_at_all_is_unknown(self, path_command):
        info = await path_command._lookup_repeater_names(
            ['BB', 'FF'], lookup_func=lookup_from(BB_MID),
        )
        assert info[1]['found'] is False


@pytest.mark.unit
class TestJointFallback:
    """A failure in joint mode must still produce a reply."""

    async def test_falls_back_to_the_per_hop_lookup(self, path_command):
        async def boom(_node_ids, _lookup_func=None):
            raise RuntimeError("boom")

        path_command._lookup_repeater_names_joint = boom
        info = await path_command._lookup_repeater_names(
            ['AA', 'BB', 'AA'], lookup_func=lookup_from(AA_SOUTH, AA_NORTH, BB_MID),
        )
        # The per-hop path keys by node id, and gives both AA hops one entry.
        assert sorted(info) == ['AA', 'BB']
