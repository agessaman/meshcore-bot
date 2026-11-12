"""Home Assistant add-on entrypoint and packaging."""

from __future__ import annotations

import configparser
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from modules.config_schema import is_known_config_key, load_documented_keys_from_example
from modules.ha_addon import (
    INI_MAP,
    VIEWER_PORT,
    as_bool,
    as_connection_type,
    as_int,
    as_log_level,
    main,
    run,
)
from modules.managed_keys import MANAGED_KEYS_ENV, MANAGED_KEYS_FILENAME

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "homeassistant" / "meshcore_bot"

MINI_EXAMPLE = """\
# keep me
[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0
ble_device_name =
hostname =
tcp_port = 5000

[Bot]
bot_name = MeshCoreBot
enabled = true
db_path = meshcore_bot.db

[Logging]
log_level = INFO
log_file = meshcore_bot.log

[Web_Viewer]
; viewer comment
enabled = false
host = 127.0.0.1
port = 8080
auto_start = false

[Feed_Command]
enabled = false
"""

SAMPLE_OPTIONS = {
    "connection_type": "tcp",
    "tcp_host": "radio.local",
    "tcp_port": 5000,
    "bot_name": "HA Radio",
    "log_level": "DEBUG",
    "web_viewer_password": "s3cret",
}

FIXED_PAIRS = {(row.section, row.key) for row in INI_MAP if row.option is None}


@pytest.fixture(autouse=True)
def blocked_exec(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Stop run() from replacing the test process, and isolate the env var."""
    calls: list[tuple[str, list[str]]] = []

    def fake(file: str, args: list[str]) -> None:
        calls.append((file, list(args)))

    monkeypatch.setattr(os, "execvp", fake)
    monkeypatch.delenv(MANAGED_KEYS_ENV, raising=False)
    return calls


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "config.ini.example").write_text(MINI_EXAMPLE, encoding="utf-8")
    (repo / "meshcore_bot.py").write_text("raise SystemExit('bot')\n", encoding="utf-8")
    return {
        "config_dir": tmp_path / "config",
        "data_dir": tmp_path / "data",
        "repo_root": repo,
    }


def _options(data_dir: Path, payload: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "options.json").write_text(json.dumps(payload), encoding="utf-8")


def _ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return parser


def _managed(data_dir: Path) -> set[tuple[str, str]]:
    payload = json.loads((data_dir / MANAGED_KEYS_FILENAME).read_text(encoding="utf-8"))
    return {tuple(pair) for pair in payload}


def _backup_names(data_dir: Path) -> list[str]:
    return sorted(path.name for path in (data_dir / "backups").iterdir())


def _assert_rerun_writes_nothing(
    layout: dict[str, Path],
    config_path: Path,
    data_dir: Path,
) -> None:
    written = config_path.read_bytes()
    backups = _backup_names(data_dir)
    run(**layout)
    assert config_path.read_bytes() == written
    assert _backup_names(data_dir) == backups


def _project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib
    except ModuleNotFoundError:
        match = re.search(r'(?m)^version = "([^"]+)"', text)
        assert match is not None
        return match.group(1)
    return tomllib.loads(text)["project"]["version"]


def load_yaml(path: Path) -> dict[str, Any]:
    """Parse one of the add-on's YAML files."""
    with path.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} is not a mapping")
    return loaded


def test_schema_and_mapping_agree_both_ways() -> None:
    schema = load_yaml(ADDON / "config.yaml")["schema"]
    mapped = {row.option for row in INI_MAP if row.option is not None}
    assert set(schema) == mapped
    for typedef in schema.values():
        assert isinstance(typedef, str)
        assert typedef.endswith("?")
    assert schema["connection_type"] == "list(serial|tcp|ble)?"
    assert schema["serial_port"] == "device(subsystem=tty)?"
    assert schema["tcp_host"] == "str?"
    assert schema["tcp_port"] == "port?"
    assert schema["ble_device_name"] == "str?"
    assert schema["bot_name"] == "str?"
    assert schema["log_level"] == "list(DEBUG|INFO|WARNING|ERROR|CRITICAL)?"
    assert schema["web_viewer_password"] == "password?"


def test_option_rows_target_the_config_keys() -> None:
    actual = {row.option: (row.section, row.key) for row in INI_MAP if row.option is not None}
    assert actual == {
        "connection_type": ("Connection", "connection_type"),
        "serial_port": ("Connection", "serial_port"),
        "tcp_host": ("Connection", "hostname"),
        "tcp_port": ("Connection", "tcp_port"),
        "ble_device_name": ("Connection", "ble_device_name"),
        "bot_name": ("Bot", "bot_name"),
        "log_level": ("Logging", "log_level"),
        "web_viewer_password": ("Web_Viewer", "web_viewer_password"),
    }


