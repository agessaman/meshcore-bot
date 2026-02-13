#!/usr/bin/env python3
"""
Unit tests for PathCommand graph-based selection logic
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime
from modules.commands.path_command import PathCommand
from tests.helpers import create_test_repeater, create_test_edge, populate_test_graph


@pytest.mark.unit
class TestPathCommandGraphSelection:
    """Test PathCommand._select_repeater_by_graph method."""
    
    def test_select_repeater_by_graph_no_graph(self, mock_bot):
        """Test when graph_based_validation is False."""
        # Disable graph validation
        mock_bot.config.set('Path_Command', 'graph_based_validation', 'false')
        path_cmd = PathCommand(mock_bot)
        
        repeaters = [create_test_repeater('01', 'Test Repeater')]
        result = path_cmd._select_repeater_by_graph(repeaters, '01', ['01'])
        
        assert result == (None, 0.0, None)
    
    def test_select_repeater_by_graph_no_mesh_graph(self, mock_bot):
        """Test when mesh_graph is None."""
        mock_bot.mesh_graph = None
        path_cmd = PathCommand(mock_bot)
        
        repeaters = [create_test_repeater('01', 'Test Repeater')]
        result = path_cmd._select_repeater_by_graph(repeaters, '01', ['01'])
        
        assert result == (None, 0.0, None)
    
    def test_select_repeater_by_graph_no_context(self, mock_bot, mesh_graph):
        """Test when node_id not in path_context."""
        mock_bot.mesh_graph = mesh_graph
        path_cmd = PathCommand(mock_bot)
        
        repeaters = [create_test_repeater('99', 'Test Repeater')]
        result = path_cmd._select_repeater_by_graph(repeaters, '99', ['01', '7e', '86'])
        
        assert result == (None, 0.0, None)
    
    def test_select_repeater_by_graph_direct_edge(self, mock_bot, mesh_graph):
        """Test selection with strong direct edge."""
        mock_bot.mesh_graph = mesh_graph
        
        # Create strong edge: 01 -> 7e
        mesh_graph.add_edge('01', '7e')
        for _ in range(10):
            mesh_graph.add_edge('01', '7e')  # 11 total observations
        
        path_cmd = PathCommand(mock_bot)
        
        # Create repeaters with matching prefix
        repeaters = [
            create_test_repeater('7e', 'Test Repeater 7e', public_key='7e' * 32)
        ]
        
        result = path_cmd._select_repeater_by_graph(repeaters, '7e', ['01', '7e', '86'])
        
        assert result[0] is not None  # Should select a repeater
        assert result[1] > 0.7  # High confidence
        assert result[2] == 'graph'  # Direct edge method
    
    def test_select_repeater_by_graph_stored_public_key_bonus(self, mock_bot, mesh_graph):
        """Test stored public key bonus."""
        mock_bot.mesh_graph = mesh_graph
        
        # Create edge with stored public key
        public_key = '7e' * 32  # 64 hex chars
        mesh_graph.add_edge('01', '7e', from_public_key='01' * 32, to_public_key=public_key)
        for _ in range(5):
            mesh_graph.add_edge('01', '7e')
        
        path_cmd = PathCommand(mock_bot)
        
        # Create repeater with matching public key
        repeaters = [
            create_test_repeater('7e', 'Test Repeater', public_key=public_key),
            create_test_repeater('7e', 'Other Repeater', public_key='aa' * 32)  # Different key
        ]
        
        result = path_cmd._select_repeater_by_graph(repeaters, '7e', ['01', '7e', '86'])
        
        assert result[0] is not None
        # Should select the repeater with matching public key
        assert result[0]['public_key'] == public_key
        assert result[1] > 0.5  # Should have good confidence with stored key bonus
    
    def test_select_repeater_by_graph_star_bias(self, mock_bot, mesh_graph):
        """Test star bias multiplier application."""
        mock_bot.mesh_graph = mesh_graph
        
        # Create edges for both repeaters
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('01', '7a')
        
        path_cmd = PathCommand(mock_bot)
        
        # Create one starred and one non-starred repeater
        repeaters = [
            create_test_repeater('7e', 'Starred Repeater', is_starred=True),
            create_test_repeater('7a', 'Regular Repeater', is_starred=False)
        ]
        
        result = path_cmd._select_repeater_by_graph(repeaters, '7e', ['01', '7e'])
        
        assert result[0] is not None
        # Starred repeater should be selected even if both have similar graph scores
        assert result[0]['is_starred'] is True
    
    def test_select_repeater_by_graph_multi_hop(self, mock_bot, mesh_graph):
        """Test multi-hop inference when direct edge has low confidence."""
        mock_bot.mesh_graph = mesh_graph
        
        # Create 2-hop path: 01 -> 7e -> 86 (no direct 01 -> 86)
        # Need at least 3 observations per edge for min_edge_observations
        for _ in range(3):
            mesh_graph.add_edge('01', '7e')
            mesh_graph.add_edge('7e', '86')
        
        path_cmd = PathCommand(mock_bot)
        
        # Create repeater that's the intermediate node
        repeaters = [
            create_test_repeater('7e', 'Intermediate Repeater', public_key='7e' * 32)
        ]
        
        # Try to select 7e when path is 01 -> 7e -> 86
        result = path_cmd._select_repeater_by_graph(repeaters, '7e', ['01', '7e', '86'])
        
        assert result[0] is not None
        # Should use graph method (direct edge exists)
        assert result[2] in ('graph', 'graph_multihop')
    
    def test_select_repeater_by_graph_hop_position(self, mock_bot, mesh_graph):
        """Test hop position validation."""
        mock_bot.mesh_graph = mesh_graph
        
        # Create edge with avg_hop_position = 1.0
        # Need at least 3 observations for min_edge_observations
        for _ in range(3):
            mesh_graph.add_edge('01', '7e', hop_position=1)
        
        path_cmd = PathCommand(mock_bot)
        path_cmd.graph_use_hop_position = True
        
        repeaters = [create_test_repeater('7e', 'Test Repeater')]
        
        # Path where 7e is at position 1
        result = path_cmd._select_repeater_by_graph(repeaters, '7e', ['01', '7e', '86'])
        
        assert result[0] is not None
        assert result[1] > 0.0
    
    def test_select_repeater_by_graph_multiple_candidates(self, mock_bot, mesh_graph):
        """Test selection from multiple candidates."""
        mock_bot.mesh_graph = mesh_graph
        
        # Create edges with different strengths
        mesh_graph.add_edge('01', '7e')
        for _ in range(10):
            mesh_graph.add_edge('01', '7e')  # Strong edge
        
        mesh_graph.add_edge('01', '7a')
        for _ in range(2):
            mesh_graph.add_edge('01', '7a')  # Weaker edge
        
        path_cmd = PathCommand(mock_bot)
        
        repeaters = [
            create_test_repeater('7e', 'Strong Edge Repeater'),
            create_test_repeater('7a', 'Weak Edge Repeater')
        ]
        
        result = path_cmd._select_repeater_by_graph(repeaters, '7e', ['01', '7e'])
        
        assert result[0] is not None
        # Should select the one with stronger edge (7e)
        assert result[0]['name'] == 'Strong Edge Repeater'
        assert result[1] > 0.5
    
    def test_select_repeater_by_graph_confidence_conversion(self, mock_bot, mesh_graph):
        """Test graph score to confidence conversion."""
        mock_bot.mesh_graph = mesh_graph
        
        # Create very strong edge
        mesh_graph.add_edge('01', '7e')
        for _ in range(20):
            mesh_graph.add_edge('01', '7e')
        
        path_cmd = PathCommand(mock_bot)
        
        repeaters = [create_test_repeater('7e', 'Test Repeater')]
        result = path_cmd._select_repeater_by_graph(repeaters, '7e', ['01', '7e'])
        
        assert result[0] is not None
        # Confidence should be capped at 1.0
        assert 0.0 <= result[1] <= 1.0
    
    def test_select_repeater_by_graph_star_bias_exceeds_one(self, mock_bot, mesh_graph):
        """Test star bias can exceed 1.0 but confidence is normalized."""
        mock_bot.mesh_graph = mesh_graph
        
        mesh_graph.add_edge('01', '7e')
        for _ in range(5):
            mesh_graph.add_edge('01', '7e')
        
        path_cmd = PathCommand(mock_bot)
        path_cmd.star_bias_multiplier = 2.5  # High multiplier
        
        # Create starred repeater
        repeaters = [create_test_repeater('7e', 'Starred Repeater', is_starred=True)]
        
        result = path_cmd._select_repeater_by_graph(repeaters, '7e', ['01', '7e'])
        
        assert result[0] is not None
        # Confidence should still be capped appropriately
        assert 0.0 <= result[1] <= 1.0
    
    def test_select_repeater_by_graph_prefix_extraction(self, mock_bot, mesh_graph):
        """Test prefix extraction from public_key."""
        mock_bot.mesh_graph = mesh_graph
        
        # Create edge
        mesh_graph.add_edge('01', '7e')
        
        path_cmd = PathCommand(mock_bot)
        
        # Repeater with public key starting with '7e'
        public_key = '7e' + '00' * 31  # 7e prefix
        repeaters = [create_test_repeater('7e', 'Test Repeater', public_key=public_key)]
        
        result = path_cmd._select_repeater_by_graph(repeaters, '7e', ['01', '7e'])
        
        assert result[0] is not None
    
    def test_select_repeater_by_graph_missing_public_key(self, mock_bot, mesh_graph):
        """Test handling when public_key is missing."""
        mock_bot.mesh_graph = mesh_graph
        
        mesh_graph.add_edge('01', '7e')
        
        path_cmd = PathCommand(mock_bot)
        
        # Repeater without public_key
        repeater = create_test_repeater('7e', 'Test Repeater')
        del repeater['public_key']  # Remove public key
        
        result = path_cmd._select_repeater_by_graph([repeater], '7e', ['01', '7e'])
        
        # Should skip this repeater (no prefix to match)
        assert result == (None, 0.0, None) or result[0] is None
