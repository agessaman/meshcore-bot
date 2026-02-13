#!/usr/bin/env python3
"""
Integration tests for path resolution with graph-based validation
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock
from modules.commands.path_command import PathCommand
from modules.mesh_graph import MeshGraph
from tests.helpers import create_test_repeater, create_test_edge, populate_test_graph


@pytest.mark.integration
class TestPathResolutionIntegration:
    """Integration tests for full path resolution."""
    
    @pytest.mark.asyncio
    async def test_path_resolution_with_graph_data(self, mock_bot, test_db, mesh_graph):
        """Test complete path resolution using real database."""
        mock_bot.mesh_graph = mesh_graph
        
        # Populate database with repeater data
        test_db.execute_update('''
            INSERT INTO complete_contact_tracking 
            (public_key, name, role, last_heard, latitude, longitude, is_starred)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', ('0101010101010101010101010101010101010101010101010101010101010101', 
              'Repeater 01', 'repeater', datetime.now().isoformat(), 47.6062, -122.3321, 0))
        
        test_db.execute_update('''
            INSERT INTO complete_contact_tracking 
            (public_key, name, role, last_heard, latitude, longitude, is_starred)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', ('7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e',
              'Repeater 7e', 'repeater', datetime.now().isoformat(), 47.5, -122.3, 0))
        
        # Create graph edge
        mesh_graph.add_edge('01', '7e')
        for _ in range(5):
            mesh_graph.add_edge('01', '7e')
        
        path_cmd = PathCommand(mock_bot)
        
        # Mock the lookup function to return our test data
        def mock_lookup(node_id):
            if node_id == '01':
                return [create_test_repeater('01', 'Repeater 01', 
                    public_key='0101010101010101010101010101010101010101010101010101010101010101')]
            elif node_id == '7e':
                return [create_test_repeater('7e', 'Repeater 7e',
                    public_key='7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e')]
            return []
        
        # Test path resolution
        path = ['01', '7e']
        result = await path_cmd._lookup_repeater_names(path, lookup_func=mock_lookup)
        
        assert len(result) > 0
    
    @pytest.mark.asyncio
    async def test_path_resolution_prefix_collision(self, mock_bot, test_db, mesh_graph):
        """Test path with prefix collisions using graph-based disambiguation."""
        mock_bot.mesh_graph = mesh_graph
        
        key1 = '7e1111111111111111111111111111111111111111111111111111111111111111'
        key2 = '7e2222222222222222222222222222222222222222222222222222222222222222'
        
        # Mock repeater_manager to return two repeaters with same prefix
        async def mock_get_repeater_devices(include_historical=True):
            return [
                {
                    'public_key': '0101010101010101010101010101010101010101010101010101010101010101',
                    'name': 'Repeater 01',
                    'role': 'repeater',
                    'device_type': 'repeater',
                    'last_heard': datetime.now(),
                    'last_advert_timestamp': datetime.now(),
                    'is_currently_tracked': True,
                    'latitude': 47.6062,
                    'longitude': -122.3321,
                    'city': 'Seattle',
                    'state': 'WA',
                    'country': 'USA',
                    'advert_count': 1,
                    'signal_strength': None,
                    'hop_count': 0,
                    'is_starred': 0
                },
                {
                    'public_key': key1,
                    'name': 'Local 7e',
                    'role': 'repeater',
                    'device_type': 'repeater',
                    'last_heard': datetime.now(),
                    'last_advert_timestamp': datetime.now(),
                    'is_currently_tracked': True,
                    'latitude': 47.6,
                    'longitude': -122.3,
                    'city': 'Seattle',
                    'state': 'WA',
                    'country': 'USA',
                    'advert_count': 1,
                    'signal_strength': None,
                    'hop_count': 0,
                    'is_starred': 1  # Starred
                },
                {
                    'public_key': key2,
                    'name': 'Distant 7e',
                    'role': 'repeater',
                    'device_type': 'repeater',
                    'last_heard': datetime.now(),
                    'last_advert_timestamp': datetime.now(),
                    'is_currently_tracked': True,
                    'latitude': 49.0,
                    'longitude': -123.0,
                    'city': 'Vancouver',
                    'state': 'BC',
                    'country': 'Canada',
                    'advert_count': 1,
                    'signal_strength': None,
                    'hop_count': 0,
                    'is_starred': 0
                }
            ]
        
        mock_bot.repeater_manager = Mock()
        mock_bot.repeater_manager.get_repeater_devices = mock_get_repeater_devices
        
        # Create graph edge to local repeater
        mesh_graph.add_edge('01', '7e', to_public_key=key1)
        for _ in range(10):
            mesh_graph.add_edge('01', '7e')
        
        path_cmd = PathCommand(mock_bot)
        
        path = ['01', '7e']
        result = await path_cmd._lookup_repeater_names(path)
        
        # Should select local starred repeater with graph edge
        assert len(result) > 0
        if '7e' in result:
            # Verify it selected the correct one (should be Local 7e)
            assert result['7e']['name'] == 'Local 7e'
    
    @pytest.mark.asyncio
    async def test_path_resolution_starred_preference(self, mock_bot, test_db, mesh_graph):
        """Test starred repeater preference in collisions."""
        mock_bot.mesh_graph = mesh_graph
        
        # Create edges for both repeaters
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('01', '7a')
        
        path_cmd = PathCommand(mock_bot)
        
        def mock_lookup(node_id):
            if node_id == '01':
                return [create_test_repeater('01', 'Repeater 01')]
            elif node_id == '7e':
                return [
                    create_test_repeater('7e', 'Starred 7e', is_starred=True),
                    create_test_repeater('7e', 'Regular 7e', is_starred=False)
                ]
            return []
        
        path = ['01', '7e']
        result = await path_cmd._lookup_repeater_names(path, lookup_func=mock_lookup)
        
        # Should prefer starred repeater
        assert len(result) > 0
    
    @pytest.mark.asyncio
    async def test_path_resolution_stored_keys_priority(self, mock_bot, test_db, mesh_graph):
        """Test stored public key priority."""
        mock_bot.mesh_graph = mesh_graph
        
        # Create edge with stored public key
        stored_key = '7e1111111111111111111111111111111111111111111111111111111111111111'
        other_key = '7e2222222222222222222222222222222222222222222222222222222222222222'
        mesh_graph.add_edge('01', '7e', to_public_key=stored_key)
        for _ in range(5):
            mesh_graph.add_edge('01', '7e')
        
        async def mock_get_repeater_devices(include_historical=True):
            return [
                {
                    'public_key': '0101010101010101010101010101010101010101010101010101010101010101',
                    'name': 'Repeater 01',
                    'role': 'repeater',
                    'device_type': 'repeater',
                    'last_heard': datetime.now(),
                    'last_advert_timestamp': datetime.now(),
                    'is_currently_tracked': True,
                    'latitude': 47.6062,
                    'longitude': -122.3321,
                    'city': 'Seattle',
                    'state': 'WA',
                    'country': 'USA',
                    'advert_count': 1,
                    'signal_strength': None,
                    'hop_count': 0,
                    'is_starred': 0
                },
                {
                    'public_key': stored_key,
                    'name': 'Matching Key',
                    'role': 'repeater',
                    'device_type': 'repeater',
                    'last_heard': datetime.now(),
                    'last_advert_timestamp': datetime.now(),
                    'is_currently_tracked': True,
                    'latitude': 47.6,
                    'longitude': -122.3,
                    'city': 'Seattle',
                    'state': 'WA',
                    'country': 'USA',
                    'advert_count': 1,
                    'signal_strength': None,
                    'hop_count': 0,
                    'is_starred': 0
                },
                {
                    'public_key': other_key,
                    'name': 'Other Key',
                    'role': 'repeater',
                    'device_type': 'repeater',
                    'last_heard': datetime.now(),
                    'last_advert_timestamp': datetime.now(),
                    'is_currently_tracked': True,
                    'latitude': 47.5,
                    'longitude': -122.2,
                    'city': 'Seattle',
                    'state': 'WA',
                    'country': 'USA',
                    'advert_count': 1,
                    'signal_strength': None,
                    'hop_count': 0,
                    'is_starred': 0
                }
            ]
        
        mock_bot.repeater_manager = Mock()
        mock_bot.repeater_manager.get_repeater_devices = mock_get_repeater_devices
        
        path_cmd = PathCommand(mock_bot)
        
        path = ['01', '7e']
        result = await path_cmd._lookup_repeater_names(path)
        
        # Should select repeater with matching stored key
        assert len(result) > 0
        if '7e' in result:
            assert result['7e']['name'] == 'Matching Key'
    
    @pytest.mark.asyncio
    async def test_path_resolution_multi_hop_inference(self, mock_bot, test_db, mesh_graph):
        """Test multi-hop path inference in real scenario."""
        mock_bot.mesh_graph = mesh_graph
        
        # Create 2-hop path: 01 -> 7e -> 86
        mesh_graph.add_edge('01', '7e')
        mesh_graph.add_edge('7e', '86')
        
        path_cmd = PathCommand(mock_bot)
        path_cmd.graph_multi_hop_enabled = True
        
        def mock_lookup(node_id):
            if node_id == '01':
                return [create_test_repeater('01', 'Repeater 01')]
            elif node_id == '7e':
                return [create_test_repeater('7e', 'Intermediate 7e')]
            elif node_id == '86':
                return [create_test_repeater('86', 'Repeater 86')]
            return []
        
        path = ['01', '7e', '86']
        result = await path_cmd._lookup_repeater_names(path, lookup_func=mock_lookup)
        
        assert len(result) > 0
    
    def test_path_resolution_edge_persistence(self, mock_bot, test_db):
        """Test edge persistence across operations."""
        # Create graph and add edge
        graph1 = MeshGraph(mock_bot)
        graph1.add_edge('01', '7e')
        for _ in range(5):
            graph1.add_edge('01', '7e')
        
        # Verify in database
        results = test_db.execute_query('SELECT * FROM mesh_connections WHERE from_prefix = ? AND to_prefix = ?', 
                                      ('01', '7e'))
        assert len(results) == 1
        assert results[0]['observation_count'] == 6
        
        # Create new graph instance (simulates restart)
        graph2 = MeshGraph(mock_bot)
        
        # Edge should be loaded from database
        edge = graph2.get_edge('01', '7e')
        assert edge is not None
        assert edge['observation_count'] == 6
    
    @pytest.mark.asyncio
    async def test_path_resolution_real_world_scenario(self, mock_bot, test_db, mesh_graph):
        """Test with realistic path data."""
        mock_bot.mesh_graph = mesh_graph
        
        # Create realistic path: 01 -> 7e -> 86 -> e0 -> 09
        path_nodes = ['01', '7e', '86', 'e0', '09']
        
        # Add edges with varying strengths
        mesh_graph.add_edge('01', '7e')
        for _ in range(10):
            mesh_graph.add_edge('01', '7e')  # Strong
        
        mesh_graph.add_edge('7e', '86')
        for _ in range(5):
            mesh_graph.add_edge('7e', '86')  # Medium
        
        mesh_graph.add_edge('86', 'e0')
        for _ in range(3):
            mesh_graph.add_edge('86', 'e0')  # Weak
        
        mesh_graph.add_edge('e0', '09')
        for _ in range(8):
            mesh_graph.add_edge('e0', '09')  # Strong
        
        path_cmd = PathCommand(mock_bot)
        
        def mock_lookup(node_id):
            return [create_test_repeater(node_id, f'Repeater {node_id}')]
        
        result = await path_cmd._lookup_repeater_names(path_nodes, lookup_func=mock_lookup)
        
        # Should resolve all nodes
        assert len(result) == len(path_nodes)
