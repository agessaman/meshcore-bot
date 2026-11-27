"""The weather provider registries, the config that selects from them, and the seams.

These tests are about the swap itself: which provider a config value produces,
how the alerts provider is resolved separately from the forecast provider, and
that a third provider can be registered and driven end to end without touching
the command.
"""

import asyncio
import configparser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.commands.wx_command import WxCommand
from modules.i18n import Translator
from modules.weather_providers import (
    ALERT_PROVIDERS,
    PROVIDERS,
    make_alerts_provider,
    make_provider,
    resolve_alerts_provider_name,
)
from modules.weather_providers.alerts_meteoalarm import MeteoalarmAlertsProvider
from modules.weather_providers.alerts_nws import NwsAlertsProvider
from modules.weather_providers.base import (
    AlertsProvider,
    AlertSummary,
    Capability,
    ForecastReply,
    WeatherProvider,
    WeatherUnits,
)
from modules.weather_providers.noaa import NoaaProvider
from modules.weather_providers.openmeteo import OpenMeteoProvider

UNITS = WeatherUnits(temperature="fahrenheit", wind_speed="mph", precipitation="inch")


def _bot(weather=None, wx_section=None, bot_section=None):
    config = configparser.ConfigParser()
    config.read_dict({
        "Weather": weather or {},
        "Wx_Command": wx_section or {},
        "Bot": bot_section or {},
    })
    bot = Mock()
    bot.config = config
    bot.translator = Translator("en")
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))
    return bot


def _wx(weather=None, wx_section=None, bot_section=None):
    return WxCommand(_bot(weather, wx_section, bot_section))


def _warnings(mock):
    return [call.args[0] for call in mock.warning.call_args_list]


# --- registries -------------------------------------------------------------


def test_the_registries_hold_the_shipped_providers():
    assert set(PROVIDERS) == {"noaa", "openmeteo"}
    assert PROVIDERS["noaa"] is NoaaProvider
    assert PROVIDERS["openmeteo"] is OpenMeteoProvider
    assert set(ALERT_PROVIDERS) == {"meteoalarm", "nws"}
    assert ALERT_PROVIDERS["nws"] is NwsAlertsProvider
    assert ALERT_PROVIDERS["meteoalarm"] is MeteoalarmAlertsProvider


def test_make_provider_binds_the_host_and_units():
    host = Mock()
    provider = make_provider("openmeteo", host, UNITS, weather_model=None)
    assert isinstance(provider, OpenMeteoProvider)
    assert provider.host is host
    assert provider.units == UNITS


def test_make_provider_passes_shared_state_to_the_constructor():
    host = Mock()
    session, lock, coverage = Mock(), Mock(), Mock()
    provider = make_provider(
        "noaa", host, UNITS, session=session, lock=lock, coverage=coverage, fallback_enabled=True
    )
    assert provider.session is session
    assert provider.lock is lock
    assert provider.coverage is coverage
    assert provider.fallback_enabled is True


def test_make_alerts_provider_binds_the_host():
    host = Mock()
    provider = make_alerts_provider("nws", host)
    assert isinstance(provider, NwsAlertsProvider)
    assert provider.host is host


def test_an_unknown_provider_name_is_a_key_error():
    with pytest.raises(KeyError):
        make_provider("nope", Mock(), UNITS)


# --- provider selection -----------------------------------------------------


def test_an_unknown_weather_provider_falls_back_to_noaa_with_one_warning():
    cmd = _wx({"weather_provider": "carrier-pigeon"})
    assert isinstance(cmd.provider, NoaaProvider)
    warnings = _warnings(cmd.logger)
    assert len(warnings) == 1
    assert "carrier-pigeon" in warnings[0]
    assert "noaa" in warnings[0] and "openmeteo" in warnings[0]


def test_a_known_weather_provider_logs_no_warning():
    cmd = _wx({"weather_provider": "openmeteo"})
    assert isinstance(cmd.provider, OpenMeteoProvider)
    assert _warnings(cmd.logger) == []


@pytest.mark.parametrize("config_value,forecast,expected", [
    ("auto", "noaa", "nws"),
    ("auto", "openmeteo", None),
    ("", "noaa", "nws"),
    ("", "openmeteo", None),
    ("none", "noaa", None),
    ("none", "openmeteo", None),
    ("nws", "openmeteo", "nws"),
    ("NWS", "noaa", "nws"),
])
def test_resolve_alerts_provider_name(config_value, forecast, expected):
    assert resolve_alerts_provider_name(config_value, forecast) == expected


