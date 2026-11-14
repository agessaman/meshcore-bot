"""Tests for modules.commands.multitest_command — pure logic and reply rendering."""

import configparser
import time
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import modules.commands.multitest_command as multitest_module
from modules.clients.corescope_client import ScopePacket
from modules.commands.multitest_command import (
    MultitestCommand,
    _condense_path_lines,
    _parse_condense_paths_mode,
    _path_to_tokens,
)
from tests.conftest import mock_message

_INTER = "\u251c"
_LAST = "\u2514"
_INDENT = "\u3000"  # 　 before nested ├/└
_CHILD_INTER = f"{_INDENT}{_INTER} "
_CHILD_LAST = f"{_INDENT}{_LAST} "
_CORNER = "\u2510"  # ┐ after common path
_NEST_COL = "  "  # nested continuation (ASCII spaces; not U+2502 — saves UTF-8 bytes on mesh)


def _make_bot():
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.add_section("Multitest_Command")
    config.set("Multitest_Command", "enabled", "true")
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.prefix_hex_chars = 2
    return bot


class TestExtractPathFromRfData:
    """Tests for extract_path_from_rf_data."""

    def setup_method(self):
        self.cmd = MultitestCommand(_make_bot())

    def test_no_routing_info_returns_none(self):
        result = self.cmd.extract_path_from_rf_data({})
        assert result is None

    def test_empty_routing_info_returns_none(self):
        result = self.cmd.extract_path_from_rf_data({"routing_info": {}})
        assert result is None

    def test_path_nodes_extracted(self):
        rf_data = {
            "routing_info": {
                "path_nodes": ["01", "7a", "55"]
            }
        }
        result = self.cmd.extract_path_from_rf_data(rf_data)
        assert result == "01,7a,55"

    def test_path_hex_fallback(self):
        rf_data = {
            "routing_info": {
                "path_nodes": [],
                "path_hex": "017a55",
                "bytes_per_hop": 1
            }
        }
        result = self.cmd.extract_path_from_rf_data(rf_data)
        assert result is not None
        assert "01" in result

    def test_invalid_nodes_skipped(self):
        rf_data = {
            "routing_info": {
                "path_nodes": ["01", "zz", "55"]
            }
        }
        result = self.cmd.extract_path_from_rf_data(rf_data)
        # zz is invalid hex, should be excluded
        if result:
            assert "zz" not in result


class TestExtractPathFromMessage:
    """Tests for extract_path_from_message."""

    def setup_method(self):
        self.cmd = MultitestCommand(_make_bot())

    def test_no_path_returns_none(self):
        msg = mock_message(content="multitest", path=None)
        msg.routing_info = None
        result = self.cmd.extract_path_from_message(msg)
        assert result is None

    def test_direct_returns_none(self):
        msg = mock_message(content="multitest", path="Direct")
        msg.routing_info = None
        result = self.cmd.extract_path_from_message(msg)
        assert result is None

    def test_zero_hops_returns_none(self):
        msg = mock_message(content="multitest", path="0 hops")
        msg.routing_info = None
        result = self.cmd.extract_path_from_message(msg)
        assert result is None

    def test_comma_path_extracted(self):
        msg = mock_message(content="multitest", path="01,7a,55")
        msg.routing_info = None
        result = self.cmd.extract_path_from_message(msg)
        assert result is not None
        assert "01" in result

    def test_routing_info_path_preferred(self):
        msg = mock_message(content="multitest", path="01,7a")
        msg.routing_info = {
            "path_length": 2,
            "path_nodes": ["7a", "55"],
            "bytes_per_hop": None
        }
        result = self.cmd.extract_path_from_message(msg)
        # routing_info is preferred
        assert result is not None


class TestMatchesKeyword:
    """Tests for matches_keyword."""

    def setup_method(self):
        self.cmd = MultitestCommand(_make_bot())

    def test_multitest_matches(self):
        assert self.cmd.matches_keyword(mock_message(content="multitest")) is True

    def test_mt_matches(self):
        assert self.cmd.matches_keyword(mock_message(content="mt")) is True

    def test_exclamation_prefix(self):
        assert self.cmd.matches_keyword(mock_message(content="!multitest")) is True

    def test_other_does_not_match(self):
        assert self.cmd.matches_keyword(mock_message(content="ping")) is False


