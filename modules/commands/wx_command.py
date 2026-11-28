#!/usr/bin/env python3
"""
Weather command for the MeshCore Bot
Provides weather information using zip codes and NOAA APIs
"""

import asyncio
import re
import threading
import xml.dom.minidom  # noqa: F401  re-exported
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone  # noqa: F401  re-exported
from typing import Any, Callable, Optional, ParamSpec, TypeVar

import requests

from .. import alert_format
from ..clients.mqtt_weather import (  # noqa: F401  re-exported
    get_mqtt_weather_topic,
    load_mqtt_weather_format_config,
    mqtt_weather_display_for_topic,
)

# First-party modules with only required dependencies; they always import.
from ..clients.wxsim_parser import WXSIMParser
from ..http_retry import make_retry_session
from ..location import get_bot_lat_lon, get_companion_lat_lon
from ..models import MeshMessage
from ..nws_alerts import (  # noqa: F401  re-exported
    WX_SPECIAL_RULES,
    entry_nws_headline,
    entry_summary,
    entry_title,
    parse_alert_fields,
)
from ..nws_coverage import NWSNoCoverageCache
from ..template_reference import template_spec
from ..utils import (
    config_value,
    format_temperature_high_low,  # noqa: F401  re-exported
    geocode_city_sync,
    geocode_zipcode_sync,
    get_nominatim_geocoder,
    normalize_us_state,
    rate_limited_nominatim_reverse_sync,
)
from ..weather_common import WeatherCommandMixin, load_open_meteo_model, load_open_meteo_units
from ..weather_providers import (
    ALERT_PROVIDERS,
    PROVIDERS,
    make_alerts_provider,
    make_provider,
    resolve_alerts_provider_name,
)
from ..weather_providers.alerts_dwd import DwdAlertsProvider
from ..weather_providers.alerts_meteoalarm import DEFAULT_BASE_URL as DEFAULT_METEOALARM_API_URL
from ..weather_providers.alerts_meteoalarm import MeteoalarmAlertsProvider
from ..weather_providers.alerts_nws import NwsAlertsProvider
from ..weather_providers.base import (
    AlertsProvider,
    AlertSummary,
    Coverage,
    ForecastReply,
    WeatherProvider,
    WeatherUnits,
)
from ..weather_providers.noaa import (
    ERROR_FETCHING_DATA as _ERROR_FETCHING_DATA,
)
from ..weather_providers.noaa import (
    MIN_BODY_BUDGET as _MIN_BODY_BUDGET,
)
from ..weather_providers.noaa import (
    NO_ALERTS as _NO_ALERTS,
)
from ..weather_providers.noaa import (
    WX_MULTIDAY_MAX_DAYS,  # noqa: F401  re-exported
    NoaaProvider,
    resolve_units,
)
from .base_command import BaseCommand
from .rain_command import nws_http_means_no_coverage  # noqa: F401  re-exported

# Kept for code that checked them; these imports used to be optional.
WX_INTERNATIONAL_AVAILABLE = True
WXSIM_PARSER_AVAILABLE = True


def __getattr__(name: str):
    """``GlobalWxCommand``, imported on first use.

    gwx is a subclass of this command, so importing it here at module level
    would be a cycle. Code that reached it as
    ``modules.commands.wx_command.GlobalWxCommand`` still can.
    """
    if name == "GlobalWxCommand":
        from .alternatives.wx_international import GlobalWxCommand

        return GlobalWxCommand
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Placeholders of the [Weather] temperature_*_format templates (utils.format_temperature_high_low).
_TEMP_FORMAT_PLACEHOLDERS = {
    "high": "Day's high, rounded",
    "low": "Day's low, rounded",
    "units": "Unit symbol, e.g. °F",
    "high_label": "H, or its translation in the reply language",
    "low_label": "L, or its translation in the reply language",
}
_TEMP_FORMAT_NOTES = [
    "A mistake makes the bot log a warning and use the default format instead.",
    "Keep {high_label}/{low_label} to follow the reply language; the preview shows English.",
]

# Settings-schema labels for the registry keys, so a new provider needs one entry
# in modules/weather_providers/ and one line here.
_PROVIDER_LABELS = {
    "noaa": "NOAA (US, includes alerts)",
    "openmeteo": "Open-Meteo (global)",
    "metno": "MET Norway (global)",
    "eccc": "Environment Canada",
}
_ALERT_PROVIDER_LABELS = {
    "nws": "NWS (US)",
    "meteoalarm": "Meteoalarm (Europe)",
    "eccc": "Environment Canada",
    "dwd": "Germany (DWD)",
}

_P = ParamSpec("_P")
_T = TypeVar("_T")


