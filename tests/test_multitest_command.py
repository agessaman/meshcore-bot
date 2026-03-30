"""Tests for modules.commands.multitest_command — pure logic functions."""

import configparser
from unittest.mock import MagicMock, Mock

from modules.commands.multitest_command import (
    MultitestCommand,
    _condense_path_lines,
    _path_to_tokens,
)
from tests.conftest import mock_message

_INTER = "\u251c"
_LAST = "\u2514"
_INDENT = "\u3000"  # 　 before nested ├/└
_CHILD_INTER = f"{_INDENT}{_INTER} "
_CHILD_LAST = f"{_INDENT}{_LAST} "
_CORNER = "\u2510"  # ┐ after common path


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
        paths = sorted(
            [
                "e6,0c,85,82,28,1a,cd,7e,01",
                "e6,0c,85,82,28,1a,cd,7e,7a",
                "e6,0c,85,82,28,1a,cd,7e,7a,09",
                "e6,0c,85,82,28,1a,cd",
            ]
        )
        out = _condense_path_lines(paths)
        expected = "\n".join(
            [
                f"e6,0c,85,82,28,1a,cd,7e {_CORNER}",
                f"{_INTER} 01",
                f"{_INTER} 7a",
                f"{_CHILD_INTER}09",
                f"{_LAST} ...",
            ]
        )
        assert out == expected

    def test_shared_prefix_no_strict_prefix_truncation(self):
        paths = sorted(
            [
                "aa,bb,cc",
                "aa,bb,cc,dd",
                "aa,bb,cc,ee",
            ]
        )
        out = _condense_path_lines(paths)
        # LCP shrinks so cc is not the whole “trunk” while dd/ee branch off
        expected = "\n".join(
            [
                f"aa,bb {_CORNER}",
                f"{_INTER} cc",
                f"{_CHILD_INTER}dd",
                f"{_CHILD_LAST}ee",
            ]
        )
        assert out == expected

    def test_one_path_ends_at_lcp_other_extends(self):
        """Shorter LCP so both 0101 and 0101,0970 appear as branches, not one hidden on the trunk."""
        paths = sorted(["cdf1,7e76,0101", "cdf1,7e76,0101,0970"])
        out = _condense_path_lines(paths)
        expected = "\n".join(
            [
                f"cdf1,7e76 {_CORNER}",
                f"{_INTER} 0101",
                f"{_CHILD_LAST}0970",
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
        out = _condense_path_lines(paths)
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
        out = _condense_path_lines(paths)
        expected = "\n".join(
            [
                f"41,96 {_CORNER}",
                f"{_INTER} 13,01",
                f"{_CHILD_INTER}1e",
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
        out = _condense_path_lines(paths)
        expected = "\n".join(
            [
                f"cc,fe,17,b3,7e {_CORNER}",
                f"{_INTER} 96,e0",
                f"{_CHILD_INTER}01",
                f"{_CHILD_INTER}09",
                f"{_CHILD_INTER}1e",
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
        out = _condense_path_lines(paths)
        expected = "\n".join(
            [
                f"e2,ab,1f,ef,55,21 {_CORNER}",
                f"{_INTER} 01",
                f"{_CHILD_INTER}1e",
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
        out = _condense_path_lines(paths)
        expected = "\n".join(
            [
                f"d38a05,c4a86a,067b75,cafee0,1ffbd6,e8154b {_CORNER}",
                f"{_INTER} 860cca",
                f"{_CHILD_LAST}010101",
            ]
        )
        assert out == expected

    def test_shared_hop_then_horiz_continuations(self):
        """All paths share first hop after LCP → one ├ hop line then 　├/　└ remainders (U+3000)."""
        paths = sorted(
            [
                "d38a05,479198,a837bc,7e7662,e0eed9",
                "d38a05,479198,a837bc,7e7662,e0eed9,010101",
                "d38a05,479198,a837bc,7e7662,e0eed9,0970d6",
                "d38a05,479198,a837bc,7e7662,e0eed9,1ed612",
                "d38a05,479198,a837bc,7e7662,e0eed9,f",
            ]
        )
        out = _condense_path_lines(paths)
        expected = "\n".join(
            [
                f"d38a05,479198,a837bc,7e7662 {_CORNER}",
                f"{_INTER} e0eed9",
                f"{_CHILD_INTER}010101",
                f"{_CHILD_INTER}0970d6",
                f"{_CHILD_INTER}1ed612",
                f"{_CHILD_LAST}f",
            ]
        )
        assert out == expected

    def test_disjoint_first_hop_groups_with_brackets(self):
        paths = sorted(["a,b", "c,d"])
        out = _condense_path_lines(paths)
        expected = "\n".join(["[a,b]", "[c,d]"])
        assert out == expected

    def test_single_path_unchanged(self):
        assert _condense_path_lines(["a,b,c"]) == "a,b,c"

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

    def test_condense_paths_default_false(self):
        cmd = MultitestCommand(_make_bot())
        assert cmd.condense_paths is False

    def test_condense_paths_true_from_config(self):
        bot = _make_bot()
        bot.config.set("Multitest_Command", "condense_paths", "true")
        cmd = MultitestCommand(bot)
        assert cmd.condense_paths is True
