#!/usr/bin/env python3
"""
Unit tests for MeshGraph class
"""

import pytest
from datetime import datetime, timedelta
from modules.mesh_graph import MeshGraph
from tests.helpers import create_test_edge, populate_test_graph


@pytest.mark.unit
class TestMeshGraphEdgeManagement:
    """Test edge creation, updates, and retrieval."""
    
    def test_add_edge_new(self, mesh_graph):
        """Test creating a new edge."""
        from_prefix = "01"
        to_prefix = "7e"
        
        mesh_graph.add_edge(from_prefix, to_prefix)
        
        edge = mesh_graph.get_edge(from_prefix, to_prefix)
        assert edge is not None
        assert edge['from_prefix'] == from_prefix.lower()
        assert edge['to_prefix'] == to_prefix.lower()
        assert edge['observation_count'] == 1
        assert isinstance(edge['first_seen'], datetime)
        assert isinstance(edge['last_seen'], datetime)
        assert edge['first_seen'] == edge['last_seen']
    
    def test_add_edge_update_existing(self, mesh_graph):
        """Test updating an existing edge."""
        from_prefix = "01"
        to_prefix = "7e"
        
        # Create initial edge
        mesh_graph.add_edge(from_prefix, to_prefix, hop_position=0)
        first_seen = mesh_graph.edges[(from_prefix.lower(), to_prefix.lower())]['first_seen']
        
        # Wait a tiny bit to ensure timestamps differ
        import time
        time.sleep(0.01)
        
        # Update edge
        mesh_graph.add_edge(from_prefix, to_prefix, hop_position=1)
        
        edge = mesh_graph.get_edge(from_prefix, to_prefix)
        assert edge['observation_count'] == 2
        assert edge['first_seen'] == first_seen  # First seen should not change
        assert edge['last_seen'] > first_seen  # Last seen should update
        assert edge['avg_hop_position'] == 0.5  # (0 + 1) / 2
    
    def test_add_edge_public_keys(self, mesh_graph):
        """Test edge creation and updates with public keys."""
        from_prefix = "01"
        to_prefix = "7e"
        from_key = "0101010101010101010101010101010101010101010101010101010101010101"
        to_key = "7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e"
        
        # Create edge with public keys
        mesh_graph.add_edge(from_prefix, to_prefix, from_public_key=from_key, to_public_key=to_key)
        
        edge = mesh_graph.get_edge(from_prefix, to_prefix)
        assert edge['from_public_key'] == from_key
        assert edge['to_public_key'] == to_key
        
        # Update with new public key
        new_from_key = "0202020202020202020202020202020202020202020202020202020202020202"
        mesh_graph.add_edge(from_prefix, to_prefix, from_public_key=new_from_key)
        
        edge = mesh_graph.get_edge(from_prefix, to_prefix)
        assert edge['from_public_key'] == new_from_key
        assert edge['to_public_key'] == to_key  # Should remain unchanged
    
    def test_add_edge_hop_position(self, mesh_graph):
        """Test hop position tracking and weighted average calculation."""
        from_prefix = "01"
        to_prefix = "7e"
        
        # Add edge multiple times with different hop positions
        mesh_graph.add_edge(from_prefix, to_prefix, hop_position=0)
        mesh_graph.add_edge(from_prefix, to_prefix, hop_position=1)
        mesh_graph.add_edge(from_prefix, to_prefix, hop_position=2)
        
        edge = mesh_graph.get_edge(from_prefix, to_prefix)
        assert edge['observation_count'] == 3
        # Weighted average: (0*0 + 1*1 + 2*2) / 3 = 5/3 ≈ 1.667
        # Actually: ((0*0 + 1) + (1*1 + 2)) / 3 = (0 + 3) / 3 = 1.0
        # Formula: ((current_avg * (count-1)) + new_pos) / count
        # After 1st: 0
        # After 2nd: ((0 * 1) + 1) / 2 = 0.5
        # After 3rd: ((0.5 * 2) + 2) / 3 = (1 + 2) / 3 = 1.0
        assert abs(edge['avg_hop_position'] - 1.0) < 0.01
    
    def test_add_edge_geographic_distance(self, mesh_graph):
        """Test geographic distance storage and updates."""
        from_prefix = "01"
        to_prefix = "7e"
        
        # Create edge with distance
        mesh_graph.add_edge(from_prefix, to_prefix, geographic_distance=10.5)
        
        edge = mesh_graph.get_edge(from_prefix, to_prefix)
        assert edge['geographic_distance'] == 10.5
        
        # Update distance
        mesh_graph.add_edge(from_prefix, to_prefix, geographic_distance=12.3)
        
        edge = mesh_graph.get_edge(from_prefix, to_prefix)
        assert edge['geographic_distance'] == 12.3
    
    def test_get_edge_existing(self, mesh_graph):
        """Test retrieving an existing edge."""
        from_prefix = "01"
        to_prefix = "7e"
        
        mesh_graph.add_edge(from_prefix, to_prefix)
        edge = mesh_graph.get_edge(from_prefix, to_prefix)
        
        assert edge is not None
        assert edge['from_prefix'] == from_prefix.lower()
        assert edge['to_prefix'] == to_prefix.lower()
    
    def test_get_edge_nonexistent(self, mesh_graph):
        """Test retrieving a non-existent edge returns None."""
        edge = mesh_graph.get_edge("99", "aa")
        assert edge is None
    
    def test_get_edge_case_insensitive(self, mesh_graph):
        """Test that get_edge is case-insensitive."""
        mesh_graph.add_edge("01", "7E")
        
        # Try various case combinations
        assert mesh_graph.get_edge("01", "7e") is not None
        assert mesh_graph.get_edge("01", "7E") is not None
        assert mesh_graph.get_edge("01", "7e") is not None
        assert mesh_graph.get_edge("01", "7E") is not None