def test_an_unknown_alerts_provider_warns_once_and_behaves_like_auto():
    logger = Mock()
    assert resolve_alerts_provider_name("carrier-pigeon", "noaa", logger) == "nws"
    warnings = _warnings(logger)
    assert len(warnings) == 1
    assert "carrier-pigeon" in warnings[0]
    assert "auto" in warnings[0] and "none" in warnings[0] and "nws" in warnings[0]

    logger.reset_mock()
    assert resolve_alerts_provider_name("carrier-pigeon", "openmeteo", logger) is None
    assert len(_warnings(logger)) == 1


def test_an_unknown_alerts_provider_warns_once_on_the_command():
    cmd = _wx({"weather_provider": "noaa", "alerts_provider": "carrier-pigeon"})
    assert isinstance(cmd.alerts_provider, NwsAlertsProvider)
    warnings = _warnings(cmd.logger)
    assert len(warnings) == 1
    assert "carrier-pigeon" in warnings[0]


def test_alerts_provider_none_leaves_the_command_without_alerts():
    cmd = _wx({"weather_provider": "noaa", "alerts_provider": "none"})
    assert cmd.alerts_provider is None


def test_auto_gives_noaa_an_nws_alerts_provider_and_openmeteo_none():
    assert isinstance(_wx({"weather_provider": "noaa"}).alerts_provider, NwsAlertsProvider)
    assert _wx({"weather_provider": "openmeteo"}).alerts_provider is None


# --- swapping by config alone ----------------------------------------------


def test_the_same_class_swaps_providers_from_config_alone():
    noaa = _wx({"weather_provider": "noaa"})
    openmeteo = _wx({"weather_provider": "openmeteo"})

    assert type(noaa) is type(openmeteo) is WxCommand
    assert isinstance(noaa.provider, NoaaProvider)
    assert isinstance(openmeteo.provider, OpenMeteoProvider)

    assert noaa.provider.translation_ns == "commands.wx"
    assert noaa.provider.location_style == "us"
    assert openmeteo.provider.translation_ns == "commands.gwx"
    assert openmeteo.provider.location_style == "global"


def test_the_provider_capabilities_and_day_caps_are_declared():
    noaa = _wx({"weather_provider": "noaa"}).provider
    openmeteo = _wx({"weather_provider": "openmeteo"}).provider
    expected = {Capability.CURRENT, Capability.TOMORROW, Capability.MULTIDAY, Capability.HOURLY}
    assert set(noaa.capabilities) == expected
    assert set(openmeteo.capabilities) == expected
    assert noaa.max_days > 0 and openmeteo.max_days > 0


def test_gwx_ignores_weather_provider_and_stays_on_openmeteo():
    cmd = GlobalWxCommand(_bot({"weather_provider": "noaa"}))
    assert isinstance(cmd.provider, OpenMeteoProvider)
    assert cmd.provider.location_style == "global"
    assert cmd.alerts_provider is None


def test_gwx_takes_an_nws_alerts_provider_when_asked():
    cmd = GlobalWxCommand(_bot({"weather_provider": "noaa", "alerts_provider": "nws"}))
    assert isinstance(cmd.provider, OpenMeteoProvider)
    assert isinstance(cmd.alerts_provider, NwsAlertsProvider)


# --- the second message -----------------------------------------------------


class _FakeAlerts(AlertsProvider):
    name = "fakealerts"
    translation_ns = "commands.fake"
    _summary = AlertSummary(text="FAKE ALERTS", count=2)

    def __init__(self, host, summary=None):
        super().__init__(host)
        if summary is not None:
            self._summary = summary

    def summary(self, lat, lon, max_length):
        return self._summary

    async def send_full_list(self, message, lat, lon):
        return True


def _forecast_reply(cmd, extra=None, alerts=None):
    """Drive one default forecast through the command with the network stubbed."""
    cmd.provider.forecast = Mock(return_value=ForecastReply(text="BODY", extra=extra))
    cmd.alerts_provider = alerts
    cmd.geocode_location = Mock(
        return_value=(47.6, -122.3, {"city": "Portland", "country_code": "us"}, None)
    )
    cmd._format_location_display = Mock(return_value="Portland, US")
    cmd.get_max_message_length = Mock(return_value=130)
    return cmd._forecast_for_location("47.6,-122.3", "city", "default", 7, None, False)


