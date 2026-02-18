# Docker Deployment Guide

This guide explains how to deploy meshcore-bot using Docker and Docker Compose.

## Prerequisites

- Docker Engine 20.10+ or Docker Desktop
- Docker Compose 2.0+ (included with Docker Desktop)

## Quick Start

1. **Clone the repository** (if you haven't already):
   ```bash
   git clone <repository-url>
   cd meshcore-bot
   ```

2. **Run the setup script** (recommended):
   ```bash
   ./docker-setup.sh
   ```
   
   This will create the necessary directories and copy the example config file.

   **Or manually:**
   ```bash
   mkdir -p data/{config,databases,logs,backups}
   cp config.ini.example data/config/config.ini
   ```

3. **Edit configuration file**:
   ```bash
   # Edit data/config/config.ini with your settings
   nano data/config/config.ini  # or your preferred editor
   ```

4. **Update database paths in config.ini**:
   The setup script (`./docker-setup.sh`) will automatically update these paths, but you can also set them manually:
   ```ini
   [Bot]
   db_path = /data/databases/meshcore_bot.db
   
   [Web_Viewer]
   db_path = /data/databases/meshcore_bot.db
   
   [Logging]
   log_file = /data/logs/meshcore_bot.log
   ```
   
   **Important**: The log file path must be absolute (`/data/logs/...`), not relative. Relative paths will resolve to the config directory which is read-only.

5. **Start the container**:
   ```bash
   docker-compose up -d
   ```

6. **View logs**:
   ```bash
   docker-compose logs -f
   ```

## Configuration

### Volume Mappings

The `docker-compose.yml` file maps the following directories:

- `./data/config` → `/data/config` (read-only) - Configuration files
- `./data/databases` → `/data/databases` - SQLite database files
- `./data/logs` → `/data/logs` - Log files
- `./data/backups` → `/data/backups` - Database backups

### Serial Port Access

**⚠️ Important: Docker Desktop on macOS does NOT support serial device passthrough.**

If you're using a serial connection, you have several options:

**Option 1: Use TCP Connection (Recommended for macOS)**
If your MeshCore device supports TCP/IP (via gateway or bridge), configure it in `config.ini`:
```ini
[Connection]
connection_type = tcp
hostname = 192.168.1.60  # Your device's IP or hostname
tcp_port = 5000
```
Then comment out or remove the `devices` section in `docker-compose.yml`.

**Option 2: Serial-to-TCP Bridge (macOS workaround)**
Use a tool like `socat` to bridge the serial port to TCP on the host:
```bash
# On macOS host, create TCP bridge
socat TCP-LISTEN:5000,reuseaddr,fork FILE:/dev/cu.usbmodem1101,raw,nonblock,waitlock=/var/run/socat.pid
```
Then configure the bot to use TCP connection to `localhost:5000`.

**Option 3: Device Mapping (Linux only)**
On Linux, you can map the serial device directly:
```yaml
devices:
  - /dev/ttyUSB0:/dev/ttyUSB0
```

**Option 4: Host Network Mode (Linux only, for BLE)**
For BLE connections on Linux, you may need host network access:
```yaml
network_mode: host
```
**Note**: Host network mode gives the container full access to the host network, which has security implications.

### Web Viewer Port

If you enable the web viewer, uncomment and adjust the `ports` section:
```yaml
ports:
  - "8080:8080"
```

Make sure your `config.ini` has:
```ini
[Web_Viewer]
enabled = true
host = 0.0.0.0  # Required for Docker port mapping
port = 8080
```

## Building the Image

### Using Docker Compose

**Important**: To avoid pull warnings, build the image first:
```bash
docker compose build
```

Then start the container:
```bash
docker compose up -d
```

Or build and start in one command:
```bash
docker compose up -d --build
```

The `--build` flag ensures the image is built locally rather than attempting to pull from a registry.

### Using Docker directly

```bash
docker build -t meshcore-bot:latest .
```

## Running the Container

### Start in background
```bash
docker-compose up -d
```

### Start in foreground (see logs)
```bash
docker-compose up
```

### Stop the container
```bash
docker-compose down
```

### Restart the container
```bash
docker-compose restart
```

## Managing the Container

### View logs
```bash
# Follow logs
docker-compose logs -f

# Last 100 lines
docker-compose logs --tail=100

# Logs for specific service
docker-compose logs meshcore-bot
```

### Execute commands in container
```bash
docker-compose exec meshcore-bot bash
```

### Update the container
```bash
# Pull latest changes
git pull

# Rebuild and restart
docker-compose up -d --build
```

## Using Pre-built Images

If you're using GitHub Container Registry images:

1. **Update docker-compose.yml** to use the image:
   ```yaml
   services:
     meshcore-bot:
       image: ghcr.io/your-username/meshcore-bot:latest
       # Remove or comment out the 'build' section
   ```

2. **Pull and start**:
   ```bash
   docker-compose pull
   docker-compose up -d
   ```

## Troubleshooting

### Permission Issues

If you encounter permission issues with database or log files:

1. **Check file ownership**:
   ```bash
   ls -la data/databases/
   ls -la data/logs/
   ```

2. **Fix permissions** (if needed):
   ```bash
   sudo chown -R 1000:1000 data/
   ```

The container runs as user ID 1000 (meshcore user).

### Serial Port Permission Denied

If you see `[Errno 13] Permission denied` when accessing serial devices:

1. **Check device permissions** (on host):
   ```bash
   ls -l /dev/ttyUSB0  # or /dev/ttyACM0
   # Should show: crw-rw---- 1 root dialout ...
   ```

2. **Ensure device has dialout group** (on host):
   ```bash
   # Check current group
   ls -l /dev/ttyUSB0 | awk '{print $4}'
   
   # If not dialout, fix it (temporary - will reset on reboot)
   sudo chmod 666 /dev/ttyUSB0
   
   # Or make it permanent with udev rules:
   sudo nano /etc/udev/rules.d/99-serial-permissions.rules
   # Add: KERNEL=="ttyUSB[0-9]*", MODE="0666", GROUP="dialout"
   # Then: sudo udevadm control --reload-rules
   ```

3. **Rebuild the container** (to ensure user is in dialout group):
   ```bash
   docker compose build
   docker compose up -d
   ```

4. **Alternative: Use privileged mode** (less secure, but works):
   ```yaml
   # In docker-compose.override.yml
   services:
     meshcore-bot:
       privileged: true
   ```

### Serial Port Not Found

1. **Check device exists**:
   ```bash
   ls -l /dev/ttyUSB0  # or your device
   ```

2. **Use host network mode** if device mapping doesn't work:
   ```yaml
   network_mode: host
   ```

### Database Locked Errors

If you see database locked errors:

1. **Stop the container**:
   ```bash
   docker-compose down
   ```

2. **Check for leftover database files**:
   ```bash
   ls -la data/databases/*.db-*
   ```

3. **Remove lock files** (if safe):
   ```bash
   rm data/databases/*.db-shm data/databases/*.db-wal
   ```

4. **Restart**:
   ```bash
   docker-compose up -d
   ```

### Container Won't Start

1. **Check logs**:
   ```bash
   docker-compose logs
   ```

2. **Verify config file exists**:
   ```bash
   ls -la data/config/config.ini
   ```

3. **Test config file syntax**:
   ```bash
   docker-compose run --rm meshcore-bot python3 -c "import configparser; c = configparser.ConfigParser(); c.read('/data/config/config.ini'); print('Config OK')"
   ```

### Build Failures on ARM Devices (Orange Pi, Raspberry Pi, etc.)

If you encounter network errors during build like:
```
failed to add the host (veth...) <=> sandbox (veth...) pair interfaces: operation not supported
```

Try these solutions:

1. **Restart Docker daemon**:
   ```bash
   sudo systemctl restart docker
   ```

2. **Check kernel modules are loaded**:
   ```bash
   lsmod | grep bridge
   lsmod | grep veth
   ```
   If missing, load them:
   ```bash
   sudo modprobe bridge
   sudo modprobe veth
   ```

3. **Build directly with docker build** (bypasses compose networking - RECOMMENDED):
   ```bash
   # Build the image directly
   DOCKER_BUILDKIT=0 docker build -t meshcore-bot:latest .
   
   # Then use docker compose normally (it will use the existing image)
   docker compose up -d
   ```
   This bypasses Docker Compose's networking setup during build, which often fails on ARM devices.

4. **Use host network mode for build** (alternative):
   ```bash
   DOCKER_BUILDKIT=0 docker build --network=host -t meshcore-bot:latest .
   docker compose up -d
   ```

5. **Check Docker bridge configuration**:
   ```bash
   sudo brctl show
   ```
   If bridge doesn't exist, Docker may need to be reconfigured.

6. **Check Docker daemon logs**:
   ```bash
   sudo journalctl -u docker -n 50
   ```

7. **Reinstall Docker** (if other solutions fail):
   ```bash
   # Backup your data first!
   sudo apt-get remove docker docker-engine docker.io containerd runc
   # Then reinstall Docker following Armbian/Docker official instructions
   ```

## Production Deployment

For production deployments:

1. **Use specific image tags** instead of `latest`:
   ```yaml
   image: ghcr.io/your-username/meshcore-bot:v1.0.0
   ```

2. **Set resource limits** in `docker-compose.yml`:
   ```yaml
   deploy:
     resources:
       limits:
         cpus: '1.0'
         memory: 512M
       reservations:
         cpus: '0.5'
         memory: 256M
   ```

3. **Use secrets management** for sensitive configuration:
   - Use Docker secrets (Docker Swarm)
   - Use environment variables for API keys
   - Mount secret files as read-only volumes

4. **Enable log rotation** (already configured in docker-compose.yml):
   ```yaml
   logging:
     driver: "json-file"
     options:
       max-size: "10m"
       max-file: "3"
   ```

5. **Set up health checks** (already included in Dockerfile):
   The container includes a basic health check. Monitor with:
   ```bash
   docker-compose ps
   ```

## Backup and Restore

### Backup

```bash
# Backup databases
docker-compose exec meshcore-bot tar czf /data/backups/backup-$(date +%Y%m%d).tar.gz /data/databases

# Or from host
tar czf backups/backup-$(date +%Y%m%d).tar.gz data/databases/
```

### Restore

```bash
# Stop container
docker-compose down

# Restore databases
tar xzf backups/backup-YYYYMMDD.tar.gz -C data/

# Start container
docker-compose up -d
```

## Security Considerations

1. **Non-root user**: The container runs as a non-root user (UID 1000)

2. **Read-only config**: Config directory is mounted read-only to prevent accidental modifications

3. **Network isolation**: By default, containers are isolated. Only expose ports you need

4. **Secrets**: Never commit API keys or sensitive data to version control. Use environment variables or secrets management

5. **Web viewer**: If enabled, ensure it's only accessible on trusted networks or use a reverse proxy with authentication

## Additional Resources

- [Docker Documentation](https://docs.docker.com/)
- [Docker Compose Documentation](https://docs.docker.com/compose/)
- [Main README](https://github.com/agessaman/meshcore-bot/blob/main/README.md) for general bot configuration.
