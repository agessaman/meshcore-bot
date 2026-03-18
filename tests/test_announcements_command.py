"""Tests for modules.commands.announcements_command — pure logic functions."""

import configparser
import time
from unittest.mock import MagicMock, Mock

import pytest

from modules.commands.announcements_command import AnnouncementsCommand
from tests.conftest import mock_message

# A valid-looking 64-char hex pubkey
VALID_PUBKEY = "a" * 64
VALID_PUBKEY2 = "b" * 64


def _make_bot(enabled=True, acl_keys=None, admin_keys=None, triggers=None):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.add_section("Announcements_Command")
    config.set("Announcements_Command", "enabled", str(enabled).lower())
    config.set("Announcements_Command", "default_announcement_channel", "Public")
    config.set("Announcements_Command", "announcement_cooldown", "60")

    if acl_keys:
        config.set("Announcements_Command", "announcements_acl", ",".join(acl_keys))
    if triggers:
        for name, text in triggers.items():
            config.set("Announcements_Command", f"announce.{name}", text)

    if admin_keys:
        config.add_section("Admin_ACL")
        config.set("Admin_ACL", "admin_pubkeys", ",".join(admin_keys))

    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    return bot


class TestLoadTriggers:
    """Tests for _load_triggers."""

    def test_no_triggers_returns_empty(self):
        bot = _make_bot()
        cmd = AnnouncementsCommand(bot)
        assert cmd.triggers == {}

    def test_triggers_loaded_from_config(self):
        bot = _make_bot(triggers={"welcome": "Welcome message!", "bye": "Goodbye!"})
        cmd = AnnouncementsCommand(bot)
        assert "welcome" in cmd.triggers
        assert cmd.triggers["welcome"] == "Welcome message!"
        assert "bye" in cmd.triggers

    def test_only_announce_keys_loaded(self):
        bot = _make_bot(triggers={"hello": "Hello message"})
        # Other keys in the section shouldn't be loaded as triggers
        cmd = AnnouncementsCommand(bot)
        assert "hello" in cmd.triggers
        assert "enabled" not in cmd.triggers


class TestCheckAnnouncementsAccess:
    """Tests for _check_announcements_access."""

    def test_no_acl_returns_false(self):
        bot = _make_bot(enabled=True)
        cmd = AnnouncementsCommand(bot)
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey=VALID_PUBKEY)
        assert cmd._check_announcements_access(msg) is False

    def test_valid_pubkey_in_acl_returns_true(self):
        bot = _make_bot(acl_keys=[VALID_PUBKEY])
        cmd = AnnouncementsCommand(bot)
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey=VALID_PUBKEY)
        assert cmd._check_announcements_access(msg) is True

    def test_wrong_pubkey_returns_false(self):
        bot = _make_bot(acl_keys=[VALID_PUBKEY])
        cmd = AnnouncementsCommand(bot)
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey=VALID_PUBKEY2)
        assert cmd._check_announcements_access(msg) is False

    def test_no_pubkey_returns_false(self):
        bot = _make_bot(acl_keys=[VALID_PUBKEY])
        cmd = AnnouncementsCommand(bot)
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey=None)
        assert cmd._check_announcements_access(msg) is False

    def test_admin_acl_inherited(self):
        bot = _make_bot(admin_keys=[VALID_PUBKEY])
        cmd = AnnouncementsCommand(bot)
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey=VALID_PUBKEY)
        assert cmd._check_announcements_access(msg) is True

    def test_invalid_pubkey_format_returns_false(self):
        bot = _make_bot(acl_keys=[VALID_PUBKEY])
        cmd = AnnouncementsCommand(bot)
        # Pubkey too short
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey="tooshort")
        assert cmd._check_announcements_access(msg) is False

    def test_case_insensitive_pubkey_match(self):
        # ACL stored as lowercase, sender sends uppercase
        bot = _make_bot(acl_keys=[VALID_PUBKEY.lower()])
        cmd = AnnouncementsCommand(bot)
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey=VALID_PUBKEY.upper())
        # Should match case-insensitively
        assert cmd._check_announcements_access(msg) is True


class TestCanExecute:
    """Tests for can_execute."""

    def test_not_dm_returns_false(self):
        bot = _make_bot(acl_keys=[VALID_PUBKEY])
        cmd = AnnouncementsCommand(bot)
        msg = mock_message(content="announce welcome", channel="general", is_dm=False, sender_pubkey=VALID_PUBKEY)
        assert cmd.can_execute(msg) is False

    def test_disabled_returns_false(self):
        bot = _make_bot(enabled=False, acl_keys=[VALID_PUBKEY])
        cmd = AnnouncementsCommand(bot)
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey=VALID_PUBKEY)
        assert cmd.can_execute(msg) is False

    def test_dm_with_valid_pubkey_returns_true(self):
        bot = _make_bot(acl_keys=[VALID_PUBKEY])
        cmd = AnnouncementsCommand(bot)
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey=VALID_PUBKEY)
        assert cmd.can_execute(msg) is True


