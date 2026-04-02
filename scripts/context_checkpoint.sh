#!/usr/bin/env bash
# Context checkpoint: updates SESSION_RESUME.md, TODO.md, BUGS.md on session stop / cron
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TASK=$(cat .claude/current_task.txt 2>/dev/null || echo "unknown")
STEP=$(cat .claude/current_step.txt 2>/dev/null || echo "unknown")
TS=$(date '+%Y-%m-%d %H:%M')

# 1. Update SESSION_RESUME.md — overwrite/append the "Current State" section
python3 - "$TASK" "$STEP" "$TS" <<'PYEOF'
import re, pathlib, sys

task, step, ts = sys.argv[1], sys.argv[2], sys.argv[3]
path = pathlib.Path("SESSION_RESUME.md")
content = path.read_text() if path.exists() else ""

new_section = (
    "## Current State\n\n"
    "- **Active task:** " + task + "\n"
    "- **Last step:** " + step + "\n"
    "- **Checkpoint:** " + ts + "\n"
    "- **Resume instruction:** Start at " + task + ", step " + step +
    " — see task list below for full context\n\n"
)

if "## Current State" in content:
    content = re.sub(
        r"## Current State\n.*?(?=^## |\Z)",
        new_section,
        content,
        flags=re.DOTALL | re.MULTILINE,
    )
else:
    content = content.rstrip("\n") + "\n\n" + new_section

path.write_text(content)
print("SESSION_RESUME.md updated")
PYEOF

# 2. Update TODO.md — mark active task as paused if not already done/paused
if [ "$TASK" != "unknown" ]; then
    python3 - "$TASK" "$TS" <<'PYEOF'
import pathlib, re, sys

task, ts = sys.argv[1], sys.argv[2]
path = pathlib.Path("TODO.md")
if not path.exists():
    print("TODO.md not found, skipping")
    sys.exit(0)

content = path.read_text()
pattern = re.compile(r"(\[ \].*?" + re.escape(task) + r".*?)(?=\n)", re.IGNORECASE)

def mark_paused(m):
    line = m.group(1)
    if "\u23f8" in line or "[x]" in line:
        return line
    return line + "  \u23f8 paused " + ts + " \u2014 see SESSION_RESUME.md"

new_content = pattern.sub(mark_paused, content)
if new_content != content:
    path.write_text(new_content)
    print("TODO.md: marked " + task + " as paused")
else:
    print("TODO.md: no open task line found for " + task)
PYEOF
fi

# 3. Flush pending bugs from .claude/pending_bugs.txt into BUGS.md
PENDING=".claude/pending_bugs.txt"
if [ -s "$PENDING" ]; then
    if printf '\n' >> BUGS.md 2>/dev/null && cat "$PENDING" >> BUGS.md 2>/dev/null; then
        : > "$PENDING"
        echo "BUGS.md: flushed pending bugs"
    else
        echo "BUGS.md: warning — could not write, pending bugs retained in ${PENDING}" >&2
    fi
fi

# 4. Append checkpoint log entry
mkdir -p .claude
LOG_LINE="$TS  task=$TASK  step=$STEP  trigger=checkpoint"
echo "$LOG_LINE" >> .claude/checkpoint_log.txt
echo "checkpoint_log.txt: $LOG_LINE"

# 5. Update last_checkpoint timestamp
echo "$TS" > .claude/last_checkpoint

echo "Checkpoint complete at $TS"
