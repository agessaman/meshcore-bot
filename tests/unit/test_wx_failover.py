"""[Weather] failover_provider: a second forecast provider answers when the first one fails."""

import configparser
from unittest.mock import Mock, patch

import pytest
from urllib3.exceptions import ReadTimeoutError

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.commands.wx_command import WxCommand
from modules.config_reload import ConfigReloadMixin
from modules.i18n import Translator
from modules.weather_providers import PROVIDERS
from modules.weather_providers.base import ForecastReply
from modules.weather_providers.metno import MetNorwayProvider
from modules.weather_providers.noaa import NoaaProvider
from modules.weather_providers.openmeteo import OpenMeteoProvider
from tests.unit.test_metno_provider import NOW, _payload, _Response, _Session

TOKYO = (35.68, 139.7)
TOKYO_ADDRESS = {"city": "Tokyo", "country_code": "jp"}
SEATTLE = (47.6, -122.3)
SEATTLE_ADDRESS = {"city": "Seattle", "state": "Washington", "country_code": "us"}

EN = Translator("en")


def _bot(weather=None, wx_section=None):
    config = configparser.ConfigParser()
    config.read_dict({
        "Weather": {"default_state": "WA", "default_country": "US", **(weather or {})},
        "Wx_Command": wx_section or {},
        "Gwx_Command": {},
        "Bot": {},
    })
    bot = Mock()
    bot.config = config
    bot.translator = EN
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))
    return bot


def _wx(weather=None, wx_section=None, cls=WxCommand):
    return cls(_bot(weather, wx_section))


def _warnings(cmd):
    return [call.args[0] for call in cmd.logger.warning.call_args_list]


def _failover_warnings(cmd):
    return [text for text in _warnings(cmd) if "answering" in text or "failover" in text]


# --- the two outages the brief names -----------------------------------------


def _stalled_open_meteo():
    """Every Open-Meteo attempt times out reading, as on nibbler on 2026-11-27."""
    attempts = []

    def make_request(pool, conn, method, url, *args, **kwargs):
        attempts.append(url)
        raise ReadTimeoutError(pool, url, "Read timed out.")

    patches = (
        patch("urllib3.connectionpool.HTTPConnectionPool._make_request", autospec=True,
              side_effect=make_request),
        patch("urllib3.util.retry.time.sleep"),
    )
    return attempts, patches


def _metno(cmd, response):
    """Point the command's MET Norway failover at a fake session and a fixed clock."""
    provider = cmd.failover_provider
    assert isinstance(provider, MetNorwayProvider)
    provider.session = _Session(response)
    provider._clock = lambda: 1000.0
    provider._now = lambda: NOW
    return provider.session


def _tokyo_global(cmd, forecast_type="default", num_days=7):
    cmd.geocode_location = Mock(return_value=(*TOKYO, TOKYO_ADDRESS, None))
    # The shared request path; gwx's own sync wrapper takes no location type.
    return cmd._forecast_for_location("Tokyo", "city", forecast_type, num_days)


def _open_meteo_stall_reply(weather, cls=WxCommand):
    cmd = _wx({"weather_provider": "openmeteo", **weather}, cls=cls)
    attempts, (make_request, sleep) = _stalled_open_meteo()
    with make_request, sleep:
        if cmd.failover_provider is not None:
            _metno(cmd, _Response(200, _payload("tokyo")))
        reply = _tokyo_global(cmd)
    return cmd, attempts, reply


@pytest.mark.parametrize("cls", [WxCommand, GlobalWxCommand])
def test_an_open_meteo_stall_is_answered_from_met_norway(cls):
    cmd, attempts, reply = _open_meteo_stall_reply({"failover_provider": "metno"}, cls=cls)

    assert len(attempts) == 3  # the first try and both retries timed out
    assert isinstance(reply, tuple)  # the forecast, then the extreme-conditions warning
    assert reply[0] == "multi_message"
    assert reply[1].startswith("Tokyo, JP: Today: ")  # the primary's label on the failover's body
    assert reply[2] == "⚠️ Heavy rain"
    assert _failover_warnings(cmd) == [
        f"openmeteo forecast failed ({EN.translate('commands.gwx.error_fetching_api')}); "
        "answering 35.68,139.7 from metno"
    ]


def test_the_failover_answers_in_the_providers_units():
    cmd, _attempts, reply = _open_meteo_stall_reply({
        "failover_provider": "metno", "temperature_unit": "celsius", "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
    })
    assert cmd.failover_provider.units == cmd.provider.units
    assert cmd.failover_provider.units.temperature == "celsius"
    assert "°C" in reply[1]


