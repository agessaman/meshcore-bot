# MeshCore Bot

A Python bot that connects to MeshCore mesh networks via serial port, BLE, or TCP/IP. The bot responds to messages containing configured keywords, executes commands, and provides various data services including weather, solar conditions, and satellite pass information. A web viewer provides a browser-based dashboard for monitoring and managing the bot.


> [!CAUTION]
> Before installing this bot, please take a moment to _truly_ consider if your mesh needs another bot. If there are already several bots on your mesh, it is likely that you are adding congestion without adding value.
>
> It is not recommended to run more than one bot on a single channel. Adding another bot to a channel already in use on your mesh may result in users unnecessarily receiving double responses, depleting mesh airtime for little additional value.
>
> If you decide to run the bot on your mesh, please take advantage of regions or hop limits to ensure that your bot is helping your neighbors not flooding the entire mesh.

## Features

- **Connection Methods**: Serial port, BLE (Bluetooth Low Energy), or TCP/IP
- **Keyword Responses**: Configurable keyword-response pairs with template variables
- **Command System**: Plugin-based command architecture with built-in commands
- **Command Aliases**: Define shorthand aliases for any command via `aliases =` key in each command's config section
- **Rate Limiting**: Global, per-user (by pubkey or name), and per-channel rate limits to prevent spam
- **User Management**: Ban/unban users with persistent storage
- **Scheduled Messages**: Send messages at configured times
- **Direct Message Support**: Respond to private messages
- **Inbound Webhook**: Accept HTTP POST payloads and relay to MeshCore channels or DMs
- **Web Viewer**: Browser-based dashboard for monitoring contacts, mesh graph, radio settings, feeds, live packets, and logs
- **Radio Control**: Reboot or connect/disconnect the radio connection from the web viewer
- **Database Migrations**: Versioned schema migrations via `MigrationRunner` — safe upgrades across versions
- **DB Backup Scheduling**: Automated daily/weekly database backups with configurable retention
- **Nightly Maintenance Email**: Daily digest with uptime, network activity, DB stats, and error counts
- **Log Rotation**: Configurable log rotation via `[Logging]` section
- **Logging**: Console and file logging with configurable levels; optional structured JSON mode for log aggregation (Loki, Elasticsearch, Splunk)

### Service Plugins

- **Discord Bridge**: One-way webhook bridge to post mesh messages to Discord ([docs](docs/discord-bridge.md))
- **Telegram Bridge**: One-way bridge to post mesh messages to Telegram ([docs](docs/telegram-bridge.md))
- **Packet Capture**: Capture and publish packets to MQTT brokers ([docs](docs/packet-capture.md))
- **Map Uploader**: Upload node adverts to map.meshcore.dev ([docs](docs/map-uploader.md))
- **Weather Service**: Scheduled forecasts, alerts, and lightning detection ([docs](docs/weather-service.md))
- **Earthquake Service**: Scheduled USGS earthquake alerts for a configured region ([docs](docs/earthquake-service.md))
- **Repeater Prefix Collision Service**: Detect and alert on repeater prefix collisions ([docs](docs/repeater-prefix-collision-service.md))
- **MQTT Weather Relay**: Publish weather data from custom MQTT topics (configured via `MqttWeather` + `[Weather]`)
- **Webhook Service**: Accept inbound HTTP POST payloads and relay to channels or DMs

## Requirements

- Python 3.10+
- MeshCore-compatible device (Heltec V3, RAK Wireless, etc.)
- USB cable or BLE capability

## Installation

### Quick Start (Development)

1. Clone the repository:
```bash
git clone https://github.com/agessaman/meshcore-bot
cd meshcore-bot
```

2. Create a virtual environment and install dependencies via Makefile:
```bash
make dev          # creates .venv, installs all deps including test tools
```

Or for production dependencies only:
```bash
make install      # creates .venv, installs runtime + optional deps
```

3. Configure the bot:

**Interactive TUI (recommended):** Launch the ncurses config editor — it reads an existing `config.ini` or lets you start from `config.ini.example`:
```bash
make config
```

