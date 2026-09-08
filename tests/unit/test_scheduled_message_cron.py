#!/usr/bin/env python3
"""
Unit tests for the scheduled-message cron helpers.
"""

import re
from datetime import date, timezone

import pytest

from modules.scheduled_message_cron import (
    decode_schedule_key_from_ini,
    encode_schedule_key_for_ini,
    is_valid_legacy_hhmm,
    parse_flexible_cron,
    parse_schedule_key,
)

TZ = timezone.utc


@pytest.mark.unit
class TestEncodeDecodeScheduleKey:
    def test_encode_replaces_colon_with_bang(self):
        assert encode_schedule_key_for_ini("4th tue 14:00 jan-oct") == "4th tue 14!00 jan-oct"

    def test_decode_replaces_bang_with_colon(self):
        assert decode_schedule_key_from_ini("4th tue 14!00 jan-oct") == "4th tue 14:00 jan-oct"

    def test_encode_does_nothing_without_a_colon(self):
        assert encode_schedule_key_for_ini("0 8 * * *") == "0 8 * * *"

    def test_decode_does_nothing_without_a_bang(self):
        assert decode_schedule_key_from_ini("0 8 * * *") == "0 8 * * *"

    def test_encode_handles_none_and_empty(self):
        assert encode_schedule_key_for_ini("") == ""
        assert encode_schedule_key_for_ini(None) == ""

    def test_decode_handles_none_and_empty(self):
        assert decode_schedule_key_from_ini("") == ""
        assert decode_schedule_key_from_ini(None) == ""

    @pytest.mark.parametrize("key", [
        "4th tue 14:00 jan-oct",
        "1st mon 09:05",
        "last fri 23:59 dec",
        "0 8 * * *",
        "@daily",
    ])
    def test_round_trips(self, key):
        assert decode_schedule_key_from_ini(encode_schedule_key_for_ini(key)) == key


@pytest.mark.unit
class TestIsValidLegacyHhmm:
    @pytest.mark.parametrize("value", ["0000", "0800", "1234", "2359"])
    def test_accepts_valid_hhmm(self, value):
        assert is_valid_legacy_hhmm(value) is True

    @pytest.mark.parametrize("value", [
        "", "800", "080", "24:00", "2400", "9999", "abcd", "08:00", "  0800", "08 0",
    ])
    def test_rejects_invalid_hhmm(self, value):
        assert is_valid_legacy_hhmm(value) is False


@pytest.mark.unit
class TestParseFlexibleCron:
    def test_parses_hhmm_with_colon(self):
        assert parse_flexible_cron("14:30") == {"hour": "14", "minute": "30"}

    def test_parses_hhmm_without_colon(self):
        assert parse_flexible_cron("1430") == {"hour": "14", "minute": "30"}

    @pytest.mark.parametrize("case", [("9h 45m", {"hour": "9", "minute": "45"}),
                                      ("9h", {"hour": "9"}),
                                      ("30m", {"minute": "30"}),
                                      ("13,14h", {"hour": "13,14"}),
                                      ("2-6h", {"hour": "2-6"}),
                                      ("15,45m", {"minute": "15,45"}),
                                      ("20-40m", {"minute": "20-40"}),
                                      ("10M 12H", {"hour": "12", "minute": "10"})])
    def test_parses_hour_and_minute_suffix_forms(self, case):
        assert parse_flexible_cron(case[0]) == case[1]

    @pytest.mark.parametrize("mon", ["jan", "feb", "mar", "apr", "may", "jun",
                                     "jul", "aug", "sep", "oct", "nov", "dec",
                                     "1", "2", "3", "4", "5", "6",
                                     "7", "8", "9", "10", "11", "12"])
    def test_parses_bare_month(self, mon):
        assert parse_flexible_cron(mon) == {"month": mon}

    def test_parses_month_range(self):
        assert parse_flexible_cron("mar-jul") == {"month": "mar-jul"}

    def test_parses_month_multiple(self):
        assert parse_flexible_cron("jan,apr,jul,oct") == {"month": "jan,apr,jul,oct"}

    @pytest.mark.parametrize("mon", ["march", "april", "june", "july", "13"])
    def test_invalid_month(self, mon):
        result = parse_flexible_cron(mon)
        assert re.match(fr'^Unparsable component: "{mon}"', result["error"])

    @pytest.mark.parametrize("day", ["15d", "10-15d", "5,15,25d"])
    def test_parses_day_of_month(self, day):
        assert parse_flexible_cron(day) == {"day": day[:-1]}

    def test_parses_week(self):
        assert parse_flexible_cron("3w") == {"week": "3"}

    @pytest.mark.parametrize("dow", ["mon", "tue", "wed", "thu", "fri", "sat", "sun",
                                     "mon,wed,fri", "mon,wed,fri", ])
    def test_parses_day_of_week(self, dow):
        assert parse_flexible_cron(dow) == {"day_of_week": dow}

    @pytest.mark.parametrize("dow", ["tues", "thur"])
    def test_invalid_day_of_week(self, dow):
        result = parse_flexible_cron(dow)
        assert re.match(fr'^Unparsable component: "{dow}"', result["error"])

    @pytest.mark.parametrize("dow", ["1st mon", "2nd tue", "3rd wed", "4th thu",
                                     "5th fri", "last sat"])
    def test_parses_day_of_week_with_ordinal_prefix(self, dow):
        assert parse_flexible_cron(dow) == {"day": dow}

    @pytest.mark.parametrize("dow", ["1st  mon", "2nd     tue"])
    def test_parses_day_of_week_with_ordinal_prefix_and_extra_spaces(self, dow):
        assert parse_flexible_cron(dow) == {"day": " ".join(dow.split())}

    def test_parses_last_alone_as_last_day_of_month(self):
        assert parse_flexible_cron("last") == {"day": "last"}

    def test_combines_multiple_fields(self):
        assert parse_flexible_cron("4th tue 14:00 jan-oct") == {
            "hour": "14",
            "minute": "00",
            "month": "jan-oct",
            "day": "4th tue",
        }

    def test_combines_suffix_time_with_day(self):
        assert parse_flexible_cron("5,15,25d 15m") == {
            "minute": "15",
            "day": "5,15,25"
        }

    def test_is_case_insensitive(self):
        assert parse_flexible_cron("4TH TUE 14:00 JAN-OCT") == {
            "hour": "14",
            "minute": "00",
            "month": "JAN-OCT",
            "day": "4TH TUE",
        }

    def test_start_date_detected(self):
        assert parse_flexible_cron("start:2027-01-01") == {
            "start_date": "2027-01-01"
        }

    def test_start_date_detected_for_current_year(self):
        current_year = date.today().year
        assert parse_flexible_cron("start:0000-01-01") == {
            "start_date": f"{current_year}-01-01"
        }

    def test_end_date_detected(self):
        assert parse_flexible_cron("end:2027-01-01") == {
            "end_date": "2027-01-01"
        }

    def test_end_date_detected_for_current_year(self):
        current_year = date.today().year
        assert parse_flexible_cron("end:0000-01-01") == {
            "end_date": f"{current_year}-01-01"
        }

    def test_empty_string_returns_empty_dict(self):
        assert parse_flexible_cron("") == {}

    def test_unrecognized_text_returns_error_attribute(self):
        result = parse_flexible_cron("nonsense")
        assert re.match(r'^Unparsable component: "nonsense"', result["error"])



