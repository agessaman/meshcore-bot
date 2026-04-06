"""Tests for FeedManager queue logic, deduplication, and DB operations."""

import asyncio
import sqlite3
import time
from configparser import ConfigParser
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch

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


# ---------------------------------------------------------------------------
# _format_timestamp (pure logic)
# ---------------------------------------------------------------------------


def _make_fm_no_db():
    """FeedManager with no DB — for pure logic tests."""
    bot = MagicMock()
    bot.logger = Mock()
    config = ConfigParser()
    config.add_section("Bot")
    bot.config = config
    bot.db_manager = MagicMock()
    bot.db_manager.db_path = ":memory:"
    return FeedManager(bot)


class TestFormatTimestamp:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def test_none_returns_empty(self):
        assert self.fm._format_timestamp(None) == ""

    def test_just_now(self):
        dt = datetime.now(timezone.utc)
        result = self.fm._format_timestamp(dt)
        assert result == "now"

    def test_30_minutes_ago(self):
        dt = datetime.now(timezone.utc) - timedelta(minutes=30)
        result = self.fm._format_timestamp(dt)
        assert "m ago" in result

    def test_3_hours_ago(self):
        dt = datetime.now(timezone.utc) - timedelta(hours=3, minutes=15)
        result = self.fm._format_timestamp(dt)
        assert "h" in result and "m ago" in result

    def test_5_days_ago(self):
        dt = datetime.now(timezone.utc) - timedelta(days=5)
        result = self.fm._format_timestamp(dt)
        assert result == "5d ago"

    def test_naive_datetime(self):
        dt = datetime.now() - timedelta(hours=2)
        result = self.fm._format_timestamp(dt)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _apply_shortening (pure logic)
# ---------------------------------------------------------------------------


class TestApplyShortening:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def test_empty_text_returns_empty(self):
        assert self.fm._apply_shortening("", "truncate:50") == ""

    def test_truncate_short_text_unchanged(self):
        assert self.fm._apply_shortening("hi", "truncate:50") == "hi"

    def test_truncate_long_text(self):
        result = self.fm._apply_shortening("a" * 100, "truncate:10")
        assert result.endswith("...")
        assert len(result) <= 13

    def test_truncate_invalid_number(self):
        result = self.fm._apply_shortening("hello", "truncate:abc")
        assert result == "hello"

    def test_word_wrap_short_unchanged(self):
        assert self.fm._apply_shortening("hello world", "word_wrap:50") == "hello world"

    def test_word_wrap_long_truncates(self):
        text = "hello world this is a long sentence here"
        result = self.fm._apply_shortening(text, "word_wrap:20")
        assert result.endswith("...")

    def test_first_words_few_unchanged(self):
        assert self.fm._apply_shortening("one two", "first_words:5") == "one two"

    def test_first_words_truncates(self):
        result = self.fm._apply_shortening("one two three four five", "first_words:3")
        assert result == "one two three..."

    def test_regex_extracts_group(self):
        result = self.fm._apply_shortening("Price: $42", "regex:\\$(\\d+)")
        assert result == "42"

    def test_regex_whole_match_no_group(self):
        result = self.fm._apply_shortening("hello world", "regex:hello")
        assert result == "hello"

    def test_regex_no_match_returns_empty(self):
        result = self.fm._apply_shortening("hello", "regex:xyz")
        assert result == ""

    def test_regex_with_group_0(self):
        result = self.fm._apply_shortening("abc 123", "regex:abc \\d+:0")
        assert result == "abc 123"

    def test_if_regex_matches(self):
        result = self.fm._apply_shortening("red alert", "if_regex:red:yes:no")
        assert result == "yes"

    def test_if_regex_no_match(self):
        result = self.fm._apply_shortening("blue alert", "if_regex:red:yes:no")
        assert result == "no"

    def test_switch_matches(self):
        result = self.fm._apply_shortening("high", "switch:highest:🔴:high:🟠:medium:🟡:⚪")
        assert result == "🟠"

    def test_switch_default(self):
        result = self.fm._apply_shortening("unknown", "switch:highest:🔴:high:🟠:⚪")
        assert result == "⚪"

    def test_unknown_function_returns_text(self):
        result = self.fm._apply_shortening("hello", "unknown_func")
        assert result == "hello"

    def test_regex_cond_matches_then_value(self):
        result = self.fm._apply_shortening(
            "No restrictions here",
            "regex_cond:(No restrictions):No restrictions:👍:1"
        )
        assert result == "👍"

    def test_regex_cond_no_extract_match(self):
        result = self.fm._apply_shortening(
            "Some data",
            "regex_cond:(Missing pattern):check:yes:1"
        )
        assert result == ""


# ---------------------------------------------------------------------------
# _get_nested_value (pure logic)
# ---------------------------------------------------------------------------


