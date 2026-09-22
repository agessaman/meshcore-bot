"""Tests for modules.command_manager."""

import re
import time
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.command_manager import CommandManager, InternetStatusCache
from modules.models import CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD, MeshMessage
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
    bot.is_radio_zombie = False
    bot.is_radio_offline = False
    bot.rate_limiter = Mock()
    bot.rate_limiter.can_send = Mock(return_value=True)
    bot.bot_tx_rate_limiter = Mock()
    bot.bot_tx_rate_limiter.wait_for_tx = Mock()
    bot.tx_delay_ms = 0
    bot.is_radio_zombie = False
    return bot


def _strip_part_suffix(text: str) -> str:
    """Drop a trailing " (i/n)" ordering marker so content can be compared."""
    return re.sub(r" \(\d+/\d+\)$", "", text)


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

    def test_help_subcommand_routes_to_base_command_with_full_message(self, cm_bot):
        net_cmd = MagicMock()
        net_cmd.keywords = ["net"]
        net_cmd.get_help_text = Mock(return_value="Network help")
        manager = make_manager(cm_bot, commands={"net": net_cmd})
        message = mock_message(content="help net create", is_dm=True)

        matches = manager.check_keywords(message)

        assert any(trigger == "help" and "Network help" in response for trigger, response in matches)
        net_cmd.get_help_text.assert_called_once_with(message)
        assert message.content == "help net create"

    def test_help_routing_preserves_exact_multiword_alias(self, cm_bot):
        dadjoke_cmd = MagicMock()
        dadjoke_cmd.keywords = ["dadjoke", "dad joke"]
        dadjoke_cmd.get_help_text = Mock(return_value="Dad joke help")
        unrelated_cmd = MagicMock()
        unrelated_cmd.keywords = ["dad"]
        unrelated_cmd.get_help_text = Mock(return_value="Wrong help")
        manager = make_manager(
            cm_bot,
            commands={"dadjoke": dadjoke_cmd, "dad": unrelated_cmd},
        )

        matches = manager.check_keywords(mock_message(content="help dad joke", is_dm=True))

        assert any(trigger == "help" and "Dad joke help" in response for trigger, response in matches)
        dadjoke_cmd.get_help_text.assert_called_once()
        unrelated_cmd.get_help_text.assert_not_called()

    def test_help_disabled_no_response(self, cm_bot):
        """[Help_Command] enabled=false must suppress the help response.

        The special help path bypasses the plugin loop (where can_execute() enforces
        enablement), so it has to honor the flag itself.
        """
        mock_help = MagicMock()
        mock_help.help_enabled = False
        mock_help.keywords = ["help"]
        mock_help.should_execute = Mock(return_value=False)
        manager = make_manager(cm_bot, commands={"help": mock_help})
        msg = mock_message(content="help", is_dm=True)
        matches = manager.check_keywords(msg)
        assert not any(trigger == "help" for trigger, _ in matches)

    def test_help_enabled_responds_when_command_loaded(self, cm_bot):
        """A loaded, enabled help command still produces a help response."""
        cm_bot.config.set("Keywords", "help", "Help: ping, test")
        mock_help = MagicMock()
        mock_help.help_enabled = True
        mock_help.keywords = ["help"]
        manager = make_manager(cm_bot, commands={"help": mock_help})
        msg = mock_message(content="help", is_dm=True)
        matches = manager.check_keywords(msg)
        assert any(trigger == "help" for trigger, _ in matches)

    def test_help_channel_override_blocks_disallowed_channel(self, cm_bot):
        """[Help_Command] channels override must gate the special help path too.

        The path bypasses the plugin loop (where is_channel_allowed is enforced), so
        it has to consult the help command's channel access directly.
        """
        cm_bot.config.set("Keywords", "help", "Help: ping, test")
        mock_help = MagicMock()
        mock_help.help_enabled = True
        mock_help.keywords = ["help"]
        # Disallow the channel the message arrives on.
        mock_help.is_channel_allowed = Mock(return_value=False)
        mock_help.should_execute = Mock(return_value=False)
        manager = make_manager(cm_bot, commands={"help": mock_help})

        msg = mock_message(content="help", channel="general", is_dm=False)
        matches = manager.check_keywords(msg)
        assert not any(trigger == "help" for trigger, _ in matches)

        # Allowed channel still responds.
        mock_help.is_channel_allowed = Mock(return_value=True)
        matches = manager.check_keywords(mock_message(content="help", channel="general", is_dm=False))
        assert any(trigger == "help" for trigger, _ in matches)

    def test_overheard_self_mention_not_stripped_issue_267(self, cm_bot):
        """Keyword scan must not delete @[bot] from overheard non-command traffic (#267)."""
        from modules.commands.ping_command import PingCommand

        cm_bot.config.set("Bot", "bot_name", "IU1IPB-1")
        cm_bot.config.set("Bot", "respond_to_mentions", "also")
        ping = PingCommand(cm_bot)
        manager = make_manager(cm_bot, commands={"ping": ping})
        body = "ack @[IU1IPB-1] | 9d12,aa11,4039 (3 hops)"
        msg = mock_message(content=body, channel="general", is_dm=False)
        matches = manager.check_keywords(msg)
        assert not any(trigger == "ping" for trigger, _ in matches)
        assert msg.content == body
        assert msg.original_content == body


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

    def test_subcommand_help_resolves_base_command_and_preserves_message(self, cm_bot):
        mock_cmd = MagicMock()
        mock_cmd.keywords = ["net"]
        mock_cmd.get_help_text = Mock(return_value="Create a network")
        manager = make_manager(cm_bot, commands={"net": mock_cmd})
        message = mock_message(content="help net create", is_dm=True)

        result = manager.get_help_for_command("net create", message)

        assert "Create a network" in result
        mock_cmd.get_help_text.assert_called_once_with(message)
        assert message.content == "help net create"

    def test_multiword_alias_is_checked_before_subcommand_fallback(self, cm_bot):
        dadjoke_cmd = MagicMock()
        dadjoke_cmd.keywords = ["dadjoke", "dad joke"]
        dadjoke_cmd.get_help_text = Mock(return_value="Dad joke help")
        unrelated_cmd = MagicMock()
        unrelated_cmd.keywords = ["dad"]
        unrelated_cmd.get_help_text = Mock(return_value="Wrong help")
        manager = make_manager(
            cm_bot,
            commands={"dadjoke": dadjoke_cmd, "dad": unrelated_cmd},
        )

        result = manager.get_help_for_command("dad joke")

        assert "Dad joke help" in result
        dadjoke_cmd.get_help_text.assert_called_once()
        unrelated_cmd.get_help_text.assert_not_called()


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
    async def test_send_channel_message_suppressed_when_radio_offline(self, cm_bot):
        """Interactive channel sends should suppress while radio-offline is active."""
        cm_bot.connected = True
        cm_bot.is_radio_offline = True
        cm_bot.meshcore = Mock()
        cm_bot.channel_manager = Mock()
        cm_bot.channel_manager.get_channel_number = Mock(return_value=3)
        manager = make_manager(cm_bot)

        result = await manager.send_channel_message("general", "Hello mesh")

        assert result is False
        cm_bot.channel_manager.get_channel_number.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_dm_suppressed_when_radio_offline(self, cm_bot):
        """Interactive DM sends should suppress while radio-offline is active."""
        cm_bot.connected = True
        cm_bot.is_radio_offline = True
        cm_bot.meshcore = Mock()
        cm_bot.meshcore.get_contact_by_name = Mock(return_value={"name": "TestUser"})
        manager = make_manager(cm_bot)

        result = await manager.send_dm("TestUser", "Hello mesh")

        assert result is False
        cm_bot.meshcore.get_contact_by_name.assert_not_called()


