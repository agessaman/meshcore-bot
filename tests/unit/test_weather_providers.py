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
from modules.weather_providers.alerts_meteoalarm import (
    MeteoalarmAlertsProvider,
    reset_rate_limits,
)
from modules.weather_providers.alerts_nws import NwsAlertsProvider
from modules.weather_providers.base import (
    AlertsProvider,
    AlertSummary,
    Capability,
    ForecastReply,
    WeatherProvider,
    WeatherUnits,
)
from modules.weather_providers.metno import MetNorwayProvider
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
    assert set(PROVIDERS) == {"metno", "noaa", "openmeteo"}
    assert PROVIDERS["noaa"] is NoaaProvider
    assert PROVIDERS["openmeteo"] is OpenMeteoProvider
    assert PROVIDERS["metno"] is MetNorwayProvider
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


def _forecast_reply(cmd, extra=None, alerts=None, ok=True, text="BODY"):
    """Drive one default forecast through the command with the network stubbed."""
    cmd.provider.forecast = Mock(return_value=ForecastReply(text=text, extra=extra, ok=ok))
    if alerts is not None:
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
    # A WXSIM source on the global style answers with gwx's own error key.
    assert cmd._get_wxsim_weather("https://w.test/cabin") == cmd.translate(
        "commands.gwx.error_fetching"
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


def test_a_token_with_a_percent_sign_is_read_raw():
    # configparser interpolates '%' on get unless the read is raw, and the
    # plugin loader would log that error instead of building the provider.
    config = configparser.ConfigParser()
    config.read_string(
        "[Weather]\nweather_provider = noaa\nalerts_provider = meteoalarm\n"
        "meteoalarm_token = tok%en\n"
    )
    bot = Mock()
    bot.config = config
    bot.translator = Translator("en")
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))

    cmd = WxCommand(bot)

    assert isinstance(cmd.alerts_provider, MeteoalarmAlertsProvider)
    assert cmd.alerts_provider.token == "tok%en"
    assert _warnings(cmd.logger) == []


def test_the_help_text_credits_meteoalarm_only_when_it_is_the_alerts_provider():
    with_credit = _wx({"weather_provider": "noaa", "alerts_provider": "meteoalarm",
                       "meteoalarm_token": "s3cr3t-token"})
    assert with_credit.get_help_text().endswith("Alerts: MeteoAlarm/EUMETNET")

    without = _wx({"weather_provider": "noaa"})
    assert "MeteoAlarm" not in without.get_help_text()


# --- the alerts option reaches the configured provider ----------------------


def _meteoalarm_command():
    """A NOAA command whose alerts provider is Meteoalarm on a fake session."""
    from tests.unit.test_meteoalarm_alerts import _Clock, _routes, _Session

    # Meteoalarm caches are shared per origin and token, so clear them first.
    reset_rate_limits()
    cmd = _wx({"weather_provider": "noaa", "alerts_provider": "meteoalarm",
               "meteoalarm_token": "s3cr3t-token"})
    provider = cmd.alerts_provider
    assert isinstance(provider, MeteoalarmAlertsProvider)
    provider.session = _Session(_routes())
    clock = _Clock()
    provider._clock = lambda: clock.monotonic
    provider._utcnow = lambda: clock.wall
    provider._wait = clock.advance

    cmd.record_execution = Mock()
    cmd.send_response = AsyncMock(return_value=True)
    cmd._pace_reply = AsyncMock()
    cmd.get_max_message_length = Mock(return_value=130)
    cmd.provider.forecast = Mock(return_value=ForecastReply(text="BODY"))
    return cmd, provider


def test_the_alerts_option_lists_meteoalarm_alerts():
    cmd, _provider = _meteoalarm_command()
    message = SimpleNamespace(content="wx 58.43,5.95 alerts", sender_id="u1")

    assert asyncio.run(cmd.execute(message)) is True

    texts = [call.args[1] for call in cmd.send_response.call_args_list]
    assert len(texts) == 2
    assert texts[0].startswith("2 alerts:\n1. \U0001f7e0 Wind from ")
    assert texts[1].startswith("2. \U0001f7e1 Gale from ")
    assert not any("No weather alerts" in text for text in texts)