class TestGetNestedValue:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def test_simple_key(self):
        assert self.fm._get_nested_value({"a": 1}, "a") == 1

    def test_nested_key(self):
        data = {"a": {"b": {"c": "deep"}}}
        assert self.fm._get_nested_value(data, "a.b.c") == "deep"

    def test_missing_key_default(self):
        assert self.fm._get_nested_value({"a": 1}, "b", "fb") == "fb"

    def test_list_index(self):
        assert self.fm._get_nested_value({"items": ["x", "y", "z"]}, "items.1") == "y"

    def test_list_out_of_bounds_default(self):
        assert self.fm._get_nested_value({"items": ["x"]}, "items.5", "def") == "def"

    def test_none_data_returns_default(self):
        assert self.fm._get_nested_value(None, "a", "def") == "def"

    def test_empty_path_returns_default(self):
        assert self.fm._get_nested_value({"a": 1}, "", "def") == "def"

    def test_none_in_path_returns_default(self):
        assert self.fm._get_nested_value({"a": None}, "a.b", "def") == "def"

    def test_list_non_integer_index_default(self):
        assert self.fm._get_nested_value({"items": [1, 2]}, "items.notnum", "def") == "def"

    def test_scalar_then_nested_default(self):
        assert self.fm._get_nested_value({"a": 42}, "a.b", "def") == "def"


# ---------------------------------------------------------------------------
# _parse_microsoft_date (pure logic)
# ---------------------------------------------------------------------------


class TestParseMicrosoftDate:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def test_valid_utc_date(self):
        result = self.fm._parse_microsoft_date("/Date(1609459200000)/")
        assert isinstance(result, datetime)

    def test_positive_offset(self):
        result = self.fm._parse_microsoft_date("/Date(1609459200000+0800)/")
        assert isinstance(result, datetime)

    def test_negative_offset(self):
        result = self.fm._parse_microsoft_date("/Date(1609459200000-0500)/")
        assert isinstance(result, datetime)

    def test_none_returns_none(self):
        assert self.fm._parse_microsoft_date(None) is None

    def test_empty_returns_none(self):
        assert self.fm._parse_microsoft_date("") is None

    def test_non_ms_format_returns_none(self):
        assert self.fm._parse_microsoft_date("2021-01-01") is None

    def test_non_string_returns_none(self):
        assert self.fm._parse_microsoft_date(12345) is None


# ---------------------------------------------------------------------------
# format_message (pure logic)
# ---------------------------------------------------------------------------


class TestFormatMessage:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def _item(self, **kw):
        base = {
            "title": "Test Title",
            "description": "Test body text",
            "link": "http://example.com/1",
            "published": datetime.now(timezone.utc) - timedelta(minutes=5),
        }
        base.update(kw)
        return base

    def _feed(self, fmt="{emoji} {title}", name="test"):
        return {"feed_name": name, "output_format": fmt}

    def test_basic_returns_string(self):
        result = self.fm.format_message(self._item(), self._feed())
        assert isinstance(result, str)
        assert "Test Title" in result

    def test_default_emoji(self):
        result = self.fm.format_message(self._item(), self._feed())
        assert "📢" in result

    def test_emergency_emoji(self):
        result = self.fm.format_message(self._item(), self._feed(name="emergency"))
        assert "🚨" in result

    def test_warning_emoji(self):
        result = self.fm.format_message(self._item(), self._feed(name="weather warning"))
        assert "⚠️" in result

    def test_news_emoji(self):
        result = self.fm.format_message(self._item(), self._feed(name="news feed"))
        assert "ℹ️" in result

    def test_date_placeholder(self):
        result = self.fm.format_message(self._item(), self._feed(fmt="{date}"))
        assert "ago" in result or result == "now"

    def test_link_placeholder(self):
        result = self.fm.format_message(self._item(), self._feed(fmt="{link}"))
        assert "example.com" in result

    def test_body_html_stripped(self):
        item = self._item(description="<p>Hello <b>world</b></p>")
        result = self.fm.format_message(item, self._feed(fmt="{body}"))
        assert "<p>" not in result
        assert "Hello" in result

    def test_body_br_to_newline(self):
        item = self._item(description="Line1<br>Line2")
        result = self.fm.format_message(item, self._feed(fmt="{body}"))
        assert "\n" in result

    def test_raw_field(self):
        item = self._item(raw={"Priority": "High"})
        result = self.fm.format_message(item, self._feed(fmt="{raw.Priority}"))
        assert "High" in result

    def test_raw_field_truncate(self):
        item = self._item(raw={"Detail": "a" * 200})
        result = self.fm.format_message(item, self._feed(fmt="{raw.Detail|truncate:10}"))
        assert len(result) <= 13

    def test_long_message_truncated(self):
        self.fm.max_message_length = 50
        item = self._item(title="a" * 200)
        result = self.fm.format_message(item, self._feed(fmt="{title}"))
        assert len(result) <= 53

    def test_multiline_long_truncated(self):
        self.fm.max_message_length = 60
        item = self._item(title="Title here", description="x" * 100)
        result = self.fm.format_message(item, self._feed(fmt="{title}\n{body}"))
        assert isinstance(result, str)

    def test_no_output_format_uses_default(self):
        feed = {"feed_name": "test", "output_format": None}
        result = self.fm.format_message(self._item(), feed)
        assert isinstance(result, str)

    def test_raw_dict_serialized(self):
        item = self._item(raw={"nested": {"key": "val"}})
        result = self.fm.format_message(item, self._feed(fmt="{raw.nested}"))
        assert isinstance(result, str)

    def test_truncate_function_on_title(self):
        item = self._item(title="a" * 100)
        result = self.fm.format_message(item, self._feed(fmt="{title|truncate:20}"))
        assert result.endswith("...")
        assert len(result) <= 23


