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
                "e6,0c,85,82,28,1a,cd,7e",
                f"{_INTER} 01",
                f"{_INTER} 7a",
                f"{_INTER} 7a,09",
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
        expected = "\n".join(
            [
                "aa,bb,cc",
                f"{_INTER} dd",
                f"{_LAST} ee",
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