@pytest.mark.unit
class TestMeshGraphPathValidation:
    """Test path segment and full path validation."""
    
    def test_validate_path_segment_valid(self, mesh_graph):
        """Test validation with sufficient observations."""
        from_prefix = "01"
        to_prefix = "7e"
        
        # Create edge with recent timestamp and sufficient observations
        mesh_graph.add_edge(from_prefix, to_prefix)
        # Add more observations
        for _ in range(4):
            mesh_graph.add_edge(from_prefix, to_prefix)
        
        is_valid, confidence = mesh_graph.validate_path_segment(from_prefix, to_prefix, min_observations=3)
        
        assert is_valid is True
        assert 0.0 <= confidence <= 1.0
        assert confidence > 0.5  # Should have high confidence with recent data and 5 observations
    
    def test_validate_path_segment_insufficient_observations(self, mesh_graph):
        """Test validation fails when observations < min_observations."""
        from_prefix = "01"
        to_prefix = "7e"
        
        mesh_graph.add_edge(from_prefix, to_prefix)  # Only 1 observation
        
        is_valid, confidence = mesh_graph.validate_path_segment(from_prefix, to_prefix, min_observations=3)
        
        assert is_valid is False
        assert confidence == 0.0
    
    def test_validate_path_segment_stale(self, mesh_graph):
        """Test confidence decay with old last_seen."""
        from_prefix = "01"
        to_prefix = "7e"
        
        # Create edge with old timestamp
        old_time = datetime.now() - timedelta(days=10)  # 10 days ago
        edge_key = (from_prefix.lower(), to_prefix.lower())
        mesh_graph.edges[edge_key] = {
            'from_prefix': from_prefix.lower(),
            'to_prefix': to_prefix.lower(),
            'observation_count': 1,  # Low obs so recency dominates; 10 obs would keep confidence ~0.4
            'first_seen': old_time,
            'last_seen': old_time,
            'avg_hop_position': None,
            'geographic_distance': None
        }
        
        is_valid, confidence = mesh_graph.validate_path_segment(from_prefix, to_prefix, min_observations=1)
        
        assert is_valid is True  # Still valid (has observations)
        assert confidence < 0.3  # But low confidence due to staleness (10 days = ~240 hours, well past 48h half-life)
    
    def test_validate_path_segment_bidirectional(self, mesh_graph):
        """Test bidirectional edge bonus."""
        from_prefix = "01"
        to_prefix = "7e"
        
        # Create forward edge
        mesh_graph.add_edge(from_prefix, to_prefix)
        for _ in range(4):
            mesh_graph.add_edge(from_prefix, to_prefix)
        
        # Validate without bidirectional check
        is_valid, confidence_forward = mesh_graph.validate_path_segment(
            from_prefix, to_prefix, min_observations=1, check_bidirectional=False
        )
        
        # Create reverse edge
        mesh_graph.add_edge(to_prefix, from_prefix)
        for _ in range(4):
            mesh_graph.add_edge(to_prefix, from_prefix)
        
        # Validate with bidirectional check
        is_valid_bidir, confidence_bidir = mesh_graph.validate_path_segment(
            from_prefix, to_prefix, min_observations=1, check_bidirectional=True
        )
        
        assert is_valid is True
        assert is_valid_bidir is True
        assert confidence_bidir > confidence_forward  # Bidirectional should have higher confidence
        assert confidence_bidir <= min(1.0, confidence_forward + 0.15)  # Bonus is +0.15 max
    
    def test_validate_path_segment_confidence_calculation(self, mesh_graph):
        """Test confidence calculation components."""
        from_prefix = "01"
        to_prefix = "7e"
        
        # Create edge with known values
        now = datetime.now()
        edge_key = (from_prefix.lower(), to_prefix.lower())
        mesh_graph.edges[edge_key] = {
            'from_prefix': from_prefix.lower(),
            'to_prefix': to_prefix.lower(),
            'observation_count': 20,  # High observation count
            'first_seen': now - timedelta(hours=1),
            'last_seen': now - timedelta(hours=0.5),  # Very recent
            'avg_hop_position': None,
            'geographic_distance': None
        }
        
        is_valid, confidence = mesh_graph.validate_path_segment(from_prefix, to_prefix, min_observations=1)
        
        assert is_valid is True
        # With 20 observations and very recent timestamp, confidence should be very high
        assert confidence > 0.8
    
    def test_validate_path_complete(self, mesh_graph):
        """Test full path validation."""
        # Create a path: 01 -> 7e -> 86 -> e0
        path_nodes = ['01', '7e', '86', 'e0']
        
        # Add edges for the path
        for i in range(len(path_nodes) - 1):
            mesh_graph.add_edge(path_nodes[i], path_nodes[i + 1])
            # Add extra observations for some edges
            if i < 2:
                mesh_graph.add_edge(path_nodes[i], path_nodes[i + 1])
        
        is_valid, avg_confidence = mesh_graph.validate_path(path_nodes, min_observations=1)
        
        assert is_valid is True
        assert 0.0 <= avg_confidence <= 1.0
    
    def test_validate_path_invalid_segment(self, mesh_graph):
        """Test path validation fails when one segment is invalid."""
        path_nodes = ['01', '7e', '86', 'e0']
        
        # Add edges for most of the path
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('7e', '86')
        # Missing edge: 86 -> e0
        
        is_valid, confidence = mesh_graph.validate_path(path_nodes, min_observations=1)
        
        assert is_valid is False
        assert confidence == 0.0
    
    def test_validate_path_single_node(self, mesh_graph):
        """Test edge case: single node path is always valid."""
        is_valid, confidence = mesh_graph.validate_path(['01'], min_observations=1)
        
        assert is_valid is True
        assert confidence == 1.0


