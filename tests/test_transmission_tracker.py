"""Tests for modules/transmission_tracker.py."""

import json
import sqlite3
import time
from contextlib import closing
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bot_with_device(mock_logger, pubkey, prefix_hex_chars=2):
    """Build a minimal bot mock whose meshcore.device.public_key == pubkey."""
    device = Mock()
    device.public_key = pubkey
    meshcore = Mock()
    meshcore.device = device
    bot = Mock()
    bot.logger = mock_logger
    bot.meshcore = meshcore
    bot.prefix_hex_chars = prefix_hex_chars
    return bot


def _make_db_with_packet_stream(db_path: str) -> None:
    """Create a minimal packet_stream table in a SQLite file."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS packet_stream (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT,
                timestamp REAL,
                data TEXT
            )
        """)
        conn.commit()


# ---------------------------------------------------------------------------
# Tests for _update_bot_prefix (lines 57-67)
# ---------------------------------------------------------------------------

class TestUpdateBotPrefix:
    """Cover lines 57-67: _update_bot_prefix with str and bytes public_key."""

    def test_str_pubkey_sets_bot_prefix(self, mock_logger):
        """When public_key is a str, bot_prefix is set to its first prefix_hex_chars."""
        bot = _make_bot_with_device(mock_logger, pubkey="abcdef1234")
        tracker = TransmissionTracker(bot)
        assert tracker.bot_prefix == "ab"

    def test_str_pubkey_prefix_hex_chars_4(self, mock_logger):
        """prefix_hex_chars=4 slices the first 4 characters."""
        bot = _make_bot_with_device(mock_logger, pubkey="deadbeef99", prefix_hex_chars=4)
        tracker = TransmissionTracker(bot)
        assert tracker.bot_prefix == "dead"

    def test_bytes_pubkey_sets_bot_prefix(self, mock_logger):
        """When public_key is bytes, bot_prefix is the hex of the first byte."""
        bot = _make_bot_with_device(mock_logger, pubkey=b"\xab\xcd\xef")
        tracker = TransmissionTracker(bot)
        assert tracker.bot_prefix == "ab"

    def test_bytes_pubkey_zero_byte(self, mock_logger):
        """Bytes public key starting with 0x00 produces '00'."""
        bot = _make_bot_with_device(mock_logger, pubkey=b"\x00\xff")
        tracker = TransmissionTracker(bot)
        assert tracker.bot_prefix == "00"

    def test_str_pubkey_too_short_stays_none(self, mock_logger):
        """A one-character str pubkey does not satisfy len >= 2; prefix stays None."""
        bot = _make_bot_with_device(mock_logger, pubkey="a")
        tracker = TransmissionTracker(bot)
        assert tracker.bot_prefix is None

    def test_no_meshcore_leaves_prefix_none(self, mock_logger):
        """When bot.meshcore is None the prefix is never set."""
        bot = Mock()
        bot.logger = mock_logger
        bot.meshcore = None
        bot.prefix_hex_chars = 2
        tracker = TransmissionTracker(bot)
        assert tracker.bot_prefix is None

    def test_exception_during_prefix_update_leaves_prefix_none(self, mock_logger):
        """If accessing device.public_key raises an exception bot_prefix remains None."""
        device = Mock()
        # Make accessing public_key raise an exception.
        type(device).public_key = property(lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
        meshcore = Mock()
        meshcore.device = device
        bot = Mock()
        bot.logger = mock_logger
        bot.meshcore = meshcore
        bot.prefix_hex_chars = 2
        tracker = TransmissionTracker(bot)
        assert tracker.bot_prefix is None


# ---------------------------------------------------------------------------
# Tests for _update_command_in_database (lines 190, 198-246)
# ---------------------------------------------------------------------------

def _build_tracker_with_db(mock_logger, tmp_path):
    """Build a tracker whose bot has a real SQLite DB at tmp_path/test.db."""
    db_path = str(tmp_path / "test.db")
    _make_db_with_packet_stream(db_path)

    config = Mock()
    config.has_section = Mock(return_value=False)
    config.has_option = Mock(return_value=False)
    config.get = Mock(return_value="")

    db_manager = Mock()
    db_manager.db_path = db_path

    bot = Mock()
    bot.logger = mock_logger
    bot.meshcore = None
    bot.prefix_hex_chars = 2
    bot.config = config
    bot.bot_root = str(tmp_path)
    bot.db_manager = db_manager
    bot.web_viewer_integration = Mock()  # truthy so the DB path is reached

    return TransmissionTracker(bot), db_path


class TestUpdateCommandInDatabase:
    """Cover lines 190 and 198-246: _update_command_in_database."""

    def test_early_return_when_command_id_is_none(self, mock_logger, tmp_path):
        """Line 190: method returns immediately when record.command_id is None."""
        tracker, db_path = _build_tracker_with_db(mock_logger, tmp_path)
        rec = TransmissionRecord(
            timestamp=time.time(),
            content="msg",
            target="ch",
            message_type="channel",
            command_id=None,
        )
        # No exception and the DB is untouched (table stays empty).
        tracker._update_command_in_database(rec)
        with closing(sqlite3.connect(db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM packet_stream").fetchone()[0]
        assert count == 0

    def test_updates_matching_row_in_database(self, mock_logger, tmp_path):
        """Lines 198-246: a matching command row is found and updated."""
        tracker, db_path = _build_tracker_with_db(mock_logger, tmp_path)

        command_id = "cmd-update-test"
        initial_data = {
            "command_id": command_id,
            "repeat_count": 0,
            "repeater_prefixes": [],
            "repeater_counts": {},
        }

        # Insert a row into packet_stream.
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO packet_stream (type, timestamp, data) VALUES (?, ?, ?)",
                ("command", time.time(), json.dumps(initial_data)),
            )
            conn.commit()

        # Build a record whose command_id matches.
        rec = TransmissionRecord(
            timestamp=time.time(),
            content="hello",
            target="general",
            message_type="channel",
            command_id=command_id,
            repeat_count=3,
        )
        rec.repeater_prefixes = {"7e", "ab"}
        rec.repeater_counts = {"7e": 2, "ab": 1}

        tracker._update_command_in_database(rec)

        # Verify the row was updated.
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                "SELECT data FROM packet_stream WHERE type = 'command'"
            ).fetchone()

        assert row is not None
        updated = json.loads(row[0])
        assert updated["repeat_count"] == 3
        assert sorted(updated["repeater_prefixes"]) == ["7e", "ab"] or set(updated["repeater_prefixes"]) == {"7e", "ab"}
        assert updated["repeater_counts"]["7e"] == 2
        assert updated["repeater_counts"]["ab"] == 1

    def test_no_matching_row_leaves_db_unchanged(self, mock_logger, tmp_path):
        """If no row matches command_id, the DB is not modified."""
        tracker, db_path = _build_tracker_with_db(mock_logger, tmp_path)

        other_data = {"command_id": "other-cmd", "repeat_count": 0}
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO packet_stream (type, timestamp, data) VALUES (?, ?, ?)",
                ("command", time.time(), json.dumps(other_data)),
            )
            conn.commit()

        rec = TransmissionRecord(
            timestamp=time.time(),
            content="msg",
            target="ch",
            message_type="channel",
            command_id="no-match-cmd",
            repeat_count=1,
        )
        tracker._update_command_in_database(rec)

        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                "SELECT data FROM packet_stream WHERE type = 'command'"
            ).fetchone()
        assert json.loads(row[0])["command_id"] == "other-cmd"

    def test_malformed_json_row_is_skipped(self, mock_logger, tmp_path):
        """A row with invalid JSON is silently skipped (json.JSONDecodeError path)."""
        tracker, db_path = _build_tracker_with_db(mock_logger, tmp_path)

        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "INSERT INTO packet_stream (type, timestamp, data) VALUES (?, ?, ?)",
                ("command", time.time(), "NOT VALID JSON"),
            )
            conn.commit()

        rec = TransmissionRecord(
            timestamp=time.time(),
            content="msg",
            target="ch",
            message_type="channel",
            command_id="cmd-x",
            repeat_count=1,
        )
        # Should not raise even though JSON is malformed.
        tracker._update_command_in_database(rec)