class TestCondensePathLines:
    """Tests for _condense_path_lines."""

    def test_meshed_up_style_strict_prefix_and_branches(self):
        """Four unique paths → four endpoints (flat); no ``...`` for the shorter ``cd`` route."""
        paths = sorted(
            [
                "e6,0c,85,82,28,1a,cd,7e,01",
                "e6,0c,85,82,28,1a,cd,7e,7a",
                "e6,0c,85,82,28,1a,cd,7e,7a,09",
                "e6,0c,85,82,28,1a,cd",
            ]
        )
        out = _condense_path_lines(paths, "flat")
        expected = "\n".join(
            [
                f"e6,0c,85,82,28,1a {_CORNER}",
                f"{_INTER} cd,7e,01",
                f"{_INTER} cd,7e,7a",
                f"{_CHILD_LAST}09",
                f"{_LAST} cd",
            ]
        )
        assert out == expected

    def test_nested_two_paths_one_hop_shorter_no_spurious_corner(self):
        """``7a,e0`` vs ``7a`` must not render ``├ 7a,e0 ┐`` / ``├  ┐`` (inner2 merge bug)."""
        paths = sorted(["12,b0,2a,e8,7a,e0", "12,b0,2a,e8,7a"])
        out = _condense_path_lines(paths, "nested")
        assert "┐" not in out.split("\n", 2)[1]  # second line is a sibling branch, not ``… ┐``
        assert "├  ┐" not in out
        assert "7a,e0" in out and out.count("7a") >= 2

    def test_meshed_up_nested_layout(self):
        paths = sorted(
            [
                "e6,0c,85,82,28,1a,cd,7e,01",
                "e6,0c,85,82,28,1a,cd,7e,7a",
                "e6,0c,85,82,28,1a,cd,7e,7a,09",
                "e6,0c,85,82,28,1a,cd",
            ]
        )
        out = _condense_path_lines(paths, "nested")
        expected = "\n".join(
            [
                f"e6,0c,85,82,28,1a {_CORNER}",
                f"{_INTER} cd,7e {_CORNER}",
                f"{_NEST_COL}{_INTER} 01",
                f"{_NEST_COL}{_INTER} 7a",
                f"{_NEST_COL}{_CHILD_LAST}09",
                f"{_LAST} cd",
            ]
        )
        assert out == expected

    def test_nested_shorter_route_same_hop_no_duplicate_last_row(self):
        """One path ends at ``e0``; others go ``e0,01`` / ``e0,1e`` / ``e0,cc`` — no second ``└ e0``."""
        paths = sorted(
            [
                "d0,ab,86,e0",
                "d0,ab,86,e0,01",
                "d0,ab,86,e0,1e",
                "d0,ab,86,e0,cc",
            ]
        )
        out = _condense_path_lines(paths, "nested")
        assert out.count("e0") == 1
        assert out == "\n".join(
            [
                f"d0,ab,86 {_CORNER}",
                f"{_INTER} e0",
                f"{_NEST_COL}{_INTER} 01",
                f"{_NEST_COL}{_INTER} 1e",
                f"{_NEST_COL}{_LAST} cc",
            ]
        )

    def test_nested_inner2_merge_trunk_no_corner_when_route_ends_at_main(self):
        """``0101`` vs ``0101,0970`` vs ``0101,0970,1ed6``: inner2 merge must not use ``├ … ┐`` on the trunk."""
        paths = sorted(
            [
                "d3a0,27cf,d261,cdf1,7e76,0101",
                "d3a0,27cf,d261,cdf1,7e76,0101,0970",
                "d3a0,27cf,d261,cdf1,7e76,0101,0970,1ed6",
            ]
        )
        out = _condense_path_lines(paths, "nested")
        assert out == "\n".join(
            [
                f"d3a0,27cf,d261,cdf1,7e76 {_CORNER}",
                f"{_INTER} 0101,0970",
                f"{_NEST_COL}{_LAST} 1ed6",
                f"{_LAST} 0101",
            ]
        )

    def test_nested_disjoint_first_hops_group_e0ee_under_trunk(self):
        """No shared LCP across all suffixes → split by first hop; ``e0ee`` subtree nests ``1ed6``/``cc5d``."""
        paths = sorted(
            [
                "b007,2a6c,e863,7e76,0101",
                "b007,2a6c,e863,7e76,21cc,0970",
                "b007,2a6c,e863,7e76,e0ee",
                "b007,2a6c,e863,7e76,e0ee,1ed6",
                "b007,2a6c,e863,7e76,e0ee,cc5d",
            ]
        )
        out = _condense_path_lines(paths, "nested")
        assert out == "\n".join(
            [
                f"b007,2a6c,e863,7e76 {_CORNER}",
                f"{_INTER} 0101",
                f"{_INTER} 21cc,0970",
                f"{_INTER} e0ee",
                f"{_NEST_COL}{_INTER} 1ed6",
                f"{_NEST_COL}{_LAST} cc5d",
            ]
        )

    def test_shared_prefix_no_strict_prefix_truncation(self):
        paths = sorted(
            [
                "aa,bb,cc",
                "aa,bb,cc,dd",
                "aa,bb,cc,ee",
            ]
        )
        out = _condense_path_lines(paths, "flat")
        expected = "\n".join(
            [
                f"aa,bb {_CORNER}",
                f"{_INTER} cc,dd",
                f"{_INTER} cc,ee",
                f"{_LAST} cc",
            ]
        )
        assert out == expected

    def test_one_path_ends_at_lcp_other_extends(self):
        """Shorter LCP so both 0101 and 0101,0970 appear as branches, not one hidden on the trunk."""
        paths = sorted(["cdf1,7e76,0101", "cdf1,7e76,0101,0970"])
        out = _condense_path_lines(paths, "flat")
        expected = "\n".join(
            [
                f"cdf1,7e76 {_CORNER}",
                f"{_INTER} 0101",
                f"{_LAST} 0101,0970",
            ]
        )
        assert out == expected

    def test_overlapping_suffix_branches_under_common_prefix(self):
        paths = sorted(
            [
                "cdf119,860cca,010101",
                "cdf119,860cca,e0eed9",
                "cdf119,860cca,e0eed9,1ed612",
            ]
        )
        out = _condense_path_lines(paths, "flat")
        expected = "\n".join(
            [
                f"cdf119,860cca {_CORNER}",
                f"{_INTER} 010101",
                f"{_INTER} e0eed9",
                f"{_CHILD_LAST}1ed612",
            ]
        )
        assert out == expected

    def test_divergent_routes_with_shared_mid_prefix(self):
        """TRM-style: in-group LCP 13,01 so 1e nests; 83,09 stays its own branch."""
        paths = sorted(["41,96,13,01", "41,96,13,01,1e", "41,96,83,09"])
        out = _condense_path_lines(paths, "flat")
        expected = "\n".join(
            [
                f"41,96 {_CORNER}",
                f"{_INTER} 13,01",
                f"{_INTER} 13,01,1e",
                f"{_LAST} 83,09",
            ]
        )
        assert out == expected

    def test_shared_second_hop_not_shown_as_endpoint(self):
        """W7ZOO-style: 96,e0 shared by all variants; endpoints are 01,09,1e and fc,7a — not 96."""
        paths = sorted(
            [
                "cc,fe,17,b3,7e,96,e0",
                "cc,fe,17,b3,7e,96,e0,01",
                "cc,fe,17,b3,7e,96,e0,09",
                "cc,fe,17,b3,7e,96,e0,1e",
                "cc,fe,17,b3,7e,fc,7a",
            ]
        )
        out = _condense_path_lines(paths, "flat")
        expected = "\n".join(
            [
                f"cc,fe,17,b3,7e {_CORNER}",
                f"{_INTER} 96,e0,01",
                f"{_INTER} 96,e0,09",
                f"{_INTER} 96,e0,1e",
                f"{_INTER} 96,e0",
                f"{_LAST} fc,7a",
            ]
        )
        assert out == expected

    def test_mixed_first_hops_nest_per_group(self):
        """Ill Eagle-style: 01 vs 01,1e share a group; 09 and e0 are separate top-level branches."""
        paths = sorted(
            [
                "e2,ab,1f,ef,55,21,01",
                "e2,ab,1f,ef,55,21,01,1e",
                "e2,ab,1f,ef,55,21,09",
                "e2,ab,1f,ef,55,21,e0",
            ]
        )
        out = _condense_path_lines(paths, "flat")
        expected = "\n".join(
            [
                f"e2,ab,1f,ef,55,21 {_CORNER}",
                f"{_INTER} 01",
                f"{_CHILD_LAST}1e",
                f"{_INTER} 09",
                f"{_LAST} e0",
            ]
        )
        assert out == expected

    def test_shorter_path_one_extra_hop_still_trees(self):
        """860cca vs 860cca,010101: shrink trunk so both show as branches."""
        paths = sorted(
            [
                "d38a05,c4a86a,067b75,cafee0,1ffbd6,e8154b,860cca,010101",
                "d38a05,c4a86a,067b75,cafee0,1ffbd6,e8154b,860cca",
            ]
        )
        out = _condense_path_lines(paths, "flat")
        expected = "\n".join(
            [
                f"d38a05,c4a86a,067b75,cafee0,1ffbd6,e8154b {_CORNER}",
                f"{_INTER} 860cca",
                f"{_LAST} 860cca,010101",
            ]
        )
        assert out == expected

    def test_shared_hop_then_horiz_continuations(self):
        """Flat layout: full suffix per branch row including shared hop (e0eed9)."""
        paths = sorted(
            [
                "d38a05,479198,a837bc,7e7662,e0eed9",
                "d38a05,479198,a837bc,7e7662,e0eed9,010101",
                "d38a05,479198,a837bc,7e7662,e0eed9,0970d6",
                "d38a05,479198,a837bc,7e7662,e0eed9,1ed612",
                "d38a05,479198,a837bc,7e7662,e0eed9,f",
            ]
        )
        out = _condense_path_lines(paths, "flat")
        expected = "\n".join(
            [
                f"d38a05,479198,a837bc,7e7662 {_CORNER}",
                f"{_INTER} e0eed9,010101",
                f"{_INTER} e0eed9,0970d6",
                f"{_INTER} e0eed9,1ed612",
                f"{_INTER} e0eed9,f",
                f"{_LAST} e0eed9",
            ]
        )
        assert out == expected

    def test_disjoint_first_hop_groups_with_brackets(self):
        paths = sorted(["a,b", "c,d"])
        out = _condense_path_lines(paths, "flat")
        expected = "\n".join(["[a,b]", "[c,d]"])
        assert out == expected

    def test_single_path_unchanged(self):
        assert _condense_path_lines(["a,b,c"], "flat") == "a,b,c"

    def test_path_to_tokens_strips_trailing_empty_segment(self):
        assert _path_to_tokens("e6,0c,cd,") == ["e6", "0c", "cd"]