class TestSendDMRecipientResolution:
    """Tests for recipient lookup in send_dm()."""

    @pytest.mark.asyncio
    async def test_send_dm_resolves_contact_by_pubkey_prefix(self, cm_bot):
        """When name lookup fails, send_dm should resolve by public key prefix."""
        from meshcore import EventType

        cm_bot.connected = True
        cm_bot.meshcore = Mock()
        cm_bot.meshcore.get_contact_by_name = Mock(return_value=None)
        cm_bot.meshcore.contacts = {
            "contact1": {
                "name": "Alice",
                "adv_name": "AliceAdv",
                "public_key": "ab12deadbeefcafebabe",
            }
        }
        cm_bot.meshcore.commands = Mock(spec=["send_msg"])
        cm_bot.meshcore.commands.send_msg = AsyncMock(return_value=Mock(type=EventType.MSG_SENT, payload=None))
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)
        manager = make_manager(cm_bot)

        result = await manager.send_dm("ab12", "Hello mesh")

        assert result is True
        cm_bot.meshcore.get_contact_by_name.assert_called_once_with("ab12")
        cm_bot.meshcore.commands.send_msg.assert_awaited_once()
        sent_contact = cm_bot.meshcore.commands.send_msg.await_args.args[0]
        assert sent_contact["name"] == "Alice"
        assert sent_contact["public_key"].startswith("ab12")

    @pytest.mark.asyncio
    async def test_send_dm_fails_when_name_and_prefix_lookup_miss(self, cm_bot):
        """send_dm should fail when recipient cannot be resolved by name or prefix."""
        cm_bot.connected = True
        cm_bot.meshcore = Mock()
        cm_bot.meshcore.get_contact_by_name = Mock(return_value=None)
        cm_bot.meshcore.contacts = {
            "contact1": {
                "name": "Bob",
                "public_key": "ffffdeadbeefcafebabe",
            }
        }
        cm_bot.meshcore.pending_contacts = {}
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)
        manager = make_manager(cm_bot)

        result = await manager.send_dm("ab12", "Hello mesh")

        assert result is False
        cm_bot.meshcore.get_contact_by_name.assert_called_once_with("ab12")
        cm_bot.logger.error.assert_called()
        assert "Contact not found for DM recipient identifier" in cm_bot.logger.error.call_args.args[0]

    @pytest.mark.asyncio
    async def test_send_dm_resolves_pending_contact_by_pubkey_prefix(self, cm_bot):
        """NEW_CONTACT peers live in pending_contacts until the next get_contacts()."""
        from meshcore import EventType

        cm_bot.connected = True
        cm_bot.meshcore = Mock()
        cm_bot.meshcore.get_contact_by_name = Mock(return_value=None)
        cm_bot.meshcore.contacts = {}
        pending_key = "3a2418b4ad42cafebabe0123456789abcdef0123456789abcdef0123456789"
        cm_bot.meshcore.pending_contacts = {
            pending_key: {
                "name": "NewCompanion",
                "adv_name": "NewCompanion",
                "public_key": pending_key,
            }
        }
        cm_bot.meshcore.commands = Mock(spec=["send_msg"])
        cm_bot.meshcore.commands.send_msg = AsyncMock(return_value=Mock(type=EventType.MSG_SENT, payload=None))
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)
        manager = make_manager(cm_bot)

        result = await manager.send_dm("3a2418b4ad42", "Pong!")

        assert result is True
        sent_contact = cm_bot.meshcore.commands.send_msg.await_args.args[0]
        assert sent_contact["public_key"] == pending_key
        assert sent_contact["name"] == "NewCompanion"

    @pytest.mark.asyncio
    async def test_send_response_dm_uses_sender_pubkey_over_name(self, cm_bot):
        """send_response for a DM must use sender_pubkey (not sender_id/name) to avoid
        misrouting when two nodes share a similar display name."""
        from meshcore import EventType

        cm_bot.connected = True
        cm_bot.meshcore = Mock()
        # Name lookup by display name would resolve the WRONG node (same name prefix)
        cm_bot.meshcore.get_contact_by_name = Mock(return_value=None)
        cm_bot.meshcore.contacts = {
            "contact1": {
                "name": "Alice",
                "adv_name": "Alice",
                "public_key": "aabbccddeeff001122",
            },
            "contact2": {
                "name": "Alice-repeater",
                "adv_name": "Alice-repeater",
                "public_key": "ffeeddccbbaa998877",
            },
        }
        cm_bot.meshcore.commands = Mock(spec=["send_msg"])
        cm_bot.meshcore.commands.send_msg = AsyncMock(return_value=Mock(type=EventType.MSG_SENT, payload=None))
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)
        manager = make_manager(cm_bot)

        msg = MeshMessage(
            content="hello",
            sender_id="Alice",
            sender_pubkey="aabbccddeeff001122",
            is_dm=True,
        )

        result = await manager.send_response(msg, "Hi back")

        assert result is True
        cm_bot.meshcore.get_contact_by_name.assert_called_once_with("aabbccddeeff001122")
        sent_contact = cm_bot.meshcore.commands.send_msg.await_args.args[0]
        assert sent_contact["public_key"] == "aabbccddeeff001122"

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