class WxCommand(WeatherCommandMixin, BaseCommand):
    """Handles weather commands with zipcode support"""

    # The global location style's two special forms; the US style matches them inline.
    _COORDINATES_RE = re.compile(r'^\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*$')
    _ZIP_RE = re.compile(r'^\d{5}$')

    # Plugin metadata
    # Read-only informational output; safe for scheduled {cmd:...} rendering.
    render_safe = True
    name = "wx"
    translation_ns = "commands.wx"
    keywords = ['wx', 'weather', 'wxa', 'wxalert']
    category = "weather"
    cooldown_seconds = 5  # 5 second cooldown per user to prevent API abuse
    # NOAA/geocoding need the network, but custom WXSIM/MQTT sources may be LAN-only; check connectivity inside execute paths.
    requires_internet = False

    # Documentation
    short_description = "Get weather for a US location using NOAA weather data"
    usage = "wx <zipcode|city> [tomorrow|<N>d|hourly|alerts]"
    examples = ["wx 98101", "wx seattle", "wx 90210 7d"]
    parameters = [
        {"name": "location", "description": "US zip code or city name"},
        {"name": "option", "description": "tomorrow, Nd (e.g. 7d, 10d), hourly, or alerts (optional)"}
    ]

    # Web-viewer settings schema (see modules/settings_schema.py)
    settings_schema = [
        {"key": "temperature_unit", "label": "Temperature unit", "type": "enum", "section": "Weather",
         "options": [
             {"value": "fahrenheit", "label": "Fahrenheit (°F)"},
             {"value": "celsius", "label": "Celsius (°C)"},
         ],
         "default": "fahrenheit",
         "help": "For wx, gwx, rain and the weather service. Celsius also shows visibility in km. "
                 "Shared weather setting."},
        {"key": "wind_speed_unit", "label": "Wind speed unit", "type": "enum", "section": "Weather",
         "options": [
             {"value": "mph", "label": "Miles per hour (mph)"},
             {"value": "kmh", "label": "Kilometers per hour (km/h)"},
             {"value": "ms", "label": "Meters per second (m/s)"},
             {"value": "kn", "label": "Knots (kn)"},
         ],
         "default": "mph", "help": "For wx, gwx and the weather service. Shared weather setting."},
        {"key": "precipitation_unit", "label": "Precipitation unit", "type": "enum", "section": "Weather",
         "options": [{"value": "inch", "label": "Inches"}, {"value": "mm", "label": "Millimeters"}],
         "default": "inch", "help": "For wx, gwx and the weather service. Shared weather setting."},
        # [Wx_Command] overrides of the shared units. "" (the default) means no
        # override; the save endpoint removes the key rather than writing it blank.
        {
            "key": "temperature_unit",
            "label": "wx-only temperature unit",
            "type": "enum",
            "options": [
                {"value": "", "label": "Same as [Weather]"},
                {"value": "fahrenheit", "label": "Fahrenheit (°F)"},
                {"value": "celsius", "label": "Celsius (°C)"},
            ],
            "default": "",
            "help": "Overrides the shared temperature unit for wx only.",
        },
        {
            "key": "wind_speed_unit",
            "label": "wx-only wind speed unit",
            "type": "enum",
            "options": [
                {"value": "", "label": "Same as [Weather]"},
                {"value": "mph", "label": "Miles per hour (mph)"},
                {"value": "kmh", "label": "Kilometers per hour (km/h)"},
                {"value": "ms", "label": "Meters per second (m/s)"},
                {"value": "kn", "label": "Knots (kn)"},
            ],
            "default": "",
            "help": "Overrides the shared wind speed unit for wx only.",
        },
        {"key": "weather_provider", "label": "Weather provider", "type": "enum", "section": "Weather",
         "options": [{"value": name, "label": _PROVIDER_LABELS.get(name, name)} for name in PROVIDERS],
         "default": "noaa", "help": "API used for forecasts. Shared weather setting."},
        {"key": "alerts_provider", "label": "Weather alerts provider", "type": "enum", "section": "Weather",
         "options": [
             {"value": "auto", "label": "Automatic (NWS with NOAA, none with Open-Meteo)"},
             {"value": "none", "label": "No weather alerts"},
             *({"value": name, "label": _ALERT_PROVIDER_LABELS.get(name, name)} for name in ALERT_PROVIDERS),
         ],
         "default": "auto", "help": "API used for weather alerts, chosen separately from the forecast provider. "
                                    "Shared weather setting."},
        {"key": "meteoalarm_api_url", "label": "Meteoalarm API URL", "type": "str", "section": "Weather",
         "default": DEFAULT_METEOALARM_API_URL,
         "help": "Only with alerts_provider = meteoalarm. Leave the MeteoGate default unless you hold a "
                 "Meteoalarm Direct re-user token. Shared weather setting."},
        {"key": "meteoalarm_token", "label": "Meteoalarm API token", "type": "password", "section": "Weather",
         "default": "",
         "help": "Only with alerts_provider = meteoalarm. Create one in the MeteoGate developer portal. "
                 "Shared weather setting."},
        {"key": "metno_user_agent", "label": "MET Norway User-Agent", "type": "str", "section": "Weather",
         "default": "",
         "help": "Only with weather_provider = metno. Overrides the identifying User-Agent the API's terms "
                 "ask for; leave empty to send the bot's own name and version. Shared weather setting."},
        {"key": "openmeteo_fallback", "label": "Open-Meteo outside NWS coverage", "type": "bool", "section": "Weather",
         "default": False,
         "help": "Only with the noaa or eccc provider: answer wx from Open-Meteo for places the provider "
                 "does not cover (outside the US, or past 150 km from a Canadian city page) instead of "
                 "replying with an error. Shared weather setting."},
        {"key": "failover_provider", "label": "Failover weather provider", "type": "enum", "section": "Weather",
         "options": [
             {"value": "none", "label": "No failover"},
             *({"value": name, "label": _PROVIDER_LABELS.get(name, name)} for name in PROVIDERS),
         ],
         "default": "none",
         "help": "Answers wx and gwx when the weather provider fails (a timeout or server error), in the "
                 "provider's units and location label. Not used for places outside the provider's "
                 "coverage; that is openmeteo_fallback. Shared weather setting."},
        {"key": "default_city", "label": "Default city", "type": "str", "section": "Weather",
         "default": "", "help": "City used for a bare 'wx' when no location is given. Shared weather setting."},
        {"key": "default_state", "label": "Default state", "type": "str", "section": "Weather",
         "default": "", "help": "2-letter state for city disambiguation (e.g. WA). Shared weather setting."},
        {"key": "default_country", "label": "Default country", "type": "str", "section": "Weather",
         "default": "US", "help": "2-letter country code (e.g. US). Shared weather setting."},
        {"key": "always_show_location", "label": "Always name the location", "type": "bool", "section": "Weather",
         "default": False,
         "help": "Name the place in every wx/gwx reply a place is found for, not only when it is outside the "
                 "default state or country. Costs message length, and a reverse lookup for ZIP codes. "
                 "Shared weather setting."},
        {"key": "temperature_high_low_format", "label": "High/low format", "type": "str", "section": "Weather",
         "default": "{high_label}:{high}{units} {low_label}:{low}{units}",
         "help": "How a day's high and low are shown in wx, gwx and the weather service. Shared weather setting.",
         "template": template_spec("format", _TEMP_FORMAT_PLACEHOLDERS, notes=_TEMP_FORMAT_NOTES,
                                   escapes=False, preview="temp_pair",
                                   blank_default="{high_label}:{high}{units} {low_label}:{low}{units}")},
        {"key": "temperature_high_only_format", "label": "High-only format", "type": "str", "section": "Weather",
         "default": "{high_label}:{high}{units}",
         "help": "Used when a day has only a high. Shared weather setting.",
         "template": template_spec("format", _TEMP_FORMAT_PLACEHOLDERS, notes=_TEMP_FORMAT_NOTES,
                                   escapes=False, preview="temp_high",
                                   blank_default="{high_label}:{high}{units}")},
        {"key": "temperature_low_only_format", "label": "Low-only format", "type": "str", "section": "Weather",
         "default": "{low_label}:{low}{units}",
         "help": "Used when a day has only a low, such as tonight's forecast. Shared weather setting.",
         "template": template_spec("format", _TEMP_FORMAT_PLACEHOLDERS, notes=_TEMP_FORMAT_NOTES,
                                   escapes=False, preview="temp_low",
                                   blank_default="{low_label}:{low}{units}")},
        {"key": "use_bot_location_when_no_location", "label": "Bot location as last resort", "type": "bool",
         "section": "Weather", "default": False,
         "help": "A bare wx/gwx with no custom source, companion location or default city uses [Bot] bot_latitude/bot_longitude instead of showing usage. Shared weather setting."},
    ]

    # Error constants
    NO_DATA_NOGPS = "No GPS data available"
    ERROR_FETCHING_DATA = _ERROR_FETCHING_DATA
    NO_ALERTS = _NO_ALERTS

    # Floor for the forecast body once a location prefix has been reserved out of
    # the frame budget, so a long "City, State: " can never squeeze the forecast
    # down to nothing (or negative, which the -10/-20 arithmetic in the period
    # formatters would turn into dropped fields).
    MIN_BODY_BUDGET = _MIN_BODY_BUDGET

    # A subclass that answers from one provider whatever [Weather] weather_provider
    # says (gwx is pinned to Open-Meteo).
    pinned_provider: Optional[str] = None

    # The config section whose `enabled` switch gates this command; None: no switch.
    enabled_section: Optional[str] = 'Wx_Command'

    # The provider that answers when the configured one fails ([Weather]
    # failover_provider), or None. Set per instance in __init__.
    failover_provider: Optional[WeatherProvider] = None

    # The one-line description, in the terms of the provider that answers. It is
    # a class attribute because the settings UI reads it off the class, and
    # __init__ replaces it on the instance when the provider is global.
    description = "Get weather information for a zip code (usage: wx 12345)"

    def __init__(self, bot):
        super().__init__(bot)
        self.wx_enabled = (
            self.get_config_value(self.enabled_section, 'enabled', fallback=True, value_type='bool')
            if self.enabled_section else True
        )

        self.wxsim_parser = WXSIMParser()

        # The forecast provider comes from [Weather] weather_provider. An unknown
        # value warns once and uses NOAA, as an unset one does.
        if self.pinned_provider:
            provider_name = self.pinned_provider
        else:
            provider_name = bot.config.get('Weather', 'weather_provider', fallback='noaa').strip().lower()
            if provider_name not in PROVIDERS:
                self.logger.warning(
                    f"Unknown [Weather] weather_provider '{provider_name}'; using 'noaa'. "
                    f"Valid values: {', '.join(sorted(PROVIDERS))}"
                )
                provider_name = 'noaa'
        self.provider_name = provider_name
        failover_name = self._failover_provider_name(provider_name)

        # A provider that can report no coverage (NOAA outside the US, Environment
        # Canada past 150 km from a city page) can be answered from Open-Meteo.
        fallback_enabled = provider_name in ('noaa', 'eccc') and bot.config.getboolean(
            'Weather', 'openmeteo_fallback', fallback=False
        )

        # An Open-Meteo provider loads [Weather] weather_model, and its warning
        # comes before the unit warnings, as it did when wx built a gwx command.
        openmeteo_model = None
        if provider_name == 'openmeteo' or fallback_enabled or failover_name == 'openmeteo':
            openmeteo_model = load_open_meteo_model(bot.config, self.logger)

        # Units are per provider: NOAA takes [Wx_Command] overrides, Open-Meteo
        # reads [Weather] only, as gwx always has. The fallback provider is an
        # Open-Meteo provider, so it validates and warns about its own units even
        # though wx's units are overlaid on it per request.
        openmeteo_units = None
        if provider_name == 'openmeteo' or fallback_enabled:
            openmeteo_units = WeatherUnits(*load_open_meteo_units(bot.config, self.logger))
        units = openmeteo_units if provider_name == 'openmeteo' else resolve_units(self._unit_setting)

        self.url_timeout = 8  # seconds (reduced from 10 for faster failure detection)
        self.forecast_duration = 3  # days
        self.num_wx_alerts = 2  # number of alerts to show
        self.use_metric = False  # Use imperial units by default
        self.zulu_time = False  # Use local time by default

        # Get default location/state/country from config for fallback/disambiguation
        self.default_city = self.bot.config.get('Weather', 'default_city', fallback='').strip()
        self.default_state = self.bot.config.get('Weather', 'default_state', fallback='')
        self.default_country = self.bot.config.get('Weather', 'default_country', fallback='US')
        self.always_show_location = self.bot.config.getboolean('Weather', 'always_show_location', fallback=False)

        # Initialize geocoder (will use rate-limited helpers for actual calls)
        # Keep geolocator for backwards compatibility, but prefer rate-limited helpers
        self.geolocator = get_nominatim_geocoder()

        # Get database manager for geocoding cache
        self.db_manager = bot.db_manager

        # requests.Session and WXSIM parser instances are shared by this command.
        # Keep provider calls serialized as they were before moving them to worker
        # threads. The lock is acquired inside the worker, never on the event loop.
        # It is re-entrant so an NWS fetch can take it while a worker already
        # holds it for the forecast that asked for the alerts.
        self._sync_provider_lock = threading.RLock()

        # The fetch and formatting live in the providers; this command keeps the
        # request (parsing, geocoding, labels) and the sending. A NOAA command's
        # forecast and alerts providers share one retry session, one worker lock
        # and one coverage cache.
        self._coverage_cache = NWSNoCoverageCache()
        self._shared_session: Optional[requests.Session] = None
        shared = {}
        if provider_name == 'noaa':
            self._shared_session = self._create_retry_session()
            shared = {
                'session': self._shared_session,
                'lock': self._sync_provider_lock,
                'coverage': self._coverage_cache,
                'fallback_enabled': fallback_enabled,
            }
        provider_kwargs = dict(shared)
        if provider_name == 'noaa' and failover_name is not None:
            # NOAA tells a point outside NWS coverage from an outage only while it
            # records coverage, and the failover must answer only an outage.
            provider_kwargs['fallback_enabled'] = True
        if provider_name == 'openmeteo':
            # The model was loaded above, before the unit warnings; handing it to
            # the provider keeps an invalid value from being reported twice.
            provider_kwargs['weather_model'] = openmeteo_model
        self.provider = make_provider(provider_name, host=self, units=units, **provider_kwargs)

        alerts_name = resolve_alerts_provider_name(
            bot.config.get('Weather', 'alerts_provider', fallback='auto'), provider_name, self.logger
        )
        self.alerts_provider = self._build_alerts_provider(alerts_name, shared)

        # The NOAA and NWS methods this command still exposes answer from these
        # when the configured providers are something else.
        self._noaa_compat: Optional[NoaaProvider] = None
        self._nws_compat: Optional[NwsAlertsProvider] = None

        # The Open-Meteo answer for a point outside NWS coverage, in wx's units.
        self.fallback_provider = None
        if fallback_enabled:
            self.fallback_provider = make_provider(
                'openmeteo', host=self, units=openmeteo_units, weather_model=openmeteo_model
            )
            if provider_name == 'noaa':
                self.logger.info(
                    "openmeteo_fallback enabled: wx answers places outside NWS coverage from Open-Meteo"
                )
            else:
                self.logger.info(
                    "openmeteo_fallback enabled: wx answers places outside Environment Canada "
                    "coverage from Open-Meteo"
                )

        # The answer when the provider itself fails (a timeout, a server error),
        # in the provider's units. Built after the fallback so its log line follows.
        if failover_name is not None:
            self.failover_provider = self._build_failover_provider(failover_name, units, openmeteo_model)
            self.logger.info(
                f"failover_provider = {failover_name}: {self.name} answers from "
                f"{failover_name} when {provider_name} fails"
            )

        if provider_name == 'openmeteo' and not self.pinned_provider:
            self.logger.info("Weather provider set to 'openmeteo': wx answers from Open-Meteo")

        # The message catalog follows the provider that answers, so wx on
        # Open-Meteo speaks commands.gwx as gwx always has.
        self.translation_ns = self.provider.translation_ns

        # An unpinned wx on Open-Meteo answers like gwx, so it says so. A pinned
        # command keeps its own class description.
        if self.provider.location_style == "global" and not self.pinned_provider:
            self.description = "Get weather information for any location (usage: wx Tokyo)"

    @property
    def _noaa(self) -> NoaaProvider:
        """The NOAA provider the NOAA-named methods answer from, whatever is configured."""
        provider = self.provider
        if isinstance(provider, NoaaProvider):
            return provider
        if self._noaa_compat is None:
            self._noaa_compat = NoaaProvider(
                host=self,
                units=provider.units,
                session=self._compat_session(),
                lock=self._sync_provider_lock,
                coverage=self._coverage_cache,
                fallback_enabled=False,
            )
        return self._noaa_compat

    @property
    def _nws(self) -> NwsAlertsProvider:
        """The NWS provider the alert methods answer from, whatever is configured."""
        provider = self.alerts_provider
        if isinstance(provider, NwsAlertsProvider):
            return provider
        if self._nws_compat is None:
            self._nws_compat = NwsAlertsProvider(
                host=self,
                session=self._compat_session(),
                lock=self._sync_provider_lock,
                coverage=self._coverage_cache,
                fallback_enabled=False,
            )
        return self._nws_compat

    def _compat_session(self) -> requests.Session:
        """The retry session a compatibility provider shares with the configured ones."""
        if self._shared_session is None:
            self._shared_session = self._create_retry_session()
        return self._shared_session

    @property
    def _nws_no_coverage(self) -> NWSNoCoverageCache:
        """The coverage cache the NOAA and NWS providers share."""
        return self._nws.coverage

    @property
    def noaa_session(self) -> requests.Session:
        """The retry session the NOAA providers share."""
        return self._noaa.session

    @noaa_session.setter
    def noaa_session(self, session: requests.Session) -> None:
        self._shared_session = session
        # Only the providers that share the NOAA session: an alerts provider
        # with its own transport keeps it.
        for provider in (self.provider, self.alerts_provider, self.failover_provider,
                         self._noaa_compat, self._nws_compat):
            if isinstance(provider, (NoaaProvider, NwsAlertsProvider)):
                provider.session = session

    def _unit_setting(self, key: str, fallback: str) -> str:
        """A unit setting: [Wx_Command] when it overrides, else the shared [Weather] one.

        Read directly rather than through get_config_value, whose [Weather] fallback
        logs a notice to move the setting into [Wx_Command]; for units, [Weather] is
        where they belong.
        """
        config = self.bot.config
        for section in ('Wx_Command', 'Weather'):
            if config.has_section(section) and config.has_option(section, key):
                return str(config.get(section, key)).strip().lower()
        return fallback

    def _noaa_units(self) -> tuple[str, str]:
        """Configured (temperature_unit, wind_speed_unit) for NOAA replies."""
        return self._noaa._noaa_units()

    @property
    def _noaa_metric_distance(self) -> bool:
        """Kilometers instead of miles, following the temperature unit as gwx does."""
        return self._noaa._noaa_metric_distance

    def _noaa_units_url(self, url: str) -> str:
        """A NOAA forecast URL asking for SI units (°C, km/h, also in the forecast text) when Celsius is configured."""
        return self._noaa._noaa_units_url(url)

    def _noaa_wind_convert(self, number: str, wind_speed_text: str) -> str:
        """A NOAA wind number in the configured wind_speed_unit (NOAA sends mph, or km/h in SI mode)."""
        return self._noaa._noaa_wind_convert(number, wind_speed_text)

    @staticmethod
    def _noaa_period_temp_symbol(period: dict) -> str:
        return NoaaProvider._noaa_period_temp_symbol(period)

    def _create_retry_session(self) -> requests.Session:
        """Create a requests session with retry logic for NOAA API calls"""
        return make_retry_session()

    def _run_sync_provider(
        self,
        operation: Callable[_P, _T],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        """Run one shared synchronous provider operation under its worker lock."""
        with self._sync_provider_lock:
            return operation(*args, **kwargs)

    async def _run_sync_provider_async(
        self,
        operation: Callable[_P, _T],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        """Move blocking provider work off-loop without concurrent Session use."""
        return await asyncio.to_thread(self._run_sync_provider, operation, *args, **kwargs)

    def _failover_provider_name(self, provider_name: str) -> Optional[str]:
        """The ``[Weather] failover_provider`` key for this command, or None.

        ``none`` (the default) and an empty value turn failover off. An unknown
        value warns and turns it off. A failover that is the command's own
        provider is off too; that warns only for an unpinned command, since gwx
        is pinned to Open-Meteo and ``failover_provider = openmeteo`` is a normal
        setting for a NOAA wx.
        """
        value = self.bot.config.get('Weather', 'failover_provider', fallback='none').strip().lower()
        if value in ('', 'none'):
            return None
        if value not in PROVIDERS:
            self.logger.warning(
                f"Unknown [Weather] failover_provider '{value}'; failover is off. "
                f"Valid values: none, {', '.join(sorted(PROVIDERS))}"
            )
            return None
        if value == provider_name:
            if not self.pinned_provider:
                self.logger.warning(
                    f"[Weather] failover_provider '{value}' is the weather provider itself; failover is off"
                )
            return None
        return value

    def _build_failover_provider(self, name: str, units: WeatherUnits,
                                 openmeteo_model: Any) -> WeatherProvider:
        """The failover forecast provider, sharing the command's NOAA state when it is NOAA.

        A NOAA failover never records points as outside coverage: only the
        configured provider decides where the coverage fallback applies.
        """
        kwargs: dict[str, Any] = {}
        if name == 'noaa':
            kwargs = {
                'session': self._compat_session(),
                'lock': self._sync_provider_lock,
                'coverage': self._coverage_cache,
                'fallback_enabled': False,
            }
        elif name == 'openmeteo':
            kwargs = {'weather_model': openmeteo_model}
        return make_provider(name, host=self, units=units, **kwargs)

    def _build_alerts_provider(self, alerts_name: Optional[str], shared: dict) -> Optional[AlertsProvider]:
        """The configured alerts provider, or None.

        Meteoalarm needs its own token and takes no NOAA state, so it is built
        apart from the providers that share the command's session and lock.
        """
        if not alerts_name:
            return None
        if alerts_name == "meteoalarm":
            # raw=True: a '%' in the token must not be read as interpolation.
            token = self.bot.config.get(
                'Weather', 'meteoalarm_token', fallback='', raw=True
            ).strip()
            if not token:
                self.logger.warning(
                    "alerts_provider = meteoalarm needs [Weather] meteoalarm_token; alerts are off"
                )
                return None
            return make_alerts_provider(
                alerts_name,
                host=self,
                token=token,
                base_url=self.bot.config.get(
                    'Weather', 'meteoalarm_api_url',
                    fallback=DEFAULT_METEOALARM_API_URL, raw=True,
                ).strip().rstrip('/'),
            )
        if alerts_name != 'nws':
            # Only NWS takes the command's NOAA state (session, worker lock,
            # coverage cache). The other alert sources own their session.
            return make_alerts_provider(alerts_name, host=self)
        if self.provider_name != 'noaa':
            # NWS alerts are not the only source when the forecast comes from
            # elsewhere: a point outside NWS coverage reads as "not available",
            # in the forecast provider's catalog, from the first request on.
            shared = {
                **shared,
                'fallback_enabled': True,
                'no_coverage_key': (
                    f'{PROVIDERS[self.provider_name].translation_ns}.source_option_not_available'
                ),
            }
        return make_alerts_provider(alerts_name, host=self, **shared)

    def get_help_text(self, message: MeshMessage | None = None) -> str:
        """The help text of the provider's catalog, with the alert credit when one is owed."""
        if self.provider.location_style == "global":
            text = self.translate(f'{self.provider.translation_ns}.help')
        else:
            text = self.translate(f'{self.provider.translation_ns}.description')
        if isinstance(self.alerts_provider, MeteoalarmAlertsProvider):
            text = f"{text} {self.translate('commands.wx.meteoalarm.attribution')}"
        if isinstance(self.alerts_provider, DwdAlertsProvider):
            text = f"{text} {self.translate('commands.wx.dwd.attribution')}"
        return text

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message starts with a weather keyword"""
        return self._cleaned_content_matches(
            message,
            lambda content_lower: any(
                content_lower.startswith(keyword + ' ') or content_lower == keyword
                for keyword in self.keywords
            ),
        )

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check the wx enable switch, then the base class's channels, cooldown and ACL."""
        # Check if wx command is enabled
        if not self.wx_enabled:
            return False

        return super().can_execute(message, skip_channel_check)

    def get_remaining_cooldown(self, user_id: Optional[str] = None) -> int:
        """Get remaining cooldown time for a specific user"""
        return super().get_remaining_cooldown(user_id)

    def _get_companion_location(self, message: MeshMessage) -> Optional[tuple[float, float]]:
        """Get companion/sender location from the contact-tracking database."""
        return get_companion_lat_lon(self.bot, message, self.logger, error_level="warning", trace=True)

    def _get_bot_location(self) -> Optional[tuple[float, float]]:
        """Get bot location from config ([Bot] bot_latitude, bot_longitude)."""
        return get_bot_lat_lon(self.bot, self.logger)

    def _get_custom_wxsim_source(self, location: Optional[str] = None) -> Optional[str]:
        """Get custom WXSIM source URL from config.

        Looks for keys in [Weather] section with pattern: custom.wxsim.<name> = <url>
        Similar to how Channels_List handles dotted keys.

        Args:
            location: Location name or None for default source

        Returns:
            Optional[str]: Source URL or None if not found
        """

        section = 'Weather'
        if not self.bot.config.has_section(section):
            self.logger.debug(f"Config section '{section}' does not exist")
            return None

        if location:
            # Strip whitespace and normalize
            location = location.strip()
            location_lower = location.lower()
            self.logger.debug(f"Checking for WXSIM source for location: '{location}' (normalized: '{location_lower}')")

            # Look for keys matching custom.wxsim.<location> pattern. Names
            # only: reading every value would interpolate the whole section,
            # and a literal '%' in another value (the Meteoalarm token, say)
            # would raise here. The matched value is interpolated on its own,
            # so "%(base)s/plaintext.txt" still resolves.
            prefix = 'custom.wxsim.'
            for name in self.bot.config.options(section):
                if not name.startswith(prefix):
                    continue
                # Extract the location name from the key (e.g., "custom.wxsim.lethbridge" -> "lethbridge")
                key_location = name[len(prefix):].strip()
                if key_location.lower() != location_lower:
                    continue
                value = config_value(self.bot.config, section, name, self.logger)
                self.logger.debug(f"Found WXSIM source: {name} = {value}")
                return value

            self.logger.debug(f"No WXSIM source found for location '{location}'")
        else:
            # Check for default source: custom.wxsim.default
            default_key = 'custom.wxsim.default'
            if self.bot.config.has_option(section, default_key):
                url = config_value(self.bot.config, section, default_key, self.logger)
                self.logger.debug(f"Found default WXSIM source: {url}")
                return url
            self.logger.debug("No default WXSIM source configured")

        return None

    def _get_wxsim_weather(self, source_url: str, forecast_type: str = "default",
                                num_days: int = 7, message: MeshMessage = None,
                                location_name: Optional[str] = None) -> str:
        """Get and format weather from WXSIM source.

        Args:
            source_url: URL to WXSIM plaintext.txt file
            forecast_type: "default", "tomorrow", or "multiday"
            num_days: Number of days for multiday forecast
            message: The MeshMessage for dynamic length calculation
            location_name: Optional location name for display

        Returns:
            str: Formatted weather string
        """

        if forecast_type in ("hourly", "alerts"):
            return self.translate(f'{self.translation_ns}.source_option_not_available')

        # gwx gave a WXSIM source ten seconds and its own error keys; wx has
        # always used a shorter timeout and the generic error text.
        global_style = self.provider.location_style == "global"
        timeout = 10 if global_style else self.url_timeout

        # Fetch WXSIM data
        text = self.wxsim_parser.fetch_from_url(source_url, timeout=timeout)
        if not text:
            if global_style:
                return self.translate('commands.gwx.error_fetching')
            return self.translate(f'{self.translation_ns}.error', error="Failed to fetch WXSIM data")

        # Parse the data
        forecast = self.wxsim_parser.parse(text)

        # Validate forecast is not stale
        is_stale, stale_reason = self.wxsim_parser.is_forecast_stale(forecast, max_age_hours=48)
        if is_stale:
            self.logger.warning(f"WXSIM forecast appears stale: {stale_reason}")
            # Still return the forecast, but log the warning
            # Optionally, we could return an error message here instead

        # Get unit preferences from config. Canonical section is [Wx_Command];
        # get_config_value falls back to legacy [Weather] for existing setups.
        # gwx read [Weather] alone.
        if global_style:
            temp_unit = self.bot.config.get('Weather', 'temperature_unit', fallback='fahrenheit').lower()
            wind_unit = self.bot.config.get('Weather', 'wind_speed_unit', fallback='mph').lower()
        else:
            temp_unit = self._unit_setting('temperature_unit', 'fahrenheit')
            wind_unit = self._unit_setting('wind_speed_unit', 'mph')

        # Format based on forecast type
        if forecast_type == "tomorrow":
            # Get tomorrow's forecast
            if len(forecast.periods) > 1:
                tomorrow = forecast.periods[1]
                high = self.wxsim_parser._convert_temp(tomorrow.high_temp, temp_unit) if tomorrow.high_temp is not None else None
                low = self.wxsim_parser._convert_temp(tomorrow.low_temp, temp_unit) if tomorrow.low_temp is not None else None
                temp_symbol = "°F" if temp_unit == 'fahrenheit' else "°C"

                result = f"Tomorrow: {tomorrow.conditions}"
                hl = self._format_high_low(high, low, temp_symbol)
                if hl:
                    result += f" {hl}"

                if tomorrow.precip_chance and tomorrow.precip_chance > 30:
                    result += f" {tomorrow.precip_chance}% PoP"

                if location_name:
                    return f"{location_name}: {result}"
                return result
            else:
                if global_style:
                    return self.translate('commands.gwx.tomorrow_not_available')
                return self.translate(f'{self.translation_ns}.error', error="Tomorrow forecast not available")

        elif forecast_type == "multiday":
            # Format multiday forecast
            summary = self.wxsim_parser.format_forecast_summary(forecast, num_days, temp_unit, wind_unit)
            if location_name:
                return f"{location_name}:\n{summary}"
            return summary

        else:
            # Default: current conditions + today's forecast
            current = self.wxsim_parser.format_current_conditions(forecast, temp_unit, wind_unit)

            # Add today's high/low if available (use first period as "today")
            if forecast.periods:
                today = forecast.periods[0]
                high = self.wxsim_parser._convert_temp(today.high_temp, temp_unit) if today.high_temp is not None else None
                low = self.wxsim_parser._convert_temp(today.low_temp, temp_unit) if today.low_temp is not None else None
                temp_symbol = "°F" if temp_unit == 'fahrenheit' else "°C"

                hl_today = self._format_high_low(high, low, temp_symbol)
                if hl_today:
                    current += f" | {hl_today}"

                # Add tomorrow if available (second period)
                if len(forecast.periods) > 1:
                    tomorrow = forecast.periods[1]
                    tomorrow_high = self.wxsim_parser._convert_temp(tomorrow.high_temp, temp_unit) if tomorrow.high_temp is not None else None
                    tomorrow_low = self.wxsim_parser._convert_temp(tomorrow.low_temp, temp_unit) if tomorrow.low_temp is not None else None

                    hl_tom = self._format_high_low(tomorrow_high, tomorrow_low, temp_symbol)
                    if hl_tom:
                        current += f" | Tomorrow: {hl_tom}"

            if location_name:
                return f"{location_name}: {current}"
            return current

    async def _fetch_wxsim(
        self, source: str, forecast_type: str, num_days: int, message: MeshMessage, location: Optional[str]
    ) -> str:
        if location is None:
            return await self._get_wxsim_weather_async(source, forecast_type, num_days, message)
        return await self._get_wxsim_weather_async(
            source, forecast_type, num_days, message, location_name=location
        )

    async def _fallback_place_name(self, lat: float, lon: float) -> Optional[str]:
        """A name for the no-location log line; the global style logs coordinates.

        gwx never spent a reverse lookup on the log line, and the reply's label
        comes from the one lookup the forecast itself makes.
        """
        if self.provider.location_style == "global":
            return None
        return await self._coordinates_to_location_string_async(lat, lon)

    async def _get_wxsim_weather_async(
        self,
        source_url: str,
        forecast_type: str = "default",
        num_days: int = 7,
        message: MeshMessage = None,
        location_name: Optional[str] = None,
    ) -> str:
        return await self._run_sync_provider_async(
            self._get_wxsim_weather,
            source_url,
            forecast_type,
            num_days,
            message,
            location_name,
        )

    def _coordinates_to_location_string(self, lat: float, lon: float) -> Optional[str]:
        """Convert coordinates to a location string (city name) using reverse geocoding.

        Args:
            lat: Latitude.
            lon: Longitude.

        Returns:
            Optional[str]: Location string (city name) or None if geocoding fails.
        """
        try:
            from ..utils import rate_limited_nominatim_reverse_sync
            result = rate_limited_nominatim_reverse_sync(self.bot, f"{lat}, {lon}", timeout=10)
            if result and hasattr(result, 'raw'):
                # Extract city name from address
                address = result.raw.get('address', {})
                city = (address.get('city') or
                       address.get('town') or
                       address.get('village') or
                       address.get('municipality') or
                       address.get('county', ''))
                state = address.get('state', '')

                # Normalize state to abbreviation
                if state:
                    state_abbr, _ = normalize_us_state(state)
                    if state_abbr:
                        state = state_abbr

                if city:
                    if state:
                        return f"{city}, {state}"
                    return city
            return None
        except Exception as e:
            self.logger.debug(f"Error reverse geocoding coordinates {lat}, {lon}: {e}")
            return None

    async def _coordinates_to_location_string_async(
        self, lat: float, lon: float
    ) -> Optional[str]:
        return await self._run_sync_provider_async(
            self._coordinates_to_location_string, lat, lon
        )

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the weather command"""
        content = message.content.strip()

        # Parse the command to extract location and forecast type
        # Support formats: "wx 12345", "wx seattle", "wx paris, tx", "weather everett", "wxa bellingham"
        # New formats: "wx 12345 tomorrow", "wx 12345 7", "wx 12345 7d", "wx 12345 7day", "wx 12345 alerts"
        parts = content.split()

        # Track if we're using companion location (so we always show location in response)
        using_companion_location = False

        # An option alone ("wx hourly") applies to the no-location fallbacks below.
        parts, option_word, option_type, option_days = self._split_option_only(parts, self.provider.max_days)

        # If no location specified, check custom MQTT then WXSIM default sources
        if len(parts) < 2:
            mqtt_topic = self._get_custom_mqtt_weather_topic(None)
            if mqtt_topic:
                return await self._reply_from_mqtt(message, mqtt_topic, option_type, None, split_multiday=False)

            wxsim_source = self._get_custom_wxsim_source(None)  # Check for default
            if wxsim_source:
                return await self._reply_from_wxsim(message, wxsim_source, option_type, option_days)

            # No custom source: the sender's position, default city, then the bot's position
            location_str, using_companion_location = await self._no_location_fallback(message)
            if location_str is None:
                await self.send_response(message, self.translate(f'{self.provider.translation_ns}.usage'))
                return True
            parts = [parts[0], location_str]

        if option_word:
            parts.append(option_word)

        # "alerts" is an option, not part of the location, wherever it comes last.
        if len(parts) > 2 and parts[-1].lower() == "alerts":
            location_parts, forecast_type, num_days = parts[1:-1], "alerts", 7
        elif len(parts) == 2 and self._is_custom_source_name(parts[1]):
            location_parts, forecast_type, num_days = parts[1:], "default", 7
        else:
            location_parts, forecast_type, num_days = self._parse_forecast_suffix(
                parts[1:], self.provider.max_days, allow_hourly=True
            )

        # Join remaining parts to handle "city, state" format
        location = ' '.join(location_parts).strip()

        if not location:
            await self.send_response(message, self.translate(f'{self.provider.translation_ns}.usage'))
            return True

        # Custom MQTT before WXSIM
        mqtt_topic = self._get_custom_mqtt_weather_topic(location)
        if mqtt_topic:
            self.logger.info(f"Using custom MQTT weather topic for location '{location}': {mqtt_topic}")
            return await self._reply_from_mqtt(message, mqtt_topic, forecast_type, location)

        # Check for custom WXSIM source first (before checking location type)
        wxsim_source = self._get_custom_wxsim_source(location)
        if wxsim_source:
            if self.provider.location_style == "us":
                # wx has always named the source it answers from; gwx has not.
                self.logger.info(f"Using custom WXSIM source for location '{location}': {wxsim_source}")
            return await self._reply_from_wxsim(message, wxsim_source, forecast_type, num_days, location)

        return await self._execute_request(
            message, location, forecast_type, num_days, using_companion_location
        )

    # Per location style: the pre-dispatch step, the forecast call, and the
    # subject the error log names. Everything else in a request is shared.
    _STYLE_HOOKS = {
        "us": ("_prepare_us_request", "_us_weather_for_location", "weather command"),
        "global": ("_prepare_global_request", "_global_weather_for_location", "global weather command"),
    }

    def _style_hooks(self) -> tuple:
        """(prepare, forecast, error subject) for the provider's location style."""
        prepare, forecast, subject = self._STYLE_HOOKS[self.provider.location_style]
        return getattr(self, prepare), getattr(self, forecast), subject

    async def _execute_request(self, message: MeshMessage, location: str, forecast_type: str,
                               num_days: int, using_companion_location: bool) -> bool:
        """One weather request: the style's pre-dispatch step, then the forecast or the alert list."""
        prepare, forecast, subject = self._style_hooks()
        location_type = await prepare(message, location, forecast_type)
        if location_type is None:
            # The pre-dispatch step already replied.
            return True

        try:
            # Record execution for this user
            self.record_execution(message.sender_id)

            # Special handling for "alerts" command
            if forecast_type == "alerts":
                # Get alerts only (no weather forecast)
                return await self._send_alert_list_for(message, location, location_type)

            # Get weather data for the location
            weather_data = await forecast(
                location, location_type, forecast_type, num_days, message, using_companion_location
            )

            return await self._send_weather_reply(message, weather_data, forecast_type)

        except Exception as e:
            self.logger.error(f"Error in {subject}: {e}")
            await self.send_response(message, self.translate(f'{self.translation_ns}.error', error=str(e)))
            return True

    async def _prepare_us_request(self, message: MeshMessage, location: str,
                                  forecast_type: str) -> Optional[str]:
        """wx's pre-dispatch step: log the fallback, then name the location type."""
        self.logger.debug(f"No custom WXSIM source found for location '{location}', using normal weather API")

        if re.match(r'^\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*$', location):
            # It's coordinates (lat,lon format)
            return "coordinates"
        if re.match(r'^\s*\d{5}\s*$', location):
            # It's a zipcode (allow surrounding whitespace; strip later in geocode)
            return "zipcode"
        # It's a city name (possibly with state)
        return "city"

    async def _prepare_global_request(self, message: MeshMessage, location: str,
                                      forecast_type: str) -> Optional[str]:
        """gwx's pre-dispatch step: refuse "alerts" before the request is recorded."""
        if forecast_type == "alerts" and self.alerts_provider is None:
            await self.send_response(
                message, self.translate(f'{self.provider.translation_ns}.source_option_not_available')
            )
            return None
        return "city"

    async def _us_weather_for_location(self, location: str, location_type: str, forecast_type: str,
                                       num_days: int, message: MeshMessage,
                                       using_companion_location: bool) -> str:
        """wx's forecast call: the six-argument get_weather_for_location."""
        return await self.get_weather_for_location(
            location, location_type, forecast_type, num_days, message,
            using_companion_location=using_companion_location,
        )

    async def _global_weather_for_location(self, location: str, location_type: str, forecast_type: str,
                                           num_days: int, message: MeshMessage,
                                           using_companion_location: bool) -> str:
        """The global path's forecast call.

        GlobalWxCommand keeps its own four-argument get_weather_for_location, so
        the call goes through this hook rather than being spelled inline.
        """
        return await self.get_weather_for_location(location, "city", forecast_type, num_days, message)

    async def _send_alert_list_for(self, message: MeshMessage, location: str, location_type: str) -> bool:
        """The "alerts" option: the configured alerts provider's full list, or a refusal.

        Both location styles come through here, so "alerts" never reaches a
        forecast provider and a command without an alerts provider says so
        instead of failing.
        """
        if self.alerts_provider is None:
            await self.send_response(
                message, self.translate(f'{self.provider.translation_ns}.source_option_not_available')
            )
            return True

        lat, lon = await self._alert_coordinates(message, location, location_type)
        if lat is None:
            return True

        # Coverage can do HTTP and sleep, so it runs on a worker thread. Only
        # NWS shares the NOAA session and its lock; an independent source must
        # not hold it while it waits on its own API.
        coverage = await self._alerts_coverage_async(lat, lon)
        if coverage is not Coverage.COVERED:
            await self.send_response(message, self.translate(self._alerts_unavailable_key()))
            return True

        return await self._send_full_alert_list(message, lat, lon)

    async def _alerts_coverage_async(self, lat: float, lon: float) -> Coverage:
        """The alerts provider's coverage for a point, off the event loop.

        A provider that raises here costs the alert part only: the point is
        treated as possibly covered, so the list request reports its own fetch
        error instead of the command failing.
        """
        try:
            if isinstance(self.alerts_provider, NwsAlertsProvider):
                return await self._run_sync_provider_async(self.alerts_provider.coverage_for, lat, lon)
            return await asyncio.to_thread(self.alerts_provider.coverage_for, lat, lon)
        except Exception as error:
            self.logger.warning(
                f"Alert coverage lookup failed: {type(error).__name__}: {error}"
            )
            return Coverage.COVERED

    def _alerts_unavailable_key(self) -> str:
        """The reply for an alerts request the provider cannot answer.

        NOAA with the Open-Meteo fallback off has always reported a failed
        fetch, because NWS alerts are the only source it has. Every other
        combination says the source does not cover the place.
        """
        if (
            self.provider_name == 'noaa'
            and self.fallback_provider is None
            and isinstance(self.alerts_provider, NwsAlertsProvider)
        ):
            return f'{self.provider.translation_ns}.error_fetching'
        return f'{self.provider.translation_ns}.source_option_not_available'

    async def _alert_coordinates(self, message: MeshMessage, location: str,
                                 location_type: str) -> tuple[Optional[float], Optional[float]]:
        """Geocode an "alerts" location in the provider's style, replying on failure."""
        if self.provider.location_style == "global":
            lat, lon, _address_info, _geocode_result = await self._run_sync_provider_async(
                self.geocode_location, location
            )
            if lat is None or lon is None:
                await self.send_response(
                    message, self.translate(f'{self.provider.translation_ns}.no_location', location=location)
                )
            return lat, lon

        if location_type == "coordinates":
            try:
                lat_str, lon_str = location.split(',')
                lat = float(lat_str.strip())
                lon = float(lon_str.strip())
                if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                    await self.send_response(message, self.translate(f'{self.translation_ns}.error', error="Invalid coordinates"))
                    return None, None
                return lat, lon
            except ValueError:
                await self.send_response(message, self.translate(f'{self.translation_ns}.error', error=f"Invalid coordinates format: {location}"))
                return None, None

        if location_type == "zipcode":
            lat, lon = await self._zipcode_to_lat_lon_async(location)
            if lat is None or lon is None:
                await self.send_response(message, self.translate(f'{self.translation_ns}.no_location_zipcode', location=location))
            return lat, lon

        result = await self._city_to_lat_lon_async(location)
        if len(result) == 3:
            lat, lon, _address_info = result
        else:
            lat, lon = result
        if lat is None or lon is None:
            region = self.default_state or self.default_country
            await self.send_response(message, self.translate(f'{self.translation_ns}.no_location_city', location=location, state=region))
        return lat, lon

    async def get_weather_for_location(self, location: str, location_type: str, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None, using_companion_location: bool = False) -> str:
        """Run the ordered synchronous geocode/NOAA workflow off the event loop.

        The worker lock is taken inside, around the provider call alone, so an
        independent alert source is never queried while it is held.
        """
        return await asyncio.to_thread(
            self._get_weather_for_location_sync,
            location,
            location_type,
            forecast_type,
            num_days,
            message,
            using_companion_location,
        )

    def _get_weather_for_location_sync(self, location: str, location_type: str, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None, using_companion_location: bool = False) -> str:
        """Get weather data for a location (coordinates, zipcode, or city)

        Args:
            location: The location (coordinates "lat,lon", zipcode, or city name)
            location_type: "coordinates", "zipcode", or "city"
            forecast_type: "default", "tomorrow", "multiday", or "hourly"
            num_days: Number of days for multiday forecast (2–16)
            message: The MeshMessage for dynamic length calculation
            using_companion_location: If True, always include location prefix even if same state
        """
        return self._forecast_for_location(
            location, location_type, forecast_type, num_days, message, using_companion_location
        )

    def _forecast_for_location(self, location: str, location_type: str, forecast_type: str = "default",
                               num_days: int = 7, message: MeshMessage = None,
                               using_companion_location: bool = False) -> str:
        """Geocode, forecast and label one request (runs off the event loop).

        The location style picks the lookup and the label; the forecast provider
        answers, and a point it does not cover goes to the fallback provider,
        labeled the global way.
        """
        try:
            reply, lat, lon, location_prefix = self._locate_for_forecast(
                location, location_type, using_companion_location
            )
            if reply is not None:
                return reply

            # A point the provider already knows it does not cover skips its request.
            if self.fallback_provider is not None and not self.provider.covers(lat, lon):
                return self._fallback_reply(location, location_type, lat, lon, forecast_type, num_days, message)

            # location_prefix is prepended to the formatted body, so it has to come
            # out of the same frame budget. Formatting the body to the full budget
            # and then prefixing it overruns the frame by the prefix's own length --
            # "Lockhart, Texas: " alone is 17 bytes, enough to push an emoji-dense
            # forecast past the 160-byte firmware limit. The alert text goes out as
            # its own message and keeps the full budget.
            max_length = self.get_max_message_length(message) if message else 130
            # The shared session is the provider's, so only its own call is
            # serialized; geocoding and the alert lookup run outside the lock,
            # and a provider with its own session does not take it at all.
            lock = self._sync_provider_lock if self.provider.uses_command_session else nullcontext()
            with lock:
                result = self._forecast_or_error(
                    self.provider, lat, lon, forecast_type, num_days,
                    self._count_display_width(location_prefix), message,
                )
            if result.no_coverage and self.fallback_provider is not None:
                return self._fallback_reply(location, location_type, lat, lon, forecast_type, num_days, message)
            if not result.ok and not result.no_coverage and self.failover_provider is not None:
                failover = self._failover_forecast(
                    result, lat, lon, forecast_type, num_days, location_prefix, message
                )
                if failover is not None:
                    return self._labeled_reply(
                        lat, lon, location_prefix, failover, forecast_type, max_length,
                        alerts=not self._alerts_share_the_forecast_service(),
                    )
            if not result.ok:
                return self._failed_reply(result.text, lat, lon, forecast_type, max_length)

            return self._labeled_reply(lat, lon, location_prefix, result, forecast_type, max_length)

        except Exception as e:
            # wx names the location type; gwx has only one.
            subject = location if self.provider.location_style == "global" else f"{location_type} {location}"
            self.logger.error(f"Error getting weather for {subject}: {e}")
            return self.translate(f'{self.provider.translation_ns}.error', error=str(e))

    def _forecast_or_error(self, provider: Any, lat: float, lon: float, forecast_type: str,
                           num_days: int, location_prefix_len: int,
                           message: Optional[MeshMessage]) -> ForecastReply:
        """One provider's forecast, with an exception turned into a failed reply.

        A provider that raises must cost the forecast and nothing else: the
        failure path then still attaches an independent alert summary, and the
        reply carries the provider's own fetch-error key rather than a traceback.
        """
        try:
            return provider.forecast(
                lat, lon, forecast_type, num_days, location_prefix_len, message
            )
        except Exception as error:
            self.logger.warning(
                f"{provider.name} forecast failed: {type(error).__name__}: {error}"
            )
            return ForecastReply(
                text=self.translate(f'{provider.translation_ns}.error_fetching'), ok=False
            )

    def _failover_forecast(self, failed: ForecastReply, lat: float, lon: float, forecast_type: str,
                           num_days: int, location_prefix: str,
                           message: Optional[MeshMessage]) -> Optional[ForecastReply]:
        """The failover provider's answer after the provider failed, or None when it fails too.

        It answers in the provider's units, under the provider's location
        label, so a reply during an outage reads like any other. A multiday
        request is capped at the failover's own day limit.
        """
        failover = self.failover_provider
        assert failover is not None
        self.logger.warning(
            f"{self.provider.name} forecast failed ({failed.text}); "
            f"answering {round(lat, 2)},{round(lon, 2)} from {failover.name}"
        )
        failover.units = self.provider.units
        lock = self._sync_provider_lock if failover.uses_command_session else nullcontext()
        with lock:
            reply = self._forecast_or_error(
                failover, lat, lon, forecast_type, min(num_days, failover.max_days),
                self._count_display_width(location_prefix), message,
            )
        if reply.ok:
            return reply
        self.logger.warning(
            f"{failover.name} failover failed too ({reply.text}); replying with {self.provider.name}'s error"
        )
        return None

    def _locate_for_forecast(self, location: str, location_type: str, using_companion_location: bool) -> tuple:
        """(error reply or None, lat, lon, location prefix) for a location, in the provider's style."""
        if self.provider.location_style == "global":
            return self._locate_global(location)
        return self._locate_us(location, location_type, using_companion_location)

    def _locate_us(self, location: str, location_type: str, using_companion_location: bool) -> tuple:
        """wx's location types: coordinates, ZIP code and city, with state labels."""
        if location_type == "coordinates":
            return self._locate_coordinates(location)
        if location_type == "zipcode":
            return self._locate_zipcode(location, using_companion_location)
        # An unrecognized type geocodes as a city but never carries the city prefix
        return self._locate_city(location, using_companion_location, labeled=location_type == "city")

    def _locate_global(self, location: str) -> tuple:
        """gwx's single lookup: coordinates, ZIP code or city, with a country label."""
        result = self.geocode_location(location)
        if not result or result[0] is None or result[1] is None:
            return self.translate(f'{self.provider.translation_ns}.no_location', location=location), None, None, ""
        lat, lon, address_info, geocode_result = result
        return None, lat, lon, self._global_location_prefix(location, address_info or {}, geocode_result)

    def _global_location_prefix(self, location: str, address_info: dict, geocode_result: Any) -> str:
        """The "City, CC: " label gwx puts in front of a reply, when it adds information."""
        location_display = ""
        if self._location_label_adds_information(location, address_info):
            location_display = self._format_location_display(address_info, geocode_result, location)
        self.logger.debug(f"Formatted location_display: '{location_display}' from location: '{location}'")
        return f"{location_display}: " if location_display else ""

    def _labeled_reply(self, lat: float, lon: float, location_prefix: str, result: ForecastReply,
                       forecast_type: str, max_length: int, alerts: bool = True) -> str | tuple:
        """The reply with its location label, plus the second message when there is one.

        *alerts* False leaves the alert summary out, for a failover answer whose
        alerts come from the service that just failed.
        """
        weather = f"{location_prefix}{result.text}"
        if forecast_type != "default":
            return weather

        # The alerts provider's summary wins over the forecast's own warning; no
        # configuration produces both today.
        summary = self._alert_summary(lat, lon, max_length) if alerts else None
        if summary is not None:
            # Always send weather first, then alerts in separate message
            self.logger.info(f"Found {summary.count} alerts - using two-message mode")
            return ("multi_message", weather, summary.text, summary.count)

        if result.extra:
            return ("multi_message", weather, result.extra)
        return weather

    def _alerts_share_the_forecast_service(self) -> bool:
        """Whether the alerts come from the same service as the forecast.

        NWS alerts are NOAA's own, so a NOAA failure is the whole answer there.
        Every other alert source is independent of the forecast.
        """
        return self.provider_name == 'noaa' and isinstance(self.alerts_provider, NwsAlertsProvider)

    def _failed_reply(self, text: str, lat: float, lon: float, forecast_type: str,
                      max_length: int) -> str | tuple:
        """A failed forecast, with a confirmed alert summary when one is independent.

        The error text stays the first message, unlabeled, as it always has; the
        alerts ride along as the second one. A non-default forecast type never
        carries alerts, and neither does the NOAA + NWS pairing.
        """
        if forecast_type != "default" or self._alerts_share_the_forecast_service():
            return text
        summary = self._alert_summary(lat, lon, max_length)
        if summary is None:
            return text
        return ("multi_message", text, summary.text, summary.count)

    def _alert_summary(self, lat: float, lon: float, max_length: int) -> Optional[AlertSummary]:
        """The alerts provider's confirmed summary for a point, or None.

        Every alerts provider is wrapped here, so a provider that raises costs
        the alert part of the reply and nothing else: the forecast still goes
        out, and the failure is logged once as a warning.
        """
        alerts = self.alerts_provider
        if alerts is None:
            return None
        try:
            if not alerts.covers(lat, lon):
                return None
            summary = alerts.summary(lat, lon, max_length)
        except Exception as error:
            self.logger.warning(
                f"Alert summary failed: {type(error).__name__}: {error}"
            )
            return None
        if summary is None or summary.count <= 0:
            return None
        return summary

    def _fallback_reply(self, location: str, location_type: str, lat: float, lon: float,
                        forecast_type: str, num_days: int, message: Optional[MeshMessage]):
        """The Open-Meteo reply for a point outside NWS coverage, labeled the global way."""
        openmeteo = self.fallback_provider
        # wx's units, [Wx_Command] overrides included, so one command never mixes unit systems.
        temperature, wind_speed = self._noaa_units()
        precipitation = self._unit_setting('precipitation_unit', openmeteo.units.precipitation)
        if precipitation not in ('inch', 'mm'):
            precipitation = openmeteo.units.precipitation
        openmeteo.units = WeatherUnits(
            temperature=temperature, wind_speed=wind_speed, precipitation=precipitation
        )

        # The label needs the country, which wx's own prefix leaves out. A city's
        # lookup is answered from the geocoding cache wx's lookup just filled; a
        # position needs gwx's reverse lookup.
        geocode_result = None
        if location_type == "coordinates" or location_type == "zipcode":
            _, _, address_info, geocode_result = self.geocode_location(location)
        else:
            result = self.city_to_lat_lon(location)
            address_info = result[2] if len(result) == 3 else None
        self.logger.info(
            "%s,%s is outside %s coverage; answering wx from Open-Meteo",
            round(lat, 2), round(lon, 2),
            "NWS" if self.provider_name == 'noaa' else "Environment Canada",
        )
        location_prefix = self._global_location_prefix(location, address_info or {}, geocode_result)
        max_length = self.get_max_message_length(message) if message else 130
        reply = self._forecast_or_error(
            openmeteo, lat, lon, forecast_type, num_days,
            self._count_display_width(location_prefix), message,
        )
        if not reply.ok:
            # The base sent the error on its own, without the location label.
            return self._failed_reply(reply.text, lat, lon, forecast_type, max_length)
        return self._labeled_reply(lat, lon, location_prefix, reply, forecast_type, max_length)

    def geocode_location(self, location: str) -> tuple:
        """Convert location string to lat/lon with address details.

        Handles both coordinate strings (lat,lon) and city names.
        Uses geocode_city_sync for proper default state/country handling,
        which prioritizes locations in the configured default state/country.

        Args:
            location: Location string (e.g., "Seattle" or "47.6,-122.3").

        Returns:
            tuple: (lat, lon, address_info, geocode_result) or (None, None, None, None) on failure.
        """
        try:
            # Check if location is coordinates (decimal numbers separated by comma, with optional spaces)
            # Handle formats like: "47.6,-122.3", "47.6, -122.3", "47.980525, -122.150649", " -47.6 , 122.3 "
            if self._COORDINATES_RE.match(location):
                return self._geocode_coordinates(location)

            # US ZIP code (5 digits): use geocode_zipcode_sync so the query is "zip, US"
            # and we don't get non‑US matches (e.g. "98104" -> Lithuania) from Nominatim.
            if self._ZIP_RE.match(location.strip()):
                return self._geocode_zipcode(location)

            return self._geocode_city(location)

        except Exception as e:
            self.logger.error(f"Error geocoding location {location}: {e}")
            return None, None, None, None

    def _geocode_coordinates(self, location: str) -> tuple:
        """geocode_location for a "lat,lon" string: validate, then reverse geocode for a label."""
        # Parse lat,lon coordinates
        try:
            lat_str, lon_str = location.split(',')
            lat = float(lat_str.strip())
            lon = float(lon_str.strip())

            # Validate coordinate ranges
            if not (-90 <= lat <= 90):
                self.logger.warning(f"Invalid latitude: {lat}. Must be between -90 and 90.")
                return None, None, None, None
            if not (-180 <= lon <= 180):
                self.logger.warning(f"Invalid longitude: {lon}. Must be between -180 and 180.")
                return None, None, None, None

            # Get address info via reverse geocoding
            address_info = None
            geocode_result = None
            try:
                reverse_location = rate_limited_nominatim_reverse_sync(
                    self.bot, f"{lat}, {lon}", timeout=10
                )
                if reverse_location:
                    geocode_result = reverse_location
                    address_info = reverse_location.raw.get('address', {})
            except Exception as e:
                self.logger.debug(f"Reverse geocoding failed for coordinates: {e}")
                address_info = {}

            return lat, lon, address_info or {}, geocode_result
        except ValueError:
            self.logger.warning(f"Invalid coordinates format: {location}")
            return None, None, None, None

    def _geocode_zipcode(self, location: str) -> tuple:
        """geocode_location for a US ZIP code (never falls through to a city lookup)."""
        lat, lon = geocode_zipcode_sync(
            self.bot, location,
            default_country=self.default_country,
            timeout=10
        )
        if lat is not None and lon is not None:
            # A ZIP code is not named in the reply (as in wx), so no reverse lookup,
            # unless [Weather] always_show_location asks for every place to be named.
            if not self.always_show_location:
                return lat, lon, {}, None
            try:
                reverse_location = rate_limited_nominatim_reverse_sync(
                    self.bot, f"{lat}, {lon}", timeout=10
                )
            except Exception as e:
                self.logger.debug(f"Reverse geocoding failed for ZIP code {location}: {e}")
                reverse_location = None
            if reverse_location:
                return lat, lon, reverse_location.raw.get('address', {}) or {}, reverse_location
            return lat, lon, {}, None
        # Invalid or unknown US ZIP; do not fall through to city (avoids foreign matches)
        return None, None, None, None

    def _geocode_city(self, location: str) -> tuple:
        """geocode_location for a place name, preferring the default state and country."""
        # Use the shared geocode_city_sync function which properly handles
        # default state and country for city disambiguation
        # This ensures "olympia" matches Olympia, WA (not Greece) when default_state=WA
        lat, lon, address_info = geocode_city_sync(
            self.bot, location,
            default_state=self.default_state,
            default_country=self.default_country,
            include_address_info=True,
            timeout=10
        )

        if lat is None or lon is None:
            return None, None, None, None

        # Get full geocode result for display name formatting
        # Try reverse geocoding to get the full result object
        geocode_result = None
        try:
            reverse_location = rate_limited_nominatim_reverse_sync(
                self.bot, f"{lat}, {lon}", timeout=10
            )
            if reverse_location:
                geocode_result = reverse_location
        except Exception:
            # If reverse geocoding fails, we still have lat/lon and address_info
            pass

        return lat, lon, address_info or {}, geocode_result

    def _location_label_adds_information(self, location: str, address_info: Optional[dict]) -> bool:
        """Whether the reply should name the place, as wx decides: only when it adds information.

        Coordinates (typed, or the sender's or bot's own position) are named when a
        place was found for them. A ZIP code is not named. A city is named when it
        resolved to another country than [Weather] default_country or, in the US, to
        another state than default_state (or no default_state is set).

        With [Weather] always_show_location, any place a lookup found is named.
        """
        if not address_info:
            return False
        is_zip = bool(self._ZIP_RE.match(location.strip()))
        if is_zip and not self.always_show_location:
            return False
        if is_zip or self._COORDINATES_RE.match(location):
            # Without a place name the label would only repeat the ZIP code or coordinates.
            return any(address_info.get(field) for field in ('city', 'town', 'village', 'municipality', 'city_district'))
        if self.always_show_location:
            return True
        country = (address_info.get('country_code') or '').upper()
        default_country = (self.default_country or '').strip().upper()
        if country and default_country and country != default_country:
            return True
        if country != 'US':
            return False
        state = address_info.get('state') or ''
        if not state or not self.default_state:
            return bool(state)
        return self._state_key(state) != self._state_key(self.default_state)

    def _state_key(self, state: str) -> str:
        """A US state as its abbreviation, for comparing "Washington" with "WA"."""
        abbreviation, _ = normalize_us_state(state)
        return (abbreviation or self._get_state_abbreviation(state.strip())).upper()

    def _format_location_display(self, address_info: dict, geocode_result: Any, fallback: str) -> str:
        """Format location name for display from address info - returns 'City, CountryCode' format.

        Args:
            address_info: Dictionary containing address details.
            geocode_result: Full geocode result object.
            fallback: Fallback location string if detailed info is missing.

        Returns:
            str: Formatted location string (e.g., "Seattle, US").
        """
        # Get country code first (prefer this over full country name)
        country_code = ''
        if address_info:
            country_code = address_info.get('country_code', '').upper()

        city = self._display_city(address_info, geocode_result)

        # For US locations, include state abbreviation
        if country_code == 'US':
            state = None
            if address_info:
                state = address_info.get('state')
            if city and state:
                state_abbrev = self._get_state_abbreviation(state)
                return f"{city}, {state_abbrev}"
            elif city:
                return f"{city}, US"

        # For international locations, always use country code if available
        if city:
            if country_code:
                return f"{city}, {country_code}"
            elif address_info and address_info.get('country'):
                # Fallback to country name if no code available
                country = address_info.get('country')
                # Shorten very long country names
                if len(country) > 15:
                    return f"{city}, {country[:15]}"
                return f"{city}, {country}"
            else:
                return city

        return self._fallback_location_label(fallback, country_code)

    def _display_city(self, address_info: dict, geocode_result: Any) -> Optional[str]:
        """The city part of _format_location_display: address fields, then display_name."""
        # Try to get city name from address_info (this is more reliable than display_name)
        city = None
        if address_info:
            # Try various address fields in order of preference
            city = (address_info.get('city') or
                    address_info.get('town') or
                    address_info.get('village') or
                    address_info.get('municipality') or
                    address_info.get('city_district'))

            # If we still don't have a city, try parsing from display_name
            if not city and geocode_result and hasattr(geocode_result, 'raw'):
                display_name = geocode_result.raw.get('display_name', '')
                if display_name:
                    # Parse display_name - usually format is "Place, City, State/Province, Country"
                    # We want the city, not the specific place
                    parts = [p.strip() for p in display_name.split(',')]
                    # Skip the first part (specific location) and look for city in later parts
                    for i, part in enumerate(parts[1:], 1):
                        # Check if this part looks like a city (not a state/province or country)
                        if i < len(parts) - 1:  # Not the last part (country)
                            city = part
                            break

        # If still no city, try extracting from display_name first part (but clean it up)
        if not city and geocode_result and hasattr(geocode_result, 'raw'):
            display_name = geocode_result.raw.get('display_name', '')
            if display_name:
                parts = [p.strip() for p in display_name.split(',')]
                if parts:
                    # Take first part but try to extract city name
                    first_part = parts[0]
                    # Remove common venue/location suffixes
                    for suffix in [' Terminal', ' Station', ' Airport', ' Hotel', ' Building',
                                   ' Plaza', ' Center', ' Centre', ' Park', ' Square']:
                        if suffix in first_part:
                            first_part = first_part.replace(suffix, '').strip()
                    city = first_part

        return city

    def _fallback_location_label(self, fallback: str, country_code: str) -> str:
        """_format_location_display's label when no city was found: built from the input."""
        # Final fallback: try to extract from input and capitalize
        if fallback:
            # Try to extract city name from input (before first comma if present)
            parts = fallback.split(',')
            city_part = parts[0].strip().title()
            # Remove common suffixes
            for suffix in [' Terminal', ' Station', ' Airport', ' Hotel', ' Building']:
                if suffix in city_part:
                    city_part = city_part.replace(suffix, '').strip()

            if country_code:
                return f"{city_part}, {country_code}"
            elif len(parts) > 1:
                # Try to get country from input
                country_part = parts[-1].strip()
                return f"{city_part}, {country_part[:10]}"  # Limit country name length
            return city_part

        return fallback.title()

    def _get_state_abbreviation(self, state: str) -> str:
        """Convert full state name to abbreviation.

        Args:
            state: Full state name (e.g., "Washington").

        Returns:
            str: Two-letter state abbreviation (e.g., "WA") or original string if not found.
        """
        state_map = {
            'Washington': 'WA', 'California': 'CA', 'New York': 'NY', 'Texas': 'TX',
            'Florida': 'FL', 'Illinois': 'IL', 'Pennsylvania': 'PA', 'Ohio': 'OH',
            'Georgia': 'GA', 'North Carolina': 'NC', 'Michigan': 'MI', 'New Jersey': 'NJ',
            'Virginia': 'VA', 'Tennessee': 'TN', 'Indiana': 'IN', 'Arizona': 'AZ',
            'Massachusetts': 'MA', 'Missouri': 'MO', 'Maryland': 'MD', 'Wisconsin': 'WI',
            'Colorado': 'CO', 'Minnesota': 'MN', 'South Carolina': 'SC', 'Alabama': 'AL',
            'Louisiana': 'LA', 'Kentucky': 'KY', 'Oregon': 'OR', 'Oklahoma': 'OK',
            'Connecticut': 'CT', 'Utah': 'UT', 'Iowa': 'IA', 'Nevada': 'NV',
            'Arkansas': 'AR', 'Mississippi': 'MS', 'Kansas': 'KS', 'New Mexico': 'NM',
            'Nebraska': 'NE', 'West Virginia': 'WV', 'Idaho': 'ID', 'Hawaii': 'HI',
            'New Hampshire': 'NH', 'Maine': 'ME', 'Montana': 'MT', 'Rhode Island': 'RI',
            'Delaware': 'DE', 'South Dakota': 'SD', 'North Dakota': 'ND', 'Alaska': 'AK',
            'Vermont': 'VT', 'Wyoming': 'WY'
        }
        return state_map.get(state, state)

    def _locate_coordinates(self, location: str) -> tuple:
        """(error reply or None, lat, lon, location prefix) for a "lat,lon" location."""
        # Parse coordinates from "lat,lon" format
        try:
            lat_str, lon_str = location.split(',')
            lat = float(lat_str.strip())
            lon = float(lon_str.strip())

            # Validate coordinate ranges
            if not (-90 <= lat <= 90):
                return self.translate(f'{self.translation_ns}.error', error=f"Invalid latitude: {lat}"), None, None, ""
            if not (-180 <= lon <= 180):
                return self.translate(f'{self.translation_ns}.error', error=f"Invalid longitude: {lon}"), None, None, ""

            # Get address_info for location display via reverse geocoding
            location_str = self._coordinates_to_location_string(lat, lon)
            if location_str:
                # Parse the location string to get city and state for address_info
                parts = location_str.split(',')
                if len(parts) >= 2:
                    city = parts[0].strip()
                    state = parts[1].strip()
                    address_info = {'city': city, 'state': state}
                else:
                    address_info = {'city': location_str}
            else:
                address_info = {}
        except ValueError:
            return self.translate(f'{self.translation_ns}.error', error=f"Invalid coordinates format: {location}"), None, None, ""

        location_prefix = ""
        if address_info:
            # For coordinates, always show location if we have address info
            city = address_info.get('city', '')
            state = address_info.get('state', '')
            if city and state:
                # Normalize state to abbreviation
                state_abbr, _ = normalize_us_state(state)
                if state_abbr:
                    state = state_abbr
                location_prefix = f"{city}, {state}: "
            elif city:
                location_prefix = f"{city}: "
        return None, lat, lon, location_prefix

    def _locate_zipcode(self, location: str, using_companion_location: bool) -> tuple:
        """(error reply or None, lat, lon, location prefix) for a zipcode location."""
        lat, lon = self.zipcode_to_lat_lon(location)
        if lat is None or lon is None:
            return self.translate(f'{self.translation_ns}.no_location_zipcode', location=location), None, None, ""

        location_prefix = ""
        if using_companion_location or self.always_show_location:
            # For zipcode with companion location, try to get city name from reverse geocoding
            location_str = self._coordinates_to_location_string(lat, lon)
            if location_str:
                location_prefix = f"{location_str}: "
        return None, lat, lon, location_prefix

    def _locate_city(self, location: str, using_companion_location: bool, labeled: bool = True) -> tuple:
        """(error reply or None, lat, lon, location prefix) for a city location."""
        result = self.city_to_lat_lon(location)
        if len(result) == 3:
            lat, lon, address_info = result
        else:
            lat, lon = result
            address_info = None

        if lat is None or lon is None:
            region = self.default_state or self.default_country
            return self.translate(f'{self.translation_ns}.no_location_city', location=location, state=region), None, None, ""

        # Check if the found city is in a different state than default
        actual_city = location
        actual_state = self.default_state or self.default_country
        if address_info:
            # Try to get the best city name from various address fields
            actual_city = (address_info.get('city') or
                         address_info.get('town') or
                         address_info.get('village') or
                         address_info.get('hamlet') or
                         address_info.get('municipality') or
                         location)
            actual_state = address_info.get('state', self.default_state)
            # Convert full state name to abbreviation if needed using the us library
            if len(actual_state) > 2:
                state_abbr, _ = normalize_us_state(actual_state)
                if state_abbr:
                    actual_state = state_abbr

            # Also check if the default state needs to be converted for comparison
            default_state_full = self.default_state
            if len(self.default_state) == 2:
                # Convert abbreviation to full name for comparison
                _, default_state_full = normalize_us_state(self.default_state)
                if not default_state_full:
                    default_state_full = self.default_state

        # Add location info if city is in a different state than default, or if using companion location
        location_prefix = ""
        if labeled and address_info:
            # Compare states (handle both full names and abbreviations)
            states_different = (actual_state != self.default_state and
                              actual_state != default_state_full)
            # Always show location if using companion location, or if state is different
            if using_companion_location or states_different or self.always_show_location:
                location_prefix = f"{actual_city}, {actual_state}: " if actual_state else f"{actual_city}: "
        return None, lat, lon, location_prefix

    def _noaa_forecast_body(self, lat: float, lon: float, forecast_type: str, num_days: int, body_max_length: int) -> tuple:
        """(error reply or None, formatted forecast) for one forecast type."""
        return self._noaa._noaa_forecast_body(lat, lon, forecast_type, num_days, body_max_length)

    def zipcode_to_lat_lon(self, zipcode: str) -> tuple:
        """Convert zipcode to latitude and longitude"""
        try:
            lat, lon = geocode_zipcode_sync(self.bot, zipcode, timeout=10)
            return lat, lon
        except Exception as e:
            self.logger.error(f"Error geocoding zipcode {zipcode}: {e}")
            return None, None

    async def _zipcode_to_lat_lon_async(self, zipcode: str) -> tuple:
        return await self._run_sync_provider_async(self.zipcode_to_lat_lon, zipcode)

    def city_to_lat_lon(self, city: str) -> tuple:
        """Convert city name to latitude and longitude using default state"""
        try:
            # Use shared geocode_city_sync function with address info
            default_country = self.bot.config.get('Weather', 'default_country', fallback='US')
            lat, lon, address_info = geocode_city_sync(
                self.bot, city, default_state=self.default_state,
                default_country=default_country,
                include_address_info=True, timeout=10
            )

            # Explicit None checks: 0 is a valid latitude (equator) or longitude (prime meridian).
            if lat is not None and lon is not None:
                return lat, lon, address_info or {}
            else:
                return None, None, None
        except Exception as e:
            self.logger.error(f"Error geocoding city {city}: {e}")
            return None, None, None

    async def _city_to_lat_lon_async(self, city: str) -> tuple:
        return await self._run_sync_provider_async(self.city_to_lat_lon, city)

    def _noaa_fetch(self, url: str, what: str, point: Optional[tuple[float, float]] = None):
        """GET *url* through the NOAA session; None, after a warning naming *what*,
        on an HTTP error status, a timeout or a connection error.

        With the Open-Meteo fallback on, a *point* request also records whether
        NWS covers that point.
        """
        return self._noaa._noaa_fetch(url, what, point)

    def get_noaa_weather(self, lat: float, lon: float, return_periods: bool = False, max_length: int = 130) -> tuple:
        """Get weather forecast from NOAA and return both weather string and points data

        Args:
            lat: Latitude
            lon: Longitude
            return_periods: If True, return forecast periods array instead of formatted string
            max_length: Maximum message length (default 130 for backwards compatibility)

        Returns:
            Tuple of (weather_string_or_periods, points_data)
        """
        return self._noaa.get_noaa_weather(lat, lon, return_periods, max_length)

    def _noaa_current_summary(self, current: dict, weather_json: dict, max_length: int) -> str:
        """The current period's line: name, sky, temperature, wind and as many details as fit."""
        return self._noaa._noaa_current_summary(current, weather_json, max_length)

    def _append_noaa_tomorrow(self, weather: str, period: dict, is_current_tonight: bool, is_current_night: bool, max_length: int) -> str:
        """``weather`` with the Tomorrow period appended when it fits; a night reply shortens its text and is more lenient."""
        return self._noaa._append_noaa_tomorrow(weather, period, is_current_tonight, is_current_night, max_length)

    def _append_noaa_period(self, weather: str, period: dict, max_length: int) -> str:
        """``weather`` with a Today or Tonight period appended when it fits.

        The period gets wind and details only while the reply so far leaves room,
        so the current period keeps its full details first.
        """
        return self._noaa._append_noaa_period(weather, period, max_length)

    def _noaa_followup_periods(self, forecast: list, current: dict) -> tuple:
        """The ``(index, period)`` pairs for Today, Tonight and Tomorrow after ``current``, and whether
        ``current`` is Tonight or any night period: ``(today, tonight, tomorrow, is_tonight, is_night)``."""
        return self._noaa._noaa_followup_periods(forecast, current)

    def _noaa_period_str(self, period: dict, forecast_text: Optional[str] = None) -> Optional[str]:
        """``" | Name: <emoji><forecast> <high/low or temp°>"`` for a forecast period.

        None when the period has no temperature or no short forecast.
        ``forecast_text`` replaces the short forecast in the text (the emoji
        still comes from the short forecast).
        """
        return self._noaa._noaa_period_str(period, forecast_text)

    def _noaa_period_wind(self, weather: str, period_str: str, period: dict, threshold: int, max_length: int) -> str:
        """Append the period's wind to ``period_str`` when ``weather + period_str`` is under ``threshold``
        display columns and the result still fits ``max_length``."""
        return self._noaa._noaa_period_wind(weather, period_str, period, threshold, max_length)

    @staticmethod
    def _abbreviate_noaa_forecast(period_short: str) -> str:
        """Shorten a long short-forecast ("A then B and C") to its main conditions."""
        return NoaaProvider._abbreviate_noaa_forecast(period_short)

    def get_noaa_hourly_weather(self, lat: float, lon: float) -> tuple:
        """Get hourly weather forecast from NOAA

        Args:
            lat: Latitude
            lon: Longitude

        Returns:
            Tuple of (hourly_periods_list, points_data)
        """
        return self._noaa.get_noaa_hourly_weather(lat, lon)

    def format_hourly_forecast(self, hourly_periods: list, max_length: int = 130) -> str:
        """Format hourly forecast to fit as many hours as possible in max_length bytes

        Args:
            hourly_periods: List of hourly forecast periods from NOAA
            max_length: Maximum message length (default 130 for backwards compatibility)

        Returns:
            Formatted string with one hour per line
        """
        return self._noaa.format_hourly_forecast(hourly_periods, max_length)

    @staticmethod
    def _parse_noaa_start_time(start_time_str: str) -> datetime:
        """Parse a NOAA ISO ``startTime``; a trailing ``Z`` means UTC."""
        return NoaaProvider._parse_noaa_start_time(start_time_str)

    def _future_hourly_periods(self, hourly_periods: list) -> list:
        """Periods starting after now; unparseable or missing times are kept.

        NOAA times carry the location's UTC offset, so they are compared as
        absolute times. Dropping the offset compared the location's wall clock
        with the bot's, which kept past hours or dropped future ones whenever
        the two were in different time zones.
        """
        return self._noaa._future_hourly_periods(hourly_periods)

    def _hourly_line(self, period: dict) -> str:
        """One hour as "10AM: 🌦️ 26% Chance Light Rain 49° SSW5"."""
        return self._noaa._hourly_line(period)

    @staticmethod
    def _period_date(period: dict):
        """The local calendar date a NOAA period starts on, from its startTime, or None.

        NOAA writes startTime in the location's own offset ("2026-10-01T18:00:00-07:00"),
        so its date part is the local date there, whatever the bot's clock says.
        """
        return NoaaProvider._period_date(period)

    def _forecast_today(self, forecast: list):
        """Today's date at the forecast's location, or the bot's date when no period has an offset.

        The location's "now" is the current instant in the UTC offset NOAA writes on
        its period times. The first period's own date is not enough: at 1 AM the
        first period can still be the night that started yesterday evening.
        """
        return self._noaa._forecast_today(forecast)

    def _period_dates(self, forecast: list) -> list:
        """Each period's local date: its startTime date, or inferred from its neighbors when it has none.

        NOAA periods alternate day and night in order, so an undated period is the
        same date as a preceding daytime period when it is a night, else the day after.
        """
        return self._noaa._period_dates(forecast)

    def _find_tomorrow_periods(self, forecast: list) -> list:
        """Tomorrow's NOAA periods: those dated the day after today at the location;
        without dates, those named "Tomorrow", else named for tomorrow's weekday, else the
        (up to two) periods after today's.
        """
        return self._noaa._find_tomorrow_periods(forecast)

    def format_tomorrow_forecast(self, forecast: list, max_length: int = 130) -> str:
        """Format a detailed forecast for tomorrow"""
        return self._noaa.format_tomorrow_forecast(forecast, max_length)

    def _fit_tomorrow_parts(self, parts: list[tuple[str, str]], max_length: int) -> str:
        """Join tomorrow's periods within *max_length* UTF-8 bytes.

        Each part is (with wind, without wind). The later periods' wind goes
        first, then the first period's, then the later periods themselves. When
        even the first period alone does not fit, it is sent anyway (the send
        path splits it) rather than leaving the reply empty.
        """
        return self._noaa._fit_tomorrow_parts(parts, max_length)

    def format_multiday_forecast(self, forecast: list, num_days: int = 7, max_length: int = 130) -> str:
        """Format a less detailed multi-day forecast summary"""
        return self._noaa.format_multiday_forecast(forecast, num_days, max_length)

    def _multiday_lines_by_date(self, forecast: list, num_days: int) -> list[str]:
        """One line per local date after today at the location, up to *num_days*.

        Grouping by startTime date (instead of weekday name against the bot's
        clock) keeps holiday-named periods ("Christmas Day") and does not depend
        on the bot sharing the location's time zone. A daytime period wins over
        that date's night period.
        """
        return self._noaa._multiday_lines_by_date(forecast, num_days)

    def _day_abbrev(self, weekday: int) -> str:
        """The multi-day label for a weekday (0 = Monday), translated (English "M", "Th", "Sa")."""
        return self._noaa._day_abbrev(weekday)

    @staticmethod
    def _multiday_day_name(period_name_lower: str) -> str | None:
        """The weekday a NOAA period belongs to, or None to skip it.

        Part-of-day periods (Tonight, This Afternoon) count only when they name a
        weekday; otherwise Today and Tomorrow map to the current and next weekday.
        """
        return NoaaProvider._multiday_day_name(period_name_lower)

    def _add_period_details(self, period_str: str, detailed_forecast: str, current_weather_length: int, max_length: int = 130, observation_data: dict = None) -> str:
        """Add additional details (humidity, dew point, visibility, etc.) to a period string

        Args:
            period_str: The base period string (e.g., " | Today: ☀️Sunny 75°")
            detailed_forecast: The detailed forecast text to extract info from
            current_weather_length: Current length of the weather string (to check total length)
            max_length: Maximum total length allowed (default 130)
            observation_data: Optional dict with observation station data (humidity, dew_point, visibility, wind_gusts, pressure)

        Returns:
            Updated period string with additional details if space allows
        """
        return self._noaa._add_period_details(period_str, detailed_forecast, current_weather_length, max_length, observation_data)

    def get_weather_alerts_noaa(self, lat: float, lon: float, return_full_data: bool = False) -> tuple:
        """Get weather alerts from NOAA with full metadata extraction and prioritization

        Args:
            lat: Latitude
            lon: Longitude
            return_full_data: If True, return list of alert dicts instead of formatted strings

        Returns:
            If return_full_data=False: (full_first_alert_text, abbreviated_first_alert_text, alert_count)
            If return_full_data=True: (list of alert dicts, alert_count)
        """
        return self._nws.get_weather_alerts_noaa(lat, lon, return_full_data)

    async def _get_weather_alerts_noaa_async(
        self, lat: float, lon: float, return_full_data: bool = False
    ) -> tuple:
        return await self._run_sync_provider_async(
            self.get_weather_alerts_noaa,
            lat,
            lon,
            return_full_data,
        )


    def _differentiate_duplicate_statements(self, alerts: list) -> list:
        """Differentiate Special Statements that have the same event type by adding unique details

        Args:
            alerts: List of alert dicts

        Returns:
            List of alerts with differentiated event names for duplicate statements
        """
        return self._nws._differentiate_duplicate_statements(alerts)

    def _prioritize_alerts(self, alerts: list) -> list:
        """Prioritize alerts using hybrid scoring system

        Scoring:
        - Severity: Extreme=100, Severe=75, Moderate=50, Minor=25, Unknown=0
        - Urgency: Immediate=50, Expected=30, Future=10, Past=0
        - Event Type: Warning=40, Watch=30, Advisory=20, Statement=10
        - Time: (hours until expiration) * -5 (sooner = higher score)

        Returns sorted list (highest priority first)
        """
        return self._nws._prioritize_alerts(alerts)

    def _format_alert_compact(self, alert: dict, include_details: bool = True) -> str:
        """Format a single alert compactly

        Shares its formatting with the proactive WeatherService broadcasts via
        ``modules.alert_format``, so both localize from one code path.

        Args:
            alert: Alert dict with event, event_type, severity, expires, office, etc.
            include_details: If True, include expiration time and office

        Returns:
            Formatted alert string, e.g. "🟠High Wind Warn til 6AM by NWS SEA"
        """
        return self._nws._format_alert_compact(alert, include_details)

    def _format_alerts_compact_summary(self, alerts: list, alert_count: int, max_length: int = 130) -> str:
        """Format multiple alerts with prioritized first alert and summary of others

        Args:
            alerts: List of prioritized alert dicts
            alert_count: Total number of alerts
            max_length: Maximum message length (default 130 for backwards compatibility)

        Returns:
            Compact formatted string: "4 alerts: 🟠High Wind Warn til 6AM | +3: 🌊Flood Watch, ❄️Freeze Adv, 🌫️Dense Fog Adv"
        """
        return self._nws._format_alerts_compact_summary(alerts, alert_count, max_length)

    def _get_event_emoji(self, event: str, event_type: str) -> str:
        """Get emoji for event type"""
        return self._nws._get_event_emoji(event, event_type)

    def _format_alert_full(self, alert: dict, index: int = None) -> str:
        """Format a single alert with full details for multi-message display

        Args:
            alert: Alert dict
            index: Optional alert number (1-based)

        Returns:
            Formatted alert string with start/stop times
        """
        return self._nws._format_alert_full(alert, index)

    async def _send_full_alert_list(self, message: MeshMessage, lat: float, lon: float) -> bool:
        """Send full list of alerts with details, splitting across multiple messages if needed.

        Returns whether every message was sent; a refused first message ends the list.
        """
        provider = self.alerts_provider if self.alerts_provider is not None else self._nws
        return await provider.send_full_list(message, lat, lon)

    def abbreviate_city_name(self, city: str) -> str:
        """Abbreviate city names for compact display (e.g., Seattle -> SEA)"""
        return alert_format.abbreviate_city_name(city)

    def compact_time(self, time_str: str) -> str:
        """Compact time format: '6:00AM' -> '6AM', 'December 16 at 3:12PM' -> 'Dec 16 3:12PM'
        Also handles ISO format: '2025-12-17T01:00:00-08:00' -> 'Dec 17 1AM'"""
        return self._noaa.compact_time(time_str)

    def abbreviate_wind_direction(self, direction: str) -> str:
        """Abbreviate wind direction to emoji + 2-3 characters"""
        return self._noaa.abbreviate_wind_direction(direction)

    def _wind_letters(self, point: str) -> str:
        """A 16-point compass abbreviation in the reply's language ("NE" is "NO" in German), as gwx shows it."""
        return self._noaa._wind_letters(point)

    def extract_humidity(self, text: str) -> str:
        """Extract humidity percentage from forecast text"""
        return self._noaa.extract_humidity(text)

    def extract_precip_chance(self, text: str) -> str:
        """Extract precipitation chance from forecast text"""
        return self._noaa.extract_precip_chance(text)

    def extract_high_low(self, text: str, units_str: str = "°F") -> str:
        """Extract high/low temperatures from forecast text; format via [Weather] templates."""
        return self._noaa.extract_high_low(text, units_str)

    def extract_uv_index(self, text: str) -> str:
        """Extract UV index from forecast text"""
        return self._noaa.extract_uv_index(text)

    def extract_dew_point(self, text: str) -> str:
        """Extract dew point temperature from forecast text"""
        return self._noaa.extract_dew_point(text)

    def extract_visibility(self, text: str) -> str:
        """Extract visibility from forecast text"""
        return self._noaa.extract_visibility(text)

    def extract_precip_probability(self, text: str) -> str:
        """Extract precipitation probability from forecast text"""
        return self._noaa.extract_precip_probability(text)

    def extract_wind_gusts(self, text: str) -> str:
        """Extract wind gusts from forecast text"""
        return self._noaa.extract_wind_gusts(text)

    def _forecast_text_gusts(self, text: str) -> str:
        """Gusts from the forecast text ("gusts up to 30 mph" or "48 km/h") in the configured unit."""
        return self._noaa._forecast_text_gusts(text)

    def extract_pressure(self, text: str) -> str:
        """Extract barometric pressure from forecast text"""
        return self._noaa.extract_pressure(text)

    def get_observation_data(self, points_data: dict) -> dict:
        """Get observation station data from NOAA and return as a dict

        Returns:
            Dict with keys: humidity, dew_point, visibility, wind_gusts, pressure
            Values are strings ready for display, or None if not available
        """
        return self._noaa.get_observation_data(points_data)

    def get_weather_emoji(self, condition: str) -> str:
        """Get emoji for weather condition"""
        return self._noaa.get_weather_emoji(condition)

    # NOAA sometimes names forecast periods after federal holidays (e.g. "Washington's Birthday")
    # instead of the weekday. Match these so we can resolve to weekday via startTime.

    def _noaa_period_display_name(self, period: dict) -> str:
        """Return display label for a NOAA forecast period. Resolves holiday names to weekday."""
        return self._noaa._noaa_period_display_name(period)

    def abbreviate_noaa(self, text: str) -> str:
        """Replace long strings with shorter ones for display"""
        return self._noaa.abbreviate_noaa(text)

    async def get_weather_for_zipcode(self, zipcode: str) -> str:
        """Get weather data for a specific zipcode (legacy method)"""
        return await self.get_weather_for_location(zipcode, "zipcode")

    def abbreviate_alert_title(self, title: str) -> str:
        """Abbreviate alert title for brevity"""
        return self._nws.abbreviate_alert_title(title)

    def get_current_conditions(self, points_data: dict) -> str:
        """Get additional current conditions data from NOAA using existing points data (legacy method)"""
        return self._noaa.get_current_conditions(points_data)