class TestCanExecute:
    """Tests for can_execute."""

    def test_enabled(self):
        bot = _make_bot()
        cmd = MultitestCommand(bot)
        msg = mock_message(content="multitest", channel="general")
        assert cmd.can_execute(msg) is True

    def test_disabled(self):
        bot = _make_bot()
        bot.config.set("Multitest_Command", "enabled", "false")
        cmd = MultitestCommand(bot)
        msg = mock_message(content="multitest", channel="general")
        assert cmd.can_execute(msg) is False

    def test_condense_paths_defaults_flat(self):
        cmd = MultitestCommand(_make_bot())
        assert cmd.condense_paths_mode == "flat"
        assert cmd.condense_paths is True

    def test_condense_paths_off_from_config(self):
        bot = _make_bot()
        bot.config.set("Multitest_Command", "condense_paths", "false")
        cmd = MultitestCommand(bot)
        assert cmd.condense_paths_mode == "off"
        assert cmd.condense_paths is False

    def test_condense_paths_true_from_config(self):
        bot = _make_bot()
        bot.config.set("Multitest_Command", "condense_paths", "true")
        cmd = MultitestCommand(bot)
        assert cmd.condense_paths_mode == "flat"
        assert cmd.condense_paths is True

    def test_condense_paths_nested_from_config(self):
        bot = _make_bot()
        bot.config.set("Multitest_Command", "condense_paths", "nested")
        cmd = MultitestCommand(bot)
        assert cmd.condense_paths_mode == "nested"
        assert cmd.condense_paths is True


