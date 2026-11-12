"""Home Assistant managed config.ini keys.

The add-on writes a JSON list of ``[section, key]`` pairs and exports the file
path in :data:`MANAGED_KEYS_ENV`. The web viewer reads that file so it can show
those settings as read-only and refuse to save over them.

A missing variable or a missing file yields an empty set and is not logged.
A file that cannot be read, or that is not a JSON list of ``[section, key]``
string pairs, is logged and also yields an empty set.

Sections and keys are normalized with the same helpers the INI writer uses
(strip, then lowercase), so a managed key is matched regardless of the
section spelling a caller uses.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path

from modules.ini_writer import IniValueError, _normalize_key, _normalize_section

logger = logging.getLogger("MeshCoreBot")

MANAGED_KEYS_FILENAME = "ha_managed_keys.json"
MANAGED_KEYS_ENV = "MESHCORE_HA_MANAGED_KEYS"


class ManagedKeyError(IniValueError):
    """A save would change or delete a key Home Assistant owns.

    Subclass of :class:`modules.ini_writer.IniValueError` so existing config
    write handlers already turn it into a client error.
    """

    def __init__(self, conflicts: Iterable[tuple[str, str]]):
        ordered = sorted(set(conflicts), key=lambda pair: (pair[0].lower(), pair[1]))
        self.conflicts = ordered
        shown = ", ".join(f"[{section}] {key}" for section, key in ordered)
        super().__init__(f"Managed by Home Assistant: {shown}")


def normalize_managed_pair(section: str, key: str) -> tuple[str, str]:
    """Section and key stripped and lowercased (matches ``ini_writer``)."""
    return (_normalize_section(section), _normalize_key(key))


def load_managed_keys() -> frozenset[tuple[str, str]]:
    """Return the managed ``(section, key)`` pairs, or an empty set."""
    path = os.environ.get(MANAGED_KEYS_ENV, "").strip()
    if not path:
        return frozenset()
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    except UnicodeDecodeError as exc:
        logger.warning("Home Assistant managed keys file %s is malformed: %s", path, exc)
        return frozenset()
    except OSError as exc:
        logger.warning("Cannot read Home Assistant managed keys from %s: %s", path, exc)
        return frozenset()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Home Assistant managed keys file %s is malformed: %s", path, exc)
        return frozenset()
    pairs = _pairs_from_payload(payload)
    if pairs is None:
        logger.warning(
            "Home Assistant managed keys file %s is malformed: "
            "expected a list of [section, key] pairs",
            path,
        )
        return frozenset()
    return pairs


def is_managed(
    section: str,
    key: str,
    managed: frozenset[tuple[str, str]],
) -> bool:
    """True when ``(section, key)`` is in the managed set."""
    return normalize_managed_pair(section, key) in managed


def guard_managed_writes(
    updates: dict[str, dict[str, str]] | None,
    deletes: dict[str, list[str] | set[str]] | None,
    config: configparser.ConfigParser | None = None,
) -> tuple[dict[str, dict[str, str]], dict[str, list[str] | set[str]] | None]:
    """Drop unchanged managed keys and reject real changes.

    Rewriting a managed key with the value already stored is not a change (the
    plugin save always rewrites ``enabled``). A different value, a new key, or
    any delete of a managed key raises :class:`ManagedKeyError` and nothing
    should be written.

    When no keys are managed, ``updates`` and ``deletes`` are returned as
    equivalent concrete dicts.
    """
    managed = load_managed_keys()
    if not managed:
        return updates or {}, deletes or None

    conflicts: list[tuple[str, str]] = []
    filtered: dict[str, dict[str, str]] = {}
    for section, values in (updates or {}).items():
        kept: dict[str, str] = {}
        for key, value in values.items():
            text = "" if value is None else str(value)
            if is_managed(section, key, managed):
                if _stored_value(config, section, key) == text:
                    continue
                conflicts.append(normalize_managed_pair(section, key))
                continue
            kept[key] = value
        if kept:
            filtered[section] = kept

    filtered_deletes: dict[str, list[str] | set[str]] = {}
    for section, keys in (deletes or {}).items():
        kept_keys: list[str] = []
        for key in keys:
            if is_managed(section, key, managed):
                conflicts.append(normalize_managed_pair(section, key))
            else:
                kept_keys.append(key)
        if kept_keys:
            filtered_deletes[section] = kept_keys

    if conflicts:
        error = ManagedKeyError(conflicts)
        logger.warning("%s", error)
        raise error
    return filtered, filtered_deletes or None


def _stored_value(
    config: configparser.ConfigParser | None,
    section: str,
    key: str,
) -> str | None:
    if config is None:
        return None
    name = _normalize_section(section)
    if not name:
        return None
    for stored in config.sections():
        if _normalize_section(stored) != name:
            continue
        if not config.has_option(stored, key):
            return None
        try:
            return config.get(stored, key, raw=True)
        except (configparser.Error, ValueError):
            return None
    return None


def _pairs_from_payload(payload: object) -> frozenset[tuple[str, str]] | None:
    """Parse the JSON list, or return None when the document is malformed."""
    if not isinstance(payload, list):
        return None
    pairs: list[tuple[str, str]] = []
    for item in payload:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            return None
        section, key = normalize_managed_pair(item[0], item[1])
        if not section or not key:
            return None
        pairs.append((section, key))
    return frozenset(pairs)
