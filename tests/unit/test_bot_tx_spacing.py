"""Bot transmissions stay spaced, and second parts of a reply are not dropped by the reply limiter."""

import asyncio
import configparser
import time
from itertools import pairwise
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from modules.models import MeshMessage
from modules.rate_limiter import BotTxRateLimiter


def test_concurrent_senders_are_spaced_by_the_tx_limit():
    limiter = BotTxRateLimiter(seconds=0.3)
    sends = []

    async def reply():
        await limiter.wait_for_tx()  # as in CommandManager._check_rate_limits
        await asyncio.sleep(0.1)  # tx delay and radio round trip before MSG_SENT
        sends.append(time.monotonic())
        limiter.record_tx()

    async def main():
        await asyncio.gather(*(reply() for _ in range(3)))

    asyncio.run(main())
    gaps = [b - a for a, b in pairwise(sends)]
    assert all(gap >= 0.29 for gap in gaps), gaps


def test_wait_for_tx_returns_at_once_when_idle():
    limiter = BotTxRateLimiter(seconds=5.0)
    started = time.monotonic()
    asyncio.run(limiter.wait_for_tx())
    assert time.monotonic() - started < 0.1


def _wx_command():
    from modules.commands.wx_command import WxCommand

    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": "noaa"}, "Wx_Command": {}, "Bot": {"bot_tx_rate_limit_seconds": "5"}})
    bot = Mock()
    bot.config = config
    bot.logger = Mock()
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))
    cmd = WxCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    return cmd


def test_wx_sends_the_alert_part_past_the_reply_limiter():
    cmd = _wx_command()
    cmd.get_weather_for_location = AsyncMock(return_value=("multi_message", "Seattle 60F", "Wind Advisory", 1))
    message = MeshMessage(content="wx 98101", sender_id="Ann", channel="general")
    with patch("asyncio.sleep", AsyncMock()):
        asyncio.run(cmd.execute(message))
    calls = cmd.send_response.await_args_list
    assert [c.args[1] for c in calls] == ["Seattle 60F", "Wind Advisory"]
    assert calls[0].kwargs.get("skip_user_rate_limit", False) is False
    assert calls[1].kwargs.get("skip_user_rate_limit") is True


def test_alert_list_parts_after_the_first_skip_the_reply_limiter():
    from modules.commands.alert_command import AlertCommand

    cmd = object.__new__(AlertCommand)
    cmd.bot = MagicMock()
    cmd.logger = MagicMock()
    cmd.send_response = AsyncMock(return_value=True)
    cmd._format_incident_compact = lambda inc: inc["text"]
    incidents = [{"text": f"Incident {i} " + "x" * 60} for i in range(6)]
    message = MeshMessage(content="alert all", sender_id="Ann", channel="general")
    with patch("asyncio.sleep", AsyncMock()):
        asyncio.run(cmd._send_all_response(message, incidents))
    calls = cmd.send_response.await_args_list
    assert len(calls) >= 2, "expected the incidents to need several messages"
    assert calls[0].kwargs.get("skip_user_rate_limit") is False
    assert all(c.kwargs.get("skip_user_rate_limit") is True for c in calls[1:])


def test_gwx_sends_the_alert_part_past_the_reply_limiter():
    from modules.commands.alternatives.wx_international import GlobalWxCommand

    config = configparser.ConfigParser()
    config.read_dict({"Weather": {}, "Gwx_Command": {}, "Bot": {"bot_tx_rate_limit_seconds": "5"}})
    bot = Mock()
    bot.config = config
    bot.logger = Mock()
    cmd = GlobalWxCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    cmd._get_custom_mqtt_weather_topic = Mock(return_value=None)
    cmd.get_weather_for_location = AsyncMock(return_value=("multi_message", "Paris 15C", "Storm warning"))
    message = MeshMessage(content="gwx paris", sender_id="Ann", channel="general")
    with patch("asyncio.sleep", AsyncMock()):
        asyncio.run(cmd.execute(message))
    calls = cmd.send_response.await_args_list
    assert [c.args[1] for c in calls][-2:] == ["Paris 15C", "Storm warning"]
    assert calls[-1].kwargs.get("skip_user_rate_limit") is True


def test_earthquake_link_follows_the_alert_past_the_reply_limiter():
    from modules.service_plugins.earthquake_service import EarthquakeService

    service = object.__new__(EarthquakeService)
    service.bot = MagicMock()
    service.bot.command_manager.send_channel_message = AsyncMock(return_value=True)
    service.logger = MagicMock()
    service.channel = "quakes"
    service.send_link = True
    service.seen_event_ids = set()
    service._last_posted_time_ms = 0
    service.time_window_minutes = 60
    service.min_magnitude = 3
    service.minlatitude = service.maxlatitude = service.minlongitude = service.maxlongitude = 0
    service._format_quake = lambda quake: "M5.0 near Somewhere"
    service.get_mesh_flood_scope = lambda: None
    response = MagicMock()
    response.json.return_value = {
        "features": [{"id": "q1", "properties": {"time": 1_000, "url": "https://example.invalid/q1"}}]
    }
    service._session = MagicMock()
    service._session.get.return_value = response

    asyncio.run(service._check_earthquakes())
    calls = service.bot.command_manager.send_channel_message.await_args_list
    assert [c.args[1] for c in calls] == ["M5.0 near Somewhere", "https://example.invalid/q1"]
    assert calls[1].kwargs.get("skip_user_rate_limit") is True


def test_announcement_confirmation_skips_the_reply_limiter():
    import inspect

    from modules.commands import announcements_command

    # The confirmation follows send_channel_message, which just used the reply limiter.
    source = inspect.getsource(announcements_command)
    i = source.index("Announcement '{trigger_name}' sent to {target_channel}")
    assert "skip_user_rate_limit=True" in source[i : i + 120]