# ---------------------------------------------------------------------------
# Tests for line 314: path with parenthesis hop-count annotation
# ---------------------------------------------------------------------------

class TestExtractRepeaterPrefixesParenPath:
    """Cover line 314: path containing '(' (hop-count annotation) is stripped."""

    def test_path_with_paren_stripped_before_split(self, mock_logger):
        """'01,7e,86(3)' should extract '86' after stripping the parenthesised part."""
        bot = Mock()
        bot.logger = mock_logger
        bot.meshcore = None
        bot.prefix_hex_chars = 2
        tracker = TransmissionTracker(bot)
        tracker.bot_prefix = None

        result = tracker.extract_repeater_prefixes_from_path("01,7e,86(3)")
        assert result == ["86"]

    def test_path_with_paren_and_via(self, mock_logger):
        """Combined annotation: ' via ROUTE_TYPE_*' and '(' in the path part."""
        bot = Mock()
        bot.logger = mock_logger
        bot.meshcore = None
        bot.prefix_hex_chars = 2
        tracker = TransmissionTracker(bot)
        tracker.bot_prefix = None

        result = tracker.extract_repeater_prefixes_from_path("01,7e,ab(2) via ROUTE_TYPE_FLOOD")
        assert result == ["ab"]


# ---------------------------------------------------------------------------
# Automatic cleanup via _maybe_cleanup
# ---------------------------------------------------------------------------

