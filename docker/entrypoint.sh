#!/bin/bash
# ============================================
# MeshCore Bot Docker Entrypoint
# ============================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# ============================================
# Configuration Setup
# ============================================

# Check if config.ini exists, if not copy from example
if [ ! -f /app/config.ini ]; then
    if [ -f /app/config.ini.example ]; then
        log_warn "No config.ini found, copying from config.ini.example"
        cp /app/config.ini.example /app/config.ini
    else
        log_error "No config.ini or config.ini.example found!"
        exit 1
    fi
fi

# ============================================
# Environment Variable Overrides
# Apply environment variables to config.ini
# ============================================

apply_config_override() {
    local section=$1
    local key=$2
    local value=$3
    local config_file="/app/config.ini"

    if [ -n "$value" ]; then
        log_info "Setting [${section}] ${key} = ${value}"
        # Use Python for reliable INI file editing
        python3 -c "
import configparser
config = configparser.ConfigParser()
config.read('${config_file}')
if not config.has_section('${section}'):
    config.add_section('${section}')
config.set('${section}', '${key}', '${value}')
with open('${config_file}', 'w') as f:
    config.write(f)
"
    fi
}

# Apply connection settings from environment
apply_config_override "Connection" "connection_type" "${MESHCORE_CONNECTION_TYPE}"
apply_config_override "Connection" "serial_port" "${MESHCORE_SERIAL_PORT}"
apply_config_override "Connection" "hostname" "${MESHCORE_TCP_HOST}"
apply_config_override "Connection" "tcp_port" "${MESHCORE_TCP_PORT}"
apply_config_override "Connection" "ble_device_name" "${MESHCORE_BLE_DEVICE}"
apply_config_override "Connection" "timeout" "${MESHCORE_TIMEOUT}"

# Apply bot settings from environment
apply_config_override "Bot" "bot_name" "${MESHCORE_BOT_NAME}"
apply_config_override "Bot" "timezone" "${TZ}"
apply_config_override "Bot" "bot_latitude" "${MESHCORE_LATITUDE}"
apply_config_override "Bot" "bot_longitude" "${MESHCORE_LONGITUDE}"

# Apply web viewer settings from environment
apply_config_override "Web_Viewer" "enabled" "${MESHCORE_WEB_ENABLED}"
apply_config_override "Web_Viewer" "host" "${MESHCORE_WEB_HOST}"
apply_config_override "Web_Viewer" "port" "${MESHCORE_WEB_PORT}"
apply_config_override "Web_Viewer" "auto_start" "${MESHCORE_WEB_AUTOSTART}"

# Apply API keys from environment
apply_config_override "External_Data" "n2yo_api_key" "${N2YO_API_KEY}"
apply_config_override "External_Data" "airnow_api_key" "${AIRNOW_API_KEY}"
apply_config_override "External_Data" "forecast_solar_api_key" "${FORECAST_SOLAR_API_KEY}"

# ============================================
# Serial-specific Setup
# ============================================

if [ "${MESHCORE_CONNECTION_TYPE}" = "serial" ]; then
    SERIAL_DEVICE="${MESHCORE_SERIAL_PORT:-/dev/ttyUSB0}"
    log_info "Serial mode detected, checking device: $SERIAL_DEVICE"

    if [ -c "$SERIAL_DEVICE" ]; then
        log_info "Serial device $SERIAL_DEVICE is available"
        # Ensure proper permissions
        chmod 666 "$SERIAL_DEVICE" 2>/dev/null || log_warn "Could not set permissions on $SERIAL_DEVICE"
    else
        log_warn "Serial device $SERIAL_DEVICE not found, connection may fail"
        log_warn "Make sure the device is mapped with --device flag"
    fi
fi

# ============================================
# BLE-specific Setup
# ============================================

if [ "${MESHCORE_CONNECTION_TYPE}" = "ble" ]; then
    log_info "BLE mode detected, checking Bluetooth availability..."

    # Check if BlueZ is available
    if command -v bluetoothctl &> /dev/null; then
        log_info "BlueZ available"
        # Start D-Bus if not running (needed for BlueZ)
        if [ ! -S /var/run/dbus/system_bus_socket ]; then
            log_warn "D-Bus socket not found, BLE may not work correctly"
        fi
    else
        log_warn "BlueZ not available, BLE connection may fail"
    fi
fi

# ============================================
# Data Directory Setup
# ============================================

# Ensure data directory exists and is writable
if [ ! -d /app/data ]; then
    log_info "Creating data directory"
    mkdir -p /app/data
fi

# Ensure logs directory exists
if [ ! -d /app/logs ]; then
    log_info "Creating logs directory"
    mkdir -p /app/logs
fi

# ============================================
# Start the Bot
# ============================================

log_info "Starting MeshCore Bot..."
log_info "Connection type: ${MESHCORE_CONNECTION_TYPE:-default from config}"

# Execute the main command (default: python meshcore_bot.py)
exec "$@"
