"""Tests for modules.commands.base_command.BaseCommand."""

import asyncio
import configparser
import time
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage
from tests.conftest import mock_message

# ---------------------------------------------------------------------------
# Concrete subclass for testing (BaseCommand is abstract)
# ---------------------------------------------------------------------------

class _Cmd(BaseCommand):
    name = "test"
    keywords = ["test"]
    description = "A test command"
    cooldown_seconds = 0

    async def execute(self, message: MeshMessage) -> bool:
        return True


def _make_bot(config=None, *, monitor_channels=None):
    bot = MagicMock()
    bot.logger = Mock()
    if config is None:
        config = configparser.ConfigParser()
        config.add_section("Bot")
        config.set("Bot", "bot_name", "TestBot")
        config.add_section("Channels")
        config.set("Channels", "monitor_channels", "general")
        config.set("Channels", "respond_to_dms", "true")
        config.add_section("Keywords")
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = monitor_channels or ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    bot.command_manager.send_response_chunked = AsyncMock(return_value=True)
    return bot


def _cmd(config=None, **kw):
    return _Cmd(_make_bot(config, **kw))


# ---------------------------------------------------------------------------
# translate / translate_get_value
# ---------------------------------------------------------------------------

class TestTranslate:
    def test_translate_calls_translator(self):
        cmd = _cmd()
        cmd.translate("some.key", x=1)
        cmd.bot.translator.translate.assert_called_once_with("some.key", x=1)

    def test_translate_get_value_calls_translator(self):
        cmd = _cmd()
        cmd.bot.translator.get_value = Mock(return_value=["a", "b"])
        result = cmd.translate_get_value("commands.test.subcommands")
        assert result == ["a", "b"]

    def test_translate_without_translator_returns_key(self):
        cmd = _cmd()
        del cmd.bot.translator
        assert cmd.translate("some.key") == "some.key"

    def test_translate_get_value_without_translator_returns_none(self):
        cmd = _cmd()
        del cmd.bot.translator
        assert cmd.translate_get_value("any.key") is None


# ---------------------------------------------------------------------------
# get_config_value — section migration + type conversions
# ---------------------------------------------------------------------------

class TestGetConfigValue:
    def _bot_with(self, section, **keys):
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot")
        cfg.set("Bot", "bot_name", "TB")
        cfg.add_section("Channels")
        cfg.set("Channels", "monitor_channels", "general")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        cfg.add_section(section)
        for k, v in keys.items():
            cfg.set(section, k, v)
        return _make_bot(cfg)

    def test_str_value_returned(self):
        bot = self._bot_with("Test_Command", foo="bar")
        cmd = _Cmd(bot)
        assert cmd.get_config_value("Test_Command", "foo") == "bar"

    def test_bool_value_returned(self):
        bot = self._bot_with("Test_Command", enabled="true")
        cmd = _Cmd(bot)
        assert cmd.get_config_value("Test_Command", "enabled", value_type="bool") is True

    def test_int_value_returned(self):
        bot = self._bot_with("Test_Command", count="42")
        cmd = _Cmd(bot)
        assert cmd.get_config_value("Test_Command", "count", value_type="int") == 42

    def test_float_value_returned(self):
        bot = self._bot_with("Test_Command", ratio="3.14")
        cmd = _Cmd(bot)
        assert abs(cmd.get_config_value("Test_Command", "ratio", value_type="float") - 3.14) < 0.001

    def test_list_value_returned(self):
        bot = self._bot_with("Test_Command", items="a, b, c")
        cmd = _Cmd(bot)
        assert cmd.get_config_value("Test_Command", "items", value_type="list") == ["a", "b", "c"]

    def test_unknown_value_type_returns_str(self):
        bot = self._bot_with("Test_Command", x="val")
        cmd = _Cmd(bot)
        result = cmd.get_config_value("Test_Command", "x", value_type="unknown_type")
        assert result == "val"

    def test_missing_key_returns_fallback(self):
        cmd = _cmd()
        assert cmd.get_config_value("Test_Command", "nonexistent", fallback="default") == "default"

    def test_old_section_fallback_logs_migration(self):
        """Hacker_Command falls back to old section 'Hacker'."""
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot"); cfg.set("Bot", "bot_name", "TB")
        cfg.add_section("Channels"); cfg.set("Channels", "monitor_channels", "g")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        cfg.add_section("Hacker")
        cfg.set("Hacker", "some_key", "legacy_val")
        bot = _make_bot(cfg)
        cmd = _Cmd(bot)
        result = cmd.get_config_value("Hacker_Command", "some_key")
        assert result == "legacy_val"
        bot.logger.info.assert_called()

    def test_legacy_key_alias_joke_enabled(self):
        """Joke_Command enabled maps to legacy [Jokes] joke_enabled."""
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot"); cfg.set("Bot", "bot_name", "TB")
        cfg.add_section("Channels"); cfg.set("Channels", "monitor_channels", "g")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        cfg.add_section("Jokes")
        cfg.set("Jokes", "joke_enabled", "true")
        bot = _make_bot(cfg)
        cmd = _Cmd(bot)
        result = cmd.get_config_value("Joke_Command", "enabled", value_type="bool")
        assert result is True

    def test_value_error_in_type_conversion_continues(self):
        bot = self._bot_with("Test_Command", count="notanint")
        cmd = _Cmd(bot)
        # Should return fallback, not raise
        result = cmd.get_config_value("Test_Command", "count", fallback=0, value_type="int")
        assert result == 0


# ---------------------------------------------------------------------------
# _derive_config_section_name
# ---------------------------------------------------------------------------

