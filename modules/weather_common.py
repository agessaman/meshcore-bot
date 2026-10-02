"""Behavior the wx (NOAA) and gwx (Open-Meteo) commands share.

``WeatherCommandMixin`` goes before ``BaseCommand`` in a weather command's
bases and expects the BaseCommand API (``bot``, ``logger``, ``translate``,
``send_response``, ``get_max_message_length``, ``response_translator``). Each
command sets ``translation_ns`` to its own catalog namespace.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Optional, Union

from .clients.mqtt_weather import (
    get_mqtt_weather_topic,
    load_mqtt_weather_format_config,
    mqtt_weather_display_for_topic,
)
from .utils import format_temperature_high_low

Number = Union[int, float]


def load_open_meteo_model(config: Any, logger: Any) -> Optional[str]:
    """[Weather] weather_model for Open-Meteo's ``models`` parameter.

    Unset means ``best_match``; explicitly blank means None (omit the parameter
    and let Open-Meteo choose). Anything outside ``[a-z0-9_,.-]`` falls back to
    ``best_match`` with a warning.
    """
    if config.has_option('Weather', 'weather_model'):
        model = config.get('Weather', 'weather_model', fallback='').strip().lower()
        if not model:
            return None
    else:
        model = 'best_match'
    if not re.fullmatch(r'[a-z0-9_,.-]+', model):
        logger.warning(f"Invalid weather_model '{model}', using 'best_match'")
        return 'best_match'
    return model


class WeatherCommandMixin:
    # Catalog namespace for this command's strings, e.g. "commands.wx".
    translation_ns: str = ""

    # Provided by BaseCommand.
    bot: Any
    logger: Any
    response_translator: Any
    translate: Any
    send_response: Any
    get_max_message_length: Any

    @staticmethod
    def _coordinates_query(lat: float, lon: float) -> str:
        """A "lat,lon" location for these coordinates, in fixed decimals.

        Plain str() of a float can give "1e-05", which the coordinate pattern
        does not match, so the point would be geocoded as a place name.
        """
        return f"{lat:.5f},{lon:.5f}"

    @staticmethod
    def _parse_forecast_suffix(
        location_parts: list[str], max_days: int, *, allow_hourly: bool
    ) -> tuple[list[str], str, int]:
        """Strip a trailing forecast option from the location words.

        "tomorrow", "hourly" (when allowed), "7day"/"7-day", "Nd" or a bare "N"
        with 2 <= N <= max_days. Returns (remaining words, forecast type, days);
        anything else stays part of the location and the type is "default".
        """
        forecast_type = "default"
        num_days = 7  # Default for multi-day forecast
        if len(location_parts) > 0:
            last_part = location_parts[-1].lower()
            if last_part == "tomorrow":
                forecast_type = "tomorrow"
                location_parts = location_parts[:-1]
            elif allow_hourly and last_part == "hourly":
                forecast_type = "hourly"
                location_parts = location_parts[:-1]
            elif last_part in ["7day", "7-day"]:
                forecast_type = "multiday"
                num_days = 7
                location_parts = location_parts[:-1]
            else:
                nd_match = re.fullmatch(r"(\d+)d", last_part)
                if nd_match:
                    days = int(nd_match.group(1))
                    if 2 <= days <= max_days:
                        forecast_type = "multiday"
                        num_days = days
                        location_parts = location_parts[:-1]
                elif last_part.isdigit():
                    days = int(last_part)
                    if 2 <= days <= max_days:
                        forecast_type = "multiday"
                        num_days = days
                        location_parts = location_parts[:-1]
        return location_parts, forecast_type, num_days

    def _format_high_low(self, high: Optional[Number], low: Optional[Number], temp_symbol: str) -> str:
        """Format high/low using [Weather] temperature_*_format templates."""
        return format_temperature_high_low(self.bot.config, high, low, temp_symbol, self.logger,
                                           translator=self.response_translator)

    def _get_custom_mqtt_weather_topic(self, location: Optional[str] = None) -> Optional[str]:
        """MQTT topic for custom.mqtt_weather.<name> (see get_mqtt_weather_topic)."""
        return get_mqtt_weather_topic(self.bot.config, location)

    def _mqtt_weather_line(self, topic: str, forecast_type: str, location_name: Optional[str]) -> str:
        """Format the cached MQTT weather payload for ``topic``, or the matching error text."""
        ns = self.translation_ns
        if forecast_type != "default":
            return self.translate(f"{ns}.mqtt_forecast_not_supported")
        fmt = load_mqtt_weather_format_config(self.bot.config)
        cache = getattr(self.bot, "mqtt_weather_cache", None)
        text, err = mqtt_weather_display_for_topic(topic, cache, fmt)
        if text is not None:
            return f"{location_name}: {text}" if location_name else text
        return self._mqtt_weather_error_key(err)

    def _mqtt_weather_error_key(self, err: Optional[str]) -> str:
        """The translated reply for an MQTT weather lookup error (overridable)."""
        ns = self.translation_ns
        if err == "no_cache":
            return self.translate(f"{ns}.mqtt_weather_no_subscriber")
        if err in ("no_data", "empty_payload", "empty_after_sanitize"):
            return self.translate(f"{ns}.mqtt_weather_no_data")
        if err == "stale":
            return self.translate(f"{ns}.mqtt_weather_stale")
        detail = (err or "unknown").replace("_", " ")
        return self.translate(f"{ns}.mqtt_weather_payload_error", detail=detail)

    def _count_display_width(self, text: str) -> int:
        """Count UTF-8 byte length of text. Matches RF packet byte limit from get_max_message_length()."""
        return len(text.encode('utf-8'))

    @staticmethod
    def _hour_label(start_time_str: str) -> str:
        """12-hour label for an ISO start time; empty when missing or unparseable."""
        if not start_time_str:
            return ""
        try:
            hour = datetime.fromisoformat(start_time_str.replace('Z', '+00:00')).hour
        except (ValueError, TypeError):
            return ""
        return f"{hour % 12 or 12}{'AM' if hour < 12 else 'PM'}"

    @staticmethod
    def _without_arrow(direction: str) -> str:
        """A wind direction label without its leading arrow emoji ("↖️WNW" -> "WNW").

        Hourly lines drop the arrow to keep an extra hour in the reply; the
        letters (translated or not) stay.
        """
        return re.sub(r"^\W+", "", direction)

    @staticmethod
    def _short_hourly_description(description: str) -> str:
        """Long hourly descriptions keep their first three words, or 18 characters."""
        if len(description) > 18:
            words = description.split()
            return ' '.join(words[:3]) if len(words) > 3 else description[:18]
        return description

    def _pack_hourly_lines(self, hourly_lines: Iterable[str], max_length: int) -> str:
        """Pack consecutive whole hours into one UTF-8 byte budget."""
        lines: list[str] = []
        for line in hourly_lines:
            if self._count_display_width("\n".join(lines + [line])) > max_length:
                break
            lines.append(line)
        return "\n".join(lines) or self.translate(f'{self.translation_ns}.hourly_not_available')

    async def _send_multiday_forecast(self, message: Any, forecast_text: str) -> None:
        """Send a multi-day forecast, packing whole lines into as few messages as fit.

        A line too long for one message goes out on its own. Messages after the
        first skip the per-user rate limit and are spaced 2 s apart.
        """
        max_length = self.get_max_message_length(message)
        lines = [line.strip() for line in forecast_text.split('\n') if line.strip()]
        if not lines:
            return
        if self._count_display_width(forecast_text) <= max_length:
            await self.send_response(message, forecast_text)
            return

        current_message = ""
        message_count = 0
        for i, line in enumerate(lines):
            if not line:
                continue
            test_message = current_message + "\n" + line if current_message else line
            if self._count_display_width(test_message) > max_length:
                if current_message:
                    await self.send_response(
                        message, current_message,
                        skip_user_rate_limit=(message_count > 0)
                    )
                    message_count += 1
                    if i < len(lines):
                        await asyncio.sleep(2.0)
                    current_message = line
                else:
                    # Single line is too long, send it anyway (will be truncated by bot)
                    await self.send_response(
                        message, line,
                        skip_user_rate_limit=(message_count > 0)
                    )
                    message_count += 1
                    if i < len(lines) - 1:
                        await asyncio.sleep(2.0)
                    current_message = ""
            elif current_message:
                current_message += "\n" + line
            else:
                current_message = line

        # Last message is a continuation, so it skips the per-user rate limit
        if current_message:
            await self.send_response(message, current_message, skip_user_rate_limit=True)
