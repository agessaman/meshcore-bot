"""Tests for modules.commands.base_command."""

from unittest.mock import Mock

from modules.commands.alert_command import AlertCommand
from modules.commands.base_command import BaseCommand
from modules.commands.dadjoke_command import DadJokeCommand
from modules.commands.hacker_command import HackerCommand
from modules.commands.joke_command import JokeCommand
from modules.commands.ping_command import PingCommand
from modules.commands.sports_command import SportsCommand
from modules.commands.stats_command import StatsCommand
from modules.models import CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD, MeshMessage
from tests.conftest import mock_message


class _TestCommand(BaseCommand):
    """Minimal concrete command for testing BaseCommand behavior."""
    name = "testcmd"
    keywords = ["testcmd"]
    description = "Test"
    short_description = "Test"
    usage = "testcmd"
    examples = ["testcmd"]

    def can_execute(self, message: MeshMessage) -> bool:
        return super().can_execute(message)

    def get_help_text(self) -> str:
        return "Help"

    async def execute(self, message: MeshMessage) -> bool:
        return await self.send_response(message, "ok")


class TestDeriveConfigSectionName:
    """Tests for _derive_config_section_name()."""

    def test_regular_name(self, command_mock_bot):
        cmd = _TestCommand(command_mock_bot)
        cmd.name = "dice"
        assert cmd._derive_config_section_name() == "Dice_Command"

    def test_camel_case_dadjoke(self, command_mock_bot):
        cmd = DadJokeCommand(command_mock_bot)
        assert cmd._derive_config_section_name() == "DadJoke_Command"

    def test_camel_case_webviewer(self, command_mock_bot):
        cmd = _TestCommand(command_mock_bot)
        cmd.name = "webviewer"
        assert cmd._derive_config_section_name() == "WebViewer_Command"


class TestIsChannelAllowed:
    """Tests for is_channel_allowed()."""

    def test_dm_always_allowed(self, command_mock_bot):
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(channel=None, is_dm=True)
        assert cmd.is_channel_allowed(msg) is True

    def test_channel_in_monitor_list_allowed(self, command_mock_bot):
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(channel="general", is_dm=False)
        assert cmd.is_channel_allowed(msg) is True

    def test_channel_not_in_monitor_list_rejected(self, command_mock_bot):
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(channel="unknown_channel", is_dm=False)
        assert cmd.is_channel_allowed(msg) is False

    def test_no_channel_rejected(self, command_mock_bot):
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(channel=None, is_dm=False)
        assert cmd.is_channel_allowed(msg) is False


