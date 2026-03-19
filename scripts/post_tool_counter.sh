#!/usr/bin/env bash
# Post-tool counter: increments tool call count and runs checkpoint every 100 calls
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COUNTER_FILE="/tmp/mc_tool_count"

# Increment counter
COUNT=$(cat "$COUNTER_FILE" 2>/dev/null || echo "0")
COUNT=$((COUNT + 1))
echo "$COUNT" > "$COUNTER_FILE"

# Every 100 tool calls, run checkpoint
if [ $((COUNT % 100)) -eq 0 ]; then
    echo "post_tool_counter: $COUNT tool calls — running context checkpoint"
    bash "$REPO_ROOT/scripts/context_checkpoint.sh"
fi
