#!/usr/bin/env python3
"""
Unit tests for MeshGraph edge management
"""

import pytest
from datetime import datetime, timedelta
from tests.helpers import create_test_edge


@pytest.mark.unit
class TestMeshGraphEdges:
    """Test MeshGraph edge management functionality."""
    
    def test_add_new_edge(self, mesh_graph):
        """Test adding a new edge to the graph."""
        mesh_graph.add_edge('01', '7e')
        
        edge = mesh_graph.get_edge('01', '7e')
        assert edge is not None
        assert edge['from_prefix'] == '01'
        assert edge['to_prefix'] == '7e'
        assert edge['observation_count'] == 1
        assert edge['first_seen'] is not None
        assert edge['last_seen'] is not None
    
    def test_add_edge_with_public_keys(self, mesh_graph):
        """Test adding edge with public keys."""
        from_key = '0101010101010101010101010101010101010101010101010101010101010101'
        to_key = '7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e'
        
        mesh_graph.add_edge('01', '7e', from_public_key=from_key, to_public_key=to_key)
        
        edge = mesh_graph.get_edge('01', '7e')
        assert edge['from_public_key'] == from_key
        assert edge['to_public_key'] == to_key
    
    def test_add_edge_with_hop_position(self, mesh_graph):
        """Test adding edge with hop position."""
        mesh_graph.add_edge('01', '7e', hop_position=2)
        
        edge = mesh_graph.get_edge('01', '7e')
        assert edge['avg_hop_position'] == 2.0
    
    def test_add_edge_with_geographic_distance(self, mesh_graph):
        """Test adding edge with geographic distance."""
        mesh_graph.add_edge('01', '7e', geographic_distance=15.5)
        
        edge = mesh_graph.get_edge('01', '7e')
        assert edge['geographic_distance'] == 15.5
    
    def test_update_existing_edge(self, mesh_graph):
        """Test updating an existing edge increments observation count."""
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('01', '7e')
        
        edge = mesh_graph.get_edge('01', '7e')
        assert edge['observation_count'] == 2
    
    def test_update_edge_hop_position_average(self, mesh_graph):
        """Test that updating edge recalculates average hop position."""
        mesh_graph.add_edge('01', '7e', hop_position=1)
        mesh_graph.add_edge('01', '7e', hop_position=3)
        
        edge = mesh_graph.get_edge('01', '7e')
        # Average should be (1 + 3) / 2 = 2.0
        assert edge['avg_hop_position'] == 2.0
        
        # Add another observation
        mesh_graph.add_edge('01', '7e', hop_position=2)
        edge = mesh_graph.get_edge('01', '7e')
        # Average should be (2.0 * 2 + 2) / 3 = 2.0
        assert edge['avg_hop_position'] == 2.0
    
    def test_update_edge_public_keys(self, mesh_graph):
        """Test that updating edge can add missing public keys."""
        # Add edge without keys
        mesh_graph.add_edge('01', '7e')
        edge = mesh_graph.get_edge('01', '7e')
        assert edge['from_public_key'] is None
        assert edge['to_public_key'] is None
        
        # Update with keys
        from_key = '0101010101010101010101010101010101010101010101010101010101010101'
        to_key = '7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e'
        mesh_graph.add_edge('01', '7e', from_public_key=from_key, to_public_key=to_key)
        
        edge = mesh_graph.get_edge('01', '7e')
        assert edge['from_public_key'] == from_key
        assert edge['to_public_key'] == to_key
    
    def test_update_edge_geographic_distance(self, mesh_graph):
        """Test that updating edge can update geographic distance."""
        mesh_graph.add_edge('01', '7e', geographic_distance=10.0)
        mesh_graph.add_edge('01', '7e', geographic_distance=15.5)
        
        edge = mesh_graph.get_edge('01', '7e')
        assert edge['geographic_distance'] == 15.5
    
    def test_get_edge_nonexistent(self, mesh_graph):
        """Test getting a non-existent edge returns None."""
        edge = mesh_graph.get_edge('01', '99')
        assert edge is None
    
    def test_has_edge(self, mesh_graph):
        """Test has_edge method."""
        assert mesh_graph.has_edge('01', '7e') is False
        
        mesh_graph.add_edge('01', '7e')
        assert mesh_graph.has_edge('01', '7e') is True
        assert mesh_graph.has_edge('7e', '01') is False  # Direction matters
    
    def test_prefix_normalization(self, mesh_graph):
        """Test that prefixes are normalized to lowercase and truncated."""
        mesh_graph.add_edge('01AB', '7EFF')
        
        # Should normalize to '01' and '7e'
        assert mesh_graph.has_edge('01', '7e') is True
        assert mesh_graph.has_edge('01ab', '7eff') is True
        assert mesh_graph.has_edge('01AB', '7EFF') is True
    
    def test_get_outgoing_edges(self, mesh_graph):
        """Test getting all outgoing edges from a node."""
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('01', '86')
        mesh_graph.add_edge('7e', '01')  # Reverse direction
        
        outgoing = mesh_graph.get_outgoing_edges('01')
        assert len(outgoing) == 2
        prefixes = {edge['to_prefix'] for edge in outgoing}
        assert '7e' in prefixes
        assert '86' in prefixes
    
    def test_get_incoming_edges(self, mesh_graph):
        """Test getting all incoming edges to a node."""
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('86', '7e')
        mesh_graph.add_edge('7e', '01')  # Reverse direction
        
        incoming = mesh_graph.get_incoming_edges('7e')
        assert len(incoming) == 2
        prefixes = {edge['from_prefix'] for edge in incoming}
        assert '01' in prefixes
        assert '86' in prefixes
    
    def test_empty_prefix_handling(self, mesh_graph):
        """Test that empty prefixes are ignored."""
        initial_count = len(mesh_graph.edges)
        
        mesh_graph.add_edge('', '7e')
        mesh_graph.add_edge('01', '')
        mesh_graph.add_edge('', '')
        
        assert len(mesh_graph.edges) == initial_count
    
    def test_edge_last_seen_updates(self, mesh_graph):
        """Test that last_seen timestamp updates on edge updates."""
        first_time = datetime.now() - timedelta(seconds=5)
        
        # Manually set first_seen to past time
        mesh_graph.add_edge('01', '7e')
        edge = mesh_graph.get_edge('01', '7e')
        edge['first_seen'] = first_time
        edge['last_seen'] = first_time
        
        # Wait a moment and update
        import time
        time.sleep(0.1)
        mesh_graph.add_edge('01', '7e')
        
        edge = mesh_graph.get_edge('01', '7e')
        assert edge['last_seen'] > first_time
        assert edge['first_seen'] == first_time  # First seen shouldn't change
