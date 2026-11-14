"""Every tracked runtime file must ship in the wheel.

``pyproject.toml`` uses ``include-package-data = false`` and lists package data
explicitly, and package discovery is the regular (non-namespace) kind, so a new
subpackage without ``__init__.py`` or a new data file outside the globs is
silently left out of an installed bot. This checks the configuration against
``git ls-files`` without building a wheel (the dev venv has no setuptools).
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")  # Python 3.11+; CI's 3.11-3.13 jobs cover it

ROOT = Path(__file__).resolve().parents[2]


def _tracked(prefix: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", prefix], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [line for line in out.splitlines() if line]


def _config():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]


def test_every_python_package_directory_is_a_regular_package():
    missing = sorted(
        {
            str(Path(f).parent)
            for f in _tracked("modules")
            if f.endswith(".py") and not (ROOT / Path(f).parent / "__init__.py").exists()
        }
    )
    assert missing == [], f"directories with modules but no __init__.py (not packaged): {missing}"


def test_every_non_python_runtime_file_matches_a_package_data_glob():
    package_data: dict[str, list[str]] = _config()["package-data"]
    uncovered = []
    for prefix, package in (("modules", "modules"), ("translations", "translations"), ("data/randomlines", "data.randomlines")):
        globs = package_data.get(package, [])
        base = ROOT / package.replace(".", "/")
        for f in _tracked(prefix):
            if f.endswith((".py", ".pyc", ".md")) or "/__pycache__/" in f:
                continue
            rel = (ROOT / f).relative_to(base).as_posix()
            # setuptools globs: '*' does not cross '/', '**' does.
            if not any(_glob_match(rel, g) for g in globs):
                uncovered.append(f)
    assert uncovered == [], f"runtime files not covered by [tool.setuptools.package-data]: {uncovered}"


def _glob_match(rel: str, pattern: str) -> bool:
    if "**" in pattern:
        head, _, tail = pattern.partition("**/")
        return rel.startswith(head) and fnmatch.fnmatch(rel[len(head):].split("/")[-1], tail) if tail else rel.startswith(head)
    rel_parts, pat_parts = rel.split("/"), pattern.split("/")
    return len(rel_parts) == len(pat_parts) and all(fnmatch.fnmatch(r, p) for r, p in zip(rel_parts, pat_parts, strict=True))
