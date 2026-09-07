#!/usr/bin/env python3
"""
Machine-readable config schema for meshcore-bot.

Provides section/key metadata used by config validation and kept in sync with
config.ini.example. Grow incrementally as new options are added.
"""

from __future__ import annotations

import configparser
import difflib
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from modules.config_validation import (
    CANONICAL_NON_COMMAND_SECTIONS,
    REQUIRED_SECTIONS,
    SEVERITY_WARNING,
)

# Sections where any user-defined key is valid (no fixed schema).
DYNAMIC_KEY_SECTIONS = frozenset({
    "Keywords",
    "Custom_Syntax",
    "RandomLine",
    "Scheduled_Messages",
    "Channels_List",
    "Plugin_Overrides",
    "Service_Overrides",
    "Rate_Limits",
    "Banned_Users",
    "Admin_ACL",
})

# Legacy section names still accepted at runtime.
DEPRECATED_SECTIONS = frozenset({"Jokes"})

# Legacy key suffixes (e.g. alert_enabled instead of enabled in *_Command sections).
LEGACY_ENABLED_KEY_RE = re.compile(r"^[a-z]+_enabled$")

# Legacy aliases for a plugin's canonical `[Section] enabled` key, tried in
# order when the canonical key is absent. Single source of truth shared by
# BaseCommand.get_config_value (runtime reads) and the web settings view
# (modules/settings_schema.read_enabled) — keep additions here so runtime
# behavior and the UI can never disagree.
# Maps canonical section -> ordered ((legacy_section, legacy_key), ...).
LEGACY_ENABLED_ALIASES: dict[str, tuple[tuple[str, str], ...]] = {
    "Joke_Command": (("Jokes", "joke_enabled"),),
    "DadJoke_Command": (("Jokes", "dadjoke_enabled"),),
    "Stats_Command": (("Stats_Command", "stats_enabled"), ("Stats", "stats_enabled")),
    "Sports_Command": (("Sports_Command", "sports_enabled"), ("Sports", "sports_enabled")),
    "Hacker_Command": (("Hacker_Command", "hacker_enabled"), ("Hacker", "hacker_enabled")),
    "Alert_Command": (("Alert_Command", "alert_enabled"),),
}

# Keys supported on every *_Command section via BaseCommand (config.ini.example may omit them).
STANDARD_COMMAND_KEYS = frozenset({
    "enabled",
    "channels",
    "aliases",
    "cooldown_queue_threshold_seconds",
    "require_path_bytes_greater_or_equal_to",
    "require_path_bytes_failure_response",
})

# Keys supported on service plugin sections via BaseServicePlugin.
STANDARD_SERVICE_KEYS = frozenset({
    "enabled",
    "flood_scope",
    "discord_webhook_urls",
    "telegram_chat_ids",
    "telegram_bot_token",
    "silence_mesh_output",
})

# Mesh channel → external target mappings in bridge service sections.
BRIDGE_SECTIONS = frozenset({"DiscordBridge", "TelegramBridge"})
BRIDGE_CHANNEL_KEY_PREFIX = "bridge."

# User-defined announcement triggers in Announcements_Command.
ANNOUNCEMENTS_TRIGGER_KEY_PREFIX = "announce."

# PulsePoint agency mappings (city, county, and legacy formats). These live in
# the PulsePoint alert service section; the legacy [Alert_Command] location is
# still accepted for backward compatibility.
ALERT_AGENCY_KEY_PREFIXES = ("agency.", "agency_")
ALERT_AGENCY_SECTIONS = ("Alert_Command", "PulsePoint_Alert_Service")

# Numbered MQTT broker keys in [PacketCapture]: mqtt1_server, mqtt2_server, ...
MQTT_BROKER_KEY_RE = re.compile(r"^mqtt\d+_(.+)$")

