"""Tests for modules.commands.base_command.BaseCommand."""

import asyncio
import configparser
import time
from unittest.mock import AsyncMock, MagicMock, Mock, patch

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