**Manual option (full config):** Enables all bot commands and provides all configuration options:
```bash
cp config.ini.example config.ini
# Edit config.ini with your settings
```

**Manual option (minimal config):** For users who only want core testing commands (ping, test, path, prefix, multitest):
```bash
cp config.ini.minimal-example config.ini
# Edit config.ini with your connection and bot settings
```

4. Run the bot:
```bash
.venv/bin/python meshcore_bot.py
```

5. Run tests and linting:
```bash
make test         # pytest with coverage
make test-no-cov  # pytest without coverage (faster)
make lint         # ruff check + mypy
make fix          # auto-fix ruff lint errors
```

### Production Installation (Systemd Service)
For production deployment as a system service:

1. Install as systemd service:
```bash
sudo ./install-service.sh
```

2. Configure the bot:
```bash
sudo nano /opt/meshcore-bot/config.ini
```

3. Start the service:
```bash
sudo systemctl start meshcore-bot
```

4. Check status:
```bash
sudo systemctl status meshcore-bot
```

See [Service installation](docs/service-installation.md) for detailed service installation instructions.

### Debian Package (.deb)

Build and install a `.deb` package for Debian/Ubuntu systems:

```bash
make deb
sudo dpkg -i dist/meshcore-bot_*.deb
```

The package installs the bot to `/opt/meshcore-bot/`, installs a systemd unit, and creates a `meshcore-bot` system user.

### Docker Deployment
For containerized deployment using Docker:

1. **Create data directories and configuration**:
   ```bash
   mkdir -p data/{config,databases,logs,backups}
   cp config.ini.example data/config/config.ini
   # Edit data/config/config.ini with your settings
   ```

2. **Update paths in config.ini** to use `/data/` directories:
   ```ini
   [Bot]
   db_path = /data/databases/meshcore_bot.db

   [Logging]
   log_file = /data/logs/meshcore_bot.log
   ```

3. **Build and start with Docker Compose**:
   ```bash
   docker compose up -d --build
   ```

4. **View logs**:
   ```bash
   docker compose logs -f
   ```

See [Docker deployment](docs/docker.md) for detailed Docker deployment instructions, including serial port access, web viewer configuration, and troubleshooting.

## NixOS
Use the Nix flake via flake.nix
```nix
meshcore-bot.url = "github:agessaman/meshcore-bot/";
```

And in your system config

```nix
{
  imports = [inputs.meshcore-bot.nixosModules.default];
  services.meshcore-bot = {
    enable = true;
    webviewer.enable = true;
    settings = {
      Connection.connection_type = "serial";
      Connection.serial_port = "/dev/ttyUSB0";
      Bot.bot_name = "MyBot";
    };
  };
}
```

## Web Viewer

The web viewer provides a browser-based dashboard for monitoring and managing the bot. Enable it in `config.ini`:

```ini
[Web_Viewer]
enabled = true
host = 0.0.0.0
port = 8080
web_viewer_password = yourpassword   # optional; omit to disable auth
```

Features:
- **Contacts** — live contact list with signal, path, and location data; star any contact; purge inactive contacts by age threshold; export to CSV/JSON
- **Mesh Graph** — interactive node graph of the mesh network
- **Radio Settings** — manage channels, reboot or connect/disconnect the radio
- **Feeds** — manage RSS/API feed subscriptions per channel
- **Packets** — raw packet monitor
- **Live Activity** — real-time color-coded packet/command/message feed with pause and clear controls
- **Live Channel Messages** — real-time channel message monitor via SocketIO
- **Logs** — real-time log viewer at `/logs`; level-based coloring, filter, pause, and clear

### Configuration Tab

The `/config` page exposes bot settings in-browser — no `config.ini` edit required.

**Email & Notifications** — configure SMTP and opt in to a nightly maintenance digest:

| Field | Description |
|-------|-------------|
| Server hostname | SMTP host (e.g. `smtp.gmail.com`) |
| Port | 587 (STARTTLS), 465 (SSL), or 25 (plain) |
| Security | STARTTLS / SSL / None |
| Username / Password | SMTP credentials (app-specific passwords recommended) |
| Sender display name | Name shown in the From field |
| Sender email | Address shown in the From field |
| Recipients | Comma-separated list of addresses |
| Nightly email toggle | Enable / disable the nightly maintenance digest |

All settings are stored in the bot database (`bot_metadata` table) and take effect immediately. Use **Send test email** to verify SMTP settings before enabling the digest.

When **Nightly maintenance email** is enabled, the scheduler sends a digest every 24 hours:

```
MeshCore Bot — Nightly Maintenance Report
============================================
Period : 2026-03-14 06:00 UTC → 2026-03-15 06:00 UTC

BOT STATUS
──────────────────────────────
  Uptime    : 2d 4h 32m
  Connected : yes

NETWORK ACTIVITY (past 24 h)
──────────────────────────────
  Active contacts  : 12
  New contacts     : 3
  Total tracked    : 47

DATABASE
──────────────────────────────
  Size : 14.2 MB
  Last retention run : 2026-03-15T06:00:00

ERRORS (past 24 h)
──────────────────────────────
  ERROR    : 2
  CRITICAL : 0

LOG FILES
──────────────────────────────
  Current : meshcore_bot.log (2.1 MB)
  Rotated : no
```

**Log Rotation** — configure via the Config tab or `[Logging]` in `config.ini`:

```ini
[Logging]
log_max_bytes = 5242880    # 5 MB per file
log_backup_count = 3       # number of rotated files to keep
```

**Database Backup** — schedule automatic backups from the Config tab:

```ini
[Maintenance]
db_backup_enabled = true
db_backup_schedule = daily      # daily | weekly | manual
db_backup_time = 02:00          # HH:MM local time
db_backup_retention_count = 7
db_backup_dir = /data/backups
```

The **Maintenance Status** card in the Config tab shows the last backup time, next scheduled run, and log rotation status.

### Radio Control

The Radio Settings page includes two control buttons next to the page heading:

- **Reboot Radio** — disconnects and reconnects the bot's radio connection (requires confirmation)
- **Connect / Disconnect** — toggles the bot's connection state; button color and label update live

These operations are queued via the database and processed by the bot's scheduler within ~5 seconds.

## Configuration

The bot uses `config.ini` for all settings. The quickest way to create and edit `config.ini` is:

```bash
make config
```

This launches an interactive ncurses TUI that lets you browse sections, edit values, validate, and save. It can also migrate from `config.ini.example` if no `config.ini` exists. In the keys pane, `r` renames the selected key (useful for changing a scheduled-message time), `a` adds a new key+value, and `d`/Delete removes a key.

Key configuration sections:

### Connection
```ini
[Connection]
connection_type = serial          # serial, ble, or tcp
serial_port = /dev/ttyUSB0        # Serial port path (for serial)
#hostname = 192.168.1.60         # TCP hostname/IP (for TCP)
#tcp_port = 5000                  # TCP port (for TCP)
#ble_device_name = MeshCore       # BLE device name (for BLE)
timeout = 30                      # Connection timeout
```

### Bot Settings
```ini
[Bot]
bot_name = MeshCoreBot            # Bot identification name
enabled = true                    # Enable/disable bot
rate_limit_seconds = 2            # Global: min seconds between any bot reply
bot_tx_rate_limit_seconds = 1.0   # Min seconds between bot transmissions
per_user_rate_limit_seconds = 30  # Per-user: min seconds between replies to same user (pubkey or name)
per_user_rate_limit_enabled = true
startup_advert = flood            # Send advert on startup
radio_probe_interval_seconds = 300   # probe interval in seconds (300–900 / 5–15 min)
radio_probe_fail_threshold = 3       # consecutive failures before zombie is declared and logged
send_timeout_seconds = 30            # max seconds to wait for a channel message send
radio_zombie_alert_enabled = false   # send immediate alert email on zombie detection (default: log only)
radio_zombie_alert_email =           # alert recipient(s); falls back to nightly email if blank
radio_offline_threshold = 3          # consecutive send timeouts before radio-offline state is entered
radio_offline_alert_enabled = true   # send alert email when radio-offline state is entered
radio_offline_alert_email =          # alert recipient(s); falls back to nightly email if blank
```