# (section, key prefix) pairs where the suffix is an operator-chosen name, so
# config.ini.example can only ever document sample entries. Keep in sync with the
# code that reads them:
#   flood_scope.<channel>       modules/models.py, modules/command_manager.py
#   custom.wxsim.<location>     modules/commands/wx_command.py
#   custom.mqtt_weather.<name>  modules/clients/mqtt_weather.py
#   flood_scope.<region-id>     modules/service_plugins/darc_mowas_service.py
DYNAMIC_SUFFIX_KEY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("Channels", "flood_scope."),
    ("Weather", "custom.wxsim."),
    ("Weather", "custom.mqtt_weather."),
    ("DARC_MoWaS_Service", "flood_scope."),
)

# Known wrong key names, per section, with the canonical spelling to suggest.
# Generic near-miss typos are caught by difflib; this is for names that are
# plausible but simply wrong (a user carrying habits over from other tools).
KEY_TYPO_SUGGESTIONS: dict[str, dict[str, str]] = {
    "Connection": {"host": "hostname", "port": "tcp_port"},
}

_VALID_CONFIG_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# A bare number is never a real key — it's comment prose like "# 0 = unlimited".
_NUMERIC_KEY_RE = re.compile(r"^\d+(\.\d+)?$")


@dataclass
class KeyMeta:
  """Metadata for a single config key."""

  type: str = "string"
  values: Optional[tuple[str, ...]] = None
  default: Optional[str] = None
  required: bool = False
  required_when: Optional[dict[str, str]] = None
  deprecated: bool = False
  deprecated_message: str = ""


@dataclass
class SectionMeta:
  """Metadata for a config section."""

  keys: dict[str, KeyMeta] = field(default_factory=dict)
  dynamic_keys: bool = False


