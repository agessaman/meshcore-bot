"""Tests for modules.commands.trace_command — pure logic functions."""

import configparser
from unittest.mock import MagicMock, Mock

from modules.commands.trace_command import TraceCommand
from tests.conftest import mock_message


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
    config.add_section("Trace_Command")
    config.set("Trace_Command", "enabled", "true")
    config.set("Trace_Command", "maximum_hops", "5")
    config.set("Trace_Command", "trace_mode", "one_byte")
    config.set("Trace_Command", "timeout_per_hop_seconds", "1.5")
    config.set("Trace_Command", "output_format", "inline")
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    return bot


class TestExtractPathFromMessage:
    """Tests for _extract_path_from_message."""

    def setup_method(self):
        self.cmd = TraceCommand(_make_bot())

    def test_no_path_returns_empty(self):
        msg = mock_message(content="trace", path=None)
        result = self.cmd._extract_path_from_message(msg)
        assert result == []

    def test_direct_message_returns_empty(self):
        msg = mock_message(content="trace", path="Direct")
        result = self.cmd._extract_path_from_message(msg)
        assert result == []

    def test_zero_hops_returns_empty(self):
        msg = mock_message(content="trace", path="0 hops")
        result = self.cmd._extract_path_from_message(msg)
        assert result == []

    def test_single_hop_path(self):
        msg = mock_message(content="trace", path="7a")
        result = self.cmd._extract_path_from_message(msg)
        assert result == ["7a"]

    def test_multi_hop_path(self):
        msg = mock_message(content="trace", path="01,7a,55")
        result = self.cmd._extract_path_from_message(msg)
        assert result == ["01", "7a", "55"]

    def test_path_with_route_type_stripped(self):
        msg = mock_message(content="trace", path="01,7a via ROUTE_TYPE_MESHCORE")
        result = self.cmd._extract_path_from_message(msg)
        assert "01" in result

    def test_path_with_parenthesis_stripped(self):
        msg = mock_message(content="trace", path="7a (1 hop)")
        result = self.cmd._extract_path_from_message(msg)
        assert result == ["7a"]

    def test_invalid_hex_ignored(self):
        msg = mock_message(content="trace", path="01,zz,55")
        result = self.cmd._extract_path_from_message(msg)
        # zz is invalid hex, should be excluded
        assert "zz" not in result


class TestParsePathArg:
    """Tests for _parse_path_arg."""

    def setup_method(self):
        self.cmd = TraceCommand(_make_bot())

    def test_no_path_arg_returns_none(self):
        result = self.cmd._parse_path_arg("trace")
        assert result is None

    def test_comma_separated_path(self):
        result = self.cmd._parse_path_arg("trace 01,7a,55")
        assert result == ["01", "7a", "55"]

    def test_contiguous_hex_path(self):
        result = self.cmd._parse_path_arg("trace 017a55")
        assert result == ["01", "7a", "55"]

    def test_invalid_hex_returns_none(self):
        result = self.cmd._parse_path_arg("trace 01,zz")
        assert result is None

    def test_odd_length_hex_returns_none(self):
        result = self.cmd._parse_path_arg("trace 017")
        assert result is None

    def test_tracer_prefix(self):
        result = self.cmd._parse_path_arg("tracer 01,7a")
        assert result == ["01", "7a"]


class TestFormatTraceInline:
    """Tests for _format_trace_inline."""

    def setup_method(self):
        self.cmd = TraceCommand(_make_bot())

    def test_basic_inline_format(self):
        from modules.trace_runner import RunTraceResult
        result = RunTraceResult(
            success=True,
            tag=0,
            path_nodes=[{"hash": "7a", "snr": -12.5}],
        )
        sender_str = "@[User] "
        output = self.cmd._format_trace_inline(sender_str, result)
        assert "[Bot]" in output
        assert "7a" in output
        assert "-12.5" in output

    def test_inline_format_no_snr(self):
        from modules.trace_runner import RunTraceResult
        result = RunTraceResult(
            success=True,
            tag=0,
            path_nodes=[{"hash": "7a", "snr": None}],
        )
        output = self.cmd._format_trace_inline("@[User] ", result)
        assert "7a" in output


