#!/usr/bin/env python3
"""
update_todos.py — Scans source files for TODO/FIXME/HACK markers and rewrites
the "Inline TODOs" section of TODO.md.  Also updates the "Last updated:" date
at the top of the file.

Usage:
    python scripts/update_todos.py          # from project root
    python scripts/update_todos.py --check  # exit 1 if TODO.md would change (CI use)

Completed item date format in TODO.md:
    - [x] (YYYY-MM-DD) description of completed item

The script manages two things in TODO.md:
  1. The "**Last updated:**" line near the top — set to today's date.
  2. The "## Inline TODOs (auto-generated)" section at the bottom — replaced
     wholesale with a fresh scan of # TODO / # FIXME / # HACK markers.
Everything else in TODO.md is left exactly as-is.
"""

import argparse
import datetime
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
TODO_FILE = PROJECT_ROOT / "TODO.md"
SCAN_DIRS = ["modules", "tests"]
SCAN_EXTENSIONS = {".py"}
MARKERS = re.compile(r"#\s*(TODO|FIXME|HACK)\b[:\s]*(.*)", re.IGNORECASE)

SECTION_START = "## Inline TODOs (auto-generated)"
SENTINEL_LINE = "> _Last scanned:"


def scan_todos():
    """Walk source directories and collect all TODO/FIXME/HACK comments."""
    results = []
    for scan_dir in SCAN_DIRS:
        root = PROJECT_ROOT / scan_dir
        if not root.exists():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fname in sorted(files):
                if Path(fname).suffix not in SCAN_EXTENSIONS:
                    continue
                fpath = Path(dirpath) / fname
                rel = fpath.relative_to(PROJECT_ROOT)
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as fh:
                        for lineno, line in enumerate(fh, 1):
                            m = MARKERS.search(line)
                            if m:
                                marker = m.group(1).upper()
                                text = m.group(2).strip().rstrip(".")
                                results.append((str(rel), lineno, marker, text))
                except OSError:
                    pass
    return results


def build_section(todos, today: str) -> str:
    """Render the Inline TODOs section as a markdown string."""
    lines = [f"{SECTION_START}\n"]
    if not todos:
        lines.append(
            f"> _Last scanned: {today}. No `# TODO`, `# FIXME`, or `# HACK` markers"
        )
        lines.append(
            "> found in `modules/` or `tests/`. Run `python scripts/update_todos.py` to refresh._\n"
        )
        return "\n".join(lines)

    lines.append(
        f"> _Last scanned: {today}. {len(todos)} item(s) found._\n"
    )

    # Group by marker type
    by_marker: dict[str, list] = {}
    for rel, lineno, marker, text in todos:
        by_marker.setdefault(marker, []).append((rel, lineno, text))

    emoji = {"TODO": "📋", "FIXME": "🔧", "HACK": "⚠️"}
    for marker in ("FIXME", "TODO", "HACK"):
        if marker not in by_marker:
            continue
        lines.append(f"### {emoji.get(marker, '')} {marker}\n")
        for rel, lineno, text in sorted(by_marker[marker]):
            label = text if text else f"(no description — see file)"
            lines.append(f"- [ ] **`{rel}:{lineno}`** — {label}")
        lines.append("")

    return "\n".join(lines)


def rewrite_todo_md(new_section: str, today: str, check_only: bool = False) -> bool:
    """
    Replace the Inline TODOs section and update the Last updated date in TODO.md.

    Returns True if the file was (or would be) changed.
    """
    if not TODO_FILE.exists():
        print(f"ERROR: {TODO_FILE} not found.", file=sys.stderr)
        sys.exit(1)

    content = TODO_FILE.read_text(encoding="utf-8")

    # Update "**Last updated:**" line
    content = re.sub(
        r"(\*\*Last updated:\*\*\s*)[\d-]+",
        rf"\g<1>{today}",
        content,
    )

    # Find the section heading and replace everything from it to EOF
    idx = content.find(f"\n{SECTION_START}")
    if idx == -1:
        # Section missing — append it
        new_content = content.rstrip() + "\n\n---\n\n" + new_section + "\n"
    else:
        new_content = content[: idx + 1] + new_section + "\n"

    changed = new_content != TODO_FILE.read_text(encoding="utf-8")

    if check_only:
        if changed:
            print("TODO.md is out of date. Run `python scripts/update_todos.py` to refresh.")
        return changed

    if changed:
        TODO_FILE.write_text(new_content, encoding="utf-8")
        print(f"TODO.md updated ({len(new_section.splitlines())} lines in Inline TODOs section).")
    else:
        print("TODO.md is already up to date.")

    return changed


def main():
    parser = argparse.ArgumentParser(description="Update Inline TODOs section in TODO.md")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with code 1 if TODO.md would change (for CI gates)",
    )
    args = parser.parse_args()

    today = datetime.date.today().isoformat()
    todos = scan_todos()
    section = build_section(todos, today)
    changed = rewrite_todo_md(section, today, check_only=args.check)

    if args.check and changed:
        sys.exit(1)


if __name__ == "__main__":
    main()