# Incremental schema — expand as features are added.
SECTIONS: dict[str, SectionMeta] = {
    "Connection": SectionMeta(keys={
        "connection_type": KeyMeta(
            type="enum",
            values=("serial", "ble", "tcp"),
            required=True,
        ),
        "serial_port": KeyMeta(required_when={"connection_type": "serial"}),
        "ble_device_name": KeyMeta(),
        "hostname": KeyMeta(required_when={"connection_type": "tcp"}),
        "tcp_port": KeyMeta(type="int", default="5000"),
        "timeout": KeyMeta(type="int"),
        "radio_debug": KeyMeta(type="bool"),
        "reconnect_max_retries": KeyMeta(type="int"),
        "reconnect_delay_seconds": KeyMeta(type="int"),
        "reconnect_max_delay_seconds": KeyMeta(type="int"),
        "command_min_interval_ms": KeyMeta(type="int"),
        "channel_fetch_interval_ms": KeyMeta(type="int"),
    }),
    "Bot": SectionMeta(keys={
        "bot_name": KeyMeta(required=True),
        "enabled": KeyMeta(type="bool"),
        "db_path": KeyMeta(),
        "local_dir_path": KeyMeta(),
        # Advanced tuning knobs the code reads but config.ini.example does not
        # show. Listed here so unknown-key validation doesn't flag them; see
        # tests/test_config_schema_sync.py::test_every_config_read_key_is_known.
        "cooldown_queue_threshold_seconds": KeyMeta(type="float", default="5.0"),
        "disconnect_timeout_seconds": KeyMeta(type="float", default="10.0"),
        "nominatim_rate_limit_seconds": KeyMeta(type="float", default="1.1"),
        "persist_settings": KeyMeta(default="config.ini"),
        "radio_offline_threshold": KeyMeta(type="int", default="3"),
        "radio_probe_fail_threshold": KeyMeta(type="int", default="3"),
        "radio_probe_interval_seconds": KeyMeta(type="int", default="300"),
        "radio_offline_alert_enabled": KeyMeta(type="bool", default="false"),
        "radio_offline_alert_email": KeyMeta(),
        "radio_zombie_alert_enabled": KeyMeta(type="bool", default="false"),
        "radio_zombie_alert_email": KeyMeta(),
    }),
    "Channels": SectionMeta(keys={
        "monitor_channels": KeyMeta(required=True),
        "respond_to_dms": KeyMeta(type="bool"),
        "max_response_hops": KeyMeta(type="int"),
        "channel_keywords": KeyMeta(),
        "flood_scopes": KeyMeta(),
        "outgoing_flood_scope_override": KeyMeta(),
    }),
    "Web_Viewer": SectionMeta(keys={
        "enabled": KeyMeta(type="bool"),
        "host": KeyMeta(),
        "port": KeyMeta(type="int"),
        "web_viewer_password": KeyMeta(),
        "auto_start": KeyMeta(type="bool"),
        "debug": KeyMeta(type="bool"),
        # Advanced; read by the viewer but not shown in config.ini.example.
        "cors_allowed_origins": KeyMeta(),
        "packet_stream_write_queue_max": KeyMeta(type="int", default="1000"),
        "restore_max_bytes": KeyMeta(type="int"),
        "sqlite_busy_timeout_ms": KeyMeta(type="int", default="60000"),
        "sqlite_foreign_keys": KeyMeta(type="bool", default="true"),
        "sqlite_journal_mode": KeyMeta(default="WAL"),
        "mesh_graph_cache_seconds": KeyMeta(type="int", default="30"),
        # Dashboard snapshot refresher — moves the landing page's aggregate
        # queries off the request path and accumulates trends that outlive the
        # raw tables' retention.
        "dashboard_snapshot_enabled": KeyMeta(type="bool", default="true"),
        "dashboard_snapshot_interval_seconds": KeyMeta(type="int", default="60"),
        "dashboard_snapshot_history_days": KeyMeta(type="int", default="400"),
        "dashboard_packet_backfill_rows": KeyMeta(type="int", default="2000"),
    }),
    "Localization": SectionMeta(keys={
        "language": KeyMeta(default="en"),
        "translation_path": KeyMeta(default="translations/"),
        "local_translation_path": KeyMeta(default="<local_dir_path>/translations"),
        "auto_detect_language": KeyMeta(type="bool", default="false"),
    }),
    "Webhook": SectionMeta(keys={
        "enabled": KeyMeta(type="bool"),
        "host": KeyMeta(),
        "port": KeyMeta(type="int"),
        "secret_token": KeyMeta(),
        "rate_limit_per_minute": KeyMeta(type="int", default="30"),
    }),
    "Feed_Manager": SectionMeta(keys={
        "feed_manager_enabled": KeyMeta(type="bool"),
        "allow_private_urls": KeyMeta(type="bool"),
        "max_response_bytes": KeyMeta(type="int", default="2097152"),
        "max_parsed_items": KeyMeta(type="int", default="500"),
        # Typed so validation catches a non-integer before getint() raises in
        # FeedManager.__init__ — that ValueError leaves feed_manager as None and
        # silently stops every feed.
        "max_items_per_check": KeyMeta(type="int", default="10"),
        "max_posts_per_check": KeyMeta(type="int", default="10"),
        "feed_request_timeout": KeyMeta(type="int", default="30"),
        "max_message_length": KeyMeta(type="int", default="130"),
        "default_check_interval_seconds": KeyMeta(type="int", default="300"),
    }),
    "Weather_Service": SectionMeta(keys={
        "enabled": KeyMeta(type="bool"),
        "rain_nowcast_cache_seconds": KeyMeta(type="int", default="300"),
        # Siblings of the documented rain_nowcast_* keys, read but not shown.
        "rain_nowcast_show_amount": KeyMeta(type="bool", default="true"),
        "rain_nowcast_amount_unit": KeyMeta(type="enum", values=("in", "mm"), default="in"),
        "rain_nowcast_min_probability": KeyMeta(type="int", default="50"),
    }),
    "Path_Command": SectionMeta(keys={
        "geographic_scoring_enabled": KeyMeta(type="bool", default="true"),
    }),
    "Rain_Command": SectionMeta(keys={
        "amount_unit": KeyMeta(type="enum", values=("in", "mm"), default="in"),
    }),
    "RepeaterPrefixCollision_Service": SectionMeta(keys={
        "enabled": KeyMeta(type="bool"),
        "channels": KeyMeta(),
        "notify_external_on_all_new_repeaters": KeyMeta(type="bool"),
        "silence_mesh_output": KeyMeta(type="bool"),
        "discord_webhook_urls": KeyMeta(),
        "telegram_chat_ids": KeyMeta(),
        "telegram_bot_token": KeyMeta(),
    }),
    "Greeter_Command": SectionMeta(keys={
        "enabled": KeyMeta(type="bool"),
        "dead_air_delay_seconds": KeyMeta(type="int", default="0"),
        "defer_to_human_greeting": KeyMeta(type="bool"),
        "levenshtein_distance": KeyMeta(type="int", default="0"),
    }),
    "Announcements_Command": SectionMeta(dynamic_keys=True),
    "Alert_Command": SectionMeta(dynamic_keys=True),
}

