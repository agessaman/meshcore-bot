"""Home Assistant managed-key loader and the config.ini write guard."""

from __future__ import annotations

import configparser
import json
import logging
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest

from modules.ini_writer import IniValueError
from modules.managed_keys import (
    MANAGED_KEYS_ENV,
    ManagedKeyError,
    guard_managed_writes,
    is_managed,
    load_managed_keys,
)
from modules.settings_store import ConfigIniStore

LOGGER = "MeshCoreBot"


@contextmanager
def _watch(caplog: pytest.LogCaptureFixture):
    """Capture MeshCoreBot warnings even after the bot logger stops propagating."""
    logger = logging.getLogger(LOGGER)
    previous = logger.propagate
    logger.propagate = True
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            yield
    finally:
        logger.propagate = previous


def _point(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv(MANAGED_KEYS_ENV, str(path))


def _managed_file(tmp_path: Path, pairs: list[tuple[str, str]]) -> Path:
    path = tmp_path / "managed.json"
    path.write_text(json.dumps(pairs), encoding="utf-8")
    return path


@pytest.fixture
def make_store(tmp_path: Path) -> Callable[[str], ConfigIniStore]:
    """Build an INI-backed store over a fresh ``config.ini`` with ``text``."""
    def _make(text: str) -> ConfigIniStore:
        config_path = tmp_path / "config.ini"
        config_path.write_text(text, encoding="utf-8")
        config = configparser.ConfigParser()
        config.read(config_path, encoding="utf-8")
        return ConfigIniStore(config, str(config_path))

    return _make


def test_missing_env_is_empty(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv(MANAGED_KEYS_ENV, raising=False)
    with _watch(caplog):
        assert load_managed_keys() == frozenset()
    assert not caplog.records


def test_blank_env_is_empty(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv(MANAGED_KEYS_ENV, "  ")
    with _watch(caplog):
        assert load_managed_keys() == frozenset()
    assert not caplog.records


def test_missing_file_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _point(monkeypatch, tmp_path / "missing.json")
    with _watch(caplog):
        assert load_managed_keys() == frozenset()
    assert not caplog.records


def test_unreadable_file_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _point(monkeypatch, tmp_path)
    with _watch(caplog):
        assert load_managed_keys() == frozenset()
    assert "cannot read" in caplog.text.lower()


@pytest.mark.parametrize(
    "payload",
    ["{", "null", '{"Bot": "db_path"}', '[["Bot"]]', '[["Bot", 1]]', '[""]', '["Bot", "db_path"]'],
)
def test_malformed_file_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    payload: str,
) -> None:
    path = tmp_path / "managed.json"
    path.write_text(payload, encoding="utf-8")
    _point(monkeypatch, path)
    with _watch(caplog):
        assert load_managed_keys() == frozenset()
    assert "malformed" in caplog.text.lower()


def test_normal_file_normalizes_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "managed.json"
    path.write_text(
        json.dumps(
            [
                [" Web_Viewer ", "Port"],
                ["Bot", "db_path"],
                ["Connection", "hostname"],
                ["Bot", "DB_Path"],
            ]
        ),
        encoding="utf-8",
    )
    _point(monkeypatch, path)
    with _watch(caplog):
        loaded = load_managed_keys()
    assert loaded == {
        ("web_viewer", "port"),
        ("bot", "db_path"),
        ("connection", "hostname"),
    }
    assert not caplog.records
    assert is_managed("Web_Viewer", "PORT", loaded)
    assert is_managed("web_viewer", "port", loaded)
    assert not is_managed("Bot", "bot_name", loaded)


def test_empty_list_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "managed.json"
    path.write_text("[]\n", encoding="utf-8")
    _point(monkeypatch, path)
    with _watch(caplog):
        assert load_managed_keys() == frozenset()
    assert not caplog.records


def _config(text: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read_string(text)
    return parser


def test_guard_drops_unchanged_managed_key_and_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _point(monkeypatch, _managed_file(tmp_path, [("Bot", "DB_Path")]))
    config = _config("[Bot]\ndb_path = /data/db\n")
    updates, deletes = guard_managed_writes(
        {"Bot": {"DB_Path": "/data/db", "bot_name": "Zed"}},
        None,
        config,
    )
    assert updates == {"Bot": {"bot_name": "Zed"}}
    assert deletes is None


def test_guard_rejects_a_changed_managed_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _point(
        monkeypatch,
        _managed_file(tmp_path, [("Bot", "db_path"), ("Connection", "hostname")]),
    )
    config = _config("[Bot]\ndb_path = /data/db\n")
    with pytest.raises(ManagedKeyError, match=r"Managed by Home Assistant: \[bot\] db_path") as caught:
        guard_managed_writes(
            {"Bot": {"db_path": "/other", "bot_name": "Zed"}},
            {"Connection": ["hostname"]},
            config,
        )
    assert caught.value.conflicts == [("bot", "db_path"), ("connection", "hostname")]
    assert issubclass(ManagedKeyError, IniValueError)


def test_store_refuses_managed_write_and_leaves_disk_and_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_store: Callable[[str], ConfigIniStore],
) -> None:
    store = make_store("[Bot]\ndb_path = /data/db\nbot_name = A\n")
    config_path = Path(store.config_path)
    _point(monkeypatch, _managed_file(tmp_path, [("Bot", "db_path")]))
    with pytest.raises(ManagedKeyError):
        store.write_values("Bot", {"db_path": "/other", "bot_name": "B"})
    assert store.config.get("Bot", "db_path") == "/data/db"
    assert store.config.get("Bot", "bot_name") == "A"
    text = config_path.read_text(encoding="utf-8")
    assert "db_path = /data/db" in text
    assert "bot_name = B" not in text


@pytest.mark.parametrize(
    ("managed_section", "write_section"),
    [("Web_Viewer", "web_viewer"), ("web_viewer", "Web_Viewer")],
)
def test_store_refuses_managed_write_across_section_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_store: Callable[[str], ConfigIniStore],
    managed_section: str,
    write_section: str,
) -> None:
    store = make_store("[Web_Viewer]\nport = 8080\n")
    config_path = Path(store.config_path)
    original = config_path.read_text(encoding="utf-8")
    _point(monkeypatch, _managed_file(tmp_path, [(managed_section, "port")]))
    with pytest.raises(ManagedKeyError):
        store.write_values(write_section, {"port": "9"})
    assert store.config.get("Web_Viewer", "port") == "8080"
    assert config_path.read_text(encoding="utf-8") == original


def test_store_allows_unchanged_managed_write_with_different_section_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_store: Callable[[str], ConfigIniStore],
) -> None:
    store = make_store("[Web_Viewer]\nport = 8080\n")
    config_path = Path(store.config_path)
    original = config_path.read_text(encoding="utf-8")
    _point(monkeypatch, _managed_file(tmp_path, [("web_viewer", "port")]))
    store.write_values("web_viewer", {"port": "8080"})
    assert store.config.get("Web_Viewer", "port") == "8080"
    assert not store.config.has_section("web_viewer")
    assert config_path.read_text(encoding="utf-8") == original


def test_store_writes_unmanaged_keys_and_leaves_an_unchanged_managed_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_store: Callable[[str], ConfigIniStore],
) -> None:
    store = make_store("[Bot]\ndb_path = /data/db\nbot_name = A\n")
    config_path = Path(store.config_path)
    _point(monkeypatch, _managed_file(tmp_path, [("Bot", "db_path")]))
    store.write_values("Bot", {"db_path": "/data/db", "bot_name": "B"})
    assert store.config.get("Bot", "bot_name") == "B"
    assert store.config.get("Bot", "db_path") == "/data/db"
    text = config_path.read_text(encoding="utf-8")
    assert "db_path = /data/db" in text
    assert "bot_name = B" in text
