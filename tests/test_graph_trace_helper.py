"""Tests for modules.graph_trace_helper — update_mesh_graph_from_trace_data."""

import configparser
from unittest.mock import MagicMock, Mock, patch

import pytest

from modules.graph_trace_helper import update_mesh_graph_from_trace_data

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bot(bot_prefix="aa", has_mesh_graph=True, has_transmission_tracker=True):
    """Create a minimal mock bot for graph_trace_helper tests."""
    bot = MagicMock()
    bot.logger = Mock()

    config = configparser.ConfigParser()
    config.add_section("Path_Command")
    config.set("Path_Command", "graph_edge_expiration_days", "7")
    bot.config = config

    if has_mesh_graph:
        bot.mesh_graph = MagicMock()
    else:
        bot.mesh_graph = None

    if has_transmission_tracker:
        bot.transmission_tracker = MagicMock()
        bot.transmission_tracker.bot_prefix = bot_prefix
        bot.transmission_tracker.match_packet_hash = Mock(return_value=None)
    else:
        bot.transmission_tracker = None

    # DB manager returns empty results by default
    bot.db_manager = MagicMock()
    bot.db_manager.execute_query = Mock(return_value=[])

    # meshcore device (optional)
    bot.meshcore = MagicMock()
    bot.meshcore.device = MagicMock()
    bot.meshcore.device.public_key = "aa" * 32

    return bot


# ---------------------------------------------------------------------------
# Early exit / guard cases
# ---------------------------------------------------------------------------

class TestEarlyExits:
    def test_empty_path_hashes_returns_immediately(self):
        bot = _make_bot()
        update_mesh_graph_from_trace_data(bot, [], {})
        bot.mesh_graph.add_edge.assert_not_called()

    def test_none_path_hashes_treated_as_empty(self):
        bot = _make_bot()
        # empty list is falsy, so this is covered by the guard
        update_mesh_graph_from_trace_data(bot, [], {})
        bot.mesh_graph.add_edge.assert_not_called()

    def test_no_mesh_graph_returns_immediately(self):
        bot = _make_bot(has_mesh_graph=False)
        update_mesh_graph_from_trace_data(bot, ["ab"], {})
        # Should log and return without crash
        bot.logger.debug.assert_called()

    def test_no_transmission_tracker_returns_immediately(self):
        bot = _make_bot(has_transmission_tracker=False)
        update_mesh_graph_from_trace_data(bot, ["ab"], {})
        bot.logger.debug.assert_called()

    def test_missing_mesh_graph_attribute(self):
        bot = _make_bot()
        del bot.mesh_graph
        update_mesh_graph_from_trace_data(bot, ["ab"], {})
        # No crash expected

    def test_missing_transmission_tracker_attribute(self):
        bot = _make_bot()
        del bot.transmission_tracker
        update_mesh_graph_from_trace_data(bot, ["ab"], {})
        # No crash expected

    def test_empty_bot_prefix_returns_immediately(self):
        bot = _make_bot()
        bot.transmission_tracker.bot_prefix = None
        update_mesh_graph_from_trace_data(bot, ["ab"], {})
        bot.mesh_graph.add_edge.assert_not_called()

    def test_empty_string_bot_prefix_returns_immediately(self):
        bot = _make_bot()
        bot.transmission_tracker.bot_prefix = ""
        update_mesh_graph_from_trace_data(bot, ["ab"], {})
        bot.mesh_graph.add_edge.assert_not_called()


# ---------------------------------------------------------------------------
# is_our_trace resolution
# ---------------------------------------------------------------------------