for _section in DYNAMIC_KEY_SECTIONS:
    if _section not in SECTIONS:
        SECTIONS[_section] = SectionMeta(dynamic_keys=True)

for _section in CANONICAL_NON_COMMAND_SECTIONS:
    SECTIONS.setdefault(_section, SectionMeta())


def _parse_key_value_line(stripped: str) -> Optional[tuple[str, str]]:
    """Parse active or commented ``key = value`` lines."""
    if not stripped or stripped.startswith(";"):
        return None
    commented = stripped.startswith("#")
    body = stripped[1:].strip() if commented else stripped
    if "=" not in body:
        return None
    key, _, value = body.partition("=")
    key = key.strip()
    if not key or key.startswith("[") or not _VALID_CONFIG_KEY_RE.match(key):
        return None
    # Prose in comments parses as a key/value pair: "# 0 = unlimited" would
    # document a key named "0" and then accept it in a real config.
    if _NUMERIC_KEY_RE.match(key):
        return None
    return key, value.strip()


def is_command_section(section: str) -> bool:
    """Return True if section is a command plugin section."""
    return section.endswith("_Command")


def is_service_section(section: str) -> bool:
    """Return True if section is a background service plugin section."""
    return section.endswith("_Service")


def is_bridge_channel_key(section: str, key: str) -> bool:
    """Return True for dynamic bridge.<mesh_channel> mapping keys."""
    return section in BRIDGE_SECTIONS and key.startswith(BRIDGE_CHANNEL_KEY_PREFIX)


def is_announcements_trigger_key(section: str, key: str) -> bool:
    """Return True for dynamic announce.<trigger_name> keys."""
    return (
        section == "Announcements_Command"
        and key.startswith(ANNOUNCEMENTS_TRIGGER_KEY_PREFIX)
        and len(key) > len(ANNOUNCEMENTS_TRIGGER_KEY_PREFIX)
    )


def is_alert_agency_key(section: str, key: str) -> bool:
    """Return True for dynamic agency.city.* / agency.county.* / legacy agency keys."""
    return section in ALERT_AGENCY_SECTIONS and key.startswith(ALERT_AGENCY_KEY_PREFIXES)


def is_dynamic_suffix_key(section: str, key: str) -> bool:
    """Return True for user-named keys under a fixed prefix in a fixed section.

    These sections mix a fixed schema with an open-ended family whose suffix the
    operator chooses (a mesh channel name, a weather station label, a German
    Regionalschlüssel), so the example can only ever show sample entries.
    """
    for allowed_section, prefix in DYNAMIC_SUFFIX_KEY_PREFIXES:
        if section == allowed_section and key.startswith(prefix) and len(key) > len(prefix):
            return True
    return False


