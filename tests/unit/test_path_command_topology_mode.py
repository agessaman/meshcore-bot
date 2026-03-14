#!/usr/bin/env python3
"""
Tests for PathCommand topology engine shadow/new modes.
"""

from unittest.mock import Mock, MagicMock

import pytest

from modules.commands.path_command import PathCommand
from tests.helpers import create_test_repeater


@pytest.mark.unit
@pytest.mark.asyncio
async def test_path_command_shadow_mode_records_comparison(mock_bot, populated_mesh_graph):
    mock_bot.mesh_graph = populated_mesh_graph
    mock_bot.topology_engine = Mock()
    mock_bot.topology_engine.select_for_hop.return_value = {
        "repeater": None,
        "confidence": 0.3,
        "method": "topology_viterbi_ghost",
        "is_topology_guess": True,
    }
    mock_bot.topology_engine.maybe_record_shadow_comparison = Mock()

    command = PathCommand(mock_bot)
    command.topology_engine_mode = "shadow"
    command.graph_based_validation = True
    command.min_edge_observations = 1

    candidate_a = create_test_repeater("7e", "A", public_key="7e" * 32)
    candidate_b = create_test_repeater("7e", "B", public_key=("7e" * 31) + "7f")

    def lookup(node_id):
        if node_id.lower() == "7e":
            return [candidate_a, candidate_b]
        if node_id.lower() == "01":
            return [create_test_repeater("01", "Start", public_key="01" * 32)]
        if node_id.lower() == "86":
            return [create_test_repeater("86", "End", public_key="86" * 32)]
        return []

    await command._lookup_repeater_names(["01", "7e", "86"], lookup_func=lookup)
    assert mock_bot.topology_engine.maybe_record_shadow_comparison.called


@pytest.mark.unit
@pytest.mark.asyncio
async def test_path_command_new_mode_prefers_topology_choice(mock_bot, populated_mesh_graph):
    mock_bot.mesh_graph = populated_mesh_graph
    model_choice = create_test_repeater("7e", "ModelChoice", public_key=("7e" * 31) + "7f")
    mock_bot.topology_engine = Mock()
    mock_bot.topology_engine.select_for_hop.return_value = {
        "repeater": model_choice,
        "confidence": 0.85,
        "method": "topology_viterbi",
        "is_topology_guess": True,
    }
    mock_bot.topology_engine.maybe_record_shadow_comparison = Mock()

    command = PathCommand(mock_bot)
    command.topology_engine_mode = "new"
    command.graph_based_validation = True
    command.min_edge_observations = 1

    candidate_a = create_test_repeater("7e", "LegacyChoice", public_key="7e" * 32)
    candidate_b = create_test_repeater("7e", "ModelChoice", public_key=("7e" * 31) + "7f")

    def lookup(node_id):
        if node_id.lower() == "7e":
            return [candidate_a, candidate_b]
        if node_id.lower() == "01":
            return [create_test_repeater("01", "Start", public_key="01" * 32)]
        if node_id.lower() == "86":
            return [create_test_repeater("86", "End", public_key="86" * 32)]
        return []

    info = await command._lookup_repeater_names(["01", "7e", "86"], lookup_func=lookup)
    assert info["7e"]["found"] is True
    assert info["7e"]["public_key"] == model_choice["public_key"]


@pytest.mark.unit
def test_path_command_formats_topology_selection_with_method(mock_bot):
    mock_bot.translator = MagicMock()
    mock_bot.translator.translate = lambda key, **kwargs: f"{key} {kwargs}"

    command = PathCommand(mock_bot)
    repeater_info = {
        "7E": {
            "found": True,
            "collision": False,
            "name": "ModelChoice",
            "confidence": 0.92,
            "topology_guess": True,
            "selection_method": "topology_viterbi",
        }
    }
    formatted = command._format_path_response(["7E"], repeater_info)
    assert "topology_viterbi" in formatted


@pytest.mark.unit
def test_path_command_reload_from_config_updates_topology_mode(mock_bot):
    command = PathCommand(mock_bot)
    assert command.topology_engine_mode == "legacy"

    mock_bot.config.set("Path_Command", "topology_engine_mode", "new")
    mock_bot.config.set("Path_Command", "topology_shadow_sample_rate", "0.25")
    mock_bot.config.set("Path_Command", "topology_ghost_enabled", "false")
    mock_bot.config.set("Path_Command", "topology_ghost_min_confidence", "0.55")
    command.reload_from_config()

    assert command.topology_engine_mode == "new"
    assert command.topology_shadow_sample_rate == 0.25
    assert command.topology_ghost_enabled is False
    assert command.topology_ghost_min_confidence == 0.55
