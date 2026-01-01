# MeshCore Bot Docker Deployment Guide

This guide covers running MeshCore Bot in Docker containers with support for Serial, TCP, and BLE connections.

## Quick Start

### Prerequisites

- Docker Engine 20.10+
- Docker Compose 2.0+
- For Serial: USB device access permissions
- For BLE: Bluetooth hardware and BlueZ on host

### 1. Build the Image

```bash
docker-compose build
```

### 2. Configure the Bot

```bash
# Copy the example config
cp config.ini.example config.ini

# Edit with your settings
nano config.ini
```

**Important settings to configure:**
- `[Connection]` section: Set `connection_type` to `serial`, `tcp`, or `ble`
- `[Bot]` section: Set `bot_latitude` and `bot_longitude` for your location
- `[Channels]` section: Set `monitor_channels` for channels to respond on

### 3. Create Data Directories

```bash
mkdir -p data logs
```

### 4. Start the Container

```bash
docker-compose up -d
```

### 5. View Logs

```bash
docker-compose logs -f
```

### 6. Access Web Viewer

Open http://localhost:8080 in your browser.

---

## Connection Types

### Serial Connection (USB)

Serial is the default connection type for direct USB connection to your MeshCore device.

**Step 1: Identify your serial device**

```bash
# Linux
ls -la /dev/ttyUSB*
# or
dmesg | grep tty

# macOS
ls -la /dev/tty.usb*
```

**Step 2: Configure**

Edit `config.ini`:
```ini
[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0
```

**Step 3: Run with device mapping**

The default `docker-compose.yml` already maps `/dev/ttyUSB0`. If your device is different:

```bash
# Set custom device path
SERIAL_DEVICE=/dev/ttyACM0 docker-compose up -d
```

**Permissions:**

On Linux, you may need to add your user to the `dialout` group:
```bash
sudo usermod -aG dialout $USER
# Log out and back in for changes to take effect
```

---

### TCP Connection

TCP is the simplest connection type when your MeshCore device is accessible over the network.

**Configure:**

```bash
# Set via environment variables
MESHCORE_CONNECTION_TYPE=tcp \
MESHCORE_TCP_HOST=192.168.1.100 \
MESHCORE_TCP_PORT=5000 \
docker-compose up -d
```

Or edit `config.ini`:
```ini
[Connection]
connection_type = tcp
hostname = 192.168.1.100
tcp_port = 5000
```

---

### BLE (Bluetooth) Connection

BLE connections require privileged access and host networking.

**Step 1: Edit docker-compose.yml**

Uncomment these lines:
```yaml
services:
  meshcore-bot:
    privileged: true
    network_mode: host
```

**Step 2: Configure**

```bash
MESHCORE_CONNECTION_TYPE=ble \
MESHCORE_BLE_DEVICE=MeshCore \
docker-compose up -d
```

**Note:** With `network_mode: host`, the web viewer is accessible directly on the host's port 8080 (port mapping is ignored).

---

## Environment Variables

All configuration can be overridden via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `MESHCORE_CONNECTION_TYPE` | Connection type: serial, tcp, ble | serial |
| `MESHCORE_SERIAL_PORT` | Serial device path | /dev/ttyUSB0 |
| `MESHCORE_TCP_HOST` | TCP hostname or IP | - |
| `MESHCORE_TCP_PORT` | TCP port | 5000 |
| `MESHCORE_BLE_DEVICE` | BLE device name | - (auto-detect) |
| `MESHCORE_TIMEOUT` | Connection timeout (seconds) | 30 |
| `MESHCORE_BOT_NAME` | Bot identification name | - |
| `MESHCORE_LATITUDE` | Bot location latitude | - |
| `MESHCORE_LONGITUDE` | Bot location longitude | - |
| `MESHCORE_WEB_ENABLED` | Enable web viewer | true |
| `MESHCORE_WEB_PORT` | Web viewer port | 8080 |
| `TZ` | Timezone | UTC |
| `N2YO_API_KEY` | N2YO satellite API key | - |
| `AIRNOW_API_KEY` | AirNow AQI API key | - |
| `FORECAST_SOLAR_API_KEY` | Forecast.Solar API key | - |

**Example with multiple environment variables:**

```bash
TZ=America/Los_Angeles \
MESHCORE_BOT_NAME=MyBot \
MESHCORE_LATITUDE=47.6062 \
MESHCORE_LONGITUDE=-122.3321 \
docker-compose up -d
```

---

## Volume Mounts

| Container Path | Purpose | Host Path |
|----------------|---------|-----------|
| `/app/config.ini` | Configuration file | `./config.ini` |
| `/app/data` | Database and persistent data | `./data` |
| `/app/logs` | Log files | `./logs` |

---

## Docker Commands Reference

### Build

```bash
# Build the image
docker-compose build

# Build without cache (for clean rebuild)
docker-compose build --no-cache
```

### Run

```bash
# Start in detached mode
docker-compose up -d

# Start with logs visible
docker-compose up

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

### Maintenance

```bash
# Restart
docker-compose restart

# Update (rebuild and restart)
docker-compose up -d --build

# Shell access
docker exec -it meshcore-bot /bin/bash

# Database backup
docker exec meshcore-bot python backup_database.py