class TestParseCondensePathsMode:
    def test_aliases(self):
        assert _parse_condense_paths_mode("false") == "off"
        assert _parse_condense_paths_mode("TRUE") == "flat"
        assert _parse_condense_paths_mode("nested") == "nested"
        assert _parse_condense_paths_mode("flat") == "flat"


class TestRenderReply:
    """The reply template: same piped placeholders and filters as the test reply."""

    def _cmd(self, response_format=None):
        bot = _make_bot()
        if response_format is not None:
            bot.config.set("Multitest_Command", "response_format", response_format)
        return MultitestCommand(bot)

    def _message(self):
        return mock_message(
            content="mt",
            sender_id="Alice",
            hops=2,
            snr=9.5,
            rssi=-98,
            routing_info={
                "path_length": 2,
                "path_nodes": ["a1b2", "c3d4"],
                "bytes_per_hop": 2,
                "packet_hash": "3F2A9C1B7E4D0A65",
            },
        )

    async def _render(self, cmd, template):
        return await cmd._render_reply(
            self._message(),
            template,
            paths_text="a1b2,c3d4\na1b2,e5f6",
            path_count=2,
            listening_duration=6,
        )

    async def test_blank_config_renders_the_default_format(self):
        cmd = self._cmd()
        assert cmd.response_format is None
        out = await self._render(cmd, cmd.DEFAULT_FORMAT)
        assert out == "Paths(2):\na1b2,c3d4\na1b2,e5f6"

    async def test_own_fields_and_sender(self):
        cmd = self._cmd()
        out = await self._render(cmd, "@[{sender}] {path_count} in {listening_duration}s:\n{paths}")
        assert out == "@[Alice] 2 in 6s:\na1b2,c3d4\na1b2,e5f6"

    async def test_message_placeholders_describe_the_triggering_message(self):
        cmd = self._cmd()
        out = await self._render(cmd, "{hops_label} | {path} | {snr} | {packet_hash}")
        assert out == "2 hops | a1b2,c3d4 (2 hops) | 9.5 | 3F2A9C1B7E4D0A65"

    async def test_filters_gate_a_clause(self):
        cmd = self._cmd()
        out = await self._render(
            cmd, "{path_count}{packet_hash|if_notempty:\"#{packet_hash}\"|prefix_if_nonempty: }"
        )
        assert out == "2 #3F2A9C1B7E4D0A65"

    async def test_pathbytes_min_clears_on_a_one_byte_path(self):
        cmd = self._cmd()
        message = mock_message(
            content="mt", sender_id="Alice", hops=2,
            routing_info={"path_length": 2, "path_nodes": ["a1", "c3"], "bytes_per_hop": 1},
        )
        out = await cmd._render_reply(
            message, "{path_count}{paths|pathbytes_min:2|prefix_if_nonempty: }",
            paths_text="a1,c3", path_count=1, listening_duration=6,
        )
        assert out == "1"

    async def test_unknown_placeholder_renders_as_nothing(self):
        cmd = self._cmd()
        out = await self._render(cmd, "{path_count} via {nope}")
        assert out == "2 via "

    async def test_escape_sequences_decoded_from_config(self):
        cmd = self._cmd('"@[{sender}]\\n{paths}"')
        assert cmd.response_format == "@[{sender}]\n{paths}"

    async def test_escapes_decoded_beside_a_non_latin1_character(self):
        """The old unicode_escape route raised here and left \\n literal."""
        cmd = self._cmd("🛰\\n{paths}")
        assert cmd.response_format == "🛰\n{paths}"


