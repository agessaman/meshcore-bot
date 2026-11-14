"""Viewer exits when the bot that launched it is gone."""

import os
import signal
import subprocess
import sys
import threading
import time
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modules.web_viewer.parent_watch import (
    HARD_EXIT_GRACE_SEC,
    PARENT_EXITED_MESSAGE,
    POLL_INTERVAL_SEC,
    parent_is_gone,
    watch_parent,
)

# Intermediate parent: spawn the watcher child, record its pid, then sleep
# until SIGKILL. The child must be a separate process so killing the parent
# reparents it.
_PARENT_SCRIPT = """\
import os
import subprocess
import sys
import time

child_pid_path, child_log_path, child_script = sys.argv[1:4]
with open(child_log_path, "w", encoding="utf-8") as log_fh:
    child = subprocess.Popen(
        [sys.executable, child_script, str(os.getpid())],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
with open(child_pid_path, "w", encoding="utf-8") as fh:
    fh.write(str(child.pid))
while True:
    time.sleep(30)
"""

_CHILD_SCRIPT = """\
import logging
import sys

from modules.web_viewer.parent_watch import watch_parent

logger = logging.getLogger("modern_web_viewer")
logger.setLevel(logging.INFO)
logger.propagate = False
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
logger.addHandler(handler)
watch_parent(int(sys.argv[1]), logger=logger)
"""