class TestSplitTextIntoUtf8Chunks:
    """Tests for CommandManager.split_text_into_utf8_chunks."""

    def test_short_text_single_chunk(self):
        result = CommandManager.split_text_into_utf8_chunks("hello", 158)
        assert result == ["hello"]

    def test_empty_string(self):
        result = CommandManager.split_text_into_utf8_chunks("", 158)
        assert result == [""]

    def test_splits_on_spaces_within_byte_budget(self):
        text = "word " * 50  # 250 chars ASCII
        result = CommandManager.split_text_into_utf8_chunks(text.strip(), 158)
        assert len(result) >= 2
        assert all(len(c.encode("utf-8")) <= 158 for c in result)

    def test_never_splits_multibyte_codepoints(self):
        # Each emoji is 4 UTF-8 bytes
        text = "😀" * 50
        result = CommandManager.split_text_into_utf8_chunks(text, 20)
        assert all(len(c.encode("utf-8")) <= 20 for c in result)
        assert "".join(result) == text
        for chunk in result:
            # Re-encoding round-trip must succeed (no truncated sequences)
            chunk.encode("utf-8").decode("utf-8")

    def test_prefers_newline_boundaries(self):
        lines = [f"line-{i}-xxxxxxxx" for i in range(20)]
        text = "\n".join(lines)
        result = CommandManager.split_text_into_utf8_chunks(text, 80)
        assert all(len(c.encode("utf-8")) <= 80 for c in result)
        # Most chunks should be whole lines (no mid-line hard splits for this input)
        for chunk in result:
            for line in chunk.split("\n"):
                if line:
                    assert line.startswith("line-")


