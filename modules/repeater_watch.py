#!/usr/bin/env python3
"""Watch chosen repeaters and report when one goes quiet (issue #353).

Unattended repeaters on rooftops and masts fail quietly: a solar battery runs
flat, a mains supply drops, firmware hangs after a storm. The bot already hears
the two signs of a working repeater, so it can notice the silence:

* **Adverts.** ``complete_contact_tracking.last_heard`` moves every time the
  repeater's advert reaches the bot.
* **Relaying.** ``mesh_connections`` records each hop-to-hop link seen in a
  packet path, so a repeater forwarding traffic keeps its edges fresh even
  between adverts.

A watched repeater counts as **offline** once neither has been seen for
``offline_after_hours``. One notice goes out when it goes offline and, if
``notify_recovery`` is on, one when it is heard again. Nothing repeats while it
stays down, so an outage costs at most two transmissions per target.

The bot can only vouch for silence it was listening for. The clock a repeater
is judged against starts when the bot started listening (restart, reconnect,
radio recovery), and a check where the bot has heard nothing from anyone
recently is skipped, so a dead antenna on the bot does not report every
repeater on the mesh as down.

Relay evidence is matched by full public key where the graph has one, and by
2- or 3-byte prefix only when no other known repeater shares that prefix. One
byte is never trusted: on a busy mesh many repeaters share each 1-byte prefix,
and another repeater's traffic would keep a dead one looking alive.

The pure helpers take a ``configparser`` and a ``DBManager`` rather than the
bot, because the web viewer runs in a separate process and has no bot object.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import string
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Optional

from modules.models import DM_BODY_LIMIT
from modules.utils import truncate_to_bytes

CONFIG_SECTION = "Repeater_Watch"

STATE_METADATA_KEY = "repeater_watch.state"
EVENTS_METADATA_KEY = "repeater_watch.events"
LISTENING_METADATA_KEY = "repeater_watch.listening_since"
LAST_CHECK_METADATA_KEY = "repeater_watch.last_check_at"

STATUS_UP = "up"
STATUS_OFFLINE = "offline"

EVENT_OFFLINE = "offline"
EVENT_RECOVERED = "recovered"

#: If the bot has heard nothing from any node for this long, the radio side is
#: the likelier fault, so the check is skipped and listening starts over.
DEAF_AFTER_SECONDS = 2 * 3600

#: How long the bot can be down between checks and still keep its listening
#: clock: a quick restart shouldn't blind it for another ``offline_after_hours``.
LISTENING_GAP_GRACE_SECONDS = 3600

#: Events kept for the web viewer's history list.
MAX_EVENTS = 50

#: Watched repeaters; far more than a mesh operator maintains, and it bounds
#: the per-check queries.
MAX_WATCHED = 200

#: Shortest key a watch entry may be: 4 bytes, which is unique in practice.
MIN_KEY_HEX = 8

TEMPLATE_FIELDS = ("name", "prefix", "silent_for", "down_for", "last_heard")

DEFAULT_OFFLINE_MESSAGE = "Repeater offline: {name} ({prefix}), not heard for {silent_for}"
DEFAULT_RECOVERED_MESSAGE = "Repeater back: {name} ({prefix}), was offline {down_for}"

REPEATER_ROLES = ("repeater", "roomserver")

_HEX_RE = re.compile(r"^[0-9a-f]+$")


@dataclass(frozen=True)
class RepeaterWatchSettings:
    """Resolved ``[Repeater_Watch]`` configuration."""

    enabled: bool = False
    watch: tuple[str, ...] = ()
    watch_starred: bool = False
    offline_after_hours: float = 24.0
    check_interval_minutes: float = 15.0
    notify_recovery: bool = True
    channels: tuple[str, ...] = ()
    dm_recipients: tuple[str, ...] = ()
    flood_scope: str = ""
    offline_message: str = DEFAULT_OFFLINE_MESSAGE
    recovered_message: str = DEFAULT_RECOVERED_MESSAGE
    discord_webhook_urls: tuple[str, ...] = ()
    telegram_chat_ids: tuple[str, ...] = ()
    telegram_bot_token: str = ""

    @property
    def offline_after_seconds(self) -> float:
        return self.offline_after_hours * 3600.0

    @property
    def check_interval_seconds(self) -> float:
        return self.check_interval_minutes * 60.0

    def has_targets(self) -> bool:
        return bool(
            self.channels or self.dm_recipients or self.discord_webhook_urls or self.telegram_chat_ids
        )


# -- settings --------------------------------------------------------------


def _get(config: Any, key: str, fallback: str = "") -> str:
    """Read one key raw; message templates are free text that may hold ``%``."""
    try:
        if config is not None and config.has_option(CONFIG_SECTION, key):
            return (config.get(CONFIG_SECTION, key, raw=True) or "").strip()
    except Exception:
        pass
    return fallback


def _get_bool(config: Any, key: str, fallback: bool) -> bool:
    raw = _get(config, key).lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return fallback


def _get_number(config: Any, key: str, fallback: float, minimum: float, maximum: float) -> float:
    """Parse a number, falling back rather than raising or clamping a typo."""
    raw = _get(config, key)
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        return fallback
    if not minimum <= value <= maximum:
        return fallback
    return value


def _split(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def normalize_key(raw: str) -> str:
    """Lower-case a watch entry; return '' unless it is hex of a usable length."""
    key = (raw or "").strip().lower()
    if len(key) < MIN_KEY_HEX or len(key) > 64 or not _HEX_RE.match(key):
        return ""
    return key


def normalize_channel(raw: str) -> str:
    """Keep the channel's own spelling, minus surrounding blanks."""
    return (raw or "").strip()