class TestExecuteLocalMode:
    """The listening-window path, pinned before CoreScope mode was added beside it.

    These assert the behaviour an operator with no CoreScope configured still gets:
    the bot registers itself as the listener, scans recent RF data, sleeps its
    window and reports what it heard itself.
    """

    def _cmd(self, **multitest):
        bot = _make_bot()
        for key, value in multitest.items():
            bot.config.set("Multitest_Command", key, value)
        bot.message_handler.multitest_listener = None
        bot.rate_limiter.can_send.return_value = True
        cmd = MultitestCommand(bot)
        cmd.get_rf_data_for_message = Mock(return_value={
            "packet_hash": "3F2A9C1B7E4D0A65",
            "timestamp": 1000.0,
            "routing_info": {"path_nodes": ["a1b2", "c3d4"], "bytes_per_hop": 2},
        })
        cmd.send_response = AsyncMock(return_value=True)
        cmd.send_response_chunked = AsyncMock(return_value=True)
        cmd.get_max_message_length = Mock(return_value=200)
        cmd._scan_recent_rf_data = Mock()
        return cmd

    def _message(self, content="mt"):
        return mock_message(
            content=content, sender_id="Alice", hops=2,
            routing_info={"path_nodes": ["a1b2", "c3d4"], "bytes_per_hop": 2,
                          "packet_hash": "3F2A9C1B7E4D0A65"},
        )

    async def test_sleeps_the_listening_window(self):
        cmd = self._cmd()
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()) as slept:
            await cmd.execute(self._message())
        assert 6.0 in [c.args[0] for c in slept.await_args_list]

    async def test_long_and_xlong_lengthen_the_window(self):
        for variant, expected in (("mt long", 10.0), ("mt xlong", 14.0)):
            cmd = self._cmd()
            with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()) as slept:
                await cmd.execute(self._message(variant))
            assert expected in [c.args[0] for c in slept.await_args_list]

    async def test_registers_itself_as_the_listener_then_unregisters(self):
        cmd = self._cmd()
        seen = []
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()) as slept:
            slept.side_effect = lambda *_a, **_k: seen.append(
                cmd.bot.message_handler.multitest_listener
            )
            await cmd.execute(self._message())
        assert cmd in seen, "listener must be registered while the window is open"
        assert cmd.bot.message_handler.multitest_listener is None

    async def test_scans_recent_rf_data(self):
        cmd = self._cmd()
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        assert cmd._scan_recent_rf_data.call_count >= 1

    async def test_reports_what_the_bot_heard_itself(self):
        cmd = self._cmd()
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        sent = cmd.send_response.await_args[0][1]
        # The triggering message's own path is seeded into the session.
        assert sent == "Paths(1):\na1b2,c3d4"

    async def test_a_second_request_while_listening_is_ignored(self):
        cmd = self._cmd()
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
            cmd._active_sessions["Alice"] = multitest_module.MultitestSession(
                user_id="Alice", target_packet_hash="3F2A9C1B7E4D0A65",
                triggering_timestamp=time.time(), listening_start_time=time.time(),
                listening_duration=6.0, collected_paths=set(),
            )
            cmd.send_response.reset_mock()
            await cmd.execute(self._message())
        cmd.send_response.assert_not_awaited()

    async def test_missing_rf_data_replies_without_listening(self):
        cmd = self._cmd()
        cmd.get_rf_data_for_message = Mock(return_value=None)
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()) as slept:
            await cmd.execute(self._message())
        slept.assert_not_awaited()
        assert "Could not find packet data" in cmd.send_response.await_args[0][1]


class TestCorescopeWiring:
    """Mode selection from config."""

    def _cmd(self, *, enabled=None, url=None, **extra):
        bot = _make_bot()
        bot.config.add_section("External_Data")
        if enabled is not None:
            bot.config.set("Multitest_Command", "corescope_enabled", enabled)
        if url is not None:
            bot.config.set("External_Data", "corescope_url", url)
        for key, value in extra.items():
            bot.config.set("Multitest_Command", key, value)
        return MultitestCommand(bot)

    def test_off_by_default(self):
        cmd = self._cmd()
        assert cmd.uses_corescope is False
        assert cmd._corescope is None

    def test_enabled_with_a_url_builds_the_client(self):
        cmd = self._cmd(enabled="true", url="https://analyzer.example.net")
        assert cmd.uses_corescope is True
        assert cmd._corescope.base_url == "https://analyzer.example.net"

    def test_enabled_without_a_url_falls_back_to_listening(self):
        cmd = self._cmd(enabled="true")
        assert cmd.uses_corescope is False
        assert cmd.bot.logger.warning.called

    def test_enabled_with_an_unusable_url_falls_back_to_listening(self):
        cmd = self._cmd(enabled="true", url="ftp://nope.example")
        assert cmd.uses_corescope is False

    def test_a_url_without_the_toggle_keeps_listening(self):
        cmd = self._cmd(url="https://analyzer.example.net")
        assert cmd.uses_corescope is False

    def test_timeout_and_cache_are_configurable(self):
        cmd = self._cmd(
            enabled="true", url="https://a.example",
            corescope_timeout="2.5", corescope_cache_seconds="60",
        )
        assert cmd._corescope.timeout == 2.5
        assert cmd._corescope.cache_seconds == 60


