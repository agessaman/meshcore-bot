"""Tests for modules/transmission_tracker.py."""

import time
from unittest.mock import Mock

import pytest

from modules.transmission_tracker import TransmissionRecord, TransmissionTracker


@pytest.fixture
def mock_bot(mock_logger):
    """Minimal bot mock for TransmissionTracker."""
    bot = Mock()
    bot.logger = mock_logger
    bot.meshcore = None  # No device connected
    bot.prefix_hex_chars = 2
    return bot


@pytest.fixture
def tracker(mock_bot):
    """TransmissionTracker instance with a mock bot."""
    return TransmissionTracker(mock_bot)


class TestTransmissionRecord:
    """Tests for TransmissionRecord dataclass."""

    def test_default_fields(self):
        rec = TransmissionRecord(
            timestamp=1234.0,
            content="hello",
            target="general",
            message_type="channel",
        )
        assert rec.repeat_count == 0
        assert rec.packet_hash is None
        assert rec.command_id is None
        assert rec.repeater_prefixes == set()
        assert rec.repeater_counts == {}

    def test_custom_fields(self):
        rec = TransmissionRecord(
            timestamp=5678.0,
            content="dm text",
            target="Alice",
            message_type="dm",
            packet_hash="abcd1234",
            command_id="cmd-001",
        )
        assert rec.packet_hash == "abcd1234"
        assert rec.command_id == "cmd-001"


class TestRecordTransmission:
    """Tests for TransmissionTracker.record_transmission()."""

    def test_returns_transmission_record(self, tracker):
        rec = tracker.record_transmission("hello", "general", "channel")
        assert isinstance(rec, TransmissionRecord)
        assert rec.content == "hello"
        assert rec.target == "general"
        assert rec.message_type == "channel"

    def test_stores_in_pending(self, tracker):
        rec = tracker.record_transmission("msg", "ch", "channel")
        key = int(rec.timestamp)
        assert key in tracker.pending_transmissions
        assert rec in tracker.pending_transmissions[key]

    def test_multiple_records_same_second(self, tracker):
        rec1 = tracker.record_transmission("a", "ch", "channel")
        rec2 = tracker.record_transmission("b", "ch", "channel")
        int(rec1.timestamp)
        # Both records should be in the same (or nearby) bucket
        assert rec1 in tracker.pending_transmissions.get(int(rec1.timestamp), [])
        assert rec2 in tracker.pending_transmissions.get(int(rec2.timestamp), [])

    def test_with_command_id(self, tracker):
        rec = tracker.record_transmission("msg", "ch", "channel", command_id="cmd-42")
        assert rec.command_id == "cmd-42"


class TestMatchPacketHash:
    """Tests for TransmissionTracker.match_packet_hash()."""

    def test_null_hash_returns_none(self, tracker):
        assert tracker.match_packet_hash("", time.time()) is None
        assert tracker.match_packet_hash("0000000000000000", time.time()) is None

    def test_matches_pending_transmission(self, tracker):
        rec = tracker.record_transmission("msg", "general", "channel")
        result = tracker.match_packet_hash("deadbeef", rec.timestamp + 1)
        assert result is not None
        assert result.packet_hash == "deadbeef"

    def test_already_confirmed_returned_immediately(self, tracker):
        rec = tracker.record_transmission("msg", "ch", "channel")
        # First match confirms it
        tracker.match_packet_hash("abc123", rec.timestamp)
        # Second call returns same confirmed record
        result2 = tracker.match_packet_hash("abc123", time.time())
        assert result2 is not None
        assert result2.packet_hash == "abc123"

    def test_no_match_outside_window(self, tracker):
        rec = tracker.record_transmission("msg", "ch", "channel")
        # RF timestamp far in the future
        result = tracker.match_packet_hash("deadbeef", rec.timestamp + 9999)
        assert result is None


class TestRecordRepeat:
    """Tests for TransmissionTracker.record_repeat()."""

    def test_null_hash_returns_false(self, tracker):
        assert tracker.record_repeat("") is False
        assert tracker.record_repeat("0000000000000000") is False

    def test_repeat_increments_count(self, tracker):
        rec = tracker.record_transmission("msg", "ch", "channel")
        # First confirm the hash
        tracker.match_packet_hash("hash01", rec.timestamp)
        # Now record a repeat
        result = tracker.record_repeat("hash01", repeater_prefix="7e")
        assert result is True
        assert rec.repeat_count == 1
        assert "7e" in rec.repeater_prefixes

    def test_repeat_without_prefix(self, tracker):
        rec = tracker.record_transmission("msg", "ch", "channel")
        tracker.match_packet_hash("hash02", rec.timestamp)
        result = tracker.record_repeat("hash02")
        assert result is True
        assert rec.repeat_count == 1
        assert rec.repeater_counts.get("_unknown") == 1

    def test_multiple_repeats_same_repeater(self, tracker):
        rec = tracker.record_transmission("msg", "ch", "channel")
        tracker.match_packet_hash("hash03", rec.timestamp)
        tracker.record_repeat("hash03", repeater_prefix="01")
        tracker.record_repeat("hash03", repeater_prefix="01")
        assert rec.repeat_count == 2
        assert rec.repeater_counts["01"] == 2

    def test_unmatched_hash_returns_false(self, tracker):
        result = tracker.record_repeat("nonexistent_hash")
        assert result is False