@pytest.mark.unit
class TestParseScheduleKey:
    def test_empty_key_returns_no_trigger(self):
        result = parse_schedule_key("", TZ)
        assert result.trigger is None
        assert result.display_label == ""
        assert result.is_deprecated_hhmm is False
        assert result.error == "No schedule_key specified"

    def test_legacy_hhmm_is_flagged_deprecated(self):
        result = parse_schedule_key("0800", TZ)
        assert result.trigger is not None
        assert result.display_label == "08:00"
        assert result.is_deprecated_hhmm is True
        assert result.error is None

    @pytest.mark.parametrize("preset", [
        "@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly",
    ])
    def test_preset_aliases_resolve(self, preset):
        result = parse_schedule_key(preset, TZ)
        assert result.trigger is not None
        assert result.is_deprecated_hhmm is False
        assert result.error is None

    def test_standard_five_field_crontab(self):
        result = parse_schedule_key("0 8 * * *", TZ)
        assert result.trigger is not None
        assert result.is_deprecated_hhmm is False
        assert result.error is None

    def test_flexible_cron_with_colon_time(self):
        result = parse_schedule_key("4th tue 14:00 jan-oct", TZ)
        assert result.trigger is not None
        assert result.is_deprecated_hhmm is False
        assert result.error is None

    def test_flexible_cron_encoded_key_is_not_directly_parsed(self):
        # The caller is responsible for decoding "!" back to ":" before calling
        # this; an encoded key on its own does not parse as a valid time.
        result = parse_schedule_key("4th tue 14!00 jan-oct", TZ)
        assert result.trigger is None
        assert re.match(r'^Unparsable component: "\s*14!00\s*"', result.error)

    def test_unrecognized_text_returns_no_trigger(self):
        result = parse_schedule_key("nonsense", TZ)
        assert result.trigger is None
        assert result.display_label == "nonsense"
        assert re.match(r'^Unparsable component: "nonsense"', result.error)

    def test_whitespace_only_returns_no_trigger(self):
        result = parse_schedule_key("   ", TZ)
        assert result.trigger is None
        assert result.error == "No schedule_key specified"

    @pytest.mark.parametrize("spec", ["34h", "75m", "12,30h", "2-25h", "15,61m", "0-69m", "0-200m"])
    def test_invalid_hour_and_minute_suffix_forms(self, spec):
        with pytest.raises(ValueError, match=r"^Error validating expression"):
            parse_schedule_key(spec, TZ)
