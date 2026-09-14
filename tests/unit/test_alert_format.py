#!/usr/bin/env python3
"""Unit tests for the shared NWS alert formatter (modules.alert_format)."""

import pytest

from modules import alert_format as af
from modules.i18n import Translator

pytestmark = pytest.mark.unit


@pytest.fixture
def en():
    return Translator("en")


@pytest.fixture
def ru():
    return Translator("ru")


def alert(**overrides):
    base = {
        "event": "Flood Warning",
        "event_type": "Warning",
        "severity": "Severe",
        "expires": "2026-06-28T18:00:00-07:00",
        "office": "NWS Seattle WA",
        "area_desc": "King County; Snohomish County",
    }
    base.update(overrides)
    return base


class TestTranslateOr:
    def test_present_key_translates(self, ru):
        assert af.translate_or(ru, "common.unknown", "Unknown") == "Неизвестно"

    def test_missing_key_uses_default(self, en):
        assert af.translate_or(en, "no.such.key.at.all", "fallback") == "fallback"

    def test_missing_key_formats_default(self, en):
        assert af.translate_or(en, "no.such.key", "{n} left", n=3) == "3 left"

    def test_no_translator_uses_default(self):
        assert af.translate_or(None, "common.unknown", "Unknown") == "Unknown"


class TestEventTypeAbbrev:
    @pytest.mark.parametrize(
        ("event_type", "expected"),
        [("Warning", "Warn"), ("Watch", "Watch"), ("Advisory", "Adv"), ("Statement", "Stmt")],
    )
    def test_known_types(self, en, event_type, expected):
        assert af.event_type_abbrev(event_type, en) == expected

    def test_unknown_type_never_leaks_a_key(self, en):
        # _parse_alert_entry emits "Unknown" for any title we cannot classify.
        assert af.event_type_abbrev("Unknown", en) == "Unknown"

    def test_unknown_type_never_leaks_a_key_localized(self, ru):
        assert af.event_type_abbrev("Unknown", ru) == "Unknown"

    def test_empty_type(self, en):
        assert af.event_type_abbrev("", en) == ""

    def test_localized_abbreviations_stay_abbreviations(self, ru):
        # Full words would eat a quarter of the 130-byte budget at 2 bytes/char.
        for event_type in ("Warning", "Watch", "Advisory", "Statement"):
            assert len(af.event_type_abbrev(event_type, ru).encode("utf-8")) <= 16


class TestCompactTime:
    def test_iso_english(self, en):
        assert af.compact_time("2026-06-28T18:00:00-07:00", en) == "Jun 28 6PM"

    def test_iso_midnight_and_noon(self, en):
        assert af.compact_time("2026-06-28T00:30:00-07:00", en) == "Jun 28 12AM"
        assert af.compact_time("2026-06-28T12:00:00-07:00", en) == "Jun 28 12PM"

    def test_iso_russian_separates_hour_from_meridiem(self, ru):
        # "6дня" is not Russian; the locale owns the separator.
        assert af.compact_time("2026-06-28T18:00:00-07:00", ru) == "июн 28 6 дня"

    def test_full_month_name_is_abbreviated(self, en):
        assert af.compact_time("June 28 at 6:00AM", en) == "Jun 28 6AM"

    def test_full_month_name_is_not_corrupted_mid_word(self, ru):
        # Replacing the "Jun" inside "June" used to leave a stray Latin "e".
        assert af.compact_time("June 28 at 6:00AM", ru) == "июн 28 6 утра"

    @pytest.mark.parametrize("month", ["March", "April", "May", "July", "August"])
    def test_no_latin_residue_in_any_month(self, ru, month):
        out = af.compact_time(f"{month} 3 at 1:00PM", ru)
        assert not any(ch.isascii() and ch.isalpha() for ch in out), out

    def test_minutes_are_kept(self, en):
        assert af.compact_time("December 16 at 3:12PM", en) == "Dec 16 3:12PM"

    def test_empty_passes_through(self, en):
        assert af.compact_time("", en) == ""

    def test_unparseable_iso_falls_back_to_text_path(self, en):
        assert af.compact_time("2026-13-45T99:00:00", en) == "2026-13-45T99:00:00"

    def test_no_translator_is_english(self):
        assert af.compact_time("2026-06-28T18:00:00-07:00") == "Jun 28 6PM"


class TestExtractClock:
    def test_reads_clock_off_english_source(self, en):
        assert af.extract_clock("December 17 at 6:00AM PST", en) == "6AM"

    def test_keeps_real_minutes(self, en):
        assert af.extract_clock("December 16 at 3:12PM PST", en) == "3:12PM"

    def test_localized(self, ru):
        assert af.extract_clock("December 17 at 6:00AM PST", ru) == "6 утра"

    def test_no_clock_returns_none(self, en):
        assert af.extract_clock("sometime tomorrow", en) is None