class TestDeriveConfigSectionName:
    def test_regular_name(self):
        cmd = _cmd()
        cmd.name = "sports"
        assert cmd._derive_config_section_name() == "Sports_Command"

    def test_camelcase_dadjoke(self):
        cmd = _cmd()
        cmd.name = "dadjoke"
        assert cmd._derive_config_section_name() == "DadJoke_Command"

    def test_camelcase_webviewer(self):
        cmd = _cmd()
        cmd.name = "webviewer"
        assert cmd._derive_config_section_name() == "WebViewer_Command"


# ---------------------------------------------------------------------------
# get_help_text / get_usage_info
# ---------------------------------------------------------------------------

class TestHelpAndUsage:
    def test_get_help_text_returns_description(self):
        cmd = _cmd()
        assert cmd.get_help_text() == "A test command"

    def test_get_help_text_fallback(self):
        cmd = _cmd()
        cmd.description = ""
        assert "No help" in cmd.get_help_text()

    def test_get_usage_info_keys(self):
        cmd = _cmd()
        info = cmd.get_usage_info()
        assert "description" in info
        assert "usage" in info
        assert "subcommands" in info
        assert "examples" in info
        assert "parameters" in info

    def test_get_usage_info_uses_translator_override(self):
        cmd = _cmd()
        cmd.bot.translator.get_value = Mock(side_effect=lambda k: (
            [{"name": "sub", "description": "d"}] if "subcommands" in k else
            ["ex1"] if "examples" in k else
            "!test <arg>" if "usage_syntax" in k else
            [{"name": "p", "description": "pd"}] if "parameters" in k else None
        ))
        info = cmd.get_usage_info()
        assert info["subcommands"] == [{"name": "sub", "description": "d"}]
        assert info["examples"] == ["ex1"]
        assert info["usage"] == "!test <arg>"

    def test_get_usage_info_translator_exception_handled(self):
        cmd = _cmd()
        cmd.bot.translator.get_value = Mock(side_effect=RuntimeError("broken"))
        info = cmd.get_usage_info()
        assert "description" in info  # still returns defaults


# ---------------------------------------------------------------------------
# _load_allowed_channels / _load_aliases_from_config
# ---------------------------------------------------------------------------

class TestLoadChannels:
    def test_no_channels_config_returns_none(self):
        cmd = _cmd()
        assert cmd.allowed_channels is None

    def test_empty_channels_config_returns_empty_list(self):
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot"); cfg.set("Bot", "bot_name", "TB")
        cfg.add_section("Channels"); cfg.set("Channels", "monitor_channels", "g")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        cfg.add_section("Test_Command")
        cfg.set("Test_Command", "channels", "")
        bot = _make_bot(cfg)
        cmd = _Cmd(bot)
        assert cmd.allowed_channels == []

    def test_channels_config_parsed(self):
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot"); cfg.set("Bot", "bot_name", "TB")
        cfg.add_section("Channels"); cfg.set("Channels", "monitor_channels", "g")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        cfg.add_section("Test_Command")
        cfg.set("Test_Command", "channels", "local,emergency")
        bot = _make_bot(cfg)
        cmd = _Cmd(bot)
        assert "local" in cmd.allowed_channels
        assert "emergency" in cmd.allowed_channels


class TestLoadAliases:
    def test_aliases_added_to_keywords(self):
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot"); cfg.set("Bot", "bot_name", "TB")
        cfg.add_section("Channels"); cfg.set("Channels", "monitor_channels", "g")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        cfg.add_section("Test_Command")
        cfg.set("Test_Command", "aliases", "t, tst")
        bot = _make_bot(cfg)
        cmd = _Cmd(bot)
        assert "t" in cmd.keywords
        assert "tst" in cmd.keywords

    def test_alias_strip_prefix(self):
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot"); cfg.set("Bot", "bot_name", "TB")
        cfg.set("Bot", "command_prefix", "!")
        cfg.add_section("Channels"); cfg.set("Channels", "monitor_channels", "g")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        cfg.add_section("Test_Command")
        cfg.set("Test_Command", "aliases", "!t")
        bot = _make_bot(cfg)
        cmd = _Cmd(bot)
        # "!t" should be normalized to "t"
        assert "t" in cmd.keywords


# ---------------------------------------------------------------------------
# is_channel_allowed / can_execute
# ---------------------------------------------------------------------------

class TestIsChannelAllowed:
    def test_dm_always_allowed(self):
        cmd = _cmd()
        msg = mock_message(is_dm=True)
        assert cmd.is_channel_allowed(msg) is True

    def test_no_channel_returns_false(self):
        cmd = _cmd()
        msg = mock_message(channel=None)
        assert cmd.is_channel_allowed(msg) is False

    def test_channel_in_monitor_channels_allowed(self):
        cmd = _cmd(monitor_channels=["general"])
        msg = mock_message(channel="general")
        assert cmd.is_channel_allowed(msg) is True

    def test_channel_not_in_monitor_channels_blocked(self):
        cmd = _cmd(monitor_channels=["general"])
        msg = mock_message(channel="other")
        assert cmd.is_channel_allowed(msg) is False

    def test_empty_allowed_channels_blocks_all(self):
        cmd = _cmd()
        cmd.allowed_channels = []
        msg = mock_message(channel="general")
        assert cmd.is_channel_allowed(msg) is False

    def test_explicit_allowed_channels_filter(self):
        cmd = _cmd()
        cmd.allowed_channels = ["emergency"]
        assert cmd.is_channel_allowed(mock_message(channel="emergency")) is True
        assert cmd.is_channel_allowed(mock_message(channel="general")) is False