def _noaa_wx(weather=None, wx_section=None, status=503):
    cmd = _wx({"weather_provider": "noaa", **(weather or {})}, wx_section)
    response = Mock(ok=status < 400, status_code=status, text="")
    cmd.noaa_session = Mock()
    cmd.noaa_session.get = Mock(return_value=response)
    cmd.city_to_lat_lon = Mock(return_value=(*SEATTLE, SEATTLE_ADDRESS))
    return cmd


def _open_meteo_answers(cmd, text="Clear 20°C"):
    provider = cmd.failover_provider
    assert isinstance(provider, OpenMeteoProvider)
    provider.get_open_meteo_weather = Mock(return_value=text)
    provider._get_open_meteo_weather_with_conditions = Mock(return_value=(text, {}))
    return provider


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_noaa_server_error_is_answered_from_open_meteo(status):
    cmd = _noaa_wx({"failover_provider": "openmeteo"}, status=status)
    _open_meteo_answers(cmd)
    # NWS alerts come from the service that just failed: not asked again.
    cmd._alert_summary = Mock()

    # Seattle is in default_state, so wx names no place, failover or not.
    assert cmd._get_weather_for_location_sync("Seattle", "city") == "Clear 20°C"
    cmd._alert_summary.assert_not_called()
    assert len(_failover_warnings(cmd)) == 1
    assert _failover_warnings(cmd)[0].startswith("noaa forecast failed (")
    assert _failover_warnings(cmd)[0].endswith("answering 47.6,-122.3 from openmeteo")


def test_a_noaa_failover_reply_keeps_wx_units_and_label():
    cmd = _noaa_wx(
        {"failover_provider": "openmeteo", "temperature_unit": "fahrenheit"},
        wx_section={"temperature_unit": "celsius", "wind_speed_unit": "kmh"},
    )
    _open_meteo_answers(cmd)
    cmd.city_to_lat_lon = Mock(return_value=(45.52, -122.68, {"city": "Portland", "state": "Oregon",
                                                               "country_code": "us"}))
    reply = cmd._get_weather_for_location_sync("Portland", "city")
    assert reply.endswith(": Clear 20°C")
    assert ", OR: " in reply or reply.startswith("Portland")
    assert (cmd.failover_provider.units.temperature, cmd.failover_provider.units.wind_speed) == (
        "celsius", "kmh"
    )


def test_an_independent_alert_source_still_rides_along():
    cmd = _wx({"weather_provider": "openmeteo", "failover_provider": "metno", "alerts_provider": "nws"})
    cmd.provider.forecast = Mock(return_value=ForecastReply(text="err", ok=False))
    cmd.failover_provider.forecast = Mock(return_value=ForecastReply(text="BODY"))
    cmd._alert_summary = Mock(return_value=Mock(text="1 alert", count=1))
    reply = _tokyo_global(cmd)
    assert reply == ("multi_message", "Tokyo, JP: BODY", "1 alert", 1)


@pytest.mark.parametrize("forecast_type,num_days,expected_days", [
    ("tomorrow", 7, 7), ("hourly", 7, 7), ("multiday", 5, 5), ("multiday", 14, 9),
])
def test_every_forecast_type_fails_over(forecast_type, num_days, expected_days):
    cmd = _wx({"weather_provider": "openmeteo", "failover_provider": "metno"})
    cmd.provider.forecast = Mock(return_value=ForecastReply(text="err", ok=False))
    cmd.failover_provider.forecast = Mock(return_value=ForecastReply(text="BODY"))
    assert _tokyo_global(cmd, forecast_type, num_days) == "Tokyo, JP: BODY"
    args = cmd.failover_provider.forecast.call_args.args
    assert args[2] == forecast_type
    assert args[3] == expected_days  # MET Norway forecasts 9 days, Open-Meteo 16


def _open_meteo_answers_http(cmd, payload):
    """Open-Meteo answers HTTP 200 with *payload*, through the provider's real formatting."""
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.get = Mock(return_value=Mock(ok=True, status_code=200, json=Mock(return_value=payload)))
    return patch("modules.weather_providers.openmeteo.make_retry_session", return_value=session)


# A day for tomorrow with no low: Open-Meteo formats it as "tomorrow_error", with ok set.
NO_LOWS = {"daily": {"time": ["2026-11-27", "2026-11-28"], "weather_code": [3, 61],
                     "temperature_2m_max": [47, 49]}}


