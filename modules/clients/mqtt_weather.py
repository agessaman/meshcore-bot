#!/usr/bin/env python3
"""
MQTT custom weather: config resolution, payload parsing, and safe formatting.

Topics are configured under [Weather] as custom.mqtt_weather.<name> (same pattern as WXSIM URLs).
Broker and format options live in [MqttWeather].
"""

from __future__ import annotations

import json
import re
import threading
import time
from configparser import ConfigParser
from dataclasses import dataclass
from typing import Any

from ..security_utils import sanitize_input

MQTT_WEATHER_PREFIX = "custom.mqtt_weather."

# Placeholders allowed in json_template (str.format_map); no arbitrary JSON keys.
JSON_TEMPLATE_PLACEHOLDERS = frozenset(
    {"time", "temperature_f", "temperature_c", "humidity", "device"}
)

_TOPIC_INVALID_CHARS = re.compile(r"[\x00+#]")

# Max lengths for string fields extracted from JSON (avoid huge blobs in mesh replies).
_MAX_TIME_STR_LEN = 120
_MAX_DEVICE_STR_LEN = 64
_DEFAULT_PASSTHROUGH_MAX = 500


def validate_mqtt_weather_topic(topic: str) -> bool:
    """Return True if topic is non-empty and has no wildcards or NUL."""
    if not topic or not str(topic).strip():
        return False
    t = str(topic).strip()
    if "#" in t or "+" in t:
        return False
    if _TOPIC_INVALID_CHARS.search(t):
        return False
    return True


def get_mqtt_weather_topic(config: ConfigParser, location: str | None) -> str | None:
    """Resolve MQTT topic for a logical source name (mirrors WXSIM URL lookup).

    Args:
        config: Bot config.
        location: None for default source, else the name from ``wx <name>``.

    Returns:
        Topic string or None.
    """
    section = "Weather"
    if not config.has_section(section):
        return None

    if location:
        location = location.strip()
        location_lower = location.lower()
        prefix = MQTT_WEATHER_PREFIX
        for key, value in config.items(section):
            if not key.startswith(prefix):
                continue
            key_location = key[len(prefix) :].strip()
            if key_location.lower() == location_lower:
                topic = (value or "").strip()
                if topic and validate_mqtt_weather_topic(topic):
                    return topic
        return None

    default_key = f"{MQTT_WEATHER_PREFIX}default"
    if config.has_option(section, default_key):
        topic = config.get(section, default_key, fallback="").strip()
        if topic and validate_mqtt_weather_topic(topic):
            return topic
    return None


def iter_mqtt_weather_topics(config: ConfigParser) -> list[str]:
    """Return unique valid topics from all custom.mqtt_weather.* keys."""
    section = "Weather"
    if not config.has_section(section):
        return []
    seen: set[str] = set()
    out: list[str] = []
    prefix = MQTT_WEATHER_PREFIX
    for key, value in config.items(section):
        if not key.startswith(prefix):
            continue
        topic = (value or "").strip()
        if not topic or not validate_mqtt_weather_topic(topic):
            continue
        if topic not in seen:
            seen.add(topic)
            out.append(topic)
    return out


@dataclass
class MqttWeatherFormatConfig:
    """Formatting options loaded from [MqttWeather]."""

    output_mode: str  # passthrough | json_template
    json_template: str
    json_device_key: str
    json_device_value: str
    max_payload_bytes: int
    passthrough_max_length: int
    stale_after_seconds: float


def load_mqtt_weather_format_config(config: ConfigParser) -> MqttWeatherFormatConfig:
    """Read [MqttWeather] formatting options (defaults are safe)."""
    sec = "MqttWeather"
    mode = "passthrough"
    if config.has_section(sec):
        mode = config.get(sec, "output_mode", fallback="passthrough").strip().lower()
    if mode not in ("passthrough", "json_template"):
        mode = "passthrough"

    default_template = (
        "{time} | {temperature_f}°F ({temperature_c}°C) | RH {humidity}%"
    )
    template = default_template
    dev_key = ""
    dev_val = ""
    max_bytes = 65536
    pass_max = _DEFAULT_PASSTHROUGH_MAX
    stale = 3600.0

    if config.has_section(sec):
        template = config.get(sec, "json_template", fallback=default_template)
        dev_key = config.get(sec, "json_device_key", fallback="").strip()
        dev_val = config.get(sec, "json_device_value", fallback="").strip()
        max_bytes = config.getint(sec, "max_payload_bytes", fallback=65536)
        pass_max = config.getint(sec, "passthrough_max_length", fallback=_DEFAULT_PASSTHROUGH_MAX)
        stale = config.getfloat(sec, "stale_after_seconds", fallback=3600.0)

    if max_bytes < 256:
        max_bytes = 256
    if max_bytes > 1_048_576:
        max_bytes = 1_048_576
    if pass_max < 64:
        pass_max = 64
    if pass_max > 4000:
        pass_max = 4000
    if stale < 5.0:
        stale = 5.0
    if stale > 86400.0 * 7:
        stale = 86400.0 * 7

    return MqttWeatherFormatConfig(
        output_mode=mode,
        json_template=template,
        json_device_key=dev_key,
        json_device_value=dev_val,
        max_payload_bytes=max_bytes,
        passthrough_max_length=pass_max,
        stale_after_seconds=stale,
    )