class TestSendDMLengthGuard:
    """Oversized DM payloads are auto-split before the radio send."""

    @pytest.mark.asyncio
    async def test_oversized_dm_is_auto_split(self, cm_bot):
        from meshcore import EventType

        cm_bot.connected = True
        cm_bot.meshcore = Mock()
        contact = {"name": "Alice", "public_key": "ab12deadbeef"}
        cm_bot.meshcore.get_contact_by_name = Mock(return_value=contact)
        cm_bot.meshcore.commands = Mock(spec=["send_msg"])
        cm_bot.meshcore.commands.send_msg = AsyncMock(
            return_value=Mock(type=EventType.MSG_SENT, payload=None)
        )
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)
        cm_bot.config.set("Bot", "bot_tx_rate_limit_seconds", "0")
        manager = make_manager(cm_bot)

        oversized = "x" * 200
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("modules.command_manager.asyncio.sleep", AsyncMock())
            result = await manager.send_dm("Alice", oversized)

        assert result is True
        assert cm_bot.meshcore.commands.send_msg.await_count >= 2
        for call in cm_bot.meshcore.commands.send_msg.await_args_list:
            payload = call.args[1]
            assert len(payload.encode("utf-8")) <= 158

    @pytest.mark.asyncio
    async def test_under_budget_dm_sends_once(self, cm_bot):
        from meshcore import EventType

        cm_bot.connected = True
        cm_bot.meshcore = Mock()
        contact = {"name": "Alice", "public_key": "ab12deadbeef"}
        cm_bot.meshcore.get_contact_by_name = Mock(return_value=contact)
        cm_bot.meshcore.commands = Mock(spec=["send_msg"])
        cm_bot.meshcore.commands.send_msg = AsyncMock(
            return_value=Mock(type=EventType.MSG_SENT, payload=None)
        )
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)
        manager = make_manager(cm_bot)

        result = await manager.send_dm("Alice", "short ok")

        assert result is True
        cm_bot.meshcore.commands.send_msg.assert_awaited_once()


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

    def test_channel_flood_scope_reduces_budget_by_10_bytes(self):
        mgr = self._make_manager(bot_name="LongBotName")
        mgr.bot.config.set("Channels", "flood_scope.weather", "#sea")
        msg = MeshMessage(content="x", channel="#Weather", is_dm=False)
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


class TestExecuteCommandsErrorPath:
    """The `except Exception` branch in execute_commands (PR #243)."""

    @staticmethod
    def _failing_command(exc):
        command = MagicMock()
        command.is_channel_allowed = Mock(return_value=True)
        command.should_execute = Mock(return_value=True)
        command.get_response_format = Mock(return_value=None)
        command.can_execute_now = Mock(return_value=True)
        command.requires_internet = False
        command.cooldown_seconds = 0
        command.last_response = None
        command._record_execution = Mock()
        command.execute = AsyncMock(side_effect=exc)
        command.translate = Mock(side_effect=lambda key, **kw: f"{key}: {kw['error']}")
        return command

    @pytest.mark.asyncio
    async def test_failure_is_logged_with_traceback(self, cm_bot):
        manager = make_manager(cm_bot, commands={"boom": self._failing_command(RuntimeError("kaboom"))})
        manager.send_response = AsyncMock(return_value=True)

        await manager.execute_commands(mock_message(content="!boom", is_dm=True))

        # logger.exception, not logger.error — the traceback is the whole point.
        cm_bot.logger.exception.assert_called_once()
        assert "kaboom" in cm_bot.logger.exception.call_args.args[0]

    @pytest.mark.asyncio
    async def test_mesh_reply_carries_only_the_exception_text(self, cm_bot):
        """The reply goes out over RF, so it must not carry a filesystem path.

        An earlier revision of #243 interpolated `file:line` from the traceback into
        both the log line and this reply, which leaked the install path over the air
        and spent airtime on the error path, where retries are most likely.
        """
        command = self._failing_command(RuntimeError("kaboom"))
        manager = make_manager(cm_bot, commands={"boom": command})
        manager.send_response = AsyncMock(return_value=True)

        await manager.execute_commands(mock_message(content="!boom", is_dm=True))

        assert command.translate.call_args.kwargs["error"] == "kaboom"
        sent = manager.send_response.await_args.args[1]
        assert sent == "errors.execution_error: kaboom"
        assert ".py" not in sent and "command_manager" not in sent