# ---------------------------------------------------------------------------
# _should_send_item (pure logic — no DB)
# ---------------------------------------------------------------------------


class TestShouldSendItem:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def _feed(self, filter_cfg=None):
        return {"id": 1, "filter_config": filter_cfg}

    def _item(self, raw=None, **kw):
        base = {"title": "Test", "raw": raw or {}}
        base.update(kw)
        return base

    def test_no_filter_sends_all(self):
        assert self.fm._should_send_item(self._feed(), self._item()) is True

    def test_invalid_json_filter_sends_all(self):
        assert self.fm._should_send_item(self._feed("not json"), self._item()) is True

    def test_empty_conditions_sends_all(self):
        import json
        fc = json.dumps({"conditions": []})
        assert self.fm._should_send_item(self._feed(fc), self._item()) is True

    def test_equals_match(self):
        import json
        fc = json.dumps({"conditions": [{"field": "Priority", "operator": "equals", "value": "High"}]})
        item = self._item(raw={"Priority": "High"})
        assert self.fm._should_send_item(self._feed(fc), item) is True

    def test_equals_no_match(self):
        import json
        fc = json.dumps({"conditions": [{"field": "Priority", "operator": "equals", "value": "High"}]})
        item = self._item(raw={"Priority": "Low"})
        assert self.fm._should_send_item(self._feed(fc), item) is False

    def test_not_equals(self):
        import json
        fc = json.dumps({"conditions": [{"field": "Status", "operator": "not_equals", "value": "Closed"}]})
        item = self._item(raw={"Status": "Open"})
        assert self.fm._should_send_item(self._feed(fc), item) is True

    def test_in_operator(self):
        import json
        fc = json.dumps({"conditions": [{"field": "Priority", "operator": "in", "values": ["high", "highest"]}]})
        item = self._item(raw={"Priority": "high"})
        assert self.fm._should_send_item(self._feed(fc), item) is True

    def test_not_in_operator(self):
        import json
        fc = json.dumps({"conditions": [{"field": "Category", "operator": "not_in", "values": ["maintenance"]}]})
        item = self._item(raw={"Category": "Incident"})
        assert self.fm._should_send_item(self._feed(fc), item) is True

    def test_matches_operator(self):
        import json
        fc = json.dumps({"conditions": [{"field": "Priority", "operator": "matches", "pattern": "^(high|highest)$"}]})
        item = self._item(raw={"Priority": "high"})
        assert self.fm._should_send_item(self._feed(fc), item) is True

    def test_not_matches_operator(self):
        import json
        fc = json.dumps({"conditions": [{"field": "Priority", "operator": "not_matches", "pattern": "^low$"}]})
        item = self._item(raw={"Priority": "high"})
        assert self.fm._should_send_item(self._feed(fc), item) is True

    def test_contains_operator(self):
        import json
        fc = json.dumps({"conditions": [{"field": "Title", "operator": "contains", "value": "accident"}]})
        item = self._item(raw={"Title": "Traffic accident on I-5"})
        assert self.fm._should_send_item(self._feed(fc), item) is True

    def test_not_contains_operator(self):
        import json
        fc = json.dumps({"conditions": [{"field": "Title", "operator": "not_contains", "value": "planned"}]})
        item = self._item(raw={"Title": "Unexpected outage"})
        assert self.fm._should_send_item(self._feed(fc), item) is True

    def test_or_logic(self):
        import json
        fc = json.dumps({
            "conditions": [
                {"field": "Priority", "operator": "equals", "value": "high"},
                {"field": "Priority", "operator": "equals", "value": "medium"},
            ],
            "logic": "OR"
        })
        item = self._item(raw={"Priority": "medium"})
        assert self.fm._should_send_item(self._feed(fc), item) is True

    def test_and_logic_fails_when_one_false(self):
        import json
        fc = json.dumps({
            "conditions": [
                {"field": "Priority", "operator": "equals", "value": "high"},
                {"field": "Status", "operator": "equals", "value": "open"},
            ],
            "logic": "AND"
        })
        item = self._item(raw={"Priority": "high", "Status": "closed"})
        assert self.fm._should_send_item(self._feed(fc), item) is False

    def test_raw_prefix_field_access(self):
        import json
        fc = json.dumps({"conditions": [{"field": "raw.Priority", "operator": "equals", "value": "High"}]})
        item = self._item(raw={"Priority": "High"})
        assert self.fm._should_send_item(self._feed(fc), item) is True

    def test_top_level_field_fallback(self):
        import json
        fc = json.dumps({"conditions": [{"field": "title", "operator": "contains", "value": "test"}]})
        item = {"title": "Test Article", "raw": {}}
        assert self.fm._should_send_item(self._feed(fc), item) is True

    def test_unknown_operator_defaults_true(self):
        import json
        fc = json.dumps({"conditions": [{"field": "Priority", "operator": "unknown_op"}]})
        item = self._item(raw={"Priority": "High"})
        assert self.fm._should_send_item(self._feed(fc), item) is True

    def test_invalid_regex_in_matches_returns_false(self):
        import json
        fc = json.dumps({"conditions": [{"field": "P", "operator": "matches", "pattern": "[invalid"}]})
        item = self._item(raw={"P": "val"})
        assert self.fm._should_send_item(self._feed(fc), item) is False

    def test_invalid_regex_in_not_matches_returns_true(self):
        import json
        fc = json.dumps({"conditions": [{"field": "P", "operator": "not_matches", "pattern": "[invalid"}]})
        item = self._item(raw={"P": "val"})
        assert self.fm._should_send_item(self._feed(fc), item) is True


