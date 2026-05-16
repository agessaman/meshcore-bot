"""Tests for scheduled_message_cron value parsing."""

import pytest

from modules.scheduled_message_cron import parse_scheduled_message_value


class TestParseScheduledMessageValue:
    def test_legacy_channel_body(self):
        ch, msg, scope = parse_scheduled_message_value("Public:Hello!")
        assert ch == "Public"
        assert msg == "Hello!"
        assert scope is None

    def test_scoped_channel_scope_body(self):
        ch, msg, scope = parse_scheduled_message_value("Public:#sea:Hello!")
        assert ch == "Public"
        assert scope == "#sea"
        assert msg == "Hello!"

    def test_scoped_body_may_contain_colons(self):
        ch, msg, scope = parse_scheduled_message_value("Public:#sea:Hello: with colons")
        assert ch == "Public"
        assert scope == "#sea"
        assert msg == "Hello: with colons"

    def test_three_parts_middle_not_hash_is_legacy(self):
        ch, msg, scope = parse_scheduled_message_value("Public:Hello: world")
        assert ch == "Public"
        assert msg == "Hello: world"
        assert scope is None

    def test_strips_whitespace(self):
        ch, msg, scope = parse_scheduled_message_value("  gen : #w : hi  ")
        assert ch == "gen"
        assert scope == "#w"
        assert msg == "hi"

    def test_no_colon_raises(self):
        with pytest.raises(ValueError):
            parse_scheduled_message_value("nocolon")

    def test_only_channel_hash_scope_two_segments_legacy(self):
        """Two segments after split(':',2) — middle does not get # form; legacy parse."""
        ch, msg, scope = parse_scheduled_message_value("Public:#sea")
        assert ch == "Public"
        assert msg == "#sea"
        assert scope is None
