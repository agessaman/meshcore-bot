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

# Function to update config.ini (section-aware: only matches key within the given section)
update_config() {
    local section=$1
    local key=$2
    local value=$3
    local tmp_file="${CONFIG_FILE}.tmp.$$"

    awk -v section="$section" -v key="$key" -v value="$value" '
        /^\[/ {
            # Emit missing key at end of target section before we update state
            leaving_target = (in_section && need_add)
            in_section = ($0 == "[" section "]")
            need_add = in_section
            if (leaving_target) { print key " = " value }
        }
        in_section && $0 ~ "^" key "[[:space:]]*=" {
            print key " = " value
            need_add = 0
            next
        }
        { print }
        END {
            if (need_add && in_section) { print key " = " value }
        }
    ' "$CONFIG_FILE" > "$tmp_file" && mv "$tmp_file" "$CONFIG_FILE"

    # Ensure section exists if key was not present (file had no such section)
    if ! grep -q "^\[$section\]" "$CONFIG_FILE"; then
        echo "" >> "$CONFIG_FILE"
        echo "[$section]" >> "$CONFIG_FILE"
        echo "$key = $value" >> "$CONFIG_FILE"
    fi
}

# Update database and log paths for Docker
echo ""
echo "Updating config.ini for Docker paths..."

UPDATED_COUNT=0

# Update Bot database path
update_config "Bot" "db_path" "/data/databases/meshcore_bot.db"
echo "  ✓ Updated [Bot] db_path"
((UPDATED_COUNT++))

# Update Logging file path
# Ensure we use absolute path to avoid resolution issues
update_config "Logging" "log_file" "/data/logs/meshcore_bot.log"
# Verify the update worked
VERIFIED_LOG=$(grep "^log_file[[:space:]]*=" "$CONFIG_FILE" 2>/dev/null | sed 's/^log_file[[:space:]]*=[[:space:]]*//;s/^[[:space:]]*//;s/[[:space:]]*$//' || echo "")
if [[ "$VERIFIED_LOG" == "/data/logs/meshcore_bot.log" ]]; then
    echo "  ✓ Updated [Logging] log_file to /data/logs/meshcore_bot.log"
    ((UPDATED_COUNT++))
else
    echo "  ⚠️  Warning: log_file update may have failed. Current value: '$VERIFIED_LOG'"
    echo "     Expected: '/data/logs/meshcore_bot.log'"
    # Try to fix it
    if [[ "$PLATFORM" == "Darwin" ]]; then
        sed -i '' 's|^log_file[[:space:]]*=.*|log_file = /data/logs/meshcore_bot.log|' "$CONFIG_FILE"
    else
        sed -i 's|^log_file[[:space:]]*=.*|log_file = /data/logs/meshcore_bot.log|' "$CONFIG_FILE"
    fi
    echo "     Attempted to fix - please verify the config file"
    ((UPDATED_COUNT++))
fi

# [Web_Viewer] db_path is intentionally not set: when unset, the viewer uses [Bot] db_path.
# See config.ini.example and docs/web-viewer.md.

# Update PacketCapture paths (if section exists)
if grep -q "^\[PacketCapture\]" "$CONFIG_FILE"; then
    # Only update output_file if it's set to a relative path
    CURRENT_OUTPUT=$(grep "^output_file[[:space:]]*=" "$CONFIG_FILE" 2>/dev/null | sed 's/^output_file[[:space:]]*=[[:space:]]*//;s/^[[:space:]]*//;s/[[:space:]]*$//' || echo "")
    if [ -n "$CURRENT_OUTPUT" ] && [[ ! "$CURRENT_OUTPUT" == /* ]]; then
        # Relative path - update to logs directory
        update_config "PacketCapture" "output_file" "/data/logs/packets.jsonl"
        echo "  ✓ Updated [PacketCapture] output_file"
        ((UPDATED_COUNT++))
    fi
    
    # Update private_key_path if it's a relative path
    CURRENT_KEY=$(grep "^private_key_path[[:space:]]*=" "$CONFIG_FILE" 2>/dev/null | sed 's/^private_key_path[[:space:]]*=[[:space:]]*//;s/^[[:space:]]*//;s/[[:space:]]*$//' || echo "")
    if [ -n "$CURRENT_KEY" ] && [[ ! "$CURRENT_KEY" == /* ]]; then
        # Relative path - update to config directory (read-only is fine for keys)
        update_config "PacketCapture" "private_key_path" "/data/config/private_key"
        echo "  ✓ Updated [PacketCapture] private_key_path"
        ((UPDATED_COUNT++))
    fi
fi

# Update MapUploader private_key_path (if section exists)
if grep -q "^\[MapUploader\]" "$CONFIG_FILE"; then
    CURRENT_KEY=$(grep "^private_key_path[[:space:]]*=" "$CONFIG_FILE" 2>/dev/null | sed 's/^private_key_path[[:space:]]*=[[:space:]]*//;s/^[[:space:]]*//;s/[[:space:]]*$//' || echo "")
    if [ -n "$CURRENT_KEY" ] && [[ ! "$CURRENT_KEY" == /* ]]; then
        # Relative path - update to config directory
        update_config "MapUploader" "private_key_path" "/data/config/private_key"
        echo "  ✓ Updated [MapUploader] private_key_path"
        ((UPDATED_COUNT++))
    fi
fi

echo ""
echo "✓ Updated $UPDATED_COUNT path(s) for Docker deployment"

# Try to detect serial device
echo ""
echo "Detecting serial devices..."

SERIAL_DEVICE=""
DOCKER_DEVICE_PATH=""
ACTUAL_DEVICE_FOR_DOCKER=""  # Will hold resolved device path for Docker

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
    
    # Resolve symlink to actual device for Docker compatibility
    # Docker containers may not resolve symlinks correctly, so use the actual device
    ACTUAL_DEVICE_FOR_DOCKER="$SERIAL_DEVICE"
    if [[ "$SERIAL_DEVICE" == /dev/serial/by-id/* ]]; then
        RESOLVED=$(readlink -f "$SERIAL_DEVICE" 2>/dev/null || echo "")
        if [ -n "$RESOLVED" ] && [ -e "$RESOLVED" ]; then
            ACTUAL_DEVICE_FOR_DOCKER="$RESOLVED"
            echo "  Resolved symlink: $SERIAL_DEVICE -> $ACTUAL_DEVICE_FOR_DOCKER"
        fi
    fi
    
    # Update docker-compose files if device found (Linux only)
    # Prefer docker-compose.override.yml if it exists, otherwise update docker-compose.yml
    COMPOSE_FILE=""
    if [ -f "docker-compose.override.yml" ]; then
        COMPOSE_FILE="docker-compose.override.yml"
        echo "Updating docker-compose.override.yml with device mapping..."
    elif [ -f "docker-compose.yml" ]; then
        COMPOSE_FILE="docker-compose.yml"
        echo "Updating docker-compose.yml with device mapping..."
    fi
    
    if [ -n "$SERIAL_DEVICE" ] && [ -n "$COMPOSE_FILE" ]; then
        # Function to update devices section in a compose file
        update_compose_devices() {
            local file=$1
            local device=$2
            local container_path=$3
            
            # First, check if device is incorrectly mapped in volumes section and remove it
            # Look for device mappings in volumes (e.g., /dev/ttyACM0:/dev/ttyUSB0)
            if grep -qE "^      - /dev/(tty|serial)" "$file"; then
                echo "  Found device mapping in volumes section, moving to devices section..."
                # Remove device mappings from volumes section
                if [[ "$PLATFORM" == "Darwin" ]]; then
                    sed -i '' '/^      - \/dev\/\(tty\|serial\)/d' "$file"
                else
                    sed -i '/^      - \/dev\/\(tty\|serial\)/d' "$file"
                fi
            fi
            
            # Check if devices section exists (commented or uncommented)
            if grep -qE "^    #? devices:" "$file"; then
                # Uncomment and update existing devices section
                if [[ "$PLATFORM" == "Darwin" ]]; then
                    # Uncomment devices line
                    sed -i '' 's/^    # devices:/    devices:/' "$file"
                    # Update device path - find commented device line and replace
                    sed -i '' "s|^    #   - /dev/.*|      - $device:$container_path|" "$file"
                    # Also handle if already uncommented - update existing device mapping
                    if grep -qE "^      - /dev/" "$file"; then
                        sed -i '' "s|^      - /dev/[^:]*:.*|      - $device:$container_path|" "$file"
                    else
                        # Add device if devices section exists but no devices listed
                        sed -i '' "/^    devices:/a\\
      - $device:$container_path
" "$file"
                    fi
                else
                    # Uncomment devices line
                    sed -i 's/^    # devices:/    devices:/' "$file"
                    # Update device path - find commented device line and replace
                    sed -i "s|^    #   - /dev/.*|      - $device:$container_path|" "$file"
                    # Also handle if already uncommented - update existing device mapping
                    if grep -qE "^      - /dev/" "$file"; then
                        sed -i "s|^      - /dev/[^:]*:.*|      - $device:$container_path|" "$file"
                    else
                        # Add device if devices section exists but no devices listed
                        sed -i "/^    devices:/a\\
      - $device:$container_path
" "$file"
                    fi
                fi
            else
                # Check if we're in a services section
                if grep -q "^services:" "$file" && grep -q "^  meshcore-bot:" "$file"; then
                    # Add devices section after meshcore-bot service definition
                    # Find a good insertion point (after restart, network_mode, or volumes)
                    if grep -q "^    network_mode:" "$file"; then
                        INSERT_AFTER="network_mode:"
                    elif grep -q "^    volumes:" "$file"; then
                        INSERT_AFTER="volumes:"
                    elif grep -q "^    restart:" "$file"; then
                        INSERT_AFTER="restart:"
                    else
                        # Default: after container_name or first line after meshcore-bot
                        INSERT_AFTER="meshcore-bot:"
                    fi
                    
                    if [[ "$PLATFORM" == "Darwin" ]]; then
                        sed -i '' "/^    $INSERT_AFTER/a\\
\\
    # Device access for serial ports\\
    devices:\\
      - $device:$container_path
" "$file"
                    else
                        sed -i "/^    $INSERT_AFTER/a\\
\\
    # Device access for serial ports\\
    devices:\\
      - $device:$container_path
" "$file"
                    fi
                else
                    # File doesn't have expected structure, append at end
                    {
                        echo ""
                        echo "services:"
                        echo "  meshcore-bot:"
                        echo "    # Device access for serial ports"
                        echo "    devices:"
                        echo "      - $device:$container_path"
                    } >> "$file"
                fi
            fi
        }
        
        # Use actual device path (not symlink) for Docker device mapping
        update_compose_devices "$COMPOSE_FILE" "$ACTUAL_DEVICE_FOR_DOCKER" "$DOCKER_DEVICE_PATH"
        echo "✓ Updated $COMPOSE_FILE with device: $ACTUAL_DEVICE_FOR_DOCKER -> $DOCKER_DEVICE_PATH"
        echo "  Note: Device mappings should be in 'devices:' section, not 'volumes:'"
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
# Use the actual device path (resolved from symlink if needed) for Docker compatibility
if [ -n "$SERIAL_DEVICE" ]; then
    # Determine the device path to use in config.ini
    CONFIG_DEVICE="$SERIAL_DEVICE"
    
    # On Linux, resolve symlinks to actual device for Docker compatibility
    if [[ "$PLATFORM" == "Linux" ]] && [[ "$SERIAL_DEVICE" == /dev/serial/by-id/* ]]; then
        # Use the resolved device if we have it, otherwise resolve now
        if [ -n "$ACTUAL_DEVICE_FOR_DOCKER" ]; then
            CONFIG_DEVICE="$ACTUAL_DEVICE_FOR_DOCKER"
        else
            RESOLVED=$(readlink -f "$SERIAL_DEVICE" 2>/dev/null || echo "")
            if [ -n "$RESOLVED" ] && [ -e "$RESOLVED" ]; then
                CONFIG_DEVICE="$RESOLVED"
            fi
        fi
    fi
    
    # Update config.ini with the device path
    update_config "Connection" "serial_port" "$CONFIG_DEVICE"
    
    if [ "$CONFIG_DEVICE" != "$SERIAL_DEVICE" ]; then
        echo "✓ Updated config.ini with serial port: $CONFIG_DEVICE"
        echo "  (resolved from $SERIAL_DEVICE for Docker compatibility)"
    else
        echo "✓ Updated config.ini with serial port: $CONFIG_DEVICE"
    fi
    
    if [[ "$PLATFORM" == "Darwin" ]]; then
        echo "  ⚠️  Remember: Docker Desktop on macOS can't access serial devices directly."
    fi
else
    echo "⚠️  No serial device detected. You may need to:"
    echo "   - Connect your MeshCore device"
    echo "   - Manually set serial_port in config.ini"
    echo "   - Or use TCP/BLE connection instead"
fi

# Detect git branch and set Docker image tag
echo ""
echo "Detecting git branch for Docker image tag..."

# Try to get current branch name
GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
DOCKER_IMAGE_TAG="latest"
DOCKER_IMAGE_REGISTRY="ghcr.io/agessaman/meshcore-bot"

if [ -n "$GIT_BRANCH" ]; then
    # Map branch names to image tags
    case "$GIT_BRANCH" in
        main|master)
            DOCKER_IMAGE_TAG="latest"
            ;;
        *)
            # Use branch name as tag (e.g., dev -> dev, feature/xyz -> feature-xyz)
            DOCKER_IMAGE_TAG=$(echo "$GIT_BRANCH" | sed 's/[\/_]/-/g')
            ;;
    esac
    echo "  ✓ Detected branch: $GIT_BRANCH -> image tag: $DOCKER_IMAGE_TAG"
else
    echo "  ⚠️  Not a git repository or branch detection failed, using 'latest'"
fi

# Create or update .env file for docker-compose
# Ensure this always runs, even if there were non-critical errors earlier
ENV_FILE=".env"

# Temporarily disable exit on error for .env creation
set +e

# Check if .env file exists and has the tag variable
if [ ! -f "$ENV_FILE" ]; then
    # Create new .env file
    cat > "$ENV_FILE" << EOF
# Docker image configuration (auto-generated by docker-setup.sh)
DOCKER_IMAGE_REGISTRY=$DOCKER_IMAGE_REGISTRY
DOCKER_IMAGE_TAG=$DOCKER_IMAGE_TAG
EOF
    if [ $? -eq 0 ]; then
        echo "  ✓ Created .env file with image tag: $DOCKER_IMAGE_TAG"
    else
        echo "  ⚠️  Warning: Failed to create .env file"
    fi
elif ! grep -q "^DOCKER_IMAGE_TAG=" "$ENV_FILE" 2>/dev/null; then
    # .env exists but doesn't have the tag - append it
    {
        echo ""
        echo "# Docker image configuration (auto-generated by docker-setup.sh)"
        echo "DOCKER_IMAGE_REGISTRY=$DOCKER_IMAGE_REGISTRY"
        echo "DOCKER_IMAGE_TAG=$DOCKER_IMAGE_TAG"
    } >> "$ENV_FILE"
    if [ $? -eq 0 ]; then
        echo "  ✓ Added Docker image configuration to .env file with tag: $DOCKER_IMAGE_TAG"
    else
        echo "  ⚠️  Warning: Failed to append to .env file"
    fi
else
    # Update existing .env file
    if [[ "$PLATFORM" == "Darwin" ]]; then
        sed -i '' "s|^DOCKER_IMAGE_TAG=.*|DOCKER_IMAGE_TAG=$DOCKER_IMAGE_TAG|" "$ENV_FILE" 2>/dev/null
        sed -i '' "s|^DOCKER_IMAGE_REGISTRY=.*|DOCKER_IMAGE_REGISTRY=$DOCKER_IMAGE_REGISTRY|" "$ENV_FILE" 2>/dev/null
    else
        sed -i "s|^DOCKER_IMAGE_TAG=.*|DOCKER_IMAGE_TAG=$DOCKER_IMAGE_TAG|" "$ENV_FILE" 2>/dev/null
        sed -i "s|^DOCKER_IMAGE_REGISTRY=.*|DOCKER_IMAGE_REGISTRY=$DOCKER_IMAGE_REGISTRY|" "$ENV_FILE" 2>/dev/null
    fi
    if [ $? -eq 0 ]; then
        echo "  ✓ Updated .env file with image tag: $DOCKER_IMAGE_TAG"
    else
        echo "  ⚠️  Warning: Failed to update .env file"
    fi
fi

# Re-enable exit on error
set -e

# Verify .env file was created/updated
if [ -f "$ENV_FILE" ] && grep -q "^DOCKER_IMAGE_TAG=" "$ENV_FILE" 2>/dev/null; then
    ACTUAL_TAG=$(grep "^DOCKER_IMAGE_TAG=" "$ENV_FILE" | cut -d'=' -f2 | tr -d ' ')
    echo "  ✓ Verified .env file contains DOCKER_IMAGE_TAG=$ACTUAL_TAG"
else
    echo "  ⚠️  Warning: .env file may not have been created correctly"
    echo "     Creating .env file manually..."
    cat > "$ENV_FILE" << EOF
# Docker image configuration (auto-generated by docker-setup.sh)
DOCKER_IMAGE_REGISTRY=$DOCKER_IMAGE_REGISTRY
DOCKER_IMAGE_TAG=$DOCKER_IMAGE_TAG
EOF
    if [ -f "$ENV_FILE" ]; then
        echo "  ✓ Manually created .env file with tag: $DOCKER_IMAGE_TAG"
    else
        echo "  ✗ Error: Could not create .env file. Please create it manually:"
        echo "     echo 'DOCKER_IMAGE_REGISTRY=$DOCKER_IMAGE_REGISTRY' > .env"
        echo "     echo 'DOCKER_IMAGE_TAG=$DOCKER_IMAGE_TAG' >> .env"
    fi
fi

# Set permissions (container runs as UID 1000)
echo ""
echo "Setting permissions..."
chmod -R 755 data/
chown -R 1000:1000 data/ 2>/dev/null || echo "Note: Could not set ownership (may need sudo)"

echo ""
echo "✓ Setup complete!"
echo ""
echo "Verifying critical paths in config.ini..."
# Verify log_file is set correctly
FINAL_LOG_FILE=$(grep "^log_file[[:space:]]*=" "$CONFIG_FILE" 2>/dev/null | sed 's/^log_file[[:space:]]*=[[:space:]]*//' | tr -d ' ' || echo "")
if [[ "$FINAL_LOG_FILE" == "/data/logs/meshcore_bot.log" ]]; then
    echo "  ✓ log_file is correctly set to: $FINAL_LOG_FILE"
else
    echo "  ⚠️  WARNING: log_file is set to: '$FINAL_LOG_FILE' (expected: '/data/logs/meshcore_bot.log')"
    echo "     This may cause 'Read-only file system' errors. Please check data/config/config.ini"
fi

# Verify db_path is set correctly
FINAL_DB_PATH=$(grep "^db_path[[:space:]]*=" "$CONFIG_FILE" 2>/dev/null | sed 's/^db_path[[:space:]]*=[[:space:]]*//' | tr -d ' ' || echo "")
if [[ "$FINAL_DB_PATH" == "/data/databases/meshcore_bot.db" ]]; then
    echo "  ✓ db_path is correctly set to: $FINAL_DB_PATH"
else
    echo "  ⚠️  WARNING: db_path is set to: '$FINAL_DB_PATH' (expected: '/data/databases/meshcore_bot.db')"
fi

echo ""
echo "Next steps:"
if [ -z "$SERIAL_DEVICE" ]; then
    echo "1. Connect your MeshCore device or configure TCP/BLE connection"
fi
echo "1. Review data/config/config.ini and adjust settings if needed"
echo "2. If you have a running container, STOP it first:"
echo "   docker compose down"
echo ""
echo "3. Build the Docker image (to avoid pull warnings):"
echo "   docker compose build"
echo ""
echo "4. Start the container:"
echo "   docker compose up -d"
echo ""
echo "   Or build and start in one command:"
echo "   docker compose up -d --build"
echo ""
echo "5. View logs:"
echo "   docker compose logs -f"
echo ""
echo "⚠️  IMPORTANT: If you had a container running before running this script,"
echo "   you MUST restart it (docker compose down && docker compose up -d) for"
echo "   the config changes to take effect!"