class TestGetRepeatInfo:
    """Tests for TransmissionTracker.get_repeat_info()."""

    def test_unknown_hash_returns_zeros(self, tracker):
        info = tracker.get_repeat_info(packet_hash="unknown")
        assert info["repeat_count"] == 0
        assert info["repeater_prefixes"] == []
        assert info["repeater_counts"] == {}

    def test_lookup_by_packet_hash(self, tracker):
        rec = tracker.record_transmission("msg", "ch", "channel")
        tracker.match_packet_hash("hashXX", rec.timestamp)
        tracker.record_repeat("hashXX", repeater_prefix="7e")
        info = tracker.get_repeat_info(packet_hash="hashXX")
        assert info["repeat_count"] == 1
        assert "7e" in info["repeater_prefixes"]

    def test_lookup_by_command_id(self, tracker):
        rec = tracker.record_transmission("msg", "ch", "channel", command_id="cmd-99")
        tracker.match_packet_hash("hashYY", rec.timestamp)
        tracker.record_repeat("hashYY", repeater_prefix="ab")
        info = tracker.get_repeat_info(command_id="cmd-99")
        assert info["repeat_count"] == 1
        assert "ab" in info["repeater_prefixes"]


class TestExtractRepeaterPrefixes:
    """Tests for TransmissionTracker.extract_repeater_prefixes_from_path()."""

    def test_extracts_last_hop_from_path_string(self, tracker):
        result = tracker.extract_repeater_prefixes_from_path("01,7e,86")
        assert result == ["86"]

    def test_extracts_from_path_nodes(self, tracker):
        result = tracker.extract_repeater_prefixes_from_path(None, path_nodes=["01", "7e", "86"])
        assert result == ["86"]

    def test_filters_own_prefix(self, tracker):
        tracker.bot_prefix = "86"
        result = tracker.extract_repeater_prefixes_from_path("01,7e,86")
        assert result == []

    def test_empty_path_returns_empty(self, tracker):
        result = tracker.extract_repeater_prefixes_from_path(None)
        assert result == []

    def test_path_with_route_type_annotation(self, tracker):
        result = tracker.extract_repeater_prefixes_from_path("01,7e,55 via ROUTE_TYPE_FLOOD")
        assert result == ["55"]

    def test_single_node_path(self, tracker):
        result = tracker.extract_repeater_prefixes_from_path("7e")
        assert result == ["7e"]


class TestCleanupOldRecords:
    """Tests for TransmissionTracker.cleanup_old_records()."""

    def test_removes_old_pending(self, tracker):
        # Inject a record with an old timestamp
        old_rec = TransmissionRecord(
            timestamp=time.time() - 600,  # 10 minutes ago (beyond cleanup_after=300)
            content="old msg",
            target="ch",
            message_type="channel",
        )
        old_key = int(old_rec.timestamp)
        tracker.pending_transmissions[old_key] = [old_rec]
        tracker.cleanup_old_records()
        assert old_key not in tracker.pending_transmissions

    def test_keeps_recent_pending(self, tracker):
        rec = tracker.record_transmission("recent", "ch", "channel")
        key = int(rec.timestamp)
        tracker.cleanup_old_records()
        assert key in tracker.pending_transmissions

    def test_removes_old_confirmed_without_repeats(self, tracker):
        old_rec = TransmissionRecord(
            timestamp=time.time() - 600,
            content="old",
            target="ch",
            message_type="channel",
            packet_hash="stale_hash",
        )
        tracker.confirmed_transmissions["stale_hash"] = old_rec
        tracker.cleanup_old_records()
        assert "stale_hash" not in tracker.confirmed_transmissions

    def test_keeps_old_confirmed_with_repeats(self, tracker):
        old_rec = TransmissionRecord(
            timestamp=time.time() - 600,
            content="old",
            target="ch",
            message_type="channel",
            packet_hash="repeat_hash",
            repeat_count=3,
        )
        tracker.confirmed_transmissions["repeat_hash"] = old_rec
        tracker.cleanup_old_records()
        assert "repeat_hash" in tracker.confirmed_transmissions