class TestCooldownLogic:
    """Tests for cooldown tracking."""

    def test_no_cooldown_when_trigger_fresh(self):
        bot = _make_bot(triggers={"hello": "Hello!"})
        cmd = AnnouncementsCommand(bot)
        remaining = cmd._get_trigger_cooldown_remaining("hello")
        assert remaining == 0

    def test_cooldown_active_after_execution(self):
        bot = _make_bot(triggers={"hello": "Hello!"})
        cmd = AnnouncementsCommand(bot)
        cmd.trigger_cooldowns["hello"] = time.time()
        remaining = cmd._get_trigger_cooldown_remaining("hello")
        assert remaining > 0

    def test_no_cooldown_when_cooldown_seconds_zero(self):
        bot = _make_bot()
        bot.config.set("Announcements_Command", "announcement_cooldown", "0")
        cmd = AnnouncementsCommand(bot)
        cmd.trigger_cooldowns["hello"] = time.time()
        remaining = cmd._get_trigger_cooldown_remaining("hello")
        assert remaining == 0


class TestParseCommand:
    """Tests for _parse_command."""

    def test_no_args_returns_none(self):
        bot = _make_bot()
        cmd = AnnouncementsCommand(bot)
        assert cmd._parse_command("announce") == (None, None, False)

    def test_trigger_only(self):
        bot = _make_bot()
        cmd = AnnouncementsCommand(bot)
        name, chan, override = cmd._parse_command("announce welcome")
        assert name == "welcome"
        assert chan is None
        assert override is False

    def test_trigger_with_channel(self):
        bot = _make_bot()
        cmd = AnnouncementsCommand(bot)
        name, chan, override = cmd._parse_command("announce welcome Public")
        assert name == "welcome"
        assert chan == "Public"

    def test_trigger_with_override(self):
        bot = _make_bot()
        cmd = AnnouncementsCommand(bot)
        name, chan, override = cmd._parse_command("announce welcome override")
        assert override is True

    def test_trigger_channel_override(self):
        bot = _make_bot()
        cmd = AnnouncementsCommand(bot)
        name, chan, override = cmd._parse_command("announce hello Public override")
        assert name == "hello"
        assert chan == "Public"
        assert override is True


class TestRecordTrigger:
    """Tests for _record_trigger_execution and _is_trigger_locked."""

    def test_record_sets_cooldown(self):
        bot = _make_bot()
        cmd = AnnouncementsCommand(bot)
        cmd._record_trigger_execution("hello")
        assert "hello" in cmd.trigger_cooldowns

    def test_fresh_trigger_not_locked(self):
        bot = _make_bot()
        cmd = AnnouncementsCommand(bot)
        assert cmd._is_trigger_locked("nonexistent") is False

    def test_just_sent_trigger_is_locked(self):
        bot = _make_bot()
        cmd = AnnouncementsCommand(bot)
        cmd._record_trigger_execution("hello")
        assert cmd._is_trigger_locked("hello") is True

    def test_old_trigger_not_locked(self):
        bot = _make_bot()
        cmd = AnnouncementsCommand(bot)
        cmd.trigger_lockouts["hello"] = time.time() - 120  # 2 min ago
        assert cmd._is_trigger_locked("hello") is False


