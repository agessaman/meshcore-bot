#!/usr/bin/env python3
"""
Parse ``[Scheduled_Messages]`` option keys into APScheduler CronTrigger instances,
and option values into ``(channel, message, scope)`` for optional regional flood scope.

Supports (schedule keys):
- Standard 5-field crontab: minute hour day-of-month month day-of-week
- Preset aliases: @yearly, @annually, @monthly, @weekly, @daily, @midnight, @hourly
- Deprecated legacy HHMM (24-hour, no colon) for daily firing at that clock time

Day-of-week uses APScheduler numbering (0=Monday … 6=Sunday), not Vixie cron
(0=Sunday; 7 often allowed). Prefer mon–sun names. ``@weekly`` expands to
``0 0 * * 0`` (Monday 00:00). See docs/configuration.md and config.ini.example.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from apscheduler.triggers.cron import CronTrigger

dow_names = r'(?:mon|tue|wed|thu|fri|sat|sun)'
month_names = r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)'
hour_pattern = r'(?:\d+|\*)(?:[\/-]\d+|(?:,\d+)+)?'
minute_pattern = r'(?:\d+|\*)(?:[\/\-]\d+|(?:,\d+)+)?'
day_pattern = r'(?:[0-2]?\d|3[01]|\*)(?:(?:[\/\-](?:[0-2]?\d|3[01]))|(?:,(?:[0-2]?\d|3[01]))+)?'
month_pattern = fr'(?:[1-9]|1[0-2]|{month_names}|\*)(?:(?:[\/\-,](?:[1-9]|1[0-2]|{month_names}))+)?'
dow_pattern = fr'(?:[0-6]|{dow_names}|\*)(?:(?:[\/\-,](?:[0-6]|{dow_names}))+)?'
dow_prefix_pattern = r'(?:(?:1st|2nd|3rd|4th|5th|last))'

cron_re = re.compile(fr'^{minute_pattern}\s+{hour_pattern}\s+{day_pattern}\s+{month_pattern}\s+{dow_pattern}$', re.I)

# regexs use to parse flexible cron
hour_re = re.compile(fr'(?:\s?){hour_pattern}h\b', re.I)
minute_re = re.compile(fr'(?:\s?){minute_pattern}m\b', re.I)
day_re = re.compile(fr'\b{day_pattern}d\b', re.I)
month_re = re.compile(fr'\b{month_pattern}\b', re.I)
dow_re = re.compile(r'\b'+dow_pattern+r'\b', re.I)
dow_prefix_re = re.compile(fr'\b{dow_prefix_pattern}\b', re.I)
week_re = re.compile(r'(?:\s?)(?:\d{1,2}|\*)(?:(?:[\/\-]\d{1,2})|(?:,\d{1,2})+)?w\b', re.I)
hhmm_re = re.compile(r'\b(?P<hour>(?:[0-1]\d|2[0-3])):?(?P<min>[0-5]\d)\b')
start_date_re = re.compile(r'\bstart[:=](?P<date>[\d\-]+)\b')
end_date_re = re.compile(r'\bend[:=](?P<date>[\d\-]+)\b')
iso_date_re = re.compile(r'\d{4}-\d{1,2}-\d{1,2}')

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

    error: Optional[str]
    """When trigger is None, error can include reason for invalid trigger."""


def encode_schedule_key_for_ini(schedule_key: str) -> str:
    """Make a schedule key safe to store as an INI key.

    The line-based INI writer/reader (and ``configparser`` itself) treat ``:``
    (and ``=``) as the key/value separator, so a flexible-cron key containing an
    HH:MM time — e.g. ``4th tue 14:00 jan-oct`` — would corrupt the line. ``!``
    never appears in a valid schedule key, so it round-trips losslessly with
    :func:`decode_schedule_key_from_ini`.
    """
    return (schedule_key or "").replace(":", "!")


def decode_schedule_key_from_ini(schedule_key: str) -> str:
    """Inverse of :func:`encode_schedule_key_for_ini`."""
    return (schedule_key or "").replace("!", ":")


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


def parse_flexible_cron(time_str: str) -> dict[str, str]:
    """Return a dictionary of CronTrigger parameters by parsing time_str.
    If time_str does not parse as a valid time expression return None."""
    params = dict()

    # Is a start date being specified
    match = start_date_re.search(time_str)
    if match:
        date_match = iso_date_re.search(match.group(0))
        if date_match:
            params['start_date'] = date_match.group(0)
            if params['start_date'].startswith('0000-'):
                # insert the current year
                params['start_date'] = re.sub(r'^0000', str(date.today().year), params['start_date'])
            time_str = start_date_re.sub('', time_str)
        else:
            params['error'] = 'Invalid ISO date (YYYY-MM-DD)'

    # Is an end date being specified
    match = end_date_re.search(time_str)
    if match:
        date_match = iso_date_re.search(match.group(0))
        if date_match:
            params['end_date'] = date_match.group(0)
            if params['end_date'].startswith('0000-'):
                # insert the current year
                params['end_date'] = re.sub(r'^0000', str(date.today().year), params['end_date'])
            time_str = end_date_re.sub('', time_str)
        else:
            params['error'] = 'Invalid ISO date (YYYY-MM-DD)'

    # Look for HH:MM or HHMM specification
    match = hhmm_re.search(time_str)
    if match:
        params['hour'] = match.group('hour')
        params['minute'] = match.group('min')
        time_str = hhmm_re.sub('', time_str)
    else:
        # Look for hours
        match = hour_re.search(time_str)
        if match:
            # [:-1] removes the 'h' from the expression
            params['hour'] = match.group(0)[:-1].strip()
            time_str = hour_re.sub('', time_str)

        # Look for mins
        match = minute_re.search(time_str)
        if match:
            # [:-1] removes the 'm' from the expression
            params['minute'] = match.group(0)[:-1].strip()
            time_str = minute_re.sub('', time_str)

    # Look for day of month
    match = day_re.search(time_str)
    if match:
        # [:-1] removes the 'd' form the expression
        params['day'] = match.group(0)[:-1]
        time_str = day_re.sub('', time_str)

    # Look for month
    match = month_re.search(time_str)
    if match:
        params['month'] = match.group(0)
        time_str = month_re.sub('', time_str)

    # Look for week
    match = week_re.search(time_str)
    if match:
        params['week'] = match.group(0)[:-1]
        time_str = week_re.sub('', time_str)

    # Look for day of week
    prefix_match = dow_prefix_re.search(time_str)
    dow_match = dow_re.search(time_str)
    if prefix_match and dow_match:
        params['day'] = f"{prefix_match.group(0)} {dow_match.group(0)}"
        time_str = dow_prefix_re.sub('', time_str)
        time_str = dow_re.sub('', time_str)
    elif prefix_match and prefix_match.group(0).lower() == 'last':
        # special case: last day of the month
        params['day'] = 'last'
        time_str = dow_prefix_re.sub('', time_str)
    elif dow_match:
        params['day_of_week'] = dow_match.group(0)
        time_str = dow_re.sub('', time_str)

    # If anything left in time_str (other than whitespace) is
    # considered an error condition and return empty dictionary
    if time_str.strip() and 'error' not in params:
        return dict(error=f'Unparsable component: "{time_str}" parsed: {params=}')
    return params


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
        return ScheduleParseResult(None, "", False, "No schedule_key specified")

    lowered = raw.lower()

    # 1) Deprecated legacy HHMM (must be checked before numeric cron fragments).
    if is_valid_legacy_hhmm(raw):
        hour = int(raw[:2])
        minute = int(raw[2:])
        trigger = CronTrigger(hour=hour, minute=minute, timezone=timezone)
        display = f"{hour:02d}:{minute:02d}"
        return ScheduleParseResult(trigger, display, True, None)

    # 2) @preset aliases
    if lowered in _SPECIAL_PRESET_TO_CRON:
        cron_expr = _SPECIAL_PRESET_TO_CRON[lowered]
        try:
            trigger = CronTrigger.from_crontab(cron_expr, timezone=timezone)
        except ValueError as e:
            return ScheduleParseResult(None, raw, False, str(e))
        return ScheduleParseResult(trigger, raw, False, None)

    # 3) Standard 5-field crontab
    if re.match(cron_re, raw):
        trigger = CronTrigger.from_crontab(raw, timezone=timezone)
        return ScheduleParseResult(trigger, raw, False, None)

    # 4) Flexible crontab
    cron_trigger_kw = parse_flexible_cron(raw)
    if cron_trigger_kw:
        if 'error' in cron_trigger_kw:
            return ScheduleParseResult(None, raw, False, cron_trigger_kw['error'])

        trigger = CronTrigger(**cron_trigger_kw, timezone=timezone)
        print(f"{trigger=}")
        if not trigger:
            return ScheduleParseResult(trigger, raw, False, "Unparsable time specification")
        return ScheduleParseResult(trigger, raw, False, None)

    # Crontab entry not recognized
    return ScheduleParseResult(None, raw, False, "crontab entry not recognized or parsable")