def _normalize_json_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value):  # NaN
            return ""
        return str(value)
    return str(value).strip()


def _safe_time_str(value: Any) -> str:
    s = _normalize_json_scalar(value)
    if len(s) > _MAX_TIME_STR_LEN:
        s = s[:_MAX_TIME_STR_LEN]
    return sanitize_input(s, max_length=_MAX_TIME_STR_LEN, strip_controls=True)


def _safe_device_str(value: Any) -> str:
    s = _normalize_json_scalar(value)
    if len(s) > _MAX_DEVICE_STR_LEN:
        s = s[:_MAX_DEVICE_STR_LEN]
    return sanitize_input(s, max_length=_MAX_DEVICE_STR_LEN, strip_controls=True)


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        if f != f or abs(f) > 1e6:  # NaN or absurd
            return None
        return f
    if isinstance(value, str):
        try:
            f = float(value.strip())
            if f != f or abs(f) > 1e6:
                return None
            return f
        except ValueError:
            return None
    return None


def _validate_json_template(template: str) -> str | None:
    """Return error message if template uses unknown placeholders."""
    for m in re.finditer(r"\{([^}]+)\}", template):
        name = m.group(1).strip()
        if name and name not in JSON_TEMPLATE_PLACEHOLDERS:
            return f"Invalid template placeholder: {{{name}}}"
    return None


def format_mqtt_weather_payload(
    payload: bytes,
    fmt: MqttWeatherFormatConfig,
) -> tuple[str | None, str | None]:
    """Turn raw MQTT payload bytes into display text.

    Returns:
        (text, error) — exactly one is non-None.
    """
    if not payload:
        return None, "empty_payload"

    if len(payload) > fmt.max_payload_bytes:
        return None, "payload_too_large"

    try:
        text = payload.decode("utf-8", errors="replace")
    except Exception:
        return None, "decode_error"

    if fmt.output_mode == "passthrough":
        out = sanitize_input(
            text, max_length=fmt.passthrough_max_length, strip_controls=True
        )
        if not out:
            return None, "empty_after_sanitize"
        return out, None

    # json_template
    tmpl_err = _validate_json_template(fmt.json_template)
    if tmpl_err:
        return None, tmpl_err

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, "invalid_json"

    if not isinstance(data, dict):
        return None, "json_not_object"

    if fmt.json_device_key:
        if _normalize_json_scalar(data.get(fmt.json_device_key)) != fmt.json_device_value:
            return None, "device_filter_mismatch"

    temp_f = _coerce_float(data.get("temperature_F"))
    if temp_f is None:
        temp_f = _coerce_float(data.get("temperature_f"))
    hum = _coerce_float(data.get("humidity"))
    if hum is not None:
        hum = round(hum, 2)
        if hum < 0 or hum > 100:
            hum = None

    temp_c: float | None = None
    if temp_f is not None:
        temp_c = (temp_f - 32.0) * 5.0 / 9.0

    mapping = {
        "time": _safe_time_str(data.get("time")),
        "device": _safe_device_str(data.get("device")),
        "temperature_f": f"{temp_f:.2f}" if temp_f is not None else "",
        "temperature_c": f"{temp_c:.2f}" if temp_c is not None else "",
        "humidity": f"{hum:.0f}" if hum is not None else "",
    }

    try:
        out = fmt.json_template.format_map(mapping)
    except (KeyError, ValueError, IndexError):
        return None, "template_format_error"

    out = sanitize_input(out, max_length=fmt.passthrough_max_length, strip_controls=True)
    if not out:
        return None, "empty_after_sanitize"
    return out, None


class MqttWeatherCache:
    """Thread-safe last-value cache per subscribed topic."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_topic: dict[str, tuple[bytes, float]] = {}

    def update(self, topic: str, payload: bytes) -> None:
        with self._lock:
            self._by_topic[topic] = (payload, time.monotonic())

    def get(self, topic: str) -> tuple[bytes | None, float | None]:
        with self._lock:
            row = self._by_topic.get(topic)
            if not row:
                return None, None
            return row[0], row[1]

    def clear(self) -> None:
        with self._lock:
            self._by_topic.clear()


def mqtt_weather_display_for_topic(
    topic: str,
    cache: MqttWeatherCache | None,
    fmt: MqttWeatherFormatConfig,
) -> tuple[str | None, str | None]:
    """Read cache for topic, check staleness, return formatted line or error key."""
    if cache is None:
        return None, "no_cache"

    payload, ts = cache.get(topic)
    if payload is None or ts is None:
        return None, "no_data"

    age = time.monotonic() - ts
    if age > fmt.stale_after_seconds:
        return None, "stale"

    return format_mqtt_weather_payload(payload, fmt)