def test_the_alerts_option_runs_coverage_off_the_event_loop():
    import threading

    cmd, provider = _meteoalarm_command()
    seen = []
    real = provider.coverage_for

    def coverage_for(lat, lon):
        seen.append(threading.current_thread())
        return real(lat, lon)

    provider.coverage_for = coverage_for
    message = SimpleNamespace(content="wx 58.43,5.95 alerts", sender_id="u1")

    assert asyncio.run(cmd.execute(message)) is True

    assert seen
    assert all(thread is not threading.main_thread() for thread in seen)


def test_the_alerts_option_replies_when_meteoalarm_cannot_fetch():
    from tests.unit.test_meteoalarm_alerts import NO_URL, _Response

    cmd, provider = _meteoalarm_command()
    provider.session.routes[NO_URL] = _Response(500, text="<html>Server Error</html>")
    message = SimpleNamespace(content="wx 58.43,5.95 alerts", sender_id="u1")

    assert asyncio.run(cmd.execute(message)) is True

    texts = [call.args[1] for call in cmd.send_response.call_args_list]
    assert texts == ["Error fetching weather alerts"]


def test_a_broken_meteoalarm_document_does_not_cost_the_gwx_forecast():
    from tests.unit.test_meteoalarm_alerts import LOCATIONS_URL, NO_URL, _Response, _routes, _Session

    reset_rate_limits()
    cmd = GlobalWxCommand(_bot({"alerts_provider": "meteoalarm", "meteoalarm_token": "s3cr3t-token"}))
    provider = cmd.alerts_provider
    assert isinstance(provider, MeteoalarmAlertsProvider)

    # A list where an object belongs, and a null feature: the alerts are lost,
    # the forecast is not.
    routes = _routes()
    routes[LOCATIONS_URL] = _Response(200, [])
    routes[NO_URL] = _Response(200, {"features": [None]})
    provider.session = _Session(routes)
    provider._wait = lambda seconds: None

    cmd.geocode_location = Mock(return_value=(58.43, 5.95, {}, None))
    cmd._get_custom_mqtt_weather_topic = Mock(return_value=None)
    cmd._get_custom_wxsim_source = Mock(return_value=None)
    cmd.provider.forecast = Mock(return_value=ForecastReply(text="BODY"))
    cmd.record_execution = Mock()
    cmd.send_response = AsyncMock(return_value=True)
    cmd._pace_reply = AsyncMock()
    cmd.get_max_message_length = Mock(return_value=130)

    message = SimpleNamespace(content="gwx 58.43,5.95", sender_id="u1")
    assert asyncio.run(cmd.execute(message)) is True

    texts = [call.args[1] for call in cmd.send_response.call_args_list]
    assert len(texts) == 1
    assert "BODY" in texts[0]
    assert "error" not in texts[0].lower()


# --- settings schema --------------------------------------------------------


def test_the_settings_schema_lists_the_meteoalarm_settings():
    from modules.commands.wx_command import _ALERT_PROVIDER_LABELS

    assert _ALERT_PROVIDER_LABELS["meteoalarm"] == "Meteoalarm (Europe)"

    fields = {field["key"]: field for field in WxCommand.settings_schema}
    assert fields["meteoalarm_api_url"]["type"] == "str"
    assert fields["meteoalarm_api_url"]["section"] == "Weather"
    assert fields["meteoalarm_api_url"]["default"] == "https://api.meteogate.eu/warnings"
    assert fields["meteoalarm_token"]["type"] == "password"
    assert fields["meteoalarm_token"]["section"] == "Weather"
    assert "meteoalarm" in [option["value"] for option in fields["alerts_provider"]["options"]]


# --- WXSIM follows the location style ---------------------------------------


def _wxsim_command(weather, wx_section=None):
    cmd = _wx(weather, wx_section)
    cmd.wxsim_parser = Mock()
    cmd.wxsim_parser.fetch_from_url = Mock(return_value="text")
    cmd.wxsim_parser.parse = Mock(return_value=SimpleNamespace(periods=[]))
    cmd.wxsim_parser.is_forecast_stale = Mock(return_value=(False, None))
    cmd.wxsim_parser.format_current_conditions = Mock(return_value="CURRENT")
    return cmd


