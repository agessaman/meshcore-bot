"""Tests for modules.command_manager."""

import time
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.command_manager import CommandManager, InternetStatusCache
from modules.models import MeshMessage
from tests.conftest import mock_message


@pytest.fixture
def cm_bot(mock_logger):
    """Mock bot for CommandManager tests."""
    bot = Mock()
    bot.logger = mock_logger
    bot.bot_root = Path("/tmp")
    bot._local_root = None  # Use bot_root / local / commands in CommandManager
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
    bot.is_radio_zombie = False
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
        manager.get_help_for_command("nonexistent")
        # Translator receives 'commands.help.unknown' key with command name
        cm_bot.translator.translate.assert_called()
        call_args = cm_bot.translator.translate.call_args
        assert call_args[0][0] == "commands.help.unknown"
        assert call_args[1]["command"] == "nonexistent"

    def test_keyword_mapping_alias_resolves_command(self, cm_bot):
        mock_cmd = MagicMock()
        mock_cmd.keywords = ["schedule"]
        mock_cmd.get_help_text = Mock(return_value="Schedule help")
        manager = make_manager(cm_bot, commands={"schedule": mock_cmd})
        manager.plugin_loader.keyword_mappings = {"sched": "schedule"}
        result = manager.get_help_for_command("sched")
        assert "Schedule help" in result

    def test_runtime_alias_in_keywords_resolves_command(self, cm_bot):
        mock_cmd = MagicMock()
        mock_cmd.keywords = ["schedule", "sched"]
        mock_cmd.get_help_text = Mock(return_value="Schedule help")
        manager = make_manager(cm_bot, commands={"schedule": mock_cmd})
        manager.plugin_loader.keyword_mappings = {}
        result = manager.get_help_for_command("sched")
        assert "Schedule help" in result


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


class TestSendChannelMessageListeners:
    """Tests for channel_sent_listeners invocation when bot sends a channel message."""

    @pytest.mark.asyncio
    async def test_successful_send_invokes_listeners_with_synthetic_event(self, cm_bot, mock_logger):
        """When send_channel_message succeeds, each channel_sent_listener is called with event.payload shape (channel_idx, text)."""
        import asyncio

        from meshcore import EventType

        cm_bot.connected = True
        cm_bot.channel_manager = Mock()
        cm_bot.channel_manager.get_channel_number = Mock(return_value=3)
        cm_bot.meshcore = Mock()
        cm_bot.meshcore.commands = Mock()
        cm_bot.meshcore.commands.send_chan_msg = AsyncMock(return_value=Mock(type=EventType.MSG_SENT, payload=None))
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)
        cm_bot.channel_sent_listeners = []
        received = []

        async def capture_listener(event, metadata=None):
            received.append(getattr(event, 'payload', None))

        cm_bot.channel_sent_listeners.append(capture_listener)

        created_tasks = []

        with patch("modules.command_manager.asyncio.create_task") as mock_create_task:
            def capture_and_run(coro):
                t = asyncio.get_event_loop().create_task(coro)
                created_tasks.append(t)
                return t

            mock_create_task.side_effect = capture_and_run

            manager = make_manager(cm_bot)
            result = await manager.send_channel_message("general", "Hello mesh")

            for t in created_tasks:
                await t

        assert result is True
        assert len(received) == 1
        assert received[0] == {"channel_idx": 3, "text": "TestBot: Hello mesh"}

    @pytest.mark.asyncio
    async def test_failed_send_does_not_invoke_listeners(self, cm_bot):
        """When send_channel_message fails (e.g. channel not found), listeners are not called."""
        cm_bot.connected = True
        cm_bot.channel_manager = Mock()
        cm_bot.channel_manager.get_channel_number = Mock(return_value=None)
        cm_bot.channel_sent_listeners = []
        received = []

        async def capture_listener(event, metadata=None):
            received.append(getattr(event, 'payload', None))

        cm_bot.channel_sent_listeners.append(capture_listener)

        manager = make_manager(cm_bot)
        result = await manager.send_channel_message("nonexistent", "Hi")

        assert result is False
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_no_listeners_no_error(self, cm_bot):
        """When channel_sent_listeners is missing or empty, send_channel_message still returns success."""
        from meshcore import EventType

        cm_bot.connected = True
        cm_bot.channel_manager = Mock()
        cm_bot.channel_manager.get_channel_number = Mock(return_value=1)
        cm_bot.meshcore = Mock()
        cm_bot.meshcore.commands = Mock()
        cm_bot.meshcore.commands.send_chan_msg = AsyncMock(return_value=Mock(type=EventType.MSG_SENT, payload=None))
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)
        cm_bot.channel_sent_listeners = []

        manager = make_manager(cm_bot)
        result = await manager.send_channel_message("general", "Hi")

        assert result is True


