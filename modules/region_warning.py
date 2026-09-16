#!/usr/bin/env python3
"""Regional flood-scope observation and the optional "set a region code" warning.

MeshCore puts a channel message on the air in one of two ways. An ordinary
``FLOOD`` is rebroadcast by every repeater that hears it, anywhere on the mesh.
A ``TC_FLOOD`` carries a transport code derived from a region key — the "region
code" — and only repeaters holding that key pass it on. A client with no region
configured therefore floods the entire mesh with every message it sends, which
is the traffic problem this module is about (issue #279).

Two things live here:

* **Observation.** Every channel message the bot hears is classified as
  ``scoped``, ``global`` or ``unknown`` and tallied into ``region_scope_daily``.
  This costs one upsert per message and no airtime, and it is what lets an
  operator see how much unscoped traffic their mesh actually carries *before*
  deciding whether telling anyone about it is worth the airtime.

* **Warning.** When explicitly enabled, a sender whose messages are confirmed
  unscoped can be sent a short note asking them to set a region code. This
  spends airtime automatically, so it is off by default, starts in dry-run, and
  is fenced by a per-sender cooldown, a mesh-wide cooldown and a daily cap.

The classification only ever warns on *positive* evidence of a global flood.
Absence of evidence classifies as ``unknown`` and is never warned about — see
``MessageHandler._classify_channel_flood_scope``, which produces the verdict
this module consumes.

The pure ``load_settings`` / summary helpers take a ``configparser`` and a
``DBManager`` rather than the bot, because the web viewer runs in a separate
process and has no bot object.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from modules.models import DM_BODY_LIMIT as _DM_BODY_LIMIT
from modules.models import channel_body_limit

CONFIG_SECTION = "Region_Warnings"

# Verdicts produced by MessageHandler._classify_channel_flood_scope.
VERDICT_SCOPED = "scoped"
VERDICT_GLOBAL = "global"
VERDICT_UNKNOWN = "unknown"
VERDICTS = (VERDICT_SCOPED, VERDICT_GLOBAL, VERDICT_UNKNOWN)

_VERDICT_COLUMN = {
    VERDICT_SCOPED: "scoped_count",
    VERDICT_GLOBAL: "global_count",
    VERDICT_UNKNOWN: "unknown_count",
}

DELIVERY_DM = "dm"
DELIVERY_CHANNEL = "channel"

ACTION_SENT = "sent"
ACTION_DRY_RUN = "dry_run"
ACTION_FAILED = "failed"

# bot_metadata key holding today's count of warnings dropped for want of a
# contact. A counter rather than event rows: this fires on every unscoped
# message from an unknown name, ahead of the cooldowns that would rate-limit
# rows, so logging each one would bury the decisions worth reading.
WITHHELD_METADATA_KEY = "region_warning.withheld_no_contact"

DEFAULT_MESSAGE = (
    "Heads up: your messages have no region code, so they flood the whole mesh. "
    "Setting one in your MeshCore app keeps things quiet. Thanks!"
)

# Re-exported so the web viewer (which has no bot object) can size the preview
# against the same budget the command layer enforces.
DM_BODY_LIMIT = _DM_BODY_LIMIT


@dataclass(frozen=True)
class RegionWarningSettings:
    """Resolved ``[Region_Warnings]`` configuration."""

    enabled: bool = False
    dry_run: bool = True
    delivery: str = DELIVERY_DM
    channels: tuple[str, ...] = ()
    message: str = DEFAULT_MESSAGE
    min_unscoped_messages: int = 3
    per_sender_cooldown_hours: float = 168.0
    mesh_cooldown_minutes: float = 30.0
    max_warnings_per_day: int = 6
    track_traffic: bool = True

    def monitors_channel(self, channel: Optional[str]) -> bool:
        """Whether ``channel`` is in the allowlist (an empty allowlist means all)."""
        if not self.channels:
            return True
        return normalize_channel(channel) in self.channels


def normalize_channel(channel: Optional[str]) -> str:
    """Case-fold a channel name and drop a leading ``#`` so config matches the wire."""
    return (channel or "").strip().lstrip("#").lower()


