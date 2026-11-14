"""Heartbeat file written by the bot event loop."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import pytest

import modules.heartbeat as heartbeat
from modules.heartbeat import (
    cancel_heartbeat,
    heartbeat_loop,
    heartbeat_path,
    start_heartbeat,
    touch_heartbeat,
)


@pytest.fixture(autouse=True)
def _reset_write_warning() -> None:
    heartbeat._write_failure_logged = False


def test_touch_creates_the_file_and_advances_mtime(tmp_path: Path) -> None:
    database = tmp_path / "meshcore_bot.db"
    path = heartbeat_path(database)
    assert path == tmp_path / "meshcore_bot.db.heartbeat"
    assert path is not None
    touch_heartbeat(path)
    assert path.is_file()
    os.utime(path, (1_700_000_000, 1_700_000_000))
    before = path.stat().st_mtime
    touch_heartbeat(path)
    assert path.stat().st_mtime > before


def test_memory_database_has_no_heartbeat_path() -> None:
    assert heartbeat_path(":memory:") is None
    assert heartbeat_path(Path(":memory:")) is None


async def test_memory_loop_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    await heartbeat_loop(":memory:", interval=0.01)
    assert list(tmp_path.iterdir()) == []
    assert start_heartbeat(":memory:") is None


async def test_loop_touches_until_cancelled(tmp_path: Path) -> None:
    database = tmp_path / "bot.db"
    path = heartbeat_path(database)
    assert path is not None
    task = asyncio.create_task(heartbeat_loop(database, interval=0.01))
    try:
        for _ in range(100):
            if path.exists():
                break
            await asyncio.sleep(0.01)
        assert path.is_file()
        first = path.stat().st_mtime_ns
        advanced = False
        for _ in range(100):
            await asyncio.sleep(0.01)
            if path.stat().st_mtime_ns != first:
                advanced = True
                break
        assert advanced
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert task.cancelled()
    frozen = path.stat().st_mtime_ns
    await asyncio.sleep(0.05)
    assert path.stat().st_mtime_ns == frozen


def test_write_failure_is_logged_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "missing-parent" / "bot.db.heartbeat"
    with caplog.at_level(logging.WARNING, logger="modules.heartbeat"):
        touch_heartbeat(path)
        touch_heartbeat(path)
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Could not write heartbeat file" in warnings[0].message
    assert not path.exists()


def _write_config(path: Path, db_path: Path) -> None:
    path.write_text(
        f"""[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
db_path = {db_path.as_posix()}
prefix_bytes = 1

[Channels]
monitor_channels = #general
""",
        encoding="utf-8",
    )


async def test_start_touches_before_radio_connect_and_stop_cancels(tmp_path: Path) -> None:
    """The loop is running while start() is still waiting on the radio."""
    from unittest.mock import AsyncMock, MagicMock

    from modules.core import MeshCoreBot

    config_file = tmp_path / "config.ini"
    database = tmp_path / "bot.db"
    _write_config(config_file, database)
    bot = MeshCoreBot(config_file=str(config_file))
    seen: list[bool] = []

    async def fake_connect() -> bool:
        await asyncio.sleep(0)
        path = heartbeat_path(database)
        seen.append(path is not None and path.is_file())
        return False

    async def fake_reconnect() -> bool:
        return False

    bot.connect = fake_connect
    bot._attempt_reconnect = fake_reconnect

    assert await bot.start() is False
    assert seen == [True]
    task = bot._heartbeat_task
    assert task is not None
    assert not task.done()

    bot.feed_manager = MagicMock()
    bot.feed_manager.stop = AsyncMock()
    bot.mesh_graph = None
    bot.services = {}
    bot.web_viewer_integration = None
    bot.scheduler = MagicMock()
    bot.scheduler.scheduler_thread = None
    bot.meshcore = None
    bot.repeater_watch = None
    await bot.stop()
    assert task.cancelled()
    await cancel_heartbeat(None)