class TestCanExecute:
    def test_blocked_channel_returns_false(self):
        cmd = _cmd(monitor_channels=["general"])
        msg = mock_message(channel="other")
        assert cmd.can_execute(msg) is False

    def test_requires_dm_in_channel_returns_false(self):
        cmd = _cmd()
        cmd.requires_dm = True
        msg = mock_message(channel="general")
        assert cmd.can_execute(msg) is False

    def test_skip_channel_check(self):
        cmd = _cmd(monitor_channels=["general"])
        msg = mock_message(channel="other")
        # With skip, channel check is bypassed
        assert cmd.can_execute(msg, skip_channel_check=True) is True

    def test_cooldown_blocks(self):
        cmd = _cmd()
        cmd.cooldown_seconds = 60
        cmd.record_execution("user1")
        msg = mock_message(sender_id="user1")
        assert cmd.can_execute(msg) is False


# ---------------------------------------------------------------------------
# check_cooldown / record_execution / get_remaining_cooldown
# ---------------------------------------------------------------------------

class TestCooldown:
    def test_no_cooldown_always_allowed(self):
        cmd = _cmd()
        cmd.cooldown_seconds = 0
        can, remaining = cmd.check_cooldown()
        assert can is True
        assert remaining == 0.0

    def test_global_cooldown_blocks(self):
        cmd = _cmd()
        cmd.cooldown_seconds = 60
        cmd._last_execution_time = time.time()
        can, remaining = cmd.check_cooldown()
        assert can is False
        assert remaining > 0

    def test_per_user_cooldown_blocks(self):
        cmd = _cmd()
        cmd.cooldown_seconds = 60
        cmd._user_cooldowns["user1"] = time.time()
        can, remaining = cmd.check_cooldown("user1")
        assert can is False

    def test_per_user_cooldown_expired_allows(self):
        cmd = _cmd()
        cmd.cooldown_seconds = 1
        cmd._user_cooldowns["user1"] = time.time() - 10
        can, _ = cmd.check_cooldown("user1")
        assert can is True

    def test_record_execution_global(self):
        cmd = _cmd()
        cmd.cooldown_seconds = 60
        cmd.record_execution()
        assert cmd._last_execution_time > 0

    def test_record_execution_per_user(self):
        cmd = _cmd()
        cmd.cooldown_seconds = 60
        cmd.record_execution("userA")
        assert "userA" in cmd._user_cooldowns

    def test_record_execution_cleans_up_old_entries(self):
        cmd = _cmd()
        cmd.cooldown_seconds = 1
        # Fill with 1001 old entries
        old_time = time.time() - 100
        for i in range(1001):
            cmd._user_cooldowns[f"u{i}"] = old_time
        cmd.record_execution("new_user")
        # Old entries should have been cleaned up
        assert len(cmd._user_cooldowns) < 1001

    def test_record_execution_alias(self):
        cmd = _cmd()
        cmd.cooldown_seconds = 60
        cmd._record_execution("user1")
        assert "user1" in cmd._user_cooldowns

    def test_get_remaining_cooldown_returns_int(self):
        cmd = _cmd()
        cmd.cooldown_seconds = 60
        cmd._last_execution_time = time.time()
        assert isinstance(cmd.get_remaining_cooldown(), int)
        assert cmd.get_remaining_cooldown() > 0


# ---------------------------------------------------------------------------
# get_max_message_length
# ---------------------------------------------------------------------------

class TestGetMaxMessageLength:
    def test_dm_returns_158(self):
        cmd = _cmd()
        assert cmd.get_max_message_length(mock_message(is_dm=True)) == 158

    def test_channel_uses_bot_name_from_config(self):
        cmd = _cmd()
        length = cmd.get_max_message_length(mock_message(channel="general"))
        assert 130 <= length <= 158

    def test_channel_uses_meshcore_self_info_dict(self):
        cmd = _cmd()
        cmd.bot.meshcore = MagicMock()
        cmd.bot.meshcore.self_info = {"name": "RadioBot"}
        length = cmd.get_max_message_length(mock_message(channel="general"))
        # "RadioBot" is 8 chars → 160 - 8 - 2 = 150
        assert length == 150

    def test_channel_uses_meshcore_self_info_object(self):
        cmd = _cmd()
        cmd.bot.meshcore = MagicMock()
        si = MagicMock()
        si.name = "BotNode"
        cmd.bot.meshcore.self_info = si
        length = cmd.get_max_message_length(mock_message(channel="general"))
        assert length == 160 - len(b"BotNode") - 2

    def test_meshcore_exception_falls_back_to_config(self):
        cmd = _cmd()
        cmd.bot.meshcore = MagicMock()
        cmd.bot.meshcore.self_info = MagicMock(side_effect=RuntimeError("broken"))
        # Should not raise
        length = cmd.get_max_message_length(mock_message(channel="general"))
        assert length >= 130


# ---------------------------------------------------------------------------
# matches_keyword / matches_custom_syntax / should_execute
# ---------------------------------------------------------------------------

