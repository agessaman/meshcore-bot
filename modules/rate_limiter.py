#!/usr/bin/env python3
"""
Rate limiting functionality for the MeshCore Bot
Controls how often messages can be sent to prevent spam
"""

import asyncio
import threading
import time
from collections import OrderedDict
from typing import Optional


class PerUserRateLimiter:
    """Per-user rate limiting: minimum seconds between bot replies to the same user.

    User identity is keyed by rate_limit_key (pubkey when available, else sender name).
    The key map is bounded by max_entries; eviction of oldest entries may allow a
    previously rate-limited user to send again slightly earlier.
    """

    def __init__(self, seconds: float, max_entries: int = 1000):
        self.seconds = seconds
        self.max_entries = max_entries
        # OrderedDict provides O(1) move-to-end + oldest-first eviction.
        self._last_send: OrderedDict[str, float] = OrderedDict()
        # Back-compat for existing tests/introspection: keep insertion/LRU order.
        self._order: list[str] = []
        self._lock = threading.Lock()

    def _normalize_key(self, key: str) -> str:
        return key.strip()

    def can_send(self, key: str) -> bool:
        """Check if we can send a message to this user (key)."""
        key = self._normalize_key(key)
        if not key:
            return True
        with self._lock:
            last = self._last_send.get(key, 0)
            return time.monotonic() - last >= self.seconds

    def time_until_next(self, key: str) -> float:
        """Get time until next allowed send for this user."""
        key = self._normalize_key(key)
        if not key:
            return 0.0
        with self._lock:
            last = self._last_send.get(key, 0)
            elapsed = time.monotonic() - last
            return max(0.0, self.seconds - elapsed)

    def record_send(self, key: str) -> None:
        """Record that we sent a message to this user."""
        key = self._normalize_key(key)
        if not key:
            return
        with self._lock:
            if key in self._last_send:
                self._last_send.move_to_end(key)
            elif len(self._last_send) >= self.max_entries:
                self._last_send.popitem(last=False)
            self._last_send[key] = time.monotonic()
            # Keep `_order` consistent for callers/tests.
            self._order = list(self._last_send.keys())


class RateLimiter:
    """Rate limiting for message sending"""

    def __init__(self, seconds: float):
        self.seconds = float(seconds)
        self.last_send = 0.0
        self._total_sends = 0
        self._total_throttled = 0
        self._lock = threading.Lock()

    def can_send(self) -> bool:
        """Check if we can send a message"""
        with self._lock:
            can = time.monotonic() - self.last_send >= self.seconds
            if not can:
                self._total_throttled += 1
            return can

    def time_until_next(self) -> float:
        """Get time until next allowed send"""
        with self._lock:
            elapsed = time.monotonic() - self.last_send
            return max(0, self.seconds - elapsed)

    def record_send(self):
        """Record that we sent a message"""
        with self._lock:
            self.last_send = time.monotonic()
            self._total_sends += 1

    def get_stats(self) -> dict:
        """Get rate limiter statistics"""
        with self._lock:
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
        self.last_tx = 0.0
        self._total_tx = 0
        self._total_throttled = 0
        self._lock = threading.Lock()

    def can_tx(self) -> bool:
        """Check if bot can transmit a message"""
        with self._lock:
            can = time.monotonic() - self.last_tx >= self.seconds
            if not can:
                self._total_throttled += 1
            return can

    def time_until_next_tx(self) -> float:
        """Get time until next allowed transmission"""
        with self._lock:
            elapsed = time.monotonic() - self.last_tx
            return max(0, self.seconds - elapsed)

    def record_tx(self):
        """Record that bot transmitted a message"""
        with self._lock:
            self.last_tx = time.monotonic()
            self._total_tx += 1

    async def wait_for_tx(self):
        """Wait for a transmit slot and claim it (async).

        Claiming the slot here, rather than only when ``record_tx`` runs after
        the radio confirms, keeps concurrent senders (two replies, a reply and a
        scheduled message) spaced too; before, they all passed the check before
        any of them recorded, and transmitted back to back.
        """
        while True:
            with self._lock:
                now = time.monotonic()
                wait_time = self.seconds - (now - self.last_tx)
                if wait_time <= 0:
                    self.last_tx = now
                    return
                self._total_throttled += 1
            await asyncio.sleep(wait_time + 0.05)  # Small buffer

    def get_stats(self) -> dict:
        """Get rate limiter statistics"""
        with self._lock:
            total_attempts = self._total_tx + self._total_throttled
            throttle_rate = self._total_throttled / max(1, total_attempts)
            return {
                'total_tx': self._total_tx,
                'total_throttled': self._total_throttled,
                'throttle_rate': throttle_rate
            }


