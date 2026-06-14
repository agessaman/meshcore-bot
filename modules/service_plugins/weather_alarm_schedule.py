#!/usr/bin/env python3
"""Parse ``[Weather_Service] weather_alarm`` schedule expressions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.base import BaseTrigger


@dataclass(frozen=True)
class WeatherAlarmSchedule:
    """Parsed weather forecast schedule."""

    mode: str
    """``fixed``, ``interval``, or ``sun_event``."""

    fixed_times: list[tuple[int, int]] = field(default_factory=list)
    interval_hours: int | None = None
    interval_minutes: int | None = None
    sun_event: str | None = None
    display: str = ""


def parse_clock_time(time_str: str) -> tuple[int, int]:
    """Parse ``HH:MM``, ``H:MM``, or legacy ``HHMM`` into hour and minute.

    Raises:
        ValueError: If the time string is invalid.
    """
    raw = (time_str or "").strip()
    if not raw:
        raise ValueError("empty time")

    if ":" in raw:
        parts = raw.split(":", 1)
        hour = int(parts[0])
        minute = int(parts[1])
    elif len(raw) == 4 and raw.isdigit():
        hour = int(raw[:2])
        minute = int(raw[2:])
    else:
        raise ValueError(f"invalid time format: {time_str!r}")

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid time value: {time_str!r}")

    return hour, minute


def parse_weather_alarm_schedule(raw: str) -> WeatherAlarmSchedule:
    """Parse ``weather_alarm`` config value.

    Supported forms:
    - Fixed clock time: ``6:00``, ``0600``
    - Multiple times: ``6:00, 12:00, 18:00``
    - Interval: ``every hour``, ``every 2 hours``, ``every 30 minutes``, ``@hourly``
    - Sun events: ``sunrise``, ``sunset``
    """
    value = (raw or "6:00").strip()
    lowered = value.lower()

    if lowered in ("sunrise", "sunset"):
        return WeatherAlarmSchedule(mode="sun_event", sun_event=lowered, display=lowered)

    if lowered == "@hourly":
        return WeatherAlarmSchedule(
            mode="interval",
            interval_hours=1,
            display="every hour",
        )

    hour_match = re.fullmatch(r"every\s+(?:(\d+)\s+)?hours?", lowered)
    if hour_match:
        hours = int(hour_match.group(1) or "1")
        if not (1 <= hours <= 24):
            raise ValueError(f"invalid hourly interval: {hours}")
        label = "every hour" if hours == 1 else f"every {hours} hours"
        return WeatherAlarmSchedule(
            mode="interval",
            interval_hours=hours,
            display=label,
        )

    minute_match = re.fullmatch(r"every\s+(\d+)\s+minutes?", lowered)
    if minute_match:
        minutes = int(minute_match.group(1))
        if not (1 <= minutes <= 59):
            raise ValueError(f"invalid minute interval: {minutes}")
        label = "every minute" if minutes == 1 else f"every {minutes} minutes"
        return WeatherAlarmSchedule(
            mode="interval",
            interval_minutes=minutes,
            display=label,
        )

    if "," in value:
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if not parts:
            raise ValueError("empty schedule list")
        fixed_times = [parse_clock_time(part) for part in parts]
        display = ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in fixed_times)
        return WeatherAlarmSchedule(mode="fixed", fixed_times=fixed_times, display=display)

    hour, minute = parse_clock_time(value)
    return WeatherAlarmSchedule(
        mode="fixed",
        fixed_times=[(hour, minute)],
        display=f"{hour:02d}:{minute:02d}",
    )


def build_forecast_cron_triggers(
    schedule: WeatherAlarmSchedule,
    timezone,
) -> list[tuple[str, BaseTrigger, str]]:
    """Build APScheduler triggers for a parsed schedule.

    Fixed times use ``CronTrigger``; intervals use ``IntervalTrigger`` so that
    the period is counted from the moment the scheduler starts rather than
    being pinned to clock boundaries (e.g. 00:00, 02:00, 04:00 …).

    Returns:
        List of ``(job_id, trigger, label)`` tuples.
    """
    if schedule.mode == "fixed":
        triggers: list[tuple[str, BaseTrigger, str]] = []
        for hour, minute in schedule.fixed_times:
            label = f"{hour:02d}:{minute:02d}"
            triggers.append(
                (
                    f"weather_forecast_{hour:02d}{minute:02d}",
                    CronTrigger(hour=hour, minute=minute, timezone=timezone),
                    label,
                )
            )
        return triggers

    if schedule.mode == "interval":
        if schedule.interval_hours is not None:
            trigger = IntervalTrigger(hours=schedule.interval_hours, timezone=timezone)
            return [("weather_forecast_interval", trigger, schedule.display)]

        if schedule.interval_minutes is not None:
            trigger = IntervalTrigger(minutes=schedule.interval_minutes, timezone=timezone)
            return [("weather_forecast_interval", trigger, schedule.display)]

    raise ValueError(f"unsupported schedule mode: {schedule.mode}")
