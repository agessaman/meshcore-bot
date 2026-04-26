# Service Plugins

Service plugins extend the bot with background services that run alongside the main message loop. Each can be enabled or disabled in `config.ini`.

| Plugin | Description |
|--------|-------------|
| [Discord Bridge](discord-bridge.md) | One-way webhook bridge to post mesh messages to Discord channels |
| [Telegram Bridge](telegram-bridge.md) | One-way bridge to post mesh messages to Telegram chats/channels |
| [Packet Capture](packet-capture.md) | Capture packets from the mesh and publish them to MQTT brokers |
| [Map Uploader](map-uploader.md) | Upload node advertisements to [map.meshcore.dev](https://map.meshcore.dev) for network visualization |
| [Weather Service](weather-service.md) | Scheduled weather forecasts, weather alerts, and lightning detection |
| MQTT Weather Relay (`MqttWeather`) | Relay weather data to custom MQTT topics configured under `[Weather]` |
| Webhook Service (`Webhook`) | Accept inbound HTTP POST payloads and relay to mesh channels or DMs |
| [Earthquake Service](earthquake-service.md) | Earthquake alerts for a configured region (USGS API, defaults: California) |
| [Repeater Prefix Collision Service](repeater-prefix-collision-service.md) | Alerts when a newly heard repeater prefix collides with an existing repeater prefix |

## Enabling a plugin

1. Add or edit the plugin's section in `config.ini` (see each plugin's doc for options).
2. Set `enabled = true` for that plugin.
3. Restart the bot.

Some plugins require additional configuration (API keys, webhook URLs, etc.) before they will run successfully.
