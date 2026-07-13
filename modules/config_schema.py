#!/usr/bin/env python3
"""
Machine-readable config schema for meshcore-bot.

Provides section/key metadata used by config validation and kept in sync with
config.ini.example. Grow incrementally as new options are added.
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from modules.config_validation import (
    CANONICAL_NON_COMMAND_SECTIONS,
    REQUIRED_SECTIONS,
    SEVERITY_INFO,
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

# PulsePoint agency mappings in Alert_Command (city, county, and legacy formats).
ALERT_AGENCY_KEY_PREFIXES = ("agency.", "agency_")

_VALID_CONFIG_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


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
    }),
    "Webhook": SectionMeta(keys={
        "enabled": KeyMeta(type="bool"),
        "host": KeyMeta(),
        "port": KeyMeta(type="int"),
        "secret_token": KeyMeta(),
    }),
    "Feed_Manager": SectionMeta(keys={
        "feed_manager_enabled": KeyMeta(type="bool"),
        "allow_private_urls": KeyMeta(type="bool"),
    }),
    "Weather_Service": SectionMeta(keys={
        "enabled": KeyMeta(type="bool"),
        "rain_nowcast_cache_seconds": KeyMeta(type="int", default="300"),
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
    return section == "Alert_Command" and key.startswith(ALERT_AGENCY_KEY_PREFIXES)


def is_known_config_key(
    section: str,
    key: str,
    example_keys: dict[str, dict[str, str]],
) -> bool:
    """Return True if key is documented or valid for this section type."""
    if section in DYNAMIC_KEY_SECTIONS:
        return True
    ex_sec = example_keys.get(section, {})
    if not ex_sec and section in example_keys:
        return True
    if key in ex_sec:
        return True
    section_meta = SECTIONS.get(section)
    if section_meta and key in section_meta.keys:
        return True
    if is_command_section(section):
        if key in STANDARD_COMMAND_KEYS:
            return True
        if LEGACY_ENABLED_KEY_RE.match(key):
            return True
    if is_service_section(section) and key in STANDARD_SERVICE_KEYS:
        return True
    if is_bridge_channel_key(section, key):
        return True
    if is_announcements_trigger_key(section, key):
        return True
    if is_alert_agency_key(section, key):
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
        cfg.optionxform = str
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


def validate_config_keys(config: configparser.ConfigParser) -> list[tuple[str, str]]:
    """
    Validate config keys against schema metadata.

    Returns list of (severity, message). Warnings only — does not block startup.
    """
    results: list[tuple[str, str]] = []

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
            or (section_meta and section_meta.dynamic_keys)
        )

        if is_dynamic or is_command:
            # Per-command legacy enabled keys
            if is_command:
                try:
                    for key in config.options(section):
                        if LEGACY_ENABLED_KEY_RE.match(key) and key != "enabled":
                            if config.has_option(section, "enabled"):
                                results.append((
                                    SEVERITY_WARNING,
                                    f"[{section_stripped}] legacy key '{key}' is redundant; "
                                    f"use 'enabled' instead.",
                                ))
                except configparser.Error:
                    pass
            continue

        if section_meta is None and section_stripped not in CANONICAL_NON_COMMAND_SECTIONS:
            continue

        try:
            options = config.options(section)
        except configparser.Error:
            continue

        known_keys = section_meta.keys if section_meta else {}
        for key in options:
            if key in known_keys:
                meta = known_keys[key]
                if meta.deprecated:
                    msg = meta.deprecated_message or f"Key '{key}' is deprecated."
                    results.append((SEVERITY_WARNING, f"[{section_stripped}] {msg}"))
                value = config.get(section, key, fallback="").strip()
                if meta.type == "enum" and meta.values and value:
                    if value.lower() not in meta.values:
                        allowed = ", ".join(meta.values)
                        results.append((
                            SEVERITY_WARNING,
                            f"[{section_stripped}] '{key}' must be one of: {allowed} "
                            f"(got '{value}').",
                        ))
                if meta.type == "int" and value:
                    try:
                        int(value)
                    except ValueError:
                        results.append((
                            SEVERITY_WARNING,
                            f"[{section_stripped}] '{key}' should be an integer (got '{value}').",
                        ))
                if meta.type == "bool" and value:
                    if value.lower() not in ("true", "false", "yes", "no", "1", "0", "on", "off"):
                        results.append((
                            SEVERITY_WARNING,
                            f"[{section_stripped}] '{key}' should be a boolean (got '{value}').",
                        ))
            elif known_keys and key not in known_keys:
                # Only warn for sections with explicit key lists in schema
                pass

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

    # Unknown keys in Connection (common mistake: host/port instead of hostname/tcp_port)
    if config.has_section("Connection"):
        documented = load_documented_keys_from_example(
            Path(__file__).resolve().parent.parent / "config.ini.example"
        ).get("Connection", {})
        try:
            for key in config.options("Connection"):
                if key in ("host", "port"):
                    results.append((
                        SEVERITY_WARNING,
                        f"[Connection] '{key}' is not valid; use "
                        f"{'hostname' if key == 'host' else 'tcp_port'} for TCP connections.",
                    ))
                elif documented and key not in documented:
                    results.append((
                        SEVERITY_INFO,
                        f"[Connection] unknown key '{key}' (not documented in config.ini.example).",
                    ))
        except configparser.Error:
            pass

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