class TestGetConfigValue:
    """Tests for get_config_value() section migration."""

    def test_new_section_used_first(self, command_mock_bot):
        command_mock_bot.config.add_section("Ping_Command")
        command_mock_bot.config.set("Ping_Command", "enabled", "true")
        cmd = PingCommand(command_mock_bot)
        assert cmd.get_config_value("Ping_Command", "enabled", fallback=False, value_type="bool") is True

    def test_legacy_section_migration(self, command_mock_bot):
        # Old [Hacker] section used when [Hacker_Command] not present
        command_mock_bot.config.add_section("Hacker")
        command_mock_bot.config.set("Hacker", "enabled", "true")
        cmd = _TestCommand(command_mock_bot)
        val = cmd.get_config_value("Hacker_Command", "enabled", fallback=False, value_type="bool")
        assert val is True

    def test_joke_command_enabled_standard(self, command_mock_bot):
        """Joke_Command uses enabled (standard) when present."""
        command_mock_bot.config.add_section("Joke_Command")
        command_mock_bot.config.set("Joke_Command", "enabled", "false")
        cmd = JokeCommand(command_mock_bot)
        assert cmd.joke_enabled is False

    def test_joke_command_joke_enabled_legacy(self, command_mock_bot):
        """Joke_Command falls back to joke_enabled when enabled absent."""
        command_mock_bot.config.add_section("Joke_Command")
        command_mock_bot.config.set("Joke_Command", "joke_enabled", "false")
        cmd = JokeCommand(command_mock_bot)
        assert cmd.joke_enabled is False

    def test_joke_command_legacy_jokes_section(self, command_mock_bot):
        """Joke_Command reads joke_enabled from legacy [Jokes] when enabled absent."""
        command_mock_bot.config.add_section("Jokes")
        command_mock_bot.config.set("Jokes", "joke_enabled", "false")
        cmd = JokeCommand(command_mock_bot)
        assert cmd.joke_enabled is False

    def test_dadjoke_command_enabled_standard(self, command_mock_bot):
        """DadJoke_Command uses enabled (standard) when present."""
        command_mock_bot.config.add_section("DadJoke_Command")
        command_mock_bot.config.set("DadJoke_Command", "enabled", "false")
        cmd = DadJokeCommand(command_mock_bot)
        assert cmd.dadjoke_enabled is False

    def test_dadjoke_command_dadjoke_enabled_legacy(self, command_mock_bot):
        """DadJoke_Command falls back to dadjoke_enabled when enabled absent."""
        command_mock_bot.config.add_section("DadJoke_Command")
        command_mock_bot.config.set("DadJoke_Command", "dadjoke_enabled", "false")
        cmd = DadJokeCommand(command_mock_bot)
        assert cmd.dadjoke_enabled is False

    def test_stats_command_enabled_standard(self, command_mock_bot_with_db):
        """Stats_Command uses enabled (standard) when present."""
        command_mock_bot_with_db.config.add_section("Stats_Command")
        command_mock_bot_with_db.config.set("Stats_Command", "enabled", "false")
        cmd = StatsCommand(command_mock_bot_with_db)
        assert cmd.stats_enabled is False

    def test_stats_command_stats_enabled_legacy(self, command_mock_bot_with_db):
        """Stats_Command falls back to stats_enabled when enabled absent."""
        command_mock_bot_with_db.config.add_section("Stats_Command")
        command_mock_bot_with_db.config.set("Stats_Command", "stats_enabled", "false")
        cmd = StatsCommand(command_mock_bot_with_db)
        assert cmd.stats_enabled is False

    def test_hacker_command_enabled_standard(self, command_mock_bot):
        """Hacker_Command uses enabled (standard) when present."""
        command_mock_bot.config.add_section("Hacker_Command")
        command_mock_bot.config.set("Hacker_Command", "enabled", "true")
        cmd = HackerCommand(command_mock_bot)
        assert cmd.enabled is True

    def test_hacker_command_hacker_enabled_legacy(self, command_mock_bot):
        """Hacker_Command falls back to hacker_enabled when enabled absent."""
        command_mock_bot.config.add_section("Hacker_Command")
        command_mock_bot.config.set("Hacker_Command", "hacker_enabled", "true")
        cmd = HackerCommand(command_mock_bot)
        assert cmd.enabled is True

    def test_sports_command_enabled_standard(self, command_mock_bot):
        """Sports_Command uses enabled (standard) when present."""
        command_mock_bot.config.add_section("Sports_Command")
        command_mock_bot.config.set("Sports_Command", "enabled", "false")
        cmd = SportsCommand(command_mock_bot)
        assert cmd.sports_enabled is False

    def test_sports_command_sports_enabled_legacy(self, command_mock_bot):
        """Sports_Command falls back to sports_enabled when enabled absent."""
        command_mock_bot.config.add_section("Sports_Command")
        command_mock_bot.config.set("Sports_Command", "sports_enabled", "false")
        cmd = SportsCommand(command_mock_bot)
        assert cmd.sports_enabled is False

    def test_alert_command_enabled_standard(self, command_mock_bot):
        """Alert_Command uses enabled (standard) when present."""
        command_mock_bot.config.add_section("Alert_Command")
        command_mock_bot.config.set("Alert_Command", "enabled", "false")
        cmd = AlertCommand(command_mock_bot)
        assert cmd.alert_enabled is False

    def test_alert_command_alert_enabled_legacy(self, command_mock_bot):
        """Alert_Command falls back to alert_enabled when enabled absent."""
        command_mock_bot.config.add_section("Alert_Command")
        command_mock_bot.config.set("Alert_Command", "alert_enabled", "false")
        cmd = AlertCommand(command_mock_bot)
        assert cmd.alert_enabled is False