class TestMatchesKeyword:
    def test_exact_match(self):
        cmd = _cmd()
        assert cmd.matches_keyword(mock_message(content="test")) is True

    def test_keyword_with_args(self):
        cmd = _cmd()
        assert cmd.matches_keyword(mock_message(content="test some args")) is True

    def test_no_match(self):
        cmd = _cmd()
        assert cmd.matches_keyword(mock_message(content="other")) is False

    def test_bang_prefix_stripped(self):
        cmd = _cmd()
        assert cmd.matches_keyword(mock_message(content="!test")) is True

    def test_config_prefix_required(self):
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot"); cfg.set("Bot", "bot_name", "TB")
        cfg.set("Bot", "command_prefix", "!")
        cfg.add_section("Channels"); cfg.set("Channels", "monitor_channels", "g")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        bot = _make_bot(cfg)
        cmd = _Cmd(bot)
        assert cmd.matches_keyword(mock_message(content="!test")) is True
        assert cmd.matches_keyword(mock_message(content="test")) is False

    def test_empty_keywords_returns_false(self):
        cmd = _cmd()
        cmd.keywords = []
        assert cmd.matches_keyword(mock_message(content="test")) is False

    def test_mention_bot_allowed(self):
        cmd = _cmd()
        cmd.bot.meshcore = None
        cmd.bot.config.set("Bot", "bot_name", "TestBot")
        # mention bot → should match
        assert cmd.matches_keyword(mock_message(content="@[TestBot] test")) is True

    def test_mention_other_user_blocked(self):
        cmd = _cmd()
        # mention someone else → should not match
        assert cmd.matches_keyword(mock_message(content="@[OtherUser] test")) is False

    def test_matches_custom_syntax_blocked_by_mention(self):
        cmd = _cmd()
        assert cmd.matches_custom_syntax(mock_message(content="@[OtherUser] test")) is False

    def test_should_execute_keyword_match(self):
        cmd = _cmd()
        assert cmd.should_execute(mock_message(content="test")) is True

    def test_should_execute_no_match(self):
        cmd = _cmd()
        assert cmd.should_execute(mock_message(content="nothing")) is False


# ---------------------------------------------------------------------------
# _extract_mentions / _is_bot_mentioned / _strip_mentions
# ---------------------------------------------------------------------------

class TestMentions:
    def test_extract_mentions_found(self):
        cmd = _cmd()
        assert cmd._extract_mentions("hey @[Alice] and @[Bob]") == ["Alice", "Bob"]

    def test_extract_mentions_empty(self):
        cmd = _cmd()
        assert cmd._extract_mentions("no mentions here") == []

    def test_is_bot_mentioned_true(self):
        cmd = _cmd()
        cmd.bot.meshcore = None
        cmd.bot.config.set("Bot", "bot_name", "TestBot")
        assert cmd._is_bot_mentioned("@[TestBot] help") is True

    def test_is_bot_mentioned_false(self):
        cmd = _cmd()
        assert cmd._is_bot_mentioned("@[OtherBot] help") is False

    def test_is_bot_mentioned_no_mentions(self):
        cmd = _cmd()
        assert cmd._is_bot_mentioned("just a message") is False

    def test_strip_mentions_removes_at_tags(self):
        cmd = _cmd()
        result = cmd._strip_mentions("@[Alice] hello @[Bob]")
        assert "@[" not in result
        assert "hello" in result


# ---------------------------------------------------------------------------
# get_path_display_string / build_enhanced_connection_info
# ---------------------------------------------------------------------------

class TestPathDisplay:
    def test_direct_path_when_zero_hops(self):
        cmd = _cmd()
        msg = mock_message(routing_info={"path_length": 0})
        assert cmd.get_path_display_string(msg) == "Direct"

    def test_multi_hop_path(self):
        cmd = _cmd()
        msg = mock_message(routing_info={"path_length": 2, "path_nodes": ["aa", "bb"]})
        result = cmd.get_path_display_string(msg)
        assert "2 hops" in result

    def test_no_routing_info_falls_back_to_path(self):
        cmd = _cmd()
        msg = mock_message(path="abc123 via ROUTE_TYPE_MESH")
        result = cmd.get_path_display_string(msg)
        assert result == "abc123"

    def test_no_path_returns_unknown(self):
        cmd = _cmd()
        msg = mock_message(path=None)
        assert cmd.get_path_display_string(msg) == "Unknown"

    def test_build_enhanced_connection_info(self):
        cmd = _cmd()
        msg = mock_message(routing_info={"path_length": 0}, snr=12.5, rssi=-80)
        result = cmd.build_enhanced_connection_info(msg)
        assert "SNR" in result
        assert "RSSI" in result


# ---------------------------------------------------------------------------
# format_timestamp / format_elapsed / format_response
# ---------------------------------------------------------------------------

class TestFormatMethods:
    def test_format_timestamp_returns_string(self):
        cmd = _cmd()
        with patch("modules.commands.base_command.get_config_timezone", return_value=(None, None)):
            with patch("modules.commands.base_command.datetime") as mock_dt:
                mock_dt.now.return_value.strftime = Mock(return_value="12:00:00")
                result = cmd.format_timestamp(mock_message())
        assert isinstance(result, str)

    def test_format_timestamp_exception_returns_unknown(self):
        cmd = _cmd()
        with patch("modules.commands.base_command.get_config_timezone", side_effect=RuntimeError):
            result = cmd.format_timestamp(mock_message())
        assert result == "Unknown"

    def test_format_elapsed_returns_string(self):
        cmd = _cmd()
        result = cmd.format_elapsed(mock_message(timestamp=None))
        assert isinstance(result, str)

    def test_format_response_substitutes_fields(self):
        cmd = _cmd()
        msg = mock_message(sender_id="Alice", path="direct", snr=5.0, rssi=-70)
        result = cmd.format_response(msg, "{sender} via {path}")
        assert "Alice" in result

    def test_format_response_key_error_returns_template(self):
        cmd = _cmd()
        result = cmd.format_response(mock_message(), "{invalid_key}")
        assert result == "{invalid_key}"