class TestExecuteCorescopeMode:
    """With CoreScope configured, the local listening work is deferred to it."""

    def _cmd(self, *, scope=None, **multitest):
        bot = _make_bot()
        bot.config.add_section("External_Data")
        bot.config.set("External_Data", "corescope_url", "https://analyzer.example.net")
        bot.config.set("Multitest_Command", "corescope_enabled", "true")
        for key, value in multitest.items():
            bot.config.set("Multitest_Command", key, value)
        bot.message_handler.multitest_listener = None
        bot.rate_limiter.can_send.return_value = True
        cmd = MultitestCommand(bot)
        cmd._corescope = MagicMock()
        cmd._corescope.fetch = Mock(return_value=scope)
        cmd.get_rf_data_for_message = Mock(return_value={
            "packet_hash": "3F2A9C1B7E4D0A65",
            "timestamp": 1000.0,
            "routing_info": {"path_nodes": ["a1b2", "c3d4"], "bytes_per_hop": 2},
        })
        cmd.send_response = AsyncMock(return_value=True)
        cmd.send_response_chunked = AsyncMock(return_value=True)
        cmd.get_max_message_length = Mock(return_value=200)
        cmd._scan_recent_rf_data = Mock()
        return cmd

    def _message(self, content="mt"):
        return mock_message(
            content=content, sender_id="Alice", hops=2,
            routing_info={"path_nodes": ["a1b2", "c3d4"], "bytes_per_hop": 2,
                          "packet_hash": "3F2A9C1B7E4D0A65"},
        )

    def _scope(self, *paths, observers=3, observations=9):
        return ScopePacket(
            unique_paths=len(paths), observations=observations, observers=observers,
            max_hops=max((p.count(",") + 1 for p in paths), default=0),
            paths=tuple(paths),
        )

    async def test_never_registers_itself_as_the_listener(self):
        cmd = self._cmd(scope=self._scope("a1b2,c3d4"))
        seen = []
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()) as slept:
            slept.side_effect = lambda *_a, **_k: seen.append(
                cmd.bot.message_handler.multitest_listener
            )
            await cmd.execute(self._message())
        assert cmd not in seen
        assert cmd.bot.message_handler.multitest_listener is None

    async def test_never_scans_local_rf_data(self):
        cmd = self._cmd(scope=self._scope("a1b2,c3d4"))
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        cmd._scan_recent_rf_data.assert_not_called()

    async def test_never_sleeps_a_listening_window(self):
        cmd = self._cmd(scope=self._scope("a1b2,c3d4"))
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()) as slept:
            await cmd.execute(self._message())
        assert 6.0 not in [c.args[0] for c in slept.await_args_list]

    async def test_looks_up_the_tracked_packet_hash(self):
        cmd = self._cmd(scope=self._scope("a1b2,c3d4"))
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        cmd._corescope.fetch.assert_called_once_with("3F2A9C1B7E4D0A65")

    async def test_the_count_and_paths_come_from_corescope(self):
        cmd = self._cmd(
            scope=self._scope("a1b2", "a1b2,c3d4", "a1b2,e5f6"),
            condense_paths="false",
        )
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        assert cmd.send_response.await_args[0][1] == (
            "Paths(3):\na1b2\na1b2,c3d4\na1b2,e5f6"
        )

    async def test_corescope_paths_use_the_configured_layout(self):
        cmd = self._cmd(scope=self._scope("a1b2,c3d4", "a1b2,e5f6"), condense_paths="true")
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        sent = cmd.send_response.await_args[0][1]
        assert sent.startswith("Paths(2):")
        assert _INTER in sent or _LAST in sent, "condensed layout should branch"

    async def test_the_local_path_is_not_counted_in_corescope_mode(self):
        """The trigger's own path seeds the session, but CoreScope is the source."""
        cmd = self._cmd(scope=self._scope("9999,8888"), condense_paths="false")
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        assert cmd.send_response.await_args[0][1] == "Paths(1):\n9999,8888"

    async def test_a_miss_names_the_packet_and_the_local_path(self):
        cmd = self._cmd(scope=None)
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        sent = cmd.send_response.await_args[0][1]
        assert "No CoreScope data for packet 3F2A9C1B7E4D0A65." in sent
        assert "Heard locally: a1b2,c3d4" in sent

    async def test_a_miss_with_no_local_path_says_so(self):
        cmd = self._cmd(scope=None)
        cmd.extract_path_from_message = Mock(return_value=None)
        cmd.extract_path_from_rf_data = Mock(return_value=None)
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        assert "No observer has reported it." in cmd.send_response.await_args[0][1]

    async def test_an_empty_path_set_is_a_miss(self):
        cmd = self._cmd(scope=self._scope())
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        assert "No CoreScope data" in cmd.send_response.await_args[0][1]

    async def test_the_session_is_released_even_when_the_lookup_raises(self):
        cmd = self._cmd(scope=None)
        cmd._corescope.fetch = Mock(side_effect=RuntimeError("boom"))
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            with pytest.raises(RuntimeError):
                await cmd.execute(self._message())
        assert cmd._active_sessions == {}


