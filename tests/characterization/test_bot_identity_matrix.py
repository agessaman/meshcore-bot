"""Characterization: every node-name lookup against every ``self_info`` shape.

The bot reads its own name in several places with different fallback orders and
different presence/truthiness rules (budget paths read ``name``/``user_name``,
mention matching reads ``name``/``adv_name``, and BaseCommand's object branch
tests attribute presence rather than truthiness). Consolidating them must keep
each caller's current answer; this golden file records it.
"""

from __future__ import annotations

import configparser
import itertools
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from modules.command_manager import CommandManager
from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage
from modules.scheduler import MessageScheduler
from tests.characterization.golden_util import assert_golden

FIELDS = ("name", "user_name", "adv_name")
VALUES = (None, "", "Dev")  # None = field absent


class _Cmd(BaseCommand):
    name = "probe"
    keywords = ["probe"]

    async def execute(self, message):  # pragma: no cover - abstract stub
        return True


def _shapes():
    yield "self_info=None", None
    for combo in itertools.product(VALUES, repeat=len(FIELDS)):
        present = {f: (f"{v}-{f}" if v == "Dev" else v) for f, v in zip(FIELDS, combo, strict=True) if v is not None}
        label = ",".join(f"{k}={v!r}" for k, v in present.items()) or "empty"
        yield f"dict[{label}]", dict(present)
        yield f"obj[{label}]", SimpleNamespace(**present)


def _bot(self_info, config_name):
    cfg = configparser.ConfigParser()
    cfg.add_section("Bot")
    if config_name is not None:
        cfg.set("Bot", "bot_name", config_name)
    cfg.add_section("Channels")
    bot = MagicMock()
    bot.config = cfg
    bot.logger = Mock()
    bot.meshcore = MagicMock()
    bot.meshcore.self_info = self_info
    return bot


def _safe(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - the exception type is the behavior
        return f"<raises {type(exc).__name__}>"


def test_node_name_lookups():
    results = {}
    for (label, self_info), config_name in itertools.product(list(_shapes()), [None, "", "CfgBot"]):
        bot = _bot(self_info, config_name)
        cm = object.__new__(CommandManager)
        cm.bot = bot
        cm.logger = bot.logger
        cmd = object.__new__(_Cmd)
        cmd.bot = bot
        cmd.logger = bot.logger
        sched = object.__new__(MessageScheduler)
        sched.bot = bot
        sched.logger = bot.logger
        channel_msg = MeshMessage(content="x", channel="general", is_dm=False)
        results[f"{label} config_bot_name={config_name!r}"] = {
            "cm.get_max_message_length": _safe(lambda: cm.get_max_message_length(channel_msg)),
            "cm.channel_body_budget": _safe(lambda: cm.channel_body_budget(channel="general")),
            "cmd.get_max_message_length": _safe(lambda: cmd.get_max_message_length(channel_msg)),
            "cmd._get_bot_name": _safe(cmd._get_bot_name),
            "scheduler._channel_body_budget": _safe(lambda: sched._channel_body_budget(None)),
        }
    assert_golden("bot_identity_matrix", results)