class TestGetMaxMessageLength:
    """Tests for BaseCommand.get_max_message_length (UTF-8 OTA byte budgets, PR #128)."""

    def test_dm_returns_158_bytes(self, command_mock_bot):
        command_mock_bot.meshcore = None
        cmd = _TestCommand(command_mock_bot)
        msg = mock_message(content="x", channel="general", is_dm=True)
        assert cmd.get_max_message_length(msg) == 158

    def test_channel_uses_bot_name_utf8_bytes(self, command_mock_bot):
        command_mock_bot.meshcore = None
        command_mock_bot.config.set("Bot", "bot_name", "LongBotName")
        cmd = _TestCommand(command_mock_bot)
        msg = mock_message(content="x", channel="general", is_dm=False)
        assert cmd.get_max_message_length(msg) == 147  # 160 - 11 - 2

    def test_channel_prefers_meshcore_username_utf8_bytes(self, command_mock_bot):
        meshcore = Mock()
        meshcore.self_info = {"name": "Radio"}
        command_mock_bot.meshcore = meshcore
        command_mock_bot.config.set("Bot", "bot_name", "fallback")
        cmd = _TestCommand(command_mock_bot)
        msg = mock_message(content="x", channel="general", is_dm=False)
        assert cmd.get_max_message_length(msg) == 153  # 160 - 5 - 2

    def test_channel_unicode_username_uses_utf8_not_char_count(self, command_mock_bot):
        """Emoji radio name: 4 chars, 16 UTF-8 bytes — budget must use byte length."""
        meshcore = Mock()
        meshcore.self_info = {"name": "😀😀😀😀"}
        command_mock_bot.meshcore = meshcore
        cmd = _TestCommand(command_mock_bot)
        msg = mock_message(content="x", channel="general", is_dm=False)
        assert cmd.get_max_message_length(msg) == 142  # 160 - 16 - 2

    def test_channel_very_long_username_hits_130_byte_floor(self, command_mock_bot):
        command_mock_bot.meshcore = None
        command_mock_bot.config.set("Bot", "bot_name", "A" * 40)
        cmd = _TestCommand(command_mock_bot)
        msg = mock_message(content="x", channel="general", is_dm=False)
        assert cmd.get_max_message_length(msg) == 130  # max(130, 160 - 40 - 2)

    def test_channel_regional_reply_scope_reduces_budget_by_10_bytes(self, command_mock_bot):
        command_mock_bot.meshcore = None
        command_mock_bot.config.set("Bot", "bot_name", "LongBotName")
        cmd = _TestCommand(command_mock_bot)
        msg = mock_message(content="x", channel="general", is_dm=False, reply_scope="#west")
        assert cmd.get_max_message_length(msg) == 147 - CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD


class TestCanExecute:
    """Tests for can_execute()."""

    def test_channel_check_blocks_unknown_channel(self, command_mock_bot):
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(content="ping", channel="other", is_dm=False)
        assert cmd.can_execute(msg) is False

    def test_dm_allowed(self, command_mock_bot):
        command_mock_bot.config.add_section("Ping_Command")
        command_mock_bot.config.set("Ping_Command", "enabled", "true")
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(content="ping", is_dm=True)
        assert cmd.can_execute(msg) is True


