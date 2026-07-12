"""Tests for the time-sync admin command permissions."""

from __future__ import annotations

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