class TestSendChannelMessagesChunked:
    """Tests for send_channel_messages_chunked."""

    @pytest.mark.asyncio
    async def test_empty_chunks_returns_true_without_send(self, cm_bot):
        """Empty chunks returns True and does not call send_channel_message."""
        manager = make_manager(cm_bot)
        manager.send_channel_message = AsyncMock(return_value=True)
        result = await manager.send_channel_messages_chunked("general", [])
        assert result is True
        manager.send_channel_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_chunk_calls_send_once_no_wait(self, cm_bot):
        """Single chunk calls send_channel_message once; no wait_for_tx or sleep."""
        cm_bot.config.set("Bot", "bot_tx_rate_limit_seconds", "1.0")
        manager = make_manager(cm_bot)
        manager.send_channel_message = AsyncMock(return_value=True)
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)

        with patch("modules.command_manager.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await manager.send_channel_messages_chunked("general", ["only one"])

        assert result is True
        assert manager.send_channel_message.call_count == 1
        cm_bot.bot_tx_rate_limiter.wait_for_tx.assert_not_called()
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_chunks_waits_and_sleeps_between(self, cm_bot):
        """Multiple chunks call send_channel_message per chunk; wait_for_tx and sleep between."""
        cm_bot.config.set("Bot", "bot_tx_rate_limit_seconds", "1.0")
        manager = make_manager(cm_bot)
        manager.send_channel_message = AsyncMock(return_value=True)
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)

        with patch("modules.command_manager.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await manager.send_channel_messages_chunked("general", ["a", "b", "c"])

        assert result is True
        assert manager.send_channel_message.call_count == 3
        assert cm_bot.bot_tx_rate_limiter.wait_for_tx.call_count == 2
        assert mock_sleep.await_count == 2

    @pytest.mark.asyncio
    async def test_chunked_first_uses_provided_rate_limit_args_subsequent_skip(self, cm_bot):
        """First chunk uses provided skip_user_rate_limit/rate_limit_key; subsequent use True/None."""
        cm_bot.config.set("Bot", "bot_tx_rate_limit_seconds", "1.0")
        manager = make_manager(cm_bot)
        manager.send_channel_message = AsyncMock(return_value=True)
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)

        with patch("modules.command_manager.asyncio.sleep", new_callable=AsyncMock):
            await manager.send_channel_messages_chunked(
                "general",
                ["first", "second"],
                skip_user_rate_limit=False,
                rate_limit_key="user123",
            )

        calls = manager.send_channel_message.call_args_list
        assert len(calls) == 2
        # First call: skip_user_rate_limit=False, rate_limit_key="user123"
        assert calls[0][1]["skip_user_rate_limit"] is False
        assert calls[0][1]["rate_limit_key"] == "user123"
        # Second call: skip_user_rate_limit=True, rate_limit_key=None
        assert calls[1][1]["skip_user_rate_limit"] is True
        assert calls[1][1]["rate_limit_key"] is None

    @pytest.mark.asyncio
    async def test_chunked_returns_false_on_first_send_failure(self, cm_bot):
        """When first send_channel_message returns False, chunked returns False and does not send rest."""
        cm_bot.config.set("Bot", "bot_tx_rate_limit_seconds", "1.0")
        manager = make_manager(cm_bot)
        manager.send_channel_message = AsyncMock(side_effect=[False, True])  # first fails
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)

        with patch("modules.command_manager.asyncio.sleep", new_callable=AsyncMock):
            result = await manager.send_channel_messages_chunked("general", ["a", "b"])

        assert result is False
        manager.send_channel_message.assert_called_once()