def test_the_alert_summary_wins_over_the_forecast_extra():
    cmd = _wx({"weather_provider": "openmeteo"})
    alerts = _FakeAlerts(cmd, AlertSummary(text="ALERTS", count=2))
    assert _forecast_reply(cmd, extra="EXTREME", alerts=alerts) == (
        "multi_message", "Portland, US: BODY", "ALERTS", 2
    )


def test_without_alerts_the_forecast_extra_is_the_second_message():
    cmd = _wx({"weather_provider": "openmeteo"})
    assert _forecast_reply(cmd, extra="EXTREME") == (
        "multi_message", "Portland, US: BODY", "EXTREME"
    )


def test_an_empty_alert_summary_leaves_the_forecast_extra_in_place():
    cmd = _wx({"weather_provider": "openmeteo"})
    alerts = _FakeAlerts(cmd, AlertSummary(text="", count=0))
    assert _forecast_reply(cmd, extra="EXTREME", alerts=alerts) == (
        "multi_message", "Portland, US: BODY", "EXTREME"
    )


def test_a_single_message_reply_is_a_plain_string():
    cmd = _wx({"weather_provider": "openmeteo"})
    assert _forecast_reply(cmd) == "Portland, US: BODY"


# --- extensibility ----------------------------------------------------------


class _FakeProvider(WeatherProvider):
    """A third forecast source, registered only for the length of one test."""

    name = "fake"
    translation_ns = "commands.fake"
    location_style = "global"
    capabilities = frozenset({Capability.CURRENT})
    max_days = 7

    def forecast(self, lat, lon, forecast_type="default", num_days=7,
                 location_prefix_len=0, message=None):
        return ForecastReply(text="FAKE FORECAST")


def test_a_registered_third_provider_answers_wx_end_to_end(monkeypatch):
    monkeypatch.setitem(PROVIDERS, "fake", _FakeProvider)
    monkeypatch.setitem(ALERT_PROVIDERS, "fakealerts", _FakeAlerts)

    cmd = _wx(
        {"weather_provider": "fake", "alerts_provider": "fakealerts"},
        bot_section={"bot_tx_rate_limit_seconds": "1.5"},
    )
    assert isinstance(cmd.provider, _FakeProvider)
    assert isinstance(cmd.alerts_provider, _FakeAlerts)

    cmd.geocode_location = Mock(
        return_value=(47.6, -122.3, {"city": "Portland", "country_code": "us"}, None)
    )
    cmd._format_location_display = Mock(return_value="Portland, US")
    cmd.get_max_message_length = Mock(return_value=130)
    cmd.record_execution = Mock()
    cmd.send_response = AsyncMock(return_value=True)
    cmd._pace_reply = AsyncMock()

    message = SimpleNamespace(content="wx 47.6,-122.3", sender_id="u1")
    assert asyncio.run(cmd.execute(message)) is True

    cmd.record_execution.assert_called_once_with("u1")
    cmd.geocode_location.assert_called_once_with("47.6,-122.3")
    assert cmd.send_response.await_count == 2
    first, second = cmd.send_response.await_args_list
    assert first.args == (message, "Portland, US: FAKE FORECAST")
    assert second.args == (message, "FAKE ALERTS")
    assert second.kwargs == {"skip_user_rate_limit": True}
    cmd._pace_reply.assert_awaited_once_with(message, 2.5)


def test_the_fake_provider_is_gone_after_the_test():
    assert "fake" not in PROVIDERS
    assert "fakealerts" not in ALERT_PROVIDERS


# --- the "alerts" option ----------------------------------------------------


def _alerts_command(weather, wx_section=None):
    cmd = _wx(weather, wx_section)
    cmd.record_execution = Mock()
    cmd.send_response = AsyncMock(return_value=True)
    cmd._send_full_alert_list = AsyncMock(return_value=True)
    cmd._zipcode_to_lat_lon_async = AsyncMock(return_value=(47.6, -122.3))
    cmd._city_to_lat_lon_async = AsyncMock(return_value=(47.6, -122.3, {"city": "Seattle"}))
    cmd.geocode_location = Mock(
        return_value=(47.6, -122.3, {"city": "Portland", "country_code": "us"}, None)
    )
    cmd._format_location_display = Mock(return_value="Portland, US")
    cmd.provider.forecast = Mock(return_value=ForecastReply(text="BODY"))
    return cmd