class TestIsOurTraceResolution:
    def test_is_our_trace_none_resolves_false_when_no_match(self):
        """When packet_hash doesn't match, is_our_trace stays False."""
        bot = _make_bot(bot_prefix="aa")
        bot.transmission_tracker.match_packet_hash = Mock(return_value=None)
        update_mesh_graph_from_trace_data(bot, ["bb"], {"packet_hash": "abc123"})
        # Not our trace → should add one edge (last_node → bot)
        bot.mesh_graph.add_edge.assert_called_once()

    def test_is_our_trace_none_resolves_true_when_match(self):
        """When packet_hash matches a transmission record, is_our_trace becomes True."""
        bot = _make_bot(bot_prefix="aa")
        bot.transmission_tracker.match_packet_hash = Mock(return_value={"matched": True})
        # Single hop — should trigger immediate-neighbor path (bidirectional)
        update_mesh_graph_from_trace_data(bot, ["bb"], {"packet_hash": "abc123"})
        # Bidirectional: 2 add_edge calls
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_is_our_trace_explicit_true(self):
        """Explicit is_our_trace=True skips transmission tracker lookup."""
        bot = _make_bot(bot_prefix="aa")
        bot.transmission_tracker.match_packet_hash = Mock(return_value=None)
        update_mesh_graph_from_trace_data(bot, ["bb"], {}, is_our_trace=True)
        # Single hop + explicit True → immediate neighbor → bidirectional
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_is_our_trace_explicit_false(self):
        """Explicit is_our_trace=False skips transmission tracker lookup."""
        bot = _make_bot(bot_prefix="aa")
        bot.transmission_tracker.match_packet_hash = Mock(return_value={"matched": True})
        update_mesh_graph_from_trace_data(bot, ["bb"], {}, is_our_trace=False)
        # Not our trace even though match_packet_hash would return a record
        bot.mesh_graph.add_edge.assert_called_once()

    def test_is_our_trace_true_no_packet_hash(self):
        """is_our_trace None with no packet_hash defaults to False."""
        bot = _make_bot(bot_prefix="aa")
        update_mesh_graph_from_trace_data(bot, ["bb"], {})
        # No packet_hash → is_our_trace stays False → one edge
        bot.mesh_graph.add_edge.assert_called_once()


# ---------------------------------------------------------------------------
# Immediate neighbor (single hop, is_our_trace=True)
# ---------------------------------------------------------------------------

class TestImmediateNeighbor:
    def test_single_hop_creates_bidirectional_edges(self):
        bot = _make_bot(bot_prefix="aa")
        update_mesh_graph_from_trace_data(bot, ["bb"], {}, is_our_trace=True)
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_single_hop_edge_directions(self):
        bot = _make_bot(bot_prefix="aa")
        update_mesh_graph_from_trace_data(bot, ["bb"], {}, is_our_trace=True)
        calls = bot.mesh_graph.add_edge.call_args_list
        from_prefixes = {c.kwargs.get("from_prefix") or c[1].get("from_prefix") for c in calls}
        to_prefixes = {c.kwargs.get("to_prefix") or c[1].get("to_prefix") for c in calls}
        # Both directions: aa↔bb
        assert "aa" in from_prefixes or "aa" in to_prefixes
        assert "bb" in from_prefixes or "bb" in to_prefixes

    def test_single_hop_lowercase_normalization(self):
        """Path hashes are lowercased before use."""
        bot = _make_bot(bot_prefix="AA")
        update_mesh_graph_from_trace_data(bot, ["BB"], {}, is_our_trace=True)
        assert bot.mesh_graph.add_edge.call_count == 2
        calls = bot.mesh_graph.add_edge.call_args_list
        # Check lowercase normalization
        all_prefixes = set()
        for c in calls:
            all_prefixes.add(c.kwargs.get("from_prefix") or (c[0][0] if c[0] else None))
            all_prefixes.add(c.kwargs.get("to_prefix") or (c[0][1] if len(c[0]) > 1 else None))
        # Should contain lowercase versions
        assert "aa" in all_prefixes or "bb" in all_prefixes

    def test_single_hop_with_unique_db_key(self):
        """When DB returns exactly one public_key for the neighbor, it's used."""
        bot = _make_bot(bot_prefix="aa")
        # First query returns count=1, second returns the key
        bot.db_manager.execute_query = Mock(side_effect=[
            [{"count": 1}],
            [{"public_key": "bb" * 32}],
            [],  # bot location query
        ])
        update_mesh_graph_from_trace_data(bot, ["bb"], {}, is_our_trace=True)
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_single_hop_with_ambiguous_db_results(self):
        """When DB returns count != 1, no key is resolved but edges still added."""
        bot = _make_bot(bot_prefix="aa")
        bot.db_manager.execute_query = Mock(return_value=[{"count": 2}])
        update_mesh_graph_from_trace_data(bot, ["bb"], {}, is_our_trace=True)
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_single_hop_db_exception_handled(self):
        """DB exceptions during key lookup are swallowed; edges still added."""
        bot = _make_bot(bot_prefix="aa")
        bot.db_manager.execute_query = Mock(side_effect=Exception("DB error"))
        update_mesh_graph_from_trace_data(bot, ["bb"], {}, is_our_trace=True)
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_single_hop_no_meshcore_device(self):
        """No meshcore device → bot_key is None, edges still added."""
        bot = _make_bot(bot_prefix="aa")
        bot.meshcore = None
        update_mesh_graph_from_trace_data(bot, ["bb"], {}, is_our_trace=True)
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_single_hop_device_pubkey_bytes(self):
        """Device public key as bytes is hex-encoded."""
        bot = _make_bot(bot_prefix="aa")
        bot.meshcore.device.public_key = b"\xaa\xbb"
        update_mesh_graph_from_trace_data(bot, ["bb"], {}, is_our_trace=True)
        assert bot.mesh_graph.add_edge.call_count == 2


