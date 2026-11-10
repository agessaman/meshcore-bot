"""Home Assistant add-on entrypoint.

The Supervisor starts ``python3 -m modules.ha_addon``. On startup this module
seeds ``config.ini`` when it is missing, applies :data:`INI_MAP` to that file,
records the keys it owns, and replaces itself with the bot.

``INI_MAP`` is the only place a setting is declared. A row with ``option`` set
reads that key from ``options.json`` when the user set it. A row with
``option`` left empty is a fixed add-on setting, applied on every start from
``constant``. Fixed rows are applied first, then user options, so a later
option can override a fixed setting. Adding an option is a new row.

A blank string and JSON ``null`` count as unset: the matching ``config.ini``
key is left alone. Home Assistant submits an empty configuration field that
way, and writing it would wipe a value the user edited in the file.

``ha_managed_keys.json`` is a JSON list of ``[section, key]`` pairs, sorted.
It holds the fixed keys plus the option keys the user set on this start. The
path is exported as ``MESHCORE_HA_MANAGED_KEYS`` for the bot and the web
viewer process it starts.

``config_dir``, ``data_dir``, and ``repo_root`` default to the Supervisor
paths and can be pointed at temporary directories.
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modules.ini_writer import update_ini_values

CONFIG_DIR = Path("/config")
DATA_DIR = Path("/data")
REPO_ROOT = Path("/app")

CONFIG_FILENAME = "config.ini"
EXAMPLE_FILENAME = "config.ini.example"
OPTIONS_FILENAME = "options.json"
MANAGED_KEYS_FILENAME = "ha_managed_keys.json"
MANAGED_KEYS_ENV = "MESHCORE_HA_MANAGED_KEYS"

# The viewer binds this port. config.yaml's ingress_port and published port
# use the same number; tests keep the three in step.
VIEWER_PORT = 8080

_CONNECTION_TYPES = ("serial", "tcp", "ble")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def as_str(value: object) -> str:
    return str(value).strip()


def as_bool(value: object) -> str:
    """Render a boolean the way ``config.ini`` spells it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = as_str(value).lower()
    if text in _TRUE:
        return "true"
    if text in _FALSE:
        return "false"
    raise ValueError(f"expected a boolean, got {value!r}")


def as_int(value: object) -> str:
    if isinstance(value, (bool, float)):
        raise ValueError(f"expected an integer, got {value!r}")
    if isinstance(value, int):
        return str(value)
    try:
        return str(int(as_str(value), 10))
    except ValueError as exc:
        raise ValueError(f"expected an integer, got {value!r}") from exc


def as_connection_type(value: object) -> str:
    text = as_str(value).lower()
    if text not in _CONNECTION_TYPES:
        allowed = ", ".join(_CONNECTION_TYPES)
        raise ValueError(f"connection type must be one of: {allowed}")
    return text


def as_log_level(value: object) -> str:
    text = as_str(value).upper()
    if text not in _LOG_LEVELS:
        allowed = ", ".join(_LOG_LEVELS)
        raise ValueError(f"log level must be one of: {allowed}")
    return text


@dataclass(frozen=True)
class IniMap:
    """One ``config.ini`` assignment the add-on owns.

    ``option`` is the key in Home Assistant's ``options.json``. ``None`` means
    a fixed setting; ``constant`` is written on every start. ``under_data``
    stores ``constant`` as a path relative to the data directory (a path with
    a suffix is a file, and its parent directory is created; any other path
    is a directory).
    """

    section: str
    key: str
    convert: Callable[[Any], str]
    option: str | None = None
    constant: Any = None
    under_data: bool = False


def _option(option: str, section: str, key: str, convert: Callable[[Any], str]) -> IniMap:
    return IniMap(section, key, convert, option=option)


def _fixed(
    section: str,
    key: str,
    convert: Callable[[Any], str],
    constant: Any,
    *,
    under_data: bool = False,
) -> IniMap:
    return IniMap(section, key, convert, constant=constant, under_data=under_data)


# HA option path -> (section, key, converter), plus fixed rows with constants.
# User options and fixed settings share this table on purpose.
INI_MAP: tuple[IniMap, ...] = (
    _option("connection_type", "Connection", "connection_type", as_connection_type),
    _option("serial_port", "Connection", "serial_port", as_str),
    _option("tcp_host", "Connection", "hostname", as_str),
    _option("tcp_port", "Connection", "tcp_port", as_int),
    _option("ble_address", "Connection", "ble_device_name", as_str),
    _option("bot_name", "Bot", "bot_name", as_str),
    _option("log_level", "Logging", "log_level", as_log_level),
    _option("web_viewer_password", "Web_Viewer", "web_viewer_password", as_str),
    _fixed("Bot", "db_path", as_str, "databases/meshcore_bot.db", under_data=True),
    _fixed("Bot", "local_dir_path", as_str, "local", under_data=True),
    _fixed("Logging", "log_file", as_str, ""),
    _fixed("Web_Viewer", "enabled", as_bool, True),
    _fixed("Web_Viewer", "host", as_str, "0.0.0.0"),
    _fixed("Web_Viewer", "port", as_int, VIEWER_PORT),
    _fixed("Web_Viewer", "ha_ingress", as_bool, True),
    # integration.py starts the viewer only when enabled and auto_start are both true.
    _fixed("Web_Viewer", "auto_start", as_bool, True),
)


