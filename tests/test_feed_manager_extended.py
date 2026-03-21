"""Extended FeedManager tests: sort, format_message, mocked RSS/API fetch, queue processing."""

from __future__ import annotations

import json
from configparser import ConfigParser
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.db_manager import DBManager
from modules.feed_manager import FeedManager


def _feed_manager_bot(mock_logger, db_path: str):
    bot = Mock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("Feed_Manager")
    bot.config.set("Feed_Manager", "feed_manager_enabled", "false")
    bot.config.set("Feed_Manager", "max_message_length", "200")
    bot.db_manager = DBManager(bot, db_path)
    return bot


@pytest.fixture
def fm_with_db(mock_logger, tmp_path):
    """FeedManager backed by a real file SQLite DB (feed tables from DBManager)."""
    db_path = str(tmp_path / "feeds.db")
    bot = _feed_manager_bot(mock_logger, db_path)
    return FeedManager(bot)


def _fake_aiohttp_response(*, text_body: str | None = None, json_body: dict | list | None = None):
    """Build a minimal async context manager compatible with async with session.get/post."""
    resp = Mock()
    resp.status = 200
    if text_body is not None:
        resp.text = AsyncMock(return_value=text_body)
    if json_body is not None:
        resp.json = AsyncMock(return_value=json_body)

    class _CM:
        def __init__(self, r):
            self._r = r

        async def __aenter__(self):
            return self._r

        async def __aexit__(self, exc_type, exc, tb):
            return False

    return _CM(resp)


class TestSortItems:
    def test_sort_by_published_desc(self, fm_with_db):
        fm = fm_with_db
        older = datetime(2020, 1, 1, tzinfo=timezone.utc)
        newer = datetime(2025, 6, 1, tzinfo=timezone.utc)
        items = [
            {"id": "a", "title": "old", "published": older, "raw": {}},
            {"id": "b", "title": "new", "published": newer, "raw": {}},
        ]
        out = fm._sort_items(items, {"field": "published", "order": "desc"})
        assert [x["id"] for x in out] == ["b", "a"]

    def test_sort_by_raw_numeric_timestamp_asc(self, fm_with_db):
        fm = fm_with_db
        items = [
            {"id": "2", "title": "t2", "raw": {"t": 200.0}, "published": None},
            {"id": "1", "title": "t1", "raw": {"t": 100.0}, "published": None},
        ]
        out = fm._sort_items(items, {"field": "raw.t", "order": "asc"})
        assert [x["id"] for x in out] == ["1", "2"]

    def test_sort_empty_field_returns_unchanged(self, fm_with_db):
        fm = fm_with_db
        items = [{"id": "x", "title": "a"}]
        out = fm._sort_items(items, {"field": "", "order": "desc"})
        assert out == items


class TestFormatMessage:
    def test_basic_placeholders(self, fm_with_db):
        fm = fm_with_db
        now = datetime.now(timezone.utc)
        feed = {"output_format": "{emoji} {title}\n{link}\n{date}", "feed_name": "news"}
        item = {
            "title": "Hello",
            "link": "https://ex.com/a",
            "description": "",
            "published": now,
            "raw": {},
        }
        msg = fm.format_message(item, feed)
        assert "Hello" in msg
        assert "https://ex.com/a" in msg
        assert "📢" in msg or "ℹ️" in msg  # emoji from feed_name or default

    def test_strips_br_and_html_from_body(self, fm_with_db):
        fm = fm_with_db
        feed = {"output_format": "{body}"}
        item = {
            "title": "t",
            "description": 'Line1<br/>Line2<p>Para</p>',
            "published": None,
            "raw": {},
        }
        msg = fm.format_message(item, feed)
        assert "<br" not in msg.lower()
        assert "<p" not in msg.lower()
        assert "Line1" in msg and "Line2" in msg

    def test_raw_field_with_truncate(self, fm_with_db):
        fm = fm_with_db
        feed = {"output_format": "{raw.Status|truncate:4}"}
        item = {
            "title": "t",
            "description": "",
            "published": None,
            "raw": {"Status": "open"},
        }
        assert fm.format_message(item, feed) == "open"

    def test_max_message_length_truncates(self, fm_with_db):
        fm = fm_with_db
        fm.max_message_length = 20
        feed = {"output_format": "{title}"}
        item = {"title": "x" * 40, "description": "", "published": None, "raw": {}}
        msg = fm.format_message(item, feed)
        assert len(msg) <= 23  # 20 + "..."
        assert msg.endswith("...")


class TestProcessRssFeed:
    @pytest.mark.asyncio
    async def test_returns_items_from_xml(self, fm_with_db):
        fm = fm_with_db
        rss = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>T</title>
