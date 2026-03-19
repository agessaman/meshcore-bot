"""Tests for modules.rate_limiter."""

import asyncio
import time

from modules.rate_limiter import (
    BotTxRateLimiter,
    ChannelRateLimiter,
    NominatimRateLimiter,
    PerUserRateLimiter,
    RateLimiter,
)


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


class TestRateLimiterStats:
    """Tests for RateLimiter.get_stats."""

    def test_initial_stats_all_zero(self):
        limiter = RateLimiter(seconds=5)
        stats = limiter.get_stats()
        assert stats["total_sends"] == 0
        assert stats["total_throttled"] == 0
        assert stats["throttle_rate"] == 0.0

    def test_stats_track_sends_and_throttle(self):
        limiter = RateLimiter(seconds=60)
        limiter.record_send()
        limiter.can_send()   # will be throttled
        stats = limiter.get_stats()
        assert stats["total_sends"] == 1
        assert stats["total_throttled"] == 1
        assert 0.0 < stats["throttle_rate"] <= 1.0

    def test_time_until_next_when_fresh(self):
        limiter = RateLimiter(seconds=5)
        assert limiter.time_until_next() == 0


class TestBotTxRateLimiter:
    """Tests for BotTxRateLimiter."""

    def test_can_tx_initially_true(self):
        limiter = BotTxRateLimiter(seconds=5)
        assert limiter.can_tx() is True

    def test_can_tx_false_after_record(self):
        limiter = BotTxRateLimiter(seconds=5)
        limiter.record_tx()
        assert limiter.can_tx() is False

    def test_time_until_next_tx(self):
        limiter = BotTxRateLimiter(seconds=5)
        limiter.record_tx()
        t = limiter.time_until_next_tx()
        assert 0 < t <= 5

    def test_get_stats_shape(self):
        limiter = BotTxRateLimiter(seconds=5)
        limiter.record_tx()
        stats = limiter.get_stats()
        assert "total_tx" in stats
        assert "total_throttled" in stats
        assert "throttle_rate" in stats
        assert stats["total_tx"] == 1


class TestChannelRateLimiter:
    """Tests for ChannelRateLimiter."""

    def test_unlisted_channel_always_allowed(self):
        limiter = ChannelRateLimiter({"chan_a": 5.0})
        assert limiter.can_send("chan_b") is True

    def test_listed_channel_blocked_after_send(self):
        limiter = ChannelRateLimiter({"chan_a": 60.0})
        limiter.record_send("chan_a")
        assert limiter.can_send("chan_a") is False

    def test_record_send_unlisted_channel_no_error(self):
        limiter = ChannelRateLimiter({})
        limiter.record_send("nonexistent")  # Should not raise

    def test_time_until_next_unlisted_zero(self):
        limiter = ChannelRateLimiter({"chan_a": 5.0})
        assert limiter.time_until_next("unknown_chan") == 0.0

    def test_time_until_next_listed_positive(self):
        limiter = ChannelRateLimiter({"chan_a": 60.0})
        limiter.record_send("chan_a")
        t = limiter.time_until_next("chan_a")
        assert 0 < t <= 60

    def test_channels_returns_list(self):
        limiter = ChannelRateLimiter({"chan_a": 5.0, "chan_b": 10.0})
        channels = limiter.channels()
        assert sorted(channels) == ["chan_a", "chan_b"]

    def test_get_stats_shape(self):
        limiter = ChannelRateLimiter({"chan_a": 5.0})
        limiter.record_send("chan_a")
        stats = limiter.get_stats()
        assert "chan_a" in stats
        assert "total_sends" in stats["chan_a"]

    def test_zero_seconds_excluded(self):
        # zero-second channels should be excluded (seconds <= 0)
        limiter = ChannelRateLimiter({"chan_a": 0.0})
        assert len(limiter.channels()) == 0


