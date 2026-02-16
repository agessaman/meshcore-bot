"""Tests for modules.command_manager."""

import time

import pytest
from configparser import ConfigParser
from unittest.mock import Mock, MagicMock, patch, AsyncMock

from modules.command_manager import CommandManager, InternetStatusCache
from modules.models import MeshMessage
from tests.conftest import mock_message


@pytest.fixture
def cm_bot(mock_logger):
    """Mock bot for CommandManager tests."""
    bot = Mock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "bot_name", "TestBot")
    bot.config.add_section("Channels")
    bot.config.set("Channels", "monitor_channels", "general,test")
    bot.config.set("Channels", "respond_to_dms", "true")
    bot.config.add_section("Keywords")
    bot.config.set("Keywords", "ping", "Pong!")
    bot.config.set("Keywords", "test", "ack")
    bot.translator = Mock()
    # Translator returns "key: kwarg_values" so assertions can check content
    bot.translator.translate = Mock(
        side_effect=lambda key, **kw: f"{key}: {' '.join(str(v) for v in kw.values())}"
    )
    bot.meshcore = None
    bot.rate_limiter = Mock()
    bot.rate_limiter.can_send = Mock(return_value=True)
    bot.bot_tx_rate_limiter = Mock()
    bot.bot_tx_rate_limiter.wait_for_tx = Mock()
    bot.tx_delay_ms = 0
    return bot


def make_manager(bot, commands=None):
    """Create CommandManager with mocked PluginLoader."""
    with patch("modules.command_manager.PluginLoader") as mock_loader_class:
        mock_loader = Mock()
        mock_loader.load_all_plugins = Mock(return_value=commands or {})
        mock_loader_class.return_value = mock_loader
        return CommandManager(bot)


class TestLoadKeywords:
    """Tests for keyword loading from config."""

    def test_load_keywords_from_config(self, cm_bot):
        manager = make_manager(cm_bot)
        assert manager.keywords["ping"] == "Pong!"
        assert manager.keywords["test"] == "ack"

    def test_load_keywords_strips_quotes(self, cm_bot):
        cm_bot.config.set("Keywords", "quoted", '"Hello World"')
        manager = make_manager(cm_bot)
        assert manager.keywords["quoted"] == "Hello World"

    def test_load_keywords_decodes_escapes(self, cm_bot):
        cm_bot.config.set("Keywords", "multiline", r"Line1\nLine2")
        manager = make_manager(cm_bot)
        assert "\n" in manager.keywords["multiline"]

    def test_load_keywords_empty_section(self, cm_bot):
        cm_bot.config.remove_section("Keywords")
        cm_bot.config.add_section("Keywords")
        manager = make_manager(cm_bot)
        assert manager.keywords == {}


class TestLoadBannedUsers:
    """Tests for banned users loading."""

    def test_load_banned_users_from_config(self, cm_bot):
        cm_bot.config.add_section("Banned_Users")
        cm_bot.config.set("Banned_Users", "banned_users", "BadUser1, BadUser2")
        manager = make_manager(cm_bot)
        assert "BadUser1" in manager.banned_users
        assert "BadUser2" in manager.banned_users

    def test_load_banned_users_empty(self, cm_bot):
        manager = make_manager(cm_bot)
        assert manager.banned_users == []

    def test_load_banned_users_whitespace_handling(self, cm_bot):
        cm_bot.config.add_section("Banned_Users")
        cm_bot.config.set("Banned_Users", "banned_users", "  user1 , user2  ")
        manager = make_manager(cm_bot)
        assert "user1" in manager.banned_users
        assert "user2" in manager.banned_users


class TestIsUserBanned:
    """Tests for ban checking logic."""

    def test_exact_match(self, cm_bot):
        cm_bot.config.add_section("Banned_Users")
        cm_bot.config.set("Banned_Users", "banned_users", "BadUser")
        manager = make_manager(cm_bot)
        assert manager.is_user_banned("BadUser") is True

    def test_prefix_match(self, cm_bot):
        cm_bot.config.add_section("Banned_Users")
        cm_bot.config.set("Banned_Users", "banned_users", "BadUser")
        manager = make_manager(cm_bot)
        assert manager.is_user_banned("BadUser 123") is True

    def test_no_match(self, cm_bot):
        cm_bot.config.add_section("Banned_Users")
        cm_bot.config.set("Banned_Users", "banned_users", "BadUser")
        manager = make_manager(cm_bot)
        assert manager.is_user_banned("GoodUser") is False

    def test_none_sender(self, cm_bot):
        manager = make_manager(cm_bot)
        assert manager.is_user_banned(None) is False


class TestChannelTriggerAllowed:
    """Tests for _is_channel_trigger_allowed."""

    def test_dm_always_allowed(self, cm_bot):
        cm_bot.config.set("Channels", "channel_keywords", "ping")
        manager = make_manager(cm_bot)
        msg = mock_message(content="wx", is_dm=True)
        assert manager._is_channel_trigger_allowed("wx", msg) is True

    def test_none_whitelist_allows_all(self, cm_bot):
        manager = make_manager(cm_bot)
        assert manager.channel_keywords is None
        msg = mock_message(content="anything", channel="general", is_dm=False)
        assert manager._is_channel_trigger_allowed("anything", msg) is True

    def test_whitelist_allows_listed(self, cm_bot):
        cm_bot.config.set("Channels", "channel_keywords", "ping, help")
        manager = make_manager(cm_bot)
        msg = mock_message(content="ping", channel="general", is_dm=False)
        assert manager._is_channel_trigger_allowed("ping", msg) is True

    def test_whitelist_blocks_unlisted(self, cm_bot):
        cm_bot.config.set("Channels", "channel_keywords", "ping, help")
        manager = make_manager(cm_bot)
        msg = mock_message(content="wx", channel="general", is_dm=False)
        assert manager._is_channel_trigger_allowed("wx", msg) is False


