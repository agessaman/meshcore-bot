"""Settings persistence abstraction for plugin/command configuration.

``config.ini`` is the source of truth today, but we are building toward an
optional ``[Bot] persist_settings = config.ini|database`` switch.  This module
defines a single :class:`SettingsStore` interface with two backends:

- :class:`ConfigIniStore` — reads/writes the INI file, using the
  comment-preserving writer in :mod:`modules.ini_writer`.  Wired and working.
- :class:`DatabaseStore` — a documented stub for a future ``plugin_settings``
  table.  Not implemented this iteration; :func:`get_settings_store` falls back
  to the INI backend with a warning if ``persist_settings = database``.

No Flask or bot-core dependencies; pass a ``configparser`` object and a path in.
"""

from __future__ import annotations

import configparser
import logging
from abc import ABC, abstractmethod
from typing import Optional

from modules.ini_writer import update_ini_values
from modules.managed_keys import guard_managed_writes

logger = logging.getLogger("MeshCoreBot")


class SettingsStore(ABC):
    """Backend-agnostic read/write interface for plugin settings."""

    @abstractmethod
    def section_exists(self, section: str) -> bool:
        ...

    @abstractmethod
    def read_section(self, section: str) -> dict[str, str]:
        """Return all key/value pairs in a section (empty dict if absent)."""

    @abstractmethod
    def get_value(self, section: str, key: str, fallback: Optional[str] = None) -> Optional[str]:
        ...

    @abstractmethod
    def write_values(self, section: str, values: dict[str, str]) -> dict:
        """Persist ``values`` under ``section``.

        Returns a summary dict (backend-specific).  For the INI backend this is
        the :func:`modules.ini_writer.update_ini_values` result, which includes
        ``backup_path``.
        """

    @abstractmethod
    def write_sections(
        self,
        updates: dict[str, dict[str, str]],
        deletes: Optional[dict[str, list[str] | set[str]]] = None,
    ) -> dict:
        """Persist multiple sections at once, with optional key deletions.

        Used by dynamic-list editors that span more than one section (e.g. a
        plugin's own section plus a managed list section) so the whole save is
        one atomic write with a single backup.
        """


class ConfigIniStore(SettingsStore):
    """INI-backed store.  Keeps the in-memory ``config`` object in sync so the
    owning process reads fresh values immediately after a write."""

    def __init__(self, config: configparser.ConfigParser, config_path: str) -> None:
        self.config = config
        self.config_path = config_path

    def section_exists(self, section: str) -> bool:
        return self.config.has_section(section)

    def read_section(self, section: str) -> dict[str, str]:
        if not self.config.has_section(section):
            return {}
        return dict(self.config.items(section))

    def get_value(self, section: str, key: str, fallback: Optional[str] = None) -> Optional[str]:
        return self.config.get(section, key, fallback=fallback)

    def write_values(self, section: str, values: dict[str, str]) -> dict:
        return self.write_sections({section: values})

    def write_sections(
        self,
        updates: dict[str, dict[str, str]],
        deletes: Optional[dict[str, list[str] | set[str]]] = None,
    ) -> dict:
        # Refuse Home Assistant owned keys before touching disk or memory.
        # Unchanged managed keys are dropped so a save that rewrites them with
        # the same value still persists the other keys.
        updates, deletes = guard_managed_writes(updates, deletes, self.config)
        # Persist to disk first: a rejected payload (IniValueError) must not
        # leave this process's in-memory config holding unwritten values.
        result = update_ini_values(self.config_path, updates, deletes)
        # Mirror writes into the in-memory config so this process stays current.
        for section, values in (updates or {}).items():
            if not self.config.has_section(section):
                self.config.add_section(section)
            for key, value in values.items():
                try:
                    self.config.set(section, key, value)
                except ValueError:
                    logger.debug("Skipped in-memory cache of %s.%s (interpolation char)", section, key)
        for section, keys in (deletes or {}).items():
            if self.config.has_section(section):
                for key in keys:
                    self.config.remove_option(section, key)
        return result


class DatabaseStore(SettingsStore):
    """STUB — future DB-backed store for ``persist_settings = database``.

    Designed against a ``plugin_settings`` table::

        CREATE TABLE plugin_settings (
            section    TEXT NOT NULL,
            key        TEXT NOT NULL,
            value      TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (section, key)
        );

    (add ``'plugin_settings'`` to ``DBManager.ALLOWED_TABLES`` and a migration
    when implementing).  Until then, callers should not construct this directly;
    :func:`get_settings_store` returns the INI backend instead.
    """

    def __init__(self, db_manager) -> None:  # pragma: no cover - stub
        self.db_manager = db_manager

    def section_exists(self, section: str) -> bool:  # pragma: no cover - stub
        raise NotImplementedError("DatabaseStore is not implemented yet")

    def read_section(self, section: str) -> dict[str, str]:  # pragma: no cover - stub
        raise NotImplementedError("DatabaseStore is not implemented yet")

    def get_value(self, section, key, fallback=None):  # pragma: no cover - stub
        raise NotImplementedError("DatabaseStore is not implemented yet")

    def write_values(self, section: str, values: dict[str, str]) -> dict:  # pragma: no cover - stub
        raise NotImplementedError("DatabaseStore is not implemented yet")

    def write_sections(self, updates, deletes=None) -> dict:  # pragma: no cover - stub
        raise NotImplementedError("DatabaseStore is not implemented yet")


def get_settings_store(
    config: configparser.ConfigParser,
    config_path: str,
    db_manager=None,
) -> SettingsStore:
    """Return the configured settings store.

    Reads ``[Bot] persist_settings`` (default ``config.ini``).  The ``database``
    backend is not implemented yet, so it logs a warning and falls back to the
    INI store, ensuring writes are never silently dropped.
    """
    mode = (config.get("Bot", "persist_settings", fallback="config.ini") or "").strip().lower()
    if mode == "database":
        logger.warning(
            "persist_settings = database requested, but the database backend is "
            "not implemented yet; falling back to config.ini"
        )
    return ConfigIniStore(config, config_path)
