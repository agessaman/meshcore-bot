#!/usr/bin/env bash
# reload_config.sh — Reload bot configuration via the admin API.
#
# Calls POST /api/admin/reload on the bot's built-in admin HTTP server.
# Equivalent to the !reload DM admin command: triggers bot.reload_config()
# in-process and returns the result immediately as JSON.
#
# Prerequisites:
#   [Admin] section in config.ini with enabled = true, port = 5001, token = <secret>
#
# Usage:
#   ./scripts/reload_config.sh                         # uses config.ini in cwd
#   ./scripts/reload_config.sh /path/to/config.ini     # explicit config path
#
# Environment overrides:
#   ADMIN_PORT   — override port  (default: read from config, fallback 5001)
#   ADMIN_TOKEN  — override token (default: read from config)

set -euo pipefail

CONFIG="${1:-config.ini}"

# --- read port and token from config.ini unless overridden by env -----
_read_config_value() {
    local key="$1" default="$2"
    if [ -f "$CONFIG" ]; then
        grep -A20 '^\[Admin\]' "$CONFIG" \
            | grep -m1 "^${key}[[:space:]]*=" \
            | sed 's/^[^=]*=[[:space:]]*//' \
            | tr -d '[:space:]' \
            || true
    fi
    # fall through to default if nothing printed
}

ADMIN_PORT="${ADMIN_PORT:-$(_read_config_value port 5001)}"
ADMIN_PORT="${ADMIN_PORT:-5001}"

ADMIN_TOKEN="${ADMIN_TOKEN:-$(_read_config_value token '')}"

if [ -z "$ADMIN_TOKEN" ]; then
    echo "ERROR: no admin token found in $CONFIG [Admin] section and ADMIN_TOKEN env not set." >&2
    exit 1
fi

URL="http://127.0.0.1:${ADMIN_PORT}/api/admin/reload"

echo "Calling ${URL} ..."
RESPONSE=$(curl -sf -X POST "$URL" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    --max-time 10 2>&1) || {
    echo "ERROR: could not reach admin server at ${URL}" >&2
    echo "       Is the bot running with [Admin] enabled = true?" >&2
    exit 1
}

echo "$RESPONSE"

# Surface the 'success' flag as the exit code (0 = success, 1 = config rejected)
if echo "$RESPONSE" | grep -q '"success": *false'; then
    exit 1
fi