class TestMaybeCleanup:
    """Tests for automatic periodic cleanup in TransmissionTracker."""

    def test_maybe_cleanup_runs_after_interval(self, tracker):
        """_maybe_cleanup runs cleanup_old_records when interval has elapsed."""
        # Record an old transmission manually
        old_record = TransmissionRecord(
            timestamp=time.time() - 600,  # 10 minutes ago (past cleanup_after=300s)
            content="old", target="chan", message_type="channel",
        )
        tracker.pending_transmissions[int(old_record.timestamp)] = [old_record]
        # Force the interval to have elapsed
        tracker._last_cleanup_time = 0.0
        tracker._maybe_cleanup()
        # Old record should be cleaned up
        assert int(old_record.timestamp) not in tracker.pending_transmissions

    def test_maybe_cleanup_skips_within_interval(self, tracker):
        """_maybe_cleanup does NOT run cleanup if interval hasn't elapsed."""
        old_record = TransmissionRecord(
            timestamp=time.time() - 600,
            content="old", target="chan", message_type="channel",
        )
        tracker.pending_transmissions[int(old_record.timestamp)] = [old_record]
        # Set last cleanup to now — interval hasn't elapsed
        tracker._last_cleanup_time = time.time()
        tracker._maybe_cleanup()
        # Old record should still be present (cleanup didn't run)
        assert int(old_record.timestamp) in tracker.pending_transmissions

    def test_record_transmission_triggers_cleanup(self, tracker):
        """record_transmission calls _maybe_cleanup, cleaning stale records."""
        old_record = TransmissionRecord(
            timestamp=time.time() - 600,
            content="old", target="chan", message_type="channel",
        )
        old_key = int(old_record.timestamp)
        tracker.pending_transmissions[old_key] = [old_record]
        tracker._last_cleanup_time = 0.0  # Force cleanup to run
        # Recording a new transmission should trigger cleanup
        tracker.record_transmission("new msg", "general", "channel")
        assert old_key not in tracker.pending_transmissions