class ChannelRateLimiter:
    """Per-channel rate limiting: minimum seconds between bot messages on the same channel.

    Channel names are mapped to ``RateLimiter`` instances using limits loaded from
    the ``[Rate_Limits]`` config section (``channel.<name>_seconds = N``).
    Channels without an explicit limit are unrestricted.
    """

    def __init__(self, channel_limits: dict[str, float]):
        normalized: dict[str, float] = {}
        for channel, seconds in channel_limits.items():
            ch = self._normalize_channel(channel)
            try:
                sec = float(seconds)
            except (TypeError, ValueError):
                continue
            if ch and sec > 0:
                normalized[ch] = sec
        self._limiters: dict[str, RateLimiter] = {
            channel: RateLimiter(seconds)
            for channel, seconds in normalized.items()
        }

    def _normalize_channel(self, channel: str) -> str:
        return channel.strip().lower()

    def can_send(self, channel: str) -> bool:
        limiter = self._limiters.get(self._normalize_channel(channel))
        return limiter.can_send() if limiter else True

    def time_until_next(self, channel: str) -> float:
        limiter = self._limiters.get(self._normalize_channel(channel))
        return limiter.time_until_next() if limiter else 0.0

    def record_send(self, channel: str) -> None:
        limiter = self._limiters.get(self._normalize_channel(channel))
        if limiter:
            limiter.record_send()

    def get_stats(self) -> dict[str, dict]:
        return {ch: lim.get_stats() for ch, lim in self._limiters.items()}

    def channels(self) -> list[str]:
        return list(self._limiters.keys())


class NominatimRateLimiter:
    """Rate limiting for Nominatim geocoding API requests

    Nominatim policy: Maximum 1 request per second
    We'll be conservative and use 1.1 seconds to ensure compliance
    """

    def __init__(self, seconds: float = 1.1):
        self.seconds = seconds
        self.last_request: float = 0.0
        self._lock: Optional[asyncio.Lock] = None
        self._lock_init = threading.Lock()  # Guards lazy creation of asyncio.Lock
        self._sync_lock = threading.Lock()  # Serializes the worker-thread path
        self._total_requests = 0
        self._total_throttled = 0

    def _get_lock(self) -> asyncio.Lock:
        """Lazily initialize the async lock (thread-safe)."""
        if self._lock is None:
            with self._lock_init:
                if self._lock is None:
                    self._lock = asyncio.Lock()
        return self._lock

    def can_request(self) -> bool:
        """Check if we can make a Nominatim request"""
        can = time.monotonic() - self.last_request >= self.seconds
        if not can:
            self._total_throttled += 1
        return can

    def time_until_next(self) -> float:
        """Get time until next allowed request"""
        elapsed = time.monotonic() - self.last_request
        return max(0, self.seconds - elapsed)

    def record_request(self):
        """Record that we made a Nominatim request"""
        with self._sync_lock:
            self.last_request = time.monotonic()
            self._total_requests += 1

    async def wait_for_request(self):
        """Wait until we can make a Nominatim request (async)"""
        while not self.can_request():
            wait_time = self.time_until_next()
            if wait_time > 0:
                await asyncio.sleep(wait_time + 0.05)  # Small buffer

    async def wait_and_request(self) -> None:
        """Atomically reserve an async request slot shared with sync callers."""
        async with self._get_lock():
            while True:
                with self._sync_lock:
                    elapsed = time.monotonic() - self.last_request
                    if elapsed >= self.seconds:
                        self.last_request = time.monotonic()
                        self._total_requests += 1
                        return
                    self._total_throttled += 1
                    wait_time = self.seconds - elapsed + 0.05
                await asyncio.sleep(wait_time)

    def wait_for_request_sync(self):
        """Wait until we can make a Nominatim request (synchronous).

        Prefer :meth:`wait_and_request_sync`: this only waits, so a caller that
        records the request afterwards leaves a check-then-act window.
        """
        while not self.can_request():
            wait_time = self.time_until_next()
            if wait_time > 0:
                time.sleep(wait_time + 0.05)  # Small buffer

    def wait_and_request_sync(self) -> None:
        """Wait until a request may be made, then reserve the slot (thread-safe).

        The sync twin of :meth:`wait_and_request`. Reserving *before* the caller's
        HTTP request, under a lock, is what keeps the 1 req/s policy: geocoding now
        runs on worker threads, and a wait-then-record-after pattern let several
        threads clear the gate together and hit Nominatim simultaneously.
        """
        while True:
            with self._sync_lock:
                elapsed = time.monotonic() - self.last_request
                if elapsed >= self.seconds:
                    self.last_request = time.monotonic()
                    self._total_requests += 1
                    return
                self._total_throttled += 1
                wait_time = self.seconds - elapsed + 0.05
            time.sleep(wait_time)  # Small buffer

    def get_stats(self) -> dict:
        """Get rate limiter statistics"""
        total_attempts = self._total_requests + self._total_throttled
        throttle_rate = self._total_throttled / max(1, total_attempts)
        return {
            'total_requests': self._total_requests,
            'total_throttled': self._total_throttled,
            'throttle_rate': throttle_rate
        }
