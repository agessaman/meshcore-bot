#!/usr/bin/env python3
"""
Data models for the MeshCore Bot
Contains shared data structures used across modules
"""

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class MeshMessage:
    """Simplified message structure for our bot"""
    content: str
    sender_id: Optional[str] = None
    sender_pubkey: Optional[str] = None
    channel: Optional[str] = None
    hops: Optional[int] = None
    path: Optional[str] = None
    is_dm: bool = False
    timestamp: Optional[int] = None
    snr: Optional[float] = None
    rssi: Optional[int] = None
    elapsed: Optional[str] = None
    # When set from RF routing: path_nodes, path_hex, bytes_per_hop, path_length, route_type, etc.
    routing_info: Optional[dict[str, Any]] = None
