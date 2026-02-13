"""Tests for modules.utils."""

import pytest
from unittest.mock import MagicMock

from modules.utils import (
    abbreviate_location,
    truncate_string,
    decode_escape_sequences,
    parse_location_string,
    calculate_distance,
    format_elapsed_display,
    parse_path_string,
)


class TestAbbreviateLocation:
    """Tests for abbreviate_location()."""

    def test_empty_returns_empty(self):
        assert abbreviate_location("") == ""
        assert abbreviate_location(None) is None

    def test_under_max_length_unchanged(self):
        assert abbreviate_location("Seattle", max_length=20) == "Seattle"
        assert abbreviate_location("Portland, OR", max_length=20) == "Portland, OR"

    def test_united_states_abbreviated(self):
        assert "USA" in abbreviate_location("United States of America", max_length=50)
        assert abbreviate_location("United States", max_length=50) == "USA"

    def test_british_columbia_abbreviated(self):
        assert abbreviate_location("Vancouver, British Columbia", max_length=50) == "Vancouver, BC"

    def test_over_max_truncates_with_ellipsis(self):
        result = abbreviate_location("Very Long City Name That Exceeds Limit", max_length=20)
        assert len(result) <= 20
        assert result.endswith("...")

    def test_comma_separated_keeps_first_part_when_truncating(self):
        result = abbreviate_location("Seattle, Washington, USA", max_length=10)
        assert "Seattle" in result or result.startswith("Seattle")


class TestTruncateString:
    """Tests for truncate_string()."""

    def test_empty_returns_empty(self):
        assert truncate_string("", 10) == ""
        assert truncate_string(None, 10) is None

    def test_under_max_unchanged(self):
        assert truncate_string("hello", 10) == "hello"

    def test_over_max_truncates_with_ellipsis(self):
        assert truncate_string("hello world", 8) == "hello..."
        assert truncate_string("hello world", 11) == "hello world"

    def test_custom_ellipsis(self):
        # max_length=8 with ellipsis=".." (2 chars) -> 6 chars + ".."
        assert truncate_string("hello world", 8, ellipsis="..") == "hello .."


class TestDecodeEscapeSequences:
    """Tests for decode_escape_sequences()."""

    def test_empty_returns_empty(self):
        assert decode_escape_sequences("") == ""
        assert decode_escape_sequences(None) is None

    def test_newline(self):
        assert decode_escape_sequences(r"Line 1\nLine 2") == "Line 1\nLine 2"

    def test_tab(self):
        assert decode_escape_sequences(r"Col1\tCol2") == "Col1\tCol2"

    def test_literal_backslash_n(self):
        assert decode_escape_sequences(r"Literal \\n here") == "Literal \\n here"

    def test_mixed(self):
        result = decode_escape_sequences(r"Line 1\nLine 2\tTab")
        assert "Line 1" in result
        assert "\n" in result
        assert "\t" in result

    def test_carriage_return(self):
        assert decode_escape_sequences(r"Line1\r\nLine2") == "Line1\r\nLine2"


class TestParseLocationString:
    """Tests for parse_location_string()."""

    def test_no_comma_returns_city_only(self):
        city, second, kind = parse_location_string("Seattle")
        assert city == "Seattle"
        assert second is None
        assert kind is None

    def test_zipcode_only(self):
        city, second, kind = parse_location_string("98101")
        assert city == "98101"
        assert second is None
        assert kind is None

    def test_city_state_format(self):
        city, second, kind = parse_location_string("Seattle, WA")
        assert city == "Seattle"
        assert second is not None
        assert kind in ("state", None)

    def test_city_country_format(self):
        city, second, kind = parse_location_string("Stockholm, Sweden")
        assert city == "Stockholm"
        assert second is not None


class TestCalculateDistance:
    """Tests for calculate_distance() (Haversine)."""

    def test_same_point_zero_distance(self):
        assert calculate_distance(47.6062, -122.3321, 47.6062, -122.3321) == 0.0

    def test_known_distance_seattle_portland(self):
        # Seattle to Portland ~233 km
        dist = calculate_distance(47.6062, -122.3321, 45.5152, -122.6784)
        assert 220 < dist < 250

    def test_known_distance_short(self):
        # ~1 degree lat at equator ~111 km
        dist = calculate_distance(0, 0, 1, 0)
        assert 110 < dist < 112


class TestFormatElapsedDisplay:
    """Tests for format_elapsed_display()."""

    def test_none_returns_sync_message(self):
        assert "Sync" in format_elapsed_display(None)
        assert "Clock" in format_elapsed_display(None)

    def test_unknown_returns_sync_message(self):
        assert "Sync" in format_elapsed_display("unknown")

    def test_invalid_type_returns_sync_message(self):
        assert "Sync" in format_elapsed_display("not_a_number")

    def test_valid_recent_timestamp_returns_ms(self):
        import time
        ts = time.time() - 1.5  # 1.5 seconds ago
        result = format_elapsed_display(ts)
        assert "ms" in result
        assert "Sync" not in result

    def test_future_timestamp_returns_sync_message(self):
        import time
        ts = time.time() + 3600  # 1 hour in future
        assert "Sync" in format_elapsed_display(ts)

    def test_translator_used_when_provided(self):
        translator = MagicMock()
        translator.translate = MagicMock(return_value="Custom Sync Message")
        result = format_elapsed_display(None, translator=translator)
        assert result == "Custom Sync Message"


class TestParsePathString:
    """Tests for parse_path_string()."""

    def test_empty_returns_empty_list(self):
        assert parse_path_string("") == []
        assert parse_path_string(None) == []

    def test_comma_separated(self):
        assert parse_path_string("01,5f,ab") == ["01", "5F", "AB"]

    def test_space_separated(self):
        assert parse_path_string("01 5f ab") == ["01", "5F", "AB"]

    def test_continuous_hex(self):
        assert parse_path_string("015fab") == ["01", "5F", "AB"]

    def test_with_hop_count_suffix(self):
        result = parse_path_string("01,5f (2 hops)")
        assert result == ["01", "5F"]

    def test_mixed_case_normalized_uppercase(self):
        assert parse_path_string("01,5f,aB") == ["01", "5F", "AB"]
