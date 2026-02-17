#!/usr/bin/env python3
"""
Unit tests for PathCommand graph-based selection
"""

import pytest
from unittest.mock import Mock
from modules.commands.path_command import PathCommand
from tests.helpers import create_test_repeater


@pytest.mark.unit
class TestPathCommandGraphSelection:
    """Test PathCommand._select_repeater_by_graph functionality."""
    
    @pytest.fixture
    def path_command(self, mock_bot, populated_mesh_graph):
        """Create a PathCommand instance with graph enabled."""
        mock_bot.mesh_graph = populated_mesh_graph
        command = PathCommand(mock_bot)
        command.graph_based_validation = True
        command.min_edge_observations = 1
        command.graph_use_bidirectional = True
        command.graph_use_hop_position = True
        command.graph_multi_hop_enabled = True
        command.graph_multi_hop_max_hops = 2
        command.graph_prefer_stored_keys = True
        command.star_bias_multiplier = 2.5
        return command
    
    def test_select_repeater_direct_edge(self, path_command, populated_mesh_graph):
        """Test selecting repeater with direct graph edge."""
        repeaters = [
            create_test_repeater('7e', 'Repeater 7e', public_key='7e' * 32)
        ]
        path_context = ['01', '7e', '86']
        
        repeater, confidence, method = path_command._select_repeater_by_graph(
            repeaters, '7e', path_context
        )
        
        assert repeater is not None
        assert confidence > 0.0
        assert method == 'graph'
    
    def test_select_repeater_stored_key_bonus(self, path_command, populated_mesh_graph):
        """Test that stored public keys provide bonus."""
        # Add edge with stored public key
        stored_key = '7e' * 32
        populated_mesh_graph.add_edge('01', '7e', to_public_key=stored_key)
        
        repeaters = [
            create_test_repeater('7e', 'Repeater 7e', public_key=stored_key),
            create_test_repeater('7e', 'Other 7e', public_key='7f' * 32)
        ]
        path_context = ['01', '7e', '86']
        
        repeater, confidence, method = path_command._select_repeater_by_graph(
            repeaters, '7e', path_context
        )
        
        assert repeater is not None
        assert repeater['name'] == 'Repeater 7e'  # Should prefer stored key match
    
    def test_select_repeater_star_bias(self, path_command, populated_mesh_graph):
        """Test that starred repeaters get score boost."""
        repeaters = [
            create_test_repeater('7e', 'Starred Repeater', is_starred=True),
            create_test_repeater('7e', 'Regular Repeater', is_starred=False)
        ]
        path_context = ['01', '7e', '86']
        
        repeater, confidence, method = path_command._select_repeater_by_graph(
            repeaters, '7e', path_context
        )
        
        assert repeater is not None
        assert repeater['is_starred'] is True
    
    def test_select_repeater_multihop(self, path_command, populated_mesh_graph):
        """Test multi-hop inference when direct edge has low confidence."""
        # Create 2-hop path: 01 -> 7e -> 86 -> e0
        populated_mesh_graph.add_edge('01', '7e')
        populated_mesh_graph.add_edge('7e', '86')
        populated_mesh_graph.add_edge('86', 'e0')
        
        repeaters = [
            create_test_repeater('86', 'Intermediate Repeater')
        ]
        path_context = ['01', '86', 'e0']
        
        repeater, confidence, method = path_command._select_repeater_by_graph(
            repeaters, '86', path_context
        )
        
        assert repeater is not None
        assert method == 'graph_multihop' or method == 'graph'
    
    def test_select_repeater_no_graph(self, path_command):
        """Test that selection returns None when graph is disabled."""
        path_command.graph_based_validation = False
        
        repeaters = [create_test_repeater('7e', 'Repeater')]
        path_context = ['01', '7e', '86']
        
        repeater, confidence, method = path_command._select_repeater_by_graph(
            repeaters, '7e', path_context
        )
        
        assert repeater is None
        assert confidence == 0.0
        assert method is None
    
    def test_select_repeater_no_candidates(self, path_command, populated_mesh_graph):
        """Test that selection returns None when no valid candidates."""
        repeaters = [
            create_test_repeater('99', 'Unknown Repeater')  # No graph edges
        ]
        path_context = ['01', '99', '86']
        
        repeater, confidence, method = path_command._select_repeater_by_graph(
            repeaters, '99', path_context
        )
        
        assert repeater is None or confidence == 0.0
    
    def test_select_repeater_confidence_conversion(self, path_command, populated_mesh_graph):
        """Test that graph scores are converted to confidence properly."""
        repeaters = [
            create_test_repeater('7e', 'Repeater 7e')
        ]
        path_context = ['01', '7e', '86']
        
        repeater, confidence, method = path_command._select_repeater_by_graph(
            repeaters, '7e', path_context
        )
        
        assert 0.0 <= confidence <= 1.0
    
    def test_select_repeater_hop_position(self, path_command, mesh_graph):
        """Test that hop position is used when enabled."""
        path_command.graph_use_hop_position = True
        
        # Add edge with specific hop position (add multiple times to establish avg)
        mesh_graph.add_edge('01', '7e', hop_position=1)
        mesh_graph.add_edge('01', '7e', hop_position=1)  # Keep avg at 1.0
        
        repeaters = [create_test_repeater('7e', 'Repeater')]
        path_context = ['01', '7e', '86']  # 7e is at position 1
        
        repeater, confidence, method = path_command._select_repeater_by_graph(
            repeaters, '7e', path_context
        )
        
        assert repeater is not None
