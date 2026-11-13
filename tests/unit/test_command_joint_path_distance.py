#!/usr/bin/env python3
"""
Unit tests for the test command's hop resolution.

``{path_distance}`` and ``{firstlast_distance}`` depend on which repeater each
hop is taken to be. [Path_Command] path_inference_mode picks how that is decided:
'greedy' (the default) resolves each hop on its own, 'joint' resolves the whole
path at once via modules.path_inference.
"""

import pytest

from modules.commands.test_command import TestCommand as MeshTestCommand
from modules.utils import calculate_distance
from tests.conftest import mock_message

BOT = (47.60, -122.33)

# Two repeaters share the 1-byte prefix AA, one south of the middle hop and one
# north of it, next to the bot. A path of AA -> BB -> AA must be two of them.
NODES = {
    'aa11': ('AA-south', 47.30, -122.35),
    'aa33': ('AA-north', 47.45, -122.30),
    'bb11': ('BB-mid', 47.20, -122.38),
}


def _row(key, name, lat, lon):
    return {
        'name': name,
        'public_key': key + '0' * (64 - len(key)),
        'device_type': 'repeater',
        'role': 'repeater',
        'last_heard': '2026-10-26T12:00:00',
        'last_seen': '2026-10-26T12:00:00',
        'last_advert_timestamp': '2026-10-26T12:00:00',
        'latitude': lat,
        'longitude': lon,
        'city': None,
        'state': None,
        'country': None,
        'advert_count': 5,
        'signal_strength': None,
        'hop_count': None,
        'is_starred': 0,
    }


ROWS = [_row(key, *value) for key, value in NODES.items()]


@pytest.fixture
def build_command(mock_bot):
    """A TestCommand whose repeater queries are answered from ROWS."""
    for section in ('Bot', 'Path_Command', 'Test_Command'):
        if not mock_bot.config.has_section(section):
            mock_bot.config.add_section(section)
    mock_bot.config.set('Bot', 'bot_latitude', str(BOT[0]))
    mock_bot.config.set('Bot', 'bot_longitude', str(BOT[1]))
    mock_bot.config.set('Test_Command', 'distance_unit', 'km')
    # A Mock() mesh_graph would answer get_edge with another Mock.
    mock_bot.mesh_graph = None

    def execute_query(_sql, params=()):
        prefix = (params[0] if params else '').rstrip('%').lower()
        return [row for row in ROWS if row['public_key'].startswith(prefix)]

    mock_bot.db_manager.execute_query = execute_query

    def _build(mode=None):
        if mode is None:
            mock_bot.config.remove_option('Path_Command', 'path_inference_mode')
        else:
            mock_bot.config.set('Path_Command', 'path_inference_mode', mode)
        return MeshTestCommand(mock_bot)

    return _build


def _three_hop_message():
    return mock_message(
        content="test",
        routing_info={'path_length': 3, 'path_nodes': ['AA', 'BB', 'AA']},
    )


@pytest.mark.unit
class TestModeSelection:
    """The flag decides, and anything unrecognized stays on the old behavior."""

    def test_defaults_to_greedy(self, build_command):
        assert build_command().path_inference_mode == 'greedy'

    def test_joint_is_read_from_config(self, build_command):
        assert build_command('joint').path_inference_mode == 'joint'

    def test_unknown_mode_falls_back_to_greedy(self, build_command):
        assert build_command('sideways').path_inference_mode == 'greedy'

    def test_mode_is_case_insensitive(self, build_command):
        assert build_command('JOINT').path_inference_mode == 'joint'


@pytest.mark.unit
class TestGreedyKeepsItsBehavior:
    """Characterization: why the flag exists."""

    def test_a_repeated_prefix_resolves_to_one_node_twice(self, build_command):
        cmd = build_command('greedy')
        resolved = cmd._resolve_hop_locations(['AA', 'BB', 'AA'])
        assert resolved[0] == resolved[2], (
            "greedy is expected to reuse one node for both AA hops; if this now "
            "differs, the per-hop path is no longer the behavior the flag guards"
        )

    def test_still_uses_the_per_hop_lookup(self, build_command):
        cmd = build_command('greedy')
        calls = []
        original = cmd._lookup_repeater_location
        cmd._lookup_repeater_location = lambda node_id, path_context=None: (
            calls.append(node_id) or original(node_id, path_context=path_context)
        )
        cmd._resolve_hop_locations(['AA', 'BB'])
        assert calls == ['AA', 'BB']


