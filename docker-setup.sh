#!/bin/bash
# Setup script for Docker deployment
# Creates necessary directories and copies example config

set -e

echo "Setting up meshcore-bot Docker environment..."

# Create data directories
echo "Creating data directories..."
mkdir -p data/{config,databases,logs,backups}

# Copy example config if config doesn't exist
if [ ! -f "data/config/config.ini" ]; then
    if [ -f "config.ini.example" ]; then
        echo "Copying config.ini.example to data/config/config.ini..."
        cp config.ini.example data/config/config.ini
    else
        echo "⚠️  Warning: config.ini.example not found. Please create data/config/config.ini manually."
        exit 1
    fi
else
    echo "✓ Config file already exists at data/config/config.ini"
fi

# Detect platform
PLATFORM=$(uname -s)
CONFIG_FILE="data/config/config.ini"

# Function to update config.ini using sed (works with both existing and new configs)
update_config() {
    local section=$1
    local key=$2
    local value=$3
    
    # Check if section exists
    if ! grep -q "^\[$section\]" "$CONFIG_FILE"; then
        echo "" >> "$CONFIG_FILE"
        echo "[$section]" >> "$CONFIG_FILE"
    fi
    
    # Check if key exists in section (look for key= with optional spaces)
    if grep -q "^$key[[:space:]]*=" "$CONFIG_FILE"; then
        # Update existing key (use | as delimiter to avoid issues with / in paths)
        if [[ "$PLATFORM" == "Darwin" ]]; then
            sed -i '' "s|^$key[[:space:]]*=.*|$key = $value|" "$CONFIG_FILE"
        else
            sed -i "s|^$key[[:space:]]*=.*|$key = $value|" "$CONFIG_FILE"
        fi
    else
        # Add new key after section header
        if [[ "$PLATFORM" == "Darwin" ]]; then
            sed -i '' "/^\[$section\]/a\\
$key = $value
" "$CONFIG_FILE"
        else
            sed -i "/^\[$section\]/a $key = $value" "$CONFIG_FILE"
        fi
    fi
}

# Update database and log paths for Docker
echo ""
echo "Updating config.ini for Docker paths..."
update_config "Bot" "db_path" "/data/databases/meshcore_bot.db"
update_config "Logging" "log_file" "/data/logs/meshcore_bot.log"

# Try to detect serial device
echo ""
echo "Detecting serial devices..."

SERIAL_DEVICE=""
DOCKER_DEVICE_PATH=""