<item><title>One</title><link>http://e/1</link><guid>g1</guid><description>D1</description></item>
<item><title>Two</title><link>http://e/2</link><guid>g2</guid><description>D2</description></item>
</channel></rss>"""
        ctx = _fake_aiohttp_response(text_body=rss)
        fm.session = Mock()
        fm.session.get = Mock(return_value=ctx)
        fm.session.closed = False

        feed = {"id": 1, "feed_url": "http://example.com/feed.xml"}
        items = await fm.process_rss_feed(feed)
        titles = {i["title"] for i in items}
        assert titles == {"One", "Two"}

    @pytest.mark.asyncio
    async def test_skips_already_processed(self, fm_with_db):
        fm = fm_with_db
        rss = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>T</title>
<item><title>Old</title><link>http://e/1</link><guid>g1</guid><description></description></item>
<item><title>New</title><link>http://e/2</link><guid>g2</guid><description></description></item>
</channel></rss>"""
        ctx = _fake_aiohttp_response(text_body=rss)
        fm.session = Mock()
        fm.session.get = Mock(return_value=ctx)
        fm.session.closed = False

        with fm.bot.db_manager.connection() as conn:
            conn.execute(
                "INSERT INTO feed_activity (feed_id, item_id, item_title, message_sent) VALUES (?,?,?,1)",
                (1, "g1", "Old"),
            )
            conn.commit()

        feed = {"id": 1, "feed_url": "http://example.com/feed.xml"}
        items = await fm.process_rss_feed(feed)
        assert len(items) == 1
        assert items[0]["title"] == "New"


class TestProcessApiFeed:
    @pytest.mark.asyncio
    async def test_get_parses_items_path(self, fm_with_db):
        fm = fm_with_db
        payload = {
            "data": {
                "rows": [
                    {"id": "10", "name": "Alpha", "created_at": 1700000000},
                ]
            }
        }
        ctx = _fake_aiohttp_response(json_body=payload)
        fm.session = Mock()
        fm.session.get = Mock(return_value=ctx)
        fm.session.closed = False

        api_config = json.dumps(
            {
                "response_parser": {
                    "items_path": "data.rows",
                    "id_field": "id",
                    "title_field": "name",
                    "timestamp_field": "created_at",
                }
            }
        )
        feed = {"id": 2, "feed_url": "http://api.example.com/x", "api_config": api_config}
        items = await fm.process_api_feed(feed)
        assert len(items) == 1
        assert items[0]["id"] == "10"
        assert items[0]["title"] == "Alpha"

    @pytest.mark.asyncio
    async def test_post_json_body(self, fm_with_db):
        fm = fm_with_db
        payload = [{"id": "z", "title": "Zed", "created_at": 1600000000}]
        ctx = _fake_aiohttp_response(json_body=payload)
        fm.session = Mock()
        fm.session.post = Mock(return_value=ctx)
        fm.session.closed = False

        api_config = json.dumps(
            {
                "method": "POST",
                "body": {"q": 1},
                "response_parser": {
                    "items_path": "",
                    "id_field": "id",
                    "title_field": "title",
                    "timestamp_field": "created_at",
                },
            }
        )
        feed = {"id": 3, "feed_url": "http://api.example.com/post", "api_config": api_config}
        items = await fm.process_api_feed(feed)
        assert len(items) == 1
        assert items[0]["title"] == "Zed"


class TestQueueAndProcessMessageQueue:
    def test_queue_feed_message_inserts_row(self, fm_with_db):
        fm = fm_with_db
        with fm.bot.db_manager.connection() as conn:
            conn.execute(
                "INSERT INTO feed_subscriptions (feed_type, feed_url, channel_name, message_send_interval_seconds) VALUES (?,?,?,?)",
                ("rss", "http://x", "#alerts", 0.0),
            )
            conn.commit()
            fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        feed = {"id": fid, "channel_name": "#alerts"}
        item = {"id": "i1", "title": "T1"}
        fm._queue_feed_message(feed, item, "hello mesh")

        with fm.bot.db_manager.connection() as conn:
            row = conn.execute(
                "SELECT message, sent_at FROM feed_message_queue WHERE feed_id = ?",
                (fid,),
            ).fetchone()
            assert row[0] == "hello mesh"
            assert row[1] is None

    @pytest.mark.asyncio
    async def test_process_message_queue_sends_and_marks_sent(self, fm_with_db):
        fm = fm_with_db
        bot = fm.bot
        bot.command_manager = MagicMock()
        bot.command_manager.send_channel_message = AsyncMock(return_value=True)

        with bot.db_manager.connection() as conn:
            conn.execute(
                "INSERT INTO feed_subscriptions (feed_type, feed_url, channel_name, message_send_interval_seconds) VALUES (?,?,?,?)",
                ("rss", "http://y", "#news", 0.0),
            )
            fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO feed_message_queue (feed_id, channel_name, message, item_id, item_title, priority)
                   VALUES (?,?,?,?,?,0)""",
                (fid, "#news", "queued body", "q1", "Queued title"),
            )
            conn.commit()

        await fm.process_message_queue()

        bot.command_manager.send_channel_message.assert_awaited_once_with("#news", "queued body")

        with bot.db_manager.connection() as conn:
            sent = conn.execute(
                "SELECT sent_at IS NOT NULL FROM feed_message_queue WHERE item_id = ?",
                ("q1",),
            ).fetchone()[0]
            assert sent == 1

            act = conn.execute(
                "SELECT COUNT(*) FROM feed_activity WHERE feed_id = ? AND item_id = ?",
                (fid, "q1"),
            ).fetchone()[0]
            assert act == 1