def is_mqtt_broker_key(
    section: str,
    key: str,
    example_keys: dict[str, dict[str, str]],
) -> bool:
    """Return True for numbered [PacketCapture] mqtt<N>_* broker keys.

    config.ini.example documents broker 1 concretely plus an ``mqttN_*``
    placeholder block, but packet_capture_service reads any broker index, so
    match the suffix against either spelling rather than the literal key.
    """
    if section != "PacketCapture":
        return False
    match = MQTT_BROKER_KEY_RE.match(key)
    if not match:
        return False
    suffix = match.group(1)
    documented = {k.lower() for k in example_keys.get(section, {})}
    return f"mqttn_{suffix}" in documented or f"mqtt1_{suffix}" in documented


def is_known_config_key(
    section: str,
    key: str,
    example_keys: dict[str, dict[str, str]],
) -> bool:
    """Return True if key is documented or valid for this section type."""
    if section in DYNAMIC_KEY_SECTIONS:
        return True
    section_meta = SECTIONS.get(section)
    if section_meta and section_meta.dynamic_keys:
        return True
    ex_sec = example_keys.get(section, {})
    if not ex_sec and section in example_keys:
        return True
    # Compare case-folded throughout: ConfigParser lowercases keys read from a user
    # config, the example is parsed case-preserving (e.g. bridge.Public), and
    # scripts/config_tui.py reads with optionxform = str.
    lowered = key.lower()
    if lowered in {k.lower() for k in ex_sec}:
        return True
    if section_meta and lowered in {k.lower() for k in section_meta.keys}:
        return True
    if is_mqtt_broker_key(section, lowered, example_keys):
        return True
    if is_dynamic_suffix_key(section, lowered):
        return True
    if is_command_section(section):
        if lowered in STANDARD_COMMAND_KEYS:
            return True
        if LEGACY_ENABLED_KEY_RE.match(lowered):
            return True
    if is_service_section(section) and lowered in STANDARD_SERVICE_KEYS:
        return True
    if is_bridge_channel_key(section, lowered):
        return True
    if is_announcements_trigger_key(section, lowered):
        return True
    if is_alert_agency_key(section, lowered):
        return True
    return False


