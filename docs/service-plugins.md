# Service Plugins

Service plugins extend the bot with background services that run alongside the main message loop. Each can be enabled or disabled in `config.ini`.

| Plugin | Description |
|--------|-------------|
| [Discord Bridge](DISCORD_BRIDGE.md) | One-way webhook bridge to post mesh messages to Discord channels |
| [Packet Capture](PACKET_CAPTURE.md) | Capture packets from the mesh and publish them to MQTT brokers |
| [Map Uploader](MAP_UPLOADER.md) | Upload node advertisements to [map.meshcore.dev](https://map.meshcore.dev) for network visualization |
| [Weather Service](WEATHER_SERVICE.md) | Scheduled weather forecasts, weather alerts, and lightning detection |

## Enabling a plugin

1. Add or edit the plugin's section in `config.ini` (see each plugin's doc for options).
2. Set `enabled = true` for that plugin.
3. Restart the bot.

Some plugins require additional configuration (API keys, webhook URLs, etc.) before they will run successfully.
