"""Shared storage for the repeater telemetry feature.

Used by both the bot process (``RepeaterTelemetryService``) and the web viewer
(``modules/web_viewer/repeater_telemetry_routes.py``), which may run as separate
processes and only share the SQLite database.

Settings resolve in three layers: built-in defaults, then ``[RepeaterTelemetry_Service]``
in config.ini, then values saved from the web page (table
``repeater_telemetry_settings``).  Web values win, so the page can change
everything at runtime without a config reload or bot restart.
"""

from __future__ import annotations

import configparser
import json
import sqlite3
import time
from typing import Any

CONFIG_SECTION = "RepeaterTelemetry_Service"

TABLE_SAMPLES = "repeater_telemetry"
TABLE_STATE = "repeater_telemetry_state"
TABLE_SETTINGS = "repeater_telemetry_settings"

MAX_ADMINS = 5
MAX_REPEATERS = 25

# Settings key used to ask the bot for an immediate poll (value: request timestamp)
POLL_REQUEST_KEY = "_poll_requested_at"

DEFAULT_TEMPLATES = {
    "warn": "⚠️ Akku niedrig: {name} {volt:.2f} V",
    "critical": "🪫 Akku KRITISCH: {name} {volt:.2f} V",
    "recovered": "🔋 Akku wieder ok: {name} {volt:.2f} V",
    "offline": "📴 Repeater {name} antwortet nicht ({fails}x)",
    "online": "📶 Repeater {name} wieder erreichbar ({volt:.2f} V)",
}

# key -> (type, default, min, max)
SCALAR_SETTINGS: dict[str, tuple[str, Any, float | None, float | None]] = {
    "paused": ("bool", False, None, None),
    "interval_minutes": ("int", 60, 5, 1440),
    "delay_between_seconds": ("int", 30, 5, 600),
    "initial_delay_seconds": ("int", 120, 0, 3600),
    "request_timeout_seconds": ("int", 0, 0, 120),
    "warn_mv": ("int", 3500, 0, 30000),
    "critical_mv": ("int", 3300, 0, 30000),
    "hysteresis_mv": ("int", 100, 0, 2000),
    "repeat_alert_hours": ("float", 12.0, 0, 168),
    "offline_after_failures": ("int", 3, 0, 100),
    "alert_channel": ("str", "", None, None),
    "silence_mesh_output": ("bool", False, None, None),
    "retention_days": ("int", 90, 1, 3650),
}
TEMPLATE_KEYS = [f"template_{k}" for k in DEFAULT_TEMPLATES]

_TRUE = {"1", "true", "yes", "on"}


class SettingsError(ValueError):
    """Invalid settings submitted (message is user-facing, German)."""


# --------------------------------------------------------------------- schema