def test_alerts_without_an_alerts_provider_replies_instead_of_crashing():
    cmd = _alerts_command({"weather_provider": "noaa", "alerts_provider": "none"})
    message = SimpleNamespace(content="wx 98101 alerts", sender_id="u1")
    assert asyncio.run(cmd.execute(message)) is True
    cmd.send_response.assert_awaited_once_with(
        message, cmd.translate("commands.wx.source_option_not_available")
    )
    cmd._send_full_alert_list.assert_not_awaited()
    cmd.provider.forecast.assert_not_called()


def test_alerts_on_the_global_path_sends_the_alert_list_not_a_forecast():
    cmd = _alerts_command({"weather_provider": "openmeteo", "alerts_provider": "nws"})
    message = SimpleNamespace(content="wx 98101 alerts", sender_id="u1")
    assert asyncio.run(cmd.execute(message)) is True
    cmd._send_full_alert_list.assert_awaited_once_with(message, 47.6, -122.3)
    cmd.provider.forecast.assert_not_called()
    cmd.send_response.assert_not_awaited()


def test_alerts_outside_the_alerts_provider_coverage_replies_not_available():
    cmd = _alerts_command({"weather_provider": "openmeteo", "alerts_provider": "nws"})
    cmd.alerts_provider.covers = Mock(return_value=False)
    message = SimpleNamespace(content="wx 98101 alerts", sender_id="u1")
    assert asyncio.run(cmd.execute(message)) is True
    cmd.send_response.assert_awaited_once_with(
        message, cmd.translate("commands.gwx.source_option_not_available")
    )
    cmd._send_full_alert_list.assert_not_awaited()


def test_alerts_keeps_the_us_call_sequence():
    cmd = _alerts_command({"weather_provider": "noaa"})
    message = SimpleNamespace(content="wx 98101 alerts", sender_id="u1")
    assert asyncio.run(cmd.execute(message)) is True
    cmd.record_execution.assert_called_once_with("u1")
    cmd._zipcode_to_lat_lon_async.assert_awaited_once_with("98101")
    cmd._send_full_alert_list.assert_awaited_once_with(message, 47.6, -122.3)


# --- NOAA and NWS methods on a non-NOAA command -----------------------------


def test_noaa_named_methods_answer_on_an_openmeteo_command(monkeypatch):
    cmd = _wx({"weather_provider": "openmeteo"})
    assert isinstance(cmd._noaa, NoaaProvider)
    assert cmd._noaa is cmd._noaa  # built once
    monkeypatch.setattr(NoaaProvider, "get_noaa_weather", lambda self, *a, **k: ("NOAA BODY", {}))
    assert cmd.get_noaa_weather(47.6, -122.3) == ("NOAA BODY", {})


def test_nws_named_methods_answer_without_an_alerts_provider(monkeypatch):
    cmd = _wx({"weather_provider": "openmeteo", "alerts_provider": "none"})
    assert cmd.alerts_provider is None
    assert isinstance(cmd._nws, NwsAlertsProvider)
    monkeypatch.setattr(
        NwsAlertsProvider, "get_weather_alerts_noaa", lambda self, *a, **k: ("ALERT", "A", 1)
    )
    assert cmd.get_weather_alerts_noaa(47.6, -122.3) == ("ALERT", "A", 1)


def test_noaa_session_and_coverage_forward_to_the_compatibility_providers():
    cmd = _wx({"weather_provider": "openmeteo"})
    session = Mock()
    cmd.noaa_session = session
    assert cmd.noaa_session is session
    assert cmd._noaa.session is session
    assert cmd._nws.session is session
    assert cmd._nws_no_coverage is cmd._nws.coverage


# --- the message catalog ----------------------------------------------------


def test_wx_on_openmeteo_speaks_the_gwx_catalog():
    cmd = _wx({"weather_provider": "openmeteo"})
    assert cmd.translation_ns == "commands.gwx"
    cmd.wxsim_parser = Mock()
    cmd.wxsim_parser.fetch_from_url = Mock(return_value=None)
    assert cmd._get_wxsim_weather("https://w.test/cabin") == cmd.translate(
        "commands.gwx.error", error="Failed to fetch WXSIM data"
    )