def test_an_unreadable_open_meteo_answer_fails_over():
    cmd = _wx({"weather_provider": "openmeteo", "failover_provider": "metno"})
    cmd.failover_provider.forecast = Mock(return_value=ForecastReply(text="BODY"))
    with _open_meteo_answers_http(cmd, NO_LOWS):
        assert _tokyo_global(cmd, "tomorrow") == "Tokyo, JP: BODY"
    assert _failover_warnings(cmd) == [
        f"openmeteo forecast failed ({EN.translate('commands.gwx.tomorrow_error')}); "
        "answering 35.68,139.7 from metno"
    ]


def test_an_unreadable_answer_without_failover_is_unchanged():
    cmd = _wx({"weather_provider": "openmeteo"})
    with _open_meteo_answers_http(cmd, NO_LOWS):
        assert _tokyo_global(cmd, "tomorrow") == f"Tokyo, JP: {EN.translate('commands.gwx.tomorrow_error')}"


def test_an_unreadable_failover_answer_never_replaces_the_primarys_error():
    cmd = _wx({"weather_provider": "metno", "failover_provider": "openmeteo"})
    cmd.provider.forecast = Mock(
        return_value=ForecastReply(text=EN.translate("commands.gwx.metno.error_fetching_api"), ok=False)
    )
    with _open_meteo_answers_http(cmd, NO_LOWS):
        assert _tokyo_global(cmd, "tomorrow") == EN.translate("commands.gwx.metno.error_fetching_api")
    assert _failover_warnings(cmd)[-1].startswith("openmeteo failover failed too")


def test_an_unreadable_primary_keeps_its_labeled_reply_when_the_failover_fails():
    cmd = _wx({"weather_provider": "openmeteo", "failover_provider": "metno"})
    cmd.failover_provider.forecast = Mock(return_value=ForecastReply(text="err", ok=False))
    with _open_meteo_answers_http(cmd, NO_LOWS):
        assert _tokyo_global(cmd, "tomorrow") == f"Tokyo, JP: {EN.translate('commands.gwx.tomorrow_error')}"


@pytest.mark.parametrize("key", ["multiday_error", "multiday_not_available"])
def test_unreadable_multiday_texts_count_with_their_day_count(key):
    cmd = _wx({"weather_provider": "openmeteo", "failover_provider": "metno"})
    cmd.provider.forecast = Mock(
        return_value=ForecastReply(text=EN.translate(f"commands.gwx.{key}", num_days=5))
    )
    cmd.failover_provider.forecast = Mock(return_value=ForecastReply(text="BODY"))
    assert _tokyo_global(cmd, "multiday", 5) == "Tokyo, JP: BODY"


def test_a_raising_primary_fails_over():
    cmd = _wx({"weather_provider": "openmeteo", "failover_provider": "metno"})
    cmd.provider.forecast = Mock(side_effect=RuntimeError("boom"))
    cmd.failover_provider.forecast = Mock(return_value=ForecastReply(text="BODY"))
    assert _tokyo_global(cmd, "hourly") == "Tokyo, JP: BODY"


# --- unset, and both down ----------------------------------------------------


@pytest.mark.parametrize("weather", [{}, {"failover_provider": "none"}, {"failover_provider": ""}])
def test_unset_failover_replies_as_before_on_an_open_meteo_stall(weather):
    cmd, attempts, reply = _open_meteo_stall_reply(weather)
    assert cmd.failover_provider is None
    assert len(attempts) == 3
    assert reply == EN.translate("commands.gwx.error_fetching_api")
    assert _failover_warnings(cmd) == []


@pytest.mark.parametrize("weather", [{}, {"failover_provider": "none"}])
def test_unset_failover_replies_as_before_on_a_noaa_server_error(weather):
    cmd = _noaa_wx(weather)
    assert cmd.failover_provider is None
    assert cmd._get_weather_for_location_sync("Seattle", "city") == EN.translate("commands.wx.error_fetching")


def test_a_failover_that_fails_too_gives_the_primarys_error():
    cmd, attempts, reply = _open_meteo_stall_reply({"failover_provider": "metno"})
    # Re-run with MET Norway down as well.
    attempts, (make_request, sleep) = _stalled_open_meteo()
    with make_request, sleep:
        session = _metno(cmd, _Response(500, text="Internal Server Error"))
        cmd.failover_provider._cache.clear()
        cmd.logger.warning.reset_mock()
        reply = _tokyo_global(cmd)
    assert session.count() >= 1
    assert reply == EN.translate("commands.gwx.error_fetching_api")
    assert reply != EN.translate("commands.gwx.metno.error_fetching_api")
    warnings = _failover_warnings(cmd)
    assert warnings[0].startswith("openmeteo forecast failed")
    assert warnings[-1].startswith("metno failover failed too")