class TestMentionHelpers:
    """Tests for BaseCommand mention-detection helper methods."""

    def _cmd(self, bot):
        bot.meshcore = None  # force bot name from config ("TestBot")
        return _TestCommand(bot)

    # _extract_mentions
    def test_extract_no_mentions(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._extract_mentions("hello world") == []

    def test_extract_single_mention(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._extract_mentions("@[Alice] hi") == ["Alice"]

    def test_extract_multiple_mentions(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._extract_mentions("@[Alice] and @[Bob]") == ["Alice", "Bob"]

    def test_extract_mention_with_spaces_in_name(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._extract_mentions("@[First Last] go") == ["First Last"]

    # _is_bot_mentioned
    def test_bot_mentioned_exact(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._is_bot_mentioned("@[TestBot] ping") is True

    def test_bot_mentioned_case_insensitive(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._is_bot_mentioned("@[testbot] ping") is True

    def test_bot_not_mentioned_other_user(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._is_bot_mentioned("@[Alice] ping") is False

    def test_bot_not_mentioned_no_mention(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._is_bot_mentioned("ping") is False

    # _check_mentions_ok
    def test_ok_no_mentions(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._check_mentions_ok("ping") is True

    def test_ok_bot_mentioned(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._check_mentions_ok("@[TestBot] ping") is True

    def test_not_ok_only_other_user(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._check_mentions_ok("@[Alice] ping") is False

    def test_ok_bot_and_other_user(self, command_mock_bot):
        # Bot is among mentions — should be OK
        assert self._cmd(command_mock_bot)._check_mentions_ok("@[TestBot] @[Alice] ping") is True

    # _strip_mentions
    def test_strip_single_mention(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._strip_mentions("@[Alice] hello") == "hello"

    def test_strip_multiple_mentions(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._strip_mentions("@[Alice] hello @[Bob]") == "hello"

    def test_strip_normalizes_whitespace(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._strip_mentions("@[Alice]   ping") == "ping"

    def test_strip_no_mention_unchanged(self, command_mock_bot):
        assert self._cmd(command_mock_bot)._strip_mentions("ping") == "ping"


class TestCleanupMessageForMatching:
    """Tests for BaseCommand.cleanup_message_for_matching() across all respond_to_mentions modes."""

    def _cmd(self, bot):
        bot.meshcore = None  # force bot name from config ("TestBot")
        return _TestCommand(bot)

    # ------------------------------------------------------------------ also --
    def test_also_no_mention_returns_content(self, command_mock_bot):
        """'also' (default): command responds even without a mention."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "also")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="testcmd")
        assert cmd.cleanup_message_for_matching(msg) == "testcmd"
        assert msg.content == "testcmd"

    def test_also_strips_bot_mention(self, command_mock_bot):
        """'also': @[bot] prefix is stripped before keyword matching."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "also")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="@[TestBot] testcmd")
        result = cmd.cleanup_message_for_matching(msg)
        assert result == "testcmd"
        assert msg.content == "testcmd"

    def test_also_strips_bot_mention_case_insensitive(self, command_mock_bot):
        """'also': bot mention matching is case-insensitive."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "also")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="@[testbot] testcmd")
        assert cmd.cleanup_message_for_matching(msg) == "testcmd"

    def test_also_blocks_other_user_mention(self, command_mock_bot):
        """'also': if only someone else is mentioned (not bot), return empty string."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "also")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="@[Alice] testcmd")
        assert cmd.cleanup_message_for_matching(msg) == ""

    def test_also_updates_message_content_and_lower(self, command_mock_bot):
        """'also': message.content and message.content_lower are updated after stripping."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "also")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="@[TestBot] TESTCMD")
        cmd.cleanup_message_for_matching(msg)
        assert msg.content == "TESTCMD"
        assert msg.content_lower == "testcmd"

    # ------------------------------------------------------------------ only --
    def test_only_with_mention_strips_and_returns(self, command_mock_bot):
        """'only': responds when bot is mentioned; strips the mention."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "only")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="@[TestBot] testcmd")
        result = cmd.cleanup_message_for_matching(msg)
        assert result == "testcmd"
        assert msg.content == "testcmd"

    def test_only_plain_message_not_gated_here(self, command_mock_bot):
        """'only': cleanup_message_for_matching does NOT gate plain (unmention'd) messages —
        the 'only' require-mention gate is enforced upstream in process_message."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "only")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="testcmd")
        # No mentions present → _check_mentions_ok returns True → content passes through
        assert cmd.cleanup_message_for_matching(msg) == "testcmd"

    def test_only_other_user_mention_returns_empty(self, command_mock_bot):
        """'only': another user mentioned but not bot → blocked."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "only")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="@[Alice] testcmd")
        assert cmd.cleanup_message_for_matching(msg) == ""

    # ------------------------------------------------------------------ false --
    def test_false_mention_not_stripped(self, command_mock_bot):
        """'false': no mention logic; mention is NOT stripped from content."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "false")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="@[TestBot] testcmd")
        result = cmd.cleanup_message_for_matching(msg)
        assert "@[testbot]" in result

    def test_false_other_user_mention_not_blocked(self, command_mock_bot):
        """'false': another user mentioned — bot still processes (no filtering)."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "false")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="@[Alice] testcmd")
        result = cmd.cleanup_message_for_matching(msg)
        assert "@[alice]" in result

    def test_false_plain_message_works(self, command_mock_bot):
        """'false': plain messages work normally."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "false")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="testcmd")
        assert cmd.cleanup_message_for_matching(msg) == "testcmd"

    # ---------------------------------------------------------------- prefix -
    def test_strips_legacy_bang_prefix(self, command_mock_bot):
        """No command_prefix configured: legacy '!' is stripped."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "false")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="!testcmd")
        assert cmd.cleanup_message_for_matching(msg) == "testcmd"

    def test_wrong_command_prefix_returns_empty(self, command_mock_bot):
        """Configured command_prefix mismatch → empty string (no match)."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "false")
        command_mock_bot.config.set("Bot", "command_prefix", "!")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="testcmd")  # missing the ! prefix
        assert cmd.cleanup_message_for_matching(msg) == ""

    # ---------------------------------------------- matches_keyword integration
    def test_matches_keyword_with_bot_mention(self, command_mock_bot):
        """matches_keyword uses cleanup_message_for_matching — @[bot] ping matches 'testcmd'."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "also")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="@[TestBot] testcmd")
        assert cmd.matches_keyword(msg) is True

    def test_matches_keyword_other_mention_blocked(self, command_mock_bot):
        """matches_keyword returns False when only another user is mentioned."""
        command_mock_bot.config.set("Bot", "respond_to_mentions", "also")
        cmd = self._cmd(command_mock_bot)
        msg = mock_message(content="@[Alice] testcmd")
        assert cmd.matches_keyword(msg) is False