def _is_set(value: object) -> bool:
    """True when an options.json value should be written.

    Missing keys never reach this. ``null`` and a blank string are the way an
    untouched Home Assistant field shows up, so they leave ``config.ini`` alone.
    """
    if value is None:
        return False
    if isinstance(value, str) and value.strip() == "":
        return False
    return True


def _constant(row: IniMap, data_dir: Path) -> Any:
    if row.under_data:
        return str(data_dir / str(row.constant))
    return row.constant


def _assignments(options: Mapping[str, Any], data_dir: Path) -> list[tuple[IniMap, str]]:
    """Fixed rows first, then options the user set. Later rows win on a shared key."""
    rows = [row for row in INI_MAP if row.option is None]
    for row in INI_MAP:
        if row.option is None or row.option not in options:
            continue
        if _is_set(options[row.option]):
            rows.append(row)

    rendered: list[tuple[IniMap, str]] = []
    for row in rows:
        raw = options[row.option] if row.option is not None else _constant(row, data_dir)
        try:
            value = row.convert(raw)
        except ValueError as exc:
            label = row.option or f"[{row.section}] {row.key}"
            raise ValueError(f"Invalid value for {label}: {exc}") from exc
        rendered.append((row, value))
    return rendered


def _read_config(config_path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with config_path.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except configparser.Error as exc:
        raise ValueError(f"Cannot parse {config_path}: {exc}") from exc
    return parser


def _updates_for(
    config_path: Path,
    assignments: list[tuple[IniMap, str]],
) -> dict[str, dict[str, str]]:
    """Keys whose file value differs from the assignment. Empty means do not write."""
    parser = _read_config(config_path)
    desired: dict[tuple[str, str], str] = {}
    for row, value in assignments:
        desired[(row.section, row.key)] = value

    updates: dict[str, dict[str, str]] = {}
    for (section, key), value in desired.items():
        if parser.has_option(section, key) and parser.get(section, key, raw=True) == value:
            continue
        updates.setdefault(section, {})[key] = value
    return updates


def _prepare_directories(config_dir: Path, data_dir: Path) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for row in INI_MAP:
        if not row.under_data or row.option is not None:
            continue
        target = data_dir / str(row.constant)
        # A suffix marks a file (the database). Anything else is a directory.
        (target.parent if target.suffix else target).mkdir(parents=True, exist_ok=True)
    return backup_dir


def _seed_config(config_path: Path, example: Path) -> None:
    if not example.is_file():
        raise FileNotFoundError(
            f"{config_path.name} does not exist and {example} was not found"
        )
    shutil.copyfile(example, config_path)


def _load_options(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Cannot parse {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must be a JSON object")
    return loaded


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_managed_keys(path: Path, assignments: list[tuple[IniMap, str]]) -> None:
    pairs = sorted({(row.section, row.key) for row, _value in assignments})
    _atomic_write(path, json.dumps(pairs, indent=2) + "\n")


def run(
    config_dir: str | Path = CONFIG_DIR,
    data_dir: str | Path = DATA_DIR,
    repo_root: str | Path = REPO_ROOT,
) -> None:
    """Apply add-on settings, then replace this process with the bot."""
    config_dir = Path(config_dir)
    data_dir = Path(data_dir)
    repo_root = Path(repo_root)

    backup_dir = _prepare_directories(config_dir, data_dir)
    config_path = config_dir / CONFIG_FILENAME
    if not config_path.exists():
        _seed_config(config_path, repo_root / EXAMPLE_FILENAME)
        print(f"Seeded {config_path} from {EXAMPLE_FILENAME}", flush=True)

    assignments = _assignments(_load_options(data_dir / OPTIONS_FILENAME), data_dir)
    updates = _updates_for(config_path, assignments)
    if updates:
        update_ini_values(str(config_path), updates, backup_dir=str(backup_dir))
        count = sum(len(keys) for keys in updates.values())
        print(f"Updated {config_path} ({count} keys)", flush=True)
    else:
        print(f"{config_path} already matches add-on settings", flush=True)

    managed_path = data_dir / MANAGED_KEYS_FILENAME
    _write_managed_keys(managed_path, assignments)
    os.environ[MANAGED_KEYS_ENV] = str(managed_path)

    script = repo_root / "meshcore_bot.py"
    if not script.is_file():
        raise FileNotFoundError(f"Bot entrypoint not found: {script}")
    print(f"Starting {script.name} --config {config_path}", flush=True)
    os.execvp(sys.executable, [sys.executable, str(script), "--config", str(config_path)])


def main() -> None:
    try:
        run()
    except (OSError, ValueError) as exc:
        print(f"Home Assistant add-on setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