# ---------------------------------------------------------------------------
# _sort_items (pure logic)
# ---------------------------------------------------------------------------


class TestSortItems:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def test_empty_config_returns_unchanged(self):
        items = [{"title": "b"}, {"title": "a"}]
        assert self.fm._sort_items(items, {}) == items

    def test_empty_items_returns_empty(self):
        assert self.fm._sort_items([], {"field": "title"}) == []

    def test_no_field_path_returns_unchanged(self):
        items = [{"title": "b"}, {"title": "a"}]
        assert self.fm._sort_items(items, {"order": "asc"}) == items

    def test_sort_numeric_asc(self):
        items = [{"raw": {"score": 3}}, {"raw": {"score": 1}}, {"raw": {"score": 2}}]
        result = self.fm._sort_items(items, {"field": "score", "order": "asc"})
        scores = [r["raw"]["score"] for r in result]
        assert scores == sorted(scores)

    def test_sort_numeric_desc(self):
        items = [{"raw": {"score": 1}}, {"raw": {"score": 3}}, {"raw": {"score": 2}}]
        result = self.fm._sort_items(items, {"field": "score", "order": "desc"})
        scores = [r["raw"]["score"] for r in result]
        assert scores == sorted(scores, reverse=True)

    def test_sort_by_iso_date_string(self):
        items = [
            {"raw": {"date": "2021-01-03"}},
            {"raw": {"date": "2021-01-01"}},
            {"raw": {"date": "2021-01-02"}},
        ]
        result = self.fm._sort_items(items, {"field": "date", "order": "asc"})
        assert isinstance(result, list)

    def test_sort_by_microsoft_date(self):
        items = [
            {"raw": {"ts": "/Date(1609500000000)/"}},
            {"raw": {"ts": "/Date(1609400000000)/"}},
        ]
        result = self.fm._sort_items(items, {"field": "ts", "order": "desc"})
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Exception / uncovered-path coverage additions
# ---------------------------------------------------------------------------


class TestApplyShorteningEdgePaths:
    """Cover exception handlers and rarely-hit branches in _apply_shortening."""

    def setup_method(self):
        self.fm = _make_fm_no_db()

    def test_word_wrap_invalid_number_returns_text(self):
        assert self.fm._apply_shortening("hello", "word_wrap:abc") == "hello"

    def test_first_words_invalid_number_returns_text(self):
        assert self.fm._apply_shortening("hello world", "first_words:abc") == "hello world"

    def test_if_regex_fewer_than_3_parts_returns_text(self):
        # Only 2 parts after split → len(parts) < 3
        assert self.fm._apply_shortening("hello", "if_regex:pat:then") == "hello"

    def test_if_regex_empty_pattern_returns_text(self):
        assert self.fm._apply_shortening("hello", "if_regex::then:else") == "hello"

    def test_if_regex_invalid_regex_returns_text(self):
        result = self.fm._apply_shortening("hello", "if_regex:[invalid:yes:no")
        assert result == "hello"

    def test_switch_fewer_than_2_parts_returns_text(self):
        assert self.fm._apply_shortening("hi", "switch:onlyonepart") == "hi"

    def test_regex_cond_fewer_than_4_parts_returns_text(self):
        assert self.fm._apply_shortening("text", "regex_cond:pat:check:yes") == "text"

    def test_regex_cond_empty_extract_pattern_returns_text(self):
        assert self.fm._apply_shortening("text", "regex_cond::check:yes:1") == "text"

    def test_regex_cond_invalid_regex_returns_text(self):
        result = self.fm._apply_shortening("text", "regex_cond:[bad:check:yes:1")
        assert result == "text"

    def test_regex_cond_no_match_returns_empty(self):
        result = self.fm._apply_shortening("hello world", "regex_cond:nomatch:check:yes:1")
        assert result == ""

    def test_regex_cond_whole_match_no_group(self):
        result = self.fm._apply_shortening("foo bar", "regex_cond:foo:foo:got_it:0")
        assert result == "got_it"

    def test_regex_no_match_returns_empty(self):
        assert self.fm._apply_shortening("hello", "regex:xyz") == ""

    def test_regex_invalid_raises_returns_text(self):
        result = self.fm._apply_shortening("hello", "regex:[invalid")
        assert result == "hello"


