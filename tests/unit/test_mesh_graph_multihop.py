#!/usr/bin/env python3
"""
Unit tests for MeshGraph multi-hop inference
"""

import pytest
from tests.helpers import create_test_edge


@pytest.mark.unit
class TestMeshGraphMultiHop:
    """Test MeshGraph multi-hop inference functionality."""
    
    def test_find_intermediate_nodes_direct_edge(self, mesh_graph):
        """Test that direct edges are not returned as intermediate nodes."""
        mesh_graph.add_edge('01', '7e')
        
        candidates = mesh_graph.find_intermediate_nodes('01', '7e')
        # Direct edge exists, so no intermediate nodes should be returned
        assert len(candidates) == 0
    
    def test_find_intermediate_nodes_2hop_path(self, mesh_graph):
        """Test finding intermediate nodes in a 2-hop path."""
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('7e', '86')
        
        candidates = mesh_graph.find_intermediate_nodes('01', '86')
        assert len(candidates) > 0
        assert candidates[0][0] == '7e'  # Intermediate node
        assert candidates[0][1] > 0.0  # Has score
    
    def test_find_intermediate_nodes_3hop_path(self, mesh_graph):
        """Test finding intermediate nodes in a 3-hop path."""
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('7e', '86')
        mesh_graph.add_edge('86', 'e0')
        
        candidates = mesh_graph.find_intermediate_nodes('01', 'e0', max_hops=3)
        assert len(candidates) > 0
        # Should return '86' (the node before destination in 3-hop path)
        assert candidates[0][0] == '86'
        assert candidates[0][1] > 0.0
    
    def test_find_intermediate_nodes_no_path(self, mesh_graph):
        """Test that no candidates are returned when no path exists."""
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('86', 'e0')  # Disconnected components
        
        candidates = mesh_graph.find_intermediate_nodes('01', 'e0')
        assert len(candidates) == 0
    
    def test_find_intermediate_nodes_min_observations(self, mesh_graph):
        """Test that min_observations filter is applied."""
        mesh_graph.add_edge('01', '7e')  # Only 1 observation
        mesh_graph.add_edge('7e', '86')
        mesh_graph.add_edge('7e', '86')  # Ensure 7e->86 has 2 observations
        
        # With min_observations=3, should find no candidates (both edges need 3+)
        candidates = mesh_graph.find_intermediate_nodes('01', '86', min_observations=3)
        assert len(candidates) == 0
        
        # Add more observations to both edges
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('01', '7e')  # Now 01->7e has 3 observations
        mesh_graph.add_edge('7e', '86')  # Now 7e->86 has 3 observations
        
        candidates = mesh_graph.find_intermediate_nodes('01', '86', min_observations=3)
        assert len(candidates) > 0
    
    def test_find_intermediate_nodes_bidirectional_bonus(self, mesh_graph):
        """Test that bidirectional paths get higher scores."""
        # Unidirectional path
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('7e', '86')
        candidates_uni = mesh_graph.find_intermediate_nodes('01', '86')
        
        # Add reverse edges (bidirectional)
        mesh_graph.add_edge('7e', '01')
        mesh_graph.add_edge('86', '7e')
        candidates_bi = mesh_graph.find_intermediate_nodes('01', '86')
        
        assert len(candidates_bi) > 0
        assert candidates_bi[0][1] > candidates_uni[0][1]
    
    def test_find_intermediate_nodes_multiple_candidates(self, mesh_graph):
        """Test finding multiple intermediate node candidates."""
        # Path 1: 01 -> 7e -> 86
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('7e', '86')
        
        # Path 2: 01 -> 7a -> 86
        mesh_graph.add_edge('01', '7a')
        mesh_graph.add_edge('7a', '86')
        
        candidates = mesh_graph.find_intermediate_nodes('01', '86')
        assert len(candidates) >= 2
        
        # Should be sorted by score (highest first)
        scores = [c[1] for c in candidates]
        assert scores == sorted(scores, reverse=True)
    
    def test_find_intermediate_nodes_3hop_score_reduction(self, mesh_graph):
        """Test that 3-hop paths have reduced scores."""
        # 2-hop path
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('7e', '86')
        candidates_2hop = mesh_graph.find_intermediate_nodes('01', '86', max_hops=2)
        
        # 3-hop path
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('7e', '86')
        mesh_graph.add_edge('86', 'e0')
        candidates_3hop = mesh_graph.find_intermediate_nodes('01', 'e0', max_hops=3)
        
        # 3-hop should have lower score due to 0.8 multiplier
        if len(candidates_2hop) > 0 and len(candidates_3hop) > 0:
            # Both paths exist, 3-hop should be lower
            assert candidates_3hop[0][1] < candidates_2hop[0][1]
    
    def test_find_intermediate_nodes_max_hops_limit(self, mesh_graph):
        """Test that max_hops parameter limits search depth."""
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('7e', '86')
        mesh_graph.add_edge('86', 'e0')
        
        # With max_hops=2, should not find 3-hop path
        candidates = mesh_graph.find_intermediate_nodes('01', 'e0', max_hops=2)
        assert len(candidates) == 0
        
        # With max_hops=3, should find it
        candidates = mesh_graph.find_intermediate_nodes('01', 'e0', max_hops=3)
        assert len(candidates) > 0
    
    def test_find_intermediate_nodes_weakest_link_scoring(self, mesh_graph):
        """Test that path score uses weakest link (minimum confidence)."""
        # Create path where one edge has low confidence
        mesh_graph.add_edge('01', '7e')  # 1 observation
        mesh_graph.add_edge('7e', '86')
        mesh_graph.add_edge('7e', '86')
        mesh_graph.add_edge('7e', '86')  # 3 observations
        
        candidates = mesh_graph.find_intermediate_nodes('01', '86')
        assert len(candidates) > 0
        
        # Score should be limited by the weaker edge (01->7e)
        score = candidates[0][1]
        assert score < 1.0  # Should be less than perfect due to weak link
    
    def test_find_intermediate_nodes_self_loop_prevention(self, mesh_graph):
        """Test that paths don't loop back to source."""
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('7e', '01')  # Loop back
        mesh_graph.add_edge('7e', '86')
        
        candidates = mesh_graph.find_intermediate_nodes('01', '86')
        # Should still find valid path through 7e
        assert len(candidates) > 0
        assert candidates[0][0] == '7e'