# ---------------------------------------------------------------------------
# requires_admin_access / _check_admin_access
# ---------------------------------------------------------------------------

VALID_PUBKEY = "a" * 64


class TestAdminAccess:
    def _bot_with_admin(self, admin_commands="test", admin_pubkeys=VALID_PUBKEY):
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot"); cfg.set("Bot", "bot_name", "TB")
        cfg.add_section("Channels"); cfg.set("Channels", "monitor_channels", "g")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        cfg.add_section("Admin_ACL")
        if admin_commands is not None:
            cfg.set("Admin_ACL", "admin_commands", admin_commands)
        if admin_pubkeys is not None:
            cfg.set("Admin_ACL", "admin_pubkeys", admin_pubkeys)
        return _make_bot(cfg)

    def test_requires_admin_true_when_configured(self):
        cmd = _Cmd(self._bot_with_admin())
        assert cmd.requires_admin_access() is True

    def test_requires_admin_false_when_not_listed(self):
        cmd = _Cmd(self._bot_with_admin(admin_commands="other"))
        assert cmd.requires_admin_access() is False

    def test_requires_admin_false_no_section(self):
        cmd = _cmd()
        assert cmd.requires_admin_access() is False

    def test_requires_admin_empty_commands_returns_false(self):
        cmd = _Cmd(self._bot_with_admin(admin_commands=""))
        assert cmd.requires_admin_access() is False

    def test_check_admin_access_valid_pubkey(self):
        cmd = _Cmd(self._bot_with_admin())
        msg = mock_message(sender_pubkey=VALID_PUBKEY)
        assert cmd._check_admin_access(msg) is True

    def test_check_admin_access_wrong_pubkey(self):
        cmd = _Cmd(self._bot_with_admin())
        msg = mock_message(sender_pubkey="b" * 64)
        assert cmd._check_admin_access(msg) is False

    def test_check_admin_access_no_pubkey(self):
        cmd = _Cmd(self._bot_with_admin())
        msg = mock_message(sender_pubkey=None)
        assert cmd._check_admin_access(msg) is False

    def test_check_admin_access_invalid_pubkey_format(self):
        cmd = _Cmd(self._bot_with_admin())
        msg = mock_message(sender_pubkey="short")
        assert cmd._check_admin_access(msg) is False

    def test_check_admin_access_invalid_config_pubkey_skipped(self):
        cmd = _Cmd(self._bot_with_admin(admin_pubkeys="bad_key," + VALID_PUBKEY))
        msg = mock_message(sender_pubkey=VALID_PUBKEY)
        assert cmd._check_admin_access(msg) is True

    def test_check_admin_access_empty_pubkeys_returns_false(self):
        cmd = _Cmd(self._bot_with_admin(admin_pubkeys=""))
        msg = mock_message(sender_pubkey=VALID_PUBKEY)
        assert cmd._check_admin_access(msg) is False

    def test_check_admin_access_no_section_returns_false(self):
        cmd = _cmd()
        assert cmd._check_admin_access(mock_message()) is False


# ---------------------------------------------------------------------------
# send_response / send_response_chunked / handle_keyword_match
# ---------------------------------------------------------------------------

class TestSendResponse:
    def test_send_response_delegates_to_command_manager(self):
        cmd = _cmd()
        result = asyncio.run(cmd.send_response(mock_message(), "hello"))
        cmd.bot.command_manager.send_response.assert_called_once()
        assert result is True

    def test_send_response_exception_returns_false(self):
        cmd = _cmd()
        cmd.bot.command_manager.send_response = AsyncMock(side_effect=RuntimeError("fail"))
        result = asyncio.run(cmd.send_response(mock_message(), "hello"))
        assert result is False

    def test_send_response_chunked_delegates(self):
        cmd = _cmd()
        result = asyncio.run(cmd.send_response_chunked(mock_message(), ["a", "b"]))
        cmd.bot.command_manager.send_response_chunked.assert_called_once()
        assert result is True

    def test_send_response_chunked_exception_returns_false(self):
        cmd = _cmd()
        cmd.bot.command_manager.send_response_chunked = AsyncMock(side_effect=RuntimeError("fail"))
        result = asyncio.run(cmd.send_response_chunked(mock_message(), ["a"]))
        assert result is False

    def test_handle_keyword_match_no_format_returns_false(self):
        cmd = _cmd()
        result = asyncio.run(cmd.handle_keyword_match(mock_message()))
        assert result is False

    def test_handle_keyword_match_with_format_sends(self):
        cmd = _cmd()
        cmd.get_response_format = Mock(return_value="response: {sender}")
        cmd.send_response = AsyncMock(return_value=True)
        result = asyncio.run(cmd.handle_keyword_match(mock_message()))
        assert result is True


# ---------------------------------------------------------------------------
# get_metadata / get_queue_threshold_seconds / _get_bot_name
# ---------------------------------------------------------------------------

