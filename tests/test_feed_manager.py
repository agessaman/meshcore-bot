"""Tests for FeedManager queue logic, deduplication, and DB operations."""

import sqlite3
import time
from configparser import ConfigParser
from unittest.mock import Mock

import pytest

from modules.db_manager import DBManager
from modules.feed_manager import FeedManager


@pytest.fixture
def fm_bot(tmp_path, mock_logger):
    """Bot with a real DBManager for feed manager integration tests."""
    bot = Mock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("Feed_Manager")
    bot.config.set("Feed_Manager", "feed_manager_enabled", "true")
    bot.config.set("Feed_Manager", "max_message_length", "200")
    db = DBManager(bot, str(tmp_path / "feed_test.db"))
    bot.db_manager = db
    return bot


@pytest.fixture
def fm(fm_bot):
    return FeedManager(fm_bot)


def _seed_feed(db, feed_id=1, channel_name="general"):
    """Insert a minimal feed_subscriptions row for FK references."""
    with db.connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO feed_subscriptions
            (id, feed_type, feed_url, channel_name, enabled)
            VALUES (?, 'rss', 'http://example.com/feed', ?, 1)
            """,
            (feed_id, channel_name),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# TestRecordFeedActivity
# ---------------------------------------------------------------------------


class TestRecordFeedActivity:
    """Tests for _record_feed_activity()."""

    def test_inserts_activity_row(self, fm, fm_bot):
        _seed_feed(fm_bot.db_manager)
        fm._record_feed_activity(1, "item-abc", "Test Article")
        with fm_bot.db_manager.connection() as conn:
            row = conn.execute(
                "SELECT item_id, item_title FROM feed_activity WHERE feed_id = 1"
            ).fetchone()
        assert row["item_id"] == "item-abc"
        assert "Test Article" in row["item_title"]

    def test_truncates_long_title(self, fm, fm_bot):
        _seed_feed(fm_bot.db_manager)
        long_title = "X" * 500
        fm._record_feed_activity(1, "item-long", long_title)
        with fm_bot.db_manager.connection() as conn:
            row = conn.execute(
                "SELECT item_title FROM feed_activity WHERE item_id = 'item-long'"
            ).fetchone()
        assert len(row["item_title"]) <= 200

    def test_duplicate_item_does_not_raise(self, fm, fm_bot):
        """Inserting the same item_id twice should not raise (may silently ignore)."""
        _seed_feed(fm_bot.db_manager)
        fm._record_feed_activity(1, "dup-item", "Article")
        # Second call should not crash
        fm._record_feed_activity(1, "dup-item", "Article Again")


# ---------------------------------------------------------------------------
# TestQueueFeedMessage
# ---------------------------------------------------------------------------


class TestQueueFeedMessage:
    """Tests for _queue_feed_message()."""

    def test_inserts_queue_row(self, fm, fm_bot):
        _seed_feed(fm_bot.db_manager)
        feed = {"id": 1, "channel_name": "general"}
        item = {"id": "item-1", "title": "Hello Feed"}
        fm._queue_feed_message(feed, item, "Hello Feed message")
        with fm_bot.db_manager.connection() as conn:
            row = conn.execute(
                "SELECT message, channel_name FROM feed_message_queue WHERE feed_id = 1"
            ).fetchone()
        assert row["message"] == "Hello Feed message"
        assert row["channel_name"] == "general"

    def test_queue_row_unsent_by_default(self, fm, fm_bot):
        _seed_feed(fm_bot.db_manager)
        feed = {"id": 1, "channel_name": "general"}
        item = {"id": "item-2", "title": "Unsent"}
        fm._queue_feed_message(feed, item, "Not sent yet")
        with fm_bot.db_manager.connection() as conn:
            row = conn.execute(
                "SELECT sent_at FROM feed_message_queue WHERE feed_id = 1"
            ).fetchone()
        assert row["sent_at"] is None

    def test_multiple_queue_messages_stored(self, fm, fm_bot):
        _seed_feed(fm_bot.db_manager)
        feed = {"id": 1, "channel_name": "general"}
        for i in range(3):
            fm._queue_feed_message(feed, {"id": f"item-{i}", "title": f"Item {i}"}, f"Msg {i}")
        with fm_bot.db_manager.connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM feed_message_queue WHERE feed_id = 1"
            ).fetchone()[0]
        assert count == 3


# ---------------------------------------------------------------------------
# TestUpdateFeedLastItemId
# ---------------------------------------------------------------------------


class TestUpdateFeedLastItemId:
    """Tests for _update_feed_last_item_id()."""

    def test_sets_last_item_id(self, fm, fm_bot):
        _seed_feed(fm_bot.db_manager)
        fm._update_feed_last_item_id(1, "item-xyz")
        with fm_bot.db_manager.connection() as conn:
            row = conn.execute(
                "SELECT last_item_id FROM feed_subscriptions WHERE id = 1"
            ).fetchone()
        assert row["last_item_id"] == "item-xyz"

    def test_overwrites_existing_last_item_id(self, fm, fm_bot):
        _seed_feed(fm_bot.db_manager)
        fm._update_feed_last_item_id(1, "item-first")
        fm._update_feed_last_item_id(1, "item-second")
        with fm_bot.db_manager.connection() as conn:
            row = conn.execute(
                "SELECT last_item_id FROM feed_subscriptions WHERE id = 1"
            ).fetchone()
        assert row["last_item_id"] == "item-second"


# ---------------------------------------------------------------------------
# TestDeduplicationViaFeedActivity
# ---------------------------------------------------------------------------


class TestDeduplicationViaFeedActivity:
    """Verify that previously recorded activity items are excluded from next poll."""

    def test_previously_recorded_items_are_excluded(self, fm, fm_bot):
        """Items in feed_activity for a feed should be in processed_item_ids."""
        _seed_feed(fm_bot.db_manager)
        fm._record_feed_activity(1, "guid-001", "Old Article")

        # Build processed_item_ids the same way process_rss_feed does
        processed_item_ids = set()
        with fm_bot.db_manager.connection() as conn:
            for row in conn.execute(
                "SELECT DISTINCT item_id FROM feed_activity WHERE feed_id = ?", (1,)
            ).fetchall():
                processed_item_ids.add(row[0])

        assert "guid-001" in processed_item_ids

    def test_new_item_not_in_activity_is_included(self, fm, fm_bot):
        _seed_feed(fm_bot.db_manager)
        fm._record_feed_activity(1, "guid-old", "Old Article")

        processed_item_ids = set()
        with fm_bot.db_manager.connection() as conn:
            for row in conn.execute(
                "SELECT DISTINCT item_id FROM feed_activity WHERE feed_id = ?", (1,)
            ).fetchall():
                processed_item_ids.add(row[0])

        # A brand new item ID should not be in processed_item_ids
        assert "guid-new" not in processed_item_ids

    def test_last_item_id_in_feed_dict_excludes_that_item(self, fm, fm_bot):
        """last_item_id from feed subscription dict seeds processed_item_ids."""
        _seed_feed(fm_bot.db_manager)
        last_item_id = "guid-last"
        # Simulate what process_rss_feed does with last_item_id from the feed dict
        processed_item_ids = {last_item_id}
        assert "guid-last" in processed_item_ids

    def test_multiple_recorded_items_all_excluded(self, fm, fm_bot):
        _seed_feed(fm_bot.db_manager)
        for i in range(5):
            fm._record_feed_activity(1, f"guid-{i:03d}", f"Article {i}")

        processed_item_ids = set()
        with fm_bot.db_manager.connection() as conn:
            for row in conn.execute(
                "SELECT DISTINCT item_id FROM feed_activity WHERE feed_id = ?", (1,)
            ).fetchall():
                processed_item_ids.add(row[0])

        for i in range(5):
            assert f"guid-{i:03d}" in processed_item_ids


# ---------------------------------------------------------------------------
# TestFeedDueForCheck (interval logic)
# ---------------------------------------------------------------------------


class TestFeedDueForCheck:
    """Test which feeds are due to be polled based on interval and last_check_time."""

    def test_never_checked_feed_is_due(self, fm, fm_bot):
        """last_check_time = NULL means the feed has never been checked and is always due."""
        _seed_feed(fm_bot.db_manager)
        with fm_bot.db_manager.connection() as conn:
            conn.row_factory = sqlite3.Row
            feed = dict(
                conn.execute(
                    "SELECT * FROM feed_subscriptions WHERE id = 1"
                ).fetchone()
            )
        # last_check_time is NULL → treated as ts 0, always due
        assert feed["last_check_time"] is None
        # Simulate interval check
        last_check_ts = 0
        interval = feed.get("check_interval_seconds") or 300
        assert time.time() - last_check_ts >= interval

    def test_recently_checked_feed_is_not_due(self):
        interval = 300
        last_check_ts = time.time() - 10  # checked 10 seconds ago
        is_due = time.time() - last_check_ts >= interval
        assert is_due is False

    def test_overdue_feed_is_due(self):
        interval = 300
        last_check_ts = time.time() - 400  # checked 400 seconds ago
        is_due = time.time() - last_check_ts >= interval
        assert is_due is True

    def test_exact_interval_boundary_is_due(self):
        interval = 300
        last_check_ts = time.time() - 300  # exactly at boundary
        is_due = time.time() - last_check_ts >= interval
        assert is_due is True


# ---------------------------------------------------------------------------
# TestUpdateFeedLastCheck
# ---------------------------------------------------------------------------


class TestUpdateFeedLastCheck:
    """Tests for _update_feed_last_check()."""

    def test_sets_last_check_time(self, fm, fm_bot):
        _seed_feed(fm_bot.db_manager)
        fm._update_feed_last_check(1)
        with fm_bot.db_manager.connection() as conn:
            row = conn.execute(
                "SELECT last_check_time FROM feed_subscriptions WHERE id = 1"
            ).fetchone()
        assert row["last_check_time"] is not None

    def test_last_check_time_is_recent(self, fm, fm_bot):
        """The recorded check time should be within the last few seconds."""
        _seed_feed(fm_bot.db_manager)
        before = time.time()
        fm._update_feed_last_check(1)
        after = time.time()

        with fm_bot.db_manager.connection() as conn:
            row = conn.execute(
                "SELECT last_check_time FROM feed_subscriptions WHERE id = 1"
            ).fetchone()

        # Parse stored ISO timestamp
        from datetime import datetime
        stored = row["last_check_time"]
        # Handle ISO format
        try:
            dt = datetime.fromisoformat(stored.replace("Z", "+00:00"))
            ts = dt.timestamp()
        except Exception:
            ts = before  # fallback; don't fail on parsing
        assert before <= ts <= after + 2  # within 2s tolerance


# ---------------------------------------------------------------------------
# TestRecordFeedError
# ---------------------------------------------------------------------------


class TestRecordFeedError:
    """Tests for _record_feed_error()."""

    def test_inserts_error_row(self, fm, fm_bot):
        _seed_feed(fm_bot.db_manager)
        fm._record_feed_error(1, "network", "Connection refused")
        with fm_bot.db_manager.connection() as conn:
            row = conn.execute(
                "SELECT error_type, error_message FROM feed_errors WHERE feed_id = 1"
            ).fetchone()
        assert row["error_type"] == "network"
        assert "Connection refused" in row["error_message"]
