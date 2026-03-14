#!/usr/bin/env python3
"""
Unit tests for WebViewerCommand.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from modules.commands.webviewer_command import WebViewerCommand
from modules.models import MeshMessage


def _make_message(content: str = "webviewer status") -> MeshMessage:
    return MeshMessage(content=content, sender_id="tester", sender_pubkey="aa" * 32, is_dm=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_webviewer_status_includes_topology_mode(command_mock_bot):
    command_mock_bot.config.add_section("Path_Command")
    command_mock_bot.config.set("Path_Command", "topology_engine_mode", "shadow")

    integration = Mock()
    integration.enabled = True
    integration.running = True
    integration.host = "127.0.0.1"
    integration.port = 8080
    integration.bot_integration = None
    command_mock_bot.web_viewer_integration = integration

    cmd = WebViewerCommand(command_mock_bot)
    cmd.send_response = AsyncMock(return_value=True)

    msg = _make_message("webviewer status")
    await cmd._handle_status(msg)

    assert cmd.send_response.called
    payload = cmd.send_response.call_args.args[1]
    assert "topology_mode: shadow" in payload
    assert "topology_validation_page_expected: True" in payload