class TestMiscMethods:
    def test_get_metadata_returns_dict(self):
        cmd = _cmd()
        meta = cmd.get_metadata()
        assert meta["name"] == "test"
        assert "keywords" in meta
        assert "class_name" in meta

    def test_get_queue_threshold_seconds(self):
        cmd = _cmd()
        cmd.cooldown_seconds = 60
        threshold = cmd.get_queue_threshold_seconds()
        assert 0.0 <= threshold <= 60

    def test_get_bot_name_from_config(self):
        cmd = _cmd()
        cmd.bot.meshcore = None
        assert cmd._get_bot_name() == "TestBot"

    def test_get_bot_name_from_meshcore_dict(self):
        cmd = _cmd()
        cmd.bot.meshcore = MagicMock()
        cmd.bot.meshcore.self_info = {"name": "RadioName"}
        assert cmd._get_bot_name() == "RadioName"

    def test_get_bot_name_from_meshcore_object(self):
        cmd = _cmd()
        cmd.bot.meshcore = MagicMock()
        si = MagicMock()
        si.name = "ObjName"
        cmd.bot.meshcore.self_info = si
        assert cmd._get_bot_name() == "ObjName"

    def test_get_bot_name_from_meshcore_adv_name(self):
        cmd = _cmd()
        cmd.bot.meshcore = MagicMock()
        si = MagicMock(spec=[])  # no 'name' attr
        si.adv_name = "AdvName"
        cmd.bot.meshcore.self_info = si
        assert cmd._get_bot_name() == "AdvName"

    def test_get_bot_name_meshcore_exception_falls_back(self):
        cmd = _cmd()
        cmd.bot.meshcore = MagicMock()
        # Accessing self_info raises
        type(cmd.bot.meshcore).self_info = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("broken"))
        )
        cmd.bot.config.set("Bot", "bot_name", "TestBot")
        assert cmd._get_bot_name() == "TestBot"

    def test_strip_quotes_removes_quotes(self):
        cmd = _cmd()
        assert cmd._strip_quotes_from_config('"hello"') == "hello"

    def test_strip_quotes_no_quotes_unchanged(self):
        cmd = _cmd()
        assert cmd._strip_quotes_from_config("hello") == "hello"

    def test_can_execute_now(self):
        cmd = _cmd()
        assert cmd.can_execute_now(mock_message()) is True

    def test_load_translated_keywords_no_translator(self):
        cmd = _cmd()
        del cmd.bot.translator
        # Should not raise
        cmd._load_translated_keywords()

    def test_load_translated_keywords_adds_new_words(self):
        cmd = _cmd()
        cmd.bot.translator.get_value = Mock(return_value=["testalt", "testalias"])
        cmd._load_translated_keywords()
        assert "testalt" in cmd.keywords

    def test_load_translated_keywords_exception_handled(self):
        cmd = _cmd()
        cmd.bot.translator.get_value = Mock(side_effect=RuntimeError("broken"))
        cmd._load_translated_keywords()  # should not raise


# ---------------------------------------------------------------------------
# Coverage gap fill: ~40 missing lines in base_command.py
# ---------------------------------------------------------------------------


class TestGetConfigValueMigrationLogs:
    """Lines 152, 185: migration log notices when old/legacy sections are used."""

    def test_old_section_migration_log(self):
        """Line 152: logs migration notice when value found in old section (e.g. [Hacker])."""
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot")
        cfg.set("Bot", "bot_name", "TestBot")
        cfg.add_section("Channels")
        cfg.set("Channels", "monitor_channels", "general")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        # Add value in old section [Hacker] not new [Hacker_Command]
        cfg.add_section("Hacker")
        cfg.set("Hacker", "hacker_enabled", "true")
        cmd = _cmd(cfg)
        result = cmd.get_config_value("Hacker_Command", "hacker_enabled", fallback=False, value_type='bool')
        # Should find value in old [Hacker] section and log migration notice
        assert result is True
        cmd.bot.logger.info.assert_called()

    def test_legacy_section_fallback_log(self):
        """Line 185: logs migration notice when value found in legacy_section_fallback."""
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot")
        cfg.set("Bot", "bot_name", "TestBot")
        cfg.add_section("Channels")
        cfg.set("Channels", "monitor_channels", "general")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        # Add value in legacy [Jokes] section (for Joke_Command joke_enabled key)
        cfg.add_section("Jokes")
        cfg.set("Jokes", "joke_enabled", "true")
        cmd = _cmd(cfg)
        result = cmd.get_config_value("Joke_Command", "joke_enabled", fallback=False, value_type='bool')
        assert result is True
        cmd.bot.logger.info.assert_called()

    def test_value_error_in_section_try_continues(self):
        """Lines 188-190: ValueError during type conversion causes 'continue' to next section."""
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot")
        cfg.set("Bot", "bot_name", "TestBot")
        cfg.add_section("Channels")
        cfg.set("Channels", "monitor_channels", "general")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        # Only new section with bad bool value → ValueError → returns fallback
        cfg.add_section("Hacker_Command")
        cfg.set("Hacker_Command", "hacker_enabled", "NOTABOOL")
        cmd = _cmd(cfg)
        result = cmd.get_config_value("Hacker_Command", "hacker_enabled", fallback=False, value_type='bool')
        assert result is False  # fallback after ValueError

    def test_exception_in_section_try_continues(self):
        """Lines 191-193: non-ValueError exception causes 'continue' to next section."""
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot")
        cfg.set("Bot", "bot_name", "TestBot")
        cfg.add_section("Channels")
        cfg.set("Channels", "monitor_channels", "general")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        cfg.add_section("Hacker_Command")
        cfg.set("Hacker_Command", "hacker_enabled", "true")
        cmd = _cmd(cfg)
        with patch.object(cmd.bot.config, 'getboolean', side_effect=RuntimeError("unexpected")):
            result = cmd.get_config_value("Hacker_Command", "hacker_enabled", fallback=False, value_type='bool')
        assert result is False