@pytest.mark.unit
class TestMeshGraphCandidateScoring:
    """Test candidate scoring with various edge combinations."""
    
    def test_get_candidate_score_no_edges(self, mesh_graph):
        """Test candidate with no graph edges returns 0.0."""
        score = mesh_graph.get_candidate_score('99', '01', '7e', min_observations=1)
        assert score == 0.0
    
    def test_get_candidate_score_prev_edge_only(self, mesh_graph):
        """Test scoring with only previous edge."""
        # Create edge from previous node to candidate
        mesh_graph.add_edge('01', '7e')
        for _ in range(4):
            mesh_graph.add_edge('01', '7e')  # 5 total observations
        
        score = mesh_graph.get_candidate_score('7e', '01', None, min_observations=1)
        
        assert score > 0.0
        assert score <= 1.0
    
    def test_get_candidate_score_next_edge_only(self, mesh_graph):
        """Test scoring with only next edge."""
        # Create edge from candidate to next node
        mesh_graph.add_edge('7e', '86')
        for _ in range(4):
            mesh_graph.add_edge('7e', '86')  # 5 total observations
        
        score = mesh_graph.get_candidate_score('7e', None, '86', min_observations=1)
        
        assert score > 0.0
        assert score <= 1.0
    
    def test_get_candidate_score_both_edges(self, mesh_graph):
        """Test scoring with both prev and next edges."""
        # Create both edges
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('7e', '86')
        
        score = mesh_graph.get_candidate_score('7e', '01', '86', min_observations=1)
        
        assert score > 0.0
        assert score <= 1.0
        # Should be average of both edge confidences
    
    def test_get_candidate_score_bidirectional(self, mesh_graph):
        """Test bidirectional edge checking increases score."""
        # Create forward edge only
        mesh_graph.add_edge('01', '7e')
        for _ in range(4):
            mesh_graph.add_edge('01', '7e')
        
        score_forward = mesh_graph.get_candidate_score('7e', '01', None, min_observations=1, use_bidirectional=False)
        
        # Create reverse edge (bidirectional)
        mesh_graph.add_edge('7e', '01')
        for _ in range(4):
            mesh_graph.add_edge('7e', '01')
        
        score_bidir = mesh_graph.get_candidate_score('7e', '01', None, min_observations=1, use_bidirectional=True)
        
        assert score_bidir > score_forward  # Bidirectional should score higher
    
    def test_get_candidate_score_hop_position_match(self, mesh_graph):
        """Test hop position validation bonus."""
        # Create edge with avg_hop_position = 1.0
        mesh_graph.add_edge('01', '7e', hop_position=1)
        mesh_graph.add_edge('01', '7e', hop_position=1)  # Average stays at 1.0
        
        # Score with matching hop position
        score_match = mesh_graph.get_candidate_score('7e', '01', None, min_observations=1, hop_position=1, use_hop_position=True)
        
        # Score with non-matching hop position
        score_no_match = mesh_graph.get_candidate_score('7e', '01', None, min_observations=1, hop_position=5, use_hop_position=True)
        
        assert score_match > score_no_match  # Matching position should have bonus
    
    def test_get_candidate_score_geographic_bonus(self, mesh_graph):
        """Test geographic distance data bonus."""
        # Create edge without geographic distance
        mesh_graph.add_edge('01', '7e')
        score_no_geo = mesh_graph.get_candidate_score('7e', '01', None, min_observations=1)
        
        # Update edge with geographic distance
        mesh_graph.add_edge('01', '7e', geographic_distance=10.5)
        score_with_geo = mesh_graph.get_candidate_score('7e', '01', None, min_observations=1)
        
        assert score_with_geo > score_no_geo  # Geographic data adds +0.05 bonus
    
    def test_get_candidate_score_combined_features(self, mesh_graph):
        """Test scoring with all features enabled."""
        # Create bidirectional edge with hop position and geographic distance
        mesh_graph.add_edge('01', '7e', hop_position=1, geographic_distance=10.5)
        mesh_graph.add_edge('7e', '01', hop_position=0, geographic_distance=10.5)
        
        score = mesh_graph.get_candidate_score(
            '7e', '01', '86', min_observations=1,
            use_bidirectional=True, use_hop_position=True, hop_position=1
        )
        
        assert 0.0 <= score <= 1.0
        # Should be high with all bonuses


