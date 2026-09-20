#!/usr/bin/env python3
"""
Shared formatting for NWS weather alerts
Used by both !wx alerts (WxCommand) and the proactive WeatherService broadcasts
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

# Month keys are the English abbreviations used throughout the catalogs; the
# translated value comes from common.date_time.month_abbreviations.
MONTH_KEYS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

FULL_MONTH_KEYS = ("January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November", "December")

SEVERITY_EMOJI = {
    'Extreme': '🔴',
    'Severe': '🟠',
    'Moderate': '🟡',
    'Minor': '⚪',
    'Unknown': '⚪',
}

# English fallbacks for the four event types NWS actually publishes. Anything
# else (NWS also emits titles we classify as "Unknown") falls back to the raw
# event_type rather than a translation key.
EVENT_TYPE_ABBREV = {
    'Warning': 'Warn',
    'Watch': 'Watch',
    'Advisory': 'Adv',
    'Statement': 'Stmt',
}

CITY_ABBREV = {
    "Seattle": "SEA", "Portland": "PDX", "San Francisco": "SF",
    "Los Angeles": "LA", "New York": "NYC", "Chicago": "CHI",
    "Houston": "HOU", "Phoenix": "PHX", "Philadelphia": "PHL",
    "San Antonio": "SAT", "San Diego": "SAN", "Dallas": "DAL",
    "San Jose": "SJC", "Austin": "AUS", "Jacksonville": "JAX",
    "Columbus": "CMH", "Fort Worth": "FTW", "Charlotte": "CLT",
    "Denver": "DEN", "Washington": "DC", "Boston": "BOS",
    "El Paso": "ELP", "Detroit": "DTW", "Nashville": "BNA",
    "Oklahoma City": "OKC", "Las Vegas": "LAS", "Memphis": "MEM",
    "Louisville": "SDF", "Baltimore": "BWI", "Milwaukee": "MKE",
    "Albuquerque": "ABQ", "Tucson": "TUS", "Fresno": "FAT",
    "Sacramento": "SAC", "Kansas City": "KC", "Mesa": "MSC",
    "Atlanta": "ATL", "Omaha": "OMA", "Colorado Springs": "COS",
    "Raleigh": "RDU", "Virginia Beach": "ORF", "Miami": "MIA",
    "Oakland": "OAK", "Minneapolis": "MSP", "Tulsa": "TUL",
    "Cleveland": "CLE", "Wichita": "ICT", "Arlington": "ARL",
    "Tampa": "TPA", "New Orleans": "MSY", "Honolulu": "HNL",
    "Anchorage": "ANC", "Bellingham": "BLI", "Everett": "EVE",
    "Spokane": "GEG", "Tacoma": "TAC", "Yakima": "YKM",
    "Olympia": "OLM", "Vancouver": "YVR", "Victoria": "YYJ",
}


def translate_or(translator: Any, key: str, default: str, **kwargs: Any) -> str:
    """Translate ``key``, falling back to ``default`` when it is not in any catalog.

    ``Translator.translate`` deliberately echoes the key back when a lookup
    misses in both the requested locale and the English fallback, which makes
    missing translations visible in development but leaks a dotted key path
    into a mesh broadcast in production. Callers with a sensible English
    default should use this instead.

    Args:
        translator: Object with a ``translate(key, **kwargs)`` method, or None.
        key: Dot-separated key path.
        default: Value to use when the key is absent.
        **kwargs: Formatting parameters for the translated string.

    Returns:
        str: Translated string, or ``default`` formatted with ``kwargs``.
    """
    if translator is not None:
        value = translator.translate(key, **kwargs)
        if value != key:
            return value
    if kwargs:
        try:
            return default.format(**kwargs)
        except (KeyError, ValueError, IndexError):
            return default
    return default


def severity_emoji(severity: str) -> str:
    """Return the colored dot for an alert severity.

    Args:
        severity: NWS severity ('Extreme', 'Severe', 'Moderate', 'Minor', ...).

    Returns:
        str: Emoji for the severity, defaulting to the 'Unknown' dot.
    """
    return SEVERITY_EMOJI.get(severity, '⚪')


def event_type_abbrev(event_type: str, translator: Any = None) -> str:
    """Abbreviate an alert event type ('Warning' -> 'Warn').

    Args:
        event_type: NWS event type, or any string our title parser produced.
        translator: Optional translator for localized abbreviations.

    Returns:
        str: Localized abbreviation, or ``event_type`` unchanged when we have
            no abbreviation for it.
    """
    if not event_type:
        return ""
    return translate_or(
        translator,
        f'common.alerts.event_types.{event_type}',
        EVENT_TYPE_ABBREV.get(event_type, event_type),
    )


def abbreviate_city_name(city: str) -> str:
    """Abbreviate a city name for compact display (Seattle -> SEA).

    Args:
        city: City name from an NWS office string.

    Returns:
        str: Known abbreviation, else the initials of the first three words,
            else the first four characters upper-cased.
    """
    if not city:
        return city

    if city in CITY_ABBREV:
        return CITY_ABBREV[city]

    # Partial match handles "Seattle WA" -> "SEA"
    for full_name, abbrev in CITY_ABBREV.items():
        if full_name in city:
            return abbrev

    words = city.split()
    if len(words) > 1:
        initials = ''.join(word[0].upper() for word in words[:3])
        if len(initials) <= 4:
            return initials

    return city[:4].upper() if len(city) >= 4 else city.upper()


@dataclass(frozen=True)
class AlertTime:
    """A parsed alert timestamp with its locale-specific renderings.

    Holding the parts rather than a formatted string is what lets callers pick
    between the time-only and dated forms without re-parsing localized output
    with English-shaped regexes.
    """

    month: str
    day: int
    hour_12: int
    meridiem: str
    time_only: str
    dated: str


def parse_display_time(time_str: str, translator: Any = None) -> Optional[AlertTime]:
    """Parse an ISO-8601 alert timestamp into its localized parts.

    Args:
        time_str: Timestamp from an NWS alert (e.g. '2025-12-17T01:00:00-08:00').
        translator: Optional translator for month names and AM/PM.

    Returns:
        Optional[AlertTime]: Parsed parts, or None when ``time_str`` is not ISO
            format or cannot be parsed.
    """
    if not time_str or 'T' not in time_str or not re.match(r'\d{4}-\d{2}-\d{2}T', time_str):
        return None

    try:
        dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
    except ValueError:
        # NWS occasionally emits a truncated timestamp ('2025-12-17T01:0').
        try:
            date_part, _, clock_part = time_str.partition('T')
            clock_part = re.split(r'[-+]', clock_part)[0]
            dt = datetime.fromisoformat(f"{date_part}T{clock_part}")
        except (ValueError, IndexError):
            return None

    month_key = MONTH_KEYS[dt.month - 1]
    month = translate_or(translator, f'common.date_time.month_abbreviations.{month_key}', month_key)

    hour = dt.hour
    if hour == 0:
        hour_12, meridiem_key, meridiem_default = 12, 'am', 'AM'
    elif hour < 12:
        hour_12, meridiem_key, meridiem_default = hour, 'am', 'AM'
    elif hour == 12:
        hour_12, meridiem_key, meridiem_default = 12, 'pm', 'PM'
    else:
        hour_12, meridiem_key, meridiem_default = hour - 12, 'pm', 'PM'

    meridiem = translate_or(translator, f'common.alerts.{meridiem_key}', meridiem_default)

    # Russian writes "6 дня" where English writes "6PM", so the separator is
    # the locale's business, not ours.
    time_only = translate_or(
        translator, 'common.alerts.time_12h', '{hour}{meridiem}',
        hour=hour_12, meridiem=meridiem,
    )
    dated = translate_or(
        translator, 'common.alerts.date_12h', '{month} {day} {time}',
        month=month, day=dt.day, time=time_only,
    )
    return AlertTime(month=month, day=dt.day, hour_12=hour_12, meridiem=meridiem,
                     time_only=time_only, dated=dated)


def compact_time(time_str: str, translator: Any = None) -> str:
    """Shorten an alert timestamp for a mesh message.

    ISO timestamps become the localized dated form ('Dec 17 1AM'). Free-text
    NWS strings ('December 16 at 3:12PM') get their month abbreviated and their
    ':00' minutes and filler 'at' dropped.

    Args:
        time_str: Timestamp or free-text time from an NWS alert.
        translator: Optional translator for month names and AM/PM.

    Returns:
        str: Compacted time string, or ``time_str`` unchanged when empty.
    """
    if not time_str:
        return time_str

    parsed = parse_display_time(time_str, translator)
    if parsed is not None:
        return parsed.dated

    # Remove leading zeros from hours: "6:00AM" -> "6AM"
    time_str = re.sub(r'(\d+):00(AM|PM)', r'\1\2', time_str)

    # Abbreviate month names. Longest first so "June" is not matched by "Jun".
    for full_key, abbrev_key in zip(FULL_MONTH_KEYS, MONTH_KEYS, strict=True):
        if full_key not in time_str:
            continue
        abbrev = translate_or(translator, f'common.date_time.month_abbreviations.{abbrev_key}', abbrev_key)
        time_str = time_str.replace(full_key, abbrev)

    # Remove "at" before time: "December 16 at 3:12PM" -> "Dec 16 3:12PM"
    time_str = re.sub(r'\s+at\s+', ' ', time_str)

    # NWS writes these titles in English ("until December 17 at 6:00AM PST"),
    # so localize the meridiem the same way the ISO path does.
    def _localize_meridiem(match: "re.Match[str]") -> str:
        meridiem = translate_or(
            translator, f'common.alerts.{match.group(2).lower()}', match.group(2).upper()
        )
        return translate_or(
            translator, 'common.alerts.time_12h', '{hour}{meridiem}',
            hour=match.group(1), meridiem=meridiem,
        )

    time_str = re.sub(r'(\d+(?::\d+)?)\s*(AM|PM)\b', _localize_meridiem, time_str,
                      flags=re.IGNORECASE)

    return time_str


def shorten_event(event: str, limit: Optional[int] = 15, max_words: int = 2) -> str:
    """Trim a long event name to its leading words.

    Args:
        event: NWS event name (e.g. 'High Wind Warning').
        limit: Length above which the name is trimmed, or None to never trim.
        max_words: Number of leading words to keep when trimming.

    Returns:
        str: Shortened event name.
    """
    if limit is None or len(event) <= limit:
        return event
    words = event.split()
    if len(words) > max_words:
        return ' '.join(words[:max_words])
    return event[:limit]


def format_event_label(event: str, event_type: str, translator: Any = None,
                       limit: Optional[int] = 15, max_words: int = 2) -> str:
    """Render '<event> <type-abbrev>', dropping the type when it is redundant.

    Args:
        event: NWS event name (e.g. 'High Wind Warning').
        event_type: NWS event type (e.g. 'Warning').
        translator: Optional translator for the type abbreviation.
        limit: Length above which the event name is trimmed, or None to never trim.
        max_words: Number of leading words to keep when trimming.

    Returns:
        str: Combined label, or just the abbreviation when ``event`` is empty.
    """
    abbrev = event_type_abbrev(event_type, translator)
    if not event:
        return abbrev
    short = shorten_event(event, limit, max_words)
    if event_type and event_type.lower() in event.lower():
        # "High Wind Warning" already says "Warning"
        return short
    return f"{short} {abbrev}" if abbrev else short


def format_event_plain(event: str, event_type: str, translator: Any = None) -> str:
    """Render '<event> <type-abbrev>' with no trimming and no redundancy check.

    This is the fallback form used when a message has already overrun its
    budget and the caller is retrying with less detail.

    Args:
        event: NWS event name.
        event_type: NWS event type.
        translator: Optional translator for the type abbreviation.

    Returns:
        str: Combined label, or just the abbreviation when ``event`` is empty.
    """
    abbrev = event_type_abbrev(event_type, translator)
    return f"{event} {abbrev}" if event else abbrev


def first_location(area_desc: str, limit: int = 20) -> str:
    """Extract one short place name from an NWS area description.

    Args:
        area_desc: Semicolon-separated areas ('King County; Snohomish County').
        limit: Maximum length of the returned name.

    Returns:
        str: Short place name, or '' when ``area_desc`` is empty.
    """
    if not area_desc:
        return ""

    first = area_desc.split(';')[0].strip()
    if ',' in first:
        # "Seattle, WA" -> "Seattle"
        location = first.split(',')[0].strip()
    else:
        words = first.split()
        if len(words) > 1 and words[-1].lower() in ('county', 'parish', 'borough'):
            location = words[0]
        else:
            location = first
    return location[:limit]


def format_office(office: str, translator: Any = None, limit: int = 10) -> str:
    """Render the issuing office compactly ('NWS Seattle WA' -> 'by NWS SEA').

    Args:
        office: Office string from the alert.
        translator: Optional translator for the 'by' label.
        limit: Length to truncate a single-token office to.

    Returns:
        str: Formatted office attribution, or '' when ``office`` is empty.
    """
    if not office:
        return ""
    by_label = translate_or(translator, 'common.alerts.by', 'by')
    parts = office.split()
    if len(parts) >= 2:
        return f"{by_label} {parts[0]} {abbreviate_city_name(parts[1])}"
    return f"{by_label} {office[:limit]}"


def format_alert_compact(alert: dict[str, Any], translator: Any = None,
                         include_details: bool = True,
                         include_location: bool = True) -> str:
    """Format one alert for a mesh message.

    Produces "🟠High Wind Warn King til 6AM by NWS SEA" (details) or
    "🟠High Wind Warn" (summary). The caller is responsible for appending a
    shortened link, which needs async work.

    Args:
        alert: Alert dict with event, event_type, severity, expires, office,
            and optionally area_desc.
        translator: Optional translator for all labels.
        include_details: If True, include location, expiry and office.
        include_location: If True (and ``include_details``), include the area.

    Returns:
        str: Formatted alert string.
    """
    event = alert.get('event', '')
    event_type = alert.get('event_type', '')
    severity = alert.get('severity', 'Unknown')
    emoji = severity_emoji(severity)

    if not include_details:
        return emoji + format_event_plain(event, event_type, translator)

    result = emoji + format_event_label(event, event_type, translator)

    if include_location:
        location = first_location(alert.get('area_desc', ''))
        if location:
            result += f" {location}"

    expires = alert.get('expires', '')
    if expires:
        til_label = translate_or(translator, 'common.alerts.til', 'til')
        result += f" {til_label} {_expiry_label(expires, translator)}"

    office = format_office(alert.get('office', ''), translator)
    if office:
        result += f" {office}"

    return result


def extract_clock(time_str: str, translator: Any = None) -> Optional[str]:
    """Pull just the clock time out of a free-text NWS timestamp.

    Args:
        time_str: Free-text time ('December 17 at 6:00AM PST').
        translator: Optional translator for AM/PM.

    Returns:
        Optional[str]: Localized clock time ('6AM', '6 утра'), or None when the
            string carries no 12-hour clock.
    """
    match = re.search(r'(\d+)(?::(\d+))?\s*(AM|PM)\b', time_str, re.IGNORECASE)
    if match is None:
        return None
    hour, minutes, meridiem_raw = match.group(1), match.group(2), match.group(3).upper()
    # ":00" reads as noise in a message this tight.
    hour_text = f"{hour}:{minutes}" if minutes and minutes != '00' else hour
    meridiem = translate_or(translator, f'common.alerts.{meridiem_raw.lower()}', meridiem_raw)
    return translate_or(translator, 'common.alerts.time_12h', '{hour}{meridiem}',
                        hour=hour_text, meridiem=meridiem)


def _expiry_label(expires: str, translator: Any = None) -> str:
    """Render an expiry timestamp as compactly as it can be read.

    Args:
        expires: Expiry timestamp from the alert.
        translator: Optional translator for month names and AM/PM.

    Returns:
        str: The clock time alone where we can find one, else a truncated
            compact form.
    """
    parsed = parse_display_time(expires, translator)
    if parsed is not None:
        # A dated expiry costs ~8 chars of a 130-byte budget; the time alone is
        # unambiguous for alerts that expire within a day.
        return parsed.time_only

    # Same reasoning for the free-text form NWS puts in ATOM titles. Read the
    # clock off the English source rather than re-parsing localized output.
    clock = extract_clock(expires, translator)
    if clock is not None:
        return clock
    return compact_time(expires, translator)[:15]


def format_alert_window(alert: dict[str, Any], translator: Any = None) -> str:
    """Render an alert's effective/expiry window ('from Dec 16 3PM til Dec 17 6AM').

    Args:
        alert: Alert dict with optional effective and expires timestamps.
        translator: Optional translator for labels.

    Returns:
        str: Formatted window, or '' when the alert carries no timestamps.
    """
    parts = []
    effective = alert.get('effective', '')
    if effective:
        from_label = translate_or(translator, 'common.alerts.from', 'from')
        parts.append(f"{from_label} {_window_label(effective, translator)}")
    expires = alert.get('expires', '')
    if expires:
        til_label = translate_or(translator, 'common.alerts.til', 'til')
        parts.append(f"{til_label} {_window_label(expires, translator)}")
    return " ".join(parts)


def _window_label(time_str: str, translator: Any = None) -> str:
    """Render a timestamp for the dated window form.

    Args:
        time_str: Timestamp from the alert.
        translator: Optional translator for month names and AM/PM.

    Returns:
        str: Dated form for ISO timestamps, else a truncated compact form.
    """
    parsed = parse_display_time(time_str, translator)
    if parsed is not None:
        return parsed.dated
    return compact_time(time_str, translator)[:25]
