"""Integration tests: TC_FLOOD scope matching → reply sent with matched scope.

These tests verify the full chain:
  incoming RF data with TC_FLOOD transport code
  → _match_scope identifies scope
  → MeshMessage.reply_scope set
  → send_response passes scope to send_channel_message
"""

import configparser
import hmac as hmac_mod
from hashlib import sha256
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.command_manager import CommandManager
from modules.message_handler import MessageHandler
from modules.models import MeshMessage

# ── helpers ──────────────────────────────────────────────────────────────────

PAYLOAD_TYPE = 0x05  # GRP_TXT (channel message flood)
PKT_PAYLOAD = b"\xde\xad\xbe\xef\x01\x02\x03\x04"


def _scope_key(name: str) -> bytes:
    return sha256(name.encode()).digest()[:16]


def make_transport_code(scope_name: str, payload_type: int, pkt_payload: bytes) -> int:
    key = _scope_key(scope_name)
    data = bytes([payload_type]) + pkt_payload
    digest = hmac_mod.new(key, data, sha256).digest()
    code = int.from_bytes(digest[:2], "little")
    if code == 0:
        code = 1
    elif code == 0xFFFF:
        code = 0xFFFE
    return code


def _tc_hex(scope_name: str, payload_type: int, pkt_payload: bytes) -> str:
    """Build the 4-byte transport_code hex string as stored in RF data (tc1 + 0000)."""
    tc1 = make_transport_code(scope_name, payload_type, pkt_payload)
    return tc1.to_bytes(2, "little").hex() + "0000"


def make_config(flood_scopes: str = "", outgoing_flood_scope_override: str = "") -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    if flood_scopes:
        config.set("Channels", "flood_scopes", flood_scopes)
    if outgoing_flood_scope_override:
        config.set("Channels", "outgoing_flood_scope_override", outgoing_flood_scope_override)
    return config


# ── _match_scope unit-level integration ──────────────────────────────────────

class TestMatchScopeIntegration:
    """_match_scope with realistic scope_keys dict from CommandManager._load_flood_scope_keys."""

    def _make_scope_keys(self, *names: str) -> dict[str, bytes]:
        return {name: _scope_key(name) for name in names}

    def test_west_scope_matched(self):
        scope_keys = self._make_scope_keys("#west")
        tc = make_transport_code("#west", PAYLOAD_TYPE, PKT_PAYLOAD)
        assert MessageHandler._match_scope(tc, PAYLOAD_TYPE, PKT_PAYLOAD, scope_keys) == "#west"

    def test_east_scope_matched_from_multiple(self):
        scope_keys = self._make_scope_keys("#west", "#east", "#north")
        tc = make_transport_code("#east", PAYLOAD_TYPE, PKT_PAYLOAD)
        assert MessageHandler._match_scope(tc, PAYLOAD_TYPE, PKT_PAYLOAD, scope_keys) == "#east"

    def test_unknown_scope_returns_none(self):
        scope_keys = self._make_scope_keys("#west")
        tc = make_transport_code("#other", PAYLOAD_TYPE, PKT_PAYLOAD)
        assert MessageHandler._match_scope(tc, PAYLOAD_TYPE, PKT_PAYLOAD, scope_keys) is None

    def test_global_flood_no_transport_code(self):
        scope_keys = self._make_scope_keys("#west")
        assert MessageHandler._match_scope(None, PAYLOAD_TYPE, PKT_PAYLOAD, scope_keys) is None


# ── CommandManager.flood_scope_keys loading ──────────────────────────────────