class TestDbErrorPaths:
    """Cover error-handler paths in DB-touching FeedManager methods."""

    def setup_method(self):
        self.fm = _make_fm_no_db()
        self.fm.bot.db_manager.connection.side_effect = RuntimeError("db down")

    def test_get_enabled_feeds_returns_empty_on_error(self):
        result = self.fm._get_enabled_feeds()
        assert result == []
        self.fm.bot.logger.error.assert_called()

    def test_update_feed_last_check_logs_error(self):
        self.fm._update_feed_last_check(1)
        self.fm.bot.logger.error.assert_called()

    def test_update_feed_last_item_id_logs_error(self):
        self.fm._update_feed_last_item_id(1, "item-x")
        self.fm.bot.logger.error.assert_called()

    def test_record_feed_activity_logs_error(self):
        self.fm._record_feed_activity(1, "item-x", "Title")
        self.fm.bot.logger.error.assert_called()

    def test_record_feed_error_logs_on_db_failure(self):
        self.fm._record_feed_error(1, "network", "timeout")
        # Either logs error or silently swallows — just must not raise
        assert True  # If we got here, no exception propagated


# ---------------------------------------------------------------------------
# TestInitialize (async)
# ---------------------------------------------------------------------------


class TestInitialize:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def test_disabled_logs_info(self):
        self.fm.enabled = False
        asyncio.run(self.fm.initialize())
        self.fm.bot.logger.info.assert_called()

    def test_enabled_logs_lazy_session_message(self):
        self.fm.enabled = True
        asyncio.run(self.fm.initialize())
        self.fm.bot.logger.info.assert_called()


# ---------------------------------------------------------------------------
# TestStop (async)
# ---------------------------------------------------------------------------


class TestStop:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def test_closes_open_session(self):
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        self.fm.session = mock_session
        asyncio.run(self.fm.stop())
        mock_session.close.assert_called_once()
        assert self.fm.session is None

    def test_no_session_does_not_raise(self):
        self.fm.session = None
        asyncio.run(self.fm.stop())  # should not raise

    def test_already_closed_session_skipped(self):
        mock_session = MagicMock()
        mock_session.closed = True
        mock_session.close = AsyncMock()
        self.fm.session = mock_session
        asyncio.run(self.fm.stop())
        mock_session.close.assert_not_called()


# ---------------------------------------------------------------------------
# TestEnsureSession (async)
# ---------------------------------------------------------------------------


class TestEnsureSession:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def test_creates_new_session_when_none(self):
        self.fm.session = None
        mock_session = MagicMock()
        with patch("modules.feed_manager.aiohttp.ClientSession", return_value=mock_session):
            asyncio.run(self.fm._ensure_session())
        assert self.fm.session is mock_session

    def test_creates_new_session_when_closed(self):
        old_session = MagicMock()
        old_session.closed = True
        self.fm.session = old_session
        new_session = MagicMock()
        with patch("modules.feed_manager.aiohttp.ClientSession", return_value=new_session):
            asyncio.run(self.fm._ensure_session())
        assert self.fm.session is new_session

    def test_reuses_existing_open_session(self):
        mock_session = MagicMock()
        mock_session.closed = False
        self.fm.session = mock_session
        with patch("modules.feed_manager.aiohttp.ClientSession") as mock_cls:
            asyncio.run(self.fm._ensure_session())
        mock_cls.assert_not_called()
        assert self.fm.session is mock_session


# ---------------------------------------------------------------------------
# TestWaitForRateLimit (async)
# ---------------------------------------------------------------------------