def test_every_mapped_key_is_known() -> None:
    example = load_documented_keys_from_example(ROOT / "config.ini.example")
    missing = [
        (row.section, row.key)
        for row in INI_MAP
        if not is_known_config_key(row.section, row.key, example)
    ]
    assert missing == []


def test_addon_version_matches_pyproject() -> None:
    config = load_yaml(ADDON / "config.yaml")
    assert config["version"] == _project_version()
    # Absent startup/boot mean Home Assistant's defaults (application / auto).
    # The add-on linter rejects a key that only restates its default.
    assert config.get("startup", "application") == "application"
    assert config.get("boot", "auto") == "auto"
    assert config["init"] is False
    assert config["ingress"] is True
    assert config["ingress_port"] == VIEWER_PORT
    assert config["ports"][f"{VIEWER_PORT}/tcp"] is None
    assert config["ports_description"][f"{VIEWER_PORT}/tcp"]
    assert config["arch"] == ["amd64", "aarch64"]
    assert config["image"] == "ghcr.io/agessaman/meshcore-bot-ha"
    assert config["slug"] == "meshcore_bot"
    assert config["uart"] is True
    assert config["host_dbus"] is True
    assert config["panel_icon"] == "mdi:radio-tower"
    assert config["map"] == ["app_config:rw"]
    assert config.get("host_network", False) is False
    assert "options" not in config


def test_translations_name_every_option() -> None:
    schema = load_yaml(ADDON / "config.yaml")["schema"]
    translated = load_yaml(ADDON / "translations" / "en.yaml")
    configuration = translated["configuration"]
    assert set(configuration) == set(schema)
    for key, block in configuration.items():
        assert block["name"].strip(), key
        assert block["description"].strip(), key
    assert f"{VIEWER_PORT}/tcp" in translated["network"]


def test_repository_yaml() -> None:
    repo = load_yaml(ROOT / "repository.yaml")
    assert repo == {
        "name": "MeshCore Bot",
        "url": "https://github.com/agessaman/meshcore-bot",
        "maintainer": "Adam Gessaman",
    }


def test_converters() -> None:
    assert as_bool(True) == "true"
    assert as_bool(False) == "false"
    assert as_bool("YES") == "true"
    assert as_bool("off") == "false"
    with pytest.raises(ValueError):
        as_bool("maybe")
    assert as_int(5000) == "5000"
    assert as_int("5000") == "5000"
    with pytest.raises(ValueError):
        as_int(True)
    with pytest.raises(ValueError):
        as_int("nope")
    assert as_connection_type(" BLE ") == "ble"
    with pytest.raises(ValueError):
        as_connection_type("wifi")
    assert as_log_level("debug") == "DEBUG"
    with pytest.raises(ValueError):
        as_log_level("verbose")


def test_first_start_seeds_and_applies_fixed_settings(
    layout: dict[str, Path],
    blocked_exec: list[tuple[str, list[str]]],
) -> None:
    run(**layout)
    config_path = layout["config_dir"] / "config.ini"
    text = config_path.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "; viewer comment" in text
    cfg = _ini(config_path)
    data = layout["data_dir"]
    assert cfg.get("Bot", "db_path") == str(data / "databases" / "meshcore_bot.db")
    assert cfg.get("Bot", "local_dir_path") == str(layout["config_dir"] / "local")
    assert cfg.get("Logging", "log_file") == ""
    assert cfg.get("Logging", "log_level") == "INFO"
    assert cfg.get("Web_Viewer", "enabled") == "true"
    assert cfg.get("Web_Viewer", "host") == "0.0.0.0"
    assert cfg.get("Web_Viewer", "port") == str(VIEWER_PORT)
    assert cfg.get("Web_Viewer", "ha_ingress") == "true"
    assert cfg.get("Web_Viewer", "auto_start") == "true"
    assert cfg.get("Bot", "bot_name") == "MeshCoreBot"
    assert cfg.get("Bot", "enabled") == "true"
    assert cfg.get("Feed_Command", "enabled") == "false"
    assert cfg.get("Connection", "serial_port") == "/dev/ttyUSB0"
    assert (data / "databases").is_dir()
    assert (layout["config_dir"] / "local").is_dir()
    assert not (layout["config_dir"] / "local" / "config.ini").exists()
    backups = list((data / "backups").glob("config.ini.bak.*"))
    assert len(backups) == 1
    assert "db_path = meshcore_bot.db" in backups[0].read_text(encoding="utf-8")
    assert list(layout["config_dir"].glob("*.bak.*")) == []
    assert _managed(data) == FIXED_PAIRS
    assert os.environ[MANAGED_KEYS_ENV] == str(data / MANAGED_KEYS_FILENAME)
    assert blocked_exec == [
        (
            sys.executable,
            [sys.executable, str(layout["repo_root"] / "meshcore_bot.py"), "--config", str(config_path)],
        )
    ]


