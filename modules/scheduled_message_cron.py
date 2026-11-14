#!/usr/bin/env python3
"""
Parse ``[Scheduled_Messages]`` option keys into APScheduler CronTrigger instances,
and option values into ``(channel, message, scope)`` for optional regional flood scope.

Option values may carry optional ``start=YYYY-MM-DD`` / ``end=YYYY-MM-DD`` bounds
ahead of the channel, which limit a schedule to a date range, and ``week=`` with an
ISO week-number expression (``week=*/2`` for every other week). They live on the value
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
``0 0 * * 0`` (Monday 00:00). See docs/scheduled-messages.md and config.ini.example.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Optional

from apscheduler.triggers.cron import CronTrigger
from apscheduler.util import astimezone, convert_to_datetime

# Leading "start=YYYY-MM-DD" / "end=YYYY-MM-DD" tokens on an option value. Anchored to
# the front so a message body can never be mistaken for a bound, and keyed with "=" so
# they do not disturb the "channel:message" split. A bound is a whole token ended by
# whitespace with no ":" in it, so a channel that merely begins with "start=" (as in
# "start=ops:Hello") stays a channel.
_BOUND_RE = re.compile(r"^\s*(?P<key>start|end|week)=(?P<date>[^\s:]+)\s+", re.IGNORECASE)
# A bound keyword with nothing usable after it ("start= Public:Hi", "start =2027-01-01
# Public:Hi"). Rejected outright rather than read as a channel called "start= Public".
_DANGLING_BOUND_RE = re.compile(r"^(?:start|end|week)(?:\s*=(?:\s|$)|\s+=)", re.IGNORECASE)
# An option with nothing after it ("start=2027-01-01"): the value has no channel.
_TRAILING_OPTION_RE = re.compile(r"(?:start|end|week)=[^\s:]+", re.IGNORECASE)
# An ISO week-number expression in crontab syntax: numbers, *, ranges, steps and lists.
_WEEK_EXPR_RE = re.compile(r"[\d*/,\-]+")
# Exactly YYYY-MM-DD. date.fromisoformat() also takes 20270101 and 2027-W01-1 on newer
# Pythons, which APScheduler then cannot parse as a start or end date.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_iso_date(value: str) -> bool:
    if not _ISO_DATE_RE.fullmatch(value or ""):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def bounds_error(start: str | None, end: str | None) -> str | None:
    """Why a ``start``/``end`` pair is unusable, or None when it is fine (or absent)."""
    for label, value in (("Start date", start), ("End date", end)):
        if value and not _is_iso_date(value):
            return f"{label} must be an ISO date (YYYY-MM-DD)"
    # Both are strict YYYY-MM-DD here, so string order is date order.
    if start and end and end < start:
        return "End date is before the start date"
    return None


def week_error(week: str | None) -> str | None:
    """Why a ``week=`` expression is unusable, or None when it is fine (or absent).

    It is APScheduler's ISO week-number field: ``*/2`` is odd weeks (1, 3, 5 ...),
    ``2-53/2`` even ones.
    """
    if not week:
        return None
    if not _WEEK_EXPR_RE.fullmatch(week):
        return "Weeks must be ISO week numbers, ranges or steps, such as */2 or 2-53/2"
    try:
        CronTrigger(week=week, timezone="UTC")
    except ValueError as exc:
        return f"Weeks: {exc}"
    return None


def split_schedule_options(raw: str) -> tuple[str | None, str | None, str | None, str]:
    """Strip leading ``start=`` / ``end=`` / ``week=`` tokens off an option value.

    Args:
        raw: Config value, e.g. ``start=2027-01-01 week=*/2 Public:Hello`` or
            ``Public:Hello``.

    Returns:
        ``(start, end, week, rest)`` -- ISO date strings and the week expression (each
        or None) and the remaining value, which is what
        :func:`parse_scheduled_message_value` expects.

    Raises:
        ValueError: If an option is repeated or malformed, or the end is before the start.
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
        value = match.group("date")
        if key == "week":
            problem = week_error(value)
            if problem:
                raise ValueError(f"week={value}: {problem}")
        elif not _is_iso_date(value):
            raise ValueError(f"{key}={value} is not an ISO date (YYYY-MM-DD)")
        found[key] = value
        rest = rest[match.end():]
    if _TRAILING_OPTION_RE.fullmatch(rest.strip()):
        raise ValueError(f"{rest.strip()} needs a channel:message after it")
    if _DANGLING_BOUND_RE.match(rest.strip()):
        raise ValueError(
            "start=/end=/week= needs a value, as in start=YYYY-MM-DD or week=*/2"
        )
    start, end = found.get("start"), found.get("end")
    if start and end and end < start:
        raise ValueError(f"end={end} is before start={start}")
    return start, end, found.get("week"), rest.strip()


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
        # split_schedule_options() removes these; reaching here means a caller skipped it.
        raise ValueError("start=/end=/week= must be stripped before parsing the value")
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

    error: Optional[str] = None
    """Why the expression was rejected, when ``trigger`` is None and there is a reason."""


