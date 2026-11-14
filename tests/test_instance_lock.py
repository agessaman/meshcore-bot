"""Tests for the single-instance guard and its wiring into main()."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import meshcore_bot
from meshcore_bot import main
from modules.database_restore import configured_bot_database_path
from modules.instance_lock import (
    InstanceAlreadyRunning,
    InstanceLock,
    acquire_instance_lock,
    instance_lock_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Holds the lock in a separate process, the way a second bot would see it.
_HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
from modules.instance_lock import acquire_instance_lock
lock = acquire_instance_lock(sys.argv[2])
print("held", flush=True)
sys.stdin.read()
"""


def _spawn_holder(database: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(REPO_ROOT), str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


def _write_config(path: Path, db_path: str) -> None:
    path.write_text(
        f"[Connection]\nconnection_type = serial\n\n[Bot]\ndb_path = {db_path}\n",
        encoding="utf-8",
    )


class TestAcquireInstanceLock:
    def test_lock_file_sits_beside_the_database_and_records_the_pid(self, tmp_path: Path):
        database = tmp_path / "bot.db"
        lock = acquire_instance_lock(database)
        try:
            assert lock.path == tmp_path / "bot.db.lock"
            assert lock.path.read_text().strip() == str(os.getpid())
        finally:
            lock.release()

    def test_second_acquire_is_refused_and_names_the_holder(self, tmp_path: Path):
        database = tmp_path / "bot.db"
        holder = _spawn_holder(database)
        try:
            with pytest.raises(InstanceAlreadyRunning) as exc:
                acquire_instance_lock(database)
            assert exc.value.pid == holder.pid
            assert exc.value.lock_path == instance_lock_path(database)
            # The refused attempt must not clobber the holder's PID.
            assert instance_lock_path(database).read_text().strip() == str(holder.pid)
        finally:
            holder.kill()
            holder.wait()

    def test_lock_frees_when_the_holder_is_killed(self, tmp_path: Path):
        """SIGKILL runs no cleanup, so the stale file alone must not block a start."""
        database = tmp_path / "bot.db"
        holder = _spawn_holder(database)
        holder.kill()
        holder.wait()
        assert instance_lock_path(database).exists()

        lock = acquire_instance_lock(database)
        try:
            assert lock.path.read_text().strip() == str(os.getpid())
        finally:
            lock.release()

    def test_release_lets_the_next_instance_in(self, tmp_path: Path):
        database = tmp_path / "bot.db"
        acquire_instance_lock(database).release()
        acquire_instance_lock(database).release()

    def test_separate_databases_do_not_conflict(self, tmp_path: Path):
        first = acquire_instance_lock(tmp_path / "a.db")
        second = acquire_instance_lock(tmp_path / "b.db")
        first.release()
        second.release()


class TestWaitForLock:
    @staticmethod
    def _acquire_in_thread(config: Path, outcome: dict[str, object]) -> threading.Thread:
        def _acquire() -> None:
            try:
                outcome["lock"] = meshcore_bot._acquire_instance_lock(str(config), wait=True)
            except Exception as exc:  # surfaced by the caller's assertions
                outcome["error"] = exc

        return threading.Thread(target=_acquire, daemon=True)

    def test_waits_for_the_holder_to_exit_then_takes_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        config = tmp_path / "config.ini"
        database = tmp_path / "bot.db"
        _write_config(config, str(database))
        monkeypatch.setattr(meshcore_bot, "_LOCK_WAIT_INTERVAL_SECONDS", 0.05)

        holder = _spawn_holder(database)
        outcome: dict[str, object] = {}
        thread = self._acquire_in_thread(config, outcome)
        lock = None
        try:
            thread.start()
            thread.join(0.3)
            assert thread.is_alive(), "the waiting acquire returned while the lock was held"

            holder.kill()
            holder.wait()

            thread.join(5)
            assert not thread.is_alive(), "the waiting acquire never picked up the freed lock"
            assert "error" not in outcome
            lock = outcome.get("lock")
            assert isinstance(lock, InstanceLock)
            assert lock.path == instance_lock_path(database)
        finally:
            if holder.poll() is None:
                holder.kill()
            holder.wait()
            if lock is not None:
                lock.release()

        err = capsys.readouterr().err
        assert err.count("waiting for it to exit") == 1
        assert str(database) in err
        assert f"PID {holder.pid}" in err
        assert "Acquired the single-instance lock" in err

    def test_free_lock_returns_without_waiting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        config = tmp_path / "config.ini"
        database = tmp_path / "bot.db"
        _write_config(config, str(database))
        monkeypatch.setattr(meshcore_bot, "_LOCK_WAIT_INTERVAL_SECONDS", 0.05)

        lock = meshcore_bot._acquire_instance_lock(str(config), wait=True)
        try:
            assert lock is not None
            assert lock.path == instance_lock_path(database)
        finally:
            lock.release()

        assert "waiting for it to exit" not in capsys.readouterr().err

    def test_held_lock_still_exits_1_without_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        config = tmp_path / "config.ini"
        database = tmp_path / "bot.db"
        _write_config(config, str(database))
        monkeypatch.setattr(meshcore_bot, "_LOCK_WAIT_INTERVAL_SECONDS", 0.05)

        holder = _spawn_holder(database)
        try:
            with pytest.raises(SystemExit) as exc:
                meshcore_bot._acquire_instance_lock(str(config))
        finally:
            holder.kill()
            holder.wait()

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "already running" in err
        assert f"PID {holder.pid}" in err
        assert "waiting for it to exit" not in err

    @pytest.mark.parametrize(
        ("extra_argv", "expected_wait"),
        [(["--wait-for-lock"], True), ([], False)],
    )
    def test_main_passes_the_flag_to_the_lock(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        extra_argv: list[str],
        expected_wait: bool,
    ):
        config = tmp_path / "config.ini"
        _write_config(config, str(tmp_path / "bot.db"))
        calls: list[tuple[str, bool]] = []

        def _record(config_path, wait=False):
            calls.append((config_path, wait))
            raise SystemExit(0)  # stop before the radio work

        monkeypatch.setattr(meshcore_bot, "_acquire_instance_lock", _record)
        monkeypatch.setattr("meshcore_bot._collect_config_issues", lambda path: [])
        monkeypatch.setattr(
            sys, "argv", ["meshcore_bot.py", *extra_argv, "--config", str(config)]
        )

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 0
        assert calls == [(str(config), expected_wait)]


@pytest.mark.parametrize("relative_path", ["meshcore-bot.service", "scripts/build-deb.sh"])
def test_service_unit_starts_with_wait_for_lock(relative_path: str) -> None:
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    exec_starts = [
        line.strip() for line in text.splitlines() if line.strip().startswith("ExecStart=")
    ]
    assert exec_starts
    assert all(line.endswith("--wait-for-lock") for line in exec_starts)


class TestConfiguredBotDatabasePath:
    def test_relative_path_resolves_from_the_config_directory(self, tmp_path: Path):
        config = tmp_path / "config.ini"
        _write_config(config, "data/bot.db")
        assert configured_bot_database_path(config) == (tmp_path / "data" / "bot.db").resolve()

    def test_viewer_database_is_never_the_bot_database(self, tmp_path: Path):
        config = tmp_path / "config.ini"
        config.write_text(
            "[Bot]\ndb_path = :memory:\n\n[Web_Viewer]\ndb_path = viewer.db\n",
            encoding="utf-8",
        )
        assert configured_bot_database_path(config) is None

    def test_missing_config_has_no_database(self, tmp_path: Path):
        assert configured_bot_database_path(tmp_path / "absent.ini") is None


class TestMainRefusesSecondInstance:
    def _fail_if_built(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import modules.core

        def _unexpected(config_file):
            raise AssertionError("MeshCoreBot must not be constructed")

        monkeypatch.setattr(modules.core, "MeshCoreBot", _unexpected)

    def test_exits_1_before_restore_or_bot_construction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        config = tmp_path / "config.ini"
        database = tmp_path / "bot.db"
        _write_config(config, str(database))
        self._fail_if_built(monkeypatch)

        def _unexpected_restore(path):
            raise AssertionError("a pending restore must not run under a live bot")

        monkeypatch.setattr(
            "modules.database_restore.apply_pending_restores_from_config", _unexpected_restore
        )
        monkeypatch.setattr("meshcore_bot._collect_config_issues", lambda path: [])
        monkeypatch.setattr(sys, "argv", ["meshcore_bot.py", "--config", str(config)])

        holder = _spawn_holder(database)
        try:
            with pytest.raises(SystemExit) as exc:
                main()
        finally:
            holder.kill()
            holder.wait()

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "already running" in err
        assert f"PID {holder.pid}" in err

    def test_unwritable_lock_location_warns_and_starts_anyway(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        config = tmp_path / "config.ini"
        _write_config(config, str(tmp_path / "bot.db"))

        def _denied(database_path):
            raise PermissionError("read-only file system")

        monkeypatch.setattr("modules.instance_lock.acquire_instance_lock", _denied)

        import meshcore_bot

        assert meshcore_bot._acquire_instance_lock(str(config)) is None
        assert "could not take the single-instance lock" in capsys.readouterr().err

    def test_inspection_flags_work_while_a_bot_is_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        config = tmp_path / "config.ini"
        database = tmp_path / "bot.db"
        _write_config(config, str(database))
        monkeypatch.setattr(
            sys, "argv", ["meshcore_bot.py", "--show-config", "--config", str(config)]
        )

        holder = _spawn_holder(database)
        try:
            with pytest.raises(SystemExit) as exc:
                main()
        finally:
            holder.kill()
            holder.wait()

        assert exc.value.code == 0
        assert "[Bot]" in capsys.readouterr().out


def test_waiting_follows_a_db_path_edited_during_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock taken must be for the database startup will open, not the one first seen."""
    import meshcore_bot

    config = tmp_path / "config.ini"
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    _write_config(config, str(first))
    monkeypatch.setattr(meshcore_bot, "_LOCK_WAIT_INTERVAL_SECONDS", 0.0)

    def _edit_config_instead_of_sleeping(seconds):
        _write_config(config, str(second))

    monkeypatch.setattr(meshcore_bot.time, "sleep", _edit_config_instead_of_sleeping)

    holder = _spawn_holder(first)
    try:
        lock = meshcore_bot._acquire_instance_lock(str(config), wait=True)
    finally:
        holder.kill()
        holder.wait()
    try:
        assert lock.path == instance_lock_path(second.resolve())
    finally:
        lock.release()
