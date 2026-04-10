#!/usr/bin/env python3
"""Check for log injection regressions.

Scans modules/ for logger calls that directly interpolate raw external
field values (node names, message content, public-key prefixes) from
radio-origin data without going through sanitize_name() or
sanitize_input() first.

This catches the SEC-04 regression pattern: adding new code that puts
unsanitized .get('name') / .get('adv_name') / msg.content directly
inside a logger f-string, which allows malicious radio nodes to inject
newlines or ANSI escape codes into log files.

Usage:
  python scripts/check_log_injection.py               # check (fails on new violations)
  python scripts/check_log_injection.py --update      # regenerate baseline file

Exit code 0 = clean or only known baseline violations, 1 = new violations found.
"""

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Patterns that indicate raw external field access inside a log call.
# ---------------------------------------------------------------------------
_RISKY = [
    re.compile(r"\.get\(['\"](?:name|adv_name|pubkey_prefix|content|text)['\"]"),
    re.compile(r"\bmsg\.content\b"),
    re.compile(r"\bpayload\[.?content.?\]"),
]

_SAFE_WRAPPERS = ("sanitize_name(", "sanitize_input(")
_LOGGER_RE = re.compile(r"self\.logger\.\w+\(|self\.log\.\w+\(|\blogger\.\w+\(")

BASELINE_FILE = Path("scripts/.log-injection-baseline.txt")

# ---------------------------------------------------------------------------


def check_file(path: Path) -> list[str]:
    violations: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return violations

    for lineno, line in enumerate(lines, 1):
        if not _LOGGER_RE.search(line):
            continue
        if any(w in line for w in _SAFE_WRAPPERS):
            continue
        for pat in _RISKY:
            if pat.search(line):
                # Fingerprint: "path:lineno:stripped_line"
                violations.append(f"{path}:{lineno}:{line.strip()}")
                break
    return violations


def load_baseline() -> set[str]:
    if not BASELINE_FILE.exists():
        return set()
    lines = BASELINE_FILE.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def save_baseline(violations: list[str]) -> None:
    BASELINE_FILE.write_text(
        "# Known log-injection technical debt — do not add new entries without a\n"
        "# corresponding fix ticket.  Run scripts/check_log_injection.py --update\n"
        "# to regenerate after fixing existing violations.\n"
        + "\n".join(sorted(violations))
        + "\n",
        encoding="utf-8",
    )
    print(f"[log-injection] Baseline written to {BASELINE_FILE} ({len(violations)} entries).")


def collect_all(root: Path) -> list[str]:
    all_violations: list[str] = []
    for py_file in sorted(root.rglob("*.py")):
        all_violations.extend(check_file(py_file))
    return all_violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Regenerate the baseline file from current violations.",
    )
    args = parser.parse_args()

    root = Path("modules")
    if not root.is_dir():
        print("check_log_injection: must be run from the project root", file=sys.stderr)
        return 2

    all_violations = collect_all(root)

    if args.update:
        save_baseline(all_violations)
        return 0

    baseline = load_baseline()
    new_violations = [v for v in all_violations if v not in baseline]
    fixed = [v for v in baseline if v not in set(all_violations)]

    if fixed:
        print(f"[log-injection] {len(fixed)} baseline violation(s) resolved — run --update to shrink baseline.")

    if new_violations:
        print(f"[log-injection] {len(new_violations)} NEW log injection violation(s) found:")
        for v in new_violations:
            print(f"  {v}")
        print(
            "\nFix: wrap the field in sanitize_name() or sanitize_input() before logging.\n"
            "Example:\n"
            "  # BAD\n"
            "  self.logger.info(f'Contact: {data.get(\"name\")}')\n"
            "  # GOOD\n"
            "  name = sanitize_name(data.get('name', 'Unknown'))\n"
            "  self.logger.info(f'Contact: {name}')"
        )
        return 1

    total = len(all_violations)
    print(
        f"[log-injection] Clean — {total} known baseline violation(s), no new ones. "
        f"(Scanned {len(list(root.rglob('*.py')))} files.)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