def _get(config: Any, key: str, fallback: str = "") -> str:
    """Read one key, raw.

    ``raw=True`` because configparser's default interpolation raises on a bare
    ``%`` in a value, and ``message`` is free text an operator types. Without it
    a message like "100% of the mesh" would throw here, get swallowed, and the
    bot would silently transmit the default wording instead of theirs. The save
    endpoint rejects ``%`` outright so a hand-edited config is the only way to
    get one, but this still has to read it rather than fall back.
    """
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


def _get_number(config: Any, key: str, fallback: float, *, minimum: float = 0.0) -> float:
    """Parse a numeric setting, falling back rather than raising.

    A value below ``minimum`` falls back to the default instead of clamping: on
    ``max_warnings_per_day`` zero is the "unlimited" sentinel, so clamping a
    typo of ``-1`` would quietly remove the daily cap.
    """
    raw = _get(config, key)
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        return fallback
    if value < minimum:
        return fallback
    return value


def load_settings(config: Any) -> RegionWarningSettings:
    """Read ``[Region_Warnings]`` into a settings object, falling back to defaults.

    Unparseable values fall back rather than raising: this runs on every config
    reload in the message path, and a typo should not stop the bot handling mail.
    """
    delivery = _get(config, "delivery", DELIVERY_DM).lower()
    if delivery not in (DELIVERY_DM, DELIVERY_CHANNEL):
        delivery = DELIVERY_DM

    channels = tuple(
        normalize_channel(part)
        for part in _get(config, "channels").split(",")
        if normalize_channel(part)
    )

    message = _get(config, "message") or DEFAULT_MESSAGE

    return RegionWarningSettings(
        enabled=_get_bool(config, "enabled", False),
        dry_run=_get_bool(config, "dry_run", True),
        delivery=delivery,
        channels=channels,
        message=message,
        min_unscoped_messages=int(_get_number(config, "min_unscoped_messages", 3, minimum=1)),
        per_sender_cooldown_hours=_get_number(config, "per_sender_cooldown_hours", 168.0),
        mesh_cooldown_minutes=_get_number(config, "mesh_cooldown_minutes", 30.0),
        max_warnings_per_day=int(_get_number(config, "max_warnings_per_day", 6)),
        track_traffic=_get_bool(config, "track_traffic", True),
    )


