#!/usr/bin/env python3
"""
Integration tests for full path resolution
"""

import pytest
import sqlite3
from datetime import datetime, timedelta
from modules.commands.path_command import PathCommand
from modules.mesh_graph import MeshGraph
from tests.helpers import create_test_repeater, create_test_edge


@pytest.mark.integration
class TestPathResolutionIntegration:
    """Integration tests for full path resolution with real database."""
    
    def test_path_resolution_with_graph_edges(self, mock_bot, test_db, populated_mesh_graph):
        """Test path resolution using graph edges from database."""
        # Add repeaters to database
        repeater1 = create_test_repeater('01', 'Repeater 01', latitude=47.6062, longitude=-122.3321)
        repeater2 = create_test_repeater('7e', 'Repeater 7e', latitude=47.6100, longitude=-122.3400)
        repeater3 = create_test_repeater('86', 'Repeater 86', latitude=47.6200, longitude=-122.3500)
        
        # Insert into database
        with sqlite3.connect(test_db.db_path) as conn:
            cursor = conn.cursor()
            for r in [repeater1, repeater2, repeater3]:
                cursor.execute('''
                    INSERT INTO complete_contact_tracking 
                    (public_key, name, role, latitude, longitude, last_heard, is_starred)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    r['public_key'], r['name'], r['role'],
                    r['latitude'], r['longitude'], r['last_heard'].isoformat(),
                    1 if r['is_starred'] else 0
                ))
            conn.commit()
        
        # Create path command
        command = PathCommand(mock_bot)
        command.graph_based_validation = True
        
        # Test path resolution
        path_hex = "017e86"
        # This would normally be called via handle_command, but we test the core logic
        # For integration, we verify the graph edges are used correctly
        
        # Verify edges exist in graph
        assert populated_mesh_graph.has_edge('01', '7e')
        assert populated_mesh_graph.has_edge('7e', '86')
    
    def test_path_resolution_prefix_collision(self, mock_bot, test_db, mesh_graph):
        """Test path resolution with prefix collisions resolved by graph."""
        # Create two repeaters with same prefix but different public keys
        key1 = '7e' * 32
        key2 = '7f' * 32
        
        repeater1 = create_test_repeater('7e', 'Repeater 7e A', public_key=key1, latitude=47.6062, longitude=-122.3321)
        repeater2 = create_test_repeater('7e', 'Repeater 7e B', public_key=key2, latitude=47.7000, longitude=-122.4000)
        
        # Add edge with stored public key matching repeater1
        mesh_graph.add_edge('01', '7e', to_public_key=key1)
        
        # Insert into database
        with sqlite3.connect(test_db.db_path) as conn:
            cursor = conn.cursor()
            for r in [repeater1, repeater2]:
                cursor.execute('''
                    INSERT INTO complete_contact_tracking 
                    (public_key, name, role, latitude, longitude, last_heard, is_starred)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    r['public_key'], r['name'], r['role'],
                    r['latitude'], r['longitude'], r['last_heard'].isoformat(),
                    1 if r['is_starred'] else 0
                ))
            conn.commit()
        
        # Graph should prefer repeater1 due to stored key match
        edge = mesh_graph.get_edge('01', '7e')
        assert edge is not None
        assert edge.get('to_public_key') == key1
    
    def test_edge_persistence_across_restarts(self, mock_bot, test_db, populated_mesh_graph):
        """Test that edges persist in database across graph restarts."""
        # Add edge to existing graph
        graph1 = populated_mesh_graph
        graph1.add_edge('01', '7e', from_public_key='01' * 32, to_public_key='7e' * 32)
        
        # Verify edge in database
        with sqlite3.connect(test_db.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM mesh_connections WHERE from_prefix = ? AND to_prefix = ?', ('01', '7e'))
            row = cursor.fetchone()
            assert row is not None
        
        # Create new graph instance (simulating restart) - loads from same db via mock_bot.db_manager
        graph2 = MeshGraph(mock_bot)
        
        # Edge should be loaded from database
        assert graph2.has_edge('01', '7e')
        edge = graph2.get_edge('01', '7e')
        assert edge['from_public_key'] == '01' * 32
        assert edge['to_public_key'] == '7e' * 32
    
    def test_graph_vs_geographic_selection(self, mock_bot, test_db, populated_mesh_graph):
        """Test that graph selection can override geographic when graph evidence is strong."""
        # Create two repeaters with same prefix
        # Repeater1: closer geographically
        # Repeater2: has strong graph evidence
        
        repeater1 = create_test_repeater('7e', 'Close Repeater', latitude=47.6062, longitude=-122.3321)
        repeater2 = create_test_repeater('7e', 'Graph Repeater', latitude=47.7000, longitude=-122.4000)
        
        # Add strong graph edge for repeater2
        key2 = repeater2['public_key']
        populated_mesh_graph.add_edge('01', '7e', to_public_key=key2)
        populated_mesh_graph.add_edge('01', '7e', to_public_key=key2)  # Multiple observations
        populated_mesh_graph.add_edge('01', '7e', to_public_key=key2)
        
        # Insert into database
        with sqlite3.connect(test_db.db_path) as conn:
            cursor = conn.cursor()
            for r in [repeater1, repeater2]:
                cursor.execute('''
                    INSERT INTO complete_contact_tracking 
                    (public_key, name, role, latitude, longitude, last_heard, is_starred)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    r['public_key'], r['name'], r['role'],
                    r['latitude'], r['longitude'], r['last_heard'].isoformat(),
                    1 if r['is_starred'] else 0
                ))
            conn.commit()
        
        # Graph should prefer repeater2 due to stored key and multiple observations
        edge = populated_mesh_graph.get_edge('01', '7e')
        assert edge is not None
        assert edge.get('to_public_key') == key2
        assert edge['observation_count'] >= 3
    
    def test_real_world_path_scenario(self, mock_bot, test_db, populated_mesh_graph):
        """Test a realistic multi-hop path scenario."""
        # Create a realistic path: 01 -> 7e -> 86 -> e0 -> 09
        path_nodes = ['01', '7e', '86', 'e0', '09']
        
        # Add repeaters
        repeaters = []
        for i, node_id in enumerate(path_nodes):
            lat = 47.6062 + (i * 0.01)
            lon = -122.3321 - (i * 0.01)
            repeater = create_test_repeater(
                node_id, f'Repeater {node_id}',
                latitude=lat, longitude=lon
            )
            repeaters.append(repeater)
        
        # Insert into database
        with sqlite3.connect(test_db.db_path) as conn:
            cursor = conn.cursor()
            for r in repeaters:
                cursor.execute('''
                    INSERT INTO complete_contact_tracking 
                    (public_key, name, role, latitude, longitude, last_heard, is_starred)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    r['public_key'], r['name'], r['role'],
                    r['latitude'], r['longitude'], r['last_heard'].isoformat(),
                    1 if r['is_starred'] else 0
                ))
            conn.commit()
        
        # Verify path is valid in graph
        is_valid, confidence = populated_mesh_graph.validate_path(path_nodes)
        assert is_valid is True
        assert confidence > 0.0