def test_wxsim_on_openmeteo_uses_the_gwx_timeout_and_error_key():
    cmd = _wxsim_command({"weather_provider": "openmeteo"})
    cmd.wxsim_parser.fetch_from_url = Mock(return_value=None)

    assert cmd._get_wxsim_weather("https://w.test/cabin") == cmd.translate(
        "commands.gwx.error_fetching"
    )
    assert cmd.wxsim_parser.fetch_from_url.call_args.kwargs["timeout"] == 10


def test_wxsim_on_noaa_keeps_the_wx_timeout_and_error_key():
    cmd = _wxsim_command({"weather_provider": "noaa"})
    cmd.wxsim_parser.fetch_from_url = Mock(return_value=None)

    assert cmd._get_wxsim_weather("https://w.test/cabin") == cmd.translate(
        "commands.wx.error", error="Failed to fetch WXSIM data"
    )
    assert cmd.wxsim_parser.fetch_from_url.call_args.kwargs["timeout"] == 8


def test_wxsim_tomorrow_on_openmeteo_uses_the_gwx_key():
    cmd = _wxsim_command({"weather_provider": "openmeteo"})
    cmd.wxsim_parser.parse = Mock(return_value=SimpleNamespace(periods=[object()]))

    assert cmd._get_wxsim_weather("https://w.test/cabin", "tomorrow") == cmd.translate(
        "commands.gwx.tomorrow_not_available"
    )


def test_wxsim_on_openmeteo_reads_weather_units_not_wx_command_units():
    cmd = _wxsim_command(
        {"weather_provider": "openmeteo", "temperature_unit": "celsius"},
        {"temperature_unit": "fahrenheit"},
    )

    assert cmd._get_wxsim_weather("https://w.test/cabin") == "CURRENT"
    assert cmd.wxsim_parser.format_current_conditions.call_args.args[1] == "celsius"


# --- the NWS fetch takes the session lock -----------------------------------


def _nws_provider(lock):
    from modules.nws_coverage import NWSNoCoverageCache

    return NwsAlertsProvider(
        host=_host(), session=Mock(), lock=lock, coverage=NWSNoCoverageCache()
    )


def _host():
    host = Mock()
    host.translate = lambda key, **kw: key
    host.get_max_message_length = Mock(return_value=130)
    host.bot.config.getfloat = Mock(return_value=1.0)
    return host


def test_the_nws_summary_takes_the_session_lock():
    import threading

    from modules.weather_providers.alerts_nws import NO_ALERTS

    lock = threading.Lock()
    provider = _nws_provider(lock)
    provider.get_weather_alerts_noaa = Mock(return_value=NO_ALERTS)

    lock.acquire()
    done = threading.Event()

    def run():
        provider.summary(47.6, -122.3, 130)
        done.set()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert not done.wait(0.2)
    finally:
        lock.release()
    assert done.wait(5)


def test_the_nws_summary_does_not_deadlock_when_the_lock_is_already_held():
    import threading

    from modules.weather_providers.alerts_nws import NO_ALERTS

    lock = threading.RLock()
    provider = _nws_provider(lock)
    provider.get_weather_alerts_noaa = Mock(return_value=NO_ALERTS)

    with lock:
        assert provider.summary(47.6, -122.3, 130) is None


# --- the fallback reply -----------------------------------------------------


def test_a_failed_fallback_fetch_is_the_bare_error():
    cmd = _wx({"weather_provider": "noaa", "openmeteo_fallback": "true"})
    cmd.fallback_provider.forecast = Mock(
        return_value=ForecastReply(text="Error fetching weather data", ok=False)
    )
    cmd.geocode_location = Mock(
        return_value=(48.85, 2.35, {"city": "Paris", "country_code": "fr"}, None)
    )
    cmd._format_location_display = Mock(return_value="Paris, FR")
    cmd.get_max_message_length = Mock(return_value=130)

    reply = cmd._fallback_reply("Paris", "city", 48.85, 2.35, "default", 7, None)

    assert reply == "Error fetching weather data"


def test_an_empty_fallback_hourly_reply_uses_the_gwx_key():
    cmd = _wx({"weather_provider": "noaa", "openmeteo_fallback": "true"})

    assert cmd.fallback_provider.host._pack_hourly_lines([], 130, "commands.gwx") == (
        cmd.translate("commands.gwx.hourly_not_available")
    )


