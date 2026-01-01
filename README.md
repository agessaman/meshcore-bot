# MeshCore Bot

A Python bot that connects to MeshCore mesh networks via serial port, BLE, or TCP/IP. The bot responds to messages containing configured keywords, executes commands, and provides various data services including weather, solar conditions, and satellite pass information.

## Features

- **Connection Methods**: Serial port, BLE (Bluetooth Low Energy), or TCP/IP
- **Keyword Responses**: Configurable keyword-response pairs with template variables
- **Command System**: Plugin-based command architecture with built-in commands
- **Rate Limiting**: Configurable rate limiting to prevent network spam
- **User Management**: Ban/unban users with persistent storage
- **Scheduled Messages**: Send messages at configured times
- **Direct Message Support**: Respond to private messages
- **Logging**: Console and file logging with configurable levels

## Requirements

- Python 3.7+
- MeshCore-compatible device (Heltec V3, RAK Wireless, etc.)
- USB cable or BLE capability

## Installation

### Quick Start (Development)
1. Clone the repository:
```bash
git clone <repository-url>
cd meshcore-bot
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Copy and configure the bot:
```bash
cp config.ini.example config.ini
# Edit config.ini with your settings
```

4. Run the bot:
```bash
python3 meshcore_bot.py
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

See [SERVICE-INSTALLATION.md](SERVICE-INSTALLATION.md) for detailed service installation instructions.

### Docker Installation

Run the bot in a Docker container for easy deployment on home servers, NAS systems, or cloud environments.

**Quick Start:**
```bash
# 1. Copy and configure
cp config.ini.example config.ini
nano config.ini  # Edit your settings

# 2. Create data directories
mkdir -p data logs

# 3. Build and run
docker-compose build
docker-compose up -d

# 4. View logs
docker-compose logs -f
```

**Serial Device (Linux/NAS):**
```bash
# Edit docker-compose.yml and uncomment the devices section:
# devices:
#   - "/dev/ttyUSB0:/dev/ttyUSB0"
# Then run:
docker-compose up -d
```

**TCP Connection:**
```bash
MESHCORE_CONNECTION_TYPE=tcp \
MESHCORE_TCP_HOST=192.168.1.100 \
docker-compose up -d
```

**Environment Variables:**
| Variable | Description | Default |
|----------|-------------|---------|
| `MESHCORE_CONNECTION_TYPE` | serial, tcp, or ble | serial |
| `MESHCORE_SERIAL_PORT` | Serial device path | /dev/ttyUSB0 |
| `MESHCORE_TCP_HOST` | TCP hostname/IP | - |
| `MESHCORE_TCP_PORT` | TCP port | 5000 |
| `MESHCORE_WEB_PORT` | Web viewer port | 8080 |
| `TZ` | Timezone | UTC |

**Web Viewer:** Access at http://localhost:8080

See [DOCKER.md](DOCKER.md) for complete Docker documentation including NAS deployment, BLE setup, and troubleshooting.

## Configuration

The bot uses `config.ini` for all settings. Key configuration sections:

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
rate_limit_seconds = 2            # Rate limiting interval
startup_advert = flood            # Send advert on startup
```

### Keywords
```ini
[Keywords]
# Format: keyword = response_template
# Variables: {sender}, {connection_info}, {snr}, {timestamp}, {path}
test = "Message received from {sender} | {connection_info}"
help = "Bot Help: test, ping, help, hello, cmd, wx, aqi, sun, moon, solar, hfcond, satpass, dice, roll, joke, dadjoke, sports, channels, path, prefix, repeater, stats, alert"
```

### Channels
```ini
[Channels]
monitor_channels = general,test,emergency  # Channels to monitor
respond_to_dms = true                      # Enable DM responses
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
alert_enabled = true                    # Enable/disable alert command
max_incident_age_hours = 24             # Maximum age for incidents (hours)
max_distance_km = 20.0                  # Maximum distance for proximity queries (km)
agency.city.<city_name> = <agency_ids>   # City-specific agency IDs (e.g., agency.city.seattle = 17D20,17M15)
agency.county.<county_name> = <agency_ids> # County-specific agency IDs (aggregates all city agencies)
```

### Logging
```ini
[Logging]
log_level = INFO                  # DEBUG, INFO, WARNING, ERROR, CRITICAL
log_file = meshcore_bot.log       # Log file path
colored_output = true             # Enable colored console output
```

## Usage

### Running the Bot

```bash
python meshcore_bot.py
```


### Available Commands

The bot responds to these commands:

**Basic Commands:**
- `test` or `t` - Test message response (can include optional phrase: `test <phrase>`)
- `ping` - Ping/pong response
- `help` - Show available commands (use `help <command>` for command details)
- `hello` - Greeting response (also responds to: hi, hey, howdy, greetings, etc.)
- `cmd` - List available commands in compact format

**Information Commands:**
- `channels` - List hashtag channels (use `channels` for general, `channels list` for all categories, `channels <category>` for specific categories, `channels #channel` for specific channel info)
- `wx <zipcode>` - Weather information for US zip code (also: `weather`, `wxa`, `wxalert`)
- `gwx <location>` - Global weather for any location worldwide (also: `globalweather`, `gwxa`)
- `aqi <location>` - Air quality index (usage: `aqi seattle`, `aqi greenwood`, `aqi vancouver canada`, `aqi 47.6,-122.3`, or `aqi help`)
- `sun` - Sunrise/sunset times
- `moon` - Moon phase and times
- `solar` - Solar conditions and HF band status
- `solarforecast` or `sf` - Solar panel production forecast (usage: `sf <location|repeater_name|coordinates|zipcode> [panel_size] [azimuth, 0=south] [angle]`)
- `hfcond` - HF band conditions
- `satpass <NORAD>` - Satellite pass information (default: radio passes, all passes above horizon)
- `satpass <NORAD> visual` - Visual passes only (must be visually observable)
- `satpass <shortcut>` - Use shortcuts like `iss`, `hst`, `hubble`, `goes18`, `tiangong`