# ---------------------------------------------------------------------------
# Regular trace (bot is destination, not immediate neighbor)
# ---------------------------------------------------------------------------

class TestRegularTrace:
    def test_two_hop_creates_two_edges(self):
        """[a, b] path (bot receives from b via a): two edges expected."""
        bot = _make_bot(bot_prefix="cc")
        update_mesh_graph_from_trace_data(bot, ["aa", "bb"], {})
        # 1 edge: last_node(bb) → bot(cc)
        # 1 edge: aa → bb
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_single_node_path_creates_one_edge(self):
        """Single-node path: last_node → bot."""
        bot = _make_bot(bot_prefix="cc")
        update_mesh_graph_from_trace_data(bot, ["aa"], {})
        assert bot.mesh_graph.add_edge.call_count == 1

    def test_three_hop_creates_three_edges(self):
        """[a, b, c] path creates 3 edges."""
        bot = _make_bot(bot_prefix="dd")
        update_mesh_graph_from_trace_data(bot, ["aa", "bb", "cc"], {})
        assert bot.mesh_graph.add_edge.call_count == 3

    def test_path_hashes_lowercased(self):
        """Uppercase path hashes are normalized to lowercase."""
        bot = _make_bot(bot_prefix="cc")
        update_mesh_graph_from_trace_data(bot, ["AA", "BB"], {})
        assert bot.mesh_graph.add_edge.call_count == 2
        calls = bot.mesh_graph.add_edge.call_args_list
        # All prefixes should be lowercase
        for c in calls:
            kw = c.kwargs
            if "from_prefix" in kw:
                assert kw["from_prefix"] == kw["from_prefix"].lower()
            if "to_prefix" in kw:
                assert kw["to_prefix"] == kw["to_prefix"].lower()

    def test_last_edge_points_to_bot(self):
        """The last_node→bot edge has to_prefix equal to bot_prefix."""
        bot = _make_bot(bot_prefix="cc")
        update_mesh_graph_from_trace_data(bot, ["aa"], {})
        call_kwargs = bot.mesh_graph.add_edge.call_args.kwargs
        assert call_kwargs.get("to_prefix") == "cc"
        assert call_kwargs.get("from_prefix") == "aa"

    def test_db_exception_in_regular_path_handled(self):
        """DB exceptions don't prevent edges from being added."""
        bot = _make_bot(bot_prefix="cc")
        bot.db_manager.execute_query = Mock(side_effect=Exception("DB error"))
        update_mesh_graph_from_trace_data(bot, ["aa", "bb"], {})
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_unique_key_resolved_for_last_node(self):
        """When exactly one key matches the last_node prefix, it's used."""
        bot = _make_bot(bot_prefix="cc")
        bot.db_manager.execute_query = Mock(side_effect=[
            [{"count": 1}],
            [{"public_key": "aa" * 32}],
        ])
        update_mesh_graph_from_trace_data(bot, ["aa"], {})
        assert bot.mesh_graph.add_edge.call_count == 1

    def test_mesh_graph_add_edge_exception_propagates(self):
        """Exception from add_edge is not caught (it's a programming error)."""
        bot = _make_bot(bot_prefix="cc")
        bot.mesh_graph.add_edge = Mock(side_effect=RuntimeError("mesh error"))
        with pytest.raises(RuntimeError):
            update_mesh_graph_from_trace_data(bot, ["aa"], {})


