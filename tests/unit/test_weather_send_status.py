"""wx and gwx: the first message of a reply goes through the user rate limit, and a refused send stops the reply."""

import asyncio
import configparser
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.commands.wx_command import WxCommand
from modules.models import MeshMessage

CLASSES = [WxCommand, GlobalWxCommand]


def _bare(cls, budget=40, sent=True):
    cmd = object.__new__(cls)
    cmd.bot = MagicMock()
    cmd.logger = MagicMock()
    cmd.get_max_message_length = lambda message: budget
    cmd.send_response = AsyncMock(return_value=sent)
    return cmd


def _msg():
    return MeshMessage(content="wx", sender_id="Ann", channel="general")


@pytest.mark.parametrize("cls", CLASSES)
def test_a_reply_that_fits_after_trimming_is_rate_limited(cls):
    # Over budget only because of blank lines and padding; packed, it is one message.
    cmd = _bare(cls)
    text = "Mon 50/40 rain   \n\n\n\n\n\n\n\n\n\n   Tue 52/41    "
    with patch("asyncio.sleep", AsyncMock()):
        ok = asyncio.run(cmd._send_multiday_forecast(_msg(), text))
    assert ok is True
    assert [c.kwargs for c in cmd.send_response.await_args_list] == [{"skip_user_rate_limit": False}]


@pytest.mark.parametrize("cls", CLASSES)
def test_a_refused_first_part_stops_the_rest(cls):
    cmd = _bare(cls, sent=False)
    text = "Mon 50/40 rain all day\nTue 52/41 sunny\nWed 49/38 showers late\nThu 51/40"
    with patch("asyncio.sleep", AsyncMock()):
        ok = asyncio.run(cmd._send_multiday_forecast(_msg(), text))
    assert ok is False
    assert cmd.send_response.await_count == 1


def _full(cls, first_sent):
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": "noaa"}, "Wx_Command": {}, "Gwx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    cmd = cls(bot)
    cmd._get_custom_mqtt_weather_topic = Mock(return_value=None)
    cmd._get_custom_wxsim_source = Mock(return_value=None)
    cmd.record_execution = Mock()
    cmd.get_weather_for_location = AsyncMock(return_value=("multi_message", "Seattle: 60°F", "⚠️ Wind Advisory"))
    cmd.send_response = AsyncMock(side_effect=[first_sent, True])
    return cmd


@pytest.mark.parametrize("cls", CLASSES)
@pytest.mark.parametrize("first_sent", [True, False])
def test_the_alert_part_never_goes_out_alone(cls, first_sent):
    cmd = _full(cls, first_sent)
    message = SimpleNamespace(content="wx Seattle", sender_id="u", sender_pubkey="pk", channel="general", is_dm=False)
    with patch("asyncio.sleep", AsyncMock()):
        ok = asyncio.run(cmd.execute(message))
    assert ok is first_sent
    sent = [c.args[1] for c in cmd.send_response.await_args_list]
    assert sent == (["Seattle: 60°F", "⚠️ Wind Advisory"] if first_sent else ["Seattle: 60°F"])
