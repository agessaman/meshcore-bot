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

### Service Plugins

- **Discord Bridge**: One-way webhook bridge to post mesh messages to Discord ([docs](docs/DISCORD_BRIDGE.md))
- **Packet Capture**: Capture and publish packets to MQTT brokers ([docs](docs/PACKET_CAPTURE.md))
- **Map Uploader**: Upload node adverts to map.meshcore.dev ([docs](docs/MAP_UPLOADER.md))
- **Weather Service**: Scheduled forecasts, alerts, and lightning detection ([docs](docs/WEATHER_SERVICE.md))

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

For a comprehensive list of all available commands with examples and detailed explanations, see [COMMANDS.md](COMMANDS.md).

Quick reference:
- **Basic:** `test`, `ping`, `help`, `hello`, `cmd`
- **Information:** `wx`, `gwx`, `aqi`, `sun`, `moon`, `solar`, `solarforecast`, `hfcond`, `satpass`, `channels`
- **Emergency:** `alert`
- **Gaming:** `dice`, `roll`, `magic8`
- **Entertainment:** `joke`, `dadjoke`, `hacker`, `catfact`
- **Sports:** `sports`
- **MeshCore Utility:** `path`, `prefix`, `stats`, `multitest`, `webviewer`
- **Management (DM only):** `repeater`, `advert`, `feed`, `announcements`, `greeter`

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
- **Service plugins** (`modules/service_plugins/`): Background services (Discord bridge, packet capture, etc.)
- **Plugin loaders**: Dynamic discovery and loading of command and service plugins
- **Message handler**: Processes incoming messages and routes to appropriate handlers

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
2. Inherit from `BaseServicePlugin`
3. Implement `start()` and `stop()` methods
4. Add configuration section to `config.ini.example`

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