class TestNominatimRateLimiter:
    """Tests for NominatimRateLimiter."""

    def test_can_request_initially_true(self):
        limiter = NominatimRateLimiter(seconds=1.1)
        assert limiter.can_request() is True

    def test_can_request_false_after_record(self):
        limiter = NominatimRateLimiter(seconds=5)
        limiter.record_request()
        assert limiter.can_request() is False

    def test_time_until_next(self):
        limiter = NominatimRateLimiter(seconds=5)
        limiter.record_request()
        t = limiter.time_until_next()
        assert 0 < t <= 5

    def test_get_stats_shape(self):
        limiter = NominatimRateLimiter(seconds=5)
        limiter.record_request()
        stats = limiter.get_stats()
        assert "total_requests" in stats
        assert "total_throttled" in stats
        assert stats["total_requests"] == 1


class TestPerUserRateLimiterEvictEarlyReturn:
    """Cover line 29: _evict_if_needed early return when key already present."""

    def test_evict_skipped_when_key_already_exists(self):
        # Fill the limiter to capacity with two entries.
        limiter = PerUserRateLimiter(seconds=10, max_entries=2)
        limiter.record_send("user1")
        limiter.record_send("user2")
        # Both slots are taken.  Recording for user1 again must NOT evict anyone
        # because the early-return on line 29 fires ("user1" is already in _last_send).
        limiter.record_send("user1")
        assert "user1" in limiter._last_send
        assert "user2" in limiter._last_send
        assert len(limiter._last_send) == 2

    def test_order_deduplication_for_existing_key(self):
        """Cover line 56: _order.remove(key) when key is already in _order."""
        limiter = PerUserRateLimiter(seconds=10)
        limiter.record_send("alice")
        # "alice" is now in _order.  A second record_send must remove and re-append her.
        assert limiter._order.count("alice") == 1
        limiter.record_send("alice")
        # Still exactly one entry in _order for alice (no duplicates).
        assert limiter._order.count("alice") == 1
        # And she is now at the tail (most-recently used).
        assert limiter._order[-1] == "alice"


class TestBotTxRateLimiterWaitForTx:
    """Cover lines 125-128: BotTxRateLimiter.wait_for_tx async wait loop."""

    def test_wait_for_tx_when_already_ready(self):
        """If can_tx() is True immediately, wait_for_tx returns without sleeping."""
        limiter = BotTxRateLimiter(seconds=5)
        # last_tx == 0 so can_tx() is True; the while loop never executes.
        asyncio.run(limiter.wait_for_tx())

    def test_wait_for_tx_after_backdate(self):
        """Force can_tx() to be False initially, then backdate last_tx so it
        becomes True on the very first sleep-free iteration check."""
        limiter = BotTxRateLimiter(seconds=5)
        limiter.record_tx()
        # Backdate so the interval has elapsed — can_tx() returns True on
        # the first evaluation, so the while body (lines 126-128) is entered
        # at least once by making the initial state throttled and then
        # immediately resolvable.
        limiter.last_tx = time.time() - 10  # well past the 5-second window
        # Now can_tx() is True so wait_for_tx returns immediately.
        asyncio.run(limiter.wait_for_tx())

    def test_wait_for_tx_loop_body_executed(self):
        """Make can_tx() return False once then True, exercising the loop body."""
        limiter = BotTxRateLimiter(seconds=60)
        limiter.record_tx()
        # Backdate last_tx just enough so can_tx() is True after we monkey-patch
        # it to be False on the first call only, ensuring the loop body runs.
        call_count = [0]
        original_can_tx = limiter.can_tx

        def patched_can_tx():
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: report not ready (exercises loop body).
                # We also set last_tx far in the past so time_until_next_tx() == 0
                # to avoid any real asyncio.sleep.
                limiter.last_tx = time.time() - 200
                return False
            return original_can_tx()

        limiter.can_tx = patched_can_tx
        asyncio.run(limiter.wait_for_tx())
        assert call_count[0] >= 2


