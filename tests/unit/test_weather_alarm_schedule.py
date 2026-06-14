#!/usr/bin/env python3
"""Unit tests for weather_alarm schedule parsing."""

import pytest

from modules.service_plugins.weather_alarm_schedule import (
    build_forecast_cron_triggers,
    parse_clock_time,
    parse_weather_alarm_schedule,
)


def test_parse_clock_time_hhmm_colon():
    assert parse_clock_time("6:00") == (6, 0)
    assert parse_clock_time("06:30") == (6, 30)


def test_parse_clock_time_legacy_hhmm():
    assert parse_clock_time("0630") == (6, 30)


def test_parse_weather_alarm_single_fixed_time():
    schedule = parse_weather_alarm_schedule("6:00")
    assert schedule.mode == "fixed"
    assert schedule.fixed_times == [(6, 0)]
    assert schedule.display == "06:00"


def test_parse_weather_alarm_multiple_fixed_times():
    schedule = parse_weather_alarm_schedule("6:00, 12:00, 18:00")
    assert schedule.mode == "fixed"
    assert schedule.fixed_times == [(6, 0), (12, 0), (18, 0)]
    assert schedule.display == "06:00, 12:00, 18:00"


def test_parse_weather_alarm_every_hour_variants():
    assert parse_weather_alarm_schedule("every hour").interval_hours == 1
    assert parse_weather_alarm_schedule("every 1 hour").interval_hours == 1
    assert parse_weather_alarm_schedule("every 2 hours").interval_hours == 2
    assert parse_weather_alarm_schedule("@hourly").interval_hours == 1


def test_parse_weather_alarm_every_minutes():
    schedule = parse_weather_alarm_schedule("every 30 minutes")
    assert schedule.mode == "interval"
    assert schedule.interval_minutes == 30


def test_parse_weather_alarm_sun_events():
    sunrise = parse_weather_alarm_schedule("sunrise")
    assert sunrise.mode == "sun_event"
    assert sunrise.sun_event == "sunrise"

    sunset = parse_weather_alarm_schedule("Sunset")
    assert sunset.mode == "sun_event"
    assert sunset.sun_event == "sunset"


def test_parse_weather_alarm_invalid_time():
    with pytest.raises(ValueError):
        parse_clock_time("25:00")


def test_build_forecast_cron_triggers_fixed_times():
    schedule = parse_weather_alarm_schedule("6:00, 18:00")
    triggers = build_forecast_cron_triggers(schedule, "UTC")
    assert len(triggers) == 2
    assert triggers[0][0] == "weather_forecast_0600"
    assert triggers[1][0] == "weather_forecast_1800"


def test_build_forecast_cron_triggers_hourly():
    schedule = parse_weather_alarm_schedule("every hour")
    triggers = build_forecast_cron_triggers(schedule, "UTC")
    assert len(triggers) == 1
    assert triggers[0][0] == "weather_forecast_interval"