class TestChannelBodyBudget:
    """Tests for channel_body_budget — the shared RF size for one channel body."""

    def test_budget_subtracts_sender_prefix(self, cm_bot):
        manager = make_manager(cm_bot)
        # "TestBot" is 7 bytes, plus 2 for the ": " framing.
        assert manager.channel_body_budget(channel="general") == 160 - 7 - 2

    def test_multibyte_bot_name_counted_in_bytes(self, cm_bot):
        cm_bot.config.set("Bot", "bot_name", "ComchanBot \U0001f916")
        manager = make_manager(cm_bot)
        # 11 ASCII chars + a 4-byte emoji = 15 bytes.
        assert manager.channel_body_budget(channel="general") == 160 - 15 - 2

    def test_regional_scope_costs_extra_bytes(self, cm_bot):
        manager = make_manager(cm_bot)
        globally = manager.channel_body_budget(channel="general")
        regional = manager.channel_body_budget(channel="general", scope="#west")
        assert globally - regional == CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD

    def test_global_scope_markers_cost_nothing(self, cm_bot):
        manager = make_manager(cm_bot)
        baseline = manager.channel_body_budget(channel="general")
        for marker in ("", "*", "0", "None"):
            assert manager.channel_body_budget(channel="general", scope=marker) == baseline

    def test_outgoing_override_is_budgeted_for(self, cm_bot):
        """An override the send will apply has to shrink the budget, or chunks overshoot."""
        cm_bot.config.set("Channels", "outgoing_flood_scope_override", "#west")
        manager = make_manager(cm_bot)
        assert manager.channel_body_budget(channel="general") == (
            160 - 7 - 2 - CHANNEL_REGIONAL_FLOOD_SCOPE_BODY_OVERHEAD
        )