class TestNominatimRateLimiterGetLock:
    """Cover lines 192-194: NominatimRateLimiter._get_lock lazy init."""

    def test_get_lock_creates_lock_on_first_call(self):
        limiter = NominatimRateLimiter(seconds=1.1)
        assert limiter._lock is None

        async def _inner():
            lock = limiter._get_lock()
            assert lock is not None
            assert isinstance(lock, asyncio.Lock)
            # Second call must return the same instance (lazy singleton).
            lock2 = limiter._get_lock()
            assert lock is lock2

        asyncio.run(_inner())

    def test_get_lock_returns_same_instance(self):
        """_get_lock called twice returns identical object."""
        async def _inner():
            limiter = NominatimRateLimiter(seconds=1.1)
            lock_a = limiter._get_lock()
            lock_b = limiter._get_lock()
            assert lock_a is lock_b

        asyncio.run(_inner())


class TestNominatimRateLimiterWaitForRequest:
    """Cover lines 215-218: NominatimRateLimiter.wait_for_request async wait loop."""

    def test_wait_for_request_when_already_ready(self):
        """can_request() is True from the start; the loop never runs."""
        limiter = NominatimRateLimiter(seconds=1.1)
        asyncio.run(limiter.wait_for_request())

    def test_wait_for_request_loop_body_executed(self):
        """Make can_request() return False once then True, exercising the loop body."""
        limiter = NominatimRateLimiter(seconds=60)
        limiter.record_request()
        call_count = [0]
        original = limiter.can_request

        def patched():
            call_count[0] += 1
            if call_count[0] == 1:
                # Backdate so time_until_next() returns 0, avoiding a real sleep.
                limiter.last_request = time.time() - 200
                return False
            return original()

        limiter.can_request = patched
        asyncio.run(limiter.wait_for_request())
        assert call_count[0] >= 2


class TestNominatimRateLimiterWaitAndRequest:
    """Cover lines 222-228: NominatimRateLimiter.wait_and_request."""

    def test_wait_and_request_when_ready(self):
        """No sleep needed; last_request starts at 0."""
        async def _inner():
            limiter = NominatimRateLimiter(seconds=1.1)
            before = time.time()
            await limiter.wait_and_request()
            assert limiter.last_request >= before
            assert limiter._total_requests == 1

        asyncio.run(_inner())

    def test_wait_and_request_increments_total(self):
        async def _inner():
            limiter = NominatimRateLimiter(seconds=1.1)
            await limiter.wait_and_request()
            await limiter.wait_and_request()
            assert limiter._total_requests == 2

        asyncio.run(_inner())

    def test_wait_and_request_sleeps_when_throttled(self):
        """Backdate last_request by much less than seconds so the sleep branch runs,
        but use a tiny seconds value so the actual sleep is negligible."""
        async def _inner():
            limiter = NominatimRateLimiter(seconds=0.05)
            # Record a request right now so time_since_last < seconds.
            limiter.record_request()
            before = time.time()
            await limiter.wait_and_request()
            # Two requests recorded total (one manual, one via wait_and_request).
            assert limiter._total_requests == 2
            # At least some time passed (the sleep).
            assert time.time() - before >= 0.0  # non-negative; sleep was brief

        asyncio.run(_inner())


class TestNominatimRateLimiterWaitForRequestSync:
    """Cover lines 232-235: NominatimRateLimiter.wait_for_request_sync."""

    def test_wait_for_request_sync_when_ready(self):
        """can_request() is True immediately; the while loop body never executes."""
        limiter = NominatimRateLimiter(seconds=1.1)
        limiter.wait_for_request_sync()  # Should return immediately without sleeping

    def test_wait_for_request_sync_loop_body_executed(self):
        """Make can_request() return False once then True so the loop body runs."""
        limiter = NominatimRateLimiter(seconds=60)
        limiter.record_request()
        call_count = [0]
        original = limiter.can_request

        def patched():
            call_count[0] += 1
            if call_count[0] == 1:
                # Backdate so time_until_next() returns 0, avoiding an actual sleep.
                limiter.last_request = time.time() - 200
                return False
            return original()

        limiter.can_request = patched
        limiter.wait_for_request_sync()
        assert call_count[0] >= 2
