"""Golden-file helpers for characterization tests.

A characterization test records what the code does today, so a refactor can
prove it changed nothing. Run with ``UPDATE_GOLDEN=1`` to (re)write a golden
file; otherwise the test compares against the committed file and fails on any
difference. Only regenerate on purpose, in a commit that says why the
behavior changed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path(__file__).parent / "golden"


def assert_golden(name: str, data: Any) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    rendered = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False, default=repr) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(rendered, encoding="utf-8")
        return
    if not path.exists():
        # Never write one implicitly: a deleted or misnamed golden would silently
        # re-record whatever the code does now, and the test would pass.
        raise AssertionError(f"golden file {path} missing; generate it with UPDATE_GOLDEN=1")
    expected = path.read_text(encoding="utf-8")
    if rendered != expected:
        exp = json.loads(expected)
        got = json.loads(rendered)
        diffs = _diff(exp, got)
        raise AssertionError(
            f"{name}: behavior differs from golden file in {len(diffs)} place(s):\n"
            + "\n".join(diffs[:40])
        )


def _diff(exp: Any, got: Any, path: str = "") -> list[str]:
    if type(exp) is not type(got):
        return [f"{path or '<root>'}: {exp!r} -> {got!r}"]
    if isinstance(exp, dict):
        out: list[str] = []
        for key in sorted(set(exp) | set(got)):
            if key not in exp:
                out.append(f"{path}/{key}: <missing> -> {got[key]!r}")
            elif key not in got:
                out.append(f"{path}/{key}: {exp[key]!r} -> <missing>")
            else:
                out.extend(_diff(exp[key], got[key], f"{path}/{key}"))
        return out
    if isinstance(exp, list):
        if len(exp) != len(got):
            return [f"{path}: length {len(exp)} -> {len(got)}"]
        out = []
        for i, (a, b) in enumerate(zip(exp, got, strict=True)):
            out.extend(_diff(a, b, f"{path}[{i}]"))
        return out
    return [] if exp == got else [f"{path}: {exp!r} -> {got!r}"]