# ---------------------------------------------------------------------------
# TestCommandAliases (per-command config)
# ---------------------------------------------------------------------------


class TestCommandAliases:
    """Tests for per-command aliases via BaseCommand._load_aliases_from_config()."""

    def _make_command(self, bot, section, aliases_value=None):
        """Create a minimal concrete BaseCommand subclass with aliases config."""
        if not bot.config.has_section(section):
            bot.config.add_section(section)
        if aliases_value is not None:
            bot.config.set(section, "aliases", aliases_value)

        from modules.commands.base_command import BaseCommand

        class _Cmd(BaseCommand):
            name = section.lower().replace("_command", "")
            keywords: list = [name]
            description = "test"

            async def execute(self, message):  # type: ignore[override]
                return True

        return _Cmd(bot)

    def test_alias_added_to_keywords_without_legacy_prefix(self, cm_bot):
        cmd = self._make_command(cm_bot, "Schedule_Command", "!s, !sched")
        assert "s" in cmd.keywords
        assert "sched" in cmd.keywords

    def test_no_aliases_key_leaves_keywords_unchanged(self, cm_bot):
        cmd = self._make_command(cm_bot, "Schedule_Command")
        assert cmd.keywords == ["schedule"]

    def test_empty_aliases_value_leaves_keywords_unchanged(self, cm_bot):
        cmd = self._make_command(cm_bot, "Schedule_Command", "")
        assert cmd.keywords == ["schedule"]

    def test_alias_already_present_not_duplicated(self, cm_bot):
        cmd = self._make_command(cm_bot, "Schedule_Command", "schedule, !s")
        assert cmd.keywords.count("schedule") == 1
        assert "s" in cmd.keywords

    def test_aliases_lowercased(self, cm_bot):
        cmd = self._make_command(cm_bot, "Schedule_Command", "!S, !Sched")
        assert "s" in cmd.keywords
        assert "sched" in cmd.keywords

    def test_alias_with_configured_prefix_is_normalized(self, cm_bot):
        cm_bot.config.set("Bot", "command_prefix", "!")
        cmd = self._make_command(cm_bot, "Schedule_Command", "!S")
        assert "s" in cmd.keywords

    def test_decorative_dot_prefix_stripped_without_command_prefix(self, cm_bot):
        cm_bot.config.set("Bot", "command_prefix", "")
        cmd = self._make_command(cm_bot, "Schedule_Command", ".sched")
        assert "sched" in cmd.keywords


