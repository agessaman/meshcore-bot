"""Machine-readable plugin settings schema, validation, and view assembly.

Plugins (commands and services) may declare an optional ``settings_schema``
class attribute describing their configurable settings.  The web viewer uses
this to render typed widgets (dropdowns, validated numbers, toggles) and the
save endpoint uses :func:`validate_field` so server-side validation mirrors the
schema exactly.  Plugins without a schema fall back to a generic enable-toggle
plus a raw key/value editor.

This module performs *class-level* discovery: it imports plugin modules and
reads class attributes WITHOUT instantiating the plugin.  That avoids plugin
``__init__`` side effects (e.g. opening HTTP sessions) and sidesteps the service
loader's enable-gate, so disabled services still appear in the UI.

It has no Flask or bot-core dependencies; pass a ``configparser`` object in.

Schema field format (a list of these dicts on ``settings_schema``)::

    {
        "key": "poll_interval",        # config key within the section
        "label": "Poll interval",      # human label
        "type": "int",                 # bool|int|float|str|enum|list
        "options": [{"value": "...", "label": "..."}],  # required for enum
        "min": 1000, "max": None,      # numeric bounds (int/float)
        "default": 60000,
        "help": "Polling cadence in ms.",
        "required": False,
        "pattern": None,               # validation regex (str/list)
        "unit": "ms",                  # optional display suffix
    }
"""

from __future__ import annotations

import configparser
import importlib
import inspect
import os
import re
from typing import Any, Optional

VALID_TYPES = {"bool", "int", "float", "str", "enum", "list"}

# Truthy/falsey string forms accepted for bool fields (configparser-compatible).
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

# Legacy [section] enabled aliases, mirroring base_command.get_config_value, so a
# plugin's current on/off state displays correctly before the first canonical save.
# Maps canonical section -> list of (legacy_section, legacy_key).
_ENABLED_LEGACY_ALIASES: dict[str, list[tuple[str, str]]] = {
    "DadJoke_Command": [("Jokes", "dadjoke_enabled")],
    "Joke_Command": [("Jokes", "joke_enabled")],
    "Stats_Command": [("Stats_Command", "stats_enabled"), ("Stats", "stats_enabled")],
    "Sports_Command": [("Sports_Command", "sports_enabled"), ("Sports", "sports_enabled")],
    "Hacker_Command": [("Hacker_Command", "hacker_enabled"), ("Hacker", "hacker_enabled")],
    "Alert_Command": [("Alert_Command", "alert_enabled")],
}

# Command name -> section base for camelCase names (mirrors
# BaseCommand._derive_config_section_name).
_CAMEL_CASE_SECTION = {"dadjoke": "DadJoke", "webviewer": "WebViewer"}


# ---------------------------------------------------------------------------
# Validation / coercion
# ---------------------------------------------------------------------------

def _coerce_bool(raw: Any) -> Optional[bool]:
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def validate_field(field: dict, raw: Any) -> tuple[bool, Any, Optional[str]]:
    """Validate and coerce a single raw value against a schema field.

    Returns ``(ok, coerced_value, error_message)``.  On failure ``ok`` is False
    and ``error_message`` explains why; ``coerced_value`` is None.
    """
    ftype = field.get("type", "str")
    label = field.get("label") or field.get("key", "value")
    required = bool(field.get("required", False))

    # Empty handling (treat None / "" as empty for str/enum/list).
    is_empty = raw is None or (isinstance(raw, str) and raw.strip() == "")
    if is_empty and ftype not in ("bool",):
        if required:
            return False, None, f"{label} is required"
        # Empty + optional: fall back to default if provided, else empty string.
        return True, field.get("default", ""), None

    if ftype == "bool":
        coerced = _coerce_bool(raw)
        if coerced is None:
            return False, None, f"{label} must be true or false"
        return True, coerced, None

    if ftype in ("int", "float"):
        try:
            coerced = int(raw) if ftype == "int" else float(raw)
        except (ValueError, TypeError):
            return False, None, f"{label} must be a number"
        lo, hi = field.get("min"), field.get("max")
        if lo is not None and coerced < lo:
            return False, None, f"{label} must be ≥ {lo}"
        if hi is not None and coerced > hi:
            return False, None, f"{label} must be ≤ {hi}"
        return True, coerced, None

    if ftype == "enum":
        allowed = {str(o.get("value")) for o in field.get("options", [])}
        s = str(raw)
        if s not in allowed:
            return False, None, f"{label} must be one of: {', '.join(sorted(allowed))}"
        return True, s, None

    if ftype == "list":
        items = [item.strip() for item in str(raw).split(",") if item.strip()]
        pattern = field.get("pattern")
        if pattern:
            rx = re.compile(pattern)
            for item in items:
                if not rx.fullmatch(item):
                    return False, None, f"{label} contains an invalid value: {item}"
        return True, items, None

    # str (default)
    s = str(raw)
    pattern = field.get("pattern")
    if pattern and not re.fullmatch(pattern, s):
        return False, None, f"{label} has an invalid format"
    return True, s, None


