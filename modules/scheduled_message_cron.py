#!/usr/bin/env python3
"""
Parse ``[Scheduled_Messages]`` option keys into APScheduler CronTrigger instances,
and option values into ``(channel, message, scope)`` for optional regional flood scope.

Option values may carry optional ``start=YYYY-MM-DD`` / ``end=YYYY-MM-DD`` bounds
ahead of the channel, which limit a schedule to a date range. They live on the value
because crontab has no field for them, and ``=`` keeps them clear of the ``:`` that
separates channel from message.

Supports (schedule keys):
- Standard 5-field crontab: minute hour day-of-month month day-of-week
- Positional day-of-month: ``last-fri``, ``4th-tue``, ``1st-mon,3rd-mon`` in the
  day-of-month field, for patterns plain crontab cannot express
- Preset aliases: @yearly, @annually, @monthly, @weekly, @daily, @midnight, @hourly
- Deprecated legacy HHMM (24-hour, no colon) for daily firing at that clock time

Day-of-week uses APScheduler numbering (0=Monday … 6=Sunday), not Vixie cron
(0=Sunday; 7 often allowed). Prefer mon–sun names. ``@weekly`` expands to
``0 0 * * 0`` (Monday 00:00). See docs/configuration.md and config.ini.example.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Optional

from apscheduler.triggers.cron import CronTrigger

# Leading "start=YYYY-MM-DD" / "end=YYYY-MM-DD" tokens on an option value. Anchored to
# the front so a message body can never be mistaken for a bound, and keyed with "=" so
# they do not disturb the "channel:message" split.
_BOUND_RE = re.compile(r"^\s*(?P<key>start|end)=(?P<date>\S+)", re.IGNORECASE)


def split_schedule_bounds(raw: str) -> tuple[str | None, str | None, str]:
    """Strip leading ``start=`` / ``end=`` tokens off an option value.

    Args:
        raw: Config value, e.g. ``start=2027-01-01 Public:Hello`` or ``Public:Hello``.

    Returns:
        ``(start, end, rest)`` -- ISO date strings (or None) and the remaining value,
        which is what :func:`parse_scheduled_message_value` expects.

    Raises:
        ValueError: If a bound is repeated, is not an ISO date, or ends after it starts.
    """
    rest = raw or ""
    found: dict[str, str] = {}
    while True:
        match = _BOUND_RE.match(rest)
        if not match:
            break
        key = match.group("key").lower()
        if key in found:
            raise ValueError(f"{key}= given more than once")
        try:
            datetime.date.fromisoformat(match.group("date"))
        except ValueError:
            raise ValueError(
                f"{key}={match.group('date')} is not an ISO date (YYYY-MM-DD)"
            ) from None
        found[key] = match.group("date")
        rest = rest[match.end():]
    start, end = found.get("start"), found.get("end")
    if start and end and end < start:
        raise ValueError(f"end={end} is before start={start}")
    return start, end, rest.strip()


def parse_scheduled_message_value(raw: str) -> tuple[str, str, str | None]:
    """Parse a ``[Scheduled_Messages]`` option value into ``(channel, message, scope)``.

    **Legacy (unscoped):** ``channel:body`` — split on the first ``:`` only; ``scope`` is
    ``None`` (global flood).

    **Scoped:** ``channel:#region:body`` — exactly three segments from ``split(':', 2)``
    where the middle segment starts with ``#`` after strip. The message body may contain
    further colons. Scope must not contain ``:``.

    Args:
        raw: Config value, e.g. ``Public:Hello`` or ``Public:#sea:Hello: more``.

    Returns:
        ``(channel, message, scope)`` with ``scope`` set only for the scoped form.

    Raises:
        ValueError: If there is no ``:`` (cannot separate channel from body).
    """
    s = (raw or "").strip()
    if ":" not in s:
        raise ValueError("scheduled message value must be channel:message")
    if _BOUND_RE.match(s):
        # split_schedule_bounds() removes these; reaching here means a caller skipped it.
        raise ValueError("start=/end= bounds must be stripped before parsing the value")
    parts = s.split(":", 2)
    if len(parts) == 3 and parts[1].strip().startswith("#"):
        channel = parts[0].strip()
        scope = parts[1].strip()
        message = parts[2].strip()
        return channel, message, scope
    channel, message = s.split(":", 1)
    return channel.strip(), message.strip(), None

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


# APScheduler's day-of-month field already understands positional expressions such as
# "last fri" and "4th tue", but they contain a space, which is crontab's field separator
# -- so from_crontab() can never reach them. Accept "-" or "_" in place of that space and
# restore it before handing the field over. Neither separator is ambiguous: crontab range
# endpoints are digits, so "last-fri" cannot be read as a range.
_POSITIONAL_DOM_RE = re.compile(
    r"(1st|2nd|3rd|4th|5th|last)[-_](mon|tue|wed|thu|fri|sat|sun)",
    re.IGNORECASE,
)


def _from_crontab(expr: str, timezone, start_date=None, end_date=None) -> CronTrigger:
    """``CronTrigger.from_crontab`` plus escaped positional day-of-month and date bounds.

    ``0 19 last-fri * *`` fires 19:00 on the last Friday of each month, and
    ``0 19 1st-tue,3rd-tue * *`` on the first and third Tuesday. Field splitting and
    the resulting trigger are otherwise identical to ``CronTrigger.from_crontab``,
    which is itself only this constructor call -- reproduced here because it takes no
    ``start_date``/``end_date``.

    Raises:
        ValueError: If ``expr`` is not a valid 5-field crontab expression.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"Wrong number of fields; got {len(fields)}, expected 5")

    # Only the day-of-month field takes positional expressions; an escape anywhere else
    # is left in place so APScheduler rejects it.
    fields[2] = _POSITIONAL_DOM_RE.sub(r"\1 \2", fields[2])
    return CronTrigger(
        minute=fields[0],
        hour=fields[1],
        day=fields[2],
        month=fields[3],
        day_of_week=fields[4],
        timezone=timezone,
        start_date=start_date,
        # An end date reads as "through this day", so run it to the end of that day
        # rather than stopping at its midnight.
        end_date=f"{end_date} 23:59:59" if end_date else None,
    )


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
    start_date: str | None = None,
    end_date: str | None = None,
) -> ScheduleParseResult:
    """Parse a ``[Scheduled_Messages]`` option name into a :class:`CronTrigger`.

    Args:
        schedule_key: Raw config option key (e.g. ``0 9 * * *``, ``0 19 last-fri * *``,
            ``@daily``, ``0900``).
        timezone: ``tzinfo`` or string accepted by APScheduler (same as scheduler).
        start_date: Optional ISO date; the schedule does not fire before it.
        end_date: Optional ISO date; the schedule does not fire after the end of it.
            Both come from the option *value* via :func:`split_schedule_bounds`.

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
        trigger = _from_crontab(
            f"{minute} {hour} * * *", timezone, start_date, end_date
        )
        display = f"{hour:02d}:{minute:02d}"
        return ScheduleParseResult(trigger, display, True)

    # 2) @preset aliases
    if lowered in _SPECIAL_PRESET_TO_CRON:
        cron_expr = _SPECIAL_PRESET_TO_CRON[lowered]
        try:
            trigger = _from_crontab(cron_expr, timezone, start_date, end_date)
        except ValueError:
            return ScheduleParseResult(None, raw, False)
        return ScheduleParseResult(trigger, raw, False)

    # 3) Standard 5-field crontab (optionally with a positional day-of-month)
    try:
        trigger = _from_crontab(raw, timezone, start_date, end_date)
    except ValueError:
        return ScheduleParseResult(None, raw, False)
    return ScheduleParseResult(trigger, raw, False)
