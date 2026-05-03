"""Tests for modules.utils."""

import configparser
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from modules.utils import (
    abbreviate_location,
    calculate_distance,
    calculate_packet_hash,
    calculate_path_distances,
    check_internet_connectivity,
    decode_escape_sequences,
    decode_path_len_byte,
    encode_path_len_byte,
    extract_path_node_ids_from_message,
    format_elapsed_display,
    format_keyword_response_with_placeholders,
    format_location_for_display,
    get_config_timezone,
    get_major_city_queries,
    is_valid_timezone,
    node_ids_from_path_string,
    parse_location_string,
    parse_path_string,
    parse_trace_payload_route_hashes,
    resolve_path,
    truncate_string,
    verify_meshcore_advert_ed25519,
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
        decoded = decode_path_len_byte(0x01)
        assert decoded is not None
        path_byte_length, bytes_per_hop = decoded
        assert path_byte_length == 1
        assert bytes_per_hop == 1

    def test_single_byte_three_hops(self):
        decoded = decode_path_len_byte(0x03)
        assert decoded is not None
        path_byte_length, bytes_per_hop = decoded
        assert path_byte_length == 3
        assert bytes_per_hop == 1

    def test_multi_byte_two_bytes_per_hop_one_hop(self):
        # size_code=1 -> 2 bytes/hop, hop_count=1 -> 2 path bytes
        decoded = decode_path_len_byte(0x41)
        assert decoded is not None
        path_byte_length, bytes_per_hop = decoded
        assert path_byte_length == 2
        assert bytes_per_hop == 2

    def test_multi_byte_two_bytes_per_hop_three_hops(self):
        # size_code=1, hop_count=3 -> 6 path bytes
        decoded = decode_path_len_byte(0x43)
        assert decoded is not None
        path_byte_length, bytes_per_hop = decoded
        assert path_byte_length == 6
        assert bytes_per_hop == 2

    def test_three_bytes_per_hop(self):
        # size_code=2 -> 3 bytes/hop, hop_count=2 -> 6 path bytes
        decoded = decode_path_len_byte(0x82)
        assert decoded is not None
        path_byte_length, bytes_per_hop = decoded
        assert path_byte_length == 6
        assert bytes_per_hop == 3

    def test_reserved_size_code_returns_none(self):
        # size_code=3 (bytes_per_hop=4) is reserved — firmware rejects
        assert decode_path_len_byte(0xC2) is None

    def test_path_exceeds_max_returns_none(self):
        # 32 hops * 2 bytes = 64, max_path_size=64 is ok; 33*2=66 > 64 — reject
        decoded = decode_path_len_byte(0x41, max_path_size=64)
        assert decoded is not None
        path_byte_length, bytes_per_hop = decoded
        assert path_byte_length == 2
        assert bytes_per_hop == 2
        assert decode_path_len_byte(0x61, max_path_size=64) is None  # 33 hops * 2

    def test_zero_hops(self):
        decoded = decode_path_len_byte(0x00)
        assert decoded is not None
        path_byte_length, bytes_per_hop = decoded
        assert path_byte_length == 0
        assert bytes_per_hop == 1


class TestParseTracePayloadRouteHashes:
    """Tests for parse_trace_payload_route_hashes() (TRACE payload tail, MeshCore firmware)."""

    def test_skye_sample_tail(self):
        payload = bytes.fromhex("5F0AED1A000000000037D637")
        assert parse_trace_payload_route_hashes(payload) == ["37", "D6", "37"]

    def test_two_byte_hashes_flags_1(self):
        pl = b"\x00" * 8 + b"\x01" + b"\xaa\xbb\xcc\xdd"
        assert parse_trace_payload_route_hashes(pl) == ["AABB", "CCDD"]

    def test_short_payload_returns_empty(self):
        assert parse_trace_payload_route_hashes(b"\x00" * 8) == []


class TestEncodePathLenByte:
    """Tests for encode_path_len_byte() (inverse of decode_path_len_byte for valid encodings)."""

    def test_four_hops_one_byte_per_hop(self):
        assert encode_path_len_byte(4, 1) == 0x04
        assert (0x04 >> 6) == 0
        assert (0x04 & 0x3F) == 4

    def test_three_hops_two_bytes_per_hop(self):
        assert encode_path_len_byte(3, 2) == 0x43
        dec = decode_path_len_byte(0x43)
        assert dec is not None
        pbl, bph = dec
        assert pbl == 6
        assert bph == 2

    def test_roundtrip_with_decode(self):
        for pb in (0x01, 0x03, 0x41, 0x43, 0x82, 0x00):
            dec = decode_path_len_byte(pb)
            if dec is None:
                continue
            pbl, bph = dec
            hop_count = pb & 0x3F
            assert encode_path_len_byte(hop_count, bph) == pb

    def test_invalid_bytes_per_hop_raises(self):
        with pytest.raises(ValueError):
            encode_path_len_byte(4, 4)


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


class TestNodeIdsFromPathString:
    """Tests for node_ids_from_path_string() — multi-byte comma tokens vs legacy parse."""

    def test_six_char_comma_tokens_not_split_into_one_byte_ids(self):
        assert node_ids_from_path_string("28667c,e0eed9", 2) == ["28667C", "E0EED9"]

    def test_strips_hop_suffix_before_comma_parse(self):
        assert node_ids_from_path_string("28667c,e0eed9 (3 hops)", 2) == ["28667C", "E0EED9"]

    def test_legacy_one_byte_comma_path(self):
        assert node_ids_from_path_string("01,5f (2 hops)", 2) == ["01", "5F"]

    def test_continuous_hex_uses_prefix_width(self):
        assert node_ids_from_path_string("01025f7e", 2) == ["01", "02", "5F", "7E"]

    def test_direct_returns_empty(self):
        assert node_ids_from_path_string("Direct", 2) == []


class TestExtractPathNodeIdsFromMessage:
    """Tests for extract_path_node_ids_from_message()."""

    def test_prefers_routing_info_path_nodes(self):
        m = Mock()
        m.path = "01,5f"
        m.routing_info = {"path_length": 2, "path_nodes": ["28667C", "E0EED9"]}
        assert extract_path_node_ids_from_message(m) == ["28667C", "E0EED9"]

    def test_path_length_zero_returns_empty(self):
        m = Mock()
        m.path = "anything"
        m.routing_info = {"path_length": 0}
        assert extract_path_node_ids_from_message(m) == []

    def test_comma_multibyte_from_path_string(self):
        m = Mock()
        m.path = "28667c,e0eed9 (3 hops)"
        m.routing_info = None
        assert extract_path_node_ids_from_message(m) == ["28667C", "E0EED9"]


# ---------------------------------------------------------------------------
# is_valid_timezone
# ---------------------------------------------------------------------------

class TestIsValidTimezone:
    """Tests for is_valid_timezone()."""

    def test_valid_utc(self):
        assert is_valid_timezone("UTC") is True

    def test_valid_us_eastern(self):
        assert is_valid_timezone("America/New_York") is True

    def test_valid_europe_london(self):
        assert is_valid_timezone("Europe/London") is True

    def test_invalid_returns_false(self):
        assert is_valid_timezone("Not/A/Timezone") is False

    def test_empty_string_returns_false(self):
        assert is_valid_timezone("") is False

    def test_whitespace_only_returns_false(self):
        assert is_valid_timezone("   ") is False

    def test_strips_whitespace_before_checking(self):
        assert is_valid_timezone("  UTC  ") is True


# ---------------------------------------------------------------------------
# get_config_timezone
# ---------------------------------------------------------------------------

class TestGetConfigTimezone:
    """Tests for get_config_timezone()."""

    def _config(self, tz_value=""):
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot")
        if tz_value:
            cfg.set("Bot", "timezone", tz_value)
        return cfg

    def test_valid_timezone_returned(self):
        cfg = self._config("UTC")
        tz, iana = get_config_timezone(cfg)
        assert iana == "UTC"
        assert tz is not None

    def test_invalid_timezone_falls_back_to_utc_iana(self):
        cfg = self._config("Not/Valid")
        _, iana = get_config_timezone(cfg)
        assert iana == "UTC"

    def test_empty_timezone_falls_back(self):
        cfg = self._config("")
        _, iana = get_config_timezone(cfg)
        assert iana == "UTC"

    def test_invalid_timezone_logs_warning_when_logger_provided(self):
        cfg = self._config("Bad/Zone")
        logger = Mock()
        get_config_timezone(cfg, logger)
        logger.warning.assert_called_once()

    def test_no_logger_no_crash_on_invalid(self):
        cfg = self._config("Bad/Zone")
        _, iana = get_config_timezone(cfg, None)
        assert iana == "UTC"


# ---------------------------------------------------------------------------
# format_location_for_display
# ---------------------------------------------------------------------------

class TestFormatLocationForDisplay:
    """Tests for format_location_for_display()."""

    def test_none_city_returns_none(self):
        assert format_location_for_display(None) is None

    def test_empty_city_returns_none(self):
        assert format_location_for_display("") is None

    def test_city_only(self):
        result = format_location_for_display("Seattle", max_length=50)
        assert "Seattle" in result

    def test_city_and_state(self):
        result = format_location_for_display("Seattle", "WA", max_length=50)
        assert "Seattle" in result
        assert "WA" in result

    def test_state_not_duplicated_when_same_as_city(self):
        result = format_location_for_display("Seattle", "Seattle", max_length=50)
        # "Seattle" should not appear twice as comma-joined
        assert result.count("Seattle") == 1

    def test_respects_max_length(self):
        result = format_location_for_display("Very Long City Name That Goes On", "State", max_length=15)
        assert len(result) <= 15


# ---------------------------------------------------------------------------
# get_major_city_queries
# ---------------------------------------------------------------------------

class TestGetMajorCityQueries:
    """Tests for get_major_city_queries()."""

    def test_known_city_returns_list(self):
        result = get_major_city_queries("seattle")
        assert isinstance(result, list)
        assert len(result) > 0
        assert any("Seattle" in q for q in result)

    def test_unknown_city_returns_empty(self):
        result = get_major_city_queries("tinyunknownvillage")
        assert result == []

    def test_case_insensitive(self):
        assert get_major_city_queries("Seattle") == get_major_city_queries("seattle")

    def test_new_york_returns_multiple_queries(self):
        result = get_major_city_queries("new york")
        assert len(result) >= 1

    def test_portland_returns_multiple_queries(self):
        # Portland has OR and ME variants
        result = get_major_city_queries("portland")
        assert len(result) >= 2


# ---------------------------------------------------------------------------
# resolve_path
# ---------------------------------------------------------------------------

class TestResolvePath:
    """Tests for resolve_path()."""

    def test_absolute_path_unchanged(self):
        result = resolve_path("/var/lib/bot/data.db", "/opt/bot")
        assert result == "/var/lib/bot/data.db"

    def test_relative_path_resolved_to_base_dir(self):
        result = resolve_path("data.db", "/opt/bot")
        assert result == "/opt/bot/data.db"

    def test_path_object_input(self):
        result = resolve_path(Path("data.db"), Path("/opt/bot"))
        assert result == "/opt/bot/data.db"

    def test_returns_string(self):
        result = resolve_path("data.db", "/opt/bot")
        assert isinstance(result, str)

    def test_dot_base_dir_resolves_to_cwd(self):
        import os
        result = resolve_path("data.db", ".")
        assert result == os.path.join(os.getcwd(), "data.db")


# ---------------------------------------------------------------------------
# check_internet_connectivity
# ---------------------------------------------------------------------------

class TestCheckInternetConnectivity:
    """Tests for check_internet_connectivity()."""

    def test_returns_true_when_socket_connects(self):
        mock_sock = Mock()
        with patch("modules.utils.socket.socket") as mock_socket_cls:
            mock_socket_cls.return_value = mock_sock
            result = check_internet_connectivity(host="8.8.8.8", port=53, timeout=1.0)
        assert result is True
        mock_sock.connect.assert_called_once_with(("8.8.8.8", 53))

    def test_returns_false_when_all_fail(self):
        with patch("modules.utils.socket.socket") as mock_socket_cls, \
             patch("modules.utils.urllib.request.urlopen") as mock_urlopen:
            mock_socket_cls.return_value.connect.side_effect = OSError("refused")
            mock_urlopen.side_effect = OSError("no net")
            result = check_internet_connectivity(host="8.8.8.8", port=53, timeout=1.0)
        assert result is False

    def test_falls_back_to_http_when_socket_fails(self):
        mock_response = Mock()
        mock_response.close = Mock()
        with patch("modules.utils.socket.socket") as mock_socket_cls, \
             patch("modules.utils.urllib.request.urlopen") as mock_urlopen:
            mock_socket_cls.return_value.connect.side_effect = OSError("refused")
            mock_urlopen.return_value = mock_response
            result = check_internet_connectivity(host="8.8.8.8", port=53, timeout=1.0)
        assert result is True


# ---------------------------------------------------------------------------
# calculate_path_distances
# ---------------------------------------------------------------------------

class TestCalculatePathDistances:
    """Tests for calculate_path_distances()."""

    def _bot(self):
        bot = Mock()
        bot.db_manager = Mock()
        bot.prefix_hex_chars = 2
        bot.logger = Mock()
        return bot

    def test_empty_path_returns_direct(self):
        path_dist, fl_dist = calculate_path_distances(self._bot(), "")
        assert "direct" in path_dist.lower()

    def test_direct_path_returns_direct(self):
        path_dist, fl_dist = calculate_path_distances(self._bot(), "Direct")
        assert "direct" in path_dist.lower()

    def test_no_db_manager_returns_unknown(self):
        bot = Mock(spec=[])  # No db_manager attribute
        path_dist, fl_dist = calculate_path_distances(bot, "01,5f")
        assert "unknown" in path_dist.lower()

    def test_single_node_returns_locally(self):
        bot = self._bot()
        with patch("modules.utils._get_node_location_from_db", return_value=None):
            path_dist, fl_dist = calculate_path_distances(bot, "01")
        assert "local" in path_dist.lower() or "1 hop" in path_dist.lower()

    def test_two_nodes_with_locations_returns_distance(self):
        bot = self._bot()
        # Seattle and Portland coords
        locations = [((47.6062, -122.3321), None), ((45.5152, -122.6784), None)]
        with patch("modules.utils._get_node_location_from_db", side_effect=locations):
            path_dist, fl_dist = calculate_path_distances(bot, "01,5f")
        assert "km" in path_dist
        assert "km" in fl_dist

    def test_two_nodes_no_locations_returns_unknown(self):
        bot = self._bot()
        with patch("modules.utils._get_node_location_from_db", return_value=None):
            path_dist, fl_dist = calculate_path_distances(bot, "01,5f")
        assert "unknown" in path_dist.lower()

    def test_multibyte_comma_path_one_segment_not_five(self):
        """6-char hop IDs must not be split into 2-char tokens (would inflate segment count)."""
        bot = self._bot()
        bot.prefix_hex_chars = 2
        locations = [((47.6062, -122.3321), None), ((45.5152, -122.6784), None)]
        with patch("modules.utils._get_node_location_from_db", side_effect=locations):
            path_dist, fl_dist = calculate_path_distances(bot, "28667c,e0eed9")
        assert "(1 segs)" in path_dist
        assert "km" in fl_dist

    def test_message_routing_info_path_nodes_overrides_display_path(self):
        bot = self._bot()
        bot.prefix_hex_chars = 2
        msg = Mock()
        msg.path = "01,5f"
        msg.routing_info = {
            "path_length": 2,
            "path_nodes": ["28667c", "e0eed9"],
        }
        locations = [((47.6062, -122.3321), None), ((45.5152, -122.6784), None)]
        with patch("modules.utils._get_node_location_from_db", side_effect=locations):
            path_dist, fl_dist = calculate_path_distances(bot, "", message=msg)
        assert "(1 segs)" in path_dist


# ---------------------------------------------------------------------------
# format_keyword_response_with_placeholders
# ---------------------------------------------------------------------------

class TestFormatKeywordResponseWithPlaceholders:
    """Tests for format_keyword_response_with_placeholders()."""

    def _bot(self):
        bot = Mock()
        bot.config = configparser.ConfigParser()
        bot.config.add_section("Bot")
        bot.config.set("Bot", "timezone", "UTC")
        bot.db_manager = Mock()
        bot.prefix_hex_chars = 2
        bot.logger = Mock()
        bot.translator = None
        return bot

    def _msg(self, **kwargs):
        msg = Mock()
        msg.sender_id = kwargs.get("sender_id", "Alice")
        msg.path = kwargs.get("path", "01,5f")
        msg.snr = kwargs.get("snr", 10)
        msg.rssi = kwargs.get("rssi", -80)
        msg.timestamp = kwargs.get("timestamp")
        msg.hops = kwargs.get("hops")
        return msg

    def test_sender_placeholder(self):
        bot = self._bot()
        msg = self._msg(sender_id="Bob")
        with patch("modules.utils.calculate_path_distances", return_value=("", "")):
            result = format_keyword_response_with_placeholders("{sender}", msg, bot)
        assert result == "Bob"

    def test_no_message_uses_unknown_defaults(self):
        bot = self._bot()
        result = format_keyword_response_with_placeholders("{sender}", None, bot)
        assert result == "Unknown"

    def test_mesh_info_total_contacts(self):
        bot = self._bot()
        mesh_info = {"total_contacts": 42}
        result = format_keyword_response_with_placeholders("{total_contacts}", None, bot, mesh_info)
        assert result == "42"

    def test_missing_placeholder_returns_format_string(self):
        bot = self._bot()
        # {nonexistent} is not in replacements -> KeyError -> returns raw string
        result = format_keyword_response_with_placeholders("{nonexistent}", None, bot)
        assert result == "{nonexistent}"

    def test_hops_label_singular(self):
        bot = self._bot()
        msg = self._msg(hops=1)
        with patch("modules.utils.calculate_path_distances", return_value=("", "")):
            result = format_keyword_response_with_placeholders("{hops_label}", msg, bot)
        assert result == "1 hop"

    def test_hops_label_plural(self):
        bot = self._bot()
        msg = self._msg(hops=3)
        with patch("modules.utils.calculate_path_distances", return_value=("", "")):
            result = format_keyword_response_with_placeholders("{hops_label}", msg, bot)
        assert result == "3 hops"

    def test_connection_info_contains_snr_rssi(self):
        bot = self._bot()
        msg = self._msg(snr=12, rssi=-75)
        with patch("modules.utils.calculate_path_distances", return_value=("", "")):
            result = format_keyword_response_with_placeholders("{connection_info}", msg, bot)
        assert "SNR" in result
        assert "RSSI" in result

class TestVerifyMeshcoreAdvertEd25519:
    """Tests for verify_meshcore_advert_ed25519() matching MeshCore createAdvert signing."""

    def test_too_short_returns_false(self):
        assert verify_meshcore_advert_ed25519(b"\x00" * 99) is False

    def test_valid_signed_payload_verifies(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        ts = (1718319158).to_bytes(4, "little")
        app_data = b"\x92TestNode"  # flags + short name; total mesh payload > 100
        msg = pub + ts + app_data
        sig = priv.sign(msg)
        mesh_payload = pub + ts + sig + app_data
        assert len(mesh_payload) >= 100
        assert verify_meshcore_advert_ed25519(mesh_payload) is True

    def test_tampered_app_data_fails(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        ts = (1).to_bytes(4, "little")
        app_data = b"\x01" * 8
        msg = pub + ts + app_data
        sig = priv.sign(msg)
        mesh_payload = bytearray(pub + ts + sig + app_data)
        mesh_payload[-1] ^= 0xFF
        assert verify_meshcore_advert_ed25519(bytes(mesh_payload)) is False

    def test_tampered_signature_fails(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        ts = (2).to_bytes(4, "little")
        app_data = b"\x02" * 8
        msg = pub + ts + app_data
        sig = priv.sign(msg)
        mesh_payload = bytearray(pub + ts + sig + app_data)
        mesh_payload[40] ^= 0xFF
        assert verify_meshcore_advert_ed25519(bytes(mesh_payload)) is False


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


class TestCalculatePacketHashEdgeCases:
    """Additional calculate_packet_hash branch coverage."""

    def test_payload_type_with_value_attr_handled(self):
        """Lines 376-378: payload_type object with .value attribute is accepted."""
        # FLOOD+TXT_MSG header: (0<<6)|(2<<2)|1 = 0x09, no transport, 0 hops, 1 payload byte
        raw = "090000ff"

        class FakeEnum:
            value = 2  # TXT_MSG

        h = calculate_packet_hash(raw, payload_type=FakeEnum())
        assert h != "0000000000000000"
        assert len(h) == 16

    def test_too_short_for_path_len_returns_default(self):
        """Line 391: packet with only header byte (and transport if applicable) → default hash."""
        # Header only, no path_len_byte: just "09" (1 byte, offset=1, len<=1 → too short)
        h = calculate_packet_hash("09")
        assert h == "0000000000000000"

    def test_not_enough_path_bytes_returns_default(self):
        """Line 399: path_len_byte says N hops but fewer bytes available → default hash."""
        # Header 0x09 (FLOOD+TXT_MSG), path_len_byte=0x02 (2 hops 1-byte), but only 1 path byte
        h = calculate_packet_hash("09020100")  # header, path_len(2 hops), 1 path byte + 1 'payload'?
        # Actually: header(09), path_len(02)=2 hops → needs 2 path bytes + 1 payload byte = 5 bytes total
        # We provide 4 bytes: 09 02 01 00 → path bytes = 01 00 but payload missing?
        # Let's check: offset=1, path_len_byte=02 → path_byte_length=2, offset after path_len=2
        # need len>=2+2=4 for path, but payload needed too: len>=5
        # Exactly 4 bytes → payload_start=4 → len==4 not > 4 → return default
        assert h == "0000000000000000"

    def test_exception_in_packet_hash_returns_default(self):
        """Lines 427-429: exception during processing returns default hash."""
        h = calculate_packet_hash("not-valid-hex!!!")
        assert h == "0000000000000000"

    def test_invalid_path_len_encoding_returns_default_hash(self):
        """Reserved path size class (hash_size==4) → strict decode fails → default hash."""
        # FLOOD + TXT_MSG, path_len byte 0xF5 (size code 3 → bytes_per_hop=4 reserved)
        h = calculate_packet_hash("09F500")
        assert h == "0000000000000000"

    def test_non_transport_type_skips_transport_bytes(self):
        """Route type 1 (FLOOD) → no transport bytes; hash succeeds."""
        # Header 0x09 = FLOOD + TXT_MSG, path_len=0, payload=0xFF
        h = calculate_packet_hash("090000ff")
        assert h != "0000000000000000"
        assert len(h) == 16

    def test_transport_type_skips_4_bytes(self):
        """Route type 0 (TRANSPORT_FLOOD) → offset +=4; packet big enough."""
        # Header 0x08 = TRANSPORT_FLOOD + TXT_MSG, 4 transport bytes, path_len=0, payload=0xFF
        h = calculate_packet_hash("0800000000" + "00" + "ff")
        assert h != "0000000000000000"

