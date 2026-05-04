#!/usr/bin/env python3
"""Unit tests for path-byte gating across test/multitest/path commands."""

import configparser
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.commands.multitest_command import MultitestCommand, MultitestSession
from modules.commands.path_command import PathCommand
from modules.commands.test_command import TestCommand as MeshTestCommand
from tests.conftest import mock_message


def _base_bot() -> MagicMock:
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = configparser.ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "bot_name", "TestBot")
    bot.config.add_section("Channels")
    bot.config.set("Channels", "monitor_channels", "general")
    bot.config.set("Channels", "respond_to_dms", "true")
    bot.config.add_section("Keywords")
    bot.config.set("Keywords", "test", "ack")
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    bot.prefix_hex_chars = 2
    return bot


@pytest.mark.asyncio
async def test_test_command_rejects_silently_when_path_bytes_too_short():
    bot = _base_bot()
    bot.config.add_section("Test_Command")
    bot.config.set("Test_Command", "enabled", "true")
    bot.config.set("Test_Command", "require_path_bytes_greater_or_equal_to", "2")
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "recency_weight", "0.2")

    cmd = MeshTestCommand(bot)
    cmd.handle_keyword_match = AsyncMock(return_value=True)
    message = mock_message(content="test", channel="general", routing_info={"path_byte_length": 1})

    result = await cmd.execute(message)

    assert result is True
    cmd.handle_keyword_match.assert_not_called()
    bot.command_manager.send_response.assert_not_called()


@pytest.mark.asyncio
async def test_test_command_sends_failure_response_when_configured():
    bot = _base_bot()
    bot.config.add_section("Test_Command")
    bot.config.set("Test_Command", "enabled", "true")
    bot.config.set("Test_Command", "require_path_bytes_greater_or_equal_to", "3")
    bot.config.set("Test_Command", "require_path_bytes_failure_response", "Need 3-byte path")
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "recency_weight", "0.2")

    cmd = MeshTestCommand(bot)
    cmd.handle_keyword_match = AsyncMock(return_value=True)
    message = mock_message(content="test", channel="general", routing_info={"path_byte_length": 2})

    result = await cmd.execute(message)

    assert result is True
    cmd.handle_keyword_match.assert_not_called()
    bot.command_manager.send_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_path_command_mode_one_behaves_like_zero():
    bot = _base_bot()
    bot.config.add_section("Path_Command")
    bot.config.set("Path_Command", "enabled", "true")
    bot.config.set("Path_Command", "require_path_bytes_greater_or_equal_to", "1")

    cmd = PathCommand(bot)
    cmd._send_path_response = AsyncMock(return_value=True)
    cmd._extract_path_from_recent_messages = AsyncMock(return_value="decoded")
    message = mock_message(content="path", channel="general", routing_info={"path_byte_length": 0})

    result = await cmd.execute(message)

    assert result is True
    cmd._extract_path_from_recent_messages.assert_awaited_once()
    cmd._send_path_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_multitest_execute_rejects_with_custom_failure_message():
    bot = _base_bot()
    bot.config.add_section("Multitest_Command")
    bot.config.set("Multitest_Command", "enabled", "true")
    bot.config.set("Multitest_Command", "require_path_bytes_greater_or_equal_to", "2")
    bot.config.set("Multitest_Command", "require_path_bytes_failure_response", "Need >=2 path bytes")

    cmd = MultitestCommand(bot)
    message = mock_message(content="multitest", channel="general", routing_info={"path_byte_length": 1})

    result = await cmd.execute(message)

    assert result is True
    bot.command_manager.send_response.assert_awaited_once()


def test_multitest_listener_filters_matching_hash_by_required_path_bytes():
    bot = _base_bot()
    bot.config.add_section("Multitest_Command")
    bot.config.set("Multitest_Command", "enabled", "true")
    cmd = MultitestCommand(bot)
    cmd.extract_path_from_rf_data = Mock(return_value="0101")
    cmd.get_rf_data_for_message = Mock(
        return_value={
            "packet_hash": "abc123",
            "routing_info": {"path_byte_length": 2, "path_nodes": ["0101"]},
        }
    )

    session = MultitestSession(
        user_id="alice",
        target_packet_hash="abc123",
        triggering_timestamp=0.0,
        listening_start_time=0.0,
        listening_duration=9999.0,
        collected_paths=set(),
        required_path_bytes_mode=3,
    )
    cmd._active_sessions["alice"] = session
    message = mock_message(content="x", sender_id="alice")

    cmd.on_message_received(message)

    assert session.collected_paths == set()
