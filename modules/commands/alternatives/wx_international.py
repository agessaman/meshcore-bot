#!/usr/bin/env python3
"""
Global Weather command for the MeshCore Bot
Provides worldwide weather information using Open-Meteo API
"""

import asyncio
import math  # noqa: F401  re-exported
import re  # noqa: F401  re-exported
from dataclasses import replace
from datetime import datetime, timedelta  # noqa: F401  re-exported
from typing import Any, Optional, Union

import requests  # noqa: F401  module attribute other code patches

from ...clients.mqtt_weather import (  # noqa: F401  re-exported
    get_mqtt_weather_topic,
    load_mqtt_weather_format_config,
    mqtt_weather_display_for_topic,
)
from ...clients.wxsim_parser import WXSIMParser  # noqa: F401  re-exported
from ...location import get_bot_lat_lon, get_companion_lat_lon  # noqa: F401  re-exported
from ...models import MeshMessage
from ...utils import (  # noqa: F401  re-exported
    config_value,
    format_temperature_high_low,
    geocode_city_sync,
    geocode_zipcode_sync,
    get_nominatim_geocoder,
    normalize_us_state,
    rate_limited_nominatim_reverse_sync,
)
from ...weather_common import (  # noqa: F401  re-exported
    WeatherCommandMixin,
    load_open_meteo_model,
)
from ...weather_providers.openmeteo import (  # noqa: F401  re-exported
    GWX_MULTIDAY_MAX_DAYS,
    HPA_TO_MMHG,
    MI_TO_KM,
    OPEN_METEO_TIMEOUT,
    VISIBILITY_CAP_KM,
    VISIBILITY_CAP_MI,
    OpenMeteoProvider,
    weather_emoji,
)
from ..base_command import BaseCommand  # noqa: F401  re-exported
from ..wx_command import WxCommand

# Kept for code that checked them; these imports used to be optional.
WXSIM_PARSER_AVAILABLE = True