OFFLINE_HOURS_RANGE = (1.0, 720.0)
CHECK_MINUTES_RANGE = (5.0, 1440.0)


def load_settings(config: Any) -> RepeaterWatchSettings:
    """Read ``[Repeater_Watch]``, falling back to defaults on anything unusable."""
    watch: list[str] = []
    for part in _split(_get(config, "watch")):
        key = normalize_key(part)
        if key and key not in watch:
            watch.append(key)

    channels: list[str] = []
    for part in _split(_get(config, "channels")):
        name = normalize_channel(part)
        if name and name not in channels:
            channels.append(name)

    offline_message = _get(config, "offline_message") or DEFAULT_OFFLINE_MESSAGE
    if template_errors(offline_message):
        offline_message = DEFAULT_OFFLINE_MESSAGE
    recovered_message = _get(config, "recovered_message") or DEFAULT_RECOVERED_MESSAGE
    if template_errors(recovered_message):
        recovered_message = DEFAULT_RECOVERED_MESSAGE

    return RepeaterWatchSettings(
        enabled=_get_bool(config, "enabled", False),
        watch=tuple(watch[:MAX_WATCHED]),
        watch_starred=_get_bool(config, "watch_starred", False),
        offline_after_hours=_get_number(config, "offline_after_hours", 24.0, *OFFLINE_HOURS_RANGE),
        check_interval_minutes=_get_number(config, "check_interval_minutes", 15.0, *CHECK_MINUTES_RANGE),
        notify_recovery=_get_bool(config, "notify_recovery", True),
        channels=tuple(channels),
        dm_recipients=tuple(dict.fromkeys(_split(_get(config, "dm_recipients")))),
        flood_scope=_get(config, "flood_scope"),
        offline_message=offline_message,
        recovered_message=recovered_message,
        discord_webhook_urls=tuple(_split(_get(config, "discord_webhook_urls"))),
        telegram_chat_ids=tuple(_split(_get(config, "telegram_chat_ids"))),
        telegram_bot_token=_get(config, "telegram_bot_token"),
    )


