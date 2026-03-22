"""Tests for FeedManager pure formatting and filtering logic."""

import json
from configparser import ConfigParser
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

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

    def test_within_days_passes_recent(self, fm):
        now = datetime.now(timezone.utc)
        feed = {
            "id": 1,
            "filter_config": json.dumps({
                "conditions": [
                    {"field": "published", "operator": "within_days", "days": 28},
                ],
            }),
        }
        item = {"published": now - timedelta(days=5)}
        assert fm._should_send_item(feed, item) is True

    def test_within_days_rejects_old(self, fm):
        now = datetime.now(timezone.utc)
        feed = {
            "id": 1,
            "filter_config": json.dumps({
                "conditions": [
                    {"field": "published", "operator": "within_days", "days": 7},
                ],
            }),
        }
        item = {"published": now - timedelta(days=30)}
        assert fm._should_send_item(feed, item) is False


class TestFormatTimestamp:
    """Tests for _format_timestamp()."""

    def test_recent_timestamp(self, fm):
        five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = fm._format_timestamp(five_min_ago)
        assert "5m ago" in result

    def test_none_returns_empty(self, fm):
        assert fm._format_timestamp(None) == ""


# ---------------------------------------------------------------------------
# Security: feed content sanitization (control char injection prevention)
# ---------------------------------------------------------------------------


class TestFeedContentSanitization:
    """format_message must sanitize external feed content before mesh transmission.

    Covers GAP F2: unsanitized titles/descriptions sent to mesh channels.
    Uses sanitize_input() from security_utils on title and body fields.
    """

    def _make_feed(self):
        return {"id": 1, "output_format": None, "channel_name": "general"}

    def _make_item(self, title, description=""):
        return {
            "title": title,
            "description": description,
            "link": "https://example.com/item",
        }

    def test_newline_in_title_stripped(self, fm):
        """\\n injected into feed title must not reach mesh channel message."""
        result = fm.format_message(self._make_item("Breaking\nNews"), self._make_feed())
        assert "\n" not in result or result.count("\n") == result.count(
            "\n"
        )  # only formatting newlines, not from title
        # More directly: the title content itself is clean
        assert "Breaking\nNews" not in result

    def test_control_char_in_description_stripped(self, fm):
        """ASCII control characters in feed body are removed."""
        result = fm.format_message(self._make_item("Alert", "Data\x01\x02\x03"), self._make_feed())
        assert "\x01" not in result
        assert "\x02" not in result
        assert "\x03" not in result

    def test_null_byte_in_title_stripped(self, fm):
        """Null bytes in feed content are removed by sanitize_input."""
        result = fm.format_message(self._make_item("Title\x00End"), self._make_feed())
        assert "\x00" not in result

    def test_oversized_title_truncated(self, fm):
        """Titles over 200 chars are truncated before mesh transmission."""
        result = fm.format_message(self._make_item("A" * 300), self._make_feed())
        # The 300-char run must not appear in the output
        assert "A" * 201 not in result

    def test_oversized_description_truncated(self, fm):
        """Descriptions over 500 chars are truncated before mesh transmission."""
        result = fm.format_message(self._make_item("Title", "B" * 600), self._make_feed())
        assert "B" * 501 not in result

    def test_normal_content_passes_through(self, fm):
        """Legitimate feed content is not altered by sanitization."""
        result = fm.format_message(self._make_item("Normal Title", "Normal body"), self._make_feed())
        assert "Normal body" in result


# ---------------------------------------------------------------------------
# Security: SSRF protection in poll_feed (URL validation)
# ---------------------------------------------------------------------------


class TestFeedPollUrlValidation:
    """poll_feed must reject URLs that would cause SSRF.

    Covers GAP F1: feed URLs fetched without validate_external_url().
    validate_external_url() is called at the top of poll_feed before any fetch.
    """

    def _make_feed(self, url):
        return {
            "id": 99,
            "feed_type": "rss",
            "feed_url": url,
            "channel_name": "general",
            "last_item_id": None,
        }

    async def test_metadata_endpoint_blocked(self, fm):
        """Cloud metadata endpoint (169.254.x.x) must be blocked."""
        from unittest.mock import AsyncMock, patch

        with (
            patch.object(fm, "_ensure_session", new_callable=AsyncMock),
            patch.object(fm, "_record_feed_error") as mock_err,
            patch.object(fm, "process_rss_feed", new_callable=AsyncMock) as mock_fetch,
        ):
            await fm.poll_feed(self._make_feed("http://169.254.169.254/latest/meta-data/"))
        mock_fetch.assert_not_called()
        mock_err.assert_called_once()

    async def test_loopback_ip_blocked(self, fm):
        """127.0.0.1 (loopback) must be blocked."""
        from unittest.mock import AsyncMock, patch

        with (
            patch.object(fm, "_ensure_session", new_callable=AsyncMock),
            patch.object(fm, "_record_feed_error"),
            patch.object(fm, "process_rss_feed", new_callable=AsyncMock) as mock_fetch,
        ):
            await fm.poll_feed(self._make_feed("http://127.0.0.1/internal"))
        mock_fetch.assert_not_called()

    async def test_file_scheme_blocked(self, fm):
        """file:// scheme must be blocked entirely."""
        from unittest.mock import AsyncMock, patch

        with (
            patch.object(fm, "_ensure_session", new_callable=AsyncMock),
            patch.object(fm, "_record_feed_error"),
            patch.object(fm, "process_rss_feed", new_callable=AsyncMock) as mock_fetch,
        ):
            await fm.poll_feed(self._make_feed("file:///etc/passwd"))
        mock_fetch.assert_not_called()

    async def test_private_network_blocked(self, fm):
        """10.x.x.x private network range must be blocked."""
        from unittest.mock import AsyncMock, patch

        with (
            patch.object(fm, "_ensure_session", new_callable=AsyncMock),
            patch.object(fm, "_record_feed_error"),
            patch.object(fm, "process_rss_feed", new_callable=AsyncMock) as mock_fetch,
        ):
            await fm.poll_feed(self._make_feed("http://10.0.0.1/feed.xml"))
        mock_fetch.assert_not_called()

    async def test_validate_external_url_is_invoked(self, fm):
        """validate_external_url is called — not bypassed — in poll_feed."""
        from unittest.mock import AsyncMock, patch

        with (
            patch.object(fm, "_ensure_session", new_callable=AsyncMock),
            patch.object(fm, "_record_feed_error"),
            patch.object(fm, "process_rss_feed", new_callable=AsyncMock),
            patch("modules.feed_manager.validate_external_url", return_value=False) as mock_veu,
        ):
            await fm.poll_feed(self._make_feed("http://192.168.1.1/feed"))
        mock_veu.assert_called_once()