def test_unset_and_blank_options_leave_existing_values(layout: dict[str, Path]) -> None:
    run(**layout)
    config_path = layout["config_dir"] / "config.ini"
    text = config_path.read_text(encoding="utf-8").replace(
        "bot_name = MeshCoreBot",
        "bot_name = Kept",
    )
    config_path.write_text(text, encoding="utf-8")
    _options(
        layout["data_dir"],
        {"bot_name": "  ", "serial_port": "", "tcp_host": None, "log_level": "WARNING"},
    )
    run(**layout)
    cfg = _ini(config_path)
    assert cfg.get("Bot", "bot_name") == "Kept"
    assert cfg.get("Connection", "serial_port") == "/dev/ttyUSB0"
    assert cfg.get("Connection", "hostname") == ""
    assert cfg.get("Logging", "log_level") == "WARNING"
    assert ("Bot", "bot_name") not in _managed(layout["data_dir"])
    assert ("Connection", "serial_port") not in _managed(layout["data_dir"])
    assert ("Logging", "log_level") in _managed(layout["data_dir"])


def test_set_options_override(layout: dict[str, Path]) -> None:
    _options(
        layout["data_dir"],
        {
            "connection_type": "ble",
            "ble_device_name": "AA:BB:CC:DD:EE:FF",
            "bot_name": "HA Radio",
            "tcp_port": 4000,
            "ignored_extra": "left alone",
        },
    )
    run(**layout)
    cfg = _ini(layout["config_dir"] / "config.ini")
    assert cfg.get("Connection", "connection_type") == "ble"
    assert cfg.get("Connection", "ble_device_name") == "AA:BB:CC:DD:EE:FF"
    assert cfg.get("Connection", "tcp_port") == "4000"
    assert cfg.get("Bot", "bot_name") == "HA Radio"
    assert cfg.get("Connection", "hostname") == ""


def test_restart_writes_nothing_when_unchanged(layout: dict[str, Path]) -> None:
    run(**layout)
    assert _backup_names(layout["data_dir"])
    _assert_rerun_writes_nothing(
        layout, layout["config_dir"] / "config.ini", layout["data_dir"]
    )


def test_percent_in_password_survives_configparser(layout: dict[str, Path]) -> None:
    _options(layout["data_dir"], {"web_viewer_password": "abc%xyz"})
    run(**layout)
    config_path = layout["config_dir"] / "config.ini"
    assert "web_viewer_password = abc%%xyz" in config_path.read_text(encoding="utf-8")

    # The bot reads config.ini with the default interpolation, so %% must come
    # back as the single % the user entered.
    plain = configparser.ConfigParser()
    plain.read(config_path, encoding="utf-8")
    assert plain.get("Web_Viewer", "web_viewer_password") == "abc%xyz"

    _assert_rerun_writes_nothing(layout, config_path, layout["data_dir"])