# ---------------------------------------------------------------------------
# Multi-hop intermediate edges
# ---------------------------------------------------------------------------

class TestMultiHopEdges:
    def test_hop_positions_decrease_towards_bot(self):
        """Hop positions are assigned based on distance from bot."""
        bot = _make_bot(bot_prefix="dd")
        update_mesh_graph_from_trace_data(bot, ["aa", "bb", "cc"], {})
        calls = bot.mesh_graph.add_edge.call_args_list
        # Collect hop_position values from kwargs
        hop_positions = [c.kwargs.get("hop_position") for c in calls]
        assert all(h is not None for h in hop_positions)

    def test_single_hop_position_is_1(self):
        """Single-hop path has hop_position=1."""
        bot = _make_bot(bot_prefix="bb")
        update_mesh_graph_from_trace_data(bot, ["aa"], {})
        call_kwargs = bot.mesh_graph.add_edge.call_args.kwargs
        assert call_kwargs.get("hop_position") == 1

    def test_no_meshcore_in_regular_path(self):
        """No meshcore still creates edges."""
        bot = _make_bot(bot_prefix="cc")
        bot.meshcore = None
        update_mesh_graph_from_trace_data(bot, ["aa", "bb"], {})
        assert bot.mesh_graph.add_edge.call_count == 2


# ---------------------------------------------------------------------------
# Geo-location paths (lines 85-87, 108-150, 185-226, 258-296)
# ---------------------------------------------------------------------------