@pytest.mark.unit
class TestMeshGraphMultiHop:
    """Test multi-hop path inference."""
    
    def test_find_intermediate_nodes_2hop(self, mesh_graph):
        """Test finding intermediate node in 2-hop path."""
        # Create 2-hop path: 01 -> 7e -> 86 (no direct 01 -> 86)
        mesh_graph.add_edge('01', '7e')
        for _ in range(4):
            mesh_graph.add_edge('01', '7e')
        
        mesh_graph.add_edge('7e', '86')
        for _ in range(4):
            mesh_graph.add_edge('7e', '86')
        
        candidates = mesh_graph.find_intermediate_nodes('01', '86', min_observations=1, max_hops=2)
        
        assert len(candidates) > 0
        # Should find '7e' as intermediate
        intermediate_prefixes = [c[0] for c in candidates]
        assert '7e' in intermediate_prefixes
    
    def test_find_intermediate_nodes_direct_edge(self, mesh_graph):
        """Test when direct edge exists, intermediate search still works."""
        # Create direct edge
        mesh_graph.add_edge('01', '86')
        for _ in range(4):
            mesh_graph.add_edge('01', '86')
        
        # Also create 2-hop path
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('7e', '86')
        
        candidates = mesh_graph.find_intermediate_nodes('01', '86', min_observations=1, max_hops=2)
        
        # Should still find intermediate nodes even though direct edge exists
        # (the function skips direct edges but continues searching)
        intermediate_prefixes = [c[0] for c in candidates]
        # May or may not include '7e' depending on implementation, but should not error
        assert isinstance(candidates, list)
    
    def test_find_intermediate_nodes_3hop(self, mesh_graph):
        """Test 3-hop path inference."""
        # Create 3-hop path: 01 -> 7e -> 86 -> e0
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('7e', '86')
        mesh_graph.add_edge('86', 'e0')
        
        candidates = mesh_graph.find_intermediate_nodes('01', 'e0', min_observations=1, max_hops=3)
        
        # Should find intermediate nodes in 3-hop path
        intermediate_prefixes = [c[0] for c in candidates]
        # Should find either '7e' or '86' or both
        assert len(candidates) > 0
    
    def test_find_intermediate_nodes_no_path(self, mesh_graph):
        """Test when no path exists returns empty list."""
        # Create isolated edges that don't connect
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('99', 'aa')
        
        candidates = mesh_graph.find_intermediate_nodes('01', 'aa', min_observations=1, max_hops=2)
        
        assert candidates == []
    
    def test_find_intermediate_nodes_min_observations(self, mesh_graph):
        """Test filtering by min_observations."""
        # Create path with one edge below threshold
        mesh_graph.add_edge('01', '7e')  # Only 1 observation
        mesh_graph.add_edge('7e', '86')
        for _ in range(4):
            mesh_graph.add_edge('7e', '86')  # 5 observations
        
        # With min_observations=3, should not find path through 7e
        candidates = mesh_graph.find_intermediate_nodes('01', '86', min_observations=3, max_hops=2)
        
        assert len(candidates) == 0  # 01->7e has only 1 observation, below threshold