def settings_to_config_values(settings: RegionWarningSettings) -> dict[str, str]:
    """Render settings back to INI strings for :mod:`modules.settings_store`."""
    def _num(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else str(value)

    return {
        "enabled": "true" if settings.enabled else "false",
        "dry_run": "true" if settings.dry_run else "false",
        "delivery": settings.delivery,
        "channels": ", ".join(settings.channels),
        "message": settings.message,
        "min_unscoped_messages": str(settings.min_unscoped_messages),
        "per_sender_cooldown_hours": _num(settings.per_sender_cooldown_hours),
        "mesh_cooldown_minutes": _num(settings.mesh_cooldown_minutes),
        "max_warnings_per_day": str(settings.max_warnings_per_day),
        "track_traffic": "true" if settings.track_traffic else "false",
    }


def render_message(template: str, sender: Optional[str], channel: Optional[str]) -> str:
    """Substitute ``{sender}`` / ``{channel}``, leaving unknown braces untouched."""
    text = template or DEFAULT_MESSAGE
    replacements = {
        "{sender}": sender or "",
        "{channel}": channel or "",
    }
    for token, value in replacements.items():
        text = text.replace(token, value)
    return text.strip()


def truncate_to_bytes(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` UTF-8 bytes without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Read helpers — shared with the web viewer, which has no bot object
# ---------------------------------------------------------------------------


def local_now(config: Any, logger: Any = None) -> datetime:
    """Now in the configured ``[Bot] timezone``, as a naive datetime.

    Everything this module stores and compares — tallies, the daily cap, both
    cooldowns — goes through here, so the whole feature agrees on which day it
    is even when ``[Bot] timezone`` differs from the host's. Naive, because the
    values are written to SQLite and read back by ``date(created_at)``, which
    has no notion of offsets.
    """
    try:
        from modules.utils import get_config_timezone

        tz, _name = get_config_timezone(config, logger)
        return datetime.now(tz).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def _local_today(config: Any, logger: Any = None) -> date:
    """Today's date in the configured ``[Bot] timezone``."""
    return local_now(config, logger).date()


def traffic_summary(
    db_manager: Any,
    config: Any = None,
    days: int = 14,
    logger: Any = None,
) -> dict[str, Any]:
    """Per-channel scoped/global/unknown tallies over the last ``days`` local days."""
    days = max(1, int(days))
    since = (_local_today(config, logger) - timedelta(days=days - 1)).isoformat()
    try:
        # Grouped case-insensitively and without a leading '#': the tally stores
        # the channel name as the radio reported it, and a rename between
        # "general" and "#General" would otherwise split one channel in two.
        rows = db_manager.execute_query(
            "SELECT TRIM(MIN(channel)) AS channel, "
            "SUM(scoped_count) AS scoped, "
            "SUM(global_count) AS global_, "
            "SUM(unknown_count) AS unknown "
            "FROM region_scope_daily WHERE date >= ? "
            "GROUP BY LOWER(LTRIM(TRIM(channel), '#')) "
            "ORDER BY (SUM(global_count) + SUM(scoped_count) + SUM(unknown_count)) DESC",
            (since,),
        )
    except Exception:
        rows = []

    channels: list[dict[str, Any]] = []
    totals: dict[str, Any] = {"scoped": 0, "global": 0, "unknown": 0}
    for row in rows:
        scoped = int(row.get("scoped") or 0)
        globally = int(row.get("global_") or 0)
        unknown = int(row.get("unknown") or 0)
        total = scoped + globally + unknown
        if total <= 0:
            continue
        channels.append({
            "channel": row.get("channel") or "",
            "scoped": scoped,
            "global": globally,
            "unknown": unknown,
            "total": total,
            # Share of *classified* traffic that was unscoped. Unknown messages
            # are excluded from the denominator rather than counted as scoped,
            # so a mesh the bot cannot classify reads as "no data", not "clean".
            "unscoped_pct": round(100.0 * globally / (scoped + globally), 1) if (scoped + globally) else None,
        })
        totals["scoped"] += scoped
        totals["global"] += globally
        totals["unknown"] += unknown

    classified = totals["scoped"] + totals["global"]
    totals["total"] = classified + totals["unknown"]
    totals["unscoped_pct"] = round(100.0 * totals["global"] / classified, 1) if classified else None
    return {"days": days, "since": since, "channels": channels, "totals": totals}


def daily_series(
    db_manager: Any,
    config: Any = None,
    days: int = 14,
    logger: Any = None,
) -> list[dict[str, Any]]:
    """One row per local date over the window, with zero-filled gaps."""
    days = max(1, int(days))
    today = _local_today(config, logger)
    since = (today - timedelta(days=days - 1)).isoformat()
    try:
        rows = db_manager.execute_query(
            "SELECT date, SUM(scoped_count) AS scoped, SUM(global_count) AS global_, "
            "SUM(unknown_count) AS unknown FROM region_scope_daily "
            "WHERE date >= ? GROUP BY date",
            (since,),
        )
    except Exception:
        rows = []
    by_date = {
        row.get("date"): (
            int(row.get("scoped") or 0),
            int(row.get("global_") or 0),
            int(row.get("unknown") or 0),
        )
        for row in rows
    }
    series = []
    for offset in range(days - 1, -1, -1):
        key = (today - timedelta(days=offset)).isoformat()
        scoped, globally, unknown = by_date.get(key, (0, 0, 0))
        series.append({
            "date": key,
            "scoped": scoped,
            "global": globally,
            "unknown": unknown,
        })
    return series


def read_withheld(db_manager: Any, today: str) -> int:
    """Today's count of warnings dropped because no contact matched the sender.

    Returns 0 for any other date: the counter is a snapshot of one local day,
    not a running total.
    """
    try:
        raw = db_manager.get_metadata(WITHHELD_METADATA_KEY)
        if not raw:
            return 0
        stored = json.loads(raw)
        if stored.get("date") != today:
            return 0
        return max(0, int(stored.get("count") or 0))
    except Exception:
        return 0


def recent_events(db_manager: Any, limit: int = 50) -> list[dict[str, Any]]:
    """Most recent warning decisions, newest first."""
    limit = max(1, min(int(limit), 500))
    try:
        return db_manager.execute_query(
            "SELECT id, created_at, sender_id, sender_pubkey, channel, delivery, action, detail "
            "FROM region_warning_events ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
    except Exception:
        return []


def warning_budget(
    db_manager: Any,
    settings: RegionWarningSettings,
    config: Any = None,
    logger: Any = None,
) -> dict[str, Any]:
    """How much of the daily cap is spent and when the mesh cooldown lifts.

    The cap counts *attempts*, failures included. Its job is to bound how much
    unprompted activity this feature can produce in a day, and a send that
    reported failure may still have put something on the air before it did.
    """
    today = _local_today(config, logger).isoformat()
    used = 0
    delivered = 0
    previewed = 0
    last_at: Optional[str] = None
    try:
        rows = db_manager.execute_query(
            "SELECT COUNT(*) AS used, "
            "SUM(CASE WHEN action = ? THEN 1 ELSE 0 END) AS delivered, "
            "SUM(CASE WHEN action = ? THEN 1 ELSE 0 END) AS previewed "
            "FROM region_warning_events WHERE date(created_at) = ?",
            (ACTION_SENT, ACTION_DRY_RUN, today),
        )
        if rows:
            used = int(rows[0].get("used") or 0)
            delivered = int(rows[0].get("delivered") or 0)
            previewed = int(rows[0].get("previewed") or 0)
        rows = db_manager.execute_query(
            "SELECT MAX(created_at) AS last_at FROM region_warning_events WHERE action IN (?, ?)",
            (ACTION_SENT, ACTION_DRY_RUN),
        )
        if rows:
            last_at = rows[0].get("last_at")
    except Exception:
        pass

    cap = settings.max_warnings_per_day
    return {
        "date": today,
        # used_today is attempts (what the cap spends). The rest break that down,
        # because reporting attempts alone put a count of four beside "last
        # warning: none yet", and folding dry runs into "sent" would report a
        # morning's preview as afternoon transmissions.
        "used_today": used,
        "delivered_today": delivered,
        "previewed_today": previewed,
        "failed_today": max(0, used - delivered - previewed),
        "withheld_today": read_withheld(db_manager, today),
        "cap": cap,
        "remaining": None if cap <= 0 else max(0, cap - used),
        "unlimited": cap <= 0,
        "last_warning_at": last_at,
    }


# ---------------------------------------------------------------------------
# Monitor — bot-side
# ---------------------------------------------------------------------------


@dataclass
class _SenderState:
    """In-memory per-sender counter, rebuilt after a restart."""

    unscoped_seen: int = 0
    counted_at: float = field(default_factory=time.monotonic)


class RegionWarningMonitor:
    """Tally flood scopes and, when enabled, warn senders who never set one.

    **Sender identity is a display name, not an identity.** MeshCore's
    ``CHANNEL_MSG_RECV`` carries no public key: the sender is the ``"Name: "``
    prefix of the decrypted text, which anyone holding the channel key can set
    to anything. Nothing here can authenticate it. What it can do is refuse to
    act on a name with no node behind it, so DM delivery requires a contact the
    radio already knows, and both the per-sender cooldown and the daily cap
    bound how much one forged name can cost.
    """

    #: How long a sender's unscoped-message run survives without new traffic.
    #: A sender who floods three times a year should not accumulate their way
    #: to a warning, so the run resets after a quiet spell.
    RUN_TTL_SECONDS = 24 * 3600

    #: Ceiling on tracked senders. A busy mesh sees thousands of names over a
    #: long uptime, and an unbounded dict here would be a slow leak in the
    #: channel-message path. Expired runs are swept first; if that is not
    #: enough, the least recently seen are dropped, which only delays a
    #: warning for a sender who had stopped talking anyway.
    MAX_TRACKED_SENDERS = 2000

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.logger = bot.logger
        self.settings = load_settings(getattr(bot, "config", None))
        self._senders: dict[str, _SenderState] = {}
        # Written-through from the DB on first use so a restart cannot reset the
        # mesh-wide cooldown and let a burst of warnings out.
        self._last_warning_monotonic: Optional[float] = None
        self._last_warning_wall: Optional[datetime] = None
        self._budget_loaded = False
        self._withheld_date: str = ""
        self._withheld_count = 0
        if self.settings.enabled:
            self.logger.info(
                "Region warnings enabled (%s, delivery=%s, cap=%s/day)",
                "dry run" if self.settings.dry_run else "live",
                self.settings.delivery,
                self.settings.max_warnings_per_day or "unlimited",
            )

    # -- configuration -----------------------------------------------------

    def reload_config(self) -> None:
        """Re-read ``[Region_Warnings]`` after a hot config reload."""
        self.settings = load_settings(getattr(self.bot, "config", None))

    # -- observation -------------------------------------------------------

    async def observe(
        self,
        *,
        verdict: str,
        sender_id: Optional[str],
        sender_pubkey: Optional[str],
        channel: Optional[str],
    ) -> None:
        """Record one channel message's scope verdict and warn if it earns one.

        Never raises: this sits on the channel-message path and a bookkeeping
        failure must not cost the message.
        """
        try:
            if verdict not in _VERDICT_COLUMN:
                return
            if self.settings.track_traffic:
                self._record_verdict(verdict, channel)
            if verdict != VERDICT_GLOBAL:
                if verdict == VERDICT_SCOPED and sender_id:
                    # A scoped message proves the sender has a region set now,
                    # so an earlier unscoped run is stale evidence.
                    self._senders.pop(sender_id, None)
                return
            await self._consider_warning(sender_id, sender_pubkey, channel)
        except Exception:
            self.logger.exception("Region warning monitor failed on a channel message")

    def _record_verdict(self, verdict: str, channel: Optional[str]) -> None:
        column = _VERDICT_COLUMN[verdict]
        today = _local_today(getattr(self.bot, "config", None), self.logger).isoformat()
        name = (channel or "").strip() or "(unknown)"
        db_manager = getattr(self.bot, "db_manager", None)
        if not db_manager:
            return
        with db_manager.connection() as conn:
            # Column name comes from the _VERDICT_COLUMN table, never from input.
            conn.execute(
                f"INSERT INTO region_scope_daily (date, channel, {column}) VALUES (?, ?, 1) "
                f"ON CONFLICT(date, channel) DO UPDATE SET {column} = {column} + 1",
                (today, name),
            )
            conn.commit()

    # -- warning decision --------------------------------------------------

    async def _consider_warning(
        self,
        sender_id: Optional[str],
        sender_pubkey: Optional[str],
        channel: Optional[str],
    ) -> None:
        settings = self.settings
        if not settings.enabled or not sender_id:
            return
        if not settings.monitors_channel(channel):
            return

        cmd_mgr = getattr(self.bot, "command_manager", None)
        if cmd_mgr is None:
            return
        if cmd_mgr.is_user_banned(sender_id):
            return
        if self._is_self(sender_id, sender_pubkey):
            return
        # channelpause silences the bot on channels; a warning is a bot response
        # and has no business being the one thing that keeps talking.
        if not getattr(self.bot, "channel_responses_enabled", True):
            return
        if settings.delivery == DELIVERY_DM and not self._is_known_contact(sender_id):
            # send_dm would fail anyway, and a failed attempt now spends a cap
            # slot and a cooldown. More to the point, the sender is a display
            # name off the wire: declining to DM a name with no contact behind
            # it keeps the bot from messaging a node on a stranger's say-so.
            #
            # Counted, not silent: a bot that keeps no contacts drops *every*
            # warning here, and without this the page would show an empty log
            # forever beside a status card claiming it was sending.
            self._record_withheld()
            self.logger.debug(
                "Region warning for %s withheld: no contact by that name", sender_id
            )
            return

        state = self._senders.get(sender_id)
        now = time.monotonic()
        if state is None or (now - state.counted_at) > self.RUN_TTL_SECONDS:
            state = _SenderState()
            self._senders[sender_id] = state
            self._evict_stale_senders(now)
        state.unscoped_seen += 1
        state.counted_at = now
        if state.unscoped_seen < settings.min_unscoped_messages:
            return

        self._load_budget_once()

        if self._mesh_cooldown_active(now):
            return
        if self._sender_cooldown_active(sender_id):
            return
        if self._daily_cap_reached():
            self.logger.debug(
                "Region warning for %s withheld: daily cap of %d reached",
                sender_id, settings.max_warnings_per_day,
            )
            return

        text = render_message(settings.message, sender_id, channel)
        if not text:
            return

        # The run has been spent whether or not the send itself succeeds; not
        # resetting it would retry on the sender's very next message.
        state.unscoped_seen = 0

        if settings.dry_run:
            self._record_event(sender_id, sender_pubkey, channel, ACTION_DRY_RUN, text)
            self._mark_warning_sent(now)
            self.logger.info(
                "Region warning (dry run) for %s on %s: %s", sender_id, channel or "?", text
            )
            return

        # Reserve the slot *before* awaiting the send. Every gate above and this
        # reservation run without an await between them, so on the single event
        # loop they are atomic: a second channel message arriving mid-send reads
        # a cooldown and a cap row that already account for this warning. Doing
        # it after the send instead let two concurrent messages both pass a cap
        # of one and both transmit.
        previous_mark = (self._last_warning_monotonic, self._last_warning_wall)
        event_id = self._record_event(
            sender_id, sender_pubkey, channel, ACTION_SENT, text)
        self._mark_warning_sent(now)

        sent, detail = await self._send_warning(sender_id, channel, text)
        if sent:
            return
        # Correct the optimistic reservation. The event row stays — the attempt
        # still counts against the daily cap, because a send that reported
        # failure may have put something on the air before it did — but a
        # failure should not hold the mesh cooldown against the next sender.
        self._update_event(event_id, ACTION_FAILED, detail)
        self._last_warning_monotonic, self._last_warning_wall = previous_mark

    async def _send_warning(
        self, sender_id: str, channel: Optional[str], text: str
    ) -> tuple[bool, str]:
        cmd_mgr = self.bot.command_manager
        if self.settings.delivery == DELIVERY_CHANNEL:
            if not channel:
                return False, "no channel to reply on"
            body = truncate_to_bytes(text, self._channel_body_limit())
            # Deliberately global scope: the recipient is by definition not
            # inside any region the bot replies under, so a scoped reply would
            # never reach them.
            ok = await cmd_mgr.send_channel_message(
                channel, body, skip_user_rate_limit=True, scope="*"
            )
            return ok, body if ok else f"channel send failed: {body}"

        body = truncate_to_bytes(text, DM_BODY_LIMIT)
        ok = await cmd_mgr.send_dm(sender_id, body, skip_user_rate_limit=True)
        return ok, body if ok else f"DM send failed (contact unknown or radio busy): {body}"

    def _channel_body_limit(self) -> int:
        """Channel body budget for a global-scope send from this node.

        Warnings always go out unscoped, so no regional-scope overhead applies.
        """
        try:
            from modules.models import MeshMessage

            probe = MeshMessage(content="", channel="", is_dm=False, reply_scope="")
            return int(self.bot.command_manager.get_max_message_length(probe))
        except Exception:
            return channel_body_limit(None)

    # -- gating ------------------------------------------------------------

    def _evict_stale_senders(self, now: float) -> None:
        """Keep ``_senders`` bounded (see ``MAX_TRACKED_SENDERS``)."""
        if len(self._senders) <= self.MAX_TRACKED_SENDERS:
            return
        for name, state in list(self._senders.items()):
            if (now - state.counted_at) > self.RUN_TTL_SECONDS:
                del self._senders[name]
        overflow = len(self._senders) - self.MAX_TRACKED_SENDERS
        if overflow <= 0:
            return
        oldest = sorted(self._senders.items(), key=lambda kv: kv[1].counted_at)
        for name, _state in oldest[:overflow]:
            del self._senders[name]

    def _now(self) -> datetime:
        return local_now(getattr(self.bot, "config", None), self.logger)

    def _is_self(self, sender_id: str, sender_pubkey: Optional[str]) -> bool:
        """Whether this message came from the bot's own node.

        The public key is checked first where one is available, but MeshCore's
        CHANNEL_MSG_RECV carries none — see the class docstring — so on the path
        this monitor actually runs on, the display name is the only signal and
        the fallback below is what decides.
        """
        own_key = self._own_public_key()
        prefix = (sender_pubkey or "").strip().lower()
        if own_key and prefix:
            return own_key.startswith(prefix) or prefix.startswith(own_key)
        try:
            bot_name = self.bot.config.get("Bot", "bot_name", fallback="") or ""
        except Exception:
            bot_name = ""
        return bool(bot_name) and sender_id.strip().lower() == bot_name.strip().lower()

    def _record_withheld(self) -> None:
        """Bump today's withheld-for-no-contact counter in ``bot_metadata``.

        Keyed by local date so it resets on its own and needs no retention.
        """
        db_manager = getattr(self.bot, "db_manager", None)
        if not db_manager:
            return
        today = self._now().date().isoformat()
        try:
            if self._withheld_date != today:
                stored = read_withheld(db_manager, today)
                self._withheld_date = today
                self._withheld_count = stored
            self._withheld_count += 1
            db_manager.set_metadata(
                WITHHELD_METADATA_KEY,
                json.dumps({"date": today, "count": self._withheld_count}),
            )
        except Exception:
            self.logger.debug("Could not record withheld region warning", exc_info=True)

    def _is_known_contact(self, sender_id: str) -> bool:
        """Whether a contact by this name is on the radio.

        The bot has no way to authenticate a channel sender, so this does not
        prove the message came from that node. It does keep a warning pointed at
        a node the bot already knows, rather than at any name someone types.
        """
        meshcore = getattr(self.bot, "meshcore", None)
        if meshcore is None:
            return False
        lookup = getattr(meshcore, "get_contact_by_name", None)
        if callable(lookup):
            try:
                if lookup(sender_id):
                    return True
            except Exception:
                pass
        contacts = getattr(meshcore, "contacts", None)
        if isinstance(contacts, dict):
            wanted = sender_id.strip().lower()
            for contact in contacts.values():
                if not isinstance(contact, dict):
                    continue
                name = (contact.get("adv_name") or contact.get("name") or "").strip().lower()
                if name and name == wanted:
                    return True
        return False

    def _own_public_key(self) -> str:
        """This node's public key as lowercase hex, or "" when unavailable."""
        try:
            self_info = getattr(getattr(self.bot, "meshcore", None), "self_info", None)
            if self_info is None:
                return ""
            if isinstance(self_info, dict):
                key = self_info.get("public_key", "")
            else:
                key = getattr(self_info, "public_key", "")
            if isinstance(key, (bytes, bytearray)):
                return bytes(key).hex()
            return str(key or "").strip().lower()
        except Exception:
            return ""

    def _load_budget_once(self) -> None:
        """Seed the mesh cooldown from the DB so a restart cannot bypass it."""
        if self._budget_loaded:
            return
        self._budget_loaded = True
        db_manager = getattr(self.bot, "db_manager", None)
        if not db_manager:
            return
        try:
            rows = db_manager.execute_query(
                "SELECT MAX(created_at) AS last_at FROM region_warning_events "
                "WHERE action IN (?, ?)",
                (ACTION_SENT, ACTION_DRY_RUN),
            )
        except Exception:
            return
        raw = rows[0].get("last_at") if rows else None
        if not raw:
            return
        parsed = _parse_timestamp(raw)
        if parsed is None:
            return
        self._last_warning_wall = parsed
        elapsed = (self._now() - parsed).total_seconds()
        if elapsed >= 0:
            self._last_warning_monotonic = time.monotonic() - elapsed

    def _mark_warning_sent(self, now: float) -> None:
        self._last_warning_monotonic = now
        self._last_warning_wall = self._now()

    def _mesh_cooldown_active(self, now: float) -> bool:
        window = self.settings.mesh_cooldown_minutes * 60.0
        if window <= 0 or self._last_warning_monotonic is None:
            return False
        return (now - self._last_warning_monotonic) < window

    def _sender_cooldown_active(self, sender_id: str) -> bool:
        """Whether this sender was warned, or attempted, inside the cooldown.

        Attempts count, matching the daily cap. Counting only successes meant a
        sender the radio can never reach — an unknown contact, which is exactly
        what a brand-new client with no region code often is — spent a cap slot
        every ``min_unscoped_messages`` messages forever, starving the feature
        while nobody was ever warned.
        """
        hours = self.settings.per_sender_cooldown_hours
        if hours <= 0:
            return False
        db_manager = getattr(self.bot, "db_manager", None)
        if not db_manager:
            return False
        try:
            rows = db_manager.execute_query(
                "SELECT MAX(created_at) AS last_at FROM region_warning_events "
                "WHERE sender_id = ?",
                (sender_id,),
            )
        except Exception:
            return False
        raw = rows[0].get("last_at") if rows else None
        parsed = _parse_timestamp(raw) if raw else None
        if parsed is None:
            return False
        return (self._now() - parsed) < timedelta(hours=hours)

    def _daily_cap_reached(self) -> bool:
        cap = self.settings.max_warnings_per_day
        if cap <= 0:
            return False
        db_manager = getattr(self.bot, "db_manager", None)
        if not db_manager:
            return False
        today = self._now().date().isoformat()
        try:
            # Attempts, not successes: see warning_budget.
            rows = db_manager.execute_query(
                "SELECT COUNT(*) AS used FROM region_warning_events "
                "WHERE date(created_at) = ?",
                (today,),
            )
        except Exception:
            return False
        used = int(rows[0].get("used") or 0) if rows else 0
        return used >= cap

    # -- persistence -------------------------------------------------------

    def _record_event(
        self,
        sender_id: str,
        sender_pubkey: Optional[str],
        channel: Optional[str],
        action: str,
        detail: str,
    ) -> Optional[int]:
        """Write one decision row; returns its id so the outcome can be corrected."""
        db_manager = getattr(self.bot, "db_manager", None)
        if not db_manager:
            return None
        try:
            with db_manager.connection() as conn:
                cursor = conn.execute(
                    "INSERT INTO region_warning_events "
                    "(created_at, sender_id, sender_pubkey, channel, delivery, action, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._now().isoformat(sep=" ", timespec="seconds"),
                        sender_id,
                        (sender_pubkey or "")[:64] or None,
                        channel,
                        self.settings.delivery,
                        action,
                        detail[:400] if detail else None,
                    ),
                )
                conn.commit()
                return int(cursor.lastrowid) if cursor.lastrowid else None
        except Exception:
            self.logger.exception("Failed to record region warning event")
            return None

    def _update_event(self, event_id: Optional[int], action: str, detail: str) -> None:
        """Replace a reserved row's outcome once the send has resolved."""
        db_manager = getattr(self.bot, "db_manager", None)
        if not db_manager or event_id is None:
            return
        try:
            with db_manager.connection() as conn:
                conn.execute(
                    "UPDATE region_warning_events SET action = ?, detail = ? WHERE id = ?",
                    (action, detail[:400] if detail else None, event_id),
                )
                conn.commit()
        except Exception:
            self.logger.exception("Failed to update region warning event")


def _parse_timestamp(raw: Any) -> Optional[datetime]:
    """Parse a stored event timestamp, tolerating ``T`` or space separators."""
    if not raw:
        return None
    text = str(raw).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None
