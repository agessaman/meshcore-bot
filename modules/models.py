#!/usr/bin/env python3
"""
Data models for the MeshCore Bot
Contains shared data structures used across modules
"""

from dataclasses import dataclass
from typing import Optional


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
