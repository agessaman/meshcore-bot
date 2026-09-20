# MeshCore Bot Service Installation

This guide explains how to install the MeshCore Bot as a systemd service on Linux systems.

## Prerequisites

- Linux system with systemd
- Python 3.10+
- `rsync`
- Root/sudo access
- MeshCore-compatible device

You can install from a Debian package instead of the standalone script. See the
[README](https://github.com/agessaman/meshcore-bot#debian-package-deb) for build
instructions.

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
   sudo nano /etc/meshcore-bot/config.ini
   ```

4. **Start the service:**
   ```bash
   sudo systemctl start meshcore-bot
   ```

5. **Check status:**
   ```bash
   sudo systemctl status meshcore-bot
   ```

## Upgrading

After updating the source checkout, run:

```bash
sudo ./install-service.sh --upgrade
```

The upgrader rebuilds the virtual environment, updates the service unit, and migrates
legacy relative paths into the current service layout. It preserves the active
configuration, database, logs, local plugins, and installed-only alternative command
files. If an upgrade fails after stopping an active service, it makes one best-effort
attempt to restart that service while preserving the original failure status.

Read the [upgrade guide](upgrade.md) before upgrading an existing installation.

### Optional feature packages

Two feature sets are not installed by default because most bots do not need them:
the profanity filter (`better-profanity`, `unidecode`) and geocoding extras
(`pycountry`, `us`). A fresh install and a `--upgrade` both prompt for each one.

For an unattended run, install both without prompting:

```bash
sudo ./install-service.sh --upgrade --install-extras
```

`--install-extras` also works alongside `--update-venv`, which otherwise keeps
whatever is already in the virtual environment and does not prompt:

```bash
sudo ./install-service.sh -u --update-venv --install-extras
```

A failed extras install is non-fatal; the bot installs and starts without them.

## Manual Installation

If you prefer to install manually:

### 1. Create Service User

```bash
sudo useradd --system --no-create-home --shell /bin/false meshcore
```

### 2. Create Directories

```bash
sudo install -d -o root -g root -m 0755 /opt/meshcore-bot
sudo install -d -o meshcore -g meshcore -m 0700 /etc/meshcore-bot
sudo install -d -o meshcore -g meshcore -m 0700 /var/lib/meshcore-bot
sudo install -d -o meshcore -g meshcore -m 0750 /var/log/meshcore-bot
```

### 3. Copy Bot Files

This manual copy sequence is for a clean installation. Use the supported upgrader
above for an existing installation so installed-only alternative commands and runtime
state are preserved.

```bash
sudo rsync -a --delete \
  --exclude=.git --exclude=venv --exclude=.venv \
  --exclude=config.ini --exclude=local/ \
  ./ /opt/meshcore-bot/
sudo chown -R root:root /opt/meshcore-bot
sudo chmod -R go-w /opt/meshcore-bot
```

Keep custom plugins under `/var/lib/meshcore-bot/local/`; do not make the executable
tree writable by the service account.

### 4. Create the Virtual Environment

```bash
sudo python3 -m venv /opt/meshcore-bot/venv
sudo /opt/meshcore-bot/venv/bin/python -m pip install \
  -r /opt/meshcore-bot/requirements.txt
```

### 5. Install Configuration

```bash
sudo cp /opt/meshcore-bot/config.ini.example /etc/meshcore-bot/config.ini
sudo chown meshcore:meshcore /etc/meshcore-bot/config.ini
sudo chmod 0600 /etc/meshcore-bot/config.ini
```

Set these paths in the active configuration:

```ini
[Bot]
db_path = /var/lib/meshcore-bot/meshcore_bot.db
local_dir_path = /var/lib/meshcore-bot/local

[Logging]
log_file = /var/log/meshcore-bot/meshcore_bot.log
```

Leave `log_file` empty to use journald only (no log files). The installer and `.deb` package rewrite relative `log_file` values to `/var/log/meshcore-bot/`.

### 6. Install the Service File

```bash
sudo cp /opt/meshcore-bot/meshcore-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable meshcore-bot
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
# Real-time logs (always available — stdout/stderr go to journald)
sudo journalctl -u meshcore-bot -f

# Recent logs
sudo journalctl -u meshcore-bot -n 100

# Logs since boot
sudo journalctl -u meshcore-bot -b

# Optional file logs when [Logging] log_file is set (service default):
#   /var/log/meshcore-bot/meshcore_bot.log
#   /var/log/meshcore-bot/web_viewer.log
sudo tail -f /var/log/meshcore-bot/meshcore_bot.log
```

### Enable/Disable Auto-start
```bash
sudo systemctl enable meshcore-bot    # Start on boot
sudo systemctl disable meshcore-bot   # Don't start on boot
```

## Configuration

The active bot configuration is `/etc/meshcore-bot/config.ini`. Edit it with:

```bash
sudo nano /etc/meshcore-bot/config.ini
```

After changing configuration, you can reload in place (no process restart):

```bash
sudo systemctl reload meshcore-bot
```

Use restart when connection/radio settings changed (serial port, BLE target, TCP host/port, timeout):

```bash
sudo systemctl restart meshcore-bot
```

## Service Features

### Security

- Runs as dedicated `meshcore` user
- No shell access for service user
- Executable code and the virtual environment are root-owned
- Only configuration, state, and log directories are service-writable
- Resource limits (1GB RAM, up to two CPU cores)

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
3. Check configuration: `sudo nano /etc/meshcore-bot/config.ini`
4. Verify dependencies: `/opt/meshcore-bot/venv/bin/python -m pip list | grep meshcore`

### Dependency Import or Syntax Errors

Do not patch installed dependencies in place. Update the source checkout and rerun
`sudo ./install-service.sh --upgrade` so the installer creates a fresh virtual
environment from the current requirements.

### Permission Issues

1. Check file ownership: `ls -la /opt/meshcore-bot/`
2. Confirm code is root-owned and not writable by `meshcore`.
3. Confirm `/etc/meshcore-bot`, `/var/lib/meshcore-bot`, and
   `/var/log/meshcore-bot` are owned by `meshcore`.

### Connection Issues
1. Verify device connection (serial port, BLE, etc.)
2. Check device permissions for service user
3. Review connection settings in config.ini

### High Resource Usage
The service has built-in limits:
- Memory: 1GB maximum
- CPU: 200% maximum (up to two fully utilized CPU cores)
- File descriptors: 65536 maximum

These are ceilings, not reservations. The 1GB/200% baseline gives the bot and
its web-viewer child process enough headroom for graph loading and bounded
SQLite maintenance on Raspberry Pi 4-class systems without allowing them to
consume the whole host.

## Uninstallation

To completely remove the service:

```bash
sudo ./uninstall-service.sh
```

This will:
- Stop and disable the service
- Remove systemd service file
- Optionally remove the installation and log directories
- Optionally remove the service user

The current uninstaller preserves `/etc/meshcore-bot` and `/var/lib/meshcore-bot`.
Back up and remove those directories separately if you want to erase all configuration
and state.

## File Locations

| Component | Location |
|-----------|----------|
| Service file | `/etc/systemd/system/meshcore-bot.service` |
| Bot files | `/opt/meshcore-bot/` |
| Configuration | `/etc/meshcore-bot/config.ini` |
| Database and local plugins | `/var/lib/meshcore-bot/` |
| Logs | `/var/log/meshcore-bot/` |
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
2. Verify configuration: `sudo nano /etc/meshcore-bot/config.ini`
3. Test manually: `sudo -u meshcore /opt/meshcore-bot/venv/bin/python /opt/meshcore-bot/meshcore_bot.py --config /etc/meshcore-bot/config.ini`
