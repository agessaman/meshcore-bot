"""Characterization: a failed reload restores every piece of state it touched.

``reload_config`` publishes a new config and rebuilds components inside a
rollback boundary. This injects a failure at each stage (command construction,
introduced plugin failures, the region-warning monitor, the scheduler) and
asserts that every attribute the rollback owns is back to its old object or
value. A refactor that moves these into a settings object must keep all of it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from modules.core import MeshCoreBot


def _write(path: Path, db_path: Path, rate: int, tx_delay: int) -> None:
    path.write_text(
        f"""[Connection]
connection_type = ble

[Bot]
db_path = {db_path.as_posix()}
rate_limit_seconds = {rate}
tx_delay_ms = {tx_delay}
max_channels = 40

[Channels]
monitor_channels = #general
""",
        encoding="utf-8",
    )


ROLLED_BACK = (
    "config",
    "_local_root",
    "rate_limiter",
    "bot_tx_rate_limiter",
    "per_user_rate_limit_enabled",
    "per_user_rate_limiter",
    "nominatim_rate_limiter",
    "channel_rate_limiter",
    "tx_delay_ms",
    "translator",
    "translation_path",
    "local_translation_path",
    "_translator_cache",
    "command_manager",
)


def _snapshot(bot):
    snap = {name: getattr(bot, name) for name in ROLLED_BACK}
    snap["max_channels"] = bot.channel_manager.max_channels
    snap["command_config_state"] = bot._command_config_state(bot.command_manager)
    snap["apscheduler"] = bot.scheduler._apscheduler
    return snap


def _fail_command_manager():
    return patch("modules.core.CommandManager", side_effect=RuntimeError("stage: commands"))


def _fail_plugins(bot):
    # First call reads the live manager's failures, the second the candidate's.
    return patch.object(
        type(bot.command_manager.plugin_loader),
        "get_failed_plugins",
        side_effect=[{}, {"probe": "stage: plugins"}],
    )


def _fail_monitor(bot):
    monitor = getattr(bot, "region_warning_monitor", None)
    calls = {"n": 0}

    def _reload():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("stage: monitor")

    return patch.object(monitor, "reload_config", side_effect=_reload)


def _fail_scheduler(bot):
    calls = {"n": 0}
    real = bot.scheduler.setup_scheduled_messages

    def _setup():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("stage: scheduler")
        return real()

    return patch.object(bot.scheduler, "setup_scheduled_messages", side_effect=_setup)


@pytest.mark.parametrize("stage", ["commands", "plugins", "monitor", "scheduler"])
def test_failed_reload_restores_all_rolled_back_state(tmp_path, stage):
    config = tmp_path / "config.ini"
    db_path = tmp_path / "bot.db"
    _write(config, db_path, rate=10, tx_delay=250)
    bot = MeshCoreBot(config_file=str(config))
    if stage == "monitor" and getattr(bot, "region_warning_monitor", None) is None:
        pytest.skip("no region warning monitor on this bot")
    before = _snapshot(bot)
    _write(config, db_path, rate=25, tx_delay=900)

    injector = {
        "commands": lambda: _fail_command_manager(),
        "plugins": lambda: _fail_plugins(bot),
        "monitor": lambda: _fail_monitor(bot),
        "scheduler": lambda: _fail_scheduler(bot),
    }[stage]()
    with injector:
        success, message = bot.reload_config()

    assert success is False
    expected = "plugin reload failed for: probe" if stage == "plugins" else f"stage: {stage}"
    assert expected in message
    after = _snapshot(bot)
    for name in ROLLED_BACK:
        assert after[name] is before[name], name
    assert after["max_channels"] == before["max_channels"]
    assert after["command_config_state"] == before["command_config_state"]
    assert bot.config.getint("Bot", "rate_limit_seconds") == 10
    assert bot.tx_delay_ms == 250
    if stage != "scheduler":
        assert after["apscheduler"] is before["apscheduler"]


def test_successful_reload_replaces_rolled_back_state(tmp_path):
    config = tmp_path / "config.ini"
    db_path = tmp_path / "bot.db"
    _write(config, db_path, rate=10, tx_delay=250)
    bot = MeshCoreBot(config_file=str(config))
    before = _snapshot(bot)
    _write(config, db_path, rate=25, tx_delay=900)

    success, _ = bot.reload_config()

    assert success is True
    assert bot.config is not before["config"]
    assert bot.rate_limiter is not before["rate_limiter"]
    # The live command manager is kept; its config state is updated in place.
    assert bot.command_manager is before["command_manager"]
    assert bot.tx_delay_ms == 900
    assert bot.config.getint("Bot", "rate_limit_seconds") == 25