def load_documented_keys_from_example(example_path: Path) -> dict[str, dict[str, str]]:
    """Return all documented keys from example (active and commented ``#key =`` lines)."""
    keys: dict[str, dict[str, str]] = {}
    if not example_path.exists():
        return keys

    # Active keys via ConfigParser
    try:
        cfg = configparser.ConfigParser(allow_no_value=True)
        cfg.optionxform = str  # type: ignore[assignment]
        cfg.read(str(example_path), encoding="utf-8")
        for section in cfg.sections():
            try:
                keys[section] = dict(cfg.items(section))
            except configparser.Error:
                keys[section] = {}
    except (configparser.Error, OSError, UnicodeDecodeError):
        keys = {}

    current_section = ""
    try:
        with open(example_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    current_section = stripped[1:-1]
                    keys.setdefault(current_section, {})
                    continue
                if not current_section:
                    continue
                parsed = _parse_key_value_line(stripped)
                if parsed:
                    key, value = parsed
                    keys.setdefault(current_section, {}).setdefault(key, value)
    except OSError:
        pass
    return keys


@lru_cache(maxsize=1)
def _shipped_example_keys() -> dict[str, dict[str, str]]:
    """Documented keys from the shipped config.ini.example (parsed once).

    The example is the main source of known key names, so an install that lacks
    it (a plain wheel — it sits at the repo root, outside the package) quietly
    falls back to SECTIONS alone and checks far fewer keys. That degradation is
    safe (fewer checks, not wrong ones) but should not be invisible.
    """
    example_path = Path(__file__).resolve().parent.parent / "config.ini.example"
    keys = load_documented_keys_from_example(example_path)
    if not keys:
        logging.getLogger(__name__).info(
            "config.ini.example not found at %s; unknown-key validation is limited "
            "to keys declared in the schema.",
            example_path,
        )
    return keys


def _section_has_documented_keys(
    section: str,
    section_meta: Optional[SectionMeta],
    example_keys: dict[str, dict[str, str]],
) -> bool:
    """True when we know enough about a section to call one of its keys unknown.

    Sections absent from both the example and the schema belong to local or
    third-party plugins whose keys we have no list for; flagging every one of
    them would bury the real typos. The unknown-*section* check already reports
    those separately.
    """
    if example_keys.get(section):
        return True
    return bool(section_meta and section_meta.keys)


def _suggest_similar_key(key: str, candidates: set[str]) -> Optional[str]:
    """Closest documented key name to `key`, if one is near enough to be a typo."""
    matches = difflib.get_close_matches(key, sorted(candidates), n=1, cutoff=0.8)
    return matches[0] if matches else None


def _known_keys_for_suggestions(
    section: str,
    section_meta: Optional[SectionMeta],
    example_keys: dict[str, dict[str, str]],
) -> set[str]:
    """Candidate key names for 'did you mean' on an unknown key."""
    candidates = {k.lower() for k in example_keys.get(section, {})}
    if section_meta:
        candidates.update(section_meta.keys)
    if is_command_section(section):
        candidates.update(STANDARD_COMMAND_KEYS)
    if is_service_section(section):
        candidates.update(STANDARD_SERVICE_KEYS)
    return candidates


def get_example_sections(example_path: Path) -> list[str]:
    """Return section names in file order from config.ini.example."""
    sections: list[str] = []
    if not example_path.exists():
        return sections
    try:
        with open(example_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    sections.append(stripped[1:-1])
    except OSError:
        pass
    return sections


def _typed_key_results(
    section: str, key: str, value: str, meta: KeyMeta
) -> list[tuple[str, str]]:
    """Deprecation and type/enum diagnostics for a key that IS in the schema."""
    results: list[tuple[str, str]] = []
    if meta.deprecated:
        msg = meta.deprecated_message or f"Key '{key}' is deprecated."
        results.append((SEVERITY_WARNING, f"[{section}] {msg}"))
    if not value:
        return results
    if meta.type == "enum" and meta.values:
        if value.lower() not in meta.values:
            allowed = ", ".join(meta.values)
            results.append((
                SEVERITY_WARNING,
                f"[{section}] '{key}' must be one of: {allowed} (got '{value}').",
            ))
    elif meta.type == "int":
        try:
            int(value)
        except ValueError:
            results.append((
                SEVERITY_WARNING,
                f"[{section}] '{key}' should be an integer (got '{value}').",
            ))
    elif meta.type == "float":
        try:
            float(value)
        except ValueError:
            results.append((
                SEVERITY_WARNING,
                f"[{section}] '{key}' should be a number (got '{value}').",
            ))
    elif meta.type == "bool":
        if value.lower() not in ("true", "false", "yes", "no", "1", "0", "on", "off"):
            results.append((
                SEVERITY_WARNING,
                f"[{section}] '{key}' should be a boolean (got '{value}').",
            ))
    return results


def _unknown_key_result(
    section: str, key: str, suggestions: set[str]
) -> tuple[str, str]:
    """Diagnostic for a key that matches nothing documented for this section."""
    typo = KEY_TYPO_SUGGESTIONS.get(section, {}).get(key)
    if typo:
        detail = (
            f"use {typo} for TCP connections."
            if section == "Connection"
            else f"use '{typo}'."
        )
        return (SEVERITY_WARNING, f"[{section}] '{key}' is not valid; {detail}")
    similar = _suggest_similar_key(key, suggestions)
    hint = (
        f" Did you mean '{similar}'?"
        if similar
        else " (not documented in config.ini.example)"
    )
    return (SEVERITY_WARNING, f"[{section}] unknown key '{key}'.{hint}")


def validate_config_keys(config: configparser.ConfigParser) -> list[tuple[str, str]]:
    """
    Validate config keys against schema metadata.

    Returns list of (severity, message). Warnings only — does not block startup.
    """
    results: list[tuple[str, str]] = []
    example_keys = _shipped_example_keys()

    for section in config.sections():
        section_stripped = section.strip()
        if not section_stripped:
            continue

        if section_stripped in DEPRECATED_SECTIONS:
            results.append((
                SEVERITY_WARNING,
                f"Deprecated section [{section_stripped}]; migrate options to "
                f"[Joke_Command] / [DadJoke_Command].",
            ))

        section_meta = SECTIONS.get(section_stripped)
        is_command = section_stripped.endswith("_Command")
        is_dynamic = (
            section_stripped in DYNAMIC_KEY_SECTIONS
            or bool(section_meta and section_meta.dynamic_keys)
        )

        try:
            # config.options() folds in [DEFAULT], which would report one stray
            # global key once per section in the file.
            defaults = set(config.defaults())
            options = [k for k in config.options(section) if k not in defaults]
        except configparser.Error:
            continue

        if is_command:
            # Per-command legacy enabled keys
            for key in options:
                if LEGACY_ENABLED_KEY_RE.match(key) and key != "enabled":
                    if config.has_option(section, "enabled"):
                        results.append((
                            SEVERITY_WARNING,
                            f"[{section_stripped}] legacy key '{key}' is redundant; "
                            f"use 'enabled' instead.",
                        ))

        check_unknown_keys = not is_dynamic and _section_has_documented_keys(
            section_stripped, section_meta, example_keys
        )
        suggestions = (
            _known_keys_for_suggestions(section_stripped, section_meta, example_keys)
            if check_unknown_keys
            else set()
        )

        known_keys = section_meta.keys if section_meta else {}
        for key in options:
            meta = known_keys.get(key)
            if meta is not None:
                # raw=True: a literal '%' in any value raises InterpolationError,
                # which startup swallows as "validation skipped" — losing every
                # diagnostic over one unrelated password.
                try:
                    value = config.get(section, key, fallback="", raw=True).strip()
                except configparser.Error:
                    continue
                results.extend(_typed_key_results(section_stripped, key, value, meta))
            elif check_unknown_keys and not is_known_config_key(
                section_stripped, key, example_keys
            ):
                results.append(
                    _unknown_key_result(section_stripped, key, suggestions)
                )

    # Conditional required keys
    if config.has_section("Connection"):
        conn_type = config.get("Connection", "connection_type", fallback="serial").lower()
        conn_meta = SECTIONS.get("Connection")
        if conn_meta:
            for key, meta in conn_meta.keys.items():
                if not meta.required_when:
                    continue
                if meta.required_when.get("connection_type") != conn_type:
                    continue
                if not config.has_option("Connection", key):
                    continue
                value = config.get("Connection", key, fallback="").strip()
                if not value:
                    results.append((
                        SEVERITY_WARNING,
                        f"[Connection] '{key}' is required when connection_type = {conn_type}.",
                    ))

    return results


def canonical_sections_missing_from_example(example_path: Path) -> list[str]:
    """Return canonical non-command sections absent from config.ini.example."""
    present = set(get_example_sections(example_path))
    return sorted(CANONICAL_NON_COMMAND_SECTIONS - present)


__all__ = [
    "DYNAMIC_KEY_SECTIONS",
    "DEPRECATED_SECTIONS",
    "KeyMeta",
    "REQUIRED_SECTIONS",
    "SECTIONS",
    "SectionMeta",
    "STANDARD_COMMAND_KEYS",
    "STANDARD_SERVICE_KEYS",
    "canonical_sections_missing_from_example",
    "get_example_sections",
    "is_command_section",
    "is_known_config_key",
    "is_service_section",
    "load_documented_keys_from_example",
    "validate_config_keys",
]