### Keywords
```ini
[Keywords]
# Format: keyword = response_template
# Variables: {sender}, {connection_info}, {snr}, {rssi}, {timestamp}, {path},
#            {hops}, {hops_label}, {elapsed}, {path_distance}, {firstlast_distance},
#            {total_contacts}, {total_repeaters}, {total_companions}, ...
test = "Message received from {sender} | {connection_info}"
help = "Bot Help: test, ping, help, hello, cmd, wx, aqi, sun, moon, solar, hfcond, satpass, dice, roll, joke, dadjoke, sports, channels, path, prefix, repeater, stats, alert"
```

### Channels
```ini
[Channels]
monitor_channels = general,test,emergency  # Channels to monitor
respond_to_dms = true                      # Enable DM responses
# Optional: limit channel responses to certain keywords (DM gets all triggers)
# channel_keywords = help,ping,test,hello
```

### Per-Channel Rate Limiting
```ini
[Rate_Limits]
# Format: channel.<name>_seconds = <float>
# Overrides the global rate_limit_seconds for a specific channel.
channel.general_seconds = 5.0
channel.emergency_seconds = 0.0   # no rate limit on emergency channel
```

### Command Aliases

Add an `aliases =` key to any command's config section. The value is a
comma-separated list of extra keywords that trigger the same command.

```ini
[Ping_Command]
aliases = p,ping-test

[WX_Command]
aliases = w,weather
```

### Inbound Webhook
```ini
[Webhook]
enabled = false
host = 0.0.0.0
port = 8765
secret_token =            # Bearer token; leave blank to disable auth
max_message_length = 200
# allowed_channels =      # Comma-separated allowlist; blank = all channels
```

Send a message to a channel:
```bash
curl -X POST http://localhost:8765/webhook \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"channel": "general", "message": "Hello from webhook!"}'
```

Send a DM:
```bash
curl -X POST http://localhost:8765/webhook \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"dm_to": "NodeName", "message": "Private message"}'
```

### External Data APIs
```ini
[External_Data]
# API keys for external services
n2yo_api_key =                    # Satellite pass data
airnow_api_key =                  # Air quality data
```

### Alert Command
```ini
[Alert_Command]
enabled = true                           # Enable/disable alert command
max_incident_age_hours = 24             # Maximum age for incidents (hours)
max_distance_km = 20.0                  # Maximum distance for proximity queries (km)
agency.city.<city_name> = <agency_ids>   # City-specific agency IDs (e.g., agency.city.seattle = 17D20,17M15)
agency.county.<county_name> = <agency_ids> # County-specific agency IDs (aggregates all city agencies)
```

### Logging
```ini
[Logging]
log_level = INFO                  # DEBUG, INFO, WARNING, ERROR, CRITICAL
log_file = meshcore_bot.log       # Log file path (empty = console only)
colored_output = true             # Enable colored console output
json_logging = false              # Emit one JSON object per log line (for Loki/Elasticsearch/Splunk)
                                  # When true, colored_output is ignored
log_max_bytes = 5242880           # Max log file size before rotation (bytes; default 5 MB)
log_backup_count = 3              # Number of rotated log files to keep
```

When `json_logging = true` each log line is a JSON object:
```json
{"timestamp":"2026-03-14T12:00:00.000Z","level":"INFO","logger":"MeshCoreBot","message":"Connected to radio"}
```

### Maintenance
```ini
[Maintenance]
db_backup_enabled = false
db_backup_schedule = daily      # daily | weekly | manual
db_backup_time = 02:00          # HH:MM local time
db_backup_retention_count = 7
db_backup_dir = /data/backups
email_attach_log = false        # attach current log file (≤ 5 MB) to nightly email before rotation
```

