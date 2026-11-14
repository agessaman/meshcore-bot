"""Characterization: how wx and the weather service turn an NWS alerts Atom feed into alert dicts.

The two parsers share most of their logic but classify Special Weather
Statements differently; both behaviors are pinned here. Fixtures are a real
feed sample (tests/fixtures/nws/alerts_real.atom) and synthetic entries that
reach each classifier branch (alerts_synthetic.atom).
"""

import configparser
import re
import xml.dom.minidom
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from tests.characterization.golden_util import assert_golden

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nws"
FEEDS = ["alerts_real", "alerts_synthetic"]


def _bot(logger, config):
    bot = Mock()
    bot.logger = logger
    bot.config = config
    bot.db_manager = Mock()
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))
    bot.command_manager = Mock()
    bot.command_manager.send_channel_message = AsyncMock()
    bot.translator.translate.side_effect = lambda key, **kwargs: f"<{key}{sorted(kwargs.items()) or ''}>"
    return bot


def _wx(logger):
    from modules.commands.wx_command import WxCommand

    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.set("Weather", "weather_provider", "noaa")
    config.add_section("Wx_Command")
    cmd = WxCommand(_bot(logger, config))
    return cmd


def _service(logger):
    from modules.service_plugins.weather_service import WeatherService

    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.add_section("Weather_Service")
    config.set("Weather_Service", "my_position_lat", "47.6")
    config.set("Weather_Service", "my_position_lon", "-122.3")
    return WeatherService(_bot(logger, config))


def _response(text):
    response = Mock()
    response.ok = True
    response.status_code = 200
    response.text = text
    return response


def _single_entry_feeds(text):
    head = text[: text.index("<entry>")]
    return [head + e + "\n</feed>\n" for e in re.findall(r"<entry>.*?</entry>", text, re.S)]


def _wx_alerts(cmd, text, full):
    cmd.noaa_session = Mock()
    cmd.noaa_session.get = Mock(return_value=_response(text))
    cmd._nws_alerts_available = None
    return cmd.get_weather_alerts_noaa(47.6, -122.3, return_full_data=full)


def _summarize(result):
    if isinstance(result, tuple) and result and isinstance(result[0], list):
        alerts, count = result
        return {"count": count, "alerts": alerts, "key_order": [list(a) for a in alerts]}
    return result


@pytest.mark.parametrize("feed", FEEDS)
def test_wx_alert_feed(feed, mock_logger):
    text = (FIXTURES / f"{feed}.atom").read_text(encoding="utf-8")
    cmd = _wx(mock_logger)
    data = {
        "full": _summarize(_wx_alerts(cmd, text, True)),
        "compact": _wx_alerts(cmd, text, False),
        "per_entry": [_summarize(_wx_alerts(cmd, t, True)) for t in _single_entry_feeds(text)],
        "warnings": [str(c) for c in mock_logger.warning.call_args_list],
        "errors": [str(c) for c in mock_logger.error.call_args_list],
    }
    assert_golden(f"nws_alerts_wx_{feed}", data)


@pytest.mark.parametrize("feed", FEEDS)
def test_service_alert_entries(feed, mock_logger):
    text = (FIXTURES / f"{feed}.atom").read_text(encoding="utf-8")
    service = _service(mock_logger)
    parsed = []
    for i, entry in enumerate(xml.dom.minidom.parseString(text).getElementsByTagName("entry")):
        alert = service._parse_alert_entry(entry, f"id-{i}")
        parsed.append({"alert": alert, "key_order": list(alert) if alert else None})
    assert_golden(f"nws_alerts_service_{feed}", parsed)
