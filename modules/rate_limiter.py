#!/usr/bin/env python3
"""
Rate limiting functionality for the MeshCore Bot
Controls how often messages can be sent to prevent spam
"""

import time
import asyncio
from typing import Optional, Dict, List


class PerUserRateLimiter:
    """Per-user rate limiting: minimum seconds between bot replies to the same user.

    User identity is keyed by rate_limit_key (pubkey when available, else sender name).
    The key map is bounded by max_entries; eviction of oldest entries may allow a
    previously rate-limited user to send again slightly earlier.
    """

    def __init__(self, seconds: float, max_entries: int = 1000):
        self.seconds = seconds
        self.max_entries = max_entries
        self._last_send: Dict[str, float] = {}
        self._order: List[str] = []  # keys in insertion order for oldest-first eviction

    def _evict_if_needed(self, new_key: str) -> None:
        """Evict oldest entry if at capacity and new_key is not already present."""
        if new_key in self._last_send:
            return
        while len(self._last_send) >= self.max_entries and self._order:
            oldest = self._order.pop(0)
            self._last_send.pop(oldest, None)

    def can_send(self, key: str) -> bool:
        """Check if we can send a message to this user (key)."""
        if not key:
            return True
        last = self._last_send.get(key, 0)
        return time.time() - last >= self.seconds

    def time_until_next(self, key: str) -> float:
        """Get time until next allowed send for this user."""
        if not key:
            return 0.0
        last = self._last_send.get(key, 0)
        elapsed = time.time() - last
        return max(0.0, self.seconds - elapsed)

    def record_send(self, key: str) -> None:
        """Record that we sent a message to this user."""
        if not key:
            return
        self._evict_if_needed(key)
        self._last_send[key] = time.time()
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)


class RateLimiter:
    """Rate limiting for message sending"""
    
    def __init__(self, seconds: int):
        self.seconds = seconds
        self.last_send = 0
        self._total_sends = 0
        self._total_throttled = 0
    
    def can_send(self) -> bool:
        """Check if we can send a message"""
        can = time.time() - self.last_send >= self.seconds
        if not can:
            self._total_throttled += 1
        return can
    
    def time_until_next(self) -> float:
        """Get time until next allowed send"""
        elapsed = time.time() - self.last_send
        return max(0, self.seconds - elapsed)
    
    def record_send(self):
        """Record that we sent a message"""
        self.last_send = time.time()
        self._total_sends += 1
    
    def get_stats(self) -> dict:
        """Get rate limiter statistics"""
        total_attempts = self._total_sends + self._total_throttled
        throttle_rate = self._total_throttled / max(1, total_attempts)
        return {
            'total_sends': self._total_sends,
            'total_throttled': self._total_throttled,
            'throttle_rate': throttle_rate
        }


class BotTxRateLimiter:
    """Rate limiting for bot transmission to prevent network overload"""
    
    def __init__(self, seconds: float = 1.0):
        self.seconds = seconds
        self.last_tx = 0
        self._total_tx = 0
        self._total_throttled = 0
    
    def can_tx(self) -> bool:
        """Check if bot can transmit a message"""
        can = time.time() - self.last_tx >= self.seconds
        if not can:
            self._total_throttled += 1
        return can
    
    def time_until_next_tx(self) -> float:
        """Get time until next allowed transmission"""
        elapsed = time.time() - self.last_tx
        return max(0, self.seconds - elapsed)
    
    def record_tx(self):
        """Record that bot transmitted a message"""
        self.last_tx = time.time()
        self._total_tx += 1
    
    async def wait_for_tx(self):
        """Wait until bot can transmit (async)"""
        while not self.can_tx():
            wait_time = self.time_until_next_tx()
            if wait_time > 0:
                await asyncio.sleep(wait_time + 0.05)  # Small buffer
    
    def get_stats(self) -> dict:
        """Get rate limiter statistics"""
        total_attempts = self._total_tx + self._total_throttled
        throttle_rate = self._total_throttled / max(1, total_attempts)
        return {
            'total_tx': self._total_tx,
            'total_throttled': self._total_throttled,
            'throttle_rate': throttle_rate
        }


class NominatimRateLimiter:
    """Rate limiting for Nominatim geocoding API requests
    
    Nominatim policy: Maximum 1 request per second
    We'll be conservative and use 1.1 seconds to ensure compliance
    """
    
    def __init__(self, seconds: float = 1.1):
        self.seconds = seconds
        self.last_request = 0
        self._lock: Optional[asyncio.Lock] = None
        self._total_requests = 0
        self._total_throttled = 0
    
    def _get_lock(self) -> asyncio.Lock:
        """Lazily initialize the async lock"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock
    
    def can_request(self) -> bool:
        """Check if we can make a Nominatim request"""
        can = time.time() - self.last_request >= self.seconds
        if not can:
            self._total_throttled += 1
        return can
    
    def time_until_next(self) -> float:
        """Get time until next allowed request"""
        elapsed = time.time() - self.last_request
        return max(0, self.seconds - elapsed)
    
    def record_request(self):
        """Record that we made a Nominatim request"""
        self.last_request = time.time()
        self._total_requests += 1
    
    async def wait_for_request(self):
        """Wait until we can make a Nominatim request (async)"""
        while not self.can_request():
            wait_time = self.time_until_next()
            if wait_time > 0:
                await asyncio.sleep(wait_time + 0.05)  # Small buffer
    
    async def wait_and_request(self) -> None:
        """Wait until a request can be made, then mark request time (thread-safe)"""
        async with self._get_lock():
            current_time = time.time()
            time_since_last = current_time - self.last_request
            if time_since_last < self.seconds:
                await asyncio.sleep(self.seconds - time_since_last)
            self.last_request = time.time()
            self._total_requests += 1
    
    def wait_for_request_sync(self):
        """Wait until we can make a Nominatim request (synchronous)"""
        while not self.can_request():
            wait_time = self.time_until_next()
            if wait_time > 0:
                time.sleep(wait_time + 0.05)  # Small buffer
    
    def get_stats(self) -> dict:
        """Get rate limiter statistics"""
        total_attempts = self._total_requests + self._total_throttled
        throttle_rate = self._total_throttled / max(1, total_attempts)
        return {
            'total_requests': self._total_requests,
            'total_throttled': self._total_throttled,
            'throttle_rate': throttle_rate
        }