class TestFloodScopeKeysLoading:
    def _make_bot(self, flood_scopes: str) -> MagicMock:
        bot = MagicMock()
        bot.logger = Mock()
        bot.config = make_config(flood_scopes=flood_scopes)
        bot.command_manager = MagicMock()
        # Minimal stubs so CommandManager.__init__ can call load_* methods without crashing
        bot.config.has_section = bot.config.has_section
        bot.config.has_option = bot.config.has_option
        bot.config.get = bot.config.get
        return bot

    def test_scope_keys_loaded_for_hash_names(self):
        bot = MagicMock()
        bot.logger = Mock()
        bot.config = make_config(flood_scopes="#west, #east")
        # Call _load_flood_scope_keys directly (bypasses full __init__)
        cm = object.__new__(CommandManager)
        cm.bot = bot
        cm.logger = Mock()
        cm.flood_scope_allow_global = False
        result = cm._load_flood_scope_keys()
        assert "#west" in result
        assert "#east" in result
        assert result["#west"] == _scope_key("#west")

    def test_bare_names_normalized_on_load(self):
        bot = MagicMock()
        bot.logger = Mock()
        bot.config = make_config(flood_scopes="west, east")
        cm = object.__new__(CommandManager)
        cm.bot = bot
        cm.logger = Mock()
        cm.flood_scope_allow_global = False
        result = cm._load_flood_scope_keys()
        assert "#west" in result
        assert "#east" in result
        assert "west" not in result

    def test_empty_flood_scopes_returns_empty_dict(self):
        bot = MagicMock()
        bot.logger = Mock()
        bot.config = make_config()
        cm = object.__new__(CommandManager)
        cm.bot = bot
        cm.logger = Mock()
        cm.flood_scope_allow_global = False
        result = cm._load_flood_scope_keys()
        assert result == {}

    def test_star_excluded_from_scope_keys(self):
        bot = MagicMock()
        bot.logger = Mock()
        bot.config = make_config(flood_scopes="#west, *")
        cm = object.__new__(CommandManager)
        cm.bot = bot
        cm.logger = Mock()
        cm.flood_scope_allow_global = False
        result = cm._load_flood_scope_keys()
        assert "*" not in result
        assert "#west" in result

    def test_star_sets_allow_global(self):
        bot = MagicMock()
        bot.logger = Mock()
        bot.config = make_config(flood_scopes="#west, *")
        cm = object.__new__(CommandManager)
        cm.bot = bot
        cm.logger = Mock()
        cm.flood_scope_allow_global = False
        cm._load_flood_scope_keys()
        assert cm.flood_scope_allow_global is True

    def test_named_only_allow_global_stays_false(self):
        bot = MagicMock()
        bot.logger = Mock()
        bot.config = make_config(flood_scopes="#west, #east")
        cm = object.__new__(CommandManager)
        cm.bot = bot
        cm.logger = Mock()
        cm.flood_scope_allow_global = False
        cm._load_flood_scope_keys()
        assert cm.flood_scope_allow_global is False


# ── Allowlist gate logic ──────────────────────────────────────────────────────

class TestAllowlistGate:
    """The allowlist gate suppresses replies when flood_scope_keys is active
    and the incoming message does not match any configured scope."""

    def _gate(self, scope_keys: dict, allow_global: bool, reply_scope, rt: int) -> bool:
        """Mirror the gate logic in message_handler.py."""
        should_suppress = False
        if scope_keys and reply_scope is None:
            if rt == 0:
                should_suppress = True  # TC_FLOOD, unknown scope
            elif not allow_global:
                should_suppress = True  # FLOOD, no * in allowlist
        return should_suppress

    def test_flood_suppressed_when_allowlist_active_no_star(self):
        """Regular FLOOD with unrecognized scope is suppressed when allow_global is False."""
        scope_keys = {"#west": _scope_key("#west")}
        allow_global = False
        rt = 1  # FLOOD
        # _match_scope would return None because it's a plain FLOOD (no tc_code1)
        reply_scope = MessageHandler._match_scope(None, PAYLOAD_TYPE, PKT_PAYLOAD, scope_keys)
        assert reply_scope is None
        assert self._gate(scope_keys, allow_global, reply_scope, rt) is True

    def test_flood_allowed_when_star_in_allowlist(self):
        """Regular FLOOD with unrecognized scope is allowed when allow_global is True."""
        scope_keys = {"#west": _scope_key("#west")}
        allow_global = True
        rt = 1  # FLOOD
        reply_scope = MessageHandler._match_scope(None, PAYLOAD_TYPE, PKT_PAYLOAD, scope_keys)
        assert reply_scope is None
        assert self._gate(scope_keys, allow_global, reply_scope, rt) is False

    def test_tc_flood_suppressed_unknown_scope(self):
        """TC_FLOOD with a scope not in allowlist is suppressed."""
        scope_keys = {"#west": _scope_key("#west")}
        allow_global = False
        rt = 0  # TC_FLOOD
        tc = make_transport_code("#other", PAYLOAD_TYPE, PKT_PAYLOAD)
        reply_scope = MessageHandler._match_scope(tc, PAYLOAD_TYPE, PKT_PAYLOAD, scope_keys)
        assert reply_scope is None
        assert self._gate(scope_keys, allow_global, reply_scope, rt) is True

    def test_tc_flood_allowed_known_scope(self):
        """TC_FLOOD with a matching scope is allowed (gate does not suppress)."""
        scope_keys = {"#west": _scope_key("#west")}
        allow_global = False
        rt = 0  # TC_FLOOD
        tc = make_transport_code("#west", PAYLOAD_TYPE, PKT_PAYLOAD)
        reply_scope = MessageHandler._match_scope(tc, PAYLOAD_TYPE, PKT_PAYLOAD, scope_keys)
        assert reply_scope == "#west"
        assert self._gate(scope_keys, allow_global, reply_scope, rt) is False

    def test_no_scope_keys_allows_everything(self):
        """Empty scope_keys dict means no allowlist is active — nothing is suppressed."""
        scope_keys = {}
        allow_global = False
        rt = 1  # FLOOD
        reply_scope = None
        assert self._gate(scope_keys, allow_global, reply_scope, rt) is False


