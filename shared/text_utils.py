#!/usr/bin/env python3
"""
Text formatting utilities shared by the bot and web viewer.
"""

from typing import Any, Optional

def abbreviate_location(location: str, max_length: int = 20) -> str:
    """Abbreviate a location string to fit within character limits.

    Args:
        location: The location string to abbreviate.
        max_length: Maximum length for the abbreviated string (default: 20).

    Returns:
        str: Abbreviated location string.
    """
    if not location:
        return location

    # Apply common abbreviations first
    abbreviated = location

    abbreviations = [
        ('Central Business District', 'CBD'),
        ('United States of America', 'USA'),
        ('Business District', 'BD'),
        ('British Columbia', 'BC'),
        ('United States', 'USA'),
        ('United Kingdom', 'UK'),
        ('Washington', 'WA'),
        ('California', 'CA'),
        ('New York', 'NY'),
        ('Texas', 'TX'),
        ('Florida', 'FL'),
        ('Illinois', 'IL'),
        ('Pennsylvania', 'PA'),
        ('Ohio', 'OH'),
        ('Georgia', 'GA'),
        ('North Carolina', 'NC'),
        ('Michigan', 'MI'),
        ('New Jersey', 'NJ'),
        ('Virginia', 'VA'),
        ('Tennessee', 'TN'),
        ('Indiana', 'IN'),
        ('Arizona', 'AZ'),
        ('Massachusetts', 'MA'),
        ('Missouri', 'MO'),
        ('Maryland', 'MD'),
        ('Wisconsin', 'WI'),
        ('Colorado', 'CO'),
        ('Minnesota', 'MN'),
        ('South Carolina', 'SC'),
        ('Alabama', 'AL'),
        ('Louisiana', 'LA'),
        ('Kentucky', 'KY'),
        ('Oregon', 'OR'),
        ('Oklahoma', 'OK'),
        ('Connecticut', 'CT'),
        ('Utah', 'UT'),
        ('Iowa', 'IA'),
        ('Nevada', 'NV'),
        ('Arkansas', 'AR'),
        ('Mississippi', 'MS'),
        ('Kansas', 'KS'),
        ('New Mexico', 'NM'),
        ('Nebraska', 'NE'),
        ('West Virginia', 'WV'),
        ('Idaho', 'ID'),
        ('Hawaii', 'HI'),
        ('New Hampshire', 'NH'),
        ('Maine', 'ME'),
        ('Montana', 'MT'),
        ('Rhode Island', 'RI'),
        ('Delaware', 'DE'),
        ('South Dakota', 'SD'),
        ('North Dakota', 'ND'),
        ('Alaska', 'AK'),
        ('Vermont', 'VT'),
        ('Wyoming', 'WY')
    ]

    # Sort by length (longest first) to ensure longer matches are checked before shorter ones
    # This prevents "United States" from matching before "United States of America"
    abbreviations.sort(key=lambda x: len(x[0]), reverse=True)

    # Apply abbreviations in order
    for full_term, abbrev in abbreviations:
        if full_term in abbreviated:
            abbreviated = abbreviated.replace(full_term, abbrev)

    # If still too long after abbreviations, try to truncate intelligently
    if len(abbreviated) > max_length:
        # Try to keep the most important part (usually the city name)
        parts = abbreviated.split(', ')
        if len(parts) > 1:
            # Keep the first part (usually city) and truncate if needed
            first_part = parts[0]
            abbreviated = first_part if len(first_part) <= max_length else first_part[:max_length - 3] + '...'
        else:
            # Just truncate with ellipsis
            abbreviated = abbreviated[:max_length-3] + '...'

    return abbreviated


def truncate_string(text: str, max_length: int, ellipsis: str = '...') -> str:
    """Truncate a string to a maximum length with ellipsis.

    Args:
        text: The string to truncate.
        max_length: Maximum length including ellipsis.
        ellipsis: String to append when truncating (default: '...').

    Returns:
        str: Truncated string.
    """
    if not text or len(text) <= max_length:
        return text

    return text[:max_length - len(ellipsis)] + ellipsis


def decode_escape_sequences(text: str) -> str:
    """Decode escape sequences in config strings (e.g. Keywords, Scheduled_Messages).

    Processes \\n (newline), \\t (tab), \\r (carriage return), \\\\ (literal backslash).
    Use a single backslash in config: \\n for newline; \\\\n for literal backslash + n.

    Args:
        text: The text string to process.

    Returns:
        str: The text with escape sequences decoded.
    """
    if not text:
        return text
    text = text.replace('\\\\', '\x00')  # Temporary placeholder for backslash
    text = text.replace('\\n', '\n')     # Newline
    text = text.replace('\\t', '\t')    # Tab
    text = text.replace('\\r', '\r')    # Carriage return
    text = text.replace('\x00', '\\')   # Restore backslash
    return text


def format_location_for_display(city: Optional[str], state: Optional[str] = None,
                               country: Optional[str] = None, max_length: int = 20) -> Optional[str]:
    """Format location data for display with intelligent abbreviation.

    Args:
        city: City name (may include neighborhood/district).
        state: State/province name (optional).
        country: Country name (optional).
        max_length: Maximum length for the formatted location (default: 20).

    Returns:
        Optional[str]: Formatted location string or None if no city provided.
    """
    if not city:
        return None

    # Start with city (which may include neighborhood)
    location_parts = [city]

    # Add state if available and different from city
    if state and state not in location_parts:
        location_parts.append(state)

    # Join parts and abbreviate if needed
    full_location = ', '.join(location_parts)
    return abbreviate_location(full_location, max_length)


_ELAPSED_MS_MAX = 5 * 60 * 1000  # 5 minutes in milliseconds


def format_elapsed_display(ts: Any, translator: Any = None) -> str:
    """Format elapsed time from sender timestamp for {elapsed} placeholder.

    Returns "Nms" when valid, or the i18n "Sync Device Clock" when the device
    clock is invalid (e.g. T-Deck before GPS sync: 0, future, or far in the past).

    Args:
        ts: Sender timestamp (int, float, None, or 'unknown').
        translator: Bot translator for i18n; uses "Sync Device Clock" if None.

    Returns:
        str: e.g. "1234ms" or translated "Sync Device Clock".
    """
    def _sync_str() -> str:
        if translator:
            return translator.translate('elapsed.sync_device_clock')
        return "Sync Device Clock"

    if ts is None or ts == 'unknown':
        return _sync_str()
    try:
        ts_f = float(ts)
    except (TypeError, ValueError):
        return _sync_str()
    from datetime import datetime, timezone
    UTC = timezone.utc
    elapsed_ms = (datetime.now(UTC).timestamp() - ts_f) * 1000
    if elapsed_ms < 0 or elapsed_ms > _ELAPSED_MS_MAX:
        return _sync_str()
    return f"{round(elapsed_ms)}ms"


