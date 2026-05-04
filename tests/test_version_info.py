"""Tests for modules.version_info."""

import json

from modules.version_info import resolve_runtime_version


def test_main_branch_uses_baked_env_version(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    monkeypatch.setenv("MESHCORE_BOT_VERSION", "0.9")

    def _fake_git_run(_root, args):
        mapping = {
            ("rev-parse", "--abbrev-ref", "HEAD"): "main",
            ("rev-parse", "--short", "HEAD"): "abc1234",
            ("show", "-s", "--format=%ci", "HEAD"): "2026-04-05 10:00:00 +0000",
        }
        return mapping.get(tuple(args))

    monkeypatch.setattr("modules.version_info._safe_git_run", _fake_git_run)
    info = resolve_runtime_version(tmp_path)

    assert info["baked"] == "v0.9"
    assert info["display"] == "v0.9"
    assert info["branch"] == "main"
    assert info["commit"] == "abc1234"
    assert info["date"] == "2026-04-05"


def test_non_main_branch_uses_branch_and_commit(tmp_path, monkeypatch):
    (tmp_path / ".version_info").write_text(
        json.dumps({"installer_version": "0.9.0"}),
        encoding="utf-8",
    )

    def _fake_git_run(_root, args):
        mapping = {
            ("rev-parse", "--abbrev-ref", "HEAD"): "dev",
            ("rev-parse", "--short", "HEAD"): "fedcba9",
            ("show", "-s", "--format=%ci", "HEAD"): "2026-04-05 11:00:00 +0000",
        }
        return mapping.get(tuple(args))

    monkeypatch.setattr("modules.version_info._safe_git_run", _fake_git_run)
    info = resolve_runtime_version(tmp_path)

    assert info["baked"] == "v0.9.0"
    assert info["display"] == "dev-fedcba9"
    assert info["branch"] == "dev"


def test_baked_precedence_file_over_pyproject(tmp_path, monkeypatch):
    (tmp_path / ".version_info").write_text(
        json.dumps({"installer_version": "0.8.1"}),
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    monkeypatch.delenv("MESHCORE_BOT_VERSION", raising=False)
    monkeypatch.setattr("modules.version_info._safe_git_run", lambda *_args, **_kwargs: None)

    info = resolve_runtime_version(tmp_path)
    assert info["baked"] == "v0.8.1"
    assert info["display"] == "v0.8.1"


def test_fallback_to_unknown_without_baked_or_git(tmp_path, monkeypatch):
    monkeypatch.delenv("MESHCORE_BOT_VERSION", raising=False)
    monkeypatch.setattr("modules.version_info._safe_git_run", lambda *_args, **_kwargs: None)

    info = resolve_runtime_version(tmp_path)
    assert info["baked"] is None
    assert info["display"] == "unknown"

