#!/usr/bin/env python3
"""
Unit tests for the scheduled-message admin helpers behind the web UI (#174).

The contract that matters: the UI must never accept a schedule the bot would then
reject, so validation runs through the same parsers the scheduler uses.
"""

import datetime

import pytest

from modules.scheduled_message_admin import (
    compose_value,
    describe_schedule,
    read_entries,
    validate_entry,
)

TZ = datetime.timezone.utc


@pytest.mark.unit
class TestDescribeSchedule:
    @pytest.mark.parametrize("cron", ["0 8 * * *", "0 6,12,18 * * *", "*/30 * * * *", "@daily"])
    def test_accepts_valid_schedules(self, cron):
        assert describe_schedule(cron, TZ)["valid"] is True

    @pytest.mark.parametrize("bad", ["", "   ", "nonsense", "0 8 * *", "99 99 * * *"])
    def test_rejects_invalid_schedules(self, bad):
        result = describe_schedule(bad, TZ)
        assert result["valid"] is False
        assert result["error"]

    def test_returns_requested_number_of_next_runs(self):
        assert len(describe_schedule("0 * * * *", TZ, count=3)["next_runs"]) == 3

    def test_next_runs_are_in_ascending_order(self):
        runs = describe_schedule("0 * * * *", TZ, count=4)["next_runs"]
        assert runs == sorted(runs)

    def test_reports_the_tightest_interval(self):
        assert describe_schedule("*/15 * * * *", TZ)["interval_seconds"] == 900
        # Uneven crons are measured by their smallest gap, not their average.
        assert describe_schedule("0,1 * * * *", TZ)["interval_seconds"] == 60

    def test_flags_the_deprecated_hhmm_form(self):
        result = describe_schedule("0800", TZ)
        assert result["valid"] is True
        assert result["deprecated"] is True
        assert "deprecated" in result["warning"].lower()


@pytest.mark.unit
class TestCommandPlaceholderFloor:
    """The 15-minute floor is refused in the UI, not silently dropped on reload."""

    def test_frequent_schedule_with_cmd_placeholder_is_rejected(self):
        result = describe_schedule("*/5 * * * *", TZ, message="wx: {cmd:wx Seattle}")
        assert result["valid"] is False
        assert "15 minutes" in result["error"]

    def test_same_schedule_without_a_placeholder_is_fine(self):
        assert describe_schedule("*/5 * * * *", TZ, message="plain text")["valid"] is True

    def test_schedule_at_the_floor_with_a_placeholder_is_accepted(self):
        assert describe_schedule("*/15 * * * *", TZ, message="{cmd:wx}")["valid"] is True


@pytest.mark.unit
class TestComposeAndValidate:
    def test_composes_unscoped_value(self):
        assert compose_value("Public", "hello") == "Public:hello"

    def test_composes_scoped_value(self):
        assert compose_value("Public", "hello", "#sea") == "Public:#sea:hello"

    def test_adds_missing_hash_to_scope(self):
        assert compose_value("Public", "hello", "sea") == "Public:#sea:hello"

    def test_round_trips_through_the_bots_own_parser(self):
        from modules.scheduled_message_cron import parse_scheduled_message_value

        value = compose_value("Public", "time is 12:30 sharp", "#sea")
        channel, message, scope = parse_scheduled_message_value(value)
        assert (channel, message, scope) == ("Public", "time is 12:30 sharp", "#sea")

    @pytest.mark.parametrize("channel,message,scope,expect", [
        ("", "hi", None, "Channel is required"),
        ("Public", "", None, "Message is required"),
        ("Pub:lic", "hi", None, "Channel cannot contain"),
        ("Public", "hi", "#a:b", "Scope cannot contain"),
        ("Public", "line\nbreak", None, "line breaks"),
    ])
    def test_rejects_unusable_entries(self, channel, message, scope, expect):
        error = validate_entry(channel, message, scope)
        assert error and expect in error

    def test_accepts_a_good_entry(self):
        assert validate_entry("Public", "hello", "#sea") is None


@pytest.mark.unit
class TestReadEntries:
    @staticmethod
    def _write(tmp_path, body):
        path = tmp_path / "config.ini"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_reads_and_describes_entries(self, tmp_path):
        path = self._write(tmp_path, "[Scheduled_Messages]\n0 8 * * * = Public:Good morning\n")
        entries = read_entries(path, TZ)
        assert len(entries) == 1
        assert entries[0]["channel"] == "Public"
        assert entries[0]["message"] == "Good morning"
        assert entries[0]["scope"] is None
        assert entries[0]["valid"] is True
        assert entries[0]["next_runs"]

    def test_reads_a_scoped_entry(self, tmp_path):
        path = self._write(tmp_path, "[Scheduled_Messages]\n0 8 * * * = Public:#sea:Hi\n")
        entry = read_entries(path, TZ)[0]
        assert entry["scope"] == "#sea"
        assert entry["message"] == "Hi"

    def test_surfaces_a_malformed_row_instead_of_hiding_it(self, tmp_path):
        """A row the bot skips is exactly what the operator needs to see."""
        path = self._write(tmp_path, "[Scheduled_Messages]\n0 8 * * * = no-colon-here\n")
        entry = read_entries(path, TZ)[0]
        assert "error" in entry
        assert entry["valid"] is not True

    def test_surfaces_an_unschedulable_row(self, tmp_path):
        path = self._write(tmp_path, "[Scheduled_Messages]\nnonsense = Public:Hi\n")
        entry = read_entries(path, TZ)[0]
        assert entry["valid"] is False
        assert entry["error"]

    def test_missing_section_is_empty_not_an_error(self, tmp_path):
        assert read_entries(self._write(tmp_path, "[Bot]\nbot_name = x\n"), TZ) == []

    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert read_entries(str(tmp_path / "nope.ini"), TZ) == []