class TestSettleConfig:
    """corescope_settle_seconds: default, clamping and variant scaling."""

    def _cmd(self, **multitest):
        bot = _make_bot()
        bot.config.add_section("External_Data")
        bot.config.set("External_Data", "corescope_url", "https://a.example")
        bot.config.set("Multitest_Command", "corescope_enabled", "true")
        for key, value in multitest.items():
            bot.config.set("Multitest_Command", key, value)
        return MultitestCommand(bot)

    def test_defaults_to_thirty_seconds(self):
        assert self._cmd().corescope_settle_seconds == 30.0

    def test_an_explicit_value_is_used(self):
        assert self._cmd(corescope_settle_seconds="12.5").corescope_settle_seconds == 12.5

    def test_zero_is_allowed(self):
        assert self._cmd(corescope_settle_seconds="0").corescope_settle_seconds == 0.0

    def test_too_long_is_clamped_and_warned(self):
        cmd = self._cmd(corescope_settle_seconds="3000")
        assert cmd.corescope_settle_seconds == cmd.MAX_SETTLE_SECONDS
        assert cmd.bot.logger.warning.called

    def test_negative_is_clamped_to_zero(self):
        assert self._cmd(corescope_settle_seconds="-5").corescope_settle_seconds == 0.0

    def test_a_non_numeric_value_falls_back_to_the_default(self):
        cmd = self._cmd(corescope_settle_seconds="soon")
        assert cmd.corescope_settle_seconds == multitest_module.DEFAULT_SETTLE_SECONDS
        assert cmd.bot.logger.warning.called

    def test_variants_scale_the_wait(self):
        cmd = self._cmd(corescope_settle_seconds="30")
        assert cmd._settle_for_variant("") == 30.0
        assert cmd._settle_for_variant("long") == 60.0
        assert cmd._settle_for_variant("xlong") == 90.0

    def test_an_unknown_variant_does_not_scale(self):
        assert self._cmd(corescope_settle_seconds="30")._settle_for_variant("nonsense") == 30.0

    def test_a_scaled_wait_is_still_capped(self):
        cmd = self._cmd(corescope_settle_seconds="200")
        assert cmd._settle_for_variant("xlong") == cmd.MAX_SETTLE_SECONDS


