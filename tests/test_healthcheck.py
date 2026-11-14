"""Health check command: fresh heartbeat, stale file, or nothing to check."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from modules.healthcheck import main
from modules.heartbeat import heartbeat_path, touch_heartbeat

REPO_ROOT = Path(__file__).resolve().parents[1]

# Same imports `python -m modules.healthcheck` performs, then list anything heavy.
_IMPORT_PROBE = r"""
import runpy
import sys

sys.argv = ["modules.healthcheck", "--config", "/nonexistent"]
try:
    runpy.run_module("modules.healthcheck", run_name="__main__")
except SystemExit:
    pass

roots = ("meshcore", "flask", "modules.core")
found = [
    name
    for name in sys.modules
    if name in roots or any(name.startswith(root + ".") for root in roots)
]
sys.stderr.write("\n".join(found))
"""


def _config(tmp_path: Path, db_path: str) -> Path:
    path = tmp_path / "config.ini"
    path.write_text(f"[Bot]\ndb_path = {db_path}\n", encoding="utf-8")
    return path


def test_fresh_heartbeat_exits_0(tmp_path: Path, capsys) -> None:
    database = tmp_path / "bot.db"
    config = _config(tmp_path, str(database))
    path = heartbeat_path(database)
    assert path is not None
    touch_heartbeat(path)
    assert main(["--config", str(config)]) == 0
    assert capsys.readouterr().out == ""


def test_stale_heartbeat_exits_1(tmp_path: Path, capsys) -> None:
    database = tmp_path / "bot.db"
    config = _config(tmp_path, str(database))
    path = heartbeat_path(database)
    assert path is not None
    touch_heartbeat(path)
    os.utime(path, (time.time() - 400, time.time() - 400))
    assert main(["--config", str(config), "--max-age", "120"]) == 1
    line = capsys.readouterr().out.strip()
    assert line.startswith("heartbeat stale by ")
    assert line.endswith(" seconds")
    seconds = int(line.removeprefix("heartbeat stale by ").removesuffix(" seconds"))
    assert 270 <= seconds <= 290


def test_missing_heartbeat_exits_1(tmp_path: Path, capsys) -> None:
    database = tmp_path / "bot.db"
    config = _config(tmp_path, str(database))
    assert main(["--config", str(config)]) == 1
    assert capsys.readouterr().out.strip() == "heartbeat file missing"


def test_missing_config_exits_1(tmp_path: Path, capsys) -> None:
    """A check aimed at the wrong path must not report healthy."""
    assert main(["--config", str(tmp_path / "absent.ini")]) == 1
    assert "config not found" in capsys.readouterr().out


def test_memory_database_exits_0(tmp_path: Path, capsys) -> None:
    config = _config(tmp_path, ":memory:")
    assert main(["--config", str(config)]) == 0
    assert "nothing to check" in capsys.readouterr().out
    assert list(tmp_path.glob("*.heartbeat")) == []


def test_unreadable_config_exits_1(tmp_path: Path, capsys) -> None:
    config = tmp_path / "config.ini"
    config.write_text("[Bot]\ndb_path = %(missing)s\n", encoding="utf-8")
    assert main(["--config", str(config)]) == 1
    assert capsys.readouterr().out.strip() == "config unreadable"


def test_module_does_not_import_meshcore_flask_or_core() -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONSTARTUP", None)
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""


def test_heartbeat_far_ahead_of_the_clock_exits_1(tmp_path: Path, capsys) -> None:
    """A clock moved back must not keep a wedged bot looking healthy."""
    database = tmp_path / "bot.db"
    config = _config(tmp_path, str(database))
    path = heartbeat_path(database)
    assert path is not None
    touch_heartbeat(path)
    os.utime(path, (time.time() + 3600, time.time() + 3600))
    assert main(["--config", str(config), "--max-age", "120"]) == 1
    assert "ahead of the clock" in capsys.readouterr().out


def test_symlinked_database_path_checks_the_file_the_bot_writes(tmp_path: Path, capsys) -> None:
    """The bot keeps an absolute symlink path; the check resolves it. Both must agree."""
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias.db"
    alias.symlink_to(real / "actual.db")
    config = _config(tmp_path, str(alias))

    written = heartbeat_path(str(alias))  # what the running bot touches
    assert written is not None
    touch_heartbeat(written)

    assert written == real / "actual.db.heartbeat"
    assert main(["--config", str(config)]) == 0