class TestSendChannelMessageLengthGuard:
    """An oversized channel body must be split, never handed to the firmware whole.

    A body over the budget produces no confirmation event, so the send burns its
    no_event_received retries and the stalled transport then reads as dead —
    the radio-reconnect loop reported against the webhook service.
    """

    def _wire_radio(self, cm_bot):
        from meshcore import EventType

        cm_bot.connected = True
        cm_bot.channel_manager = Mock()
        cm_bot.channel_manager.get_channel_number = Mock(return_value=5)
        cm_bot.meshcore = Mock()
        cm_bot.meshcore.commands = Mock(spec=["send_chan_msg"])
        cm_bot.meshcore.commands.send_chan_msg = AsyncMock(
            return_value=Mock(type=EventType.MSG_SENT, payload=None)
        )
        cm_bot.bot_tx_rate_limiter.wait_for_tx = AsyncMock(return_value=None)
        cm_bot.channel_sent_listeners = []
        return cm_bot.meshcore.commands.send_chan_msg

    @pytest.mark.asyncio
    async def test_within_budget_sends_once_unchanged(self, cm_bot):
        send_chan_msg = self._wire_radio(cm_bot)
        manager = make_manager(cm_bot)

        result = await manager.send_channel_message("general", "short and sweet")

        assert result is True
        send_chan_msg.assert_awaited_once()
        assert send_chan_msg.await_args[0][1] == "short and sweet"

    @pytest.mark.asyncio
    async def test_oversized_body_is_split_to_budget(self, cm_bot):
        send_chan_msg = self._wire_radio(cm_bot)
        manager = make_manager(cm_bot)
        budget = manager.channel_body_budget(channel="ky-wx")
        # The message from the reported failure: 173 bytes against a ~151-byte budget.
        content = (
            "The Heat Advisory for the I-35 Corridor and Coastal Plains (Hays, "
            "Bexar, Comal, Guadalupe, Caldwell, Atascosa, Wilson, Karnes, "
            "Gonzales, De Witt) has expired as of 7 PM CDT."
        )
        assert len(content.encode("utf-8")) > budget

        with patch("modules.command_manager.asyncio.sleep", new=AsyncMock()):
            result = await manager.send_channel_message("ky-wx", content)

        assert result is True
        assert send_chan_msg.await_count > 1
        for call in send_chan_msg.await_args_list:
            assert len(call[0][1].encode("utf-8")) <= budget

    @pytest.mark.asyncio
    async def test_split_preserves_every_word(self, cm_bot):
        send_chan_msg = self._wire_radio(cm_bot)
        manager = make_manager(cm_bot)
        content = " ".join(f"word{i}" for i in range(60))

        with patch("modules.command_manager.asyncio.sleep", new=AsyncMock()):
            await manager.send_channel_message("general", content)

        sent = " ".join(_strip_part_suffix(call[0][1]) for call in send_chan_msg.await_args_list)
        assert sent.split() == content.split()

    @pytest.mark.asyncio
    async def test_multibyte_split_stays_within_budget(self, cm_bot):
        send_chan_msg = self._wire_radio(cm_bot)
        manager = make_manager(cm_bot)
        budget = manager.channel_body_budget(channel="general")
        content = "ä" * 200  # 400 UTF-8 bytes

        with patch("modules.command_manager.asyncio.sleep", new=AsyncMock()):
            await manager.send_channel_message("general", content)

        for call in send_chan_msg.await_args_list:
            assert len(call[0][1].encode("utf-8")) <= budget
        rejoined = "".join(
            _strip_part_suffix(call[0][1]) for call in send_chan_msg.await_args_list
        )
        assert rejoined == content

    @pytest.mark.asyncio
    async def test_split_parts_are_numbered_in_order(self, cm_bot):
        send_chan_msg = self._wire_radio(cm_bot)
        manager = make_manager(cm_bot)

        with patch("modules.command_manager.asyncio.sleep", new=AsyncMock()):
            await manager.send_channel_message("general", "y " * 300)

        sent = [call[0][1] for call in send_chan_msg.await_args_list]
        total = len(sent)
        assert total > 1
        for i, text in enumerate(sent, 1):
            assert text.endswith(f" ({i}/{total})")

    @pytest.mark.asyncio
    async def test_unsplit_message_is_not_numbered(self, cm_bot):
        send_chan_msg = self._wire_radio(cm_bot)
        manager = make_manager(cm_bot)

        await manager.send_channel_message("general", "fits in one frame")

        assert send_chan_msg.await_args[0][1] == "fits in one frame"

    @pytest.mark.asyncio
    async def test_chunked_send_does_not_resplit(self, cm_bot):
        """send_channel_messages_chunked already sized its chunks; re-entry would loop."""
        send_chan_msg = self._wire_radio(cm_bot)
        manager = make_manager(cm_bot)
        chunks = ["first part", "second part"]

        with patch("modules.command_manager.asyncio.sleep", new=AsyncMock()):
            result = await manager.send_channel_messages_chunked("general", chunks)

        assert result is True
        assert [call[0][1] for call in send_chan_msg.await_args_list] == chunks

    @pytest.mark.asyncio
    async def test_guard_runs_before_rate_limit_accounting(self, cm_bot):
        """The split path owns its own limiting; the guard must not double-charge."""
        self._wire_radio(cm_bot)
        manager = make_manager(cm_bot)
        manager._check_rate_limits = AsyncMock(return_value=(True, ""))
        content = "x" * 400

        with patch("modules.command_manager.asyncio.sleep", new=AsyncMock()):
            await manager.send_channel_message("general", content)

        # One check per chunk sent, not an extra one for the unsplit body.
        expected = len(
            manager.split_text_into_utf8_chunks(
                content, manager.channel_body_budget(channel="general")
            )
        )
        assert manager._check_rate_limits.await_count == expected