# --- reload -----------------------------------------------------------------


def test_command_only_weather_keys_do_not_need_a_restart():
    from modules.config_reload import ConfigReloadMixin

    class WeatherService:
        config_section = "Weather_Service"

    class _Bot(ConfigReloadMixin):
        pass

    bot = _Bot()
    bot.services = {"weather": WeatherService()}
    old = configparser.ConfigParser()
    old.read_dict({"Weather": {
        "weather_provider": "noaa", "alerts_provider": "auto", "openmeteo_fallback": "false",
        "meteoalarm_token": "", "meteoalarm_api_url": "https://api.meteogate.eu/warnings",
        "temperature_unit": "fahrenheit",
    }})
    new = configparser.ConfigParser()
    new.read_string(
        "[Weather]\nweather_provider = openmeteo\nalerts_provider = meteoalarm\n"
        "openmeteo_fallback = true\nmeteoalarm_token = tok%en\n"
        "meteoalarm_api_url = https://api.meteoalarm.org/edr/v1\n"
        "temperature_unit = fahrenheit\n"
    )

    assert bot._restart_only_config_changes(old, new) == []

    new.set("Weather", "temperature_unit", "celsius")
    assert bot._restart_only_config_changes(old, new) == ["[Weather]"]


# --- a bare Open-Meteo wx ---------------------------------------------------


def test_a_bare_openmeteo_wx_logs_coordinates_without_a_reverse_lookup():
    cmd = _wx({"weather_provider": "openmeteo"})
    cmd._get_custom_mqtt_weather_topic = Mock(return_value=None)
    cmd._get_custom_wxsim_source = Mock(return_value=None)
    cmd._get_companion_location = Mock(return_value=(47.6, -122.3))
    cmd._coordinates_to_location_string_async = AsyncMock(return_value="Seattle")
    cmd.get_weather_for_location = AsyncMock(return_value="WEATHER")
    cmd.send_response = AsyncMock(return_value=True)
    cmd.record_execution = Mock()

    asyncio.run(cmd.execute(SimpleNamespace(content="wx", sender_id="u1")))

    cmd._coordinates_to_location_string_async.assert_not_awaited()
    logged = [call.args[0] for call in cmd.logger.info.call_args_list]
    assert any(line.startswith("Using companion coordinates") for line in logged)
    assert not any("Using companion location" in line for line in logged)


# --- an invalid weather_model is reported once ------------------------------


def test_an_invalid_weather_model_warns_once():
    cmd = _wx({"weather_provider": "openmeteo", "weather_model": "not a model"})

    warnings = [line for line in _warnings(cmd.logger) if "weather_model" in line]
    assert len(warnings) == 1


# --- the alerts reply policy ------------------------------------------------


def test_alerts_outside_nws_coverage_without_the_fallback_reports_a_fetch_error():
    cmd = _alerts_command({"weather_provider": "noaa"})
    cmd.alerts_provider.covers = Mock(return_value=False)
    message = SimpleNamespace(content="wx 98101 alerts", sender_id="u1")

    assert asyncio.run(cmd.execute(message)) is True

    cmd.send_response.assert_awaited_once_with(
        message, cmd.translate("commands.wx.error_fetching")
    )


def test_alerts_outside_nws_coverage_with_the_fallback_says_not_available():
    cmd = _alerts_command({"weather_provider": "noaa", "openmeteo_fallback": "true"})
    cmd.alerts_provider.covers = Mock(return_value=False)
    message = SimpleNamespace(content="wx 98101 alerts", sender_id="u1")

    assert asyncio.run(cmd.execute(message)) is True

    cmd.send_response.assert_awaited_once_with(
        message, cmd.translate("commands.wx.source_option_not_available")
    )


# --- alerts are independent of the forecast ---------------------------------


def test_a_failed_forecast_still_carries_an_independent_alert_summary():
    cmd = _wx({"weather_provider": "noaa", "alerts_provider": "meteoalarm",
               "meteoalarm_token": "s3cr3t"})
    alerts = _FakeAlerts(cmd, AlertSummary(text="ALERTS", count=1))
    cmd._locate_for_forecast = Mock(return_value=(None, 47.6, -122.3, "Portland, US: "))

    # The error goes out unlabeled, as it always has; the alerts follow it.
    assert _forecast_reply(cmd, alerts=alerts, ok=False, text="NOAA DOWN") == (
        "multi_message", "NOAA DOWN", "ALERTS", 1
    )