# Check status
docker-compose ps
```

---

## Portainer Deployment

Use the standalone compose file for Portainer or other container management platforms.

### Using Portainer Stacks

1. **Build the image first** (on the Docker host):
   ```bash
   git clone https://github.com/agessaman/meshcore-bot.git
   cd meshcore-bot
   docker build -t meshcore-bot:latest .
   ```

2. **Create required files** on the host:
   ```bash
   # Create directories
   mkdir -p /opt/meshcore-bot/data /opt/meshcore-bot/logs

   # Copy and edit config
   cp config.ini.example /opt/meshcore-bot/config.ini
   nano /opt/meshcore-bot/config.ini
   ```

3. **In Portainer**, go to **Stacks** → **Add Stack**

4. **Copy the contents** of `docker-compose.standalone.yml` into the web editor

5. **Update the volume paths** to match your system:
   ```yaml
   volumes:
     - /opt/meshcore-bot/config.ini:/app/config.ini:rw
     - /opt/meshcore-bot/data:/app/data
     - /opt/meshcore-bot/logs:/app/logs
   ```

6. **For serial devices**, uncomment and configure:
   ```yaml
   devices:
     - /dev/ttyUSB0:/dev/ttyUSB0
   ```

7. **Deploy the stack**

### Standalone Compose File

The `docker-compose.standalone.yml` file is designed for:
- Portainer Stacks
- Remote Docker hosts
- Container management platforms
- Environments without the full repository

Key features:
- All configuration via environment variables
- Clear documentation in comments
- Pre-configured health checks
- Logging limits to prevent disk fill

### Environment Variables in Portainer

Instead of editing the compose file, you can set environment variables in Portainer's stack configuration:

| Variable | Example Value |
|----------|---------------|
| `TZ` | `America/New_York` |
| `MESHCORE_CONNECTION_TYPE` | `serial` |
| `MESHCORE_SERIAL_PORT` | `/dev/ttyUSB0` |
| `MESHCORE_LATITUDE` | `47.6062` |
| `MESHCORE_LONGITUDE` | `-122.3321` |

---

## NAS Deployment (Synology/QNAP)

### Synology DSM

1. **Install Docker** from Package Center

2. **SSH into your NAS** and navigate to the project directory

3. **Identify the serial device:**
   ```bash
   ls /dev/tty*
   ```
   Common devices: `/dev/ttyUSB0`, `/dev/ttyACM0`

4. **Build and run:**
   ```bash
   cd /volume1/docker/meshcore-bot
   docker-compose build
   SERIAL_DEVICE=/dev/ttyUSB0 docker-compose up -d
   ```

### QNAP Container Station

1. **Create a new container** using the built image or Dockerfile

2. **Map the serial device** in Container Station settings

3. **Mount volumes** for config, data, and logs

---

## Troubleshooting

### Container won't start

```bash
# Check logs
docker-compose logs

# Check if config is mounted correctly
docker exec meshcore-bot cat /app/config.ini

# Check container status
docker-compose ps
```

### Serial device not found

```bash
# Verify device exists on host
ls -la /dev/ttyUSB0

# Check if device is mapped
docker exec meshcore-bot ls -la /dev/ttyUSB0

# Check permissions
docker exec meshcore-bot stat /dev/ttyUSB0

# Try privileged mode (add to docker-compose.yml)
privileged: true
```

### Connection timeout

```bash
# Increase timeout
MESHCORE_TIMEOUT=60 docker-compose up -d

# Or edit config.ini
[Connection]
timeout = 60
```

### Web viewer not accessible

```bash
# Check if port is exposed
docker port meshcore-bot

# Check if web viewer is enabled
docker exec meshcore-bot grep -A5 "\[Web_Viewer\]" /app/config.ini

# Check container logs for Flask startup
docker-compose logs | grep -i flask
```

### Database issues

```bash
# Check data directory permissions
ls -la ./data/

# Check inside container
docker exec meshcore-bot ls -la /app/data/

# Manually run database backup
docker exec meshcore-bot python backup_database.py
```

---

## Security Considerations

1. **Web Viewer**: Has NO authentication built-in. Restrict access via:
   - Firewall rules
   - Reverse proxy with authentication
   - Bind to localhost only (`MESHCORE_WEB_HOST=127.0.0.1`)

2. **Config file**: Contains no secrets by default, but API keys may be sensitive

3. **Serial access**: Requires root or dialout group membership

4. **Privileged mode**: Required for BLE, gives container full host access

---

## Manual Docker Run (without Compose)

If you prefer not to use Docker Compose:

```bash
# Build
docker build -t meshcore-bot:latest .

# Run with serial device
docker run -d \
  --name meshcore-bot \
  --restart unless-stopped \
  --device /dev/ttyUSB0:/dev/ttyUSB0 \
  -v $(pwd)/config.ini:/app/config.ini:rw \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  -p 8080:8080 \
  -e TZ=America/New_York \
  -e MESHCORE_CONNECTION_TYPE=serial \
  meshcore-bot:latest

# View logs
docker logs -f meshcore-bot

# Stop
docker stop meshcore-bot
docker rm meshcore-bot
```

---

## Updating

To update to a new version:

```bash
# Pull latest code
git pull

# Rebuild image
docker-compose build --no-cache

# Restart with new image
docker-compose up -d
```

Your data and configuration will be preserved in the mounted volumes.
