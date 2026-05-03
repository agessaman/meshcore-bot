"""Tests for meshcore_bot.py CLI config-inspection flags."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from meshcore_bot import main


def _write_config(path: Path) -> None:
    path.write_text(
        """[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0

[Bot]
db_path = /tmp/bot.db
api_token = super-secret-token

[Notifications]
smtp_user = alerts@example.com
smtp_password = hunter2
recipient = ops@example.com
""",
        encoding="utf-8",
    )


def test_show_config_prints_redacted_ini(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    config_path = tmp_path / "config.ini"
    _write_config(config_path)
    monkeypatch.setattr(sys, "argv", ["meshcore_bot.py", "--show-config", "--config", str(config_path)])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "[Bot]" in out
    assert "db_path = /tmp/bot.db" in out
    assert "api_token = ●●●●●●" in out
    assert "smtp_user = ●●●●●●" in out
    assert "smtp_password = ●●●●●●" in out


def test_show_config_json_prints_redacted_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.ini"
    _write_config(config_path)
    monkeypatch.setattr(
        sys, "argv", ["meshcore_bot.py", "--show-config-json", "--config", str(config_path)]
    )

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["Bot"]["db_path"] == "/tmp/bot.db"
    assert payload["Bot"]["api_token"] == "●●●●●●"
    assert payload["Notifications"]["smtp_password"] == "●●●●●●"


def test_show_config_missing_file_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_path = tmp_path / "missing.ini"
    monkeypatch.setattr(sys, "argv", ["meshcore_bot.py", "--show-config", "--config", str(missing_path)])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1

    err = capsys.readouterr().err
    assert "Config file not found" in err


def test_show_config_invalid_ini_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad_path = tmp_path / "bad.ini"
    bad_path.write_text("[Connection\nserial_port=/dev/ttyUSB0\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["meshcore_bot.py", "--show-config-json", "--config", str(bad_path)])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1

    err = capsys.readouterr().err
    assert "Invalid config file" in err