@pytest.mark.unit
class TestJointResolution:
    """Joint mode resolves the path as a whole."""

    def test_a_repeated_prefix_resolves_to_two_nodes(self, build_command):
        cmd = build_command('joint')
        resolved = cmd._resolve_hop_locations(['AA', 'BB', 'AA'])
        assert all(location is not None for location in resolved)
        assert resolved[0] != resolved[2]

    def test_path_distance_counts_each_hop_once(self, build_command):
        cmd = build_command('joint')
        resolved = cmd._resolve_hop_locations(['AA', 'BB', 'AA'])
        expected = sum(
            calculate_distance(a[0], a[1], b[0], b[1])
            for a, b in zip(resolved, resolved[1:], strict=False)
        )
        assert cmd._calculate_path_distance(_three_hop_message()) == (
            f"{expected:.1f}km (2 segs)"
        )

    def test_path_distance_differs_from_the_greedy_total(self, build_command):
        greedy = build_command('greedy')._calculate_path_distance(_three_hop_message())
        joint = build_command('joint')._calculate_path_distance(_three_hop_message())
        assert greedy != joint

    def test_firstlast_distance_uses_the_jointly_chosen_endpoints(self, build_command):
        cmd = build_command('joint')
        resolved = cmd._resolve_hop_locations(['AA', 'BB', 'AA'])
        first, last = resolved[0], resolved[-1]
        expected = calculate_distance(first[0], first[1], last[0], last[1])
        assert cmd._calculate_firstlast_distance(_three_hop_message()) == f"{expected:.1f}km"

    def test_an_unlisted_hop_is_skipped_not_bridged(self, build_command):
        # A prefix no stored repeater matches contributes no coordinate, so it is
        # counted as a gap rather than stretching the neighbouring hops across it.
        cmd = build_command('joint')
        resolved = cmd._resolve_hop_locations(['AA', 'FF', 'BB'])
        assert resolved[1] is None
        message = mock_message(
            content="test",
            routing_info={'path_length': 3, 'path_nodes': ['AA', 'FF', 'BB']},
        )
        assert "1 no-loc" in cmd._calculate_path_distance(message)

    def test_direct_message_is_still_not_applicable(self, build_command):
        cmd = build_command('joint')
        message = mock_message(content="test", routing_info={'path_length': 0, 'path_nodes': []})
        assert cmd._calculate_path_distance(message) == "N/A"
        assert cmd._calculate_firstlast_distance(message) == "N/A"


@pytest.mark.unit
class TestJointFallback:
    """A failure in the joint path must not cost the command its reply."""

    def test_falls_back_to_the_per_hop_lookup(self, build_command):
        cmd = build_command('joint')
        cmd._joint_hop_locations = lambda node_ids: (_ for _ in ()).throw(RuntimeError("boom"))
        resolved = cmd._resolve_hop_locations(['AA', 'BB', 'AA'])
        # The greedy path answers instead, reusing one node as it always has.
        assert resolved[0] == resolved[2]

    def test_missing_database_resolves_nothing(self, build_command):
        cmd = build_command('joint')
        cmd.bot.db_manager = None
        assert cmd._resolve_hop_locations(['AA', 'BB']) == [None, None]


@pytest.mark.unit
class TestOneResolutionPerReply:
    """Both distance placeholders read the same path, so it is resolved once."""

    def test_repeated_calls_reuse_the_resolution(self, build_command):
        cmd = build_command('joint')
        calls = []
        original = cmd._joint_hop_locations
        cmd._joint_hop_locations = lambda node_ids: (
            calls.append(tuple(node_ids)) or original(node_ids)
        )
        message = _three_hop_message()
        cmd._calculate_path_distance(message)
        cmd._calculate_firstlast_distance(message)
        assert calls == [('AA', 'BB', 'AA')]

    def test_a_different_path_is_resolved_again(self, build_command):
        cmd = build_command('joint')
        calls = []
        original = cmd._joint_hop_locations
        cmd._joint_hop_locations = lambda node_ids: (
            calls.append(tuple(node_ids)) or original(node_ids)
        )
        cmd._resolve_hop_locations(['AA', 'BB'])
        cmd._resolve_hop_locations(['BB', 'AA'])
        assert calls == [('AA', 'BB'), ('BB', 'AA')]

    async def test_execute_clears_the_cache(self, build_command):
        cmd = build_command('joint')
        cmd._resolve_hop_locations(['AA', 'BB'])
        assert cmd._hop_locations_cache is not None
        cmd.handle_keyword_match = lambda _message: _noop()
        await cmd.execute(_three_hop_message())
        assert cmd._hop_locations_cache is None


async def _noop():
    return True