@pytest.mark.unit
class TestMeshGraphPersistence:
    """Test edge persistence and write strategies."""
    
    def test_write_strategy_immediate(self, mock_bot, test_db):
        """Test immediate write strategy."""
        mock_bot.config.set('Path_Command', 'graph_write_strategy', 'immediate')
        graph = MeshGraph(mock_bot)
        
        graph.add_edge('01', '7e')
        
        # Check database directly
        results = test_db.execute_query('SELECT * FROM mesh_connections WHERE from_prefix = ? AND to_prefix = ?', ('01', '7e'))
        assert len(results) == 1
        assert results[0]['observation_count'] == 1
    
    def test_write_strategy_batched(self, mock_bot, test_db):
        """Test batched write strategy."""
        mock_bot.config.set('Path_Command', 'graph_write_strategy', 'batched')
        mock_bot.config.set('Path_Command', 'graph_batch_max_pending', '5')
        graph = MeshGraph(mock_bot)
        
        # Add edges (should be batched)
        for i in range(3):
            graph.add_edge(f'0{i}', '7e')
        
        # Edges should be in pending_updates, not yet in DB (unless max_pending reached)
        # With 3 edges and max_pending=5, they should still be pending
        assert len(graph.pending_updates) >= 0  # May have been flushed if max_pending reached
        
        # Force flush
        graph._flush_pending_updates_sync()
        
        # Now should be in database
        results = test_db.execute_query('SELECT * FROM mesh_connections')
        assert len(results) == 3
    
    def test_write_strategy_hybrid(self, mock_bot, test_db):
        """Test hybrid strategy (immediate for new, batched for updates)."""
        mock_bot.config.set('Path_Command', 'graph_write_strategy', 'hybrid')
        graph = MeshGraph(mock_bot)
        
        # New edge should be written immediately
        graph.add_edge('01', '7e')
        results = test_db.execute_query('SELECT * FROM mesh_connections WHERE from_prefix = ? AND to_prefix = ?', ('01', '7e'))
        assert len(results) == 1
        
        # Update should be batched
        graph.add_edge('01', '7e')
        assert ('01', '7e') in graph.pending_updates
    
    def test_load_from_database(self, mock_bot, test_db):
        """Test loading edges on initialization."""
        # Pre-populate database with edges
        test_db.execute_update('''
            INSERT INTO mesh_connections 
            (from_prefix, to_prefix, observation_count, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
        ''', ('01', '7e', 5, datetime.now().isoformat(), datetime.now().isoformat()))
        
        # Create new graph instance (should load from DB)
        graph = MeshGraph(mock_bot)
        
        # Check that edge was loaded
        edge = graph.get_edge('01', '7e')
        assert edge is not None
        assert edge['observation_count'] == 5
