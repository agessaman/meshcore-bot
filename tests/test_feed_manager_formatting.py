"""Tests for FeedManager pure formatting and filtering logic."""

import json
from datetime import datetime, timezone, timedelta

import pytest
from configparser import ConfigParser
from unittest.mock import Mock

from modules.feed_manager import FeedManager


@pytest.fixture
def fm(mock_logger):
    """FeedManager with disabled networking for pure-logic tests."""
    bot = Mock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("Feed_Manager")
    bot.config.set("Feed_Manager", "feed_manager_enabled", "false")
    bot.config.set("Feed_Manager", "max_message_length", "200")
    bot.db_manager = Mock()
    bot.db_manager.db_path = "/dev/null"
    return FeedManager(bot)


class TestApplyShortening:
    """Tests for _apply_shortening()."""

    def test_truncate_short_text_unchanged(self, fm):
        assert fm._apply_shortening("hello", "truncate:20") == "hello"

    def test_truncate_long_text_adds_ellipsis(self, fm):
        result = fm._apply_shortening("Hello World", "truncate:5")
        assert result == "Hello..."

    def test_word_wrap_breaks_at_boundary(self, fm):
        result = fm._apply_shortening("Hello beautiful world", "word_wrap:15")
        # word_wrap truncates at a word boundary and appends "..."
        # "Hello beautiful world"[:15] = "Hello beautiful", last space at 5 (too early),
        # so result is "Hello beautiful..." (truncated at 15 chars + ellipsis)
        assert result.endswith("...")
        # The base text (without ellipsis) should be <= the wrap limit
        assert len(result.rstrip(".")) <= 15 or result == "Hello beautiful..."

    def test_first_words_limits_count(self, fm):
        result = fm._apply_shortening("one two three four", "first_words:2")
        assert result.startswith("one two")

    def test_regex_extracts_group(self, fm):
        result = fm._apply_shortening("Price: $42.99 today", r"regex:Price: \$(\d+\.\d+)")
        assert result == "42.99"

    def test_if_regex_returns_then_on_match(self, fm):
        result = fm._apply_shortening("open", "if_regex:open:YES:NO")
        assert result == "YES"

    def test_if_regex_returns_else_on_no_match(self, fm):
        result = fm._apply_shortening("closed", "if_regex:open:YES:NO")
        assert result == "NO"

    def test_empty_text_returns_empty(self, fm):
        assert fm._apply_shortening("", "truncate:10") == ""


class TestGetNestedValue:
    """Tests for _get_nested_value()."""

    def test_simple_field_access(self, fm):
        assert fm._get_nested_value({"name": "test"}, "name") == "test"

    def test_nested_field_access(self, fm):
        data = {"raw": {"Priority": "high"}}
        assert fm._get_nested_value(data, "raw.Priority") == "high"

    def test_missing_field_returns_default(self, fm):
        assert fm._get_nested_value({}, "missing") == ""
        assert fm._get_nested_value({}, "missing", "N/A") == "N/A"


class TestShouldSendItem:
    """Tests for _should_send_item() filter evaluation."""

    def test_no_filter_sends_all(self, fm):
        feed = {"id": 1}
        item = {"raw": {"Priority": "low"}}
        assert fm._should_send_item(feed, item) is True

    def test_equals_filter_matches(self, fm):
        feed = {
            "id": 1,
            "filter_config": json.dumps({
                "conditions": [
                    {"field": "Priority", "operator": "equals", "value": "high"}
                ]
            }),
        }
        item = {"raw": {"Priority": "high"}}
        assert fm._should_send_item(feed, item) is True

    def test_equals_filter_rejects(self, fm):
        feed = {
            "id": 1,
            "filter_config": json.dumps({
                "conditions": [
                    {"field": "Priority", "operator": "equals", "value": "high"}
                ]
            }),
        }
        item = {"raw": {"Priority": "low"}}
        assert fm._should_send_item(feed, item) is False

    def test_in_filter_matches(self, fm):
        feed = {
            "id": 1,
            "filter_config": json.dumps({
                "conditions": [
                    {"field": "Priority", "operator": "in", "values": ["high", "highest"]}
                ]
            }),
        }
        item = {"raw": {"Priority": "highest"}}
        assert fm._should_send_item(feed, item) is True

    def test_and_logic_all_must_pass(self, fm):
        feed = {
            "id": 1,
            "filter_config": json.dumps({
                "conditions": [
                    {"field": "Priority", "operator": "equals", "value": "high"},
                    {"field": "Status", "operator": "equals", "value": "open"},
                ],
                "logic": "AND",
            }),
        }
        # First condition passes, second fails
        item = {"raw": {"Priority": "high", "Status": "closed"}}
        assert fm._should_send_item(feed, item) is False


class TestFormatTimestamp:
    """Tests for _format_timestamp()."""

    def test_recent_timestamp(self, fm):
        five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = fm._format_timestamp(five_min_ago)
        assert "5m ago" in result

    def test_none_returns_empty(self, fm):
        assert fm._format_timestamp(None) == ""