def to_config_string(field: dict, coerced: Any) -> str:
    """Convert a coerced value into its config.ini string form."""
    ftype = field.get("type", "str")
    if ftype == "bool":
        return "true" if coerced else "false"
    if ftype == "list":
        if isinstance(coerced, (list, tuple)):
            return ", ".join(str(x) for x in coerced)
        return str(coerced)
    return str(coerced)


# ---------------------------------------------------------------------------
# Section / value resolution
# ---------------------------------------------------------------------------

def command_section_name(name: str) -> str:
    """Derive a command's config section from its name (mirrors base_command)."""
    base = _CAMEL_CASE_SECTION.get(name, name.title())
    return f"{base}_Command"


def service_section_name(service_class: type) -> str:
    """Derive a service's config section from its class attr / class name."""
    explicit = getattr(service_class, "config_section", None)
    if explicit:
        return explicit
    cls = service_class.__name__
    return cls[:-7] if cls.endswith("Service") else cls


def _read_typed(config: configparser.ConfigParser, section: str, field: dict) -> Any:
    """Read a field's current value from config, typed, falling back to default.

    A field may override ``section`` to read from a shared section (e.g. a
    weather command exposing ``[Weather] default_state``).
    """
    key = field["key"]
    section = field.get("section") or section
    ftype = field.get("type", "str")
    default = field.get("default")
    if not config.has_section(section) or not config.has_option(section, key):
        return default
    # raw=True so values containing '%' (cron/strftime/templates) don't trip
    # configparser's interpolation, which would raise InterpolationError.
    try:
        if ftype == "bool":
            return config.getboolean(section, key, raw=True)
        if ftype == "int":
            return config.getint(section, key, raw=True)
        if ftype == "float":
            return config.getfloat(section, key, raw=True)
        if ftype == "list":
            raw = config.get(section, key, raw=True)
            return [item.strip() for item in raw.split(",") if item.strip()]
        return config.get(section, key, raw=True)
    except (ValueError, TypeError):
        return default


def read_enabled(
    config: configparser.ConfigParser, section: str, default: bool
) -> bool:
    """Read a plugin's on/off state: canonical key first, then legacy aliases."""
    if config.has_section(section) and config.has_option(section, "enabled"):
        try:
            return config.getboolean(section, "enabled", raw=True)
        except ValueError:
            pass
    for legacy_section, legacy_key in _ENABLED_LEGACY_ALIASES.get(section, []):
        if config.has_section(legacy_section) and config.has_option(legacy_section, legacy_key):
            try:
                return config.getboolean(legacy_section, legacy_key, raw=True)
            except ValueError:
                continue
    return default


# ---------------------------------------------------------------------------
# Class-level discovery
# ---------------------------------------------------------------------------

def _discover_classes(base_class: type, package: str, directory: str, logger=None) -> list[type]:
    """Import every ``*.py`` in ``directory`` and collect ``base_class`` subclasses."""
    found: list[type] = []
    if not os.path.isdir(directory):
        return found
    excluded = {"__init__", "base_command", "base_service"}
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".py"):
            continue
        stem = fname[:-3]
        if stem in excluded or stem.endswith("_utils"):
            continue
        module_path = f"{package}.{stem}"
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 - never let one bad plugin break the list
            if logger:
                logger.warning("Could not import %s for settings discovery: %s", module_path, exc)
            continue
        for _n, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, base_class)
                and obj is not base_class
                and obj.__module__ == module_path
            ):
                found.append(obj)
                break
    return found