class TestFormatTraceVertical:
    """Tests for _format_trace_vertical."""

    def setup_method(self):
        self.cmd = TraceCommand(_make_bot())

    def test_vertical_format_basic(self):
        from modules.trace_runner import RunTraceResult
        result = RunTraceResult(
            success=True,
            tag=0,
            path_nodes=[
                {"hash": "7a", "snr": -12.5},
                {"hash": "55", "snr": -8.0},
            ],
        )
        output = self.cmd._format_trace_vertical("@[User] ", result)
        assert "Trace:" in output
        assert "\n" in output  # Multiple lines

    def test_vertical_format_single_node(self):
        from modules.trace_runner import RunTraceResult
        result = RunTraceResult(
            success=True,
            tag=0,
            path_nodes=[{"hash": "7a", "snr": -5.0}],
        )
        output = self.cmd._format_trace_vertical("@[User] ", result)
        assert "[Bot]" in output


class TestBuildReciprocalPath:
    """Tests for _build_reciprocal_path."""

    def setup_method(self):
        self.cmd = TraceCommand(_make_bot())

    def test_empty_list_unchanged(self):
        assert self.cmd._build_reciprocal_path([]) == []

    def test_single_node_unchanged(self):
        assert self.cmd._build_reciprocal_path(["01"]) == ["01"]

    def test_two_node_reciprocal(self):
        result = self.cmd._build_reciprocal_path(["01", "7a"])
        assert result == ["01", "7a", "01"]

    def test_three_node_reciprocal(self):
        result = self.cmd._build_reciprocal_path(["01", "7a", "55"])
        assert result == ["01", "7a", "55", "7a", "01"]


class TestMatchesKeyword:
    """Tests for matches_keyword."""

    def setup_method(self):
        self.cmd = TraceCommand(_make_bot())

    def test_trace_matches(self):
        assert self.cmd.matches_keyword(mock_message(content="trace")) is True

    def test_tracer_matches(self):
        assert self.cmd.matches_keyword(mock_message(content="tracer")) is True

    def test_trace_with_path_matches(self):
        assert self.cmd.matches_keyword(mock_message(content="trace 01,7a")) is True

    def test_other_does_not_match(self):
        assert self.cmd.matches_keyword(mock_message(content="ping")) is False

    def test_bang_prefix_trace_matches(self):
        """!trace should be recognized."""
        assert self.cmd.matches_keyword(mock_message(content="!trace")) is True

    def test_bang_prefix_tracer_matches(self):
        assert self.cmd.matches_keyword(mock_message(content="!tracer 01,7a")) is True


class TestCanExecuteTrace:
    """Tests for can_execute."""

    def test_enabled_returns_true(self):
        bot = _make_bot()
        cmd = TraceCommand(bot)
        msg = mock_message(content="trace", channel="general")
        assert cmd.can_execute(msg) is True

    def test_disabled_returns_false(self):
        bot = _make_bot()
        bot.config.set("Trace_Command", "enabled", "false")
        cmd = TraceCommand(bot)
        msg = mock_message(content="trace", channel="general")
        assert cmd.can_execute(msg) is False


class TestGetHelpTextTrace:
    def test_returns_string(self):
        cmd = TraceCommand(_make_bot())
        result = cmd.get_help_text()
        assert isinstance(result, str)
        assert "trace" in result.lower()


class TestExtractPathEdgeCases:
    def setup_method(self):
        self.cmd = TraceCommand(_make_bot())

    def test_single_node_invalid_length(self):
        """3-char path segment is not valid 2-char hex → returns []."""
        msg = mock_message(content="trace", path="abc")
        result = self.cmd._extract_path_from_message(msg)
        assert result == []

    def test_single_node_non_hex(self):
        """2-char non-hex → returns []."""
        msg = mock_message(content="trace", path="zz")
        result = self.cmd._extract_path_from_message(msg)
        assert result == []


