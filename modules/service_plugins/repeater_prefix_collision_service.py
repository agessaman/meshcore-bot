#!/usr/bin/env python3
"""
Repeater Prefix Collision Service for MeshCore Bot

Watches NEW_CONTACT events and notifies channels when a newly discovered repeater
shares a prefix with an existing repeater (configurable: 1/2/3 bytes).
"""

import asyncio
import copy
import time
from dataclasses import dataclass
from typing import Any

from meshcore import EventType

from .base_service import BaseServicePlugin


@dataclass(frozen=True)
class _NotifyKey:
    public_key: str
    prefix_bytes: int


class RepeaterPrefixCollisionService(BaseServicePlugin):
    config_section = "RepeaterPrefixCollision_Service"
    description = "Notifies when a newly heard repeater prefix collides with an existing repeater"

    def __init__(self, bot: Any) -> None:
        super().__init__(bot)

        section = self.config_section

        # Channels
        self.channels: list[str] = self._load_channels(section)

        # Prefix match lengths to notify on: 1/2/3 bytes (hex chars = bytes*2)
        self.notify_on_prefix_bytes: list[int] = self._load_notify_on_prefix_bytes(section)

        # Optional windowing
        self.heard_window_days = self.bot.config.getint(section, "heard_window_days", fallback=30)
        self.prefix_free_days = self.bot.config.getint(section, "prefix_free_days", fallback=30)

        # Post-processing wait so track_contact_advertisement can write + geocode
        self.post_process_delay_seconds = self.bot.config.getfloat(
            section, "post_process_delay_seconds", fallback=0.5
        )
        self.post_process_timeout_seconds = self.bot.config.getfloat(
            section, "post_process_timeout_seconds", fallback=15.0
        )
        self.post_process_poll_interval_seconds = self.bot.config.getfloat(
            section, "post_process_poll_interval_seconds", fallback=0.2
        )

        # Message content toggles
        self.include_prefix_free_hint = self.bot.config.getboolean(
            section, "include_prefix_free_hint", fallback=True
        )

        # Spam control / dedupe
        self.cooldown_minutes_per_prefix = self.bot.config.getint(
            section, "cooldown_minutes_per_prefix", fallback=60
        )
        self._notified: dict[_NotifyKey, float] = {}  # key -> last_sent_epoch_seconds
        self._prefix_cooldown: dict[tuple[int, str], float] = {}  # (bytes, prefix_hex_lower) -> last_sent_epoch_seconds
        self._dedupe_prune_max_age_seconds = max(
            3600.0,
            float(self.cooldown_minutes_per_prefix) * 120.0,
        )

        self._running = False
        self._handler_installed = False
        self._handler_lock = asyncio.Lock()

        self.logger.info(
            "RepeaterPrefixCollision service initialized: channels=%s notify_on_prefix_bytes=%s",
            self.channels,
            self.notify_on_prefix_bytes,
        )

    async def start(self) -> None:
        if not self.enabled:
            self.logger.info("RepeaterPrefixCollision service is disabled")
            return

        if not getattr(self.bot, "meshcore", None):
            self.logger.error("RepeaterPrefixCollision cannot start: meshcore not available")
            return

        async with self._handler_lock:
            if self._handler_installed:
                self._running = True
                return
            self.bot.meshcore.subscribe(EventType.NEW_CONTACT, self._on_new_contact)
            self._handler_installed = True
            self._running = True

        self.logger.info("RepeaterPrefixCollision service started")

    async def stop(self) -> None:
        self._running = False
        # meshcore currently does not expose a stable unsubscribe API in this codebase;
        # we rely on _running checks to avoid work after stop.
        self.logger.info("RepeaterPrefixCollision service stopped")

    async def _on_new_contact(self, event, metadata=None) -> None:
        """Thin callback: schedule work so meshcore does not block on sleep/poll/DB."""
        if not self._running:
            return

        try:
            payload = copy.deepcopy(event.payload) if hasattr(event, "payload") else None
            if not payload:
                return

            public_key = (payload.get("public_key") or "").strip()
            if not public_key:
                return

            asyncio.create_task(self._handle_new_contact_payload(payload))
        except Exception as e:
            self.logger.error("Error scheduling RepeaterPrefixCollision handler: %s", e, exc_info=True)

    async def _handle_new_contact_payload(self, payload: dict[str, Any]) -> None:
        """
        Process NEW_CONTACT after MessageHandler has persisted the row.

        Notify only when this public_key is new to tracking today (first_heard is today)
        and exactly one distinct advert packet hash exists in unique_advert_packets for
        today—strict gate; skips double-ingest or same-day re-adverts.
        """
        if not self._running:
            return

        try:
            public_key = (payload.get("public_key") or "").strip()
            if not public_key:
                return

            pk_short = f"{public_key[:16]}…" if len(public_key) > 16 else public_key
            self.logger.debug("RepeaterPrefixCollision: NEW_CONTACT %s", pk_short)

            if self.post_process_delay_seconds > 0:
                await asyncio.sleep(self.post_process_delay_seconds)

            row = await self._wait_for_contact_row(public_key)
            if not row:
                self.logger.debug(
                    "RepeaterPrefixCollision: skip %s — no DB row within post_process_timeout",
                    pk_short,
                )
                return

            if not self._db_first_heard_is_today(row.get("first_heard")):
                self.logger.debug(
                    "RepeaterPrefixCollision: skip %s — first_heard not today (%r)",
                    pk_short,
                    row.get("first_heard"),
                )
                return

            n_unique = self._db_unique_advert_count_today(public_key)
            if n_unique != 1:
                self.logger.debug(
                    "RepeaterPrefixCollision: skip %s — unique_advert_packets today=%s "
                    "(strict gate requires exactly 1; e.g. second path/hash same day skips)",
                    pk_short,
                    n_unique,
                )
                return

            role = (row.get("role") or "").lower()
            if role not in ("repeater", "roomserver"):
                self.logger.debug(
                    "RepeaterPrefixCollision: skip %s — role=%r",
                    pk_short,
                    row.get("role"),
                )
                return

            name = row.get("name") or payload.get("name") or payload.get("adv_name") or "Unknown"
            location = self._format_location(row)

            self._prune_old_dedupe_state()

            for nbytes in self.notify_on_prefix_bytes:
                await self._maybe_notify_for_prefix_bytes(
                    public_key=public_key,
                    name=name,
                    location=location,
                    prefix_bytes=nbytes,
                )
        except Exception as e:
            self.logger.error("Error in RepeaterPrefixCollision NEW_CONTACT handler: %s", e, exc_info=True)

    async def _wait_for_contact_row(self, public_key: str) -> dict[str, Any] | None:
        deadline = time.time() + max(0.0, float(self.post_process_timeout_seconds))
        poll = max(0.05, float(self.post_process_poll_interval_seconds))
        while time.time() <= deadline:
            row = self._db_get_contact_row(public_key)
            if row:
                return row
            await asyncio.sleep(poll)
        return None

    def _db_get_contact_row(self, public_key: str) -> dict[str, Any] | None:
        if not getattr(self.bot, "db_manager", None):
            return None
        rows = self.bot.db_manager.execute_query(
            """
            SELECT public_key, name, role, advert_count, first_heard, last_heard,
                   latitude, longitude, city, state, country
            FROM complete_contact_tracking
            WHERE public_key = ?
            LIMIT 1
            """,
            (public_key,),
        )
        return rows[0] if rows else None

    def _db_first_heard_is_today(self, first_heard: Any) -> bool:
        if first_heard is None:
            return False
        if not getattr(self.bot, "db_manager", None):
            return False
        rows = self.bot.db_manager.execute_query(
            "SELECT 1 AS ok WHERE DATE(?) = DATE('now', 'localtime')",
            (first_heard,),
        )
        return bool(rows)

    def _db_unique_advert_count_today(self, public_key: str) -> int:
        if not getattr(self.bot, "db_manager", None):
            return 0
        rows = self.bot.db_manager.execute_query(
            """
            SELECT COUNT(*) AS n
            FROM unique_advert_packets
            WHERE public_key = ? AND date = DATE('now', 'localtime')
            """,
            (public_key,),
        )
        if not rows:
            return 0
        try:
            return int(rows[0].get("n", 0))
        except (TypeError, ValueError):
            return 0

    def _prune_old_dedupe_state(self) -> None:
        """Drop dedupe entries older than cooldown window to avoid unbounded growth."""
        now = time.time()
        cutoff = now - self._dedupe_prune_max_age_seconds
        if self._notified:
            stale = [k for k, ts in self._notified.items() if ts < cutoff]
            for notify_key in stale:
                del self._notified[notify_key]
        if self._prefix_cooldown:
            stale_p = [k for k, ts in self._prefix_cooldown.items() if ts < cutoff]
            for prefix_key in stale_p:
                del self._prefix_cooldown[prefix_key]

    async def _maybe_notify_for_prefix_bytes(
        self,
        public_key: str,
        name: str,
        location: str,
        prefix_bytes: int,
    ) -> None:
        prefix_hex_chars = prefix_bytes * 2
        if len(public_key) < prefix_hex_chars:
            return

        prefix = public_key[:prefix_hex_chars].lower()

        # Per-node dedupe (public_key + bytes)
        if self._is_recently_notified(_NotifyKey(public_key=public_key, prefix_bytes=prefix_bytes)):
            return

        # Per-prefix cooldown across many nodes
        if self._is_prefix_in_cooldown(prefix_bytes, prefix):
            return

        if not self._db_prefix_is_duplicate(public_key, prefix, prefix_hex_chars):
            return

        prefixes_free = self._count_free_prefixes(prefix_hex_chars)
        text = self._format_message(
            name=name,
            prefix=prefix.upper(),
            location=location,
            prefixes_free=prefixes_free,
            prefix_bytes=prefix_bytes,
        )

        await self._send_to_channels(text)

        pk_short = f"{public_key[:16]}…" if len(public_key) > 16 else public_key
        self.logger.info(
            "RepeaterPrefixCollision: posted alert for %s name=%r prefix=%s (%d byte(s))",
            pk_short,
            name,
            prefix.upper(),
            prefix_bytes,
        )

        now = time.time()
        self._notified[_NotifyKey(public_key=public_key, prefix_bytes=prefix_bytes)] = now
        self._prefix_cooldown[(prefix_bytes, prefix)] = now

    def _db_prefix_is_duplicate(self, public_key: str, prefix: str, prefix_hex_chars: int) -> bool:
        if not getattr(self.bot, "db_manager", None):
            return False
        where_window = ""
        params: list[Any] = [public_key, prefix_hex_chars, prefix, prefix_hex_chars]
        if self.heard_window_days and self.heard_window_days > 0:
            where_window = (
                f" AND last_heard >= datetime('now', 'localtime', '-{int(self.heard_window_days)} days')"
            )
        rows = self.bot.db_manager.execute_query(
            f"""
            SELECT COUNT(*) AS cnt
            FROM complete_contact_tracking
            WHERE role IN ('repeater','roomserver')
              AND public_key != ?
              AND SUBSTR(public_key, 1, ?) = SUBSTR(?, 1, ?)
              {where_window}
            """,
            tuple(params),
        )
        try:
            cnt = int(rows[0].get("cnt", 0)) if rows else 0
        except Exception:
            cnt = 0
        return cnt > 0

    def _count_free_prefixes(self, prefix_hex_chars: int) -> int | None:
        """
        Return how many prefixes are free (unused by repeaters/roomservers) for the given prefix length.
        Mirrors prefix_command behavior by excluding all-zeros and all-FF..FF.
        """
        if not getattr(self.bot, "db_manager", None):
            return None

        where_window = ""
        if self.prefix_free_days and self.prefix_free_days > 0:
            where_window = (
                f" AND last_heard >= datetime('now', 'localtime', '-{int(self.prefix_free_days)} days')"
            )
        rows = self.bot.db_manager.execute_query(
            f"""
            SELECT COUNT(DISTINCT SUBSTR(public_key, 1, {int(prefix_hex_chars)})) AS used
            FROM complete_contact_tracking
            WHERE role IN ('repeater','roomserver')
              AND LENGTH(public_key) >= {int(prefix_hex_chars)}
              {where_window}
            """
        )
        try:
            used = int(rows[0].get("used", 0)) if rows else 0
        except Exception:
            used = 0

        total = (16 ** prefix_hex_chars)  # hex chars, not bytes
        total_valid = max(0, total - 2)  # exclude all-zeros and all-FF..FF
        free = total_valid - used
        return max(0, int(free))

    def _format_message(
        self,
        name: str,
        prefix: str,
        location: str,
        prefixes_free: int | None,
        prefix_bytes: int,
    ) -> str:
        free_str = "Unknown"
        if prefixes_free is not None:
            free_str = str(prefixes_free)

        text = f"Heard new repeater {name} with prefix {prefix} near {location}. {free_str} free prefixes remain."
        if self.include_prefix_free_hint and prefix_bytes == 1:
            text += " Type 'prefix free' to find one."
        return text

    async def _send_to_channels(self, text: str) -> None:
        for ch in self.channels:
            await self.bot.command_manager.send_channel_message(
                ch, text, skip_user_rate_limit=True
            )

    def _format_location(self, row: dict[str, Any]) -> str:
        city = (row.get("city") or "").strip()
        state = (row.get("state") or "").strip()
        country = (row.get("country") or "").strip()
        lat = row.get("latitude")
        lon = row.get("longitude")

        parts = [p for p in (city, state, country) if p]
        if parts:
            return ", ".join(parts)

        try:
            if lat is not None and lon is not None and not (float(lat) == 0.0 and float(lon) == 0.0):
                return f"{float(lat):.4f},{float(lon):.4f}"
        except Exception:
            pass
        return "Unknown"

    def _is_recently_notified(self, key: _NotifyKey) -> bool:
        ts = self._notified.get(key)
        if not ts:
            return False
        cooldown_s = max(0, int(self.cooldown_minutes_per_prefix)) * 60
        return (time.time() - ts) < cooldown_s if cooldown_s else False

    def _is_prefix_in_cooldown(self, prefix_bytes: int, prefix_hex_lower: str) -> bool:
        ts = self._prefix_cooldown.get((prefix_bytes, prefix_hex_lower))
        if not ts:
            return False
        cooldown_s = max(0, int(self.cooldown_minutes_per_prefix)) * 60
        return (time.time() - ts) < cooldown_s if cooldown_s else False

    def _load_channels(self, section: str) -> list[str]:
        raw = ""
        if self.bot.config.has_option(section, "channels"):
            raw = (self.bot.config.get(section, "channels") or "").strip()
        if not raw:
            raw = (self.bot.config.get(section, "channel", fallback="#general") or "").strip()
        channels = [c.strip() for c in raw.split(",") if c.strip()]
        if not channels:
            channels = ["#general"]
        return channels

    def _load_notify_on_prefix_bytes(self, section: str) -> list[int]:
        raw = (self.bot.config.get(section, "notify_on_prefix_bytes", fallback="1") or "").strip()
        vals: set[int] = set()
        for part in raw.split(","):
            p = part.strip()
            if not p:
                continue
            try:
                v = int(p)
            except ValueError:
                continue
            if v in (1, 2, 3):
                vals.add(v)
        if not vals:
            vals.add(1)
        return sorted(vals)