class TestFormatAlertCompact:
    def test_english_iso_expiry_is_time_only(self, en):
        assert af.format_alert_compact(alert(), en) == "🟠Flood Warning King til 6PM by NWS SEA"

    def test_russian_iso_expiry_is_time_only(self, ru):
        # The English-month check plus an (AM|PM) regex used to send every
        # non-English locale down a mid-string truncation.
        assert af.format_alert_compact(alert(), ru) == "🟠Flood Warning King до 6 дня от NWS SEA"

    def test_free_text_expiry_is_time_only(self, en):
        out = af.format_alert_compact(alert(expires="December 17 at 6:00AM PST"), en)
        assert out == "🟠Flood Warning King til 6AM by NWS SEA"

    def test_free_text_expiry_is_not_truncated_mid_word(self, ru):
        out = af.format_alert_compact(alert(expires="December 17 at 6:00AM PST"), ru)
        assert out == "🟠Flood Warning King до 6 утра от NWS SEA"

    def test_unknown_event_type_never_leaks_a_key(self, en):
        out = af.format_alert_compact(
            alert(event="Hazardous", event_type="Unknown", severity="Minor",
                  expires="", office="", area_desc=""),
            en,
        )
        assert out == "⚪Hazardous Unknown"

    def test_summary_form_omits_details(self, en):
        out = af.format_alert_compact(alert(), en, include_details=False)
        assert out == "🟠Flood Warning Warn"

    def test_location_can_be_suppressed(self, en):
        out = af.format_alert_compact(alert(), en, include_location=False)
        assert out == "🟠Flood Warning til 6PM by NWS SEA"

    def test_redundant_event_type_is_dropped(self, en):
        # "Flood Warning" already says "Warning".
        assert "Warn " not in af.format_alert_compact(alert(), en)

    def test_event_type_is_appended_when_not_redundant(self, en):
        out = af.format_alert_compact(
            alert(event="Dense Fog", event_type="Advisory", expires="", office="", area_desc=""), en
        )
        assert out == "🟠Dense Fog Adv"

    def test_severity_emoji(self, en):
        for severity, emoji in (("Extreme", "🔴"), ("Severe", "🟠"),
                                ("Moderate", "🟡"), ("Minor", "⚪"), ("Nonsense", "⚪")):
            out = af.format_alert_compact(
                alert(severity=severity, expires="", office="", area_desc=""), en
            )
            assert out.startswith(emoji)

    def test_fits_the_mesh_budget_in_both_locales(self, en, ru):
        for translator in (en, ru):
            out = af.format_alert_compact(alert(), translator)
            assert len(out.encode("utf-8")) <= 130, out


class TestShortenEvent:
    def test_short_name_untouched(self):
        assert af.shorten_event("Dense Fog") == "Dense Fog"

    def test_long_name_keeps_two_words(self):
        assert af.shorten_event("Excessive Heat Warning Extended") == "Excessive Heat"

    def test_single_long_word_is_cut(self):
        assert af.shorten_event("Thunderstormageddon") == "Thunderstormage"

    def test_summary_mode_keeps_one_word(self):
        assert af.shorten_event("Excessive Heat Warning", limit=12, max_words=1) == "Excessive"

    def test_none_limit_never_trims(self):
        assert af.shorten_event("Excessive Heat Warning Extended", limit=None) == \
            "Excessive Heat Warning Extended"


class TestLocation:
    def test_strips_state(self):
        assert af.first_location("Seattle, WA") == "Seattle"

    def test_strips_county(self):
        assert af.first_location("King County; Snohomish County") == "King"

    def test_keeps_multiword_place(self):
        assert af.first_location("Puget Sound") == "Puget Sound"

    def test_truncates_long_name(self):
        assert len(af.first_location("A" * 40)) == 20

    def test_empty(self):
        assert af.first_location("") == ""


class TestOffice:
    def test_known_city(self, en):
        assert af.format_office("NWS Seattle WA", en) == "by NWS SEA"

    def test_localized_label(self, ru):
        assert af.format_office("NWS Seattle WA", ru) == "от NWS SEA"

    def test_single_token(self, en):
        assert af.format_office("NWS", en) == "by NWS"

    def test_empty(self, en):
        assert af.format_office("", en) == ""


class TestCityAbbrev:
    @pytest.mark.parametrize(("city", "expected"),
                             [("Seattle", "SEA"), ("Seattle WA", "SEA"), ("Portland", "PDX")])
    def test_known(self, city, expected):
        assert af.abbreviate_city_name(city) == expected

    def test_initials_for_unknown_multiword(self):
        assert af.abbreviate_city_name("Little Rock") == "LR"

    def test_prefix_for_unknown_single_word(self):
        assert af.abbreviate_city_name("Bozeman") == "BOZE"

    def test_empty(self):
        assert af.abbreviate_city_name("") == ""


class TestAlertWindow:
    def test_both_ends(self, en):
        out = af.format_alert_window(
            {"effective": "2026-06-28T15:00:00-07:00", "expires": "2026-06-29T06:00:00-07:00"}, en
        )
        assert out == "from Jun 28 3PM til Jun 29 6AM"

    def test_localized(self, ru):
        out = af.format_alert_window({"expires": "2026-06-29T06:00:00-07:00"}, ru)
        assert out == "до июн 29 6 утра"

    def test_no_times(self, en):
        assert af.format_alert_window({}, en) == ""
