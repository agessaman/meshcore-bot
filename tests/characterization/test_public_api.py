"""Characterization: every public name under modules/ that local plugins could use.

Local plugins and services (local/) may call any public module function,
class or method, so a refactor must not remove one. New names are fine; the
golden file only fails on removals (regenerate with UPDATE_GOLDEN=1 when a
removal is intentional).

Public names a module imports from another project module count too: code
(and test patches) can reach them as ``modules.<module>.<name>``. Those are
recorded separately in public_imports.json.
"""

import ast
import json
from pathlib import Path

from tests.characterization.golden_util import GOLDEN_DIR

MODULES = Path(__file__).resolve().parents[2] / "modules"
GOLDEN = GOLDEN_DIR / "public_api.json"
IMPORTS_GOLDEN = GOLDEN_DIR / "public_imports.json"


def _public_names() -> dict[str, list[str]]:
    names: dict[str, list[str]] = {}
    for path in sorted(MODULES.rglob("*.py")):
        rel = path.relative_to(MODULES.parent).as_posix()
        found = set()
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                found.add(node.name)
            elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                found.add(node.name)
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and not member.name.startswith("_"):
                        found.add(f"{node.name}.{member.name}")
        if found:
            names[rel] = sorted(found)
    return names


def _public_imports(root: Path = MODULES) -> dict[str, list[str]]:
    """Public names each module binds by importing them from a project module."""
    names: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent).as_posix()
        found = set()
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level == 0 and not (node.module or "").startswith("modules"):
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound != "*" and not bound.startswith("_"):
                    found.add(bound)
        if found:
            names[rel] = sorted(found)
    return names


def _resolves(module: str, name: str) -> bool:
    import importlib

    obj = importlib.import_module(module[:-3].replace("/", "."))
    for part in name.split("."):
        if not hasattr(obj, part):
            return False
        obj = getattr(obj, part)
    return True


def test_no_public_name_was_removed():
    import os

    current = _public_names()
    if os.environ.get("UPDATE_GOLDEN") == "1" or not GOLDEN.exists():
        GOLDEN.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return
    recorded = json.loads(GOLDEN.read_text(encoding="utf-8"))
    missing = []
    for module, names in recorded.items():
        defined = set(current.get(module, []))
        for name in names:
            # A method moved to a base class, or a function re-exported, still counts.
            if name not in defined and not _resolves(module, name):
                missing.append(f"{module}: {name}")
    assert missing == [], "public names removed:\n  " + "\n  ".join(missing)


def test_no_imported_public_name_was_dropped():
    import os

    if os.environ.get("UPDATE_GOLDEN") == "1" or not IMPORTS_GOLDEN.exists():
        IMPORTS_GOLDEN.write_text(json.dumps(_public_imports(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return
    recorded = json.loads(IMPORTS_GOLDEN.read_text(encoding="utf-8"))
    missing = [
        f"{module}: {name}"
        for module, names in recorded.items()
        for name in names
        if not _resolves(module, name)
    ]
    assert missing == [], "imported public names no longer reachable:\n  " + "\n  ".join(missing)