if [[ "$PLATFORM" == "Linux" ]]; then
    # Linux: Prefer /dev/serial/by-id/ for stable device identification
    if [ -d "/dev/serial/by-id" ] && [ -n "$(ls -A /dev/serial/by-id 2>/dev/null)" ]; then
        # Look for common MeshCore device patterns (case-insensitive)
        # Prioritize devices that might be MeshCore-related
        DEVICE=$(ls /dev/serial/by-id/* 2>/dev/null | grep -iE "(meshcore|heltec|rak|ch340|cp210|ft232)" | head -1)
        # If no specific match, take the first USB serial device
        if [ -z "$DEVICE" ]; then
            DEVICE=$(ls /dev/serial/by-id/* 2>/dev/null | grep -i "usb" | head -1)
        fi
        # Last resort: any serial device
        if [ -z "$DEVICE" ]; then
            DEVICE=$(ls /dev/serial/by-id/* 2>/dev/null | head -1)
        fi
        
        if [ -n "$DEVICE" ] && [ -e "$DEVICE" ]; then
            SERIAL_DEVICE="$DEVICE"
            # For Docker, we'll map to /dev/ttyUSB0 in container
            DOCKER_DEVICE_PATH="/dev/ttyUSB0"
            echo "✓ Found serial device (by-id): $SERIAL_DEVICE"
        fi
    fi
    
    # Fallback to /dev/ttyUSB* or /dev/ttyACM* if by-id not found
    if [ -z "$SERIAL_DEVICE" ]; then
        # Try ttyUSB first (more common)
        for dev in /dev/ttyUSB* /dev/ttyACM*; do
            if [ -e "$dev" ]; then
                SERIAL_DEVICE="$dev"
                DOCKER_DEVICE_PATH="/dev/ttyUSB0"
                echo "✓ Found serial device: $SERIAL_DEVICE"
                break
            fi
        done
    fi
    
    # Update docker-compose.yml if device found (Linux only)
    if [ -n "$SERIAL_DEVICE" ] && [ -f "docker-compose.yml" ]; then
        echo "Updating docker-compose.yml with device mapping..."
        
        # Check if devices section exists (commented or uncommented)
        if grep -qE "^    #? devices:" docker-compose.yml; then
            # Uncomment and update existing devices section
            if [[ "$PLATFORM" == "Darwin" ]]; then
                # Uncomment devices line
                sed -i '' 's/^    # devices:/    devices:/' docker-compose.yml
                # Update device path - find commented device line and replace
                sed -i '' "s|^    #   - /dev/.*|      - $SERIAL_DEVICE:$DOCKER_DEVICE_PATH|" docker-compose.yml
                # Also handle if already uncommented
                sed -i '' "s|^      - /dev/.*|      - $SERIAL_DEVICE:$DOCKER_DEVICE_PATH|" docker-compose.yml
            else
                # Uncomment devices line
                sed -i 's/^    # devices:/    devices:/' docker-compose.yml
                # Update device path - find commented device line and replace
                sed -i "s|^    #   - /dev/.*|      - $SERIAL_DEVICE:$DOCKER_DEVICE_PATH|" docker-compose.yml
                # Also handle if already uncommented
                sed -i "s|^      - /dev/.*|      - $SERIAL_DEVICE:$DOCKER_DEVICE_PATH|" docker-compose.yml
            fi
        else
            # Add devices section after restart line
            if [[ "$PLATFORM" == "Darwin" ]]; then
                sed -i '' "/^    restart: unless-stopped/a\\
\\
    # Device access for serial ports (Linux)\\
    devices:\\
      - $SERIAL_DEVICE:$DOCKER_DEVICE_PATH
" docker-compose.yml
            else
                sed -i "/^    restart: unless-stopped/a\\
\\
    # Device access for serial ports (Linux)\\
    devices:\\
      - $SERIAL_DEVICE:$DOCKER_DEVICE_PATH
" docker-compose.yml
            fi
        fi
        echo "✓ Updated docker-compose.yml with device: $SERIAL_DEVICE -> $DOCKER_DEVICE_PATH"
    fi
    
elif [[ "$PLATFORM" == "Darwin" ]]; then
    # macOS: Use /dev/cu.* devices
    DEVICE=$(ls /dev/cu.usbmodem* /dev/cu.usbserial* 2>/dev/null | head -1)
    if [ -n "$DEVICE" ]; then
        SERIAL_DEVICE="$DEVICE"
        echo "✓ Found serial device: $SERIAL_DEVICE"
        echo "  Note: Docker Desktop on macOS doesn't support device passthrough."
        echo "  Consider using TCP connection or running natively on macOS."
    fi
fi

# Update config.ini with serial device if found
if [ -n "$SERIAL_DEVICE" ]; then
    if [[ "$PLATFORM" == "Linux" ]] && [[ "$SERIAL_DEVICE" == /dev/serial/by-id/* ]]; then
        # On Linux with by-id path, use it directly in config (more stable)
        update_config "Connection" "serial_port" "$SERIAL_DEVICE"
        echo "✓ Updated config.ini with serial port: $SERIAL_DEVICE"
    elif [[ "$PLATFORM" == "Linux" ]]; then
        # On Linux, use the Docker mapped path in config
        update_config "Connection" "serial_port" "$DOCKER_DEVICE_PATH"
        echo "✓ Updated config.ini with serial port: $DOCKER_DEVICE_PATH (mapped from $SERIAL_DEVICE)"
    else
        # macOS: just note it, but user needs to handle differently
        update_config "Connection" "serial_port" "$SERIAL_DEVICE"
        echo "✓ Updated config.ini with serial port: $SERIAL_DEVICE"
        echo "  ⚠️  Remember: Docker Desktop on macOS can't access serial devices directly."
    fi
else
    echo "⚠️  No serial device detected. You may need to:"
    echo "   - Connect your MeshCore device"
    echo "   - Manually set serial_port in config.ini"
    echo "   - Or use TCP/BLE connection instead"
fi

# Set permissions (container runs as UID 1000)
echo ""
echo "Setting permissions..."
chmod -R 755 data/
chown -R 1000:1000 data/ 2>/dev/null || echo "Note: Could not set ownership (may need sudo)"

echo ""
echo "✓ Setup complete!"
echo ""
echo "Next steps:"
if [ -z "$SERIAL_DEVICE" ]; then
    echo "1. Connect your MeshCore device or configure TCP/BLE connection"
fi
echo "1. Review data/config/config.ini and adjust settings if needed"
echo "2. Build the Docker image (to avoid pull warnings):"
echo "   docker compose build"
echo ""
echo "3. Start the container:"
echo "   docker compose up -d"
echo ""
echo "   Or build and start in one command:"
echo "   docker compose up -d --build"
echo ""
echo "4. View logs:"
echo "   docker compose logs -f"
