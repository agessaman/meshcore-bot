#!/usr/bin/env python3
"""
Test helper functions and factories for creating test data
"""

from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple


def create_test_repeater(
    prefix: str = "01",
    name: str = "Test Repeater",
    public_key: Optional[str] = None,
    latitude: float = 47.6062,
    longitude: float = -122.3321,
    is_starred: bool = False,
    last_heard: Optional[datetime] = None,
    last_advert_timestamp: Optional[datetime] = None,
    role: str = "repeater"
) -> Dict[str, Any]:
    """Factory function to create test repeater data.
    
    Args:
        prefix: Two-character hex prefix (default: "01")
        name: Repeater name
        public_key: Full public key (default: prefix repeated 32 times)
        latitude: Latitude coordinate
        longitude: Longitude coordinate
        is_starred: Whether repeater is starred
        last_heard: Last heard timestamp (default: now)
        last_advert_timestamp: Last advert timestamp (default: now)
        role: Device role (default: "repeater")
    
    Returns:
        Dictionary with repeater data matching database schema
    """
    if public_key is None:
        # Generate a realistic-looking public key from prefix
        public_key = (prefix.lower() * 16)[:64]  # 64 hex chars = 32 bytes
    
    now = datetime.now()
    if last_heard is None:
        last_heard = now
    if last_advert_timestamp is None:
        last_advert_timestamp = now
    
    return {
        'name': name,
        'public_key': public_key,
        'device_type': 'repeater',
        'last_seen': last_heard,
        'last_heard': last_heard,
        'last_advert_timestamp': last_advert_timestamp,
        'is_active': True,
        'latitude': latitude,
        'longitude': longitude,
        'city': 'Seattle',
        'state': 'WA',
        'country': 'USA',
        'advert_count': 1,
        'signal_strength': None,
        'hop_count': 0,
        'role': role,
        'is_starred': is_starred
    }


def create_test_edge(
    from_prefix: str,
    to_prefix: str,
    from_public_key: Optional[str] = None,
    to_public_key: Optional[str] = None,
    observation_count: int = 1,
    first_seen: Optional[datetime] = None,
    last_seen: Optional[datetime] = None,
    avg_hop_position: Optional[float] = None,
    geographic_distance: Optional[float] = None
) -> Dict[str, Any]:
    """Factory function to create test edge data.
    
    Args:
        from_prefix: Source node prefix
        to_prefix: Destination node prefix
        from_public_key: Source public key (default: generated from prefix)
        to_public_key: Destination public key (default: generated from prefix)
        observation_count: Number of times edge observed
        first_seen: First observation time (default: now)
        last_seen: Last observation time (default: now)
        avg_hop_position: Average hop position in paths
        geographic_distance: Distance in km
    
    Returns:
        Dictionary with edge data matching MeshGraph edge structure
    """
    now = datetime.now()
    if first_seen is None:
        first_seen = now
    if last_seen is None:
        last_seen = now
    
    if from_public_key is None:
        from_public_key = (from_prefix.lower() * 16)[:64]
    if to_public_key is None:
        to_public_key = (to_prefix.lower() * 16)[:64]
    
    return {
        'from_prefix': from_prefix.lower()[:2],
        'to_prefix': to_prefix.lower()[:2],
        'from_public_key': from_public_key,
        'to_public_key': to_public_key,
        'observation_count': observation_count,
        'first_seen': first_seen,
        'last_seen': last_seen,
        'avg_hop_position': avg_hop_position,
        'geographic_distance': geographic_distance
    }


def create_test_path(node_ids: List[str]) -> List[str]:
    """Factory function to create test path data.
    
    Args:
        node_ids: List of node prefixes in path order
    
    Returns:
        List of node IDs (normalized to lowercase)
    """
    return [node_id.lower()[:2] for node_id in node_ids]


def populate_test_graph(mesh_graph, edges: List[Dict[str, Any]]):
    """Helper to populate a MeshGraph instance with test edges.
    
    Args:
        mesh_graph: MeshGraph instance to populate
        edges: List of edge dictionaries (from create_test_edge)
    """
    for edge in edges:
        mesh_graph.add_edge(
            edge['from_prefix'],
            edge['to_prefix'],
            from_public_key=edge.get('from_public_key'),
            to_public_key=edge.get('to_public_key'),
            hop_position=edge.get('avg_hop_position'),
            geographic_distance=edge.get('geographic_distance')
        )
        # Manually set observation_count and timestamps if needed
        edge_key = (edge['from_prefix'].lower()[:2], edge['to_prefix'].lower()[:2])
        if edge_key in mesh_graph.edges:
            if edge.get('observation_count', 1) > 1:
                mesh_graph.edges[edge_key]['observation_count'] = edge['observation_count']
            if edge.get('first_seen'):
                mesh_graph.edges[edge_key]['first_seen'] = edge['first_seen']
            if edge.get('last_seen'):
                mesh_graph.edges[edge_key]['last_seen'] = edge['last_seen']
