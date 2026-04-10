#!/bin/bash
# MeshCore Bot - Uninstall Script
# Removes all traces of the bot from this machine:
#   - Virtual environment (.venv/)
#   - Database files (*.db, *.db-wal, *.db-shm)
#   - config.ini
#   - Log files (*.log)
#   - Runtime artifacts (bot_start_time.txt)
#   - systemd service (if installed)
#   - The bot directory itself (optional)
#
# Usage:
#   ./uninstall.sh           # Remove bot data, keep source files
#   ./uninstall.sh --full    # Remove everything including the bot directory

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

FULL=false
BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="meshcore-bot"

for arg in "$@"; do
  case $arg in
    --full|-f) FULL=true ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
  esac
done

echo -e "${CYAN}=== MeshCore Bot Uninstaller ===${NC}"
echo -e "${YELLOW}Bot directory: ${BOT_DIR}${NC}"
echo ""

if [[ "$FULL" == true ]]; then
  echo -e "${RED}WARNING: --full will delete the entire bot directory (${BOT_DIR}).${NC}"
fi

read -rp "Continue? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi

# Stop and remove systemd service if present
if command -v systemctl &>/dev/null && systemctl list-unit-files "${SERVICE_NAME}.service" &>/dev/null 2>&1; then
  echo -e "${CYAN}Stopping systemd service...${NC}"
  sudo systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  sudo systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  sudo rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  sudo systemctl daemon-reload
  echo -e "${GREEN}Service removed.${NC}"
fi

# Remove virtual environment
if [[ -d "${BOT_DIR}/.venv" ]]; then
  echo -e "${CYAN}Removing virtual environment...${NC}"
  rm -rf "${BOT_DIR}/.venv"
  echo -e "${GREEN}.venv removed.${NC}"
fi

# Remove config.ini
if [[ -f "${BOT_DIR}/config.ini" ]]; then
  echo -e "${CYAN}Removing config.ini...${NC}"
  rm -f "${BOT_DIR}/config.ini"
  echo -e "${GREEN}config.ini removed.${NC}"
fi

# Remove databases
find "${BOT_DIR}" -maxdepth 2 -name "*.db" -o -name "*.db-wal" -o -name "*.db-shm" | while read -r f; do
  echo -e "${CYAN}Removing ${f}...${NC}"
  rm -f "$f"
done

# Remove log files
find "${BOT_DIR}" -maxdepth 2 -name "*.log" -o -name "*.log.*" | while read -r f; do
  echo -e "${CYAN}Removing ${f}...${NC}"
  rm -f "$f"
done

# Remove runtime artifacts
rm -f "${BOT_DIR}/bot_start_time.txt"

# Remove __pycache__
find "${BOT_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo ""
if [[ "$FULL" == true ]]; then
  echo -e "${CYAN}Removing bot directory: ${BOT_DIR}${NC}"
  rm -rf "${BOT_DIR}"
  echo -e "${GREEN}Bot directory removed.${NC}"
else
  echo -e "${GREEN}=== Uninstall complete ===${NC}"
  echo "Source files kept. To also remove the bot directory, run:"
  echo "  ./uninstall.sh --full"
fi
