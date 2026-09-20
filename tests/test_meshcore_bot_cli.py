"""Tests for meshcore_bot.py CLI config-inspection flags."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from meshcore_bot import (
    _MAX_CONFIG_ISSUES_SHOWN,
    _collect_config_issues,
    _report_config_issues,
    main,
)


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


class _RecordingLogger:
    def __init__(self):
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def warning(self, msg):
        self.warnings.append(msg)

    def error(self, msg):
        self.errors.append(msg)


class TestReportConfigIssues:
    """Startup config findings must reach the configured log file, not just stderr:
    an operator reading logs/ never saw the linter name their misspelled key."""

    def test_findings_go_to_the_logger_when_there_is_one(self):
        logger = _RecordingLogger()
        _report_config_issues(
            [("warning", "[Test_Command] unknown key 'alias'. Did you mean 'aliases'?")],
            logger,
        )
        assert logger.warnings == [
            "Config warning: [Test_Command] unknown key 'alias'. Did you mean 'aliases'?"
        ]
        assert logger.errors == []

    def test_errors_log_at_error_level(self):
        logger = _RecordingLogger()
        _report_config_issues([("error", "boom"), ("warning", "meh")], logger)
        assert logger.errors == ["Config error: boom"]
        assert logger.warnings == ["Config warning: meh"]

    def test_without_a_logger_it_still_reaches_stderr(self, capsys):
        """The window before the bot is constructed, where stderr is all there is."""
        _report_config_issues([("warning", "early problem")])
        captured = capsys.readouterr()
        assert "Config warning: early problem" in captured.err
        assert captured.out == ""

    def test_long_reports_are_truncated_with_a_pointer(self):
        logger = _RecordingLogger()
        issues = [("warning", f"issue {i}") for i in range(_MAX_CONFIG_ISSUES_SHOWN + 4)]
        _report_config_issues(issues, logger)
        assert len(logger.warnings) == _MAX_CONFIG_ISSUES_SHOWN + 1
        assert "and 4 more issue(s)" in logger.warnings[-1]
        assert "--validate-config" in logger.warnings[-1]

    def test_nothing_is_emitted_when_the_config_is_clean(self, capsys):
        logger = _RecordingLogger()
        _report_config_issues([], logger)
        assert logger.warnings == [] and logger.errors == []
        assert capsys.readouterr().err == ""


class TestCollectConfigIssues:
    """Only errors and warnings are worth an operator's attention at startup."""

    def test_info_severity_is_filtered_out(self, tmp_path: Path):
        cfg = tmp_path / "config.ini"
        cfg.write_text(
            "[Test_Command]\nenabled = true\nalias = p,path\n\n[Nonsense_Section]\nx = 1\n",
            encoding="utf-8",
        )
        issues = _collect_config_issues(str(cfg))
        assert all(sev in ("error", "warning") for sev, _ in issues)
        assert any("alias" in msg and "aliases" in msg for _, msg in issues)

    def test_a_broken_linter_never_blocks_startup(self, monkeypatch, capsys):
        import modules.config_validation as cv

        def explode(_path):
            raise RuntimeError("linter exploded")

        monkeypatch.setattr(cv, "validate_config", explode)
        assert _collect_config_issues("whatever.ini") == []
        assert "Config validation skipped" in capsys.readouterr().err