class TestLoadMonitorChannels:
    """Tests for monitor channels loading."""

    def test_load_monitor_channels(self, cm_bot):
        manager = make_manager(cm_bot)
        assert "general" in manager.monitor_channels
        assert "test" in manager.monitor_channels
        assert len(manager.monitor_channels) == 2

    def test_load_monitor_channels_empty(self, cm_bot):
        cm_bot.config.set("Channels", "monitor_channels", "")
        manager = make_manager(cm_bot)
        assert manager.monitor_channels == []

    def test_load_monitor_channels_quoted(self, cm_bot):
        """Quoted monitor_channels (e.g. \"#bot,#bot-everett,#bots\") is supported."""
        cm_bot.config.set("Channels", "monitor_channels", '"#bot,#bot-everett,#bots"')
        manager = make_manager(cm_bot)
        assert manager.monitor_channels == ["#bot", "#bot-everett", "#bots"]


class TestLoadChannelKeywords:
    """Tests for channel keyword whitelist loading."""

    def test_load_channel_keywords_returns_list(self, cm_bot):
        cm_bot.config.set("Channels", "channel_keywords", "ping, wx, help")
        manager = make_manager(cm_bot)
        assert isinstance(manager.channel_keywords, list)
        assert "ping" in manager.channel_keywords
        assert "wx" in manager.channel_keywords
        assert "help" in manager.channel_keywords

    def test_load_channel_keywords_empty_returns_none(self, cm_bot):
        cm_bot.config.set("Channels", "channel_keywords", "")
        manager = make_manager(cm_bot)
        assert manager.channel_keywords is None

    def test_load_channel_keywords_not_set_returns_none(self, cm_bot):
        manager = make_manager(cm_bot)
        assert manager.channel_keywords is None


class TestCheckKeywords:
    """Tests for check_keywords() message matching."""

    def test_exact_keyword_match(self, cm_bot):
        manager = make_manager(cm_bot)
        msg = mock_message(content="ping", channel="general", is_dm=False)
        matches = manager.check_keywords(msg)
        assert any(trigger == "ping" for trigger, _ in matches)

    def test_prefix_required_blocks_bare_keyword(self, cm_bot):
        cm_bot.config.set("Bot", "command_prefix", "!")
        manager = make_manager(cm_bot)
        msg = mock_message(content="ping", channel="general", is_dm=False)
        matches = manager.check_keywords(msg)
        assert len(matches) == 0

    def test_prefix_matches(self, cm_bot):
        cm_bot.config.set("Bot", "command_prefix", "!")
        manager = make_manager(cm_bot)
        msg = mock_message(content="!ping", channel="general", is_dm=False)
        matches = manager.check_keywords(msg)
        assert any(trigger == "ping" for trigger, _ in matches)

    def test_wrong_channel_no_match(self, cm_bot):
        manager = make_manager(cm_bot)
        msg = mock_message(content="ping", channel="other", is_dm=False)
        matches = manager.check_keywords(msg)
        assert len(matches) == 0

    def test_dm_allowed(self, cm_bot):
        manager = make_manager(cm_bot)
        msg = mock_message(content="ping", is_dm=True)
        matches = manager.check_keywords(msg)
        assert any(trigger == "ping" for trigger, _ in matches)

    def test_help_routing(self, cm_bot):
        manager = make_manager(cm_bot)
        msg = mock_message(content="help", is_dm=True)
        matches = manager.check_keywords(msg)
        assert any(trigger == "help" for trigger, _ in matches)


class TestGetHelpForCommand:
    """Tests for command-specific help."""

    def test_known_command_returns_help(self, cm_bot):
        mock_cmd = MagicMock()
        mock_cmd.keywords = ["wx"]
        mock_cmd.get_help_text = Mock(return_value="Weather forecast info")
        mock_cmd.dm_only = False
        mock_cmd.requires_internet = False
        manager = make_manager(cm_bot, commands={"wx": mock_cmd})
        result = manager.get_help_for_command("wx")
        # Translator receives help_text as kwarg, so it appears in the output
        assert "Weather forecast info" in result
        # Verify translator was called with the right key
        cm_bot.translator.translate.assert_called_with(
            "commands.help.specific", command="wx", help_text="Weather forecast info"
        )

    def test_unknown_command_returns_error(self, cm_bot):
        manager = make_manager(cm_bot)
        result = manager.get_help_for_command("nonexistent")
        # Translator receives 'commands.help.unknown' key with command name
        cm_bot.translator.translate.assert_called()
        call_args = cm_bot.translator.translate.call_args
        assert call_args[0][0] == "commands.help.unknown"
        assert call_args[1]["command"] == "nonexistent"


class TestInternetStatusCache:
    """Tests for InternetStatusCache."""

    def test_is_valid_fresh(self):
        cache = InternetStatusCache(has_internet=True, timestamp=time.time())
        assert cache.is_valid(30) is True

    def test_is_valid_stale(self):
        cache = InternetStatusCache(has_internet=True, timestamp=time.time() - 60)
        assert cache.is_valid(30) is False

    def test_get_lock_lazy_creation(self):
        cache = InternetStatusCache(has_internet=True, timestamp=0)
        assert cache._lock is None
        lock1 = cache._get_lock()
        lock2 = cache._get_lock()
        assert lock1 is lock2