**Emergency Commands:**
- `alert <city|zipcode|street city|lat,lon|county> [all]` - Get active emergency incidents (e.g., `alert seattle`, `alert 98101`, `alert seattle all`)

**Gaming Commands:**
- `dice` - Roll dice (d6 by default, or specify like `dice d20`, `dice 2d6`)
- `roll` - Roll random number (1-100 by default, or specify like `roll 50`)

**Entertainment Commands:**
- `joke` - Get a random joke (use `joke [category]` for specific category)
- `dadjoke` - Get a dad joke from icanhazdadjoke.com
- `hacker` - Responds to Linux commands (`sudo`, `ps aux`, `grep`, `ls -l`, etc.) with supervillain mainframe errors

**Sports Commands:**
- `sports` - Get scores for default teams
- `sports <team>` - Get scores for specific team
- `sports <league>` - Get scores for league (nfl, mlb, nba, etc.)

**MeshCore Utility Commands:**
- `path` or `decode` or `route` - Decode message routing path
- `prefix <XX>` - Look up repeaters by two-character prefix (e.g., `prefix 1A`)
  - `prefix refresh` - Refresh prefix cache
  - `prefix free` or `prefix available` - Show available prefixes
  - `prefix <XX> all` - Include all repeaters (not just active)
- `stats` - Show bot usage statistics for past 24 hours
  - `stats messages` - Message statistics
  - `stats channels` - Channel statistics
  - `stats paths` - Path statistics
- `multitest` or `mt` - Listens for 6 seconds and collects all unique paths from incoming messages

**Management Commands (DM only):**
- `repeater` or `repeaters` or `rp` - Manage repeater contacts (DM only, requires ACL permissions)
  - `repeater scan` - Scan and catalog new repeaters
  - `repeater list` - List repeater contacts (use `--all` to show purged ones)
  - `repeater purge <days>` - Purge repeaters older than specified days
  - `repeater purge <name>` - Purge specific repeater by name
  - `repeater purge all` - Purge all repeaters
  - `repeater restore <name>` - Restore a previously purged repeater
  - `repeater stats` - Show repeater management statistics
  - `repeater status` - Show contact list status and limits
  - `repeater manage` - Auto-manage contact list (use `--dry-run` to preview)
  - See `help repeater` for full list of subcommands
- `advert` - Send network flood advert (DM only, 1hr cooldown)

## Message Response Templates

Keyword responses support these template variables:

- `{sender}` - Sender's node ID
- `{connection_info}` - Connection details (direct/routed)
- `{snr}` - Signal-to-noise ratio
- `{timestamp}` - Message timestamp
- `{path}` - Message routing path

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
   - Adjust `rate_limit_seconds` in config
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
- **Plugin loader**: Dynamic discovery and loading of command plugins
- **Message handler**: Processes incoming messages and routes to appropriate handlers

### Adding New Commands

1. Create a new command file in `modules/commands/`
2. Inherit from `BaseCommand`
3. Implement the `execute()` method
4. The plugin loader will automatically discover and load the command

Example:
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

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## License

This project is licensed under the MIT License.

## Acknowledgments

- [MeshCore Project](https://github.com/meshcore-dev/MeshCore) for the mesh networking protocol
- Some commands adapted from MeshingAround bot by K7MHI Kelly Keeton 2024