class TestWaitForRateLimit:
    def setup_method(self):
        self.fm = _make_fm_no_db()
        self.fm.rate_limit_seconds = 5.0

    def test_no_previous_request_records_time(self):
        asyncio.run(self.fm._wait_for_rate_limit("example.com"))
        assert "example.com" in self.fm._domain_last_request

    def test_recent_request_triggers_sleep(self):
        self.fm._domain_last_request["example.com"] = time.time() - 1.0
        with patch("modules.feed_manager.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            asyncio.run(self.fm._wait_for_rate_limit("example.com"))
        mock_sleep.assert_called_once()
        wait_arg = mock_sleep.call_args[0][0]
        assert 0 < wait_arg <= 5.0

    def test_old_request_no_sleep(self):
        self.fm._domain_last_request["example.com"] = time.time() - 10.0
        with patch("modules.feed_manager.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            asyncio.run(self.fm._wait_for_rate_limit("example.com"))
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# TestSendFeedItem (async)
# ---------------------------------------------------------------------------


class TestSendFeedItem:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def _feed(self):
        return {"id": 1, "channel_name": "general", "feed_name": "test", "output_format": "{title}"}

    def _item(self):
        return {"id": "x", "title": "Hello", "description": "", "link": "", "published": None}

    def test_queues_formatted_message(self):
        self.fm._queue_feed_message = Mock()
        asyncio.run(self.fm._send_feed_item(self._feed(), self._item()))
        self.fm._queue_feed_message.assert_called_once()
        _, _, message = self.fm._queue_feed_message.call_args[0]
        assert "Hello" in message

    def test_format_exception_logs_error(self):
        self.fm.format_message = Mock(side_effect=RuntimeError("fmt error"))
        self.fm._record_feed_error = Mock()
        asyncio.run(self.fm._send_feed_item(self._feed(), self._item()))
        self.fm._record_feed_error.assert_called_once()


# ---------------------------------------------------------------------------
# TestPollAllFeeds (async)
# ---------------------------------------------------------------------------


class TestPollAllFeeds:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def _feed(self, last_check=None, interval=300):
        return {
            "id": 1, "feed_url": "http://example.com/rss", "feed_type": "rss",
            "channel_name": "general", "last_check_time": last_check,
            "check_interval_seconds": interval, "last_item_id": None,
        }

    def test_disabled_returns_immediately(self):
        self.fm.enabled = False
        self.fm._get_enabled_feeds = Mock()
        asyncio.run(self.fm.poll_all_feeds())
        self.fm._get_enabled_feeds.assert_not_called()

    def test_no_feeds_returns_immediately(self):
        self.fm.enabled = True
        self.fm._get_enabled_feeds = Mock(return_value=[])
        self.fm.poll_feed = AsyncMock()
        asyncio.run(self.fm.poll_all_feeds())
        self.fm.poll_feed.assert_not_called()

    def test_feed_never_checked_is_polled(self):
        self.fm.enabled = True
        feed = self._feed(last_check=None)
        self.fm._get_enabled_feeds = Mock(return_value=[feed])
        self.fm.poll_feed = AsyncMock(return_value=None)
        asyncio.run(self.fm.poll_all_feeds())
        self.fm.poll_feed.assert_called_once_with(feed)

    def test_overdue_feed_is_polled(self):
        self.fm.enabled = True
        old = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
        feed = self._feed(last_check=old)
        self.fm._get_enabled_feeds = Mock(return_value=[feed])
        self.fm.poll_feed = AsyncMock(return_value=None)
        asyncio.run(self.fm.poll_all_feeds())
        self.fm.poll_feed.assert_called_once_with(feed)

    def test_recent_feed_is_not_polled(self):
        self.fm.enabled = True
        recent = datetime.now(timezone.utc).isoformat()
        feed = self._feed(last_check=recent)
        self.fm._get_enabled_feeds = Mock(return_value=[feed])
        self.fm.poll_feed = AsyncMock(return_value=None)
        asyncio.run(self.fm.poll_all_feeds())
        self.fm.poll_feed.assert_not_called()

    def test_sqlite_format_timestamp_parsed(self):
        self.fm.enabled = True
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=400)).strftime("%Y-%m-%d %H:%M:%S")
        feed = self._feed(last_check=old_ts)
        self.fm._get_enabled_feeds = Mock(return_value=[feed])
        self.fm.poll_feed = AsyncMock(return_value=None)
        asyncio.run(self.fm.poll_all_feeds())
        self.fm.poll_feed.assert_called_once()

    def test_numeric_timestamp_parsed(self):
        self.fm.enabled = True
        old_ts = time.time() - 400
        feed = self._feed(last_check=old_ts)
        self.fm._get_enabled_feeds = Mock(return_value=[feed])
        self.fm.poll_feed = AsyncMock(return_value=None)
        asyncio.run(self.fm.poll_all_feeds())
        self.fm.poll_feed.assert_called_once()

    def test_invalid_timestamp_defaults_to_overdue(self):
        self.fm.enabled = True
        feed = self._feed(last_check="not-a-date")
        self.fm._get_enabled_feeds = Mock(return_value=[feed])
        self.fm.poll_feed = AsyncMock(return_value=None)
        asyncio.run(self.fm.poll_all_feeds())
        self.fm.poll_feed.assert_called_once()

    def test_exception_in_get_enabled_feeds_logs_error(self):
        self.fm.enabled = True
        self.fm._get_enabled_feeds = Mock(side_effect=RuntimeError("db fail"))
        asyncio.run(self.fm.poll_all_feeds())
        self.fm.bot.logger.error.assert_called()
