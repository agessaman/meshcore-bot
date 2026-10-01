"""Bot transmissions stay spaced, and second parts of a reply are not dropped by the reply limiter."""

import asyncio
import configparser
import time
from itertools import pairwise
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from modules.models import MeshMessage
from modules.rate_limiter import BotTxRateLimiter, RateLimiter


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


def test_the_reply_limiter_would_drop_a_second_part_sent_without_skipping():
    # Why the flag matters: the default 10 s reply limit refuses a second send a few seconds later.
    limiter = RateLimiter(10)
    limiter.record_send()
    assert limiter.can_send() is False


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