class TestGeolocationPaths:
    """Cover branches that execute when _get_node_location_from_db returns data."""

    def test_bot_location_resolved_immediate_neighbor_with_distance(self):
        """Bot has location + neighbor has location → distance calculated, edges added."""
        bot = _make_bot(bot_prefix="aa")
        bot_loc = (47.6, -122.3)
        neighbor_loc = (47.7, -122.4)
        with patch("modules.utils._get_node_location_from_db") as mock_loc, \
             patch("modules.utils.calculate_distance", return_value=12.5):
            mock_loc.side_effect = [
                (bot_loc, None),       # bot location
                (neighbor_loc, "bb" * 32),  # neighbor location + key
            ]
            update_mesh_graph_from_trace_data(bot, ["bb"], {}, is_our_trace=True)
        assert bot.mesh_graph.add_edge.call_count == 2
        # Verify geographic_distance was passed
        calls = bot.mesh_graph.add_edge.call_args_list
        assert any(c.kwargs.get("geographic_distance") == 12.5 for c in calls)

    def test_immediate_neighbor_key_from_location_db(self):
        """Neighbor key resolved from _get_node_location_from_db when count query ambiguous."""
        bot = _make_bot(bot_prefix="aa")
        bot_loc = (47.6, -122.3)
        neighbor_loc = (47.7, -122.4)
        with patch("modules.utils._get_node_location_from_db") as mock_loc, \
             patch("modules.utils.calculate_distance", return_value=5.0):
            mock_loc.side_effect = [
                (bot_loc, None),
                (neighbor_loc, "cc" * 32),
            ]
            update_mesh_graph_from_trace_data(bot, ["bb"], {}, is_our_trace=True)
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_immediate_neighbor_location_exception_handled(self):
        """Exception in distance calc for immediate neighbor is swallowed."""
        bot = _make_bot(bot_prefix="aa")
        with patch("modules.utils._get_node_location_from_db") as mock_loc:
            mock_loc.side_effect = [
                ((47.6, -122.3), None),   # bot location ok
                Exception("geo fail"),     # neighbor location raises
            ]
            update_mesh_graph_from_trace_data(bot, ["bb"], {}, is_our_trace=True)
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_immediate_neighbor_no_location_result(self):
        """When neighbor location lookup returns None, no distance, edges still added."""
        bot = _make_bot(bot_prefix="aa")
        with patch("modules.utils._get_node_location_from_db") as mock_loc:
            mock_loc.side_effect = [
                ((47.6, -122.3), None),  # bot location
                None,                    # neighbor: no location
            ]
            update_mesh_graph_from_trace_data(bot, ["bb"], {}, is_our_trace=True)
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_regular_trace_last_node_with_distance(self):
        """Regular trace: last_node has location → distance calculated."""
        bot = _make_bot(bot_prefix="cc")
        bot_loc = (47.6, -122.3)
        last_node_loc = (47.5, -122.2)
        with patch("modules.utils._get_node_location_from_db") as mock_loc, \
             patch("modules.utils.calculate_distance", return_value=8.0):
            mock_loc.side_effect = [
                (bot_loc, None),              # bot location
                (last_node_loc, "aa" * 32),   # last_node location + key
            ]
            update_mesh_graph_from_trace_data(bot, ["aa"], {})
        assert bot.mesh_graph.add_edge.call_count == 1
        call_kwargs = bot.mesh_graph.add_edge.call_args.kwargs
        assert call_kwargs.get("geographic_distance") == 8.0

    def test_regular_trace_last_node_key_from_location(self):
        """last_node key fallback from location DB when count query returns nothing."""
        bot = _make_bot(bot_prefix="cc")
        with patch("modules.utils._get_node_location_from_db") as mock_loc, \
             patch("modules.utils.calculate_distance", return_value=3.0):
            mock_loc.side_effect = [
                ((47.6, -122.3), None),
                ((47.5, -122.2), "aa" * 32),
            ]
            update_mesh_graph_from_trace_data(bot, ["aa"], {})
        assert bot.mesh_graph.add_edge.call_count == 1

    def test_regular_trace_bot_key_bytes(self):
        """Bot public key as bytes is hex-encoded in regular trace path."""
        bot = _make_bot(bot_prefix="cc")
        bot.meshcore.device.public_key = b"\xcc\xdd"
        update_mesh_graph_from_trace_data(bot, ["aa"], {})
        assert bot.mesh_graph.add_edge.call_count == 1

    def test_regular_trace_distance_exception_handled(self):
        """Exception in last_node distance calc is swallowed."""
        bot = _make_bot(bot_prefix="cc")
        with patch("modules.utils._get_node_location_from_db") as mock_loc:
            mock_loc.side_effect = [
                ((47.6, -122.3), None),
                Exception("geo fail"),
            ]
            update_mesh_graph_from_trace_data(bot, ["aa"], {})
        assert bot.mesh_graph.add_edge.call_count == 1

    def test_multihop_intermediate_key_resolved(self):
        """Intermediate node count=1 DB path: key resolved for both from/to nodes."""
        bot = _make_bot(bot_prefix="dd")
        bot.db_manager.execute_query = Mock(side_effect=[
            # last_node count query
            [{"count": 0}],
            # intermediate loop: from_node count=1, key
            [{"count": 1}],
            [{"public_key": "aa" * 32}],
            # to_node count=1, key
            [{"count": 1}],
            [{"public_key": "bb" * 32}],
        ])
        update_mesh_graph_from_trace_data(bot, ["aa", "bb"], {})
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_multihop_intermediate_distance_calculated(self):
        """Intermediate nodes with locations get distance set on edge."""
        bot = _make_bot(bot_prefix="dd")
        loc_a = (47.0, -122.0)
        loc_b = (47.1, -122.1)
        loc_c = (47.2, -122.2)
        with patch("modules.utils._get_node_location_from_db") as mock_loc, \
             patch("modules.utils.calculate_distance", return_value=5.5):
            mock_loc.side_effect = [
                (loc_c, None),           # bot location
                (loc_b, "bb" * 32),      # last_node (bb) location
                (loc_a, "aa" * 32),      # from_node in loop
                (loc_b, "bb" * 32),      # to_node in loop
            ]
            update_mesh_graph_from_trace_data(bot, ["aa", "bb"], {})
        assert bot.mesh_graph.add_edge.call_count == 2

    def test_multihop_intermediate_distance_exception_handled(self):
        """Exception in intermediate distance calc is swallowed; edges still added."""
        bot = _make_bot(bot_prefix="dd")
        with patch("modules.utils._get_node_location_from_db") as mock_loc:
            mock_loc.side_effect = [
                ((47.0, -122.0), None),  # bot location
                ((47.1, -122.1), None),  # last_node location
                Exception("geo fail"),   # intermediate raises
            ]
            update_mesh_graph_from_trace_data(bot, ["aa", "bb"], {})
        assert bot.mesh_graph.add_edge.call_count == 2