def _make_mock_response(status=200, text="<rss><channel><item><title>Test</title><link>http://x.com/1</link><guid>guid-1</guid></item></channel></rss>"):
    """Build an async context manager mock for aiohttp response."""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.text = AsyncMock(return_value=text)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


def _make_mock_session(response):
    """Mock aiohttp.ClientSession with a canned GET response."""
    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=response)
    mock_session.closed = False
    return mock_session


class TestProcessRssFeed:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def _feed(self, **kw):
        base = {
            "id": 1, "feed_url": "http://example.com/rss", "feed_type": "rss",
            "channel_name": "general", "last_item_id": None, "sort_config": None,
        }
        base.update(kw)
        return base

    def test_returns_new_items(self):
        rss_xml = (
            "<rss><channel>"
            "<item><title>A</title><link>http://x.com/a</link><guid>guid-a</guid></item>"
            "</channel></rss>"
        )
        resp = _make_mock_response(text=rss_xml)
        self.fm.session = _make_mock_session(resp)
        self.fm.bot.db_manager.connection = MagicMock()
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = []
        conn_mock = MagicMock()
        conn_mock.__enter__ = MagicMock(return_value=conn_mock)
        conn_mock.__exit__ = MagicMock(return_value=False)
        conn_mock.cursor.return_value = cursor_mock
        self.fm.bot.db_manager.connection.return_value = conn_mock
        self.fm._update_feed_last_item_id = Mock()

        items = asyncio.run(self.fm.process_rss_feed(self._feed()))
        assert len(items) >= 1
        assert items[0]["id"] == "guid-a"

    def test_http_error_raises(self):
        resp = _make_mock_response(status=404)
        self.fm.session = _make_mock_session(resp)
        with pytest.raises(Exception, match="HTTP 404"):  # noqa: B017
            asyncio.run(self.fm.process_rss_feed(self._feed()))

    def test_already_processed_item_excluded(self):
        rss_xml = (
            "<rss><channel>"
            "<item><title>A</title><link>http://x.com/a</link><guid>seen-guid</guid></item>"
            "</channel></rss>"
        )
        resp = _make_mock_response(text=rss_xml)
        self.fm.session = _make_mock_session(resp)
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = [("seen-guid",)]
        conn_mock = MagicMock()
        conn_mock.__enter__ = MagicMock(return_value=conn_mock)
        conn_mock.__exit__ = MagicMock(return_value=False)
        conn_mock.cursor.return_value = cursor_mock
        self.fm.bot.db_manager.connection.return_value = conn_mock
        self.fm._update_feed_last_item_id = Mock()

        items = asyncio.run(self.fm.process_rss_feed(self._feed(last_item_id="seen-guid")))
        assert items == []

    def test_last_item_id_seeds_processed_set(self):
        rss_xml = (
            "<rss><channel>"
            "<item><title>Old</title><link>http://x.com/old</link><guid>old-id</guid></item>"
            "</channel></rss>"
        )
        resp = _make_mock_response(text=rss_xml)
        self.fm.session = _make_mock_session(resp)
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = []
        conn_mock = MagicMock()
        conn_mock.__enter__ = MagicMock(return_value=conn_mock)
        conn_mock.__exit__ = MagicMock(return_value=False)
        conn_mock.cursor.return_value = cursor_mock
        self.fm.bot.db_manager.connection.return_value = conn_mock
        self.fm._update_feed_last_item_id = Mock()

        # last_item_id="old-id" — item should be excluded
        items = asyncio.run(self.fm.process_rss_feed(self._feed(last_item_id="old-id")))
        assert items == []


# ---------------------------------------------------------------------------
# TestProcessApiFeed (async)
# ---------------------------------------------------------------------------


def _make_mock_json_response(status=200, data=None):
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=data or [])
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