def test_overlay_managed_keys_are_deleted(
    layout: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(**layout)
    overlay = layout["config_dir"] / "local" / "config.ini"
    overlay.write_text(
        "; local comment\n"
        "[Bot]\n"
        "bot_name = Override\n"
        "local_dir_path = /elsewhere\n"
        "[Feed_Command]\n"
        "enabled = true\n",
        encoding="utf-8",
    )
    _options(layout["data_dir"], {"bot_name": "HA Radio"})
    run(**layout)
    cfg = _ini(overlay)
    assert not cfg.has_option("Bot", "bot_name")
    assert not cfg.has_option("Bot", "local_dir_path")
    assert cfg.get("Feed_Command", "enabled") == "true"
    assert "; local comment" in overlay.read_text(encoding="utf-8")

    out = capsys.readouterr().out
    assert "[Bot] bot_name" in out
    assert "[Bot] local_dir_path" in out
    assert "the add-on writes these keys" in out


def test_overlay_without_managed_keys_is_ignored(layout: dict[str, Path]) -> None:
    run(**layout)
    overlay = layout["config_dir"] / "local" / "config.ini"
    assert not overlay.exists()
    backups = sorted(path.name for path in (layout["data_dir"] / "backups").iterdir())

    run(**layout)
    assert not overlay.exists()
    assert sorted(path.name for path in (layout["data_dir"] / "backups").iterdir()) == backups

    content = "; local only\n[Feed_Command]\nenabled = true\n"
    overlay.write_text(content, encoding="utf-8")
    run(**layout)
    assert overlay.read_text(encoding="utf-8") == content
    assert sorted(path.name for path in (layout["data_dir"] / "backups").iterdir()) == backups


def test_base_and_overlay_backups_do_not_collide(
    layout: dict[str, Path],
    blocked_exec: list[tuple[str, list[str]]],
) -> None:
    overlay = layout["config_dir"] / "local" / "config.ini"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("[Bot]\nbot_name = Override\n", encoding="utf-8")
    _options(layout["data_dir"], {"bot_name": "HA Radio"})

    run(**layout)

    base_backups = list((layout["data_dir"] / "backups").glob("config.ini.bak.*"))
    overlay_backups = list(
        (layout["data_dir"] / "backups" / "local").glob("config.ini.bak.*")
    )
    assert len(base_backups) == 1
    assert len(overlay_backups) == 1
    assert "bot_name = MeshCoreBot" in base_backups[0].read_text(encoding="utf-8")
    assert "bot_name = Override" in overlay_backups[0].read_text(encoding="utf-8")


def test_real_example_with_sample_options(tmp_path: Path, blocked_exec: list[tuple[str, list[str]]]) -> None:
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    run_kwargs = {"config_dir": config_dir, "data_dir": data_dir, "repo_root": ROOT}
    _options(data_dir, SAMPLE_OPTIONS)
    run(**run_kwargs)
    config_path = config_dir / "config.ini"
    text = config_path.read_text(encoding="utf-8")
    assert "# Connection type: serial, ble, or tcp" in text
    cfg = _ini(config_path)
    assert cfg.get("Connection", "connection_type") == "tcp"
    assert cfg.get("Connection", "hostname") == "radio.local"
    assert cfg.get("Connection", "tcp_port") == "5000"
    assert cfg.get("Connection", "serial_port") == "/dev/ttyUSB0"
    assert cfg.get("Connection", "ble_device_name") == ""
    assert cfg.get("Bot", "bot_name") == "HA Radio"
    assert cfg.get("Bot", "enabled") == "true"
    assert cfg.get("Logging", "log_level") == "DEBUG"
    assert cfg.get("Logging", "log_file") == ""
    assert cfg.get("Web_Viewer", "web_viewer_password") == "s3cret"
    assert cfg.get("Web_Viewer", "enabled") == "true"
    assert cfg.get("Web_Viewer", "host") == "0.0.0.0"
    assert cfg.get("Web_Viewer", "port") == "8080"
    assert cfg.get("Web_Viewer", "ha_ingress") == "true"
    assert cfg.get("Web_Viewer", "auto_start") == "true"
    assert cfg.get("Feed_Command", "enabled") == "false"
    assert cfg.get("Bot", "db_path") == str(data_dir / "databases" / "meshcore_bot.db")
    assert cfg.get("Bot", "local_dir_path") == str(config_dir / "local")
    owned = _managed(data_dir)
    assert owned == FIXED_PAIRS | {
        ("Connection", "connection_type"),
        ("Connection", "hostname"),
        ("Connection", "tcp_port"),
        ("Bot", "bot_name"),
        ("Logging", "log_level"),
        ("Web_Viewer", "web_viewer_password"),
    }
    assert ("Connection", "serial_port") not in owned
    file, args = blocked_exec[-1]
    assert file == sys.executable
    assert args[-2:] == ["--config", str(config_path)]
    assert args[1] == str(ROOT / "meshcore_bot.py")

    _assert_rerun_writes_nothing(run_kwargs, config_path, data_dir)


def test_bad_options_json_does_not_write(layout: dict[str, Path]) -> None:
    run(**layout)
    config_path = layout["config_dir"] / "config.ini"
    before = config_path.read_bytes()
    options = layout["data_dir"] / "options.json"
    options.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot parse"):
        run(**layout)
    options.write_text("[1]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        run(**layout)
    assert config_path.read_bytes() == before


def test_invalid_option_does_not_write(
    layout: dict[str, Path],
    blocked_exec: list[tuple[str, list[str]]],
) -> None:
    run(**layout)
    config_path = layout["config_dir"] / "config.ini"
    before = config_path.read_bytes()
    _options(layout["data_dir"], {"connection_type": "wifi"})
    with pytest.raises(ValueError, match="connection_type"):
        run(**layout)
    assert config_path.read_bytes() == before
    assert len(blocked_exec) == 1


def test_unparseable_config_is_left_in_place(layout: dict[str, Path]) -> None:
    run(**layout)
    config_path = layout["config_dir"] / "config.ini"
    config_path.write_text("[Connection]\nconnection_type = serial\n[Connection]\n", encoding="utf-8")
    before = config_path.read_bytes()
    with pytest.raises(ValueError, match="Cannot parse"):
        run(**layout)
    assert config_path.read_bytes() == before


def test_missing_example_does_not_exec(tmp_path: Path, blocked_exec: list[tuple[str, list[str]]]) -> None:
    with pytest.raises(FileNotFoundError):
        run(config_dir=tmp_path / "config", data_dir=tmp_path / "data", repo_root=tmp_path / "empty")
    assert blocked_exec == []


def test_main_exits_when_setup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise ValueError("nope")

    monkeypatch.setattr("modules.ha_addon.run", boom)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