# ── send_response passes reply_scope to send_channel_message ─────────────────

@pytest.mark.asyncio
async def test_send_response_passes_reply_scope_to_channel_send():
    """MeshMessage.reply_scope is forwarded as scope= to send_channel_message."""
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = make_config()
    bot.connected = True
    bot.meshcore = MagicMock()

    cm = object.__new__(CommandManager)
    cm.bot = bot
    cm.logger = bot.logger

    # Patch send_channel_message to capture args
    cm.send_channel_message = AsyncMock(return_value=True)

    msg = MeshMessage(
        content="hello",
        channel="general",
        is_dm=False,
        sender_id="Alice",
        reply_scope="#west",
    )
    await cm.send_response(msg, "reply text")

    cm.send_channel_message.assert_awaited_once()
    _, kwargs = cm.send_channel_message.call_args
    assert kwargs.get("scope") == "#west"


@pytest.mark.asyncio
async def test_send_response_passes_none_scope_when_unset():
    """MeshMessage without reply_scope → scope=None (global flood)."""
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = make_config()

    cm = object.__new__(CommandManager)
    cm.bot = bot
    cm.logger = bot.logger
    cm.send_channel_message = AsyncMock(return_value=True)

    msg = MeshMessage(content="hello", channel="general", is_dm=False, sender_id="Alice")
    await cm.send_response(msg, "reply text")

    _, kwargs = cm.send_channel_message.call_args
    assert kwargs.get("scope") is None


@pytest.mark.asyncio
async def test_send_response_forwards_command_id_to_channel_send():
    """Optional command_id is passed to send_channel_message for repeat tracking."""
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = make_config()
    bot.connected = True
    bot.meshcore = MagicMock()

    cm = object.__new__(CommandManager)
    cm.bot = bot
    cm.logger = bot.logger
    cm.send_channel_message = AsyncMock(return_value=True)
    cm.get_rate_limit_key = Mock(return_value="rk")

    msg = MeshMessage(content="hello", channel="general", is_dm=False, sender_id="Alice")
    await cm.send_response(msg, "reply text", command_id="keyword_foo_1")

    cm.send_channel_message.assert_awaited_once()
    args, kwargs = cm.send_channel_message.call_args
    assert args[2] == "keyword_foo_1"
    assert kwargs.get("scope") is None


@pytest.mark.asyncio
async def test_send_response_forwards_command_id_to_dm():
    """Optional command_id is passed to send_dm for repeat tracking."""
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = make_config()
    bot.connected = True
    bot.meshcore = MagicMock()

    cm = object.__new__(CommandManager)
    cm.bot = bot
    cm.logger = bot.logger
    cm.send_dm = AsyncMock(return_value=True)
    cm.get_rate_limit_key = Mock(return_value="rk")

    msg = MeshMessage(content="hello", channel=None, is_dm=True, sender_id="Bob")
    await cm.send_response(msg, "reply text", command_id="keyword_bar_2")

    cm.send_dm.assert_awaited_once()
    args, kwargs = cm.send_dm.call_args
    assert args[2] == "keyword_bar_2"


# ── scope normalization in send_channel_message ───────────────────────────────

@pytest.mark.asyncio
async def test_send_channel_message_normalizes_bare_scope():
    """scope='west' passed in is normalized to '#west' before set_flood_scope call."""
    bot = MagicMock()
    bot.logger = Mock()
    bot.config = make_config()
    bot.connected = True
    bot.meshcore = MagicMock()
    bot.is_radio_zombie = False
    bot.is_radio_offline = False
    bot.channel_manager = MagicMock()
    bot.channel_manager.get_channel_number = Mock(return_value=0)

    cm = object.__new__(CommandManager)
    cm.bot = bot
    cm.logger = bot.logger

    set_flood_scope = AsyncMock(return_value=MagicMock(type="OK"))
    send_chan_msg = AsyncMock(return_value=MagicMock(type="OK", payload={}))
    bot.meshcore.commands.set_flood_scope = set_flood_scope
    bot.meshcore.commands.send_chan_msg = send_chan_msg

    # Stub out rate limiters and other helpers so the method runs end-to-end
    cm._check_rate_limits = AsyncMock(return_value=(True, None))
    cm._is_no_event_received = Mock(return_value=False)
    cm._handle_send_result = Mock(return_value=True)

    await cm.send_channel_message("general", "hi", scope="west")

    # set_flood_scope should have been called with "#west", not "west"
    calls = set_flood_scope.await_args_list
    scope_set = [c.args[0] for c in calls if c.args]
    assert "#west" in scope_set
