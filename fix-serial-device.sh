#!/bin/bash
# Quick script to find and update the serial device in config.ini

CONFIG_FILE="data/config/config.ini"

# Find the actual device the symlink points to
SYMLINK="/dev/serial/by-id/usb-RAKwireless_WisCore_RAK4631_Board_B2C39A2D9735430C-if00"

if [ -L "$SYMLINK" ]; then
    ACTUAL_DEVICE=$(readlink -f "$SYMLINK" 2>/dev/null)
    if [ -n "$ACTUAL_DEVICE" ] && [ -e "$ACTUAL_DEVICE" ]; then
        echo "Found device: $SYMLINK -> $ACTUAL_DEVICE"
        
        # Update config.ini
        if [[ "$(uname -s)" == "Darwin" ]]; then
            sed -i '' "s|^serial_port[[:space:]]*=.*|serial_port = $ACTUAL_DEVICE|" "$CONFIG_FILE"
        else
            sed -i "s|^serial_port[[:space:]]*=.*|serial_port = $ACTUAL_DEVICE|" "$CONFIG_FILE"
        fi
        
        echo "✓ Updated config.ini to use: $ACTUAL_DEVICE"
        echo ""
        echo "Restart the container:"
        echo "  docker compose restart"
    else
        echo "Error: Could not resolve symlink"
    fi
else
    echo "Symlink not found: $SYMLINK"
    echo "Available serial devices:"
    ls -la /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || echo "  None found"
fi
