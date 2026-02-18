# MeshCore Bot Service Installation

This guide explains how to install the MeshCore Bot as a systemd service on Linux systems.

## Prerequisites

- Linux system with systemd
- Python 3.7+
- Root/sudo access
- MeshCore-compatible device

## Quick Installation

1. **Clone and navigate to the bot directory:**
   ```bash
   git clone <repository-url>
   cd meshcore-bot
   ```

2. **Run the installation script:**
   ```bash
   sudo ./install-service.sh
   ```

3. **Configure the bot:**
   ```bash
   sudo nano /opt/meshcore-bot/config.ini
   ```

4. **Start the service:**
   ```bash
   sudo systemctl start meshcore-bot
   ```

5. **Check status:**
   ```bash
   sudo systemctl status meshcore-bot
   ```

## Manual Installation

If you prefer to install manually:

### 1. Create Service User
```bash
sudo useradd --system --no-create-home --shell /bin/false meshcore
```

### 2. Create Directories
```bash
sudo mkdir -p /opt/meshcore-bot
sudo mkdir -p /var/log/meshcore-bot
```

### 3. Copy Bot Files
```bash
sudo cp -r . /opt/meshcore-bot/
sudo chown -R meshcore:meshcore /opt/meshcore-bot
sudo chown -R meshcore:meshcore /var/log/meshcore-bot
```

### 4. Install Service File
```bash
sudo cp meshcore-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable meshcore-bot
```

### 5. Install Dependencies
```bash
sudo pip3 install -r /opt/meshcore-bot/requirements.txt
```

## Service Management

### Start/Stop/Restart
```bash
sudo systemctl start meshcore-bot
sudo systemctl stop meshcore-bot
sudo systemctl restart meshcore-bot
```

### Check Status
```bash
sudo systemctl status meshcore-bot
```

### View Logs
```bash
# Real-time logs
sudo journalctl -u meshcore-bot -f

# Recent logs
sudo journalctl -u meshcore-bot -n 100

# Logs since boot
sudo journalctl -u meshcore-bot -b
```

### Enable/Disable Auto-start
```bash
sudo systemctl enable meshcore-bot    # Start on boot
sudo systemctl disable meshcore-bot   # Don't start on boot
```

## Configuration

The bot configuration is located at `/opt/meshcore-bot/config.ini`. Edit it with:

```bash
sudo nano /opt/meshcore-bot/config.ini
```

After changing configuration, restart the service:

```bash
sudo systemctl restart meshcore-bot
```

## Service Features

### Security
- Runs as dedicated `meshcore` user
- No shell access for service user
- Restricted file system access
- Resource limits (512MB RAM, 50% CPU)

### Reliability
- Automatic restart on failure
- Restart delay of 10 seconds
- Maximum 3 restart attempts per minute
- Logs to systemd journal

### Monitoring
- Systemd journal integration
- Status monitoring via systemctl
- Resource usage tracking

## Troubleshooting

### Service Won't Start
1. Check service status: `sudo systemctl status meshcore-bot`
2. View logs: `sudo journalctl -u meshcore-bot -n 50`
3. Check configuration: `sudo nano /opt/meshcore-bot/config.ini`
4. Verify dependencies: `sudo pip3 list | grep meshcore`

### Permission Issues
1. Check file ownership: `ls -la /opt/meshcore-bot/`
2. Fix permissions: `sudo chown -R meshcore:meshcore /opt/meshcore-bot`

### Connection Issues
1. Verify device connection (serial port, BLE, etc.)
2. Check device permissions for service user
3. Review connection settings in config.ini

### High Resource Usage
The service has built-in limits:
- Memory: 512MB maximum
- CPU: 50% maximum
- File descriptors: 65536 maximum

## Uninstallation

To completely remove the service:

```bash
sudo ./uninstall-service.sh
```

This will:
- Stop and disable the service
- Remove systemd service file
- Remove installation directory
- Remove log directory
- Remove service user

## File Locations

| Component | Location |
|-----------|----------|
| Service file | `/etc/systemd/system/meshcore-bot.service` |
| Bot files | `/opt/meshcore-bot/` |
| Configuration | `/opt/meshcore-bot/config.ini` |
| Logs | `/var/log/meshcore-bot/` (if configured) |
| System logs | `journalctl -u meshcore-bot` |

## Advanced Configuration

### Custom Installation Directory
Edit the service file to change the installation directory:

```bash
sudo nano /etc/systemd/system/meshcore-bot.service
```

Change the `WorkingDirectory` and `ExecStart` paths.

### Custom User
To use a different user, edit the service file and update the installation script.

### Environment Variables
Add environment variables to the service file:

```ini
[Service]
Environment=PYTHONPATH=/opt/meshcore-bot
Environment=DEBUG=true
Environment=CUSTOM_VAR=value
```

## Support

For issues with the service installation:
1. Check the logs: `sudo journalctl -u meshcore-bot -f`
2. Verify configuration: `sudo nano /opt/meshcore-bot/config.ini`
3. Test manually: `sudo -u meshcore python3 /opt/meshcore-bot/meshcore_bot.py`