class TestSplitTextIntoNumberedUtf8Chunks:
    """The suffix comes out of the same budget as the body, so it has to be reserved."""

    def test_fitting_text_is_returned_unsuffixed(self):
        assert CommandManager.split_text_into_numbered_utf8_chunks("short", 100) == ["short"]

    def test_every_part_is_tagged_with_its_position(self):
        chunks = CommandManager.split_text_into_numbered_utf8_chunks("a " * 200, 60)
        total = len(chunks)
        assert total > 1
        for i, chunk in enumerate(chunks, 1):
            assert chunk.endswith(f" ({i}/{total})")

    def test_suffix_is_inside_the_byte_budget(self):
        for budget in (32, 40, 60, 100, 130, 143):
            chunks = CommandManager.split_text_into_numbered_utf8_chunks("word " * 200, budget)
            for chunk in chunks:
                assert len(chunk.encode("utf-8")) <= budget, (budget, chunk)

    def test_budget_holds_when_the_count_reaches_double_digits(self):
        """Crossing ten parts widens the suffix, which must not push a part over."""
        chunks = CommandManager.split_text_into_numbered_utf8_chunks("token " * 300, 40)
        assert len(chunks) >= 10
        assert chunks[-1].endswith(f" ({len(chunks)}/{len(chunks)})")
        for chunk in chunks:
            assert len(chunk.encode("utf-8")) <= 40

    def test_multibyte_text_never_splits_a_codepoint(self):
        text = "日本語のテキスト " * 20
        chunks = CommandManager.split_text_into_numbered_utf8_chunks(text, 50)
        for chunk in chunks:
            assert len(chunk.encode("utf-8")) <= 50
        rejoined = "".join(_strip_part_suffix(c) for c in chunks)
        assert rejoined.replace(" ", "") == text.replace(" ", "")

    def test_content_survives_the_round_trip(self):
        text = " ".join(f"tok{i}" for i in range(120))
        chunks = CommandManager.split_text_into_numbered_utf8_chunks(text, 70)
        assert " ".join(_strip_part_suffix(c) for c in chunks).split() == text.split()


class TestSplitKeepsLinksIntact:
    """A link cut across a chunk boundary arrives on the mesh unclickable.

    Whitespace boundaries cannot land inside a link, so the exposure is the
    hard-split fallback: text with no break opportunity before the link, such as
    CJK or a punctuation-joined "...40mph|https://...".
    """

    SHORT_LINK = "https://is.gd/a1B2c3"
    NWS_LINK = (
        "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0."
        "abcdef1234567890abcdef12.001.1"
    )

    @staticmethod
    def _bodies(chunks):
        return [_strip_part_suffix(c) for c in chunks]

    def test_link_after_a_space_survives(self):
        text = (
            "\U0001f7e1Wind Adv Lockhart TX til 9PM CDT by NWS Austin/San Antonio, "
            f"gusts to 40mph this evening {self.SHORT_LINK}"
        )
        chunks = CommandManager.split_text_into_numbered_utf8_chunks(text, 60)
        assert len(chunks) > 1
        assert any(self.SHORT_LINK in b for b in self._bodies(chunks))

    def test_punctuation_joined_link_survives(self):
        text = f"Wind Adv Lockhart TX til 9PM by NWS EWX gusts 40mph|{self.SHORT_LINK}"
        chunks = CommandManager.split_text_into_numbered_utf8_chunks(text, 60)
        assert any(self.SHORT_LINK in b for b in self._bodies(chunks))

    def test_link_after_unspaced_cjk_survives(self):
        """The regression case: a hard split cut a link that had room to travel whole."""
        text = f"大阪の天気{self.SHORT_LINK}"
        chunks = CommandManager.split_text_into_utf8_chunks(text, 30)
        assert any(self.SHORT_LINK in c for c in chunks)
        assert chunks[0] == "大阪の天気"

    def test_hyphenated_text_with_no_spaces_before_the_link(self):
        """No whitespace anywhere in the window, so only the retreat can save this."""
        text = f"WindAdv-Lockhart-TX-til-9PM-EWX|{self.SHORT_LINK}"
        chunks = CommandManager.split_text_into_utf8_chunks(text, 40)
        assert any(self.SHORT_LINK in c for c in chunks)
        assert chunks[0] == "WindAdv-Lockhart-TX-til-9PM-EWX|"

    def test_unspaced_www_link_survives(self):
        link = "www.weather.gov/austin"
        text = f"Gusts40mph-TakeShelter-CaldwellCounty:{link}"
        chunks = CommandManager.split_text_into_utf8_chunks(text, 45)
        assert any(link in c for c in chunks)

    def test_retreat_runs_on_the_second_chunk_too(self):
        """A message with two unspaced links must keep both, not just the first."""
        a, b = "https://is.gd/aaa1111", "https://is.gd/bbb2222"
        text = f"FloodWarn-Hays:{a}|Details-Comal:{b}"
        chunks = CommandManager.split_text_into_utf8_chunks(text, 40)
        assert any(a in c for c in chunks), chunks
        assert any(b in c for c in chunks), chunks

    def test_long_nws_link_survives_a_real_budget(self):
        text = (
            "\U0001f534Tornado Warning Caldwell TX til 7:45PM by NWS EWX take shelter "
            f"now {self.NWS_LINK}"
        )
        chunks = CommandManager.split_text_into_numbered_utf8_chunks(text, 143)
        assert any(self.NWS_LINK in b for b in self._bodies(chunks))

    def test_multiple_links_all_survive(self):
        a, b = "https://is.gd/aaa1111", "https://is.gd/bbb2222"
        text = f"Flood Warn {a} details {b} stay clear of low water crossings"
        bodies = self._bodies(
            CommandManager.split_text_into_numbered_utf8_chunks(text, 80)
        )
        assert any(a in body for body in bodies)
        assert any(b in body for body in bodies)

    def test_www_link_survives(self):
        link = "www.weather.gov/austin/warnings"
        text = f"Details at{link} tonight for Caldwell county and surrounding areas"
        bodies = self._bodies(
            CommandManager.split_text_into_numbered_utf8_chunks(text, 45)
        )
        assert any(link in body for body in bodies)

    def test_link_at_the_start_survives(self):
        text = f"{self.SHORT_LINK} flooding reported near the river crossing tonight"
        bodies = self._bodies(
            CommandManager.split_text_into_numbered_utf8_chunks(text, 40)
        )
        assert any(self.SHORT_LINK in body for body in bodies)

    def test_chunks_still_respect_the_budget(self):
        text = f"Wind Adv Lockhart TX til 9PM by NWS EWX gusts 40mph|{self.SHORT_LINK}"
        for budget in (32, 40, 60, 80, 143):
            for chunk in CommandManager.split_text_into_numbered_utf8_chunks(text, budget):
                assert len(chunk.encode("utf-8")) <= budget, (budget, chunk)

    def test_retreating_never_produces_an_empty_chunk(self):
        text = f"a{self.SHORT_LINK}"
        chunks = CommandManager.split_text_into_utf8_chunks(text, 12)
        assert all(chunk for chunk in chunks)

    def test_plain_prose_is_not_mistaken_for_a_link(self):
        """A bare host.tld pattern in prose must not move split points."""
        text = "Gusts to 40mph.Take shelter now and avoid travel on I-35 through Hays"
        assert CommandManager.split_text_into_utf8_chunks(
            text, 40
        ) == CommandManager.split_text_into_utf8_chunks(text, 40)
        for chunk in CommandManager.split_text_into_utf8_chunks(text, 40):
            assert len(chunk.encode("utf-8")) <= 40


