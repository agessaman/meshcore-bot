"""Unit tests for modules.feed_filter_eval (filter_config parsing and time windows)."""

from datetime import datetime, timedelta, timezone

from modules.feed_filter_eval import (
    item_passes_filter_config,
    parse_item_field_as_datetime,
    parse_microsoft_date,
)


def _utc_now():
    return datetime.now(timezone.utc)


class TestParseItemFieldAsDatetime:
    def test_published_datetime(self):
        dt = _utc_now() - timedelta(days=1)
        item = {"published": dt}
        assert parse_item_field_as_datetime(item, "published") is not None

    def test_microsoft_date_string(self):
        # /Date(ms)/ format
        ms = int(_utc_now().timestamp() * 1000)
        s = f"/Date({ms})/"
        item = {"raw": {"t": s}}
        assert parse_item_field_as_datetime(item, "raw.t") is not None

    def test_iso_string_in_raw(self):
        item = {"raw": {"when": "2024-06-01T12:00:00Z"}}
        out = parse_item_field_as_datetime(item, "raw.when")
        assert out is not None
        assert out.year == 2024


class TestWithinDaysWeeks:
    def test_within_days_recent_passes(self):
        now = _utc_now()
        item = {"published": now - timedelta(days=5)}
        cfg = {
            "conditions": [
                {"field": "published", "operator": "within_days", "days": 28},
            ],
            "logic": "AND",
        }
        assert item_passes_filter_config(item, cfg) is True

    def test_within_days_old_fails(self):
        now = _utc_now()
        item = {"published": now - timedelta(days=40)}
        cfg = {
            "conditions": [
                {"field": "published", "operator": "within_days", "days": 28},
            ],
            "logic": "AND",
        }
        assert item_passes_filter_config(item, cfg) is False

    def test_within_weeks_equivalent(self):
        now = _utc_now()
        item = {"published": now - timedelta(days=10)}
        cfg = {
            "conditions": [
                {"field": "published", "operator": "within_weeks", "weeks": 1},
            ],
            "logic": "AND",
        }
        assert item_passes_filter_config(item, cfg) is False

    def test_within_weeks_recent_passes(self):
        now = _utc_now()
        item = {"published": now - timedelta(days=5)}
        cfg = {
            "conditions": [
                {"field": "published", "operator": "within_weeks", "weeks": 1},
            ],
            "logic": "AND",
        }
        assert item_passes_filter_config(item, cfg) is True

    def test_missing_date_strict_fails(self):
        item = {"title": "x"}
        cfg = {
            "conditions": [
                {"field": "published", "operator": "within_days", "days": 7},
            ],
            "logic": "AND",
        }
        assert item_passes_filter_config(item, cfg) is False

    def test_missing_date_include_if_missing(self):
        item = {"title": "x"}
        cfg = {
            "conditions": [
                {
                    "field": "published",
                    "operator": "within_days",
                    "days": 7,
                    "include_if_missing": True,
                },
            ],
            "logic": "AND",
        }
        assert item_passes_filter_config(item, cfg) is True

    def test_within_days_without_days_key_fails(self):
        item = {"published": _utc_now()}
        cfg = {
            "conditions": [
                {"field": "published", "operator": "within_days"},
            ],
            "logic": "AND",
        }
        assert item_passes_filter_config(item, cfg) is False

    def test_combined_with_priority(self):
        now = _utc_now()
        item = {
            "published": now - timedelta(days=1),
            "raw": {"Priority": "high"},
        }
        cfg = {
            "conditions": [
                {"field": "published", "operator": "within_days", "days": 28},
                {"field": "Priority", "operator": "equals", "value": "high"},
            ],
            "logic": "AND",
        }
        assert item_passes_filter_config(item, cfg) is True


class TestMicrosoftDateParse:
    def test_parse_microsoft_date(self):
        ms = int(_utc_now().timestamp() * 1000)
        dt = parse_microsoft_date(f"/Date({ms}+0000)/")
        assert dt is not None
