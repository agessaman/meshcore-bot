"""Characterization: every flood-scope resolver against every scope source.

Pins today's behavior, divergences included (the scheduler budgets ``*`` as
regional, and an explicitly empty per-channel scope falls through to the
override in its resolver). A refactor that consolidates scope resolution must
leave this golden file unchanged; a deliberate fix regenerates it.
"""

from __future__ import annotations

import asyncio
import configparser
import itertools
from unittest.mock import AsyncMock, MagicMock, Mock

from meshcore import EventType

from modules.command_manager import CommandManager
from modules.models import MeshMessage
from modules.scheduler import MessageScheduler
from modules.service_plugins.base_service import BaseServicePlugin
from tests.characterization.golden_util import assert_golden

UNSET = "<unset>"
OVERRIDES = [UNSET, "", "#west", "west", "*", "none", "  #pad  "]
CHANNEL_SCOPES = [UNSET, "", "*", "#sea", "0"]
SECTION_SCOPES = [UNSET, "", "#svc", "*"]
EXPLICIT = [None, "", "*", "west", "#east", "None"]
REPLY = [None, "", "#r", "*"]


class _Svc(BaseServicePlugin):
    config_section = "Svc"

    async def start(self):  # pragma: no cover - abstract stub
        pass

    async def stop(self):  # pragma: no cover - abstract stub
        pass


def _config(override, channel_scope, section_scope):
    cfg = configparser.ConfigParser()
    cfg.add_section("Bot")
    cfg.set("Bot", "bot_name", "ScopeBot")
    cfg.add_section("Channels")
    cfg.set("Channels", "monitor_channels", "general")
    if override is not UNSET:
        cfg.set("Channels", "outgoing_flood_scope_override", override)
    if channel_scope is not UNSET:
        cfg.set("Channels", "flood_scope.general", channel_scope)
    cfg.add_section("Svc")
    if section_scope is not UNSET:
        cfg.set("Svc", "flood_scope", section_scope)
    return cfg


def _bot(cfg):
    bot = MagicMock()
    bot.config = cfg
    bot.logger = Mock()
    bot.connected = True
    bot.is_radio_zombie = False
    bot.is_radio_offline = False
    bot.meshcore = MagicMock()
    bot.meshcore.self_info = {"name": "ScopeBot"}
    bot.channel_manager = MagicMock()
    bot.channel_manager.get_channel_number = Mock(return_value=1)
    bot.transmission_tracker = None
    return bot


def _cm(bot):
    cm = object.__new__(CommandManager)
    cm.bot = bot
    cm.logger = bot.logger
    cm._check_rate_limits = AsyncMock(return_value=(True, None))
    cm._is_no_event_received = Mock(return_value=False)
    cm._handle_send_result = Mock(return_value=True)
    bot.command_manager = cm
    return cm


def _radio_scopes(cm, bot, scope):
    """Scopes handed to set_flood_scope by one real send_channel_message call."""
    calls: list[str] = []

    async def _set(value):
        calls.append(value)
        return MagicMock(type=EventType.OK)

    bot.meshcore.commands.set_flood_scope = AsyncMock(side_effect=_set)
    bot.meshcore.commands.send_chan_msg = AsyncMock(
        return_value=MagicMock(type=EventType.OK, payload={})
    )
    asyncio.run(cm.send_channel_message("general", "hi", scope=scope))
    return calls


def _safe(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - the exception type is the behavior
        return f"<raises {type(exc).__name__}>"


def test_scope_resolution_matrix():
    results = {}
    for override, chan, section in itertools.product(OVERRIDES, CHANNEL_SCOPES, SECTION_SCOPES):
        cfg = _config(override, chan, section)
        bot = _bot(cfg)
        cm = _cm(bot)
        sched = object.__new__(MessageScheduler)
        sched.bot = bot
        sched.logger = bot.logger
        svc = object.__new__(_Svc)
        svc.bot = bot
        cfg_key = f"override={override!r} channel={chan!r} section={section!r}"
        row = {
            "service_scope": _safe(svc.get_mesh_flood_scope),
        }
        for reply in REPLY:
            msg = MeshMessage(content="x", channel="general", is_dm=False, reply_scope=reply)
            row[f"message.effective(reply={reply!r})"] = _safe(
                lambda msg=msg: msg.effective_outgoing_flood_scope(bot)
            )
            row[f"resolve(msg reply={reply!r}, section)"] = _safe(
                lambda msg=msg: cm.resolve_channel_send_scope(
                    message=msg, config_section="Svc", channel="general"
                )
            )
        dm = MeshMessage(content="x", channel=None, is_dm=True, reply_scope="#r")
        row["message.effective(dm)"] = _safe(lambda: dm.effective_outgoing_flood_scope(bot))
        for explicit in EXPLICIT:
            key = f"explicit={explicit!r}"
            row[f"resolve({key})"] = _safe(
                lambda e=explicit: cm.resolve_channel_send_scope(scope=e, channel="general")
            )
            row[f"effective({key})"] = _safe(
                lambda e=explicit: cm.effective_channel_send_scope(channel="general", scope=e)
            )
            row[f"budget({key})"] = _safe(
                lambda e=explicit: cm.channel_body_budget(channel="general", scope=e)
            )
            sched_scope = _safe(lambda e=explicit: sched._effective_send_scope("general", e))
            row[f"scheduler.scope({key})"] = sched_scope
            row[f"scheduler.budget({key})"] = _safe(
                lambda s=sched_scope: sched._channel_body_budget(s)
            )
            row[f"radio({key})"] = _safe(lambda e=explicit: _radio_scopes(cm, bot, e))
        # A channel with no per-channel entry, to separate the override path.
        row["effective(other channel)"] = _safe(
            lambda: cm.effective_channel_send_scope(channel="other", scope=None)
        )
        results[cfg_key] = row
    assert_golden("scope_matrix", results)


def test_global_marker_predicates_agree():
    from modules.flood_scope import is_global_marker

    for value in ["", "*", "0", "None", "none", "NONE", "#west", "west", " ", "nOnE"]:
        assert MeshMessage.is_global_flood_scope(value) == is_global_marker(value), value