def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_SAMPLES} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            name TEXT NOT NULL,
            public_key TEXT,
            ok INTEGER NOT NULL,
            bat_mv INTEGER,
            uptime_s INTEGER,
            noise_floor INTEGER,
            last_rssi INTEGER,
            tx_queue INTEGER,
            nb_recv INTEGER,
            nb_sent INTEGER,
            error TEXT
        )""")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE_SAMPLES}_name_ts ON {TABLE_SAMPLES}(name, ts)")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_STATE} (
            name TEXT PRIMARY KEY,
            level TEXT NOT NULL DEFAULT 'ok',
            fails INTEGER NOT NULL DEFAULT 0,
            offline INTEGER NOT NULL DEFAULT 0,
            last_alert_ts INTEGER NOT NULL DEFAULT 0
        )""")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_SETTINGS} (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
    conn.commit()


# ------------------------------------------------------------------- parsing

def parse_repeaters(raw: str) -> list[dict[str, str]]:
    """Parse ``name:password, name2`` (password = everything after the first colon)."""
    out: list[dict[str, str]] = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, password = entry.partition(":")
        name = name.strip()
        if name:
            out.append({"name": name, "password": password.strip() if sep else ""})
    return out


def _coerce(key: str, raw: Any) -> Any:
    typ, default, lo, hi = SCALAR_SETTINGS[key]
    if typ == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in _TRUE
    if typ == "str":
        return "" if raw is None else str(raw).strip()
    try:
        val: float = float(raw)
    except (TypeError, ValueError):
        raise SettingsError(f"{key}: keine gültige Zahl")
    if lo is not None and val < lo:
        raise SettingsError(f"{key}: Wert muss mindestens {lo:g} sein")
    if hi is not None and val > hi:
        raise SettingsError(f"{key}: Wert darf höchstens {hi:g} sein")
    return int(val) if typ == "int" else val


def _clean_name(name: Any) -> str:
    name = "" if name is None else str(name).strip()
    if any(c in name for c in ",:\n\r"):
        raise SettingsError(f"Name '{name}' darf kein Komma, keinen Doppelpunkt und keinen Zeilenumbruch enthalten")
    return name


def _defaults() -> dict[str, Any]:
    s: dict[str, Any] = {k: v[1] for k, v in SCALAR_SETTINGS.items()}
    s["repeaters"] = []
    s["admins"] = []
    for k in DEFAULT_TEMPLATES:
        s[f"template_{k}"] = DEFAULT_TEMPLATES[k]
    return s


def _from_config(config: configparser.ConfigParser | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if config is None or not config.has_section(CONFIG_SECTION):
        return out
    get = lambda k: (config.get(CONFIG_SECTION, k, fallback="", raw=True) or "").strip()  # noqa: E731
    for key in SCALAR_SETTINGS:
        raw = get(key)
        if raw == "":
            continue
        try:
            out[key] = _coerce(key, raw)
        except SettingsError:
            pass  # invalid ini values fall back to defaults
    repeaters = get("repeaters")
    if repeaters:
        out["repeaters"] = parse_repeaters(repeaters)
    admins = get("admins") or get("alert_dm")
    if admins:
        out["admins"] = [a.strip() for a in admins.split(",") if a.strip()][:MAX_ADMINS]
    for key in TEMPLATE_KEYS:
        raw = get(key)
        if raw:
            out[key] = raw
    return out


def _from_db(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        rows = conn.execute(f"SELECT key, value FROM {TABLE_SETTINGS}").fetchall()
    except sqlite3.OperationalError:
        return out  # table not created yet
    for key, value in rows:
        try:
            out[key] = json.loads(value)
        except (TypeError, ValueError):
            continue
    return out


def settings_from_config(config: configparser.ConfigParser | None) -> dict[str, Any]:
    """Defaults <- config.ini, without web overrides."""
    s = _defaults()
    s.update(_from_config(config))
    s["web_managed"] = False
    s[POLL_REQUEST_KEY] = 0
    return s


def load_settings(conn: sqlite3.Connection, config: configparser.ConfigParser | None) -> dict[str, Any]:
    """Effective settings: defaults <- config.ini <- web (DB)."""
    s = settings_from_config(config)
    db = _from_db(conn)
    for key in list(SCALAR_SETTINGS) + TEMPLATE_KEYS + ["repeaters", "admins"]:
        if key in db:
            s[key] = db[key]
    s["admins"] = list(s.get("admins") or [])[:MAX_ADMINS]
    s["web_managed"] = bool(set(db) - {POLL_REQUEST_KEY})
    s[POLL_REQUEST_KEY] = db.get(POLL_REQUEST_KEY, 0)
    return s


# ------------------------------------------------------------------- writing

def validate_submission(data: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Validate settings posted by the web page; returns values to store.

    Repeater passwords left blank keep the currently stored password, so the
    page never has to receive the plaintext secret.
    """
    out: dict[str, Any] = {}
    for key in SCALAR_SETTINGS:
        if key in data:
            out[key] = _coerce(key, data[key])

    if "repeaters" in data:
        old_pw = {r["name"].lower(): r.get("password", "") for r in current.get("repeaters", [])}
        repeaters: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in data.get("repeaters") or []:
            name = _clean_name((row or {}).get("name"))
            if not name:
                continue
            if name.lower() in seen:
                raise SettingsError(f"Repeater '{name}' ist doppelt eingetragen")
            seen.add(name.lower())
            pw = (row or {}).get("password")
            if (row or {}).get("clear_password"):
                pw = ""
            elif pw is None or pw == "":
                pw = old_pw.get(name.lower(), "")
            repeaters.append({"name": name, "password": str(pw)})
        if len(repeaters) > MAX_REPEATERS:
            raise SettingsError(f"Höchstens {MAX_REPEATERS} Repeater möglich")
        out["repeaters"] = repeaters

    if "admins" in data:
        admins: list[str] = []
        for a in data.get("admins") or []:
            name = _clean_name(a)
            if name and name.lower() not in {x.lower() for x in admins}:
                admins.append(name)
        if len(admins) > MAX_ADMINS:
            raise SettingsError(f"Höchstens {MAX_ADMINS} Admins möglich")
        out["admins"] = admins

    for key in TEMPLATE_KEYS:
        if key in data:
            tpl = str(data[key] or "").strip() or DEFAULT_TEMPLATES[key[len("template_"):]]
            try:
                tpl.format(name="X", volt=3.7, mv=3700, fails=1)
            except (KeyError, IndexError, ValueError) as e:
                raise SettingsError(f"Vorlage {key} ist ungültig: {e}")
            out[key] = tpl

    warn = out.get("warn_mv", current.get("warn_mv"))
    crit = out.get("critical_mv", current.get("critical_mv"))
    if warn is not None and crit is not None and crit > warn:
        raise SettingsError("Die kritische Schwelle muss unter der Warnschwelle liegen")
    return out


def save_settings(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    ensure_tables(conn)
    conn.executemany(
        f"INSERT INTO {TABLE_SETTINGS} (key, value) VALUES (?, ?) "
        f"ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [(k, json.dumps(v, ensure_ascii=False)) for k, v in values.items()],
    )
    conn.commit()


def reset_web_settings(conn: sqlite3.Connection) -> None:
    """Forget web overrides so config.ini applies again."""
    ensure_tables(conn)
    conn.execute(f"DELETE FROM {TABLE_SETTINGS} WHERE key != ?", (POLL_REQUEST_KEY,))
    conn.commit()


def request_poll(conn: sqlite3.Connection) -> None:
    save_settings(conn, {POLL_REQUEST_KEY: int(time.time())})


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Settings safe to send to the browser (no passwords)."""
    s = {k: v for k, v in settings.items() if k != "repeaters"}
    s["repeaters"] = [
        {"name": r["name"], "has_password": bool(r.get("password"))} for r in settings.get("repeaters", [])
    ]
    return s


# -------------------------------------------------------------------- reading

def status_overview(conn: sqlite3.Connection, names: list[str]) -> list[dict[str, Any]]:
    """Per configured repeater: latest good sample, last attempt and alert state."""
    conn.row_factory = sqlite3.Row
    out = []
    for name in names:
        good = conn.execute(
            f"SELECT ts, bat_mv, uptime_s, last_rssi, noise_floor FROM {TABLE_SAMPLES} "
            f"WHERE name = ? AND ok = 1 ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
        last = conn.execute(
            f"SELECT ts, ok, error FROM {TABLE_SAMPLES} WHERE name = ? ORDER BY ts DESC LIMIT 1",
            (name,)).fetchone()
        state = conn.execute(f"SELECT level, fails, offline FROM {TABLE_STATE} WHERE name = ?",
                             (name,)).fetchone()
        out.append({
            "name": name,
            "bat_mv": good["bat_mv"] if good else None,
            "sample_ts": good["ts"] if good else None,
            "uptime_s": good["uptime_s"] if good else None,
            "last_rssi": good["last_rssi"] if good else None,
            "noise_floor": good["noise_floor"] if good else None,
            "last_try_ts": last["ts"] if last else None,
            "last_ok": bool(last["ok"]) if last else None,
            "last_error": last["error"] if last else None,
            "level": state["level"] if state else "ok",
            "fails": state["fails"] if state else 0,
            "offline": bool(state["offline"]) if state else False,
        })
    return out


def history(conn: sqlite3.Connection, name: str, days: int) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    since = int(time.time()) - max(1, days) * 86400
    rows = conn.execute(
        f"SELECT ts, ok, bat_mv, last_rssi FROM {TABLE_SAMPLES} WHERE name = ? AND ts >= ? ORDER BY ts",
        (name, since)).fetchall()
    return [dict(r) for r in rows]