class TestParseBangPrefix:
    def test_bang_prefix_stripped(self):
        cmd = TraceCommand(_make_bot())
        result = cmd._parse_path_arg("!trace 01,7a")
        assert result == ["01", "7a"]


class TestFormatTraceResult:
    """Tests for _format_trace_result."""

    def setup_method(self):
        self.cmd = TraceCommand(_make_bot())

    def test_failed_result_shows_error(self):
        from modules.trace_runner import RunTraceResult
        result = RunTraceResult(success=False, tag=0, path_nodes=[], error_message="timeout")
        output = self.cmd._format_trace_result(mock_message(content="trace"), result)
        assert "failed" in output.lower() or "timeout" in output

    def test_success_inline(self):
        from modules.trace_runner import RunTraceResult
        result = RunTraceResult(success=True, tag=0, path_nodes=[{"hash": "7a", "snr": -5.0}])
        self.cmd.output_format = "inline"
        output = self.cmd._format_trace_result(mock_message(content="trace", sender_id="Alice"), result)
        assert isinstance(output, str)
        assert "7a" in output

    def test_success_vertical(self):
        from modules.trace_runner import RunTraceResult
        result = RunTraceResult(success=True, tag=0, path_nodes=[{"hash": "7a", "snr": -5.0}])
        self.cmd.output_format = "vertical"
        output = self.cmd._format_trace_result(mock_message(content="trace", sender_id="Alice"), result)
        assert "Trace:" in output


class TestFormatTraceVerticalThreeNodes:
    """Tests for _format_trace_vertical with multiple nodes (middle hop)."""

    def setup_method(self):
        self.cmd = TraceCommand(_make_bot())

    def test_three_nodes_has_middle_hop(self):
        from modules.trace_runner import RunTraceResult
        result = RunTraceResult(
            success=True,
            tag=0,
            path_nodes=[
                {"hash": "aa", "snr": -10.0},
                {"hash": "bb", "snr": -8.0},
                {"hash": "cc", "snr": -12.0},
            ],
        )
        output = self.cmd._format_trace_vertical("@[User] ", result)
        # aa and bb appear as from_labels for subsequent hops
        assert "aa" in output
        assert "bb" in output
        # cc is the last node's hash — it's never used as a from_label
        # and doesn't appear in the output
        lines = output.split("\n")
        assert len(lines) >= 4  # Header + 3 hops

    def test_three_nodes_no_snr(self):
        from modules.trace_runner import RunTraceResult
        result = RunTraceResult(
            success=True,
            tag=0,
            path_nodes=[
                {"hash": "aa", "snr": None},
                {"hash": "bb", "snr": None},
                {"hash": "cc", "snr": None},
            ],
        )
        output = self.cmd._format_trace_vertical("@[User] ", result)
        assert "—" in output  # Unknown SNR marker


class TestTraceExecute:
    """Tests for execute() reachable paths (no real radio)."""

    def test_execute_no_path_sends_error(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot()
        cmd = TraceCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        # Message with no path and no path arg
        msg = mock_message(content="trace", path=None)
        result = asyncio.run(cmd.execute(msg))
        assert result is True
        call_text = cmd.send_response.call_args[0][1]
        assert "path" in call_text.lower()

    def test_execute_not_connected(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot()
        bot.connected = False
        bot.meshcore = None
        cmd = TraceCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="trace 01,7a")
        result = asyncio.run(cmd.execute(msg))
        assert result is True
        cmd.send_response.assert_called_once()

    def test_execute_no_meshcore_commands(self):
        import asyncio
        from unittest.mock import AsyncMock
        bot = _make_bot()
        bot.connected = True
        bot.meshcore = MagicMock()
        bot.meshcore.commands = None
        cmd = TraceCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="trace 01,7a")
        result = asyncio.run(cmd.execute(msg))
        assert result is True
