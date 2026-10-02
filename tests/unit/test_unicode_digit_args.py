"""Superscript and other non-decimal digits pass str.isdigit() but not int().

Each of these parsers checked isdigit() and then called int(), so an argument
like "²" raised ValueError instead of being treated as text.
"""

import asyncio
import configparser
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.commands.dice_command import DiceCommand
from modules.commands.roll_command import RollCommand
from modules.commands.wx_command import WxCommand
from modules.feed_format import apply_feed_field_function

NON_DECIMAL_DIGITS = ["²", "¹²", "①", "⁴⁵"]


def _bot():
    config = configparser.ConfigParser()
    config.read_dict({"Weather": {"weather_provider": "noaa"}, "Wx_Command": {}, "Bot": {}})
    bot = Mock()
    bot.config = config
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    return bot


def _run_weather(cls, content):
    cmd = cls(_bot())
    cmd._get_custom_mqtt_weather_topic = Mock(return_value=None)
    cmd._get_custom_wxsim_source = Mock(return_value=None)
    cmd.send_response = AsyncMock(return_value=True)
    cmd.record_execution = Mock()
    cmd.get_weather_for_location = AsyncMock(return_value="ok")
    message = SimpleNamespace(content=content, sender_id="u", sender_pubkey="pk", channel="general", is_dm=False)
    assert asyncio.run(cmd.execute(message)) is True
    # wx and gwx order these parameters differently; read them by name.
    bound = inspect.signature(cls.get_weather_for_location).bind(
        cmd, *cmd.get_weather_for_location.await_args.args, **cmd.get_weather_for_location.await_args.kwargs
    )
    bound.apply_defaults()
    return bound.arguments["location"], bound.arguments["forecast_type"], bound.arguments["num_days"]


@pytest.mark.parametrize("cls", [WxCommand, GlobalWxCommand])
@pytest.mark.parametrize("digits", NON_DECIMAL_DIGITS)
def test_weather_keeps_a_non_decimal_digit_as_part_of_the_location(cls, digits):
    args = _run_weather(cls, f"wx seattle {digits}")
    assert args[:2] == (f"seattle {digits}", "default")


@pytest.mark.parametrize("cls", [WxCommand, GlobalWxCommand])
def test_weather_still_reads_ascii_and_other_decimal_day_counts(cls):
    assert _run_weather(cls, "wx seattle 5") == ("seattle", "multiday", 5)
    assert _run_weather(cls, "wx seattle ٣") == ("seattle", "multiday", 3)  # Arabic-Indic three


@pytest.mark.parametrize("digits", NON_DECIMAL_DIGITS)
def test_dice_and_roll_reject_non_decimal_digits(digits):
    assert DiceCommand(_bot()).parse_dice_notation(digits) == (None, None, False)
    assert RollCommand(_bot()).parse_roll_notation(digits) is None


def test_feed_regex_treats_a_non_decimal_suffix_as_part_of_the_pattern():
    # Before, ":²" was taken for a group number and int("²") aborted the function.
    assert apply_feed_field_function("abc:²x", "regex:(b)c:²") == "b"
    assert apply_feed_field_function("abc", "regex_cond:(b):x:y:²") == "b"
    # A real group number still works.
    assert apply_feed_field_function("abc", "regex:(a)(b):2") == "b"