def test_a_failed_noaa_forecast_does_not_append_nws_alerts():
    cmd = _wx({"weather_provider": "noaa"})
    cmd.alerts_provider.summary = Mock(return_value=AlertSummary(text="ALERTS", count=1))
    cmd._locate_for_forecast = Mock(return_value=(None, 47.6, -122.3, "Portland, US: "))

    assert _forecast_reply(cmd, ok=False, text="NOAA DOWN") == "NOAA DOWN"


# --- the lock covers the provider call, not the alert lookup ----------------


def test_the_alert_lookup_runs_outside_the_noaa_lock():
    import threading

    cmd = _wx({"weather_provider": "noaa", "alerts_provider": "meteoalarm",
               "meteoalarm_token": "s3cr3t"})
    cmd.alerts_provider.covers = Mock(return_value=True)
    probes = {}

    def probe(name):
        """Whether another thread can take the worker lock right now."""
        got = []

        def run():
            acquired = cmd._sync_provider_lock.acquire(timeout=0.2)
            got.append(acquired)
            if acquired:
                cmd._sync_provider_lock.release()

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        probes[name] = got[0]

    def forecast(*args, **kwargs):
        probe("forecast")
        return ForecastReply(text="BODY")

    def summary(lat, lon, max_length):
        probe("summary")
        return AlertSummary(text="ALERTS", count=1)

    cmd.provider.forecast = forecast
    cmd.alerts_provider.summary = summary
    cmd._locate_for_forecast = Mock(return_value=(None, 47.6, -122.3, "Portland, US: "))
    cmd.get_max_message_length = Mock(return_value=130)

    cmd._forecast_for_location("47.6,-122.3", "city", "default", 7, None, False)

    assert probes == {"forecast": False, "summary": True}


# --- regressions: the command side ------------------------------------------


def test_gwx_keeps_its_own_description():
    cmd = GlobalWxCommand(_bot())
    assert cmd.get_metadata()["description"] == (
        "Get weather information for any global location (usage: gwx Tokyo)"
    )

    # An unpinned wx on Open-Meteo answers like gwx, so it says so.
    assert _wx({"weather_provider": "openmeteo"}).description == (
        "Get weather information for any location (usage: wx Tokyo)"
    )


def test_the_noaa_named_helpers_answer_on_openmeteo():
    openmeteo = _wx({"weather_provider": "openmeteo"})
    noaa = _wx({"weather_provider": "noaa"})

    assert openmeteo._noaa_units() == noaa._noaa_units()
    assert openmeteo._noaa_metric_distance == noaa._noaa_metric_distance
    url = "https://api.weather.gov/gridpoints/SEW/125,68/forecast"
    assert openmeteo._noaa_units_url(url) == noaa._noaa_units_url(url)
    assert openmeteo._noaa_wind_convert("10", "mph") == noaa._noaa_wind_convert("10", "mph")


def test_a_percent_in_a_weather_value_does_not_break_wx():
    config = configparser.ConfigParser()
    config.read_string(
        "[Weather]\n"
        "weather_provider = noaa\n"
        "alerts_provider = meteoalarm\n"
        "meteoalarm_token = ab%cd\n"
        "custom.wxsim.seattle = https://example.invalid/plaintext.txt\n"
    )
    bot = Mock()
    bot.config = config
    bot.translator = Translator("en")
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))

    cmd = WxCommand(bot)

    assert cmd._get_custom_wxsim_source("seattle") == "https://example.invalid/plaintext.txt"
    assert cmd._get_custom_wxsim_source("paris") is None
    assert cmd._get_custom_wxsim_source(None) is None


def _meteoalarm_alerts_command():
    """A wx command whose alerts provider is Meteoalarm on a fake session."""
    from tests.unit.test_meteoalarm_alerts import _Clock, _routes, _Session

    reset_rate_limits()
    cmd = _wx({"weather_provider": "noaa", "alerts_provider": "meteoalarm",
               "meteoalarm_token": "s3cr3t-token"})
    provider = cmd.alerts_provider
    provider.session = _Session(_routes())
    clock = _Clock()
    provider._clock = lambda: clock.monotonic
    provider._utcnow = lambda: clock.wall
    provider._wait = clock.advance

    cmd.record_execution = Mock()
    cmd.send_response = AsyncMock(return_value=True)
    cmd._pace_reply = AsyncMock()
    cmd.get_max_message_length = Mock(return_value=130)
    return cmd, provider