def test_a_failed_noaa_failover_gives_noaas_error_key():
    cmd = _noaa_wx({"failover_provider": "openmeteo"})
    _open_meteo_answers(cmd, text=EN.translate("commands.gwx.error_fetching"))
    assert cmd._get_weather_for_location_sync("Seattle", "city") == EN.translate("commands.wx.error_fetching")


# --- no coverage is not a failure --------------------------------------------


def test_no_coverage_never_fails_over():
    cmd = _noaa_wx({"failover_provider": "openmeteo"}, status=404)
    provider = _open_meteo_answers(cmd)
    cmd.city_to_lat_lon = Mock(return_value=(*TOKYO, TOKYO_ADDRESS))
    # openmeteo_fallback is off, so NOAA's 404 stays NOAA's error. NOAA records
    # the point as uncovered, which is how it tells a 404 from an outage.
    assert cmd._get_weather_for_location_sync("Tokyo", "city") == EN.translate("commands.wx.error_fetching")
    assert cmd._nws_no_coverage.is_unavailable(*TOKYO)
    provider.get_open_meteo_weather.assert_not_called()
    provider._get_open_meteo_weather_with_conditions.assert_not_called()


def test_no_coverage_goes_to_the_coverage_fallback_not_the_failover():
    cmd = _noaa_wx({"failover_provider": "metno", "openmeteo_fallback": "true"}, status=404)
    cmd.city_to_lat_lon = Mock(return_value=(*TOKYO, TOKYO_ADDRESS))
    cmd.fallback_provider.get_open_meteo_weather = Mock(return_value="Clear 20°C")
    cmd.fallback_provider._get_open_meteo_weather_with_conditions = Mock(return_value=("Clear 20°C", {}))
    cmd.geocode_location = Mock(return_value=(*TOKYO, TOKYO_ADDRESS, None))
    cmd.failover_provider.forecast = Mock()
    assert cmd._get_weather_for_location_sync("Tokyo", "city") == "Tokyo, JP: Clear 20°C"
    cmd.failover_provider.forecast.assert_not_called()


def test_a_reply_that_says_no_coverage_never_fails_over():
    cmd = _wx({"weather_provider": "eccc", "failover_provider": "metno"})
    cmd.provider.forecast = Mock(return_value=ForecastReply(text="err", ok=False, no_coverage=True))
    cmd.failover_provider.forecast = Mock()
    assert _tokyo_global(cmd) == "err"
    cmd.failover_provider.forecast.assert_not_called()


# --- configuration -----------------------------------------------------------


@pytest.mark.parametrize("primary", sorted(PROVIDERS))
@pytest.mark.parametrize("failover", sorted(PROVIDERS))
def test_every_pair_builds(primary, failover):
    cmd = _wx({"weather_provider": primary, "failover_provider": failover})
    if failover == primary:
        assert cmd.failover_provider is None
        assert any("is the weather provider itself" in text for text in _warnings(cmd))
    else:
        assert cmd.failover_provider is not None
        assert cmd.failover_provider.name == failover
        assert cmd.failover_provider.units == cmd.provider.units


def test_unknown_value_warns_and_turns_failover_off():
    cmd = _wx({"weather_provider": "openmeteo", "failover_provider": "accuweather"})
    assert cmd.failover_provider is None
    assert any("Unknown [Weather] failover_provider 'accuweather'" in text for text in _warnings(cmd))


def test_gwx_with_its_own_provider_as_failover_is_silent():
    """failover_provider = openmeteo is normal for a NOAA wx; gwx, pinned to Open-Meteo, just has none."""
    cmd = _wx({"weather_provider": "noaa", "failover_provider": "openmeteo"}, cls=GlobalWxCommand)
    assert cmd.failover_provider is None
    assert _warnings(cmd) == []


def test_a_noaa_failover_shares_the_commands_noaa_state():
    cmd = _wx({"weather_provider": "openmeteo", "failover_provider": "noaa"})
    assert isinstance(cmd.failover_provider, NoaaProvider)
    assert cmd.failover_provider.fallback_enabled is False
    session = Mock()
    cmd.noaa_session = session
    assert cmd.failover_provider.session is session


def test_settings_and_reload_know_the_key():
    entry = next(e for e in WxCommand.settings_schema if e.get("key") == "failover_provider")
    assert entry["default"] == "none"
    assert [o["value"] for o in entry["options"]] == ["none", *PROVIDERS]
    assert "failover_provider" in ConfigReloadMixin._COMMAND_ONLY_WEATHER_KEYS