class TestGetConfigValueLegacyKeyAlias:
    """Lines 204-215: legacy_key_alias iteration with various value types."""

    def _cfg_with_jokes(self, value: str) -> configparser.ConfigParser:
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot")
        cfg.set("Bot", "bot_name", "TestBot")
        cfg.add_section("Channels")
        cfg.set("Channels", "monitor_channels", "general")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        cfg.add_section("Jokes")
        cfg.set("Jokes", "joke_enabled", value)
        return cfg

    def test_legacy_key_alias_bool(self):
        """Lines 202-203: bool branch in legacy alias."""
        cmd = _cmd(self._cfg_with_jokes("true"))
        result = cmd.get_config_value("Joke_Command", "enabled", fallback=False, value_type='bool')
        assert result is True

    def test_legacy_key_alias_int(self):
        """Lines 204-205: int branch in legacy alias."""
        cfg = self._cfg_with_jokes("42")
        cmd = _cmd(cfg)
        result = cmd.get_config_value("Joke_Command", "enabled", fallback=0, value_type='int')
        assert result == 42

    def test_legacy_key_alias_float(self):
        """Lines 206-207: float branch in legacy alias."""
        cfg = self._cfg_with_jokes("3.14")
        cmd = _cmd(cfg)
        result = cmd.get_config_value("Joke_Command", "enabled", fallback=0.0, value_type='float')
        assert result == pytest.approx(3.14)

    def test_legacy_key_alias_list(self):
        """Lines 208-210: list branch in legacy alias."""
        cfg = self._cfg_with_jokes("a, b, c")
        cmd = _cmd(cfg)
        result = cmd.get_config_value("Joke_Command", "enabled", fallback=[], value_type='list')
        assert result == ["a", "b", "c"]

    def test_legacy_key_alias_str(self):
        """Lines 211-212: str (else) branch in legacy alias."""
        cfg = self._cfg_with_jokes("hello")
        cmd = _cmd(cfg)
        result = cmd.get_config_value("Joke_Command", "enabled", fallback="", value_type='str')
        assert result == "hello"

    def test_legacy_key_alias_value_error_skipped(self):
        """Lines 214-215: ValueError in legacy alias is caught; fallback returned."""
        cfg = self._cfg_with_jokes("notabool")
        cmd = _cmd(cfg)
        result = cmd.get_config_value("Joke_Command", "enabled", fallback=False, value_type='bool')
        assert result is False


class TestNormalizeAliasFromConfig:
    """Lines 376, 385, 390-391, 393-394: _normalize_alias_from_config edge cases."""

    def test_empty_string_returns_empty(self):
        """Line 376: empty alias → return ''."""
        cmd = _cmd()
        assert cmd._normalize_alias_from_config("") == ""

    def test_alias_empties_after_stripping_becomes_empty(self):
        """Line 385: loop strips all chars leaving empty alias → break."""
        cmd = _cmd()
        # command_prefix is '' so cp_lower is '', decorative stripping applies
        result = cmd._normalize_alias_from_config("!")
        assert result == ""

    def test_no_prefix_strips_decorative_first_char(self):
        """Lines 390-391: no cp_lower, alias starts with decorative char → strip it."""
        cmd = _cmd()
        # No command_prefix configured → cp_lower = ''
        result = cmd._normalize_alias_from_config(".weather")
        assert result == "weather"

    def test_cp_lower_set_strips_decorative_not_prefix(self):
        """Lines 393-394: cp_lower set, alias starts with different decorative char → strip it."""
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot")
        cfg.set("Bot", "bot_name", "TestBot")
        cfg.set("Bot", "command_prefix", "!")
        cfg.add_section("Channels")
        cfg.set("Channels", "monitor_channels", "general")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        cmd = _cmd(cfg)
        # cp_lower = '!' but alias starts with '.' (different decorative)
        result = cmd._normalize_alias_from_config(".weather")
        assert result == "weather"


class TestLoadAliasesSkipsEmpty:
    """Line 416: empty alias after normalization is skipped."""

    def test_empty_alias_after_normalize_skipped(self):
        """Line 416: ',' alone in aliases string produces empty alias → not added."""
        cfg = configparser.ConfigParser()
        cfg.add_section("Bot")
        cfg.set("Bot", "bot_name", "TestBot")
        cfg.add_section("Channels")
        cfg.set("Channels", "monitor_channels", "general")
        cfg.set("Channels", "respond_to_dms", "true")
        cfg.add_section("Keywords")
        cfg.add_section("Test_Command")
        # Comma-only aliases → all empty after split+strip → all skipped
        cfg.set("Test_Command", "aliases", ",")
        cmd = _cmd(cfg)
        initial_keywords = list(cmd.keywords)
        cmd._load_aliases_from_config()
        # No new keywords should have been added
        assert cmd.keywords == initial_keywords


