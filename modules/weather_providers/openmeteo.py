#!/usr/bin/env python3
"""Open-Meteo forecasts: the worldwide provider behind gwx and wx's fallback.

The fetch, the formatting and the message catalog live here. The command keeps
the request (option parsing, geocoding, location labels) and the sending, and
hands this provider the units to ask for. Formatting helpers shared with the
command (byte width, high/low templates, hour labels) come from the host, so
one implementation serves both.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Optional

from ..http_retry import make_retry_session
from ..weather_common import _ARROWS_8, _COMPASS_16, load_open_meteo_model
from .base import Capability, ForecastReply, ProviderHost, WeatherProvider, WeatherUnits

# Multiday: plain digits, 7day/7-day, or suffix form 7d/10d (min 2, max below). Open-Meteo allows up to 16 forecast days.
GWX_MULTIDAY_MAX_DAYS = 16

MI_TO_KM = 1.609344
HPA_TO_MMHG = 0.750062
# Past ~20 mi / 32 km, visibility is reported as unlimited anyway.
VISIBILITY_CAP_MI = 20
VISIBILITY_CAP_KM = 32

# Open-Meteo normally answers in about a second but now and then stalls without
# answering. A short read timeout plus make_retry_session's two retries gets past
# a stall in about 20 s at worst, where one 10 s attempt failed the command.
OPEN_METEO_TIMEOUT = (5, 6)  # (connect, read) seconds, per attempt

# Placeholder a failed fetch is compared against; the reply the user sees is the
# translated commands.gwx.error_fetching_api.
ERROR_FETCHING_DATA = "ERROR_FETCHING_DATA"


def _int_or_none(value: Any) -> Optional[int]:
    """int(value), or None for a missing or null field."""
    return None if value is None else int(value)


def weather_emoji(code: int, is_day: Optional[bool] = True) -> str:
    """Convert WMO weather code to emoji.

    Args:
        code: WMO weather code.
        is_day: False at night, when clear skies show a moon instead of a sun.

    Returns:
        str: Weather emoji.
    """
    emoji_map = {
        0: "☀️",      # Clear
        1: "🌤️",     # Mostly Clear
        2: "⛅",     # Partly Cloudy
        3: "☁️",      # Overcast
        45: "🌫️",    # Fog
        48: "🌫️",    # Fog
        51: "🌦️",    # Drizzle
        53: "🌦️",    # Drizzle
        55: "🌧️",    # Heavy Drizzle
        56: "🌧️",    # Freezing Drizzle
        57: "🌧️",    # Freezing Drizzle
        61: "🌧️",    # Rain
        63: "🌧️",    # Rain
        65: "🌧️",    # Heavy Rain
        66: "🌧️",    # Freezing Rain
        67: "🌧️",    # Freezing Rain
        71: "❄️",     # Snow
        73: "❄️",     # Snow
        75: "❄️",     # Heavy Snow
        77: "❄️",     # Snow Grains
        80: "🌦️",    # Showers
        81: "🌦️",    # Showers
        82: "🌧️",    # Heavy Showers
        85: "🌨️",    # Snow Showers
        86: "🌨️",    # Snow Showers
        95: "⛈️",     # Thunderstorm
        96: "⛈️",     # Thunderstorm with Hail
        97: "⛈️",     # Heavy Thunderstorm
        99: "⛈️"      # Severe Thunderstorm
    }

    if is_day is False and code in (0, 1):
        return "🌙"
    return emoji_map.get(code, "🌤️")


# Sentinel: load the model from config, as a provider built by the registry does.
_LOAD_FROM_CONFIG = object()


class OpenMeteoProvider(WeatherProvider):
    """Open-Meteo (api.open-meteo.com) forecasts, worldwide, in the reply's units."""

    name = "openmeteo"
    translation_ns = "commands.gwx"
    location_style = "global"
    capabilities = frozenset({
        Capability.CURRENT,
        Capability.TOMORROW,
        Capability.MULTIDAY,
        Capability.HOURLY,
    })
    max_days = GWX_MULTIDAY_MAX_DAYS

    def __init__(self, host: ProviderHost, units: WeatherUnits, weather_model: Any = _LOAD_FROM_CONFIG):
        """Bind the provider to its host and units.

        *weather_model* is the already-loaded model selection when the caller
        loaded it itself (the command does, so its warning comes first); the
        default loads it from config here.
        """
        super().__init__(host, units)
        self.weather_model = self.load_weather_model() if weather_model is _LOAD_FROM_CONFIG else weather_model

    @property
    def metric_distance(self) -> bool:
        """Whether distances should be shown in kilometers.

        Derived from the temperature unit rather than the response language so
        every unit in one reply agrees: a bot configured for Fahrenheit should
        not print kilometers just because it answers in Russian.
        """
        return self.units.temperature == 'celsius'

    def load_weather_model(self) -> Optional[str]:
        """Load and normalize Open-Meteo model selection from config.

        Returns:
            Optional[str]: Model string, or None to omit the models parameter.
        """
        return load_open_meteo_model(self.host.bot.config, self.host.logger)

    def forecast(
        self,
        lat: float,
        lon: float,
        forecast_type: str = "default",
        num_days: int = 7,
        location_prefix_len: int = 0,
        message: Any = None,
    ) -> ForecastReply:
        """The Open-Meteo reply for a point, without the command's location label.

        *location_prefix_len* is the display width of the location prefix the
        command prepends; the body is formatted to what is left of the frame
        budget.
        """
        current = {}
        if forecast_type == "tomorrow":
            text = self.get_open_meteo_weather(
                lat, lon, forecast_type="tomorrow", message=message, location_prefix_len=location_prefix_len
            )
        elif forecast_type == "multiday":
            text = self.get_open_meteo_weather(
                lat, lon, forecast_type="multiday", num_days=num_days, message=message,
                location_prefix_len=location_prefix_len,
            )
        elif forecast_type == "hourly":
            text = self.get_open_meteo_weather(
                lat, lon, forecast_type="hourly", message=message, location_prefix_len=location_prefix_len
            )
        else:
            text, current = self._get_open_meteo_weather_with_conditions(
                lat, lon, message=message, location_prefix_len=location_prefix_len
            )

        # Check if it's an error (translated error message)
        if text == self.host.translate('commands.gwx.error_fetching') or text == ERROR_FETCHING_DATA:
            return ForecastReply(text=self.host.translate('commands.gwx.error_fetching_api'), ok=False)

        # Severe weather warnings only accompany the current conditions.
        if forecast_type == "default":
            alert_text = self._check_extreme_conditions(current)
            if alert_text:
                return ForecastReply(text=text, extra=alert_text)

        return ForecastReply(text=text)

    def get_open_meteo_weather(self, lat: float, lon: float, forecast_type: str = "default", num_days: int = 7, message: Any = None, location_prefix_len: int = 0) -> str:
        """Get weather forecast from Open-Meteo API.

        Args:
            lat: Latitude.
            lon: Longitude.
            forecast_type: "default", "tomorrow", "multiday", or "hourly".
            num_days: Number of days for multiday forecast (2–16).
            message: The MeshMessage for dynamic length calculation.
            location_prefix_len: Length of location prefix (e.g., "City, CC: ") that will be added later.

        Returns:
            str: Formatted weather string or error message.
        """
        weather, _ = self._get_open_meteo_weather_with_conditions(
            lat, lon, forecast_type, num_days, message, location_prefix_len
        )
        return weather

    def _get_open_meteo_weather_with_conditions(self, lat: float, lon: float, forecast_type: str = "default", num_days: int = 7, message: Any = None, location_prefix_len: int = 0) -> tuple[str, dict]:
        """Return the reply and current conditions from the same Open-Meteo request."""
        # Get max message length dynamically, then subtract location prefix length
        max_length = self.host.get_max_message_length(message) if message else 130
        max_length = max_length - location_prefix_len  # Account for location prefix
        try:
            # Open-Meteo API endpoint with current weather and forecast
            api_url = "https://api.open-meteo.com/v1/forecast"

            # Determine forecast_days based on type
            if forecast_type == "multiday":
                # Index 0 is today and the forecast starts tomorrow, so N days need N+1
                # (Open-Meteo returns at most 16).
                forecast_days = min(num_days + 1, 16)
            elif forecast_type == "tomorrow":
                forecast_days = 2  # Need today and tomorrow
            else:
                forecast_days = 2  # Default

            params = {
                'latitude': lat,
                'longitude': lon,
                'current': 'temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m,dewpoint_2m,visibility,surface_pressure,is_day',
                'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max',
                'hourly': 'temperature_2m,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m',
                'temperature_unit': self.units.temperature,
                'wind_speed_unit': self.units.wind_speed,
                'precipitation_unit': self.units.precipitation,
                'timezone': 'auto',
                'forecast_days': forecast_days
            }
            if self.weather_model:
                params['models'] = self.weather_model
            if forecast_type == "hourly":
                params['hourly'] += ',precipitation_probability,is_day'

            # A session per request: gwx runs in worker threads and a session is not thread-safe.
            with make_retry_session() as session:
                response = session.get(api_url, params=params, timeout=OPEN_METEO_TIMEOUT)

            if not response.ok:
                self.host.logger.warning(f"Error fetching weather from Open-Meteo: {response.status_code}")
                return self.host.translate('commands.gwx.error_fetching'), {}

            data = response.json()

            if forecast_type == "tomorrow":
                return self.format_tomorrow_forecast(data, max_length), {}
            if forecast_type == "multiday":
                return self.format_multiday_forecast(data, num_days), {}
            if forecast_type == "hourly":
                return self._open_meteo_hourly(data, max_length), {}

            temp_symbol = "°F" if self.units.temperature == 'fahrenheit' else "°C"
            weather = self._open_meteo_current(data, max_length, temp_symbol)
            if weather is None:
                return self.host.translate('commands.gwx.error_fetching'), {}
            # Forecast high/low for today (the current conditions already name the period).
            daily = data.get('daily', {})
            if daily:
                weather = self._open_meteo_daily_tail(weather, daily, max_length, temp_symbol)
            return weather, data.get('current', {})

        except Exception as e:
            self.host.logger.error(f"Error fetching Open-Meteo weather: {e}")
            return self.host.translate('commands.gwx.error_fetching'), {}

    def _open_meteo_hourly(self, data: dict, max_length: int) -> str:
        """Hours after Open-Meteo's current local hour, packed into one reply."""
        try:
            current_time = datetime.fromisoformat(data.get('current', {}).get('time', ''))
        except (ValueError, TypeError):
            return self.host.translate('commands.gwx.hourly_not_available')
        next_hour = current_time.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        hourly = data.get('hourly', {})
        lines = []
        for i, time_str in enumerate(hourly.get('time', [])):
            try:
                start_time = datetime.fromisoformat(time_str)
            except (ValueError, TypeError):
                continue
            if start_time < next_hour:
                continue

            def value(field: str) -> Any:
                values = hourly.get(field, [])
                return values[i] if i < len(values) else None

            temperature = value('temperature_2m')
            code = value('weather_code')
            if temperature is None or code is None:
                break
            is_day = value('is_day')
            daytime = bool(is_day) if is_day is not None else 6 <= start_time.hour < 18
            parts = [f"{self.host._hour_label(time_str)}:", self._get_weather_emoji(code, is_day=daytime)]
            probability = value('precipitation_probability')
            if probability is not None and probability > 0:
                parts.append(f"{int(probability)}%")
            parts.append(self.host._short_hourly_description(self._get_weather_description(code)))
            parts.append(f"{int(temperature)}°")
            speed = value('wind_speed_10m')
            direction = value('wind_direction_10m')
            if speed is not None and direction is not None:
                parts.append(f"{self.host._without_arrow(self._degrees_to_direction(direction))}{int(speed)}")
            lines.append(" ".join(parts))
        return self.host._pack_hourly_lines(lines, max_length)

    def _open_meteo_current(self, data: dict, max_length: int, temp_symbol: str) -> Optional[str]:
        """Current conditions for the default gwx reply, with extra conditions when they fit.

        None when the response has no current temperature: a reply built from
        defaults would invent "0°, 0%RH" weather. Other missing or null
        current fields are left out of the reply one by one.
        """
        # Check units in response to verify API is respecting our unit requests
        current_units = data.get('current_units', {})
        visibility_unit = current_units.get('visibility', 'm')

        # Extract current conditions
        current = data.get('current', {})

        # Current conditions - API should return in Fahrenheit when requested
        if current.get('temperature_2m') is None:
            self.host.logger.warning("Open-Meteo response has no current temperature")
            return None
        temp = int(current['temperature_2m'])
        feels_like = _int_or_none(current.get('apparent_temperature'))
        if feels_like is None:
            feels_like = temp
        dewpoint = current.get('dewpoint_2m')
        humidity = _int_or_none(current.get('relative_humidity_2m'))
        wind_speed = _int_or_none(current.get('wind_speed_10m'))
        wind_direction = self._degrees_to_direction(current.get('wind_direction_10m'))
        wind_gusts = _int_or_none(current.get('wind_gusts_10m'))
        visibility = current.get('visibility')
        pressure = current.get('surface_pressure')
        weather_code = current.get('weather_code')

        visibility_mi = self._visibility_miles(visibility, visibility_unit)

        # Pressure validation - account for high elevation locations
        # Normal sea level pressure is 1013 hPa, range is typically 950-1050 hPa
        # At high elevations (e.g., 2500m), pressure can be 750-800 hPa, which is normal
        # Only filter out extremely low pressures (< 600 hPa) which would be invalid
        if pressure is not None and pressure < 600:
            self.host.logger.warning(f"Extremely low pressure value: {pressure} hPa - might be invalid")
            pressure = None

        # Get weather description and emoji (none for a missing code, rather than "clear")
        if weather_code is None:
            condition = ""
        else:
            emoji = self._get_weather_emoji(weather_code, is_day=self._open_meteo_daytime(current))
            condition = f"{emoji}{self._get_weather_description(weather_code)} "

        period_name = self.host.translate(f'commands.gwx.periods.{self._open_meteo_period_key(current)}')

        # Build current weather string
        weather = f"{period_name}: {condition}{temp}{temp_symbol}"

        # Add feels like if significantly different
        if abs(feels_like - temp) >= 5:
            feels_str = self.host.translate('commands.gwx.feels_like', value=feels_like, unit=temp_symbol)
            weather += f" {feels_str}"

        # Add wind info (always show if >= 3 mph, show gusts if significant)
        if wind_speed is not None and wind_speed >= 3:
            weather += f" {wind_direction}{wind_speed}"
            if wind_gusts is not None and wind_gusts > wind_speed + 3:
                gust_str = self.host.translate('commands.gwx.gust', value=wind_gusts)
                weather += gust_str

        # Add humidity
        if humidity is not None:
            humidity_str = self.host.translate('commands.gwx.humidity', value=humidity)
            weather += f" {humidity_str}"

        # Add additional conditions if space allows
        conditions = self._open_meteo_extra_conditions(dewpoint, visibility_mi, pressure, temp_symbol)

        # Add conditions to weather string if space allows
        # Reserve space for forecast data (high/low and tomorrow)
        conditions_max_length = max_length - 80  # Reserve ~80 chars for forecast data
        if conditions and self.host._count_display_width(weather) < conditions_max_length:
            weather += " " + " ".join(conditions)
        return weather

    def _visibility_miles(self, visibility, visibility_unit) -> Optional[float]:
        """Open-Meteo's visibility in miles, from feet or meters as current_units says."""
        # Convert visibility to miles based on actual unit from API
        # API returns visibility in feet when using imperial units
        if visibility is not None:
            if visibility_unit == 'ft' or 'ft' in str(visibility_unit).lower():
                # Convert from feet to miles (1 mile = 5280 feet)
                visibility_mi = visibility / 5280.0
            else:
                # Assume meters, convert to miles (1 mile = 1609.34 meters)
                visibility_mi = visibility / 1609.34
        else:
            visibility_mi = None
        return visibility_mi

    def _open_meteo_extra_conditions(self, dewpoint, visibility_mi, pressure, temp_symbol: str) -> list:
        """Dew point, visibility and pressure strings, each only when its value is present."""
        conditions = []

        # Add dew point
        if dewpoint is not None:
            dewpoint_val = int(dewpoint)
            dew_str = self.host.translate('commands.gwx.dew_point', value=dewpoint_val, unit=temp_symbol)
            conditions.append(dew_str)

        # Add visibility (already converted to miles above)
        if visibility_mi is not None and visibility_mi > 0:
            # Beyond ~20 mi visibility is essentially unlimited, so cap the
            # display at that in whichever unit we are showing.
            if self.metric_distance:
                visibility_display = min(int(visibility_mi * MI_TO_KM), VISIBILITY_CAP_KM)
                vis_str = self.host.translate('commands.gwx.visibility_km', value=visibility_display)
            else:
                visibility_display = min(int(visibility_mi), VISIBILITY_CAP_MI)
                vis_str = self.host.translate('commands.gwx.visibility', value=visibility_display)
            conditions.append(vis_str)

        # Add pressure (convert from hPa to display format)
        if pressure is not None:
            pressure_hpa = int(pressure)
            # Which pressure unit reads as normal is a locale convention, not
            # a metric/imperial split: Russia uses mmHg, most of metric
            # Europe uses hPa. The catalog names its own.
            if self.host.translate('commands.gwx.pressure_unit').strip().lower() == 'mmhg':
                press_str = self.host.translate('commands.gwx.pressure_mmhg',
                                                value=round(pressure_hpa * HPA_TO_MMHG))
            else:
                press_str = self.host.translate('commands.gwx.pressure', value=pressure_hpa)
            conditions.append(press_str)
        return conditions

    def _open_meteo_daily_tail(self, weather: str, daily: dict, max_length: int, temp_symbol: str) -> str:
        """Append today's high/low, then tomorrow and its precipitation while they fit."""
        today_high = int(daily['temperature_2m_max'][0])
        today_low = int(daily['temperature_2m_min'][0])

        weather += f" | {self.host._format_high_low(today_high, today_low, temp_symbol)}"

        # Add tomorrow if space allows (check length more carefully)
        if len(daily['temperature_2m_max']) > 1:
            tomorrow_high = int(daily['temperature_2m_max'][1])
            tomorrow_low = int(daily['temperature_2m_min'][1])

            tomorrow_code = daily['weather_code'][1]
            tomorrow_emoji = self._get_weather_emoji(tomorrow_code)

            # Get tomorrow's period name
            tomorrow_period = self.host.translate('commands.gwx.periods.tomorrow')
            tomorrow_hl = self.host._format_high_low(tomorrow_high, tomorrow_low, temp_symbol)
            tomorrow_str = f" | {tomorrow_period}: {tomorrow_emoji} {tomorrow_hl}"
            # Name tomorrow's weather when it fits; the emoji alone is ambiguous.
            if tomorrow_code is not None:
                described = (f" | {tomorrow_period}: {tomorrow_emoji}"
                             f"{self._get_weather_description(tomorrow_code)} {tomorrow_hl}")
                if self.host._count_display_width(weather + described) <= max_length - 10:
                    tomorrow_str = described

            # Only add if we have space (leave room for potential precipitation)
            # Use display width to account for emojis
            if self.host._count_display_width(weather + tomorrow_str) <= max_length - 10:  # Leave 10 chars buffer
                weather += tomorrow_str

                # Add precipitation probability and amount if significant and space allows
                if len(daily.get('precipitation_probability_max', [])) > 1:
                    precip_prob = daily['precipitation_probability_max'][1]
                    if precip_prob is not None and precip_prob >= 30:
                        # Get precipitation amount if available
                        precip_amount = None
                        if len(daily.get('precipitation_sum', [])) > 1:
                            precip_amount = daily['precipitation_sum'][1]

                        # Format precipitation info
                        if precip_amount is not None and precip_amount > 0:
                            # Show both probability and amount
                            precip_unit = "in" if self.units.precipitation == 'inch' else "mm"
                            precip_str = f" 🌦️{precip_prob}% {precip_amount:.2f}{precip_unit}"
                        else:
                            # Only show probability if no amount available
                            precip_str = f" 🌦️{precip_prob}%"

                        # Use display width to check if we have space, with buffer to avoid cutting emojis
                        # Add buffer of 5 chars to ensure we don't truncate in middle of emoji
                        if self.host._count_display_width(weather + precip_str) <= max_length - 5:
                            weather += precip_str
        return weather

    def _open_meteo_daytime(self, current: dict) -> bool:
        """Whether the sun is up at the location: Open-Meteo's is_day, else 06:00-18:00 local."""
        flag = current.get('is_day') if isinstance(current, dict) else None
        if flag is not None:
            return bool(flag)
        return 6 <= self._open_meteo_local_hour(current) < 18

    def _open_meteo_period_key(self, current: dict) -> str:
        """Period label key for the current conditions: today while the sun is up,
        overnight from midnight to sunrise, tonight from sunset to midnight.

        Open-Meteo's current time is local to the location (timezone=auto); the
        bot's own clock is only the fallback.
        """
        if self._open_meteo_daytime(current):
            return 'today'
        return 'overnight' if self._open_meteo_local_hour(current) < 12 else 'tonight'

    @staticmethod
    def _open_meteo_local_hour(current: dict) -> int:
        """Hour of day at the location from Open-Meteo's current time ("2026-10-01T19:45"), else the bot's."""
        stamp = current.get('time') if isinstance(current, dict) else None
        if isinstance(stamp, str):
            try:
                return datetime.fromisoformat(stamp).hour
            except ValueError:
                pass
        return datetime.now().hour

    @staticmethod
    def _open_meteo_date(dates: list, index: int):
        """The date at *index* of Open-Meteo's daily time array, or None."""
        if index < len(dates) and isinstance(dates[index], str):
            try:
                return datetime.strptime(dates[index][:10], '%Y-%m-%d')
            except ValueError:
                return None
        return None

    def format_tomorrow_forecast(self, data: dict, max_length: Optional[int] = None) -> str:
        """Format a detailed forecast for tomorrow.

        Args:
            data: Weather data dictionary from Open-Meteo.
            max_length: UTF-8 byte budget; details are left out, least important
                first, until the reply fits. None keeps every detail.

        Returns:
            str: Formatted tomorrow forecast string.
        """
        try:
            daily = data.get('daily', {})
            if not daily or len(daily.get('temperature_2m_max', [])) < 2:
                return self.host.translate('commands.gwx.tomorrow_not_available')
            if daily['temperature_2m_max'][1] is None or daily['temperature_2m_min'][1] is None:
                return self.host.translate('commands.gwx.tomorrow_not_available')

            temp_symbol = "°F" if self.units.temperature == 'fahrenheit' else "°C"
            tomorrow_high = int(daily['temperature_2m_max'][1])
            tomorrow_low = int(daily['temperature_2m_min'][1])
            tomorrow_code = daily['weather_code'][1]
            tomorrow_emoji = self._get_weather_emoji(tomorrow_code)
            tomorrow_desc = self._get_weather_description(tomorrow_code)

            # Get wind info if available
            wind_info = ""
            gust_info = ""
            if len(daily.get('wind_speed_10m_max', [])) > 1:
                wind_speed = int(daily['wind_speed_10m_max'][1])
                if wind_speed >= 3:
                    wind_info = f" {wind_speed}"
                    if len(daily.get('wind_gusts_10m_max', [])) > 1:
                        wind_gusts = int(daily['wind_gusts_10m_max'][1])
                        if wind_gusts > wind_speed + 3:
                            gust_info = self.host.translate('commands.gwx.gust', value=wind_gusts)

            # Get precipitation probability and amount
            precip_info = ""
            precip_chance = ""
            if len(daily.get('precipitation_probability_max', [])) > 1:
                precip_prob = daily['precipitation_probability_max'][1]
                if precip_prob is not None and precip_prob >= 30:
                    # Get precipitation amount if available
                    precip_amount = None
                    if len(daily.get('precipitation_sum', [])) > 1:
                        precip_amount = daily['precipitation_sum'][1]

                    # Format precipitation info
                    precip_chance = f" 🌦️{precip_prob}%"
                    if precip_amount is not None and precip_amount > 0:
                        # Show both probability and amount
                        precip_unit = "in" if self.units.precipitation == 'inch' else "mm"
                        precip_info = f" 🌦️{precip_prob}% {precip_amount:.2f}{precip_unit}"
                    else:
                        # Only show probability if no amount available
                        precip_info = f" 🌦️{precip_prob}%"

            tomorrow_period = self.host.translate('commands.gwx.periods.tomorrow')
            hl = self.host._format_high_low(tomorrow_high, tomorrow_low, temp_symbol)
            head = f"{tomorrow_period}: {tomorrow_emoji}"
            # Least important detail first: precipitation amount, gusts, wind, chance, description.
            candidates = [
                f"{head}{tomorrow_desc} {hl}{wind_info}{gust_info}{precip_info}",
                f"{head}{tomorrow_desc} {hl}{wind_info}{gust_info}{precip_chance}",
                f"{head}{tomorrow_desc} {hl}{wind_info}{precip_chance}",
                f"{head}{tomorrow_desc} {hl}{precip_chance}",
                f"{head}{tomorrow_desc} {hl}",
                f"{head} {hl}",
            ]
            if max_length is None:
                return candidates[0]
            for candidate in candidates:
                if self.host._count_display_width(candidate) <= max_length:
                    return candidate
            return candidates[-1]

        except Exception as e:
            self.host.logger.error(f"Error formatting tomorrow forecast: {e}")
            return self.host.translate('commands.gwx.tomorrow_error')

    def format_multiday_forecast(self, data: dict, num_days: int = 7) -> str:
        """Format a less detailed multi-day forecast summary.

        Args:
            data: Weather data dictionary from Open-Meteo.
            num_days: Number of days to include in forecast.

        Returns:
            str: Formatted multi-day forecast string (newlines separate days).
        """
        try:
            daily = data.get('daily', {})
            if not daily:
                return self.host.translate('commands.gwx.multiday_not_available', num_days=num_days)

            temp_symbol = "°F" if self.units.temperature == 'fahrenheit' else "°C"
            temps_max = daily.get('temperature_2m_max', [])
            temps_min = daily.get('temperature_2m_min', [])
            weather_codes = daily.get('weather_code', [])

            if len(temps_max) < num_days + 1:  # +1 because index 0 is today
                num_days = len(temps_max) - 1

            # Map day names to 1-2 letter abbreviations
            day_abbrev_map = {
                'Monday': self.host.translate('commands.gwx.day_abbrev.Monday'),
                'Tuesday': self.host.translate('commands.gwx.day_abbrev.Tuesday'),
                'Wednesday': self.host.translate('commands.gwx.day_abbrev.Wednesday'),
                'Thursday': self.host.translate('commands.gwx.day_abbrev.Thursday'),
                'Friday': self.host.translate('commands.gwx.day_abbrev.Friday'),
                'Saturday': self.host.translate('commands.gwx.day_abbrev.Saturday'),
                'Sunday': self.host.translate('commands.gwx.day_abbrev.Sunday')
            }

            parts = []
            today = datetime.now()
            day_dates = daily.get('time') or []

            # Start from tomorrow (index 1)
            for i in range(1, min(num_days + 1, len(temps_max))):
                # Open-Meteo pads the days past a model's horizon with nulls
                # (up to 9 of 16 with icon_seamless), so the forecast ends there.
                if temps_max[i] is None or i >= len(temps_min) or temps_min[i] is None:
                    break
                # Label from Open-Meteo's own (location-local) date when present.
                day_date = self._open_meteo_date(day_dates, i) or (today + timedelta(days=i))
                day_name = day_date.strftime('%A')
                day_abbrev = day_abbrev_map.get(day_name, day_name[:2])

                high = int(temps_max[i])
                low = int(temps_min[i])
                code = weather_codes[i] if i < len(weather_codes) else 0
                emoji = self._get_weather_emoji(code)
                desc = self._get_weather_description(code)

                # Abbreviate description if needed
                desc_short = desc
                if len(desc) > 20:
                    desc_short = desc[:17] + "..."

                parts.append(f"{day_abbrev}: {emoji}{desc_short} {self.host._format_high_low(high, low, temp_symbol)}")

            if not parts:
                return self.host.translate('commands.gwx.multiday_not_available', num_days=num_days)

            return "\n".join(parts)

        except Exception as e:
            self.host.logger.error(f"Error formatting {num_days}-day forecast: {e}")
            return self.host.translate('commands.gwx.multiday_error', num_days=num_days)

    def _degrees_to_direction(self, degrees: float) -> str:
        """Convert wind direction in degrees to compass direction with emoji.

        Uses the nearest of the 16 compass points (as NOAA's own labels do) and
        the nearest 8-point arrow: 22.5° is NNE, 350° is N. Letters come from the
        translation catalog.

        Args:
            degrees: Wind direction in degrees.

        Returns:
            str: Compass direction string with emoji (e.g., "↗️NNE").
        """
        if degrees is None:
            return ""
        degrees = float(degrees)
        if not math.isfinite(degrees):
            return ""
        index = int((degrees % 360) / 22.5 + 0.5) % 16
        key = _COMPASS_16[index]
        arrow = _ARROWS_8[int(index / 2 + 0.5) % 8]
        translated = self.host.translate(f"common.wind_directions.{key}")
        return f"{arrow}{translated}"

    def _get_weather_description(self, code: int) -> str:
        """Convert WMO weather code to description.

        Args:
            code: WMO weather code.

        Returns:
            str: Weather description.
        """
        # Try to get from translations first
        key = f"commands.gwx.weather_descriptions.{code}"
        description = self.host.translate(key)

        # If translation returned the key (not found), try fallback
        if description == key:
            # Fallback to hardcoded descriptions
            weather_codes = {
                0: "Clear",
                1: "Mostly Clear",
                2: "Partly Cloudy",
                3: "Overcast",
                45: "Foggy",
                48: "Foggy",
                51: "Light Drizzle",
                53: "Drizzle",
                55: "Heavy Drizzle",
                56: "Light Freezing Drizzle",
                57: "Freezing Drizzle",
                61: "Light Rain",
                63: "Rain",
                65: "Heavy Rain",
                66: "Light Freezing Rain",
                67: "Freezing Rain",
                71: "Light Snow",
                73: "Snow",
                75: "Heavy Snow",
                77: "Snow Grains",
                80: "Light Showers",
                81: "Showers",
                82: "Heavy Showers",
                85: "Light Snow Showers",
                86: "Snow Showers",
                95: "Thunderstorm",
                96: "T-Storm w/Hail",
                97: "Heavy T-Storm",
                99: "Severe T-Storm"
            }
            return weather_codes.get(code, self.host.translate('commands.gwx.weather_descriptions.unknown'))

        return description

    def _get_weather_emoji(self, code: int, is_day: Optional[bool] = True) -> str:
        """Convert WMO weather code to emoji (see weather_emoji)."""
        return weather_emoji(code, is_day)

    def _check_extreme_conditions(self, current: dict) -> Optional[str]:
        """Check for extreme weather conditions that warrant warnings.

        Args:
            current: Open-Meteo current conditions in the configured units.

        Returns:
            Optional[str]: Warning text if conditions found, None otherwise.
        """
        warnings = []

        # Keep the thresholds in Fahrenheit and mph, regardless of reply units.
        temp = current.get('temperature_2m')
        if temp is not None:
            if self.units.temperature == 'celsius':
                temp = temp * 9 / 5 + 32
            if temp >= 95:
                warnings.append(self.host.translate('commands.gwx.warnings.extreme_heat'))
            elif temp <= 20:
                warnings.append(self.host.translate('commands.gwx.warnings.extreme_cold'))

        code = current.get('weather_code')
        if code in (65, 82):
            warnings.append(self.host.translate('commands.gwx.warnings.heavy_rain'))

        if code in (95, 96, 97, 99):
            warnings.append(self.host.translate('commands.gwx.warnings.thunderstorms'))

        # The old "Snow Showers" match also covered light snow showers (85).
        if code in (75, 85, 86):
            warnings.append(self.host.translate('commands.gwx.warnings.heavy_snow'))

        # Sustained wind only, as before. The threshold is 30 mph; the warning shows the
        # speed in the configured unit, like the wind in the reply it follows.
        wind_factor = {'mph': 1, 'kmh': MI_TO_KM, 'ms': 0.44704, 'kn': 0.868976}[self.units.wind_speed]
        wind = current.get('wind_speed_10m') or 0
        if wind / wind_factor >= 30:
            unit = self.host.translate(f'services.weather_service.wind_speed_units.{self.units.wind_speed}')
            warnings.append(self.host.translate('commands.gwx.warnings.high_winds', wind_speed=int(wind), unit=unit))

        return " | ".join(warnings) if warnings else None
