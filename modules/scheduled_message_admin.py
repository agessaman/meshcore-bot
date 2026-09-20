#!/usr/bin/env python3
"""Read/validate/compose ``[Scheduled_Messages]`` entries for the web viewer.

The bot's schedules live in ``config.ini`` and are applied by
:meth:`modules.scheduler.MessageScheduler.setup_scheduled_messages`, which
``reload_config()`` re-runs — so edits take effect without a restart and there is
no second store to keep in sync.

Everything here validates through the same parsers the scheduler uses
(:mod:`modules.scheduled_message_cron`), so the UI can never accept a schedule the
bot would then reject at startup.
"""

from __future__ import annotations

import configparser
import datetime
from typing import Any

from .scheduled_message_cron import parse_schedule_key, parse_scheduled_message_value

SECTION = "Scheduled_Messages"

# Mirrors MessageScheduler.MIN_COMMAND_PLACEHOLDER_INTERVAL_SECONDS. Imported lazily
# in _min_interval_floor() to avoid importing the scheduler (and APScheduler's
# background machinery) into the web viewer process just for a constant.
_COMMAND_PLACEHOLDER = "{cmd:"


def _min_interval_floor() -> int:
    from .scheduler import MessageScheduler

    return int(MessageScheduler.MIN_COMMAND_PLACEHOLDER_INTERVAL_SECONDS)


def _min_fire_interval_seconds(trigger: Any, tz: Any, samples: int = 12) -> float | None:
    """Smallest gap between consecutive firings, measured the way the scheduler does."""
    from .scheduler import MessageScheduler

    return MessageScheduler._min_fire_interval_seconds(trigger, tz, samples)


def next_run_times(trigger: Any, tz: Any, count: int = 5) -> list[str]:
    """Next *count* firing times as ISO strings, for previewing a schedule."""
    if trigger is None:
        return []
    out: list[str] = []
    previous = trigger.get_next_fire_time(None, datetime.datetime.now(tz))
    while previous is not None and len(out) < count:
        out.append(previous.isoformat())
        previous = trigger.get_next_fire_time(
            previous, previous + datetime.timedelta(microseconds=1)
        )
    return out


def describe_schedule(
    schedule: str, tz: Any, message: str = "", count: int = 5
) -> dict[str, Any]:
    """Validate a schedule key and describe when it would fire.

    Args:
        schedule: The raw key an operator typed, e.g. ``0 6,12,18 * * *`` or ``@daily``.
        tz: Timezone the bot schedules in.
        message: Message body, only needed to apply the ``{cmd:...}`` airtime floor.
        count: How many upcoming runs to return.

    Returns:
        ``valid``, a human ``label``, ``next_runs``, ``interval_seconds`` (tightest gap),
        ``deprecated`` for the legacy HHMM form, and ``error`` when unusable.
    """
    raw = (schedule or "").strip()
    if not raw:
        return {"valid": False, "error": "Schedule is required", "next_runs": []}

    try:
        parsed = parse_schedule_key(raw, tz)
    except Exception as exc:  # noqa: BLE001 - any parser error is just an invalid schedule
        return {"valid": False, "error": f"Could not parse schedule: {exc}", "next_runs": []}

    if parsed.trigger is None:
        return {
            "valid": False,
            "error": (
                "Not a valid schedule. Use 5-field cron (minute hour day-of-month "
                "month day-of-week), or a preset like @daily or @hourly."
            ),
            "next_runs": [],
        }

    interval = _min_fire_interval_seconds(parsed.trigger, tz)
    result: dict[str, Any] = {
        "valid": True,
        "label": parsed.display_label,
        "next_runs": next_run_times(parsed.trigger, tz, count),
        "interval_seconds": interval,
        "deprecated": bool(parsed.is_deprecated_hhmm),
        "error": None,
    }
    if parsed.is_deprecated_hhmm:
        result["warning"] = (
            f"{raw} is the deprecated HHMM form and will stop working in a future "
            "release. Use 5-field cron instead."
        )

    # Same floor the scheduler enforces at startup, applied here so the UI refuses it
    # up front rather than letting it be saved and silently dropped on reload.
    if _COMMAND_PLACEHOLDER in (message or ""):
        floor = _min_interval_floor()
        if interval is not None and interval < floor:
            result["valid"] = False
            result["error"] = (
                f"A message using {{cmd:...}} may not run more often than every "
                f"{floor // 60} minutes; this fires every "
                f"{_humanize_seconds(interval)}. Each run costs airtime."
            )
    return result


def _humanize_seconds(seconds: float) -> str:
    seconds = int(seconds)
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''}"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} seconds"


def compose_value(channel: str, message: str, scope: str | None = None) -> str:
    """Build the config value for an entry, matching parse_scheduled_message_value."""
    channel = (channel or "").strip()
    message = (message or "").strip()
    scope = (scope or "").strip()
    if scope:
        if not scope.startswith("#"):
            scope = f"#{scope}"
        return f"{channel}:{scope}:{message}"
    return f"{channel}:{message}"


def validate_entry(channel: str, message: str, scope: str | None) -> str | None:
    """Return an error string for an unusable entry, or None when it is fine."""
    if not (channel or "").strip():
        return "Channel is required"
    if not (message or "").strip():
        return "Message is required"
    if ":" in (channel or ""):
        return "Channel cannot contain ':'"
    if scope and ":" in scope:
        return "Scope cannot contain ':'"
    # The INI writer rejects these outright; catching them here gives a better message.
    for field, value in (("channel", channel), ("message", message), ("scope", scope or "")):
        if "\n" in value or "\r" in value:
            return f"The {field} cannot contain line breaks (use \\n for a mesh line break)"
    return None


def read_entries(config_path: str, tz: Any) -> list[dict[str, Any]]:
    """Read every ``[Scheduled_Messages]`` entry from disk, described for the UI.

    Reads the file rather than a cached ConfigParser so the list reflects what was
    just written. Malformed rows are returned with an ``error`` instead of being
    hidden, since a row the bot is skipping is exactly what an operator needs to see.
    """
    # Default optionxform (lower-casing) on purpose: the bot reads this section with a
    # plain ConfigParser, so the UI must show the same keys the scheduler registers.
    # ini_writer matches keys case-insensitively, so edits and deletes still line up.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return []

    if not parser.has_section(SECTION):
        return []

    entries: list[dict[str, Any]] = []
    for schedule, raw_value in parser.items(SECTION):
        entry: dict[str, Any] = {
            "schedule": schedule,
            "raw_value": raw_value,
            "channel": "",
            "scope": None,
            "message": "",
        }
        try:
            channel, message, scope = parse_scheduled_message_value(raw_value)
            entry.update(channel=channel, message=message, scope=scope)
        except ValueError as exc:
            # Keep the same shape as a described entry so callers never have to
            # special-case a malformed row to find out it is not running.
            entry["valid"] = False
            entry["error"] = f"Malformed value: {exc}"
            entry["next_runs"] = []
            entries.append(entry)
            continue
        entry.update(describe_schedule(schedule, tz, message=message))
        entries.append(entry)
    return entries