class TestExecute:
    """Tests for execute()."""

    def test_no_trigger_with_configured_triggers_shows_list(self):
        from unittest.mock import AsyncMock
        bot = _make_bot(acl_keys=[VALID_PUBKEY], triggers={"welcome": "Hello!"})
        bot.command_manager.send_response = AsyncMock(return_value=True)
        cmd = AnnouncementsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="announce", is_dm=True, sender_pubkey=VALID_PUBKEY)
        import asyncio
        result = asyncio.run(cmd.execute(msg))
        assert result is True
        cmd.send_response.assert_called_once()

    def test_no_trigger_no_configured_triggers(self):
        from unittest.mock import AsyncMock
        bot = _make_bot(acl_keys=[VALID_PUBKEY])
        cmd = AnnouncementsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="announce", is_dm=True, sender_pubkey=VALID_PUBKEY)
        import asyncio
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_list_subcommand_with_triggers(self):
        from unittest.mock import AsyncMock
        bot = _make_bot(acl_keys=[VALID_PUBKEY], triggers={"welcome": "Hello!"})
        cmd = AnnouncementsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="announce list", is_dm=True, sender_pubkey=VALID_PUBKEY)
        import asyncio
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_list_subcommand_no_triggers(self):
        from unittest.mock import AsyncMock
        bot = _make_bot(acl_keys=[VALID_PUBKEY])
        cmd = AnnouncementsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="announce list", is_dm=True, sender_pubkey=VALID_PUBKEY)
        import asyncio
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_unknown_trigger(self):
        from unittest.mock import AsyncMock
        bot = _make_bot(acl_keys=[VALID_PUBKEY], triggers={"welcome": "Hello!"})
        cmd = AnnouncementsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="announce unknown_trigger", is_dm=True, sender_pubkey=VALID_PUBKEY)
        import asyncio
        result = asyncio.run(cmd.execute(msg))
        assert result is True
        cmd.send_response.assert_called_once()

    def test_trigger_locked_prevents_send(self):
        from unittest.mock import AsyncMock
        bot = _make_bot(acl_keys=[VALID_PUBKEY], triggers={"welcome": "Hello!"})
        cmd = AnnouncementsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        cmd._record_trigger_execution("welcome")
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey=VALID_PUBKEY)
        import asyncio
        result = asyncio.run(cmd.execute(msg))
        assert result is True
        # Should warn about lockout
        call_args = cmd.send_response.call_args[0][1]
        assert "just sent" in call_args.lower() or "wait" in call_args.lower()

    def test_trigger_on_cooldown(self):
        from unittest.mock import AsyncMock
        bot = _make_bot(acl_keys=[VALID_PUBKEY], triggers={"welcome": "Hello!"})
        cmd = AnnouncementsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        # Set cooldown but not lockout
        cmd.trigger_cooldowns["welcome"] = time.time()
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey=VALID_PUBKEY)
        import asyncio
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_trigger_override_bypasses_cooldown(self):
        from unittest.mock import AsyncMock
        bot = _make_bot(acl_keys=[VALID_PUBKEY], triggers={"welcome": "Hello!"})
        bot.command_manager.send_channel_message = AsyncMock(return_value=True)
        cmd = AnnouncementsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        # Set cooldown
        cmd.trigger_cooldowns["welcome"] = time.time()
        msg = mock_message(content="announce welcome override", is_dm=True, sender_pubkey=VALID_PUBKEY)
        import asyncio
        result = asyncio.run(cmd.execute(msg))
        assert result is True

    def test_successful_announcement_send(self):
        from unittest.mock import AsyncMock
        bot = _make_bot(acl_keys=[VALID_PUBKEY], triggers={"welcome": "Hello!"})
        bot.command_manager.send_channel_message = AsyncMock(return_value=True)
        cmd = AnnouncementsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey=VALID_PUBKEY)
        import asyncio
        result = asyncio.run(cmd.execute(msg))
        assert result is True
        bot.command_manager.send_channel_message.assert_called_once()

    def test_failed_announcement_send(self):
        from unittest.mock import AsyncMock
        bot = _make_bot(acl_keys=[VALID_PUBKEY], triggers={"welcome": "Hello!"})
        bot.command_manager.send_channel_message = AsyncMock(return_value=False)
        cmd = AnnouncementsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey=VALID_PUBKEY)
        import asyncio
        result = asyncio.run(cmd.execute(msg))
        assert result is True
        call_text = cmd.send_response.call_args[0][1]
        assert "failed" in call_text.lower()

    def test_trigger_with_custom_channel(self):
        from unittest.mock import AsyncMock
        bot = _make_bot(acl_keys=[VALID_PUBKEY], triggers={"welcome": "Hello!"})
        bot.command_manager.send_channel_message = AsyncMock(return_value=True)
        cmd = AnnouncementsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="announce welcome Emergency", is_dm=True, sender_pubkey=VALID_PUBKEY)
        import asyncio
        result = asyncio.run(cmd.execute(msg))
        assert result is True
        channel_arg = bot.command_manager.send_channel_message.call_args[0][0]
        assert channel_arg == "Emergency"

    def test_execute_exception_returns_false(self):
        from unittest.mock import AsyncMock
        bot = _make_bot(acl_keys=[VALID_PUBKEY], triggers={"welcome": "Hello!"})
        bot.command_manager.send_channel_message = AsyncMock(side_effect=RuntimeError("oops"))
        cmd = AnnouncementsCommand(bot)
        cmd.send_response = AsyncMock(return_value=True)
        msg = mock_message(content="announce welcome", is_dm=True, sender_pubkey=VALID_PUBKEY)
        import asyncio
        result = asyncio.run(cmd.execute(msg))
        assert result is False
