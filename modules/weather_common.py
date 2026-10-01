"""Behavior the wx (NOAA) and gwx (Open-Meteo) commands share.

``WeatherCommandMixin`` goes before ``BaseCommand`` in a weather command's
bases and expects the BaseCommand API (``bot``, ``logger``, ``translate``,
``send_response``, ``get_max_message_length``, ``response_translator``). Each
command sets ``translation_ns`` to its own catalog namespace.
"""

from __future__ import annotations

import asyncio
import re
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
