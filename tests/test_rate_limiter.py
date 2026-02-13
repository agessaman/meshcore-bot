"""Tests for modules.rate_limiter."""

import time
from unittest.mock import patch

import pytest

from modules.rate_limiter import RateLimiter, PerUserRateLimiter


class TestRateLimiter:
    """Tests for RateLimiter."""

    def test_can_send_initially_true(self):
        limiter = RateLimiter(seconds=5)
        assert limiter.can_send() is True

    def test_can_send_after_record_send_false_within_interval(self):
        limiter = RateLimiter(seconds=5)
        limiter.record_send()
        assert limiter.can_send() is False

    def test_can_send_after_interval_elapsed(self):
        limiter = RateLimiter(seconds=1)
        limiter.record_send()
        time.sleep(1.1)
        assert limiter.can_send() is True

    def test_time_until_next(self):
        limiter = RateLimiter(seconds=5)
        limiter.record_send()
        t = limiter.time_until_next()
        assert 0 < t <= 5

    def test_record_send_updates_last_send(self):
        limiter = RateLimiter(seconds=10)
        before = time.time()
        limiter.record_send()
        after = time.time()
        assert before <= limiter.last_send <= after


class TestPerUserRateLimiter:
    """Tests for PerUserRateLimiter."""

    def test_empty_key_always_allowed(self):
        limiter = PerUserRateLimiter(seconds=5)
        assert limiter.can_send("") is True
        limiter.record_send("")
        assert limiter.can_send("") is True

    def test_per_key_tracking(self):
        limiter = PerUserRateLimiter(seconds=5)
        limiter.record_send("user1")
        assert limiter.can_send("user1") is False
        assert limiter.can_send("user2") is True

    def test_record_send_then_wait_allows_send(self):
        limiter = PerUserRateLimiter(seconds=1)
        limiter.record_send("user1")
        time.sleep(1.1)
        assert limiter.can_send("user1") is True

    def test_time_until_next(self):
        limiter = PerUserRateLimiter(seconds=5)
        limiter.record_send("user1")
        t = limiter.time_until_next("user1")
        assert 0 < t <= 5
        assert limiter.time_until_next("") == 0.0

    def test_eviction_at_max_entries(self):
        limiter = PerUserRateLimiter(seconds=10, max_entries=2)
        limiter.record_send("user1")
        limiter.record_send("user2")
        # Add third user - should evict user1
        limiter.record_send("user3")
        assert "user1" not in limiter._last_send or len(limiter._last_send) <= 2
        assert "user3" in limiter._last_send
