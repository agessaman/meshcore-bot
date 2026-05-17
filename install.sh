#!/bin/bash
# MeshCore Bot - Quick Install Script
# Sets up a Python virtual environment and installs all dependencies.
# For full service/daemon installation, use install-service.sh instead.
#
# Usage:
#   ./install.sh              # Install / refresh dependencies
#   ./install.sh --upgrade    # Upgrade all packages to latest
#
# After running this script:
#   1. Copy config.ini.example to config.ini and edit it
#   2. Set connection_type = tcp (or serial/ble) and configure [Channels]
#   3. Run the bot: .venv/bin/python meshcore_bot.py

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

VENV_DIR=".venv"
UPGRADE=false

for arg in "$@"; do
  case $arg in
    --upgrade|-u) UPGRADE=true ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
  esac
done

echo -e "${CYAN}=== MeshCore Bot Installer ===${NC}"

# Check Python 3
if ! command -v python3 &>/dev/null; then
  echo -e "${RED}Error: python3 not found. Install Python 3.8+ and retry.${NC}"
  exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MIN="3.8"
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)"; then
  echo -e "${GREEN}Python ${PY_VER} detected${NC}"
else
  echo -e "${RED}Error: Python 3.8+ required (found ${PY_VER})${NC}"
  exit 1
fi

# Create virtualenv if missing
if [[ ! -d "$VENV_DIR" ]]; then
  echo -e "${CYAN}Creating virtual environment...${NC}"
  python3 -m venv "$VENV_DIR"
  echo -e "${GREEN}Virtual environment created at ${VENV_DIR}/${NC}"
fi

# Upgrade pip quietly
"$VENV_DIR/bin/pip" install --quiet --upgrade pip

# Install / upgrade dependencies
if [[ "$UPGRADE" == true ]]; then
  echo -e "${CYAN}Upgrading all dependencies...${NC}"
  "$VENV_DIR/bin/pip" install --upgrade -r requirements.txt
else
  echo -e "${CYAN}Installing dependencies...${NC}"
  "$VENV_DIR/bin/pip" install -r requirements.txt
fi

echo -e "${GREEN}Dependencies installed.${NC}"

# Config setup
if [[ ! -f "config.ini" ]]; then
  echo ""
  echo -e "${YELLOW}No config.ini found. Copying config.ini.example -> config.ini${NC}"
  cp config.ini.example config.ini
  echo -e "${YELLOW}Edit config.ini before starting the bot:${NC}"
  echo "  - Set connection_type (tcp/serial/ble) and hostname/port or serial_port"
  echo "  - Set monitor_channels under [Channels]"
  echo "  - Set bot_latitude / bot_longitude for your location"
  echo "  - Optionally set prefix_bytes = 2 or 3 for multi-byte hash display"
else
  echo -e "${GREEN}config.ini already exists — skipping copy.${NC}"
fi

echo ""
echo -e "${GREEN}=== Install complete ===${NC}"
echo ""
echo "Start the bot:"
echo "  .venv/bin/python meshcore_bot.py"
echo ""
echo "For service/daemon installation (auto-start on boot):"
echo "  sudo ./install-service.sh"
