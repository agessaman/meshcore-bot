#!/usr/bin/env python3
"""
Repeater telemetry service for the MeshCore Bot.

Periodically logs in to configured repeaters, requests their status (battery
voltage, uptime, noise floor, RSSI, packet counters), stores every sample in
SQLite and raises alerts when the battery runs low or a repeater stops
answering.  Alerts go as DM to up to five admins, to a mesh channel and/or to
Discord/Telegram (via the shared ``discord_webhook_urls`` / ``telegram_chat_ids`` keys).

Settings come from ``[RepeaterTelemetry_Service]`` in config.ini and can be
overridden at runtime from the web viewer's "Repeater-Akku" page (see
modules/repeater_telemetry_store.py and docs/repeater-telemetry.md).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional

from modules import repeater_telemetry_store as store

from .base_service import BaseServicePlugin

# Re-exported for callers/tests
TABLE_SAMPLES = store.TABLE_SAMPLES
TABLE_STATE = store.TABLE_STATE
DEFAULT_TEMPLATES = store.DEFAULT_TEMPLATES

# Alert levels, ordered by severity
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_CRITICAL = "critical"
_LEVEL_RANK = {LEVEL_OK: 0, LEVEL_WARN: 1, LEVEL_CRITICAL: 2}

# How often the idle loop re-reads settings and checks for "poll now" requests
SETTINGS_TICK_SECONDS = 15


@dataclass
class RepeaterTarget:
    """One repeater to monitor, as configured."""

    label: str      # name or key prefix from config
    password: str   # login password ('' = guest / no password)


@dataclass
class PollResult:
    """Outcome of one status request."""

    ok: bool
    name: str
    pubkey: str = ""
    bat_mv: Optional[int] = None
    status: Optional[dict[str, Any]] = None
    error: str = ""


def parse_targets(raw: str) -> list[RepeaterTarget]:
    """Parse ``name:password, name2, abcd12:pw`` into targets."""
    return [RepeaterTarget(r["name"], r["password"]) for r in store.parse_repeaters(raw)]


def classify_battery(bat_mv: int, warn_mv: int, critical_mv: int) -> str:
    """Map a battery voltage to an alert level."""
    if bat_mv <= 0:
        return LEVEL_OK  # 0 = repeater does not report a battery (e.g. mains powered)
    if bat_mv < critical_mv:
        return LEVEL_CRITICAL
    if bat_mv < warn_mv:
        return LEVEL_WARN
    return LEVEL_OK


def next_level(previous: str, bat_mv: int, warn_mv: int, critical_mv: int, hysteresis_mv: int) -> str:
    """Level with hysteresis: only step down in severity once the voltage has
    risen ``hysteresis_mv`` above the threshold, so a battery hovering around
    the limit does not flap between warn and ok on every poll."""
    raw = classify_battery(bat_mv, warn_mv, critical_mv)
    if _LEVEL_RANK[raw] >= _LEVEL_RANK[previous]:
        return raw
    relaxed = classify_battery(bat_mv - hysteresis_mv, warn_mv, critical_mv)
    return relaxed if _LEVEL_RANK[relaxed] < _LEVEL_RANK[previous] else previous


class RepeaterTelemetryService(BaseServicePlugin):
    """Polls repeater status and warns on low battery / unreachable repeaters."""

    config_section = store.CONFIG_SECTION
    name = "repeatertelemetry"
    description = "Polls repeater status (battery, uptime, RSSI) and warns admins on low battery"

    settings_schema = [
        {"key": "repeaters", "label": "Repeaters", "type": "password", "default": "",
         "help": "Comma-separated name:password (or key-prefix:password). Easier: use the "
                 "'Repeater-Akku' page, whose settings override these values."},
        {"key": "admins", "label": "Admins (max 5)", "type": "list", "default": "",
         "help": "Contact names that receive alerts as MeshCore DM (up to 5)."},
        {"key": "interval_minutes", "label": "Poll interval", "type": "int", "min": 5,
         "default": 60, "unit": "min", "help": "How often all repeaters are polled."},
        {"key": "delay_between_seconds", "label": "Delay between repeaters", "type": "int", "min": 5,
         "default": 30, "unit": "s", "help": "Pause between two repeaters to keep airtime low."},
        {"key": "request_timeout_seconds", "label": "Request timeout", "type": "int", "min": 0,
         "default": 0, "unit": "s", "help": "0 = use the timeout suggested by the radio."},
        {"key": "warn_mv", "label": "Warn below", "type": "int", "min": 0,
         "default": 3500, "unit": "mV", "help": "Battery warning threshold."},
        {"key": "critical_mv", "label": "Critical below", "type": "int", "min": 0,
         "default": 3300, "unit": "mV", "help": "Critical battery threshold."},
        {"key": "hysteresis_mv", "label": "Hysteresis", "type": "int", "min": 0,
         "default": 100, "unit": "mV", "help": "Voltage must rise this much above a threshold before 'recovered'."},
        {"key": "repeat_alert_hours", "label": "Repeat alert after", "type": "float", "min": 0,
         "default": 12, "unit": "h", "help": "Re-send an unchanged alert after this long (0 = never)."},
        {"key": "offline_after_failures", "label": "Offline after", "type": "int", "min": 0,
         "default": 3, "help": "Consecutive failed polls before an offline alert (0 = no offline alerts)."},
        {"key": "alert_channel", "label": "Alert channel", "type": "str", "default": "",
         "help": "Optional mesh channel for alerts (e.g. #admin). Empty = admins by DM only."},
        {"key": "retention_days", "label": "Keep history", "type": "int", "min": 1,
         "default": 90, "unit": "days", "help": "Samples older than this are deleted."},
        {"key": "silence_mesh_output", "label": "Silence mesh output", "type": "bool", "default": False,
         "help": "Only send alerts to Discord/Telegram, never on the mesh."},
    ]

    def __init__(self, bot: Any):
        super().__init__(bot)
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._poll_now = asyncio.Event()
        self._handled_poll_request = 0
        self.settings: dict[str, Any] = {}
        # config.ini only until start() can read the web overrides from the DB
        self._apply_settings(store.settings_from_config(bot.config))

    # ------------------------------------------------------------------ config

    def _load_settings(self) -> None:
        """(Re-)read effective settings: defaults <- config.ini <- web page."""
        with self.bot.db_manager.connection() as conn:
            settings = store.load_settings(conn, self.bot.config)
        self._apply_settings(settings)

    def _apply_settings(self, s: dict[str, Any]) -> None:
        self.settings = s
        self.targets = [RepeaterTarget(r["name"], r.get("password", "")) for r in s["repeaters"]]
        self.admins = list(s["admins"])[: store.MAX_ADMINS]
        self.paused = bool(s["paused"])
        self.interval_s = int(s["interval_minutes"]) * 60
        self.delay_between_s = int(s["delay_between_seconds"])
        self.initial_delay_s = int(s["initial_delay_seconds"])
        self.request_timeout_s = int(s["request_timeout_seconds"])
        self.warn_mv = int(s["warn_mv"])
        self.critical_mv = int(s["critical_mv"])
        self.hysteresis_mv = int(s["hysteresis_mv"])
        self.repeat_alert_s = float(s["repeat_alert_hours"]) * 3600
        self.offline_after = int(s["offline_after_failures"])
        self.alert_channel = s["alert_channel"]
        self.silence_mesh = bool(s["silence_mesh_output"])
        self.retention_days = int(s["retention_days"])
        self.templates = {k: s.get(f"template_{k}") or v for k, v in DEFAULT_TEMPLATES.items()}
        self._external_notify_cache = None

    # ----------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._init_db()
        self._load_settings()
        # Don't treat a poll request left over from before this start as new
        self._handled_poll_request = int(self.settings.get(store.POLL_REQUEST_KEY) or 0)
        if not self.targets:
            self.logger.warning("[RepeaterTelemetry] no repeaters configured yet (web page 'Repeater-Akku')")
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        self._running = True
        self.logger.info(
            f"[RepeaterTelemetry] started: {len(self.targets)} repeater(s), {len(self.admins)} admin(s), "
            f"every {self.interval_s // 60} min, warn < {self.warn_mv} mV, critical < {self.critical_mv} mV"
        )

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.logger.info("[RepeaterTelemetry] stopped")

    def request_poll(self) -> None:
        """Trigger an immediate poll cycle (used by the akku command)."""
        self._poll_now.set()

    async def _run_loop(self) -> None:
        if await self._wait(self.initial_delay_s):
            return
        while not self._stop_event.is_set():
            try:
                self._load_settings()
                if not self.paused:
                    await self.poll_all()
                self._prune_old_samples()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(f"[RepeaterTelemetry] poll cycle failed: {e}", exc_info=True)
            if await self._wait(self.interval_s):
                return

    async def _wait(self, seconds: float) -> bool:
        """Wait for the next cycle. Wakes early on stop, on 'akku jetzt', on a
        poll request from the web page, or when the web page shortened the
        interval. Returns True when stopping."""
        self._poll_now.clear()
        started = time.monotonic()
        while not self._stop_event.is_set():
            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                return False
            tick = min(remaining, SETTINGS_TICK_SECONDS)
            stop_wait = asyncio.create_task(self._stop_event.wait())
            poll_wait = asyncio.create_task(self._poll_now.wait())
            try:
                await asyncio.wait({stop_wait, poll_wait}, timeout=tick, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop_wait.cancel()
                poll_wait.cancel()
            if self._poll_now.is_set():
                return self._stop_event.is_set()
            if self._check_web_changes():
                seconds = min(seconds, self.interval_s)
                requested = int(self.settings.get(store.POLL_REQUEST_KEY) or 0)
                if requested > self._handled_poll_request:
                    self._handled_poll_request = requested
                    self.logger.info("[RepeaterTelemetry] poll requested from web page")
                    return False
        return True

    def _check_web_changes(self) -> bool:
        try:
            self._load_settings()
            return True
        except Exception as e:
            self.logger.debug(f"[RepeaterTelemetry] reading settings failed: {e}")
            return False

    # ------------------------------------------------------------------- polling

    async def poll_all(self) -> list[PollResult]:
        results: list[PollResult] = []
        for i, target in enumerate(self.targets):
            if self._stop_event.is_set():
                break
            if i > 0 and await self._sleep_plain(self.delay_between_s):
                break
            if not self._is_connected():
                self.logger.info("[RepeaterTelemetry] radio not connected, skipping this cycle")
                break
            result = await self.poll_one(target)
            self._store_sample(result)
            await self._evaluate(result)
            results.append(result)
        return results

    async def _sleep_plain(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        return self._stop_event.is_set()

    def _is_connected(self) -> bool:
        return bool(getattr(self.bot, "connected", False) and getattr(self.bot, "meshcore", None))

    def _find_contact(self, label: str) -> Optional[dict[str, Any]]:
        mc = self.bot.meshcore
        contact = mc.get_contact_by_name(label)
        if contact is None and len(label) >= 4 and all(c in "0123456789abcdefABCDEF" for c in label):
            contact = mc.get_contact_by_key_prefix(label.lower())
        return contact

    async def poll_one(self, target: RepeaterTarget) -> PollResult:
        # Samples and alert state are keyed by the configured label so a repeater
        # keeps one history even when it drops out of the contact list.
        name = target.label
        contact = self._find_contact(target.label)
        if contact is None:
            return PollResult(ok=False, name=name, error="not in contacts")
        pubkey = contact.get("public_key", "")
        cmds = self.bot.meshcore.commands
        timeout = self.request_timeout_s
        try:
            login = await self._login(contact, target.password, timeout)
            if not login:
                return PollResult(ok=False, name=name, pubkey=pubkey, error="login failed/timeout")
            status = await cmds.req_status_sync(contact, timeout=timeout, min_timeout=5)
        except Exception as e:
            return PollResult(ok=False, name=name, pubkey=pubkey, error=f"{type(e).__name__}: {e}")
        if not status:
            return PollResult(ok=False, name=name, pubkey=pubkey, error="no status response")
        bat = status.get("bat")
        return PollResult(ok=True, name=name, pubkey=pubkey, bat_mv=int(bat) if bat is not None else None,
                          status=status)

    async def _login(self, contact: dict[str, Any], password: str, timeout: int) -> bool:
        cmds = self.bot.meshcore.commands
        if hasattr(cmds, "send_login_sync"):
            event = await cmds.send_login_sync(contact, password, timeout=timeout, min_timeout=5)
            return event is not None
        # Older meshcore_py: fire login and wait for LOGIN_SUCCESS ourselves
        from meshcore import EventType

        sent = await cmds.send_login(contact, password)
        if sent is None or sent.type == EventType.ERROR:
            return False
        wait_s = timeout or max(5.0, sent.payload.get("suggested_timeout", 8000) / 800)
        prefix = (contact.get("public_key") or "")[:12]
        event = await self.bot.meshcore.wait_for_event(
            EventType.LOGIN_SUCCESS, attribute_filters={"pubkey_prefix": prefix} if prefix else None, timeout=wait_s
        )
        return event is not None

    # ----------------------------------------------------------------- database

    def _init_db(self) -> None:
        with self.bot.db_manager.connection() as conn:
            store.ensure_tables(conn)

    def _store_sample(self, r: PollResult) -> None:
        s = r.status or {}
        try:
            self.bot.db_manager.execute_update(
                f"INSERT INTO {TABLE_SAMPLES} (ts, name, public_key, ok, bat_mv, uptime_s, noise_floor, "
                f"last_rssi, tx_queue, nb_recv, nb_sent, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time()), r.name, r.pubkey, 1 if r.ok else 0, r.bat_mv, s.get("uptime"),
                 s.get("noise_floor"), s.get("last_rssi"), s.get("tx_queue_len"), s.get("nb_recv"),
                 s.get("nb_sent"), r.error or None),
            )
        except Exception as e:
            self.logger.error(f"[RepeaterTelemetry] storing sample failed: {e}")
        if r.ok:
            self.logger.info(f"[RepeaterTelemetry] {r.name}: {r.bat_mv} mV")
        else:
            self.logger.warning(f"[RepeaterTelemetry] {r.name}: {r.error}")

    def _prune_old_samples(self) -> None:
        cutoff = int(time.time()) - self.retention_days * 86400
        self.bot.db_manager.execute_update(f"DELETE FROM {TABLE_SAMPLES} WHERE ts < ?", (cutoff,))

    def _get_state(self, name: str) -> dict[str, Any]:
        rows = self.bot.db_manager.execute_query(f"SELECT * FROM {TABLE_STATE} WHERE name = ?", (name,))
        return rows[0] if rows else {"name": name, "level": LEVEL_OK, "fails": 0, "offline": 0, "last_alert_ts": 0}

    def _save_state(self, st: dict[str, Any]) -> None:
        self.bot.db_manager.execute_update(
            f"INSERT INTO {TABLE_STATE} (name, level, fails, offline, last_alert_ts) VALUES (?,?,?,?,?) "
            f"ON CONFLICT(name) DO UPDATE SET level=excluded.level, fails=excluded.fails, "
            f"offline=excluded.offline, last_alert_ts=excluded.last_alert_ts",
            (st["name"], st["level"], st["fails"], st["offline"], st["last_alert_ts"]),
        )

    def latest_samples(self) -> list[dict[str, Any]]:
        """Latest successful sample per repeater plus current state (for the akku command)."""
        return self.bot.db_manager.execute_query(f"""
            SELECT s.name, s.bat_mv, s.ts, s.uptime_s, s.last_rssi, st.fails, st.offline, st.level
            FROM {TABLE_SAMPLES} s
            JOIN (SELECT name, MAX(ts) AS ts FROM {TABLE_SAMPLES} WHERE ok = 1 GROUP BY name) m
              ON s.name = m.name AND s.ts = m.ts AND s.ok = 1
            LEFT JOIN {TABLE_STATE} st ON st.name = s.name
            ORDER BY s.name""")

    # ------------------------------------------------------------------- alerts

    async def _evaluate(self, r: PollResult) -> None:
        st = self._get_state(r.name)
        now = int(time.time())
        messages: list[str] = []

        if not r.ok:
            st["fails"] = int(st["fails"]) + 1
            if self.offline_after and st["fails"] >= self.offline_after and not st["offline"]:
                st["offline"] = 1
                messages.append(self._fmt("offline", r.name, None, st["fails"]))
            else:
                self._maybe_repeat(st, now, messages, r)
            await self._finish(st, now, messages)
            return

        if st["offline"]:
            messages.append(self._fmt("online", r.name, r.bat_mv, 0))
        st["fails"] = 0
        st["offline"] = 0

        if r.bat_mv is not None:
            prev = st["level"] or LEVEL_OK
            level = next_level(prev, r.bat_mv, self.warn_mv, self.critical_mv, self.hysteresis_mv)
            if level != prev:
                # critical -> warn also reports, so everyone sees it improved
                messages.append(self._fmt("recovered" if level == LEVEL_OK else level, r.name, r.bat_mv, 0))
                st["level"] = level
            else:
                self._maybe_repeat(st, now, messages, r)
        await self._finish(st, now, messages)

    def _maybe_repeat(self, st: dict[str, Any], now: int, messages: list[str], r: PollResult) -> None:
        """Re-send an unchanged, still-active alert after repeat_alert_hours."""
        if not self.repeat_alert_s or now - int(st["last_alert_ts"]) < self.repeat_alert_s:
            return
        if st["offline"]:
            messages.append(self._fmt("offline", r.name, None, st["fails"]))
        elif st["level"] != LEVEL_OK and r.bat_mv is not None:
            messages.append(self._fmt(st["level"], r.name, r.bat_mv, 0))

    async def _finish(self, st: dict[str, Any], now: int, messages: list[str]) -> None:
        if messages:
            st["last_alert_ts"] = now
        self._save_state(st)
        for text in messages:
            await self.send_alert(text)

    def _fmt(self, kind: str, name: str, bat_mv: Optional[int], fails: int) -> str:
        volt = (bat_mv or 0) / 1000.0
        try:
            return self.templates[kind].format(name=name, volt=volt, mv=bat_mv or 0, fails=fails)
        except (KeyError, IndexError, ValueError) as e:
            self.logger.warning(f"[RepeaterTelemetry] bad template_{kind}: {e}")
            return DEFAULT_TEMPLATES[kind].format(name=name, volt=volt, mv=bat_mv or 0, fails=fails)

    async def send_alert(self, text: str) -> None:
        self.logger.info(f"[RepeaterTelemetry] alert: {text}")
        if not self.silence_mesh:
            for admin in self.admins:
                try:
                    await self.bot.bot_tx_rate_limiter.wait_for_tx()
                    await self.bot.command_manager.send_dm(admin, text, skip_user_rate_limit=True)
                except Exception as e:
                    self.logger.error(f"[RepeaterTelemetry] DM alert to admin {admin} failed: {e}")
            if self.alert_channel:
                try:
                    await self.bot.command_manager.send_channel_message(
                        self.alert_channel, text, skip_user_rate_limit=True, scope=self.get_mesh_flood_scope()
                    )
                except Exception as e:
                    self.logger.error(f"[RepeaterTelemetry] channel alert failed: {e}")
        await self.send_external_notifications(text)
