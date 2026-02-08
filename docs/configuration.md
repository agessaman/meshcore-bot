# Configuration

The bot is configured via `config.ini` in the project root (or the path given with `--config`). This page describes how configuration is organized and where to find command-specific options.

## config.ini structure

- **Sections** are named in square brackets, e.g. `[Bot]`, `[Connection]`, `[Path_Command]`.
- **Options** are `key = value` (or `key=value`). Comments start with `#` or `;`.
- **Paths** can be relative (to the directory containing the config file) or absolute. For Docker, use absolute paths under `/data/` (see [Docker deployment](DOCKER.md)).

The main sections include:

| Section | Purpose |
|--------|---------|
| `[Bot]` | Bot name, database path, response toggles, command prefix |
| `[Connection]` | Serial, BLE, or TCP connection to the MeshCore device |
| `[Channels]` | Channels to monitor, DM behavior |
| `[Admin_ACL]` | Admin public keys and admin-only commands |
| `[Keywords]` | Keyword → response pairs |
| `[Weather]` | Units and settings shared by `wx` / `gwx` and Weather Service |
| `[Logging]` | Log file path and level |

## Command and feature sections

Many commands and features have their own section. Options there control whether the command is enabled and how it behaves.

### Enabling and disabling commands

- **`enabled`** – Common option to turn a command or plugin on or off. Example:
  ```ini
  [Aurora_Command]
  enabled = true
  ```
- Commands without an `enabled` key are typically always available (subject to [Admin_ACL](https://github.com/agessaman/meshcore-bot/blob/main/README.md) for admin-only commands).

### Command-specific sections

Examples of sections that configure specific commands or features:

- **`[Path_Command]`** – Path decoding and repeater selection. See [Path Command](PATH_COMMAND_CONFIG.md) for all options.
- **`[Prefix_Command]`** – Prefix lookup, prefix best, range limits.
- **`[Weather]`** – Used by the `wx` / `gwx` commands and the Weather Service plugin (see [Weather Service](WEATHER_SERVICE.md)).
- **`[Airplanes_Command]`** – Aircraft/ADS-B command (API URL, radius, limits).
- **`[Aurora_Command]`** – Aurora command (default coordinates).
- **`[Alert_Command]`** – Emergency alerts (agency IDs, etc.).
- **`[Sports_Command]`** – Sports scores (teams, leagues).
- **`[Joke_Command]`**, **`[DadJoke_Command]`** – Joke sources and options.

Full reference: see `config.ini.example` in the repository for every section and option, with inline comments.

## Path Command configuration

The Path command has many options (presets, proximity, graph validation, etc.). All are documented in:

**[Path Command](PATH_COMMAND_CONFIG.md)** – Presets, geographic and graph settings, and tuning.

## Service plugin configuration

Service plugins (Discord Bridge, Packet Capture, Map Uploader, Weather Service) each have their own section and are documented under [Service Plugins](service-plugins.md).

## Reloading configuration

Some configuration can be reloaded without restarting the bot using the **`reload`** command (admin only). Radio/connection settings are not changed by reload; restart the bot for those.
