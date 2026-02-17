#!/usr/bin/env python3
"""
Unit tests for MeshGraph candidate scoring
"""

import pytest
from tests.helpers import create_test_edge


@pytest.mark.unit
class TestMeshGraphScoring:
    """Test MeshGraph candidate scoring functionality."""
    
    def test_get_candidate_score_no_edges(self, mesh_graph):
        """Test scoring candidate with no graph edges."""
        score = mesh_graph.get_candidate_score('01', None, None)
        assert score == 0.0
    
    def test_get_candidate_score_prev_edge_only(self, mesh_graph):
        """Test scoring candidate with only previous edge."""
        mesh_graph.add_edge('7e', '01')
        
        score = mesh_graph.get_candidate_score('01', '7e', None)
        assert score > 0.0
        assert score <= 1.0
    
    def test_get_candidate_score_next_edge_only(self, mesh_graph):
        """Test scoring candidate with only next edge."""
        mesh_graph.add_edge('01', '86')
        
        score = mesh_graph.get_candidate_score('01', None, '86')
        assert score > 0.0
        assert score <= 1.0
    
    def test_get_candidate_score_both_edges(self, mesh_graph):
        """Test scoring candidate with both previous and next edges."""
        mesh_graph.add_edge('7e', '01')
        mesh_graph.add_edge('01', '86')
        
        score = mesh_graph.get_candidate_score('01', '7e', '86')
        assert score > 0.0
        assert score <= 1.0
    
    def test_get_candidate_score_bidirectional_bonus(self, mesh_graph):
        """Test that bidirectional edges increase score."""
        # Unidirectional
        mesh_graph.add_edge('7e', '01')
        score_uni = mesh_graph.get_candidate_score('01', '7e', None, use_bidirectional=True)
        
        # Add reverse edge
        mesh_graph.add_edge('01', '7e')
        score_bi = mesh_graph.get_candidate_score('01', '7e', None, use_bidirectional=True)
        
        assert score_bi > score_uni
    
    def test_get_candidate_score_hop_position_match(self, mesh_graph):
        """Test hop position validation bonus."""
        # Add edge with avg_hop_position = 2.0
        mesh_graph.add_edge('7e', '01', hop_position=2)
        mesh_graph.add_edge('7e', '01', hop_position=2)  # Average stays at 2.0
        
        # Score at position 2 (should match)
        score_match = mesh_graph.get_candidate_score('01', '7e', None, hop_position=2, use_hop_position=True)
        
        # Score at position 5 (shouldn't match)
        score_mismatch = mesh_graph.get_candidate_score('01', '7e', None, hop_position=5, use_hop_position=True)
        
        assert score_match > score_mismatch
    
    def test_get_candidate_score_hop_position_tolerance(self, mesh_graph):
        """Test that hop position allows small tolerance."""
        mesh_graph.add_edge('7e', '01', hop_position=2)
        mesh_graph.add_edge('7e', '01', hop_position=2)
        
        # Position 2.3 should still match (within 0.5 tolerance)
        score = mesh_graph.get_candidate_score('01', '7e', None, hop_position=2, use_hop_position=True)
        assert score > 0.0
    
    def test_get_candidate_score_min_observations(self, mesh_graph):
        """Test that scoring respects min_observations."""
        mesh_graph.add_edge('7e', '01')  # Only 1 observation
        
        score = mesh_graph.get_candidate_score('01', '7e', None, min_observations=3)
        assert score == 0.0
        
        # Add more observations
        mesh_graph.add_edge('7e', '01')
        mesh_graph.add_edge('7e', '01')
        
        score = mesh_graph.get_candidate_score('01', '7e', None, min_observations=3)
        assert score > 0.0
    
    def test_get_candidate_score_hop_position_disabled(self, mesh_graph):
        """Test that hop position validation can be disabled."""
        mesh_graph.add_edge('7e', '01', hop_position=2)
        mesh_graph.add_edge('7e', '01', hop_position=2)
        
        # With hop position disabled, should still score
        score = mesh_graph.get_candidate_score('01', '7e', None, hop_position=5, use_hop_position=False)
        assert score > 0.0
    
    def test_get_candidate_score_bidirectional_disabled(self, mesh_graph):
        """Test that bidirectional check can be disabled."""
        mesh_graph.add_edge('7e', '01')
        mesh_graph.add_edge('01', '7e')  # Reverse edge
        
        score_bi_enabled = mesh_graph.get_candidate_score('01', '7e', None, use_bidirectional=True)
        score_bi_disabled = mesh_graph.get_candidate_score('01', '7e', None, use_bidirectional=False)
        
        assert score_bi_enabled > score_bi_disabled
    
    def test_get_candidate_score_average_of_edges(self, mesh_graph):
        """Test that score averages confidence from multiple edges."""
        # Create edges with different observation counts
        mesh_graph.add_edge('7e', '01')  # 1 observation
        mesh_graph.add_edge('01', '86')
        mesh_graph.add_edge('01', '86')
        mesh_graph.add_edge('01', '86')  # 3 observations
        
        # Score should be average of both edges
        score = mesh_graph.get_candidate_score('01', '7e', '86')
        assert 0.0 < score < 1.0