def build_plugin_settings_view(
    config: configparser.ConfigParser,
    *,
    logger=None,
    commands_dir: Optional[str] = None,
    services_dir: Optional[str] = None,
) -> list[dict]:
    """Assemble the per-plugin settings view for the web UI.

    Returns a list of dicts, one per discovered command/service::

        {name, kind, section, label, description, category,
         enabled, has_schema, fields, values}

    ``fields`` is the plugin's ``settings_schema`` with a resolved ``value`` on
    each field.  ``values`` is the raw current config section (minus ``enabled``)
    used by the generic editor for plugins without a schema.
    """
    # Import bases lazily so this module stays import-light.
    from modules.commands.base_command import BaseCommand
    from modules.service_plugins.base_service import BaseServicePlugin

    here = os.path.dirname(__file__)
    commands_dir = commands_dir or os.path.join(here, "commands")
    services_dir = services_dir or os.path.join(here, "service_plugins")

    view: list[dict] = []

    # --- Commands (default enabled = True; they're active unless turned off) ---
    for cls in _discover_classes(BaseCommand, "modules.commands", commands_dir, logger):
        name = getattr(cls, "name", "") or cls.__name__.lower().replace("command", "")
        if not name:
            continue
        section = command_section_name(name)
        try:
            view.append(_assemble_entry(
                config, cls, kind="command", name=name, section=section,
                label=name.replace("_", " ").title(),
                description=getattr(cls, "description", "") or "",
                category=getattr(cls, "category", "general") or "general",
                enabled_default=True,
            ))
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not break the list
            if logger:
                logger.warning("Skipping command %s in settings view: %s", name, exc)

    # --- Services (default enabled = False; must opt in) ---
    for cls in _discover_classes(BaseServicePlugin, "modules.service_plugins", services_dir, logger):
        section = service_section_name(cls)
        name = getattr(cls, "name", "") or cls.__name__.lower().replace("service", "")
        try:
            view.append(_assemble_entry(
                config, cls, kind="service", name=name, section=section,
                label=section.replace("_", " "),
                description=getattr(cls, "description", "") or "",
                category="service",
                enabled_default=False,
            ))
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not break the list
            if logger:
                logger.warning("Skipping service %s in settings view: %s", name, exc)

    view.sort(key=lambda e: (e["kind"], e["label"].lower()))
    return view


def _assemble_entry(
    config: configparser.ConfigParser,
    cls: type,
    *,
    kind: str,
    name: str,
    section: str,
    label: str,
    description: str,
    category: str,
    enabled_default: bool,
) -> dict:
    schema = list(getattr(cls, "settings_schema", []) or [])
    fields: list[dict] = []
    for field in schema:
        if not isinstance(field, dict) or "key" not in field:
            continue
        resolved = dict(field)
        resolved["value"] = _read_typed(config, section, field)
        fields.append(resolved)

    # Every command can restrict which channels it responds in
    # ([Name_Command] channels). Surface it on every command card unless the
    # plugin already declares its own channels field.
    if kind == "command" and not any(f["key"].lower() == "channels" for f in fields):
        channels_field = {
            "key": "channels", "label": "Channels", "type": "list", "default": "",
            "help": ("Comma-separated channels this command responds in. Blank = the "
                     "global monitored channels (default). DMs always work."),
        }
        channels_field["value"] = _read_typed(config, section, channels_field)
        fields.append(channels_field)

    # Keys handled elsewhere shouldn't appear in the raw "Other config values"
    # editor: the enable toggle, its legacy *_enabled aliases in this section,
    # the channels field above, and aliases (managed via keywords).
    skip_keys = {"enabled", "channels", "aliases"}
    for legacy_section, legacy_key in _ENABLED_LEGACY_ALIASES.get(section, []):
        if legacy_section == section:
            skip_keys.add(legacy_key.lower())
    schema_keys = {f["key"].lower() for f in fields}

    # Raw values for the fallback editor: every other key in the section.
    # raw=True so '%' in values doesn't trip configparser interpolation.
    values: dict[str, str] = {}
    if config.has_section(section):
        for key, raw in config.items(section, raw=True):
            if key.lower() in skip_keys or key.lower() in schema_keys:
                continue
            values[key] = raw

    return {
        "name": name,
        "kind": kind,
        "section": section,
        "label": label,
        "description": description,
        "category": category,
        "enabled": read_enabled(config, section, enabled_default),
        "has_schema": bool(fields),
        "fields": fields,
        "values": values,
        "dynamic_sections": _read_dynamic_sections(config, cls),
        "repeating_blocks": _read_repeating_blocks(config, cls, section),
    }