def test_wx_on_noaa_keeps_the_wx_catalog():
    cmd = _wx({"weather_provider": "noaa"})
    assert cmd.translation_ns == "commands.wx"
    cmd.wxsim_parser = Mock()
    cmd.wxsim_parser.fetch_from_url = Mock(return_value=None)
    assert cmd._get_wxsim_weather("https://w.test/cabin") == cmd.translate(
        "commands.wx.error", error="Failed to fetch WXSIM data"
    )


# --- fallback units ---------------------------------------------------------


def test_the_fallback_provider_validates_its_own_units():
    cmd = _wx({"weather_provider": "noaa", "openmeteo_fallback": "true",
               "temperature_unit": "kelvin", "precipitation_unit": "mm"})
    assert cmd.fallback_provider.units == WeatherUnits("fahrenheit", "mph", "mm")
    assert any("temperature_unit" in w for w in _warnings(cmd.logger))


def test_the_model_warning_comes_before_the_unit_warnings():
    cmd = _wx({"weather_provider": "noaa", "openmeteo_fallback": "true",
               "weather_model": "bad model!", "temperature_unit": "kelvin"})
    warnings = _warnings(cmd.logger)
    assert "weather_model" in warnings[0]
    assert any("temperature_unit" in w for w in warnings[1:])


def test_an_invalid_wx_precipitation_unit_keeps_openmeteos_validated_one():
    cmd = _wx({"weather_provider": "noaa", "openmeteo_fallback": "true",
               "precipitation_unit": "mm"},
              wx_section={"precipitation_unit": "furlongs"})
    cmd.fallback_provider.forecast = Mock(return_value=ForecastReply(text="BODY"))
    cmd.geocode_location = Mock(
        return_value=(47.6, -122.3, {"city": "Portland", "country_code": "us"}, None)
    )
    cmd._format_location_display = Mock(return_value="Portland, US")
    cmd.get_max_message_length = Mock(return_value=130)
    cmd._fallback_reply("47.6,-122.3", "coordinates", 47.6, -122.3, "default", 7, None)
    assert cmd.fallback_provider.units.precipitation == "mm"


# --- Meteoalarm -------------------------------------------------------------


def test_meteoalarm_is_selected_with_its_token():
    cmd = _wx({"weather_provider": "noaa", "alerts_provider": "meteoalarm",
               "meteoalarm_token": "s3cr3t-token"})
    assert isinstance(cmd.alerts_provider, MeteoalarmAlertsProvider)
    assert cmd.alerts_provider.token == "s3cr3t-token"
    assert _warnings(cmd.logger) == []


def test_meteoalarm_uses_the_meteogate_gateway_by_default():
    cmd = _wx({"weather_provider": "noaa", "alerts_provider": "meteoalarm",
               "meteoalarm_token": "s3cr3t-token"})
    assert cmd.alerts_provider.base_url == "https://api.meteogate.eu/warnings"


def test_meteoalarm_honours_a_configured_api_url():
    cmd = _wx({"weather_provider": "noaa", "alerts_provider": "meteoalarm",
               "meteoalarm_token": "s3cr3t-token",
               "meteoalarm_api_url": "https://api.meteoalarm.org/edr/v1/"})
    assert cmd.alerts_provider.base_url == "https://api.meteoalarm.org/edr/v1"
    assert cmd.alerts_provider.locations_url == (
        "https://api.meteoalarm.org/edr/v1/collections/warnings/locations"
    )


def test_meteoalarm_without_a_token_warns_once_and_turns_alerts_off():
    cmd = _wx({"weather_provider": "noaa", "alerts_provider": "meteoalarm"})
    assert cmd.alerts_provider is None
    warnings = _warnings(cmd.logger)
    assert len(warnings) == 1
    assert "meteoalarm_token" in warnings[0]


def test_meteoalarm_with_an_empty_token_warns_once_and_turns_alerts_off():
    cmd = _wx({"weather_provider": "noaa", "alerts_provider": "meteoalarm",
               "meteoalarm_token": "   "})
    assert cmd.alerts_provider is None
    assert len(_warnings(cmd.logger)) == 1


def test_the_help_text_credits_meteoalarm_only_when_it_is_the_alerts_provider():
    with_credit = _wx({"weather_provider": "noaa", "alerts_provider": "meteoalarm",
                       "meteoalarm_token": "s3cr3t-token"})
    assert with_credit.get_help_text().endswith("Alerts: MeteoAlarm/EUMETNET")

    without = _wx({"weather_provider": "noaa"})
    assert "MeteoAlarm" not in without.get_help_text()