class TestLinksSplitAcross:
    """Reporting the one case that cannot be fixed within a fixed frame."""

    def test_reports_a_link_too_long_for_any_chunk(self):
        link = "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.abcdef.001.1"
        text = f"Alert {link}"
        chunks = CommandManager.split_text_into_numbered_utf8_chunks(text, 40)
        assert CommandManager.links_split_across(text, chunks) == [link]

    def test_reports_nothing_when_links_survive(self):
        link = "https://is.gd/a1B2c3"
        text = f"Wind Adv Lockhart TX til 9PM by NWS EWX gusts 40mph {link}"
        chunks = CommandManager.split_text_into_numbered_utf8_chunks(text, 60)
        assert CommandManager.links_split_across(text, chunks) == []

    def test_reports_nothing_for_text_without_links(self):
        text = "Wind Advisory for Lockhart TX until 9PM CDT this evening, gusts 40mph"
        chunks = CommandManager.split_text_into_numbered_utf8_chunks(text, 40)
        assert CommandManager.links_split_across(text, chunks) == []


class TestLinkSpanStraddling:
    def test_index_inside_a_link_is_reported(self):
        text = "abc https://example.com/x def"
        span = CommandManager._link_span_straddling(text, 12)
        assert span == (4, 25)

    def test_index_on_a_boundary_is_not_straddling(self):
        text = "abc https://example.com/x def"
        assert CommandManager._link_span_straddling(text, 4) is None
        assert CommandManager._link_span_straddling(text, 25) is None

    def test_index_outside_every_link(self):
        text = "abc https://example.com/x def"
        assert CommandManager._link_span_straddling(text, 2) is None
        assert CommandManager._link_span_straddling(text, 27) is None

    def test_second_of_two_links(self):
        text = "https://a.example/1 mid https://b.example/22"
        span = CommandManager._link_span_straddling(text, 30)
        assert span == (24, len(text))
        assert text[span[0]:span[1]] == "https://b.example/22"