def test_meteoalarm_coverage_does_not_take_the_noaa_lock():
    import threading

    cmd, provider = _meteoalarm_alerts_command()
    # The NOAA worker lock is what _run_sync_provider_async takes; an
    # independent alert source must not go through it.
    cmd._run_sync_provider_async = Mock(side_effect=AssertionError("NOAA lock taken"))
    threads = []
    real = provider.coverage_for

    def coverage_for(lat, lon):
        threads.append(threading.current_thread())
        return real(lat, lon)

    provider.coverage_for = coverage_for
    message = SimpleNamespace(content="wx 58.43,5.95 alerts", sender_id="u1")

    assert asyncio.run(cmd.execute(message)) is True

    assert cmd._run_sync_provider_async.call_count == 0
    assert threads and threads[0] is not threading.main_thread()
    texts = [call.args[1] for call in cmd.send_response.call_args_list]
    assert texts and "alerts:" in texts[0]


def test_the_noaa_session_setter_leaves_meteoalarm_alone():
    cmd, provider = _meteoalarm_alerts_command()
    meteoalarm_session = provider.session
    session = Mock()

    cmd.noaa_session = session

    assert cmd.provider.session is session
    assert provider.session is meteoalarm_session


def test_gwx_with_nws_alerts_says_not_available_from_the_first_request():
    cmd = GlobalWxCommand(_bot({"alerts_provider": "nws"}))
    assert isinstance(cmd.alerts_provider, NwsAlertsProvider)
    # A point outside NWS coverage: /alerts answers 404.
    cmd.alerts_provider.session = Mock(
        get=Mock(return_value=Mock(ok=False, status_code=404))
    )
    cmd.geocode_location = Mock(return_value=(48.85, 2.35, {}, None))
    cmd.record_execution = Mock()
    cmd.send_response = AsyncMock(return_value=True)
    cmd._pace_reply = AsyncMock()
    cmd.get_max_message_length = Mock(return_value=130)
    message = SimpleNamespace(content="gwx 48.85,2.35 alerts", sender_id="u1")

    assert asyncio.run(cmd.execute(message)) is True
    assert asyncio.run(cmd.execute(message)) is True

    texts = [call.args[1] for call in cmd.send_response.call_args_list]
    expected = Translator("en").translate("commands.gwx.source_option_not_available")
    assert texts == [expected, expected]


# --- MET Norway, the third forecast provider --------------------------------


def test_metno_answers_wx_end_to_end():
    from tests.unit.test_metno_provider import NOW, _payload, _Response, _Session

    cmd = _wx({"weather_provider": "metno"})
    assert isinstance(cmd.provider, MetNorwayProvider)
    assert cmd.provider.location_style == "global"
    assert cmd.provider.translation_ns == "commands.gwx"
    # auto gives no alerts with a non-NOAA forecast provider.
    assert cmd.alerts_provider is None

    cmd.provider.session = _Session(_Response(200, _payload("tokyo")))
    cmd.provider._clock = lambda: 1000.0
    cmd.provider._now = lambda: NOW
    cmd.geocode_location = Mock(return_value=(35.68, 139.7, {}, None))
    cmd._get_custom_mqtt_weather_topic = Mock(return_value=None)
    cmd._get_custom_wxsim_source = Mock(return_value=None)
    cmd.record_execution = Mock()
    cmd.send_response = AsyncMock(return_value=True)
    cmd._pace_reply = AsyncMock()
    cmd.get_max_message_length = Mock(return_value=150)

    message = SimpleNamespace(content="wx Tokyo", sender_id="u1")
    assert asyncio.run(cmd.execute(message)) is True

    texts = [call.args[1] for call in cmd.send_response.call_args_list]
    # The forecast, then the extreme-conditions warning as its own message.
    assert len(texts) == 2
    assert texts[0].startswith("Today: ")
    assert "°F" in texts[0]
    assert texts[1] == "⚠️ Heavy rain"
