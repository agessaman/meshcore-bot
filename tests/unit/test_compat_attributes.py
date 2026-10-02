"""Attributes local plugins may read survive the dead-code cleanup."""

import configparser
from unittest.mock import AsyncMock, Mock, patch

import pytest
from meshcore import EventType

from modules.commands.aqi_command import AqiCommand
from modules.core import MeshCoreBot


def test_aqi_keeps_geolocator():
    config = configparser.ConfigParser()
    config.read_dict({"Aqi_Command": {}, "Bot": {}, "Weather": {}})
    bot = Mock()
    bot.config = config
    bot.db_manager.db_path = "meshcore_bot.db"
    with patch("modules.commands.aqi_command.requests_cache.CachedSession"), \
         patch("modules.commands.aqi_command.retry", side_effect=lambda s, **kw: s), \
         patch("modules.commands.aqi_command.openmeteo_requests.Client", return_value=Mock()):
        assert AqiCommand(bot).geolocator is not None


@pytest.mark.asyncio
async def test_set_radio_clock_records_the_sync_time():
    bot = MeshCoreBot.__new__(MeshCoreBot)
    bot.logger = Mock()
    bot.last_clock_sync_time = None
    bot.meshcore = Mock(is_connected=True)
    bot.meshcore.commands.get_time = AsyncMock(return_value=Mock(type=EventType.OK, payload={"time": 100}))
    bot.meshcore.commands.set_time = AsyncMock(return_value=Mock(type=EventType.OK))
    assert await bot.set_radio_clock() is True
    synced = bot.meshcore.commands.set_time.await_args.args[0]
    assert bot.last_clock_sync_time == synced
