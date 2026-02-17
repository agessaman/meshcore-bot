#!/usr/bin/env python3
"""
Unit tests for MeshGraph path validation
"""

import pytest
from datetime import datetime, timedelta
from tests.helpers import create_test_edge, create_test_path


@pytest.mark.unit
class TestMeshGraphValidation:
    """Test MeshGraph path validation functionality."""
    
    def test_validate_path_segment_exists(self, mesh_graph):
        """Test validating an existing path segment."""
        mesh_graph.add_edge('01', '7e', hop_position=1)
        
        is_valid, confidence = mesh_graph.validate_path_segment('01', '7e')
        assert is_valid is True
        assert 0.0 <= confidence <= 1.0
    
    def test_validate_path_segment_nonexistent(self, mesh_graph):
        """Test validating a non-existent path segment."""
        is_valid, confidence = mesh_graph.validate_path_segment('01', '99')
        assert is_valid is False
        assert confidence == 0.0
    
    def test_validate_path_segment_min_observations(self, mesh_graph):
        """Test that validation respects min_observations threshold."""
        mesh_graph.add_edge('01', '7e')
        # Edge has observation_count=1
        
        is_valid, confidence = mesh_graph.validate_path_segment('01', '7e', min_observations=3)
        assert is_valid is False
        assert confidence == 0.0
        
        # Add more observations
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('01', '7e')
        
        is_valid, confidence = mesh_graph.validate_path_segment('01', '7e', min_observations=3)
        assert is_valid is True
    
    def test_validate_path_segment_recency(self, mesh_graph):
        """Test that validation considers recency of edge."""
        # Add edge with recent timestamp
        mesh_graph.add_edge('01', '7e')
        recent_is_valid, recent_confidence = mesh_graph.validate_path_segment('01', '7e')
        
        # Manually set old timestamp
        edge = mesh_graph.get_edge('01', '7e')
        edge['last_seen'] = datetime.now() - timedelta(days=30)
        
        stale_is_valid, stale_confidence = mesh_graph.validate_path_segment('01', '7e')
        
        # Recent edge should have higher confidence
        assert recent_confidence > stale_confidence
    
    def test_validate_path_segment_bidirectional(self, mesh_graph):
        """Test bidirectional edge bonus."""
        # Add unidirectional edge
        mesh_graph.add_edge('01', '7e')
        is_valid, unidirectional_confidence = mesh_graph.validate_path_segment(
            '01', '7e', check_bidirectional=True
        )
        
        # Add reverse edge
        mesh_graph.add_edge('7e', '01')
        is_valid, bidirectional_confidence = mesh_graph.validate_path_segment(
            '01', '7e', check_bidirectional=True
        )
        
        # Bidirectional should have higher confidence
        assert bidirectional_confidence > unidirectional_confidence
    
    def test_validate_path_segment_bidirectional_min_observations(self, mesh_graph):
        """Test that bidirectional check respects min_observations."""
        # Add forward edge with enough observations
        mesh_graph.add_edge('01', '7e', hop_position=1)
        mesh_graph.add_edge('01', '7e', hop_position=1)
        mesh_graph.add_edge('01', '7e', hop_position=1)  # 3 observations
        
        mesh_graph.add_edge('7e', '01')  # Reverse edge with only 1 observation
        
        # With min_observations=3, forward edge should be valid
        is_valid, confidence = mesh_graph.validate_path_segment(
            '01', '7e', min_observations=3, check_bidirectional=True
        )
        # Should still be valid from forward edge, but no bidirectional bonus
        assert is_valid is True
        
        # Add more observations to reverse edge
        mesh_graph.add_edge('7e', '01')
        mesh_graph.add_edge('7e', '01')
        
        is_valid, confidence_with_bonus = mesh_graph.validate_path_segment(
            '01', '7e', min_observations=3, check_bidirectional=True
        )
        assert confidence_with_bonus > confidence
    
    def test_validate_path_single_node(self, mesh_graph):
        """Test that single-node path is always valid."""
        is_valid, confidence = mesh_graph.validate_path(['01'])
        assert is_valid is True
        assert confidence == 1.0
    
    def test_validate_path_empty(self, mesh_graph):
        """Test that empty path is always valid."""
        is_valid, confidence = mesh_graph.validate_path([])
        assert is_valid is True
        assert confidence == 1.0
    
    def test_validate_path_valid(self, populated_mesh_graph):
        """Test validating a valid path."""
        path = create_test_path(['01', '7e', '86'])
        is_valid, confidence = populated_mesh_graph.validate_path(path)
        assert is_valid is True
        assert confidence > 0.0
    
    def test_validate_path_invalid_missing_edge(self, populated_mesh_graph):
        """Test validating a path with missing edge."""
        path = create_test_path(['01', '99', '86'])  # 01->99 doesn't exist
        is_valid, confidence = populated_mesh_graph.validate_path(path)
        assert is_valid is False
        assert confidence == 0.0
    
    def test_validate_path_invalid_insufficient_observations(self, mesh_graph):
        """Test validating a path with edge below min_observations."""
        mesh_graph.add_edge('01', '7e')  # Only 1 observation
        
        path = create_test_path(['01', '7e'])
        is_valid, confidence = mesh_graph.validate_path(path, min_observations=3)
        assert is_valid is False
        assert confidence == 0.0
    
    def test_validate_path_average_confidence(self, mesh_graph):
        """Test that path validation returns average confidence."""
        # Create path with edges of different observation counts
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('01', '7e')  # 2 observations
        mesh_graph.add_edge('7e', '86')
        mesh_graph.add_edge('7e', '86')
        mesh_graph.add_edge('7e', '86')
        mesh_graph.add_edge('7e', '86')
        mesh_graph.add_edge('7e', '86')  # 5 observations
        
        path = create_test_path(['01', '7e', '86'])
        is_valid, confidence = mesh_graph.validate_path(path)
        
        assert is_valid is True
        # Confidence should be average of both segments
        assert 0.0 < confidence < 1.0