# APScheduler's day-of-month field already understands positional expressions such as
# "last fri" and "4th tue", but they contain a space, which is crontab's field separator
# -- so from_crontab() can never reach them. Accept "-" or "_" in place of that space and
# restore it before handing the field over. Neither separator is ambiguous: crontab range
# endpoints are digits, so "last-fri" cannot be read as a range.
_POSITIONAL_DOM_RE = re.compile(
    r"(1st|2nd|3rd|4th|5th|last)[-_](mon|tue|wed|thu|fri|sat|sun)",
    re.IGNORECASE,
)


def _end_of_local_day(end_date: str, timezone) -> datetime.datetime:
    """The last second before ``end_date``'s following local day begins.

    Worked out in UTC from the start of the next day rather than as ``23:59:59``,
    which a DST change that skips the day's last hour (America/Nuuk, 2027-03-27)
    would push past midnight and let the schedule fire on the following date.
    """
    tz = astimezone(timezone)
    next_day = datetime.date.fromisoformat(end_date) + datetime.timedelta(days=1)
    next_midnight = convert_to_datetime(
        datetime.datetime.combine(next_day, datetime.time()), tz, "end_date"
    )
    utc_end = next_midnight.astimezone(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    return utc_end.astimezone(tz)


_ORDINAL_PREFIXES = ("1st", "2nd", "3rd", "4th", "5th", "last")


def _unescape_positional_dom(field: str) -> str:
    """Restore the space in each escaped positional item of a day-of-month field.

    Each comma-separated item must be a positional expression on its own or contain
    none: APScheduler silently drops anything after one (``1st tue/2`` fires every
    first Tuesday), so ``1st-tue-3rd-tue`` or ``last-fri/2`` would save and then not
    do what it says.

    Raises:
        ValueError: If an item mixes a positional expression with anything else.
    """
    items = []
    for item in field.split(","):
        # APScheduler matches "last" and the ordinals as prefixes, so "last--fri",
        # "lastfri" or "last-" would quietly mean the last day of the month.
        lowered = item.lower()
        if lowered.startswith(_ORDINAL_PREFIXES) and lowered != "last" and not _POSITIONAL_DOM_RE.fullmatch(item):
            raise ValueError(
                f"{item!r} is not a day-of-month: use last, or an ordinal and a weekday "
                "such as last-fri or 2nd-wed"
            )
        if _POSITIONAL_DOM_RE.search(item):
            match = _POSITIONAL_DOM_RE.fullmatch(item)
            if not match:
                raise ValueError(
                    f"{item!r}: a positional day such as last-fri or 4th-tue must stand "
                    "alone between commas"
                )
            item = f"{match.group(1).lower()} {match.group(2).lower()}"
        items.append(item)
    return ",".join(items)


def _from_crontab(
    expr: str, timezone, start_date=None, end_date=None, week=None
) -> CronTrigger:
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
    day = _unescape_positional_dom(fields[2])
    if day != fields[2] and fields[4] != "*":
        # APScheduler requires both fields to match, so "1st-tue * mon" never fires.
        raise ValueError(
            "a positional day such as last-fri already names the weekday; "
            "leave day-of-week as *"
        )
    fields[2] = day
    return CronTrigger(
        minute=fields[0],
        hour=fields[1],
        day=fields[2],
        month=fields[3],
        day_of_week=fields[4],
        week=week,
        timezone=timezone,
        start_date=start_date,
        # An end date reads as "through this day", so run it to the end of that day
        # rather than stopping at its midnight.
        end_date=_end_of_local_day(end_date, timezone) if end_date else None,
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
    week: str | None = None,
) -> ScheduleParseResult:
    """Parse a ``[Scheduled_Messages]`` option name into a :class:`CronTrigger`.

    Args:
        schedule_key: Raw config option key (e.g. ``0 9 * * *``, ``0 19 last-fri * *``,
            ``@daily``, ``0900``).
        timezone: ``tzinfo`` or string accepted by APScheduler (same as scheduler).
        start_date: Optional ISO date; the schedule does not fire before it.
        end_date: Optional ISO date; the schedule does not fire after the end of it.
        week: Optional ISO week-number expression, e.g. ``*/2`` for every other week.
            All three come from the option *value* via :func:`split_schedule_options`.

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
        try:
            trigger = _from_crontab(
                f"{minute} {hour} * * *", timezone, start_date, end_date, week
            )
        except ValueError as exc:
            return ScheduleParseResult(None, raw, True, str(exc))
        display = f"{hour:02d}:{minute:02d}"
        return ScheduleParseResult(trigger, display, True)

    # 2) @preset aliases
    if lowered in _SPECIAL_PRESET_TO_CRON:
        cron_expr = _SPECIAL_PRESET_TO_CRON[lowered]
        try:
            trigger = _from_crontab(cron_expr, timezone, start_date, end_date, week)
        except ValueError as exc:
            return ScheduleParseResult(None, raw, False, str(exc))
        return ScheduleParseResult(trigger, raw, False)

    # 3) Standard 5-field crontab (optionally with a positional day-of-month)
    try:
        trigger = _from_crontab(raw, timezone, start_date, end_date, week)
    except ValueError as exc:
        return ScheduleParseResult(None, raw, False, str(exc))
    return ScheduleParseResult(trigger, raw, False)