def settings_to_config_values(settings: RepeaterWatchSettings) -> dict[str, str]:
    """Render settings back to INI strings for :mod:`modules.settings_store`."""
    def _num(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else str(value)

    return {
        "enabled": "true" if settings.enabled else "false",
        "watch": ", ".join(settings.watch),
        "watch_starred": "true" if settings.watch_starred else "false",
        "offline_after_hours": _num(settings.offline_after_hours),
        "check_interval_minutes": _num(settings.check_interval_minutes),
        "notify_recovery": "true" if settings.notify_recovery else "false",
        "channels": ", ".join(settings.channels),
        "dm_recipients": ", ".join(settings.dm_recipients),
        "flood_scope": settings.flood_scope,
        "offline_message": settings.offline_message,
        "recovered_message": settings.recovered_message,
        "discord_webhook_urls": ", ".join(settings.discord_webhook_urls),
        "telegram_chat_ids": ", ".join(settings.telegram_chat_ids),
        "telegram_bot_token": settings.telegram_bot_token,
    }


def template_errors(template: str) -> list[str]:
    """Problems that would stop ``template`` rendering; empty when it is usable."""
    errors: list[str] = []
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        return [f"unbalanced braces ({exc})"]
    for _literal, field_name, spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in TEMPLATE_FIELDS:
            errors.append(f"unknown placeholder {{{field_name}}}")
        elif spec or conversion:
            errors.append(f"{{{field_name}}} takes no format spec")
    return errors


# -- formatting ------------------------------------------------------------


def format_duration(seconds: Optional[float]) -> str:
    """Compact duration for a mesh message: ``45m``, ``26h``, ``3d 4h``."""
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    hours = seconds // 3600
    if hours < 48:
        return f"{hours}h"
    days, rem_hours = divmod(hours, 24)
    return f"{days}d {rem_hours}h" if rem_hours else f"{days}d"


def display_prefix(public_key: str, prefix_bytes: int = 1) -> str:
    """The hop prefix operators know a repeater by, in upper case."""
    width = max(1, min(int(prefix_bytes or 1), 3)) * 2
    return public_key[:width].upper()


def render_template(template: str, values: dict[str, str]) -> str:
    """Fill a template, leaving any unknown placeholder visible rather than raising."""
    class _Safe(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        return template.format_map(_Safe(values))
    except (ValueError, IndexError):
        return template


def pack_lines(lines: list[str], limit: int, separator: str = " | ") -> list[str]:
    """Join notice lines into as few messages as fit ``limit`` bytes each."""
    messages: list[str] = []
    current = ""
    for line in lines:
        line = truncate_to_bytes(line, limit)
        candidate = f"{current}{separator}{line}" if current else line
        if len(candidate.encode("utf-8")) <= limit:
            current = candidate
            continue
        if current:
            messages.append(current)
        current = line
    if current:
        messages.append(current)
    return messages


# -- evidence --------------------------------------------------------------


def _to_epoch(raw: Any) -> Optional[float]:
    """Epoch seconds for a stored timestamp; naive values are bot-local time."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.timestamp()
    return dt.astimezone(timezone.utc).timestamp()


@dataclass
class RepeaterActivity:
    """What the bot has heard from one watched repeater."""

    public_key: str
    name: str = ""
    role: str = ""
    known: bool = False
    starred: bool = False
    city: str = ""
    last_advert: Optional[float] = None
    last_relay: Optional[float] = None

    @property
    def last_activity(self) -> Optional[float]:
        times = [t for t in (self.last_advert, self.last_relay) if t is not None]
        return max(times) if times else None

    @property
    def label(self) -> str:
        return self.name or self.public_key[:8].upper()


def _placeholders(count: int) -> str:
    return ",".join("?" * count)


def resolve_watch_list(db_manager: Any, settings: RepeaterWatchSettings) -> list[str]:
    """Full public keys to watch: configured entries plus starred repeaters if asked.

    A configured entry shorter than a full key resolves to the most recently
    heard repeater it prefixes, so a hand-edited ``watch = a1b2c3d4`` works.
    """
    keys: list[str] = []
    roles = list(REPEATER_ROLES)
    for entry in settings.watch:
        if len(entry) == 64:
            keys.append(entry)
            continue
        rows = db_manager.execute_query(
            "SELECT public_key FROM complete_contact_tracking "
            f"WHERE lower(public_key) LIKE ? AND role IN ({_placeholders(len(roles))}) "
            "ORDER BY last_heard DESC LIMIT 1",
            (entry + "%", *roles),
        )
        keys.append((rows[0]["public_key"] or "").lower() if rows else entry)
    if settings.watch_starred:
        rows = db_manager.execute_query(
            "SELECT public_key FROM complete_contact_tracking "
            f"WHERE is_starred = 1 AND role IN ({_placeholders(len(roles))})",
            tuple(roles),
        )
        keys.extend((r["public_key"] or "").lower() for r in rows)
    return list(dict.fromkeys(k for k in keys if k))[:MAX_WATCHED]


def gather_activity(db_manager: Any, public_keys: list[str]) -> dict[str, RepeaterActivity]:
    """Last advert and last relay evidence for each key."""
    activity = {key: RepeaterActivity(public_key=key) for key in public_keys}
    if not public_keys:
        return activity

    full_keys = [k for k in public_keys if len(k) == 64]
    if full_keys:
        rows = db_manager.execute_query(
            "SELECT lower(public_key) AS public_key, name, role, last_heard, is_starred, city "
            f"FROM complete_contact_tracking WHERE lower(public_key) IN ({_placeholders(len(full_keys))})",
            tuple(full_keys),
        )
        for row in rows:
            entry = activity.get(row["public_key"])
            if entry is None:
                continue
            heard = _to_epoch(row.get("last_heard"))
            if entry.last_advert is None or (heard is not None and heard > entry.last_advert):
                entry.last_advert = heard
            entry.known = True
            entry.name = row.get("name") or entry.name
            entry.role = row.get("role") or entry.role
            entry.starred = bool(row.get("is_starred"))
            entry.city = row.get("city") or entry.city

    _apply_relay_evidence(db_manager, activity)
    return activity


def _unique_prefixes(db_manager: Any, keys: list[str]) -> dict[str, str]:
    """Map 2- and 3-byte prefixes of ``keys`` to their key, if no other repeater shares them."""
    wanted = {key[:width]: key for key in keys for width in (4, 6) if len(key) >= width}
    if not wanted:
        return {}
    roles = list(REPEATER_ROLES)
    rows = db_manager.execute_query(
        "SELECT lower(public_key) AS public_key FROM complete_contact_tracking "
        f"WHERE role IN ({_placeholders(len(roles))})",
        tuple(roles),
    )
    owners: dict[str, set[str]] = {prefix: {key} for prefix, key in wanted.items()}
    for row in rows:
        other = row["public_key"] or ""
        for width in (4, 6):
            prefix = other[:width]
            if prefix in owners:
                owners[prefix].add(other)
    return {prefix: next(iter(keys_)) for prefix, keys_ in owners.items() if len(keys_) == 1}


def _apply_relay_evidence(db_manager: Any, activity: dict[str, RepeaterActivity]) -> None:
    keys = [k for k in activity if len(k) == 64]
    if not keys:
        return
    unique = _unique_prefixes(db_manager, keys)
    prefixes = list(unique)
    clauses = [
        f"lower(from_public_key) IN ({_placeholders(len(keys))})",
        f"lower(to_public_key) IN ({_placeholders(len(keys))})",
    ]
    params: list[Any] = [*keys, *keys]
    if prefixes:
        clauses.append(f"lower(from_prefix) IN ({_placeholders(len(prefixes))})")
        clauses.append(f"lower(to_prefix) IN ({_placeholders(len(prefixes))})")
        params.extend(prefixes)
        params.extend(prefixes)
    rows = db_manager.execute_query(
        "SELECT lower(from_prefix) AS from_prefix, lower(to_prefix) AS to_prefix, "
        "lower(from_public_key) AS from_public_key, lower(to_public_key) AS to_public_key, last_seen "
        f"FROM mesh_connections WHERE {' OR '.join(clauses)}",
        tuple(params),
    )
    for row in rows:
        seen = _to_epoch(row.get("last_seen"))
        if seen is None:
            continue
        for side in ("from", "to"):
            owner = _edge_owner(row.get(f"{side}_public_key"), row.get(f"{side}_prefix"), activity, unique)
            if owner is None:
                continue
            entry = activity[owner]
            if entry.last_relay is None or seen > entry.last_relay:
                entry.last_relay = seen


def _edge_owner(
    edge_key: Optional[str],
    edge_prefix: Optional[str],
    activity: dict[str, RepeaterActivity],
    unique: dict[str, str],
) -> Optional[str]:
    """Which watched repeater one end of an edge belongs to, if any.

    An endpoint recorded against a different public key is never credited by
    prefix: the graph already knows it was someone else.
    """
    if edge_key:
        return edge_key if edge_key in activity else None
    if edge_prefix and edge_prefix in unique:
        return unique[edge_prefix]
    return None


def latest_heard_anything(db_manager: Any) -> Optional[float]:
    """When the bot last heard any node at all, from adverts or packet paths."""
    latest: Optional[float] = None
    for sql in (
        "SELECT MAX(last_heard) AS t FROM complete_contact_tracking",
        "SELECT MAX(last_seen) AS t FROM mesh_connections",
    ):
        try:
            rows = db_manager.execute_query(sql)
        except Exception:
            continue
        value = _to_epoch(rows[0].get("t")) if rows else None
        if value is not None and (latest is None or value > latest):
            latest = value
    return latest


# -- state -----------------------------------------------------------------


@dataclass
class WatchState:
    """Per-repeater status, persisted so a restart does not repeat a notice."""

    status: str = STATUS_UP
    changed_at: Optional[float] = None
    last_activity: Optional[float] = None

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "changed_at": self.changed_at, "last_activity": self.last_activity}

    @classmethod
    def from_json(cls, raw: Any) -> WatchState:
        if not isinstance(raw, dict):
            return cls()
        status = STATUS_OFFLINE if raw.get("status") == STATUS_OFFLINE else STATUS_UP
        return cls(
            status=status,
            changed_at=_num_or_none(raw.get("changed_at")),
            last_activity=_num_or_none(raw.get("last_activity")),
        )


def _num_or_none(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def load_state(db_manager: Any) -> dict[str, WatchState]:
    raw = _read_json(db_manager, STATE_METADATA_KEY, {})
    if not isinstance(raw, dict):
        return {}
    return {str(k): WatchState.from_json(v) for k, v in raw.items()}


def save_state(db_manager: Any, state: dict[str, WatchState]) -> None:
    db_manager.set_metadata(
        STATE_METADATA_KEY, json.dumps({k: v.to_json() for k, v in state.items()})
    )


def recent_events(db_manager: Any, limit: int = MAX_EVENTS) -> list[dict[str, Any]]:
    raw = _read_json(db_manager, EVENTS_METADATA_KEY, [])
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict)][:limit]


def _append_events(db_manager: Any, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    kept = (list(reversed(events)) + recent_events(db_manager))[:MAX_EVENTS]
    db_manager.set_metadata(EVENTS_METADATA_KEY, json.dumps(kept))


def _read_json(db_manager: Any, key: str, default: Any) -> Any:
    try:
        raw = db_manager.get_metadata(key)
    except Exception:
        return default
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Transition:
    """A repeater changing status in one check."""

    public_key: str
    event: str
    name: str
    silent_for: Optional[float] = None
    down_for: Optional[float] = None
    last_heard: Optional[float] = None


def evaluate(
    now: float,
    activity: dict[str, RepeaterActivity],
    state: dict[str, WatchState],
    listening_since: float,
    offline_after_seconds: float,
) -> tuple[list[Transition], dict[str, WatchState]]:
    """Decide which watched repeaters went offline or came back since the last check.

    Pure, so the web viewer and the tests can run it without a radio. A
    repeater is never judged against silence from before ``listening_since``.
    """
    transitions: list[Transition] = []
    new_state: dict[str, WatchState] = {}
    for key, entry in activity.items():
        prev = state.get(key, WatchState())
        last = entry.last_activity
        if prev.status == STATUS_OFFLINE:
            if last is not None and (prev.changed_at is None or last > prev.changed_at):
                base = prev.last_activity if prev.last_activity is not None else prev.changed_at
                transitions.append(Transition(
                    public_key=key, event=EVENT_RECOVERED, name=entry.label,
                    down_for=(last - base) if base is not None else None, last_heard=last,
                ))
                new_state[key] = WatchState(STATUS_UP, now, last)
            else:
                new_state[key] = replace(prev)
            continue

        reference = max(last or 0.0, listening_since)
        if now - reference >= offline_after_seconds:
            transitions.append(Transition(
                public_key=key, event=EVENT_OFFLINE, name=entry.label,
                silent_for=(now - last) if last is not None else None, last_heard=last,
            ))
            new_state[key] = WatchState(STATUS_OFFLINE, now, last)
        else:
            new_state[key] = WatchState(STATUS_UP, prev.changed_at, last)
    return transitions, new_state


def notice_lines(
    transitions: list[Transition],
    settings: RepeaterWatchSettings,
    now: float,
    listening_since: float,
    prefix_bytes: int = 1,
) -> list[str]:
    """One rendered notice per transition, in a stable order (offline first)."""
    lines: list[str] = []
    ordered = sorted(transitions, key=lambda t: (t.event != EVENT_OFFLINE, t.name.lower()))
    for t in ordered:
        if t.event == EVENT_RECOVERED and not settings.notify_recovery:
            continue
        if t.silent_for is not None:
            silent = format_duration(t.silent_for)
        else:
            # Never heard since the bot started listening.
            silent = format_duration(now - listening_since) + "+"
        values = {
            "name": t.name,
            "prefix": display_prefix(t.public_key, prefix_bytes),
            "silent_for": silent,
            "down_for": format_duration(t.down_for),
            "last_heard": _format_clock(t.last_heard),
        }
        template = settings.offline_message if t.event == EVENT_OFFLINE else settings.recovered_message
        lines.append(render_template(template, values))
    return lines


def _format_clock(epoch: Optional[float]) -> str:
    if epoch is None:
        return "never"
    dt = datetime.fromtimestamp(epoch)
    return f"{dt:%b} {dt.day} {dt:%H:%M}"


# -- status view (web viewer) -------------------------------------------------


def status_view(db_manager: Any, config: Any, now: Optional[float] = None) -> dict[str, Any]:
    """Everything the settings page shows about the watched repeaters, read-only."""
    now = time.time() if now is None else now
    settings = load_settings(config)
    keys = resolve_watch_list(db_manager, settings)
    activity = gather_activity(db_manager, keys)
    state = load_state(db_manager)
    listening = _num_or_none(_read_json(db_manager, LISTENING_METADATA_KEY, None))
    last_check = _num_or_none(_read_json(db_manager, LAST_CHECK_METADATA_KEY, None))
    configured = set(settings.watch)
    rows: list[dict[str, Any]] = []
    for key in keys:
        entry = activity[key]
        st = state.get(key, WatchState())
        last = entry.last_activity
        reference = max(last or 0.0, listening or 0.0) if listening else last
        quiet_for = (now - reference) if reference else None
        rows.append({
            "public_key": key,
            "name": entry.name,
            "label": entry.label,
            "role": entry.role,
            "city": entry.city,
            "known": entry.known,
            "starred": entry.starred,
            "configured": key in configured or any(key.startswith(c) for c in configured),
            "last_advert": entry.last_advert,
            "last_relay": entry.last_relay,
            "last_activity": last,
            "status": st.status,
            "status_since": st.changed_at,
            "quiet_for": quiet_for,
        })
    rows.sort(key=lambda r: (r["status"] != STATUS_OFFLINE, -float(r["last_activity"] or 0)))
    return {
        "now": now,
        "rows": rows,
        "listening_since": listening,
        "last_check_at": last_check,
        "latest_heard": latest_heard_anything(db_manager),
    }


def candidate_repeaters(db_manager: Any, limit: int = 500) -> list[dict[str, Any]]:
    """Known repeaters and room servers, most recently heard first, for the picker."""
    roles = list(REPEATER_ROLES)
    rows = db_manager.execute_query(
        "SELECT lower(public_key) AS public_key, name, role, city, state, last_heard, is_starred "
        f"FROM complete_contact_tracking WHERE role IN ({_placeholders(len(roles))}) "
        "ORDER BY last_heard DESC LIMIT ?",
        (*roles, int(limit)),
    )
    seen: set[str] = set()
    out = []
    for row in rows:
        key = row.get("public_key") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({
            "public_key": key,
            "name": row.get("name") or "",
            "role": row.get("role") or "",
            "place": ", ".join(p for p in (row.get("city"), row.get("state")) if p),
            "last_heard": _to_epoch(row.get("last_heard")),
            "starred": bool(row.get("is_starred")),
        })
    return out


# -- monitor (bot process) -----------------------------------------------------


@dataclass
class _CheckOutcome:
    skipped: str = ""
    transitions: list[Transition] = field(default_factory=list)


class RepeaterWatchMonitor:
    """Runs the periodic check inside the bot and sends the notices.

    Settings are read from ``bot.config`` on every tick, so a hot config reload
    (including turning the watch on or off) applies without a restart.
    """

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.logger = bot.logger
        self._task: Optional[asyncio.Task[Any]] = None
        self._next_check_at = 0.0
        self._listening_since: Optional[float] = None
        self._listening_loaded = False

    @property
    def settings(self) -> RepeaterWatchSettings:
        return load_settings(getattr(self.bot, "config", None))

    def tick(self, now: float) -> None:
        """Start a check if one is due. Called from the bot's main loop."""
        settings = self.settings
        if not settings.enabled or now < self._next_check_at:
            return
        if self._task is not None and not self._task.done():
            return
        self._next_check_at = now + settings.check_interval_seconds
        self._task = asyncio.create_task(self._run_check(settings))

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_check(self, settings: RepeaterWatchSettings) -> None:
        try:
            await self.check(settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error("Repeater watch check failed: %s", exc)

    def _radio_listening(self) -> bool:
        bot = self.bot
        if not getattr(bot, "connected", False):
            return False
        if getattr(bot, "is_radio_zombie", False) or getattr(bot, "is_radio_offline", False):
            return False
        return True

    def _load_listening(self, db: Any, now: float, settings: RepeaterWatchSettings) -> None:
        """Keep the persisted listening clock across a quick restart."""
        self._listening_loaded = True
        since = _num_or_none(_read_json(db, LISTENING_METADATA_KEY, None))
        last_check = _num_or_none(_read_json(db, LAST_CHECK_METADATA_KEY, None))
        allowed_gap = settings.check_interval_seconds + LISTENING_GAP_GRACE_SECONDS
        if since is not None and last_check is not None and now - last_check <= allowed_gap:
            self._listening_since = since

    def _set_listening(self, db: Any, value: Optional[float]) -> None:
        self._listening_since = value
        db.set_metadata(LISTENING_METADATA_KEY, json.dumps(value))

    async def check(self, settings: RepeaterWatchSettings, now: Optional[float] = None) -> _CheckOutcome:
        """One pass: gather evidence, decide transitions, persist, notify."""
        now = time.time() if now is None else now
        db = self.bot.db_manager
        if not self._listening_loaded:
            await asyncio.to_thread(self._load_listening, db, now, settings)

        if not self._radio_listening():
            if self._listening_since is not None:
                await asyncio.to_thread(self._set_listening, db, None)
            return _CheckOutcome(skipped="radio not listening")

        latest = await asyncio.to_thread(latest_heard_anything, db)
        if latest is None or now - latest > DEAF_AFTER_SECONDS:
            if self._listening_since is not None:
                self.logger.warning(
                    "Repeater watch paused: the bot has heard no node for over %s",
                    format_duration(DEAF_AFTER_SECONDS),
                )
                await asyncio.to_thread(self._set_listening, db, None)
            return _CheckOutcome(skipped="nothing heard recently")

        if self._listening_since is None:
            await asyncio.to_thread(self._set_listening, db, now)
        listening_since = self._listening_since or now

        def _gather() -> tuple[dict[str, RepeaterActivity], dict[str, WatchState]]:
            keys = resolve_watch_list(db, settings)
            return gather_activity(db, keys), load_state(db)

        activity, state = await asyncio.to_thread(_gather)
        transitions, new_state = evaluate(
            now, activity, state, listening_since, settings.offline_after_seconds
        )

        events = []
        for t in transitions:
            events.append({
                "at": now,
                "public_key": t.public_key,
                "name": t.name,
                "event": t.event,
                "silent_for": t.silent_for,
                "down_for": t.down_for,
            })
            if t.event == EVENT_OFFLINE:
                self.logger.warning("Repeater watch: %s went offline", t.name)
            else:
                self.logger.info("Repeater watch: %s is back", t.name)

        def _persist() -> None:
            save_state(db, new_state)
            _append_events(db, events)
            db.set_metadata(LAST_CHECK_METADATA_KEY, json.dumps(now))

        await asyncio.to_thread(_persist)

        if transitions:
            await self._notify(settings, transitions, now, listening_since)
        return _CheckOutcome(transitions=transitions)

    def _prefix_bytes(self) -> int:
        try:
            return int(self.bot.config.get("Bot", "prefix_bytes", fallback="1") or 1)
        except Exception:
            return 1

    async def _notify(
        self,
        settings: RepeaterWatchSettings,
        transitions: list[Transition],
        now: float,
        listening_since: float,
    ) -> None:
        lines = notice_lines(transitions, settings, now, listening_since, self._prefix_bytes())
        if not lines:
            return
        if not settings.has_targets():
            self.logger.info("Repeater watch has no notification targets; logged only")
            return

        cmd = getattr(self.bot, "command_manager", None)
        scope = settings.flood_scope or None
        if cmd is not None:
            for channel in settings.channels:
                try:
                    limit = int(cmd.channel_body_budget(channel=channel, scope=scope))
                except Exception:
                    limit = 100
                try:
                    await cmd.send_channel_messages_chunked(
                        channel, pack_lines(lines, limit), scope=scope
                    )
                except Exception as exc:
                    self.logger.warning("Repeater watch: channel %s send failed: %s", channel, exc)
            for recipient in settings.dm_recipients:
                for message in pack_lines(lines, DM_BODY_LIMIT):
                    try:
                        ok = await cmd.send_dm(recipient, message, skip_user_rate_limit=True)
                    except Exception as exc:
                        ok = False
                        self.logger.warning("Repeater watch: DM to %s failed: %s", recipient, exc)
                    if not ok:
                        self.logger.warning("Repeater watch: DM to %s was not sent", recipient)
                        break

        await self._notify_external(settings, "\n".join(lines))

    async def _notify_external(self, settings: RepeaterWatchSettings, text: str) -> None:
        from modules import bridge_outbound

        urls = [u for u in settings.discord_webhook_urls if bridge_outbound.is_valid_discord_webhook_url(u)]
        token = settings.telegram_bot_token or (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            try:
                token = (self.bot.config.get("TelegramBridge", "api_token", fallback="") or "").strip()
            except Exception:
                token = ""
        chats = list(settings.telegram_chat_ids) if token else []
        if settings.telegram_chat_ids and not token:
            self.logger.warning("Repeater watch: telegram_chat_ids set but no Telegram bot token")
        try:
            bot_name = (self.bot.config.get("Bot", "bot_name", fallback="") or "").strip()
        except Exception:
            bot_name = ""
        username = f"{bot_name or 'MeshCore Bot'} repeater watch"
        for url in urls:
            try:
                await bridge_outbound.post_discord_webhook(
                    url, text, username=username, session=None, logger=self.logger
                )
            except Exception as exc:
                self.logger.warning("Repeater watch: Discord post failed: %s", exc)
        for chat in chats:
            try:
                await bridge_outbound.post_telegram_message(
                    token, chat, text, session=None, logger=self.logger
                )
            except Exception as exc:
                self.logger.warning("Repeater watch: Telegram post failed: %s", exc)
