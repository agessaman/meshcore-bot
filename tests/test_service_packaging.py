"""Regression tests for wheel contents and hardened service deployment policy."""

from __future__ import annotations

import configparser
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from scripts.migrate_service_layout import migrate_service_layout
from scripts.preserve_service_alternatives import (
    backup_installed_only,
    restore_backup,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_service_units_do_not_make_code_tree_writable():
    unit = (REPO_ROOT / "meshcore-bot.service").read_text(encoding="utf-8")
    assert "--config /etc/meshcore-bot/config.ini" in unit
    assert "ReadWritePaths=/opt/meshcore-bot" not in unit
    assert "ReadWritePaths=/var/lib/meshcore-bot" in unit
    assert "ReadWritePaths=/etc/meshcore-bot" in unit
    assert "UMask=0077" in unit
    assert "MemoryMax=1G" in unit
    assert "CPUQuota=200%" in unit
    assert "MemoryMax=512M" not in unit
    assert "CPUQuota=50%" not in unit

    deb_builder = (REPO_ROOT / "scripts/build-deb.sh").read_text(encoding="utf-8")
    assert "MemoryMax=1G" in deb_builder
    assert "CPUQuota=200%" in deb_builder
    assert "MemoryMax=512M" not in deb_builder
    assert "CPUQuota=50%" not in deb_builder
    assert 'chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_ROOT}"' not in deb_builder
    assert "migrate_service_layout.py" in deb_builder
    assert 'chmod 0600 "${BUILD_DIR}${CONF_DIR}/config.ini"' in deb_builder
    assert (
        'chown -R "${SERVICE_USER}:${SERVICE_USER}" "${CONF_DIR}" "${DATA_DIR}" "${LOG_DIR}"'
        in deb_builder
    )
    assert 'find "${CONF_DIR}" "${DATA_DIR}" -type f -exec chmod 0600' in deb_builder
    assert 'VENV_BUILD="${INSTALL_ROOT}/.venv-build-$$"' in deb_builder
    assert 'if [ ! -d "${INSTALL_ROOT}/venv" ]' not in deb_builder
    assert 'scripts/rewrite_venv_shebangs.sh" "${INSTALL_ROOT}/venv"' in deb_builder
    assert '/bin/python" -m pip install --quiet --upgrade pip' in deb_builder
    assert "/bin/pip\" install" not in deb_builder
    assert (
        'mv "${VENV_OLD}" "${INSTALL_ROOT}/venv"' in deb_builder
        and deb_builder.index('rewrite_venv_shebangs.sh" "${INSTALL_ROOT}/venv"')
        < deb_builder.index('rm -rf "${VENV_OLD}"')
    )
    assert 'scripts/debian_service_state.sh" "${BUILD_DIR}/DEBIAN/preinst"' in deb_builder
    assert 'debian_service_state.sh" restore "${2:-}"' in deb_builder
    assert 'debian_service_state.sh preremove "${1:-}"' in deb_builder
    assert 'scripts/armv7_pip_args.sh' in deb_builder
    assert 'configure_armv7_pip_args "${INSTALL_ROOT}/requirements.txt"' in deb_builder
    assert 'pip install --quiet "${ARMV7_PIP_ARGS[@]}" -r "${INSTALL_ROOT}/requirements.txt"' in deb_builder
    assert (
        deb_builder.index('configure_armv7_pip_args "${INSTALL_ROOT}/requirements.txt"')
        < deb_builder.index(
            'pip install --quiet "${ARMV7_PIP_ARGS[@]}" -r "${INSTALL_ROOT}/requirements.txt"'
        )
    )


def test_standalone_installer_separates_code_and_private_state():
    installer = (REPO_ROOT / "install-service.sh").read_text(encoding="utf-8")
    assert 'chown -R "root:$CODE_GROUP" "$INSTALL_DIR"' in installer
    assert 'chown -R "$SERVICE_USER:$SERVICE_GROUP" "$CONF_DIR" "$STATE_DIR"' in installer
    assert 'find "$CONF_DIR" "$STATE_DIR" -type f -exec chmod 600' in installer
    assert 'systemctl stop "$SERVICE_NAME"' in installer
    assert "migrate_service_layout.py" in installer
    assert "rsync -a --delete" in installer
    assert "rsync -a --update" not in installer
    assert "source-authoritative secure install" in installer
    assert "preserve_service_alternatives.py" in installer
    assert 'VENV_BUILD="$INSTALL_DIR/.venv-build-$$"' in installer
    assert 'rewrite_venv_shebangs.sh" "$INSTALL_DIR/venv"' in installer
    assert 'bash "$rewrite_helper" "$venv"' in installer
    assert "update_venv_in_place" in installer
    assert installer.index("update_venv_in_place()") < installer.index(
        'bash "$rewrite_helper" "$venv"'
    )
    assert installer.index('bash "$rewrite_helper" "$venv"') < installer.index(
        'harden_venv_permissions "$venv"'
    )
    assert "Restored the previous virtualenv after shebang rewrite failure" in installer
    assert '"$VENV_PYTHON" -m pip install --quiet "better-profanity' in installer
    assert '"$VENV_PYTHON" -m pip install --quiet "pycountry' in installer
    assert '"$INSTALL_DIR/venv/bin/pip"' not in installer
    assert 'scripts/armv7_pip_args.sh' in installer
    assert "configure_armv7_pip_args" in installer
    assert 'pip install --quiet "${ARMV7_PIP_ARGS[@]}" -r' in installer
    assert 'Preserving existing virtual environment' not in installer
    assert "Python 3.10+ installed" in installer
    assert "sys.version_info < (3, 10)" in installer
    assert installer.index("command -v rsync") < installer.index(
        "# Stop a running legacy service"
    )
    assert "trap restore_active_service_on_failure EXIT" in installer
    assert "trap - EXIT" in installer
    assert 'SERVICE_RESTART_PENDING=true' in installer
    assert "SERVICE_RESTART_SAFE=false" in installer
    assert "restart_previously_active_service" in installer
    assert "Previously active service was restarted after the failed upgrade" in installer
    assert 'sync_executable_tree "$dest_dir" "$rollback_backup"' in installer
    assert 'sync_executable_tree "$rollback_backup" "$dest_dir"' in installer
    assert "Refusing to restart a potentially partial code tree" in installer


def _run_armv7_pip_args(
    machine: str, requirements: Path, extra_bash: str = ""
) -> subprocess.CompletedProcess[str]:
    helper = REPO_ROOT / "scripts/armv7_pip_args.sh"
    return subprocess.run(
        [
            "bash",
            "-c",
            f"""
            uname() {{ echo {machine}; }}
            {extra_bash}
            # shellcheck disable=SC1091
            source "$1"
            configure_armv7_pip_args "$2"
            printf '%s\\n' "${{ARMV7_PIP_ARGS[@]}}"
            """,
            "armv7-pip-args-test",
            str(helper),
            str(requirements),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_armv7_pip_args_helper_contents() -> None:
    helper = (REPO_ROOT / "scripts/armv7_pip_args.sh").read_text(encoding="utf-8")
    assert "armv6l|armv7l" in helper
    assert "--extra-index-url https://www.piwheels.org/simple" in helper
    assert "constraints-armv7.txt" in helper
    assert "ARMV7_PIP_ARGS+=(-c" in helper
    assert 'source this file; do not execute it' in helper


def test_armv7_pip_args_helper_refuses_direct_execution() -> None:
    helper = REPO_ROOT / "scripts/armv7_pip_args.sh"
    result = subprocess.run(
        ["bash", str(helper)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "source this file" in result.stderr


def test_armv7_pip_args_helper_is_noop_off_32bit_arm(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("flask-compress>=1.14\n", encoding="utf-8")
    result = _run_armv7_pip_args("x86_64", requirements)
    assert result.stdout.strip() == ""
    assert result.stderr.strip() == ""


def test_armv7_pip_args_helper_selects_piwheels_on_armv6l(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("flask-compress>=1.14\n", encoding="utf-8")
    (tmp_path / "constraints-armv7.txt").write_text(
        'brotli <= 1.1.0 ; platform_machine == "armv6l"\n',
        encoding="utf-8",
    )
    result = _run_armv7_pip_args("armv6l", requirements)
    args = result.stdout.split()
    assert args[:2] == ["--extra-index-url", "https://www.piwheels.org/simple"]
    assert "-c" in args
    assert str(tmp_path / "constraints-armv7.txt") in args
    assert "32-bit ARM detected" in result.stderr


def test_armv7_pip_args_helper_warns_when_constraints_missing(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("flask-compress>=1.14\n", encoding="utf-8")
    result = _run_armv7_pip_args("armv7l", requirements)
    assert result.stdout.split() == [
        "--extra-index-url",
        "https://www.piwheels.org/simple",
    ]
    assert "constraints-armv7.txt not found" in result.stderr
    assert "brotli and ephem will compile from source" in result.stderr


def test_armv7_pip_args_helper_uses_print_info_when_defined(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("flask-compress>=1.14\n", encoding="utf-8")
    (tmp_path / "constraints-armv7.txt").write_text("brotli <= 1.1.0\n", encoding="utf-8")
    result = _run_armv7_pip_args(
        "armv7l",
        requirements,
        extra_bash='print_info() { echo "INFO:$1"; }',
    )
    assert "INFO:32-bit ARM detected" in result.stdout + result.stderr


def test_relocated_venv_shebangs_are_rewritten(tmp_path: Path) -> None:
    """Regression for issue #229: atomic venv mv leaves broken console shebangs."""
    build = tmp_path / ".venv-build-test"
    final = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(build)], check=True)

    pip_build = build / "bin" / "pip"
    assert pip_build.is_file()
    shebang_before = pip_build.read_text(encoding="utf-8").splitlines()[0]
    assert str(build) in shebang_before

    build.rename(final)
    pip_final = final / "bin" / "pip"
    # After relocate, the baked shebang still points at the old build path.
    assert str(build) in pip_final.read_text(encoding="utf-8").splitlines()[0]
    try:
        broken = subprocess.run(
            [str(pip_final), "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert broken.returncode != 0
    except FileNotFoundError:
        # macOS/execve reports a missing shebang interpreter as ENOENT on the
        # script path itself rather than a non-zero exit from pip.
        pass

    rewrite = REPO_ROOT / "scripts" / "rewrite_venv_shebangs.sh"
    subprocess.run(["bash", str(rewrite), str(final)], check=True)

    shebang_after = pip_final.read_text(encoding="utf-8").splitlines()[0]
    assert shebang_after == f"#!{final / 'bin' / 'python'}"
    fixed = subprocess.run(
        [str(pip_final), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert fixed.returncode == 0, fixed.stderr
    assert "pip" in fixed.stdout.lower()


def test_service_documentation_matches_hardened_layout() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    getting_started = (REPO_ROOT / "docs/getting-started.md").read_text(encoding="utf-8")
    service_docs = (REPO_ROOT / "docs/service-installation.md").read_text(
        encoding="utf-8"
    )
    upgrade_docs = (REPO_ROOT / "docs/upgrade.md").read_text(encoding="utf-8")

    for text in (readme, getting_started, service_docs):
        assert "sudo nano /opt/meshcore-bot/config.ini" not in text
        assert "sudo nano /etc/meshcore-bot/config.ini" in text

    assert "Python 3.10+" in service_docs
    assert "`rsync`" in service_docs
    assert "sudo chown -R meshcore:meshcore /opt/meshcore-bot" not in service_docs
    assert "patches the meshcore file" not in service_docs
    assert "/var/lib/meshcore-bot" in service_docs
    assert "venv/bin/python -m pip" in service_docs
    assert "venv/bin/pip install" not in service_docs
    assert "venv/bin/pip list" not in service_docs
    assert "Upgrading from v0.9.3 to v1.0.0" in upgrade_docs


def test_service_sync_preserves_only_installed_only_alternatives(tmp_path: Path) -> None:
    source = tmp_path / "source"
    installed = tmp_path / "installed"
    backup = tmp_path / "backup"
    (source / "nested").mkdir(parents=True)
    (installed / "nested").mkdir(parents=True)

    (source / "shipped.py").write_text("SOURCE = 2\n", encoding="utf-8")
    (installed / "shipped.py").write_text("LOCAL = 1\n", encoding="utf-8")
    (installed / "custom.py").write_text("CUSTOM = 1\n", encoding="utf-8")
    (installed / "nested" / "custom.py").write_text("NESTED = 1\n", encoding="utf-8")
    (installed / "ignored.pyc").write_bytes(b"cache")
    (installed / "custom-link.py").symlink_to("custom.py")

    preserved = backup_installed_only(source, installed, backup)

    assert preserved == [
        Path("custom-link.py"),
        Path("custom.py"),
        Path("nested/custom.py"),
    ]
    assert not (backup / "shipped.py").exists()
    assert (backup / "custom-link.py").is_symlink()
    assert (backup / "custom-link.py").readlink() == Path("custom.py")
    assert not (backup / "ignored.pyc").exists()

    shutil.rmtree(installed)
    installed.mkdir()
    (installed / "shipped.py").write_text("SOURCE = 2\n", encoding="utf-8")
    (installed / "stale.py").write_text("STALE = 1\n", encoding="utf-8")

    restored = restore_backup(backup, installed)

    assert restored == preserved
    assert (installed / "shipped.py").read_text(encoding="utf-8") == "SOURCE = 2\n"
    assert (installed / "custom.py").read_text(encoding="utf-8") == "CUSTOM = 1\n"
    assert (installed / "custom-link.py").is_symlink()
    assert (installed / "custom-link.py").readlink() == Path("custom.py")
    assert (installed / "nested" / "custom.py").read_text(encoding="utf-8") == "NESTED = 1\n"
    assert not backup.exists()


def test_service_sync_rolls_back_when_alternative_restore_fails(
    tmp_path: Path,
) -> None:
    installer = (REPO_ROOT / "install-service.sh").read_text(encoding="utf-8")
    function_block = installer.split("sync_executable_tree() {", 1)[1].split(
        "# Copy files using smart copy function", 1
    )[0]

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    rsync_count = tmp_path / "rsync-count"
    rsync_count.write_text("0\n", encoding="utf-8")
    fake_rsync = fake_bin / "rsync"
    fake_rsync.write_text(
        """#!/bin/bash
count_file="${FAKE_RSYNC_COUNT:?}"
count="$(cat "$count_file")"
printf '%s\\n' "$((count + 1))" > "$count_file"
""",
        encoding="utf-8",
    )
    fake_rsync.chmod(0o755)
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        """#!/bin/bash
case " $* " in
    *" backup "*) exit 0 ;;
    *" restore "*) exit 1 ;;
    *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    harness = tmp_path / "sync-harness.sh"
    harness.write_text(
        """#!/bin/bash
print_info() { :; }
print_warning() { :; }
print_error() { :; }
print_success() { :; }
SCRIPT_DIR="${FAKE_SCRIPT_DIR:?}"
SERVICE_RESTART_SAFE=true
sync_executable_tree() {
"""
        + function_block
        + """
copy_files_smart "$FAKE_SOURCE" "$FAKE_INSTALLED"
result=$?
printf 'result=%s safe=%s\\n' "$result" "$SERVICE_RESTART_SAFE"
""",
        encoding="utf-8",
    )
    harness.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["FAKE_RSYNC_COUNT"] = str(rsync_count)
    env["FAKE_SCRIPT_DIR"] = str(REPO_ROOT)
    env["FAKE_SOURCE"] = str(tmp_path / "source")
    env["FAKE_INSTALLED"] = str(tmp_path / "installed")
    env["TMPDIR"] = str(tmp_path)
    result = subprocess.run(
        ["bash", str(harness)],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "result=1 safe=true"
    assert rsync_count.read_text(encoding="utf-8").strip() == "3"


def _service_state_test_environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_state = tmp_path / "systemctl-state"
    maint_state = tmp_path / "maint-state"
    fake_bin.mkdir()
    fake_state.mkdir()
    maint_state.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        """#!/bin/bash
set -e
state="${FAKE_SYSTEMCTL_STATE:?}"
case "${1:-}" in
    is-active) [ -f "$state/active" ] ;;
    is-enabled) [ -f "$state/enabled" ] ;;
    start) touch "$state/active" ;;
    stop) rm -f "$state/active" ;;
    enable) touch "$state/enabled" ;;
    disable) rm -f "$state/enabled" ;;
    daemon-reload) : ;;
    *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["FAKE_SYSTEMCTL_STATE"] = str(fake_state)
    env["MESHCORE_MAINT_STATE_DIR"] = str(maint_state)
    env["MESHCORE_MAINT_POLICY_MARKER"] = str(tmp_path / "policy-marker")
    return env, fake_state, maint_state


def _run_service_state(script: Path, *args: str, env: dict[str, str]) -> None:
    subprocess.run(
        ["bash", str(script), *args],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )


def test_first_hardened_deb_upgrade_survives_legacy_prerm(tmp_path: Path) -> None:
    """Model Debian's old prerm -> new preinst -> new postinst ordering."""
    env, service_state, maint_state = _service_state_test_environment(tmp_path)
    helper = REPO_ROOT / "scripts/debian_service_state.sh"
    preinst = tmp_path / "preinst"
    shutil.copy2(helper, preinst)

    # The baseline package's old prerm destroys state before new code runs.
    # The one-time migration policy favors availability and records completion.
    (service_state / "enabled").touch()
    (service_state / "active").touch()
    (service_state / "enabled").unlink()
    (service_state / "active").unlink()
    _run_service_state(preinst, "upgrade", "0.9.2", env=env)
    assert not list(maint_state.iterdir())

    _run_service_state(helper, "restore", "0.9.2", env=env)

    assert (service_state / "enabled").is_file()
    assert (service_state / "active").is_file()
    assert not list(maint_state.iterdir())
    assert Path(env["MESHCORE_MAINT_POLICY_MARKER"]).is_file()


def test_deb_upgrade_preserves_intentionally_disabled_stopped_service(
    tmp_path: Path,
) -> None:
    env, service_state, maint_state = _service_state_test_environment(tmp_path)
    helper = REPO_ROOT / "scripts/debian_service_state.sh"
    preinst = tmp_path / "preinst"
    shutil.copy2(helper, preinst)
    Path(env["MESHCORE_MAINT_POLICY_MARKER"]).touch()

    # Hardened old prerm runs first and writes no state markers because this
    # service was intentionally disabled/stopped.  New preinst preserves that.
    _run_service_state(helper, "preremove", "upgrade", env=env)
    _run_service_state(preinst, "upgrade", "0.9.2", env=env)
    _run_service_state(helper, "restore", "0.9.2", env=env)

    assert not (service_state / "enabled").exists()
    assert not (service_state / "active").exists()
    assert not list(maint_state.iterdir())


def test_later_deb_upgrade_preserves_enabled_active_service(tmp_path: Path) -> None:
    env, service_state, maint_state = _service_state_test_environment(tmp_path)
    helper = REPO_ROOT / "scripts/debian_service_state.sh"
    preinst = tmp_path / "preinst"
    shutil.copy2(helper, preinst)
    Path(env["MESHCORE_MAINT_POLICY_MARKER"]).touch()
    (service_state / "enabled").touch()
    (service_state / "active").touch()

    # Real Debian order: hardened old prerm, new preinst, new postinst.
    _run_service_state(helper, "preremove", "upgrade", env=env)
    assert not (service_state / "active").exists()
    assert (maint_state / "meshcore-bot-was-enabled").is_file()
    assert (maint_state / "meshcore-bot-was-active").is_file()
    _run_service_state(preinst, "upgrade", "0.9.2", env=env)
    _run_service_state(helper, "restore", "0.9.2", env=env)

    assert (service_state / "enabled").is_file()
    assert (service_state / "active").is_file()
    assert not list(maint_state.iterdir())


def test_relative_legacy_paths_are_migrated_without_losing_data(tmp_path):
    # URI-significant characters prove the read-only SQLite URI is encoded.
    legacy = tmp_path / "legacy #1?"
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    config_dir = tmp_path / "config"
    legacy.mkdir()
    config_dir.mkdir()
    (legacy / "local" / "commands").mkdir(parents=True)
    (legacy / "local" / "commands" / "custom.py").write_text("VALUE = 1\n", encoding="utf-8")
    (legacy / "meshcore_bot.log").write_text("old log\n", encoding="utf-8")

    db_path = legacy / "meshcore_bot.db"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE messages (body TEXT)")
    connection.execute("INSERT INTO messages VALUES ('preserved')")
    connection.commit()

    config_path = config_dir / "config.ini"
    config_path.write_text(
        "# keep this comment\n"
        "[Bot]\n"
        "db_path = meshcore_bot.db\n"
        "local_dir_path = local\n"
        "\n[Logging]\n"
        "log_file = meshcore_bot.log\n",
        encoding="utf-8",
    )

    try:
        updates = migrate_service_layout(config_path, legacy, state, logs)
    finally:
        connection.close()

    assert set(updates) == {"Bot", "Logging"}
    migrated = sqlite3.connect(state / "meshcore_bot.db")
    try:
        assert migrated.execute("SELECT body FROM messages").fetchall() == [("preserved",)]
    finally:
        migrated.close()
    assert (state / "local" / "commands" / "custom.py").is_file()
    assert (logs / "meshcore_bot.log").read_text(encoding="utf-8") == "old log\n"
    assert config_path.read_text(encoding="utf-8").startswith("# keep this comment\n")

    parsed = configparser.ConfigParser()
    parsed.read(config_path)
    assert parsed.get("Bot", "db_path") == str((state / "meshcore_bot.db").resolve())
    assert parsed.get("Bot", "local_dir_path") == str((state / "local").resolve())
    assert parsed.get("Logging", "log_file") == str((logs / "meshcore_bot.log").resolve())


def test_absolute_custom_paths_are_preserved(tmp_path):
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[Bot]\n"
        f"db_path = {tmp_path / 'custom.db'}\n"
        f"local_dir_path = {tmp_path / 'custom-local'}\n"
        "[Logging]\nlog_file = \n",
        encoding="utf-8",
    )
    before = config_path.read_text(encoding="utf-8")

    updates = migrate_service_layout(
        config_path,
        tmp_path / "legacy",
        tmp_path / "state",
        tmp_path / "logs",
    )

    assert updates == {}
    assert config_path.read_text(encoding="utf-8") == before


def test_relative_legacy_database_replaces_but_preserves_stale_target(tmp_path):
    legacy = tmp_path / "legacy"
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    legacy.mkdir()
    state.mkdir()
    logs.mkdir()

    def create_db(path: Path, marker: str) -> None:
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE marker (value TEXT)")
            conn.execute("INSERT INTO marker VALUES (?)", (marker,))
            conn.commit()

    create_db(legacy / "meshcore_bot.db", "current-legacy")
    create_db(state / "meshcore_bot.db", "stale-target")
    config_path = tmp_path / "config.ini"
    config_path.write_text("[Bot]\ndb_path = meshcore_bot.db\n", encoding="utf-8")

    migrate_service_layout(config_path, legacy, state, logs)

    with sqlite3.connect(state / "meshcore_bot.db") as conn:
        assert conn.execute("SELECT value FROM marker").fetchone() == ("current-legacy",)
    backups = list(state.glob("meshcore_bot.pre-layout-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone() == ("stale-target",)


def test_split_bot_and_viewer_databases_migrate_without_filename_collision(tmp_path):
    legacy = tmp_path / "legacy"
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    (legacy / "bot").mkdir(parents=True)
    (legacy / "viewer").mkdir(parents=True)

    for path, marker in (
        (legacy / "bot" / "shared.db", "bot"),
        (legacy / "viewer" / "shared.db", "viewer"),
    ):
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE marker (value TEXT)")
            conn.execute("INSERT INTO marker VALUES (?)", (marker,))
            conn.commit()

    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[Bot]\n"
        "db_path = bot/shared.db\n"
        "[Web_Viewer]\n"
        "db_path = viewer/shared.db\n",
        encoding="utf-8",
    )

    migrate_service_layout(config_path, legacy, state, logs)

    parsed = configparser.ConfigParser()
    parsed.read(config_path)
    bot_target = Path(parsed.get("Bot", "db_path"))
    viewer_target = Path(parsed.get("Web_Viewer", "db_path"))
    assert bot_target != viewer_target
    with sqlite3.connect(bot_target) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone() == ("bot",)
    with sqlite3.connect(viewer_target) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone() == ("viewer",)


def test_legacy_local_plugins_replace_and_preserve_stale_target_tree(tmp_path):
    legacy = tmp_path / "legacy"
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    (legacy / "local" / "commands").mkdir(parents=True)
    (legacy / "local" / "commands" / "current.py").write_text("CURRENT = 1\n")
    (state / "local" / "commands").mkdir(parents=True)
    (state / "local" / "commands" / "stale.py").write_text("STALE = 1\n")
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[Bot]\n"
        f"db_path = {tmp_path / 'absolute.db'}\n"
        "local_dir_path = local\n",
        encoding="utf-8",
    )

    migrate_service_layout(config_path, legacy, state, logs)

    assert (state / "local" / "commands" / "current.py").is_file()
    assert not (state / "local" / "commands" / "stale.py").exists()
    backups = list(state.glob("local.pre-layout-*"))
    assert len(backups) == 1
    assert (backups[0] / "commands" / "stale.py").is_file()
