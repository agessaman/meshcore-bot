#!/usr/bin/env python3
"""Unit tests for modules.clients.mqtt_weather."""

import json
import time
from configparser import ConfigParser

from modules.clients.mqtt_weather import (
    MqttWeatherCache,
    MqttWeatherFormatConfig,
    format_mqtt_weather_payload,
    get_mqtt_weather_topic,
    iter_mqtt_weather_topics,
    load_mqtt_weather_format_config,
    mqtt_weather_display_for_topic,
    validate_mqtt_weather_topic,
)


def test_validate_mqtt_topic() -> None:
    assert validate_mqtt_weather_topic("home/weather") is True
    assert validate_mqtt_weather_topic("") is False
    assert validate_mqtt_weather_topic("a/+") is False
    assert validate_mqtt_weather_topic("a/#") is False


def test_get_mqtt_weather_topic_named_and_default() -> None:
    cfg = ConfigParser()
    cfg.read_dict(
        {
            "Weather": {
                "custom.mqtt_weather.default": "t/default",
                "custom.mqtt_weather.patio": "t/patio",
            }
        }
    )
    assert get_mqtt_weather_topic(cfg, None) == "t/default"
    assert get_mqtt_weather_topic(cfg, "patio") == "t/patio"
    assert get_mqtt_weather_topic(cfg, "missing") is None


def test_iter_mqtt_weather_topics_unique() -> None:
    cfg = ConfigParser()
    cfg.read_dict(
        {
            "Weather": {
                "custom.mqtt_weather.default": "same/topic",
                "custom.mqtt_weather.a": "same/topic",
                "custom.mqtt_weather.bad": "+/invalid",
            }
        }
    )
    topics = iter_mqtt_weather_topics(cfg)
    assert topics == ["same/topic"]


def test_format_passthrough() -> None:
    fmt = MqttWeatherFormatConfig(
        output_mode="passthrough",
        json_template="",
        json_device_key="",
        json_device_value="",
        max_payload_bytes=1024,
        passthrough_max_length=200,
        stale_after_seconds=60.0,
    )
    raw = b"Date/Time: now\nTemp: 20 C"
    text, err = format_mqtt_weather_payload(raw, fmt)
    assert err is None
    assert text == "Date/Time: now\nTemp: 20 C"


def test_format_json_template_and_device_filter() -> None:
    fmt = MqttWeatherFormatConfig(
        output_mode="json_template",
        json_template="{time} | {temperature_f}F / {temperature_c}C | {humidity}%",
        json_device_key="device",
        json_device_value="231",
        max_payload_bytes=1024,
        passthrough_max_length=500,
        stale_after_seconds=60.0,
    )
    payload = {
        "device": "231",
        "temperature_F": 50.0,
        "humidity": 55,
        "time": "2026-04-01 12:00",
    }
    raw = json.dumps(payload).encode()
    text, err = format_mqtt_weather_payload(raw, fmt)
    assert err is None
    assert "50.0F" in text or "50F" in text
    assert "10.0C" in text or "10C" in text
    assert "55" in text and "%" in text

    payload_bad = dict(payload)
    payload_bad["device"] = "999"
    raw2 = json.dumps(payload_bad).encode()
    text2, err2 = format_mqtt_weather_payload(raw2, fmt)
    assert text2 is None
    assert err2 == "device_filter_mismatch"


def test_format_json_template_extended_placeholders() -> None:
    fmt = MqttWeatherFormatConfig(
        output_mode="json_template",
        json_template=(
            "{temperature_f}F dew {dewpoint_f} | {pressure_hpa} hPa | "
            "rain24 {rain_24hour_in} | wind {wind_current_kmh} @ {wind_direction_deg} | "
            "bat {battery_voltage_v} | up {uptime_s} | {sensor_name}"
        ),
        json_device_key="",
        json_device_value="",
        max_payload_bytes=4096,
        passthrough_max_length=500,
        stale_after_seconds=60.0,
    )
    payload = {
        "temperature_f": 69.8,
        "dewpoint_f": 45.02,
        "pressure_hpa": 1024.5,
        "rain_24hour_in": 0.319,
        "wind_current_kmh": 0,
        "wind_direction_deg": 45,
        "battery_voltage_v": 4.12,
        "uptime_s": 1_059_124,
        "sensor_name": "Weather Station",
    }
    raw = json.dumps(payload).encode()
    text, err = format_mqtt_weather_payload(raw, fmt)
    assert err is None
    assert "69.8F" in text or "69.8" in text
    assert "1024.5" in text
    assert "1059124" in text
    assert "Weather Station" in text


def test_load_mqtt_weather_json_template_literal_percent() -> None:
    """ConfigParser interpolation must not treat '% RH' as an interpolation token."""
    cfg = ConfigParser()
    cfg.read_string(
        "[MqttWeather]\n"
        "output_mode = json_template\n"
        "json_template = {humidity}% RH | {pressure_hpa:.0f} hPa\n"
    )
    fmt = load_mqtt_weather_format_config(cfg)
    assert "% RH" in fmt.json_template
    assert "{pressure_hpa:.0f}" in fmt.json_template
    raw = json.dumps({"humidity": 41, "pressure_hpa": 1024.5}).encode()
    text, err = format_mqtt_weather_payload(raw, fmt)
    assert err is None
    assert "41.0% RH" in text or "41% RH" in text
    assert "1024 hPa" in text


def test_payload_too_large() -> None:
    fmt = MqttWeatherFormatConfig(
        output_mode="passthrough",
        json_template="",
        json_device_key="",
        json_device_value="",
        max_payload_bytes=10,
        passthrough_max_length=500,
        stale_after_seconds=60.0,
    )
    text, err = format_mqtt_weather_payload(b"x" * 20, fmt)
    assert text is None
    assert err == "payload_too_large"


def test_invalid_template_placeholder() -> None:
    fmt = MqttWeatherFormatConfig(
        output_mode="json_template",
        json_template="{time} {bogus}",
        json_device_key="",
        json_device_value="",
        max_payload_bytes=1024,
        passthrough_max_length=500,
        stale_after_seconds=60.0,
    )
    raw = json.dumps(
        {"time": "t", "temperature_F": 32, "humidity": 40, "device": "1"}
    ).encode()
    text, err = format_mqtt_weather_payload(raw, fmt)
    assert text is None
    assert err is not None
    assert "bogus" in err


def test_mqtt_weather_display_stale() -> None:
    cache = MqttWeatherCache()
    cache.update("t1", b"hello")
    # force old timestamp
    with cache._lock:
        payload, _ts = cache._by_topic["t1"]
        cache._by_topic["t1"] = (payload, time.monotonic() - 100.0)

    fmt = MqttWeatherFormatConfig(
        output_mode="passthrough",
        json_template="",
        json_device_key="",
        json_device_value="",
        max_payload_bytes=1024,
        passthrough_max_length=500,
        stale_after_seconds=30.0,
    )
    text, err = mqtt_weather_display_for_topic("t1", cache, fmt)
    assert text is None
    assert err == "stale"


def test_mqtt_weather_display_no_cache() -> None:
    fmt = MqttWeatherFormatConfig(
        output_mode="passthrough",
        json_template="",
        json_device_key="",
        json_device_value="",
        max_payload_bytes=1024,
        passthrough_max_length=500,
        stale_after_seconds=60.0,
    )
    text, err = mqtt_weather_display_for_topic("missing", MqttWeatherCache(), fmt)
    assert text is None
    assert err == "no_data"

    text2, err2 = mqtt_weather_display_for_topic("missing", None, fmt)
    assert text2 is None
    assert err2 == "no_cache"