def _read_repeating_blocks(
    config: configparser.ConfigParser, cls: type, section: str
) -> list[dict]:
    """Build the repeating structured-block editors a plugin declares.

    A plugin may declare ``settings_repeating_blocks`` for families of indexed
    keys like ``mqtt1_server``, ``mqtt2_server`` … (PacketCapture).  Each block
    is a structured group with its own sub-schema and an enable toggle
    (``mqtt<N>_enabled``).  Blocks are returned sorted by index; ``values``
    carries every sub-key (including ones not in the sub-schema) so unknown
    fields survive a save.
    """
    decls = list(getattr(cls, "settings_repeating_blocks", []) or [])
    result: list[dict] = []
    for d in decls:
        if not isinstance(d, dict) or "id" not in d:
            continue
        bid = d["id"]
        enabled_field = d.get("enabled_field", "enabled")
        rx = re.compile(rf"^{re.escape(bid)}(\d+)_(.+)$")
        groups: dict[int, dict[str, str]] = {}
        if config.has_section(section):
            for key, raw in config.items(section, raw=True):
                m = rx.match(key)
                if m:
                    groups.setdefault(int(m.group(1)), {})[m.group(2)] = raw
        blocks = []
        for idx in sorted(groups):
            sub = groups[idx]
            enabled = str(sub.get(enabled_field, "true")).strip().lower() in _TRUE
            values = {k: v for k, v in sub.items() if k != enabled_field}
            blocks.append({"index": idx, "enabled": enabled, "values": values})
        result.append({
            "id": bid,
            "label": d.get("label", bid),
            "item_label": d.get("item_label", "item"),
            "help": d.get("help", ""),
            "enabled_field": enabled_field,
            "fields": list(d.get("fields", [])),
            "blocks": blocks,
        })
    return result


def read_section_items(
    config: configparser.ConfigParser, section: str, prefix: str = ""
) -> list[dict]:
    """Return a section's key/value pairs as ``[{"key", "value"}, ...]`` (raw).

    When ``prefix`` is given, only keys starting with it are returned and the
    prefix is stripped from the displayed key (e.g. ``agency.county1`` ->
    ``county1``).  This lets a dynamic editor manage just the prefixed keys in a
    section that also holds typed schema fields.
    """
    items: list[dict] = []
    if not config.has_section(section):
        return items
    pl = prefix.lower()
    # raw=True so '%' in values doesn't trip interpolation.
    for key, raw in config.items(section, raw=True):
        if prefix:
            if not key.lower().startswith(pl):
                continue
            items.append({"key": key[len(prefix):], "value": raw})
        else:
            items.append({"key": key, "value": raw})
    return items


def _read_dynamic_sections(config: configparser.ConfigParser, cls: type) -> list[dict]:
    """Build the dynamic key/value section editors a plugin declares.

    A plugin may declare ``settings_dynamic_sections`` — a list of descriptors
    for whole config sections that hold a free-form, user-extendable list of
    ``key = value`` entries (e.g. ``[Channels_List]``).  The web viewer renders
    these as add/edit/delete tables.
    """
    declared = list(getattr(cls, "settings_dynamic_sections", []) or [])
    result: list[dict] = []
    for ds in declared:
        if not isinstance(ds, dict) or "section" not in ds:
            continue
        sec = ds["section"]
        prefix = ds.get("key_prefix", "") or ""
        result.append({
            "section": sec,
            "key_prefix": prefix,
            "label": ds.get("label", sec),
            "help": ds.get("help", ""),
            "key_label": ds.get("key_label", "Key"),
            "value_label": ds.get("value_label", "Value"),
            "key_placeholder": ds.get("key_placeholder", ""),
            "value_placeholder": ds.get("value_placeholder", ""),
            "items": read_section_items(config, sec, prefix),
        })
    return result
