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
    assert "50.00F" in text or "50.00" in text
    assert "10.00C" in text or "10.00" in text
    assert "55%" in text

    payload_bad = dict(payload)
    payload_bad["device"] = "999"
    raw2 = json.dumps(payload_bad).encode()
    text2, err2 = format_mqtt_weather_payload(raw2, fmt)
    assert text2 is None
    assert err2 == "device_filter_mismatch"


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
