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
    decode_path_len_byte,
    calculate_packet_hash,
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


class TestDecodePathLenByte:
    """Tests for decode_path_len_byte() (RF path_len encoding: low 6 bits = hop count, high 2 = size code)."""

    def test_single_byte_one_hop(self):
        # size_code=0 -> 1 byte/hop, hop_count=1 -> 1 path byte
        path_byte_length, bytes_per_hop = decode_path_len_byte(0x01)
        assert path_byte_length == 1
        assert bytes_per_hop == 1

    def test_single_byte_three_hops(self):
        path_byte_length, bytes_per_hop = decode_path_len_byte(0x03)
        assert path_byte_length == 3
        assert bytes_per_hop == 1

    def test_multi_byte_two_bytes_per_hop_one_hop(self):
        # size_code=1 -> 2 bytes/hop, hop_count=1 -> 2 path bytes
        path_byte_length, bytes_per_hop = decode_path_len_byte(0x41)
        assert path_byte_length == 2
        assert bytes_per_hop == 2

    def test_multi_byte_two_bytes_per_hop_three_hops(self):
        # size_code=1, hop_count=3 -> 6 path bytes
        path_byte_length, bytes_per_hop = decode_path_len_byte(0x43)
        assert path_byte_length == 6
        assert bytes_per_hop == 2

    def test_three_bytes_per_hop(self):
        # size_code=2 -> 3 bytes/hop, hop_count=2 -> 6 path bytes
        path_byte_length, bytes_per_hop = decode_path_len_byte(0x82)
        assert path_byte_length == 6
        assert bytes_per_hop == 3

    def test_reserved_size_code_fallback(self):
        # size_code=3 (bytes_per_hop=4) is reserved -> legacy: path_len_byte as raw byte count, 1 byte/hop
        path_byte_length, bytes_per_hop = decode_path_len_byte(0xC2)
        assert path_byte_length == 0xC2  # raw byte value
        assert bytes_per_hop == 1

    def test_path_exceeds_max_fallback(self):
        # 32 hops * 2 bytes = 64, max_path_size=64 is ok; 33*2=66 > 64 -> legacy
        path_byte_length, bytes_per_hop = decode_path_len_byte(0x41, max_path_size=64)
        assert path_byte_length == 2
        assert bytes_per_hop == 2
        path_byte_length, bytes_per_hop = decode_path_len_byte(0x61, max_path_size=64)  # 33 hops * 2
        assert path_byte_length == 0x61
        assert bytes_per_hop == 1

    def test_zero_hops(self):
        path_byte_length, bytes_per_hop = decode_path_len_byte(0x00)
        assert path_byte_length == 0
        assert bytes_per_hop == 1


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

    # --- Multi-byte (4 hex chars = 2 bytes per hop) ---

    def test_four_char_continuous_hex(self):
        assert parse_path_string("01025fab", prefix_hex_chars=4) == ["0102", "5FAB"]

    def test_four_char_comma_separated(self):
        assert parse_path_string("0102,5fab,abcd", prefix_hex_chars=4) == ["0102", "5FAB", "ABCD"]

    def test_four_char_space_separated(self):
        assert parse_path_string("0102 5fab abcd", prefix_hex_chars=4) == ["0102", "5FAB", "ABCD"]

    def test_four_char_legacy_fallback_when_no_four_char_matches(self):
        # Input that has no 4-char groups (odd length or different pattern) -> fallback to 2-char
        result = parse_path_string("01", prefix_hex_chars=4)
        assert result == ["01"]

    def test_two_char_explicit(self):
        """Ensure prefix_hex_chars=2 still works when passed explicitly."""
        assert parse_path_string("015fab", prefix_hex_chars=2) == ["01", "5F", "AB"]


class TestCalculatePacketHashPathLength:
    """Tests that calculate_packet_hash uses decode_path_len_byte so multi-byte paths skip correctly."""

    def test_single_byte_path_hash_valid(self):
        # Minimal TRACE packet: header(0x24=route0 type9), 4B transport, path_len=0x01 (1 hop, 1 byte), path [0x01], payload
        raw = "24000000000101deadbeef"
        h = calculate_packet_hash(raw)
        assert len(h) == 16
        assert all(c in "0123456789ABCDEF" for c in h)
        assert h != "0000000000000000"

    def test_multi_byte_path_hash_valid(self):
        # Same but path_len=0x41 (1 hop, 2 bytes), path 0x01 0x02, same payload
        raw = "2400000000410102deadbeef"
        h = calculate_packet_hash(raw)
        assert len(h) == 16
        assert all(c in "0123456789ABCDEF" for c in h)
        assert h != "0000000000000000"

    def test_single_vs_multi_byte_path_different_hashes(self):
        # TRACE includes path_byte_length in hash, so different path lengths must produce different hashes
        single = "24000000000101deadbeef"
        multi = "2400000000410102deadbeef"
        assert calculate_packet_hash(single) != calculate_packet_hash(multi)


class TestMultiBytePathDisplayContract:
    """Contract: path_hex stored with bytes_per_hop=2 should format to comma-separated 4-char nodes."""

    def test_multi_byte_path_format_contract(self):
        # path_hex "01025fab" with 2 bytes per hop (4 hex chars per node) -> "0102,5fab"
        path_hex = "01025fab"
        nodes = parse_path_string(path_hex, prefix_hex_chars=4)
        assert nodes == ["0102", "5FAB"]
        display = ",".join(n.lower() for n in nodes)
        assert display == "0102,5fab"

    def test_single_byte_path_format_contract(self):
        # path_hex "01025fab" with 1 byte per hop (2 hex chars) -> "01,02,5f,ab"
        path_hex = "01025fab"
        nodes = parse_path_string(path_hex, prefix_hex_chars=2)
        assert nodes == ["01", "02", "5F", "AB"]
        display = ",".join(n.lower() for n in nodes)
        assert display == "01,02,5f,ab"