class TestSendChannelMessageRetry:
    """Tests for no_event_received retry logic in send_channel_message (BUG-025)."""

    def _make_no_event_result(self):
        """Return a mock result that looks like EventType.ERROR / no_event_received."""
        from meshcore import EventType
        r = MagicMock()
        r.type = EventType.ERROR
        r.payload = {'reason': 'no_event_received'}
        return r

    def _make_success_result(self):
        from meshcore import EventType
        r = MagicMock()
        r.type = EventType.MSG_SENT
        r.payload = None
        return r

    def _setup_bot(self, cm_bot):
        cm_bot.connected = True
        cm_bot.is_radio_zombie = False
        cm_bot.channel_manager = Mock()
        cm_bot.channel_manager.get_channel_number = Mock(return_value=2)
        cm_bot.meshcore = Mock()
        cm_bot.meshcore.commands = Mock()
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)
        cm_bot.channel_sent_listeners = []
        return cm_bot

    @pytest.mark.asyncio
    async def test_success_on_first_attempt_no_retry(self, cm_bot):
        """No retry when first attempt succeeds."""
        self._setup_bot(cm_bot)
        cm_bot.meshcore.commands.send_chan_msg = AsyncMock(
            return_value=self._make_success_result()
        )
        manager = make_manager(cm_bot)
        with patch("modules.command_manager.asyncio.sleep") as mock_sleep:
            result = await manager.send_channel_message("general", "hi")
        assert result is True
        mock_sleep.assert_not_called()
        assert cm_bot.meshcore.commands.send_chan_msg.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_no_event_received_then_succeeds(self, cm_bot):
        """Retries up to 2 times when no_event_received; succeeds on 3rd attempt."""
        self._setup_bot(cm_bot)
        cm_bot.meshcore.commands.send_chan_msg = AsyncMock(
            side_effect=[
                self._make_no_event_result(),
                self._make_no_event_result(),
                self._make_success_result(),
            ]
        )
        manager = make_manager(cm_bot)
        with patch("modules.command_manager.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await manager.send_channel_message("testing", "hello")
        assert result is True
        assert cm_bot.meshcore.commands.send_chan_msg.call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(2)

    @pytest.mark.asyncio
    async def test_all_attempts_fail_returns_false(self, cm_bot):
        """Returns False when all 3 attempts (initial + 2 retries) get no_event_received."""
        self._setup_bot(cm_bot)
        cm_bot.meshcore.commands.send_chan_msg = AsyncMock(
            return_value=self._make_no_event_result()
        )
        manager = make_manager(cm_bot)
        with patch("modules.command_manager.asyncio.sleep", new_callable=AsyncMock):
            result = await manager.send_channel_message("testing", "hello")
        assert result is False
        assert cm_bot.meshcore.commands.send_chan_msg.call_count == 3

    @pytest.mark.asyncio
    async def test_is_no_event_received_helper(self, cm_bot):
        """_is_no_event_received returns True only for ERROR/no_event_received."""
        from meshcore import EventType
        manager = make_manager(cm_bot)

        no_event = self._make_no_event_result()
        assert manager._is_no_event_received(no_event) is True

        success = self._make_success_result()
        assert manager._is_no_event_received(success) is False

        assert manager._is_no_event_received(None) is False

        other_error = MagicMock()
        other_error.type = EventType.ERROR
        other_error.payload = {'reason': 'timeout'}
        assert manager._is_no_event_received(other_error) is False

    @pytest.mark.asyncio
    async def test_retry_only_fires_once_when_second_attempt_succeeds(self, cm_bot):
        """Only one retry (sleep) when second attempt succeeds."""
        self._setup_bot(cm_bot)
        cm_bot.meshcore.commands.send_chan_msg = AsyncMock(
            side_effect=[
                self._make_no_event_result(),
                self._make_success_result(),
            ]
        )
        manager = make_manager(cm_bot)
        with patch("modules.command_manager.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await manager.send_channel_message("general", "msg")
        assert result is True
        assert cm_bot.meshcore.commands.send_chan_msg.call_count == 2
        assert mock_sleep.call_count == 1


class TestSplitTextIntoChunks:
    """Tests for CommandManager.split_text_into_chunks."""

    def test_short_text_single_chunk(self):
        result = CommandManager.split_text_into_chunks("hello", 150)
        assert result == ["hello"]

    def test_empty_string(self):
        result = CommandManager.split_text_into_chunks("", 150)
        assert result == [""]

    def test_exact_limit_single_chunk(self):
        text = "a" * 150
        result = CommandManager.split_text_into_chunks(text, 150)
        assert result == [text]

    def test_double_limit_two_chunks(self):
        # 300 chars, limit 150 → 2 chunks
        word = "word "  # 5 chars
        text = word * 60  # 300 chars, space-separated
        result = CommandManager.split_text_into_chunks(text.strip(), 150)
        assert len(result) == 2
        assert all(len(c) <= 150 for c in result)
        assert " ".join(result) == text.strip()

    def test_five_times_limit_five_chunks(self):
        # Construct text that is ~750 chars worth of space-separated words
        word = "xy "  # 3 chars
        text = (word * 250).strip()  # 749 chars
        result = CommandManager.split_text_into_chunks(text, 150)
        assert len(result) == 5
        assert all(len(c) <= 150 for c in result)
        # Reassembling (space join) should equal original
        assert " ".join(result) == text

    def test_no_content_dropped(self):
        # Every character in original text must appear in exactly one chunk
        import random
        import string
        random.seed(42)
        words = ["".join(random.choices(string.ascii_lowercase, k=random.randint(3, 12))) for _ in range(60)]
        text = " ".join(words)
        chunks = CommandManager.split_text_into_chunks(text, 50)
        assert all(len(c) <= 50 for c in chunks)
        reassembled = " ".join(chunks)
        assert reassembled == text

    def test_hard_split_no_spaces(self):
        text = "a" * 300
        result = CommandManager.split_text_into_chunks(text, 100)
        assert len(result) == 3
        assert all(len(c) == 100 for c in result)

    def test_max_len_one(self):
        result = CommandManager.split_text_into_chunks("abc", 1)
        assert len(result) == 3
        assert all(len(c) == 1 for c in result)


class TestGetMaxMessageLength:
    """Tests for CommandManager.get_max_message_length."""

    def _make_manager(self, bot_name: str = "Bot", username: str | None = None) -> CommandManager:
        bot = Mock()
        bot.logger = Mock()
        bot.bot_root = Path("/tmp")
        bot._local_root = None
        bot.config = ConfigParser()
        bot.config.add_section("Bot")
        bot.config.set("Bot", "bot_name", bot_name)
        bot.config.add_section("Channels")
        bot.config.set("Channels", "monitor_channels", "general")
        bot.config.set("Channels", "respond_to_dms", "true")
        bot.config.add_section("Keywords")
        if username is not None:
            self_info = {"name": username}
            meshcore = Mock()
            meshcore.self_info = self_info
            bot.meshcore = meshcore
        else:
            bot.meshcore = None
        bot.translator = Mock()
        bot.translator.translate = Mock(return_value="")
        return make_manager(bot)

    def test_dm_returns_158_bytes(self):
        mgr = self._make_manager()
        msg = MeshMessage(content="x", is_dm=True)
        assert mgr.get_max_message_length(msg) == 158

    def test_channel_uses_bot_name_utf8_bytes(self):
        mgr = self._make_manager(bot_name="LongBotName")
        msg = MeshMessage(content="x", channel="general", is_dm=False)
        # 160 - utf8("LongBotName") - 2 = 160 - 11 - 2 = 147
        assert mgr.get_max_message_length(msg) == 147

    def test_channel_uses_meshcore_username_utf8_bytes(self):
        mgr = self._make_manager(bot_name="fallback", username="Radio")
        msg = MeshMessage(content="x", channel="general", is_dm=False)
        # 160 - utf8("Radio") - 2 = 160 - 5 - 2 = 153
        assert mgr.get_max_message_length(msg) == 153

    def test_channel_regional_reply_scope_reduces_budget_by_10_bytes(self):
        mgr = self._make_manager(bot_name="LongBotName")
        msg = MeshMessage(content="x", channel="general", is_dm=False, reply_scope="#west")
        assert mgr.get_max_message_length(msg) == 137  # 147 - 10

    def test_channel_outgoing_flood_scope_override_reduces_budget_by_10_bytes(self):
        mgr = self._make_manager(bot_name="LongBotName")
        mgr.bot.config.set("Channels", "outgoing_flood_scope_override", "#west")
        msg = MeshMessage(content="x", channel="general", is_dm=False)
        assert mgr.get_max_message_length(msg) == 137

    def test_parity_with_base_command_get_max_message_length(self):
        """CommandManager must mirror BaseCommand byte budgets (PR #128)."""
        from tests.commands.test_base_command import _TestCommand

        cases: list[tuple[str, str | None, bool, str | None]] = [
            ("LongBotName", None, False, None),
            ("Bot", None, True, None),
            ("fallback", "Radio", False, None),
            ("x", "😀😀", False, None),
            ("LongBotName", None, False, "#west"),
        ]
        for bot_name, username, is_dm, reply_scope in cases:
            mgr = self._make_manager(bot_name=bot_name, username=username)
            cmd = _TestCommand(mgr.bot)
            msg = MeshMessage(
                content="x",
                channel=None if is_dm else "general",
                is_dm=is_dm,
                reply_scope=reply_scope,
            )
            m_len = mgr.get_max_message_length(msg)
            b_len = cmd.get_max_message_length(msg)
            assert m_len == b_len, (bot_name, username, is_dm, reply_scope, m_len, b_len)