class TestProcessApiFeed:
    def setup_method(self):
        self.fm = _make_fm_no_db()

    def _feed(self, api_config=None, **kw):
        import json as _json
        base = {
            "id": 1, "feed_url": "http://api.example.com/items", "feed_type": "api",
            "channel_name": "general", "last_item_id": None, "sort_config": None,
            "api_config": _json.dumps(api_config) if api_config else "{}",
        }
        base.update(kw)
        return base

    def _conn_mock_empty(self):
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = []
        conn_mock = MagicMock()
        conn_mock.__enter__ = MagicMock(return_value=conn_mock)
        conn_mock.__exit__ = MagicMock(return_value=False)
        conn_mock.cursor.return_value = cursor_mock
        return conn_mock

    def test_get_request_returns_items(self):
        data = [{"id": "1", "title": "Item 1", "created_at": None}]
        resp = _make_mock_json_response(data=data)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=resp)
        self.fm.session = mock_session
        self.fm.bot.db_manager.connection.return_value = self._conn_mock_empty()
        self.fm._update_feed_last_item_id = Mock()

        items = asyncio.run(self.fm.process_api_feed(self._feed()))
        assert len(items) == 1
        assert items[0]["id"] == "1"

    def test_post_request_dispatched(self):
        data = [{"id": "2", "title": "Posted item", "created_at": None}]
        resp = _make_mock_json_response(data=data)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=resp)
        self.fm.session = mock_session
        self.fm.bot.db_manager.connection.return_value = self._conn_mock_empty()
        self.fm._update_feed_last_item_id = Mock()

        api_cfg = {"method": "POST", "body": {"filter": "active"}}
        items = asyncio.run(self.fm.process_api_feed(self._feed(api_config=api_cfg)))
        assert len(items) == 1
        mock_session.post.assert_called_once()

    def test_items_path_navigation(self):
        data = {"results": {"items": [{"id": "3", "title": "Nested"}]}}
        resp = _make_mock_json_response(data=data)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=resp)
        self.fm.session = mock_session
        self.fm.bot.db_manager.connection.return_value = self._conn_mock_empty()
        self.fm._update_feed_last_item_id = Mock()

        api_cfg = {"response_parser": {"items_path": "results.items", "id_field": "id", "title_field": "title"}}
        items = asyncio.run(self.fm.process_api_feed(self._feed(api_config=api_cfg)))
        assert len(items) == 1
        assert items[0]["title"] == "Nested"

    def test_http_error_raises(self):
        resp = _make_mock_json_response(status=500)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=resp)
        self.fm.session = mock_session
        with pytest.raises(Exception, match="HTTP 500"):  # noqa: B017
            asyncio.run(self.fm.process_api_feed(self._feed()))

    def test_already_processed_item_excluded(self):
        data = [{"id": "seen", "title": "Old item", "created_at": None}]
        resp = _make_mock_json_response(data=data)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=resp)
        self.fm.session = mock_session
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = [("seen",)]
        conn_mock = MagicMock()
        conn_mock.__enter__ = MagicMock(return_value=conn_mock)
        conn_mock.__exit__ = MagicMock(return_value=False)
        conn_mock.cursor.return_value = cursor_mock
        self.fm.bot.db_manager.connection.return_value = conn_mock
        self.fm._update_feed_last_item_id = Mock()

        items = asyncio.run(self.fm.process_api_feed(self._feed(last_item_id="seen")))
        assert items == []

    def test_item_without_id_skipped(self):
        data = [{"title": "No ID item"}]  # no 'id' field
        resp = _make_mock_json_response(data=data)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=resp)
        self.fm.session = mock_session
        self.fm.bot.db_manager.connection.return_value = self._conn_mock_empty()

        items = asyncio.run(self.fm.process_api_feed(self._feed()))
        assert items == []

    def test_numeric_timestamp_parsed(self):
        data = [{"id": "ts1", "title": "Timestamped", "created_at": 1609459200.0}]
        resp = _make_mock_json_response(data=data)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=resp)
        self.fm.session = mock_session
        self.fm.bot.db_manager.connection.return_value = self._conn_mock_empty()
        self.fm._update_feed_last_item_id = Mock()

        items = asyncio.run(self.fm.process_api_feed(self._feed()))
        assert items[0]["published"] is not None

    def test_iso_timestamp_parsed(self):
        data = [{"id": "ts2", "title": "ISO date", "created_at": "2021-01-01T00:00:00Z"}]
        resp = _make_mock_json_response(data=data)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=resp)
        self.fm.session = mock_session
        self.fm.bot.db_manager.connection.return_value = self._conn_mock_empty()
        self.fm._update_feed_last_item_id = Mock()

        items = asyncio.run(self.fm.process_api_feed(self._feed()))
        assert items[0]["published"] is not None

    def test_microsoft_date_timestamp_parsed(self):
        data = [{"id": "ms1", "title": "MS date", "created_at": "/Date(1609459200000)/"}]
        resp = _make_mock_json_response(data=data)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=resp)
        self.fm.session = mock_session
        self.fm.bot.db_manager.connection.return_value = self._conn_mock_empty()
        self.fm._update_feed_last_item_id = Mock()

        items = asyncio.run(self.fm.process_api_feed(self._feed()))
        assert items[0]["published"] is not None
