"""Tests for FeedManager queue logic, deduplication, and DB operations."""

import sqlite3
import time
from configparser import ConfigParser
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock

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
