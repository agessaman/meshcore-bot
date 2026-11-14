"""Tests for the {path_names} placeholder (modules/path_names.py)."""

import configparser
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from modules.models import MeshMessage
from modules.path_inference import NodeSelection
from modules.path_names import format_path_names, template_uses_path_names
from modules.utils import format_keyword_response_with_placeholders


def _repeater(name, public_key):
    return {"name": name, "public_key": public_key, "device_type": "repeater",
            "last_heard": None, "last_seen": None, "latitude": None, "longitude": None,
            "city": None, "state": None, "country": None}


DIRECTORY = {
    "A1B2": [_repeater("Black Hills", "a1b2" + "0" * 60)],
    "C3D4": [_repeater("Kalama Summit 🏔", "c3d4" + "0" * 60)],
    "E5F6": [_repeater("Kelso G3", "e5f6" + "0" * 60)],
    # Two repeaters share this prefix and neither graph nor geography can pick one.
    "0102": [_repeater("Alpha", "0102" + "1" * 60), _repeater("Beta", "0102" + "2" * 60)],
}


def _lookup(node_id):
    return DIRECTORY.get(node_id, [])


def _bot(**path_settings):
    config = configparser.ConfigParser()
    config.add_section("Path_Command")
    config.set("Path_Command", "graph_based_validation", "false")
    config.set("Path_Command", "geographic_guessing_enabled", "false")
    for key, value in path_settings.items():
        config.set("Path_Command", key, value)
    return SimpleNamespace(config=config, db_manager=MagicMock(), logger=Mock(), mesh_graph=None)


def _message(nodes, bytes_per_hop):
    routing_info = {"path_length": len(nodes), "path_nodes": list(nodes)}
    if bytes_per_hop:
        routing_info["bytes_per_hop"] = bytes_per_hop
    return MeshMessage(content="t", sender_id="Alice", hops=len(nodes), routing_info=routing_info)


@pytest.mark.unit
class TestFormatPathNames:
    def test_confident_multibyte_hops_become_short_names(self):
        msg = _message(["a1b2", "c3d4", "e5f6"], 2)
        assert format_path_names(_bot(), msg, lookup_func=_lookup) == "BlackHls▸KalamaSum▸KelsoG3"

    def test_unresolved_collision_keeps_its_hex_prefix(self):
        msg = _message(["a1b2", "0102", "e5f6"], 2)
        assert format_path_names(_bot(), msg, lookup_func=_lookup) == "BlackHls▸0102▸KelsoG3"

    def test_unknown_prefix_keeps_its_hex_prefix(self):
        msg = _message(["a1b2", "ffff"], 2)
        assert format_path_names(_bot(), msg, lookup_func=_lookup) == "BlackHls▸ffff"

    def test_one_byte_path_is_never_looked_up(self):
        lookup = Mock(side_effect=_lookup)
        msg = _message(["a1", "c3"], 1)
        assert format_path_names(_bot(), msg, lookup_func=lookup) == "a1▸c3"
        lookup.assert_not_called()

    def test_direct_message(self):
        msg = MeshMessage(content="t", sender_id="Alice", hops=0, routing_info={"path_length": 0})
        assert format_path_names(_bot(), msg) == "Direct"

    def test_unknown_route_is_empty(self):
        assert format_path_names(_bot(), MeshMessage(content="t", sender_id="Alice")) == ""
        assert format_path_names(_bot(), None) == ""

    def test_lookup_failure_falls_back_to_hex(self):
        msg = _message(["a1b2", "c3d4"], 2)
        boom = Mock(side_effect=RuntimeError("db gone"))
        assert format_path_names(_bot(), msg, lookup_func=boom) == "a1b2▸c3d4"

    @pytest.mark.parametrize("confidence, expected", [(0.85, "0102"), (0.95, "Alpha")])
    def test_resolved_collision_needs_min_confidence(self, confidence, expected):
        winner = DIRECTORY["0102"][0]
        selection = NodeSelection("resolved", winner, confidence, "graph", 2, DIRECTORY["0102"])
        msg = _message(["0102"], 2)
        with patch("modules.path_inference.select_node_repeater", return_value=selection):
            assert format_path_names(_bot(), msg, lookup_func=_lookup) == expected

    def test_settings_shape_the_output(self):
        bot = _bot(path_names_separator='" > "', path_names_max_name_length="5")
        msg = _message(["a1b2", "e5f6"], 2)
        assert format_path_names(bot, msg, lookup_func=_lookup) == "BlcHl > KlsG3"

    def test_byte_budget(self):
        bot = _bot(path_names_max_bytes="20")
        msg = _message(["a1b2", "c3d4", "e5f6", "a1b2", "c3d4", "e5f6"], 2)
        assert len(format_path_names(bot, msg, lookup_func=_lookup).encode()) <= 20

    def test_bad_settings_fall_back_to_defaults(self):
        bot = _bot(path_names_max_bytes="lots", path_names_min_confidence="high")
        msg = _message(["a1b2"], 2)
        assert format_path_names(bot, msg, lookup_func=_lookup) == "BlackHls"


@pytest.mark.unit
class TestRealDatabase:
    """The candidate query against a real complete_contact_tracking table."""

    def test_names_from_the_database(self, mock_bot):
        for name, key in [("Black Hills", "a1b2" + "0" * 60), ("Kelso G3", "e5f6" + "0" * 60),
                          ("Old Companion", "c3d4" + "0" * 60)]:
            role = "companion" if name == "Old Companion" else "repeater"
            mock_bot.db_manager.execute_update(
                "INSERT INTO complete_contact_tracking (public_key, name, role, device_type) "
                "VALUES (?, ?, ?, 'repeater')", (key, name, role))
        mock_bot.mesh_graph = None
        msg = _message(["a1b2", "c3d4", "e5f6"], 2)
        # c3d4 is only a companion, so it is not a repeater candidate.
        assert format_path_names(mock_bot, msg) == "BlackHls▸c3d4▸KelsoG3"


@pytest.mark.unit
class TestWiring:
    def test_template_detection(self):
        assert template_uses_path_names("ack {path_names}")
        assert template_uses_path_names('{path_names|prefix_if_nonempty:" via "}')
        assert not template_uses_path_names("ack {path}")
        assert not template_uses_path_names(None)

    def test_keyword_response(self):
        bot = _bot()
        msg = _message(["a1b2", "e5f6"], 2)
        with patch("modules.utils.format_path_names", return_value="BlackHls▸KelsoG3") as fmt:
            out = format_keyword_response_with_placeholders("via {path_names}", msg, bot)
        assert out == "via BlackHls▸KelsoG3"
        fmt.assert_called_once()

    def test_keyword_response_skips_the_lookup_when_unused(self):
        msg = _message(["a1b2"], 2)
        with patch("modules.utils.format_path_names") as fmt:
            format_keyword_response_with_placeholders("ack {sender}", msg, _bot())
        fmt.assert_not_called()

    def test_test_command_renders_path_names(self):
        from tests.unit.test_template_reference import _test_command

        cmd = _test_command()
        msg = _message(["a1b2", "e5f6"], 2)
        with patch("modules.commands.test_command.format_path_names", return_value="BlackHls▸KelsoG3"):
            assert cmd.format_response(msg, "ack {path_names}") == "ack BlackHls▸KelsoG3"
        with patch("modules.commands.test_command.format_path_names") as fmt:
            cmd.format_response(msg, "ack {path}")
        fmt.assert_not_called()
