#!/usr/bin/env python3
"""
Parse [Scheduled_Messages] option keys into APScheduler CronTrigger instances.

Supports:
- Standard 5-field crontab: minute hour day-of-month month day-of-week
- Preset aliases: @yearly, @annually, @monthly, @weekly, @daily, @midnight, @hourly
- Deprecated legacy HHMM (24-hour, no colon) for daily firing at that clock time
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from apscheduler.triggers.cron import CronTrigger

# Maps @preset (lowercase) -> 5-field crontab (APScheduler does not accept @syntax in from_crontab).
_SPECIAL_PRESET_TO_CRON: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


@dataclass(frozen=True)
class ScheduleParseResult:
    """Outcome of parsing a scheduled message key."""

    trigger: Optional[CronTrigger]
    """APScheduler trigger, or None if the expression is invalid."""

    display_label: str
    """Human-readable schedule for logs and the ``schedule`` command."""

    is_deprecated_hhmm: bool
    """True when the legacy HHMM daily form was used."""


def is_valid_legacy_hhmm(time_str: str) -> bool:
    """Return True if ``time_str`` is a valid legacy HHMM clock time (24h)."""
    try:
        if len(time_str) != 4 or not time_str.isdigit():
            return False
        hour = int(time_str[:2])
        minute = int(time_str[2:])
        return 0 <= hour <= 23 and 0 <= minute <= 59
    except ValueError:
        return False


def parse_schedule_key(
    schedule_key: str,
    timezone,
) -> ScheduleParseResult:
    """Parse a ``[Scheduled_Messages]`` option name into a :class:`CronTrigger`.

    Args:
        schedule_key: Raw config option key (e.g. ``0 9 * * *``, ``@daily``, ``0900``).
        timezone: ``tzinfo`` or string accepted by APScheduler (same as scheduler).

    Returns:
        ScheduleParseResult with ``trigger`` set when valid, else ``trigger`` is None
        and ``display_label`` still describes what was attempted.
    """
    raw = (schedule_key or "").strip()
    if not raw:
        return ScheduleParseResult(None, "", False)

    lowered = raw.lower()

    # 1) Deprecated legacy HHMM (must be checked before numeric cron fragments).
    if is_valid_legacy_hhmm(raw):
        hour = int(raw[:2])
        minute = int(raw[2:])
        trigger = CronTrigger(hour=hour, minute=minute, timezone=timezone)
        display = f"{hour:02d}:{minute:02d}"
        return ScheduleParseResult(trigger, display, True)

    # 2) @preset aliases
    if lowered in _SPECIAL_PRESET_TO_CRON:
        cron_expr = _SPECIAL_PRESET_TO_CRON[lowered]
        try:
            trigger = CronTrigger.from_crontab(cron_expr, timezone=timezone)
        except ValueError:
            return ScheduleParseResult(None, raw, False)
        return ScheduleParseResult(trigger, raw, False)

    # 3) Standard 5-field crontab
    try:
        trigger = CronTrigger.from_crontab(raw, timezone=timezone)
    except ValueError:
        return ScheduleParseResult(None, raw, False)
    return ScheduleParseResult(trigger, raw, False)
