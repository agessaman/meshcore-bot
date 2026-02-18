# Getting Started

Get meshcore-bot running on your machine in a few minutes.

## Requirements

- **Python 3.7+**
- **MeshCore-compatible device** (Heltec V3, RAK Wireless, etc.)
- **Connection**: USB cable, BLE, or TCP/IP to the device

## Quick start (development)

1. **Clone and install**

   ```bash
   git clone https://github.com/agessaman/meshcore-bot.git
   cd meshcore-bot
   pip install -r requirements.txt
   ```

2. **Configure**

   Copy an example config and edit with your connection and bot settings:

   - **Full config** (all commands and options):
     ```bash
     cp config.ini.example config.ini
     ```
   - **Minimal config** (core commands only: ping, test, path, prefix, multitest):
     ```bash
     cp config.ini.minimal-example config.ini
     ```

   Edit `config.ini`: set at least `[Connection]` (serial/BLE/TCP) and `[Bot]` (e.g. `bot_name`).

3. **Run**

   ```bash
   python3 meshcore_bot.py
   ```

## Production deployment

### Systemd service

Run the bot as a system service on Linux:

```bash
sudo ./install-service.sh
sudo nano /opt/meshcore-bot/config.ini   # configure
sudo systemctl start meshcore-bot
sudo systemctl status meshcore-bot
```

See [Service installation](service-installation.md) for full steps.

### Docker

Run in a container with Docker Compose:

```bash
mkdir -p data/{config,databases,logs,backups}
cp config.ini.example data/config/config.ini
# Edit data/config/config.ini and set paths to /data/... (see [Docker](docker.md))
docker compose up -d --build
```

See [Docker deployment](docker.md) for paths, serial access, and troubleshooting.

### NixOS

Use the flake:

```nix
meshcore-bot.url = "github:agessaman/meshcore-bot/";
```

## Next steps

- **[Command Reference](command-reference.md)** — Full command reference (wx, aqi, sun, path, prefix, etc.)
- **[README](https://github.com/agessaman/meshcore-bot/blob/main/README.md)** — Features, keywords, configuration overview
- **Guides** (sidebar) — Path command, repeater commands, feeds, weather service, Discord bridge, map uploader, packet capture
