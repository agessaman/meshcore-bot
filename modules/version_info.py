#!/usr/bin/env python3
"""
Version resolution utilities for MeshCore Bot.

Centralizes runtime version lookup so bot command output, web viewer, and
services all report consistent version information.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


def _normalize_tag(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    return value if value.startswith("v") else f"v{value}"


def _safe_git_run(repo_root: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root)] + args,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        out = (result.stdout or "").strip()
        return out or None
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def _read_version_file(repo_root: Path) -> str | None:
    version_file = repo_root / ".version_info"
    if not version_file.is_file():
        return None
    try:
        with open(version_file, encoding="utf-8") as fh:
            data = json.load(fh)
        version = data.get("installer_version") or data.get("tag")
        return _normalize_tag(version)
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _read_pyproject_version(repo_root: Path) -> str | None:
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.is_file():
        return None
    try:
        text = pyproject_path.read_text(encoding="utf-8")
    except OSError:
        return None

    in_project_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_project_section = line == "[project]"
            continue
        if not in_project_section:
            continue
        match = re.match(r'version\s*=\s*"([^"]+)"', line)
        if match:
            return _normalize_tag(match.group(1))
    return None


def resolve_runtime_version(repo_root: Path | str) -> dict[str, str | None]:
    """Resolve version metadata and a single runtime display value.

    Returns a dict with:
      - baked: release-like version from env/.version_info/pyproject (v-prefixed)
      - tag: same as baked for template compatibility
      - branch, commit, date: git metadata when available
      - display: final runtime version string
    """
    root = Path(repo_root).resolve()

    env_version = _normalize_tag(os.environ.get("MESHCORE_BOT_VERSION", "").strip())
    file_version = _read_version_file(root)
    pyproject_version = _read_pyproject_version(root)
    baked = env_version or file_version or pyproject_version

    branch = _safe_git_run(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    commit = _safe_git_run(root, ["rev-parse", "--short", "HEAD"])
    date_raw = _safe_git_run(root, ["show", "-s", "--format=%ci", "HEAD"])
    date = None
    if date_raw:
        # %ci format is "YYYY-MM-DD HH:MM:SS +TZ"; keep date only.
        date = date_raw.split()[0] if " " in date_raw else date_raw

    display: str | None
    if branch and branch != "main" and commit:
        display = f"{branch}-{commit}"
    elif branch == "main" and baked:
        display = baked
    else:
        # Fallbacks for non-git/runtime-constrained environments.
        display = baked or (f"{branch}-{commit}" if branch and commit else None) or "unknown"

    return {
        "baked": baked,
        "tag": baked,
        "branch": branch,
        "commit": commit,
        "date": date,
        "display": display,
    }