class TestCorescopeAckAndSettle:
    """The acknowledgement, and waiting for observers before asking."""

    def _cmd(self, *, scope=None, **multitest):
        bot = _make_bot()
        bot.config.add_section("External_Data")
        bot.config.set("External_Data", "corescope_url", "https://a.example")
        bot.config.set("Multitest_Command", "corescope_enabled", "true")
        for key, value in multitest.items():
            bot.config.set("Multitest_Command", key, value)
        bot.message_handler.multitest_listener = None
        bot.rate_limiter.can_send.return_value = True
        bot.command_manager.split_text_into_utf8_chunks = Mock(
            side_effect=lambda text, budget: [text[:budget]]
        )
        cmd = MultitestCommand(bot)
        cmd._corescope = MagicMock()
        cmd._corescope.fetch = Mock(return_value=scope or ScopePacket(
            unique_paths=1, observations=1, observers=1, max_hops=2, paths=("a1b2,c3d4",),
        ))
        cmd.get_rf_data_for_message = Mock(return_value={
            "packet_hash": "3F2A9C1B7E4D0A65", "timestamp": 1000.0,
            "routing_info": {"path_nodes": ["a1b2", "c3d4"], "bytes_per_hop": 2},
        })
        cmd.send_response = AsyncMock(return_value=True)
        cmd.send_response_chunked = AsyncMock(return_value=True)
        cmd.get_max_message_length = Mock(return_value=200)
        return cmd

    def _message(self, content="mt"):
        return mock_message(
            content=content, sender_id="Alice", hops=2,
            routing_info={"path_nodes": ["a1b2", "c3d4"], "bytes_per_hop": 2,
                          "packet_hash": "3F2A9C1B7E4D0A65"},
        )

    async def test_acknowledges_then_waits_then_replies(self):
        cmd = self._cmd()
        order = []
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()) as slept:
            slept.side_effect = lambda *_a, **_k: order.append(
                ("slept", cmd.send_response.await_count)
            )
            await cmd.execute(self._message())
        assert order == [("slept", 1)], "the ack must go out before the wait"
        assert cmd.send_response.await_count == 2, "ack, then the result"

    async def test_the_ack_names_the_wait(self):
        cmd = self._cmd(corescope_settle_seconds="30")
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        assert cmd.send_response.await_args_list[0][0][1] == (
            "Checking CoreScope for all paths, reply in ~30s"
        )

    async def test_the_ack_names_the_variant_scaled_wait(self):
        cmd = self._cmd(corescope_settle_seconds="30")
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message("mt xlong"))
        assert "~90s" in cmd.send_response.await_args_list[0][0][1]

    async def test_the_ack_does_not_spend_the_users_rate_limit(self):
        cmd = self._cmd()
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        assert cmd.send_response.await_args_list[0][1]["skip_user_rate_limit"] is True

    async def test_a_blank_ack_is_not_sent(self):
        cmd = self._cmd(corescope_ack_message="")
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        assert cmd.corescope_ack_message == ""
        assert cmd.send_response.await_count == 1, "only the result"
        cmd._corescope.fetch.assert_called_once()

    async def test_a_configured_ack_takes_placeholders_and_filters(self):
        cmd = self._cmd(corescope_ack_message="@[{sender}] {wait_seconds}s, pkt {packet_hash}")
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        assert cmd.send_response.await_args_list[0][0][1] == (
            "@[Alice] 30s, pkt 3F2A9C1B7E4D0A65"
        )

    async def test_an_overlong_ack_is_truncated_not_chunked(self):
        cmd = self._cmd(corescope_ack_message="x" * 400)
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        assert len(cmd.send_response.await_args_list[0][0][1]) == 200
        cmd.send_response_chunked.assert_not_awaited()

    async def test_the_wait_runs_to_a_deadline_from_the_trigger(self):
        """A slow ack shortens the sleep instead of extending the total."""
        cmd = self._cmd(corescope_settle_seconds="30")
        clock = iter([100.0, 112.0])
        with patch.object(multitest_module.time, "monotonic", side_effect=lambda: next(clock, 112.0)):
            with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()) as slept:
                await cmd.execute(self._message())
        assert slept.await_args_list[0][0][0] == pytest.approx(18.0)

    async def test_an_ack_slower_than_the_settle_skips_the_wait(self):
        cmd = self._cmd(corescope_settle_seconds="30")
        clock = iter([100.0, 140.0])
        with patch.object(multitest_module.time, "monotonic", side_effect=lambda: next(clock, 140.0)):
            with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()) as slept:
                await cmd.execute(self._message())
        slept.assert_not_awaited()
        cmd._corescope.fetch.assert_called_once()

    async def test_a_zero_settle_asks_immediately(self):
        cmd = self._cmd(corescope_settle_seconds="0")
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()) as slept:
            await cmd.execute(self._message())
        slept.assert_not_awaited()
        cmd._corescope.fetch.assert_called_once()

    async def test_a_rejected_request_is_never_acknowledged(self):
        cmd = self._cmd()
        cmd.enforce_path_byte_requirement = AsyncMock(return_value=False)
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        cmd.send_response.assert_not_awaited()
        cmd._corescope.fetch.assert_not_called()

    async def test_a_second_request_during_the_settle_is_ignored(self):
        cmd = self._cmd(corescope_settle_seconds="30")
        cmd._active_sessions["Alice"] = multitest_module.MultitestSession(
            user_id="Alice", target_packet_hash="3F2A9C1B7E4D0A65",
            triggering_timestamp=time.time(), listening_start_time=time.time(),
            listening_duration=30.0, collected_paths=set(),
        )
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        cmd.send_response.assert_not_awaited()

    async def test_local_mode_sends_no_acknowledgement(self):
        bot = _make_bot()
        bot.message_handler.multitest_listener = None
        bot.rate_limiter.can_send.return_value = True
        cmd = MultitestCommand(bot)
        cmd.get_rf_data_for_message = Mock(return_value={
            "packet_hash": "3F2A9C1B7E4D0A65", "timestamp": 1000.0,
            "routing_info": {"path_nodes": ["a1b2", "c3d4"], "bytes_per_hop": 2},
        })
        cmd.send_response = AsyncMock(return_value=True)
        cmd.get_max_message_length = Mock(return_value=200)
        cmd._scan_recent_rf_data = Mock()
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        assert cmd.send_response.await_count == 1, "only the result"

    async def test_the_reply_reports_the_seconds_waited(self):
        cmd = self._cmd(
            corescope_settle_seconds="45",
            response_format="{path_count} in {listening_duration}s",
        )
        with patch.object(multitest_module.asyncio, "sleep", new=AsyncMock()):
            await cmd.execute(self._message())
        assert cmd.send_response.await_args[0][1] == "1 in 45s"