class GlobalWxCommand(WxCommand):
    """gwx: the global weather command, pinned to Open-Meteo.

    Everything else is wx's: one execute, one request, one set of providers. This
    class keeps gwx's triggers, its message catalog and its global location style,
    and the Open-Meteo surface tests and plugins reach on the command.
    """

    # Plugin metadata
    # Every reply goes through send_response, so a scheduled {cmd:gwx ...} renders
    # without transmitting, as wx already does.
    render_safe = True
    name = "gwx"
    translation_ns = "commands.gwx"
    keywords = ['gwx', 'globalweather', 'gwxa']
    description = "Get weather information for any global location (usage: gwx Tokyo)"
    category = "weather"
    cooldown_seconds = 5  # 5 second cooldown per user to prevent API abuse
    # Open-Meteo/geocoding need the network; custom MQTT/WXSIM may be LAN-only.
    requires_internet = False

    # Documentation
    short_description = "Get weather for any global location using Open-Meteo API"
    usage = "gwx <location> [tomorrow|<N>d|hourly]"
    examples = ["gwx Tokyo", "gwx Paris, France"]
    parameters = [
        {"name": "location", "description": "City name, country, or coordinates"},
        {"name": "option", "description": "tomorrow, Nd (e.g. 7d, 10d), or hourly (optional)"}
    ]

    # gwx answers from Open-Meteo whatever [Weather] weather_provider says.
    pinned_provider = "openmeteo"
    # gwx has never had an enable switch of its own; [Gwx_Command] enabled only
    # gates a scheduled {cmd:gwx ...} placeholder.
    enabled_section = None

    def __init__(self, bot: Any):
        """Initialize the global weather command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.url_timeout = 10  # seconds

    # Units and the model selection belong to the provider; these forward so
    # code that assigns them on the command still reaches the request the
    # provider builds.

    @property
    def temperature_unit(self) -> str:
        """Temperature unit the provider asks Open-Meteo for."""
        return self.provider.units.temperature

    @temperature_unit.setter
    def temperature_unit(self, value: str) -> None:
        self.provider.units = replace(self.provider.units, temperature=value)

    @property
    def wind_speed_unit(self) -> str:
        """Wind speed unit the provider asks Open-Meteo for."""
        return self.provider.units.wind_speed

    @wind_speed_unit.setter
    def wind_speed_unit(self, value: str) -> None:
        self.provider.units = replace(self.provider.units, wind_speed=value)

    @property
    def precipitation_unit(self) -> str:
        """Precipitation unit the provider asks Open-Meteo for."""
        return self.provider.units.precipitation

    @precipitation_unit.setter
    def precipitation_unit(self, value: str) -> None:
        self.provider.units = replace(self.provider.units, precipitation=value)

    @property
    def weather_model(self) -> Optional[str]:
        """Open-Meteo model selection, or None to omit the models parameter."""
        return self.provider.weather_model

    @weather_model.setter
    def weather_model(self, value: Optional[str]) -> None:
        self.provider.weather_model = value

    @property
    def metric_distance(self) -> bool:
        """Whether distances should be shown in kilometers.

        Derived from [Weather] temperature_unit rather than the response
        language so every unit in one reply agrees: a bot configured for
        Fahrenheit should not print kilometers just because it answers in
        Russian.
        """
        return self.provider.metric_distance

    def _load_weather_model(self) -> Optional[str]:
        """Load and normalize Open-Meteo model selection from config.

        Returns:
            Optional[str]: Model string, or None to omit the models parameter.
        """
        return self.provider.load_weather_model()

    async def _fallback_place_name(self, lat: float, lon: float) -> Optional[str]:
        """gwx logs the coordinates it forecasts, not a reverse-geocoded name."""
        return None

    async def get_weather_for_location(self, location: str, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None) -> Union[str, tuple[str, str, str]]:
        """Get weather data for any global location.

        Args:
            location: The location (city name, etc.).
            forecast_type: "default", "tomorrow", "multiday", or "hourly".
            num_days: Number of days for multiday forecast (2–16).
            message: The MeshMessage for dynamic length calculation.

        Geocoding and the Open-Meteo fetch are both blocking HTTP, so the whole
        body runs on a worker thread; inline it stalled every other coroutine for
        up to several chained 10s timeouts.

        Returns:
            Union[str, Tuple[str, str, str]]: Format string or tuple for multi-message response.
        """
        return await asyncio.to_thread(
            self._get_weather_for_location_sync, location, forecast_type, num_days, message
        )

    async def _global_weather_for_location(self, location: str, location_type: str, forecast_type: str,
                                           num_days: int, message: MeshMessage,
                                           using_companion_location: bool) -> Union[str, tuple[str, str, str]]:
        """gwx's own four-argument forecast call, kept for its callers and tests."""
        return await self.get_weather_for_location(location, forecast_type, num_days, message)

    def _get_weather_for_location_sync(
        self,
        location: str,
        forecast_type: str = "default",
        num_days: int = 7,
        message: MeshMessage = None,
    ) -> Union[str, tuple[str, str, str]]:
        """Blocking body of :meth:`get_weather_for_location` (runs off the event loop)."""
        return self._forecast_for_location(location, "city", forecast_type, num_days, message, False)

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
            return None

        if location:
            # Strip whitespace and normalize
            location = location.strip()
            location_lower = location.lower()

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
                return config_value(self.bot.config, section, name, self.logger)
        else:
            # Check for default source: custom.wxsim.default
            default_key = 'custom.wxsim.default'
            if self.bot.config.has_option(section, default_key):
                return config_value(self.bot.config, section, default_key, self.logger)

        return None

    async def _fetch_wxsim(
        self, source: str, forecast_type: str, num_days: int, message: MeshMessage, location: Optional[str]
    ) -> str:
        # Blocking HTTP fetch of the WXSIM plaintext file.
        if location is None:
            return await asyncio.to_thread(self._get_wxsim_weather, source, forecast_type, num_days, message)
        return await asyncio.to_thread(
            self._get_wxsim_weather, source, forecast_type, num_days, message, location
        )

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
            return self.translate('commands.gwx.source_option_not_available')

        # Fetch WXSIM data
        text = self.wxsim_parser.fetch_from_url(source_url, timeout=self.url_timeout)
        if not text:
            return self.translate('commands.gwx.error_fetching')

        # Parse the data
        forecast = self.wxsim_parser.parse(text)

        # Validate forecast is not stale (as wx does)
        is_stale, stale_reason = self.wxsim_parser.is_forecast_stale(forecast, max_age_hours=48)
        if is_stale:
            self.logger.warning(f"WXSIM forecast appears stale: {stale_reason}")

        # Get unit preferences from config
        temp_unit = self.bot.config.get('Weather', 'temperature_unit', fallback='fahrenheit').lower()
        wind_unit = self.bot.config.get('Weather', 'wind_speed_unit', fallback='mph').lower()

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
                return self.translate('commands.gwx.tomorrow_not_available')

        elif forecast_type == "multiday":
            # Format multiday forecast
            summary = self.wxsim_parser.format_forecast_summary(forecast, num_days, temp_unit, wind_unit)
            if location_name:
                return f"{location_name}:\n{summary}"
            return summary

        else:
            # Default: current conditions + today's forecast
            current = self.wxsim_parser.format_current_conditions(forecast, temp_unit, wind_unit)

            # Add today's high/low if available
            if forecast.periods:
                today = forecast.periods[0]
                high = self.wxsim_parser._convert_temp(today.high_temp, temp_unit) if today.high_temp is not None else None
                low = self.wxsim_parser._convert_temp(today.low_temp, temp_unit) if today.low_temp is not None else None
                temp_symbol = "°F" if temp_unit == 'fahrenheit' else "°C"

                hl_today = self._format_high_low(high, low, temp_symbol)
                if hl_today:
                    current += f" | {hl_today}"

                # Add tomorrow if available
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

    def _coordinates_to_location_string(self, lat: float, lon: float) -> Optional[str]:
        """Convert coordinates to a location string (city name) using reverse geocoding.

        Args:
            lat: Latitude.
            lon: Longitude.

        Returns:
            Optional[str]: Location string (city name) or None if geocoding fails.
        """
        try:
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
                country = address.get('country', '')

                if city:
                    if state and country:
                        return f"{city}, {state}, {country}"
                    elif state:
                        return f"{city}, {state}"
                    elif country:
                        return f"{city}, {country}"
                    return city
            return None
        except Exception as e:
            self.logger.debug(f"Error reverse geocoding coordinates {lat}, {lon}: {e}")
            return None


    def get_open_meteo_weather(self, lat: float, lon: float, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None, location_prefix_len: int = 0) -> str:
        """Get weather forecast from Open-Meteo API.

        Args:
            lat: Latitude.
            lon: Longitude.
            forecast_type: "default", "tomorrow", "multiday", or "hourly".
            num_days: Number of days for multiday forecast (2-16).
            message: The MeshMessage for dynamic length calculation.
            location_prefix_len: Length of location prefix (e.g., "City, CC: ") that will be added later.

        Returns:
            str: Formatted weather string or error message.
        """
        return self.provider.get_open_meteo_weather(
            lat, lon, forecast_type, num_days, message, location_prefix_len
        )

    def _get_open_meteo_weather_with_conditions(self, lat: float, lon: float, forecast_type: str = "default", num_days: int = 7, message: MeshMessage = None, location_prefix_len: int = 0) -> tuple[str, dict]:
        """Return the reply and current conditions from the same Open-Meteo request."""
        return self.provider._get_open_meteo_weather_with_conditions(
            lat, lon, forecast_type, num_days, message, location_prefix_len
        )

    def _open_meteo_hourly(self, data: dict, max_length: int) -> str:
        """Hours after Open-Meteo's current local hour, packed into one reply."""
        return self.provider._open_meteo_hourly(data, max_length)

    def _open_meteo_current(self, data: dict, max_length: int, temp_symbol: str) -> Optional[str]:
        """Current conditions for the default gwx reply, or None without a temperature."""
        return self.provider._open_meteo_current(data, max_length, temp_symbol)

    def _visibility_miles(self, visibility, visibility_unit) -> Optional[float]:
        """Open-Meteo's visibility in miles, from feet or meters as current_units says."""
        return self.provider._visibility_miles(visibility, visibility_unit)

    def _open_meteo_extra_conditions(self, dewpoint, visibility_mi, pressure, temp_symbol: str) -> list:
        """Dew point, visibility and pressure strings, each only when its value is present."""
        return self.provider._open_meteo_extra_conditions(dewpoint, visibility_mi, pressure, temp_symbol)

    def _open_meteo_daily_tail(self, weather: str, daily: dict, max_length: int, temp_symbol: str) -> str:
        """Append today's high/low, then tomorrow and its precipitation while they fit."""
        return self.provider._open_meteo_daily_tail(weather, daily, max_length, temp_symbol)

    def _open_meteo_daytime(self, current: dict) -> bool:
        """Whether the sun is up at the location: Open-Meteo's is_day, else 06:00-18:00 local."""
        return self.provider._open_meteo_daytime(current)

    def _open_meteo_period_key(self, current: dict) -> str:
        """Period label key for the current conditions (today, overnight or tonight)."""
        return self.provider._open_meteo_period_key(current)

    @staticmethod
    def _open_meteo_local_hour(current: dict) -> int:
        """Hour of day at the location from Open-Meteo's current time, else the bot's."""
        return OpenMeteoProvider._open_meteo_local_hour(current)

    @staticmethod
    def _open_meteo_date(dates: list, index: int):
        """The date at *index* of Open-Meteo's daily time array, or None."""
        return OpenMeteoProvider._open_meteo_date(dates, index)

    def format_tomorrow_forecast(self, data: dict, max_length: Optional[int] = None) -> str:
        """Format a detailed forecast for tomorrow.

        Args:
            data: Weather data dictionary from Open-Meteo.
            max_length: UTF-8 byte budget; details are left out, least important
                first, until the reply fits. None keeps every detail.

        Returns:
            str: Formatted tomorrow forecast string.
        """
        return self.provider.format_tomorrow_forecast(data, max_length)

    def format_multiday_forecast(self, data: dict, num_days: int = 7) -> str:
        """Format a less detailed multi-day forecast summary.

        Args:
            data: Weather data dictionary from Open-Meteo.
            num_days: Number of days to include in forecast.

        Returns:
            str: Formatted multi-day forecast string (newlines separate days).
        """
        return self.provider.format_multiday_forecast(data, num_days)

    def _degrees_to_direction(self, degrees: float) -> str:
        """Convert wind direction in degrees to compass direction with emoji.

        Args:
            degrees: Wind direction in degrees.

        Returns:
            str: Compass direction string with emoji (e.g., "↗️NNE").
        """
        return self.provider._degrees_to_direction(degrees)

    def _get_weather_description(self, code: int) -> str:
        """Convert WMO weather code to description.

        Args:
            code: WMO weather code.

        Returns:
            str: Weather description.
        """
        return self.provider._get_weather_description(code)

    def _get_weather_emoji(self, code: int, is_day: Optional[bool] = True) -> str:
        """Convert WMO weather code to emoji.

        Args:
            code: WMO weather code.
            is_day: False at night, when clear skies show a moon instead of a sun.

        Returns:
            str: Weather emoji.
        """
        return weather_emoji(code, is_day)

    def _check_extreme_conditions(self, current: dict) -> Optional[str]:
        """Check for extreme weather conditions that warrant warnings.

        Args:
            current: Open-Meteo current conditions in the configured units.

        Returns:
            Optional[str]: Warning text if conditions found, None otherwise.
        """
        return self.provider._check_extreme_conditions(current)
