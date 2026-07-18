"""Tests for the time-sync admin command permissions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.commands.timesync_command import TimeSyncCommand
from tests.conftest import mock_message


ADMIN_KEY = "a" * 64


def _configure_admin_acl(bot, admin_commands: str) -> None:
    if not bot.config.has_section("Admin_ACL"):
        bot.config.add_section("Admin_ACL")
    bot.config.set("Admin_ACL", "admin_pubkeys", ADMIN_KEY)
    bot.config.set("Admin_ACL", "admin_commands", admin_commands)


def test_timesync_requires_admin_command_allowlist(command_mock_bot):
    _configure_admin_acl(command_mock_bot, "reload")
    command = TimeSyncCommand(command_mock_bot)
    message = mock_message(content="timesync status", is_dm=True, sender_pubkey=ADMIN_KEY)

    assert command.can_execute(message) is False


def test_timesync_allows_listed_admin(command_mock_bot):
    _configure_admin_acl(command_mock_bot, "reload,timesync")
    command = TimeSyncCommand(command_mock_bot)
    message = mock_message(content="timesync status", is_dm=True, sender_pubkey=ADMIN_KEY)

    assert command.can_execute(message) is True


@pytest.mark.asyncio
async def test_timesync_send_broadcasts_once(command_mock_bot):
    service = MagicMock()
    service.send_once = AsyncMock(return_value=True)
    command = TimeSyncCommand(command_mock_bot)
    message = mock_message(content="timesync send", is_dm=True, sender_pubkey=ADMIN_KEY)

    with patch.object(command, "_get_service", return_value=service):
        result = await command.execute(message)

    assert result is True
    service.send_once.assert_awaited_once_with()
    command_mock_bot.command_manager.send_response.assert_awaited_once()
    assert command_mock_bot.command_manager.send_response.await_args.args[1] == "Time sync broadcast sent."


@pytest.mark.asyncio
async def test_timesync_send_reports_false_result(command_mock_bot):
    service = MagicMock()
    service.send_once = AsyncMock(return_value=False)
    command = TimeSyncCommand(command_mock_bot)
    message = mock_message(content="timesync broadcast", is_dm=True, sender_pubkey=ADMIN_KEY)

    with patch.object(command, "_get_service", return_value=service):
        await command.execute(message)

    service.send_once.assert_awaited_once_with()
    response = command_mock_bot.command_manager.send_response.await_args.args[1]
    assert response == "Time sync broadcast failed; check logs."


@pytest.mark.asyncio
async def test_timesync_send_reports_service_exception(command_mock_bot):
    service = MagicMock()
    service.send_once = AsyncMock(side_effect=RuntimeError("signing unavailable"))
    command = TimeSyncCommand(command_mock_bot)
    message = mock_message(content="timesync now", is_dm=True, sender_pubkey=ADMIN_KEY)

    with patch.object(command, "_get_service", return_value=service):
        await command.execute(message)

    service.send_once.assert_awaited_once_with()
    response = command_mock_bot.command_manager.send_response.await_args.args[1]
    assert response == "Time sync broadcast failed: signing unavailable"
