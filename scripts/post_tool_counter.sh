#!/usr/bin/env bash
# Post-tool counter: increments tool call count and runs checkpoint every 100 calls
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COUNTER_FILE="/tmp/mc_tool_count"

# Increment counter (flock makes read-modify-write atomic)
LOCK_FILE="${COUNTER_FILE}.lock"
COUNT=$(
    (
        flock -x 9
        C=$(cat "$COUNTER_FILE" 2>/dev/null || echo "0")
        C=$((C + 1))
        echo "$C" > "$COUNTER_FILE"
        echo "$C"
    ) 9>"$LOCK_FILE"
)

# Every 100 tool calls, run checkpoint
if [ $((COUNT % 100)) -eq 0 ]; then
    echo "post_tool_counter: $COUNT tool calls — running context checkpoint"
    bash "$REPO_ROOT/scripts/context_checkpoint.sh"
fi