### Notifications
```ini
[Notifications]
email_enabled = false
smtp_host =
smtp_port = 587
smtp_user =
smtp_password =
smtp_from =
email_recipients =              # comma-separated
email_send_time = 06:00         # nightly digest send time (HH:MM local)
```

## Usage

### Running the Bot

```bash
.venv/bin/python meshcore_bot.py
```

Or if installed as a package entry point:
```bash
.venv/bin/meshcore-bot
```

### Available Commands

For a comprehensive list of all available commands with examples and detailed explanations, see [Command reference](docs/command-reference.md).

Quick reference:
 - **Basic:** `test`, `ping`, `version`, `help`, `hello`, `cmd`
- **Information:** `wx`, `gwx`, `aqi`, `sun`, `moon`, `solar`, `solarforecast`, `hfcond`, `satpass`, `channels`
- **Emergency:** `alert`
- **Gaming:** `dice`, `roll`, `magic8`
- **Entertainment:** `joke`, `dadjoke`, `hacker`, `catfact`
- **Sports:** `sports`
- **MeshCore Utility:** `path`, `prefix`, `stats`, `multitest`, `webviewer`
- **Management (DM only):** `repeater`, `advert`, `feed`, `announcements`, `greeter`, `schedule`

### `schedule` Command (DM only)

The `!schedule` command (DM only by default) shows all upcoming scheduled messages and the current advert interval:

```
Scheduled Messages (2 configured):
  06:00 → #general: "Good morning, mesh!"
  18:00 → #general: "Evening check-in"
Advert interval: every 30 min
```

## Message Response Templates

Keyword responses support these template variables:

- `{sender}` - Sender's node ID
- `{connection_info}` - Connection details (path | SNR | RSSI)
- `{snr}` - Signal-to-noise ratio in dB
- `{rssi}` - Received signal strength in dBm
- `{timestamp}` - Current time (HH:MM:SS in configured timezone)
- `{elapsed}` - Time since message was sent
- `{path}` - Message routing path
- `{hops}` - Hop count (integer or `?`)
- `{hops_label}` - Hop count with label (`"1 hop"`, `"3 hops"`, `"?"`)
- `{path_distance}` - Estimated total path distance in km
- `{firstlast_distance}` - First-to-last repeater distance in km
- `{total_contacts}`, `{total_repeaters}`, `{total_companions}` - Mesh network counts (for scheduled messages)

### Adding Newlines

To add newlines in keyword responses, use `\n` (single backslash + n):

```ini
[Keywords]
test = "Line 1\nLine 2\nLine 3"
```

This will output:
```
Line 1
Line 2
Line 3
```

To use a literal backslash + n, use `\\n` (double backslash + n).
Other escape sequences: `\t` (tab), `\r` (carriage return), `\\` (literal backslash)

Example:
```ini
[Keywords]
test = "Message received from {sender} | {connection_info}"
ping = "Pong!"
help = "Bot Help: test, ping, help, hello, cmd, wx, gwx, aqi, sun, moon, solar, solarforecast, hfcond, satpass, dice, roll, joke, dadjoke, sports, channels, path, prefix, repeater, stats, multitest, alert, webviewer"
```

## Hardware Setup

### Serial Connection

1. Flash MeshCore firmware to your device
2. Connect via USB
3. Configure serial port in `config.ini`:
   ```ini
   [Connection]
   connection_type = serial
   serial_port = /dev/ttyUSB0  # Linux
   # serial_port = COM3        # Windows
   # serial_port = /dev/tty.usbserial-*  # macOS
   ```

### BLE Connection

1. Ensure your MeshCore device supports BLE
2. Configure BLE in `config.ini`:
   ```ini
   [Connection]
   connection_type = ble
   ble_device_name = MeshCore
   ```

### TCP Connection