class TestGetMaxMessageLengthMissingPaths:
    """Lines 576-579, 583: get_max_message_length meshcore user_name paths."""

    def test_self_info_dict_with_user_name_no_name(self):
        """Lines 572-573: self_info dict with 'user_name' but no 'name' → uses user_name."""
        cmd = _cmd()
        cmd.bot.meshcore = MagicMock()
        # dict has 'user_name' but no 'name' key → username = "RadioUser"
        cmd.bot.meshcore.self_info = {"user_name": "RadioUser"}
        msg = mock_message(channel="general")
        msg.is_dm = False
        length = cmd.get_max_message_length(msg)
        # "RadioUser" = 9 bytes → 160 - 9 - 2 = 149, clamped to max(130, 149) = 149
        assert isinstance(length, int)
        assert length == 149

    def test_self_info_object_with_user_name_attr(self):
        """Lines 576-577: self_info object lacking 'name' but has 'user_name' attr."""
        class _Info:
            user_name = "AttrUser"
        cmd = _cmd()
        cmd.bot.meshcore = MagicMock()
        cmd.bot.meshcore.self_info = _Info()
        msg = mock_message(channel="general")
        msg.is_dm = False
        length = cmd.get_max_message_length(msg)
        # "AttrUser" = 8 bytes → 160 - 8 - 2 = 150
        assert isinstance(length, int)
        assert length == 150

    def test_self_info_dict_no_name_no_user_name_falls_back_to_config(self):
        """Line 583: username is None after meshcore → fallback to bot_name config."""
        cmd = _cmd()
        cmd.bot.meshcore = MagicMock()
        # dict with no 'name' or 'user_name' → username stays None → config fallback
        cmd.bot.meshcore.self_info = {"other": "value"}
        cmd.bot.config.set("Bot", "bot_name", "TestBot")
        msg = mock_message(channel="general")
        msg.is_dm = False
        length = cmd.get_max_message_length(msg)
        # "TestBot" = 7 bytes → 160 - 7 - 2 = 151
        assert isinstance(length, int)
        assert length == 151

    def test_self_info_property_raises_exception_is_caught(self):
        """Lines 578-579: exception accessing self_info → caught, fallback to config."""
        cmd = _cmd()
        cmd.bot.meshcore = MagicMock()
        # Property raises RuntimeError → caught by the except Exception block
        type(cmd.bot.meshcore).self_info = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("broken"))
        )
        cmd.bot.config.set("Bot", "bot_name", "TestBot")
        msg = mock_message(channel="general")
        msg.is_dm = False
        length = cmd.get_max_message_length(msg)
        assert isinstance(length, int)


class TestCooldownExpiredPath:
    """Line 625: global cooldown expired returns (True, 0.0)."""

    def test_global_cooldown_expired_returns_true(self):
        """Line 625: elapsed > cooldown → (True, 0.0)."""
        cmd = _cmd()
        cmd.cooldown_seconds = 5
        cmd._last_execution_time = time.time() - 10  # 10 seconds ago
        can, remaining = cmd.check_cooldown()
        assert can is True
        assert remaining == 0.0


class TestShouldExecuteRequiresDmChannelNotAllowed:
    """Lines 894-896: requires_dm=True + not DM + channel not allowed → False."""

    def test_requires_dm_channel_not_allowed_returns_false(self):
        """Lines 894-896: DM-only command in a non-DM channel that is not allowed."""
        cmd = _cmd()
        cmd.requires_dm = True
        # allowed_channels restricts to 'admin' only
        cmd.allowed_channels = ["admin"]
        # Use content="test" so matches_keyword returns True (keyword is "test")
        msg = mock_message(content="test", channel="private_ch", is_dm=False)
        result = cmd.should_execute(msg)
        assert result is False


class TestRequiresAdminAccessExceptionPath:
    """Lines 983-985: requires_admin_access exception → False."""

    def test_requires_admin_access_exception_returns_false(self):
        """Lines 983-985: exception in config.get → logs warning, returns False."""
        cmd = _cmd()
        cmd.bot.config.add_section("Admin_ACL")
        cmd.bot.config.set("Admin_ACL", "admin_commands", "test")
        # Force an exception inside the try block
        with patch.object(cmd.bot.config, 'get', side_effect=RuntimeError("bad")):
            result = cmd.requires_admin_access()
        assert result is False
        cmd.bot.logger.warning.assert_called()


class TestCheckAdminAccessEdgePaths:
    """Lines 1015, 1025-1026, 1062-1064: admin ACL edge cases."""

    def _valid_pubkey(self):
        return "a" * 64

    def test_empty_key_in_pubkeys_list_skipped(self):
        """Line 1015: empty string after split/strip is skipped (continue)."""
        cmd = _cmd()
        cmd.bot.config.add_section("Admin_ACL")
        # Leading comma produces an empty entry that should be skipped
        cmd.bot.config.set("Admin_ACL", "admin_pubkeys", f",{self._valid_pubkey()}")
        msg = mock_message()
        msg.sender_pubkey = self._valid_pubkey()
        result = cmd._check_admin_access(msg)
        assert result is True  # valid key still matched after skipping the empty entry

    def test_no_valid_pubkeys_after_validation_returns_false(self):
        """Lines 1025-1026: all pubkeys invalid → logs error, returns False."""
        cmd = _cmd()
        cmd.bot.config.add_section("Admin_ACL")
        # Short (invalid) pubkeys → all fail validate_pubkey_format
        cmd.bot.config.set("Admin_ACL", "admin_pubkeys", "short_invalid_key,another_bad")
        msg = mock_message()
        msg.sender_pubkey = "a" * 64
        result = cmd._check_admin_access(msg)
        assert result is False
        cmd.bot.logger.error.assert_called()

    def test_check_admin_access_exception_returns_false(self):
        """Lines 1062-1064: unexpected exception in try → logs error, returns False."""
        cmd = _cmd()
        cmd.bot.config.add_section("Admin_ACL")
        cmd.bot.config.set("Admin_ACL", "admin_pubkeys", self._valid_pubkey())
        msg = mock_message()
        msg.sender_pubkey = self._valid_pubkey()
        # Force exception inside the try block after the section check
        with patch("modules.commands.base_command.validate_pubkey_format", side_effect=RuntimeError("oops")):
            result = cmd._check_admin_access(msg)
        assert result is False
        cmd.bot.logger.error.assert_called()
