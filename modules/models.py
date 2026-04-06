#!/usr/bin/env python3
"""
Data models for the MeshCore Bot
Contains shared data structures used across modules
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class MeshMessage:
    """Simplified message structure for our bot"""
    content: str
    sender_id: str | None = None
    sender_pubkey: str | None = None
    channel: str | None = None
    hops: int | None = None
    path: str | None = None
    is_dm: bool = False
    timestamp: int | None = None
    snr: float | None = None
    rssi: int | None = None
    elapsed: str | None = None
    # When set from RF routing: path_nodes, path_hex, bytes_per_hop, path_length, route_type, etc.
    routing_info: dict[str, Any] | None = None