1. Ensure your MeshCore device has TCP/IP connectivity (e.g., via gateway or bridge)
2. Configure TCP in `config.ini`:
   ```ini
   [Connection]
   connection_type = tcp
   hostname = 192.168.1.60  # IP address or hostname
   tcp_port = 5000          # TCP port (default: 5000)
   ```

## Troubleshooting

### Common Issues

1. **Serial Port Not Found**:
   - Check device connection
   - Verify port name in config
   - List available ports: `python -c "import serial.tools.list_ports; print([p.device for p in serial.tools.list_ports.comports()])"`

2. **BLE Connection Issues**:
   - Ensure device is discoverable
   - Check device name in config
   - Verify BLE permissions

3. **TCP Connection Issues**:
   - Verify hostname/IP address is correct
   - Check that TCP port is open and accessible
   - Ensure network connectivity to the device
   - Verify the MeshCore device supports TCP connections
   - Check firewall settings if connection fails

4. **Message Parsing Errors**:
   - Enable DEBUG logging for detailed information
   - Check meshcore library documentation for protocol details

5. **Rate Limiting**:
   - **Global**: `rate_limit_seconds` — minimum time between any two bot replies
   - **Per-user**: `per_user_rate_limit_seconds` and `per_user_rate_limit_enabled` — minimum time between replies to the same user (user identified by public key when available, else sender name)
   - **Per-channel**: `[Rate_Limits] channel.<name>_seconds` — override rate limit for a specific channel
   - **Bot TX**: `bot_tx_rate_limit_seconds` — minimum time between bot transmissions on the mesh
   - Check logs for rate limiting messages

### Debug Mode

Enable debug logging:
```ini
[Logging]
log_level = DEBUG
```

## Architecture

The bot uses a modular plugin architecture:

- **Core modules** (`modules/`): Shared utilities and core functionality
- **Command plugins** (`modules/commands/`): Individual command implementations
- **Service plugins** (`modules/service_plugins/`): Background services (Discord/Telegram bridges, webhook, packet capture, etc.)
- **Web viewer** (`modules/web_viewer/`): Flask + SocketIO browser dashboard
- **Plugin loaders**: Dynamic discovery and loading of command and service plugins
- **Message handler**: Processes incoming messages and routes to appropriate handlers
- **Scheduler**: Background thread for timed tasks; dispatches async ops into the bot's event loop
- **Database migrations**: `modules/db_migrations.py` — versioned `MigrationRunner` applied once on startup

### Adding New Plugins

**Command Plugin:**
1. Create a new file in `modules/commands/`
2. Inherit from `BaseCommand`
3. Implement the `execute()` method
4. The plugin loader will automatically discover and load it

```python
from .base_command import BaseCommand
from ..models import MeshMessage

class MyCommand(BaseCommand):
    name = "mycommand"
    keywords = ['mycommand']
    description = "My custom command"

    async def execute(self, message: MeshMessage) -> bool:
        await self.send_response(message, "Hello from my command!")
        return True
```

**Service Plugin:**
1. Create a new file in `modules/service_plugins/`
2. Inherit from `BaseServicePlugin`, set `config_section = 'My_Section'`
3. Implement `async start()` and `async stop()` methods
4. Add `[My_Section] enabled = true` to `config.ini.example`

**Database Migration:**
1. Write `_mNNNN_short_desc(cursor)` in `modules/db_migrations.py`
2. Append `(NNNN, "description", _mNNNN_...)` to `MIGRATIONS`
3. Never modify or remove existing migrations — add a new one instead

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request against the dev branch

## License

This project is licensed under the MIT License.

## Acknowledgments

- [MeshCore Project](https://github.com/meshcore-dev/MeshCore) for the mesh networking protocol
- Some commands adapted from MeshingAround bot by K7MHI Kelly Keeton 2024
- Packet capture service based on [meshcore-packet-capture](https://github.com/agessaman/meshcore-packet-capture) by agessaman
- [meshcore-decoder](https://github.com/michaelhart/meshcore-decoder) by Michael Hart for client-side packet decoding and decryption in the web viewer
