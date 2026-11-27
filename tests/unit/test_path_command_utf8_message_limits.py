#!/usr/bin/env python3
"""Unit tests for PathCommand UTF-8 byte truncation and multi-message splitting (PR #128)."""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.commands.path_command import PathCommand
from modules.models import MeshMessage


@pytest.mark.unit
class TestPathCommandTruncateToByteLength:
    """Tests for _truncate_to_byte_length (no split within code points)."""

    @pytest.fixture
    def path_cmd(self, mock_bot):
        return PathCommand(mock_bot)

    def test_short_string_unchanged(self, path_cmd):
        assert path_cmd._truncate_to_byte_length("hello", 20) == "hello"

    def test_truncates_on_utf8_bytes_not_chars(self, path_cmd):
        """Budget is UTF-8 bytes; result must respect max_bytes including ellipsis."""
        ellipsis = "..."
        out = path_cmd._truncate_to_byte_length("😀😀", 7, ellipsis)
        assert out.endswith(ellipsis)
        assert len(out.encode("utf-8")) <= 7

    def test_does_not_emit_lone_surrogate_fragment(self, path_cmd):
        """Truncated bytes decode with errors='ignore' — result must be valid UTF-8."""
        out = path_cmd._truncate_to_byte_length("éééé", 5, "...")
        assert out.encode("utf-8") == out.encode("utf-8")  # round-trip
        out.encode("utf-8").decode("utf-8")  # no exception


@pytest.mark.unit
class TestPathCommandFormatPathResponseByteCap:
    """_format_path_response applies per-line UTF-8 byte cap (150)."""

    @pytest.fixture
    def path_cmd(self, mock_bot):
        cmd = PathCommand(mock_bot)
        cmd.translate = MockTranslate()
        return cmd

    def test_unknown_line_with_emoji_truncated_to_byte_budget(self, path_cmd):
        """Line may be few characters but many bytes; must fit 150-byte line cap."""
        node_ids = ["AB"]
        repeater_info = {"AB": {"found": False}}
        raw = path_cmd._format_path_response(node_ids, repeater_info)
        assert len(raw.encode("utf-8")) <= 150
        assert "AB" in raw


class MockTranslate:
    """Minimal translate: long unknown line to exercise 150-byte line cap."""

    def __call__(self, key: str, **kwargs):
        if key == "commands.path.node_unknown":
            node_id = kwargs.get("node_id", "")
            return f"unknown {node_id}" + "😀" * 50
        if key == "commands.path.truncation":
            return "..."
        return key


@pytest.mark.unit
class TestPathCommandSendPathResponseByteSplitting:
    """_send_path_response splits on UTF-8 byte length, not character count."""

    @pytest.fixture
    def path_cmd(self, mock_bot):
        cmd = PathCommand(mock_bot)
        cmd.translate = MockTranslateForSend()
        cmd.send_response = AsyncMock(return_value=True)
        return cmd

    @pytest.mark.asyncio
    async def test_splits_when_combined_lines_exceed_byte_budget(self, path_cmd):
        """Two lines: 12 + newline + 13 = 26 UTF-8 bytes > budget 25 -> second send."""
        path_cmd.get_max_message_length = lambda _msg: 25
        msg = MeshMessage(content="path", channel="general", is_dm=False)
        response = "a" * 12 + "\n" + "b" * 13
        with patch("modules.commands.path_command.asyncio.sleep", new_callable=AsyncMock):
            await path_cmd._send_path_response(msg, response)
        assert path_cmd.send_response.await_count >= 2

    @pytest.mark.asyncio
    async def test_continuation_marker_counts_against_budget(self, path_cmd):
        """The end marker must fit in its chunk, or the send layer re-splits it.

        Seven repeater lines behind a mention prefix: the first five lines plus
        prefix filled the 142-byte channel budget exactly, and the appended "\\n..."
        tipped it over, so one path reply went out as three RF messages.
        """
        path_cmd.translate = lambda key, **kw: {
            "commands.path.continuation_end": "\n...",
            "commands.path.continuation_start": f"...\n{kw.get('line', '')}",
        }.get(key, key)
        path_cmd.path_reply_prefix = "@[Nutcrack-TD+]"
        budget = 142
        path_cmd.get_max_message_length = lambda _msg: budget
        msg = MeshMessage(content="p", channel="general", is_dm=False, sender_id="Nutcrack-TD+")
        response = "\n".join([
            "757575: Powerline Trail",
            "DF2B0C: Anthology_Apts",
            "E03DA8: WYR ISS Newport way",
            "B1FA1E: PicoW Repeater",
            "9FDC10: River City Repeater",
            "7E7662: WW7STR/PugetMesh Cougar",
            "9E6EC8: N7JMV-EVERETT-RP",
        ])
        with patch("modules.commands.path_command.asyncio.sleep", new_callable=AsyncMock):
            await path_cmd._send_path_response(msg, response)
        sent = [c[0][1] for c in path_cmd.send_response.call_args_list]
        assert len(sent) == 2
        assert all(len(s.encode("utf-8")) <= budget for s in sent)
        assert sent[0].startswith("@[Nutcrack-TD+]\n") and sent[0].endswith("\n...")
        assert sent[1].startswith("...\n")

    @pytest.mark.asyncio
    async def test_single_send_when_under_byte_budget(self, path_cmd):
        path_cmd.get_max_message_length = lambda _msg: 100
        msg = MeshMessage(content="path", channel="general", is_dm=False)
        response = "short"
        with patch("modules.commands.path_command.asyncio.sleep", new_callable=AsyncMock):
            await path_cmd._send_path_response(msg, response)
        path_cmd.send_response.assert_awaited_once()