def _pid_alive(pid: int) -> bool:
    """True when pid is a running process. Zombies count as gone."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        result = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    state = result.stdout.strip()
    if not state:
        return False
    return not state.startswith("Z")


def _kill_if_alive(pid: int | None) -> None:
    if pid is None or pid <= 1 or not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _wait_for_pidfile(path: Path, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text.isdigit():
                return int(text)
        time.sleep(0.05)
    raise AssertionError(f"child pid was not written to {path}")


def _make_bot(config_file: str):
    bot = MagicMock()
    bot.logger = MagicMock()
    bot.config_file = config_file
    config = ConfigParser()
    config.add_section("Bot")
    config.add_section("Web_Viewer")
    config.set("Web_Viewer", "host", "127.0.0.1")
    config.set("Web_Viewer", "port", "18765")
    config.set("Web_Viewer", "enabled", "false")
    config.set("Web_Viewer", "auto_start", "false")
    config.set("Web_Viewer", "debug", "false")
    bot.config = config
    bot.db_manager = MagicMock()
    bot.db_manager.db_path = ":memory:"
    return bot


def test_watcher_does_nothing_while_parent_is_alive():
    assert parent_is_gone(os.getppid()) is False
    fired: list[int] = []
    stop = threading.Event()
    thread = threading.Thread(
        target=watch_parent,
        kwargs={
            "parent_pid": os.getppid(),
            "poll_interval": 0.05,
            "stop_event": stop,
            "on_parent_gone": lambda: fired.append(1),
        },
        daemon=True,
    )
    thread.start()
    try:
        time.sleep(0.3)
        assert fired == []
        if sys.platform != "win32":
            assert thread.is_alive()
    finally:
        stop.set()
        thread.join(timeout=2)
    assert fired == []
    assert not thread.is_alive()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX reparents orphans; getppid is the check")
def test_parent_is_gone_compares_ppid_not_kill():
    # Our own pid is not our parent. kill(pid, 0) would still succeed.
    assert parent_is_gone(os.getppid()) is False
    assert parent_is_gone(os.getpid()) is True
    fired = threading.Event()
    watch_parent(os.getpid(), poll_interval=30, on_parent_gone=fired.set)
    assert fired.is_set()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX reparents orphans; Windows uses a process handle")
def test_child_exits_when_parent_is_sigkilled(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    parent_script = tmp_path / "parent_runner.py"
    child_script = tmp_path / "child_runner.py"
    parent_script.write_text(_PARENT_SCRIPT, encoding="utf-8")
    child_script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    child_pid_path = tmp_path / "child.pid"
    child_log_path = tmp_path / "child.log"
    parent_log_path = tmp_path / "parent.log"

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")

    parent: subprocess.Popen | None = None
    child_pid: int | None = None
    try:
        with parent_log_path.open("w", encoding="utf-8") as parent_log:
            parent = subprocess.Popen(
                [
                    sys.executable,
                    str(parent_script),
                    str(child_pid_path),
                    str(child_log_path),
                    str(child_script),
                ],
                cwd=str(repo),
                env=env,
                stdout=parent_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        child_pid = _wait_for_pidfile(child_pid_path, timeout=5)
        assert _pid_alive(child_pid), _failure_logs(parent_log_path, child_log_path)
        # One full poll must pass while the parent is still alive.
        time.sleep(POLL_INTERVAL_SEC + 0.5)
        assert _pid_alive(parent.pid)
        assert _pid_alive(child_pid), _failure_logs(parent_log_path, child_log_path)

        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)

        deadline = time.monotonic() + POLL_INTERVAL_SEC + HARD_EXIT_GRACE_SEC + 3
        while time.monotonic() < deadline and _pid_alive(child_pid):
            time.sleep(0.1)
        logs = _failure_logs(parent_log_path, child_log_path)
        assert not _pid_alive(child_pid), logs
        assert PARENT_EXITED_MESSAGE in child_log_path.read_text(encoding="utf-8"), logs
        assert "INFO" in child_log_path.read_text(encoding="utf-8")
    finally:
        _kill_if_alive(child_pid)
        if parent is not None and parent.poll() is None:
            _kill_if_alive(parent.pid)
        if parent is not None:
            try:
                parent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_if_alive(parent.pid)
                parent.wait(timeout=5)


def _failure_logs(parent_log: Path, child_log: Path) -> str:
    parent_text = parent_log.read_text(encoding="utf-8") if parent_log.is_file() else ""
    child_text = child_log.read_text(encoding="utf-8") if child_log.is_file() else ""
    return f"parent log:\n{parent_text}\nchild log:\n{child_text}"


def test_run_viewer_command_includes_exit_with_parent(tmp_path: Path):
    from modules.web_viewer.integration import WebViewerIntegration

    bot = _make_bot(str(tmp_path / "config.ini"))
    with patch("modules.web_viewer.integration.BotIntegration._init_http_session"), \
         patch("modules.web_viewer.integration.BotIntegration._init_packet_stream_table"), \
         patch("modules.web_viewer.integration.BotIntegration._start_drain_thread"):
        wvi = WebViewerIntegration(bot)
    wvi.port = 18765
    captured: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        captured.append(list(cmd))
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.returncode = 1
        return proc

    with patch("modules.web_viewer.integration.subprocess.Popen", side_effect=fake_popen), \
         patch("modules.web_viewer.integration.time.sleep"):
        wvi._run_viewer()

    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("app.py")
    flag = cmd.index("--exit-with-parent")
    assert cmd[flag + 1] == str(os.getpid())


def test_main_without_flag_does_not_start_watcher():
    with patch("modules.web_viewer.app.BotDataViewer") as viewer_cls, \
         patch("modules.web_viewer.parent_watch.start_parent_watcher") as start:
        viewer_cls.return_value.run.return_value = None
        from modules.web_viewer.app import main

        main(["--config", "unused.ini", "--port", "18765"])
    start.assert_not_called()
    viewer_cls.return_value.run.assert_called_once_with(
        host="127.0.0.1",
        port=18765,
        debug=False,
    )


def test_main_debug_without_flag_still_uses_default_reloader():
    with patch("modules.web_viewer.app.BotDataViewer") as viewer_cls, \
         patch("modules.web_viewer.parent_watch.start_parent_watcher") as start:
        from modules.web_viewer.app import main

        main(["--debug", "--port", "18765"])
    start.assert_not_called()
    viewer_cls.return_value.run.assert_called_once_with(
        host="127.0.0.1",
        port=18765,
        debug=True,
    )


def test_main_with_flag_starts_watcher_before_run():
    events: list[str] = []
    with patch("modules.web_viewer.app.BotDataViewer") as viewer_cls, \
         patch(
             "modules.web_viewer.parent_watch.start_parent_watcher",
             side_effect=lambda *args, **kwargs: events.append("watch"),
         ) as start:
        viewer_cls.return_value.run.side_effect = lambda **kwargs: events.append("run")
        from modules.web_viewer.app import main

        main([
            "--exit-with-parent", "4321",
            "--debug",
            "--config", "unused.ini",
            "--port", "18765",
        ])
    assert events == ["watch", "run"]
    start.assert_called_once_with(4321, logger=viewer_cls.return_value.logger)
    # The reloader would put the server one process further from the bot.
    viewer_cls.return_value.run.assert_called_once_with(
        host="127.0.0.1",
        port=18765,
        debug=True,
        use_reloader=False,
    )