@pytest.mark.unit
class TestPathCommandReplyPrefix:
    """reply_prefix prepended to first RF payload only; last_response includes prefix."""

    @pytest.fixture
    def path_cmd(self, mock_bot):
        mock_bot.translator = MagicMock()
        mock_bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
        cmd = PathCommand(mock_bot)
        cmd.send_response = AsyncMock(return_value=True)
        return cmd

    @pytest.mark.asyncio
    async def test_prepends_prefix_to_single_send(self, path_cmd, mock_bot):
        path_cmd.path_reply_prefix = "[{sender}]"
        path_cmd.get_max_message_length = lambda _msg: 200
        msg = MeshMessage(content="path", channel="general", is_dm=False, sender_id="alice")
        await path_cmd._send_path_response(msg, "line1")
        path_cmd.send_response.assert_awaited_once()
        payload = path_cmd.send_response.call_args[0][1]
        assert payload == "[alice]\nline1"
        assert path_cmd.last_response == "[alice]\nline1"

    @pytest.mark.asyncio
    async def test_prefix_supports_hops_and_enhanced_path(self, path_cmd, mock_bot):
        path_cmd.path_reply_prefix = "[{sender}] {path} ({hops_label})"
        path_cmd.get_max_message_length = lambda _msg: 200
        msg = MeshMessage(
            content="path",
            channel="general",
            is_dm=False,
            sender_id="alice",
            hops=2,
            path="raw fallback",
            routing_info={"path_length": 2, "path_nodes": ["01", "5f"]},
        )

        await path_cmd._send_path_response(msg, "line1")

        path_cmd.send_response.assert_awaited_once()
        payload = path_cmd.send_response.call_args[0][1]
        assert payload == "[alice] 01,5f (2 hops) (2 hops)\nline1"
        assert path_cmd.last_response == "[alice] 01,5f (2 hops) (2 hops)\nline1"

    @pytest.mark.asyncio
    async def test_prefix_only_on_first_split_message(self, path_cmd, mock_bot):
        path_cmd.path_reply_prefix = "P:"
        path_cmd.get_max_message_length = lambda _msg: 25
        path_cmd.translate = MockTranslateForSend()
        msg = MeshMessage(content="path", channel="general", is_dm=False)
        response = "a" * 12 + "\n" + "b" * 13
        with patch("modules.commands.path_command.asyncio.sleep", new_callable=AsyncMock):
            await path_cmd._send_path_response(msg, response)
        assert path_cmd.send_response.await_count >= 2
        first = path_cmd.send_response.call_args_list[0][0][1]
        assert first.startswith("P:\n")
        second = path_cmd.send_response.call_args_list[1][0][1]
        assert not second.startswith("P:\n")


class MockTranslateForSend:
    def __call__(self, key: str, **kwargs):
        if key == "commands.path.continuation_end":
            return "\n>>"
        if key == "commands.path.continuation_start":
            return f"<< {kwargs.get('line', '')}"
        return key
