# Configuration

The bot is configured via `config.ini` in the project root (or the path given with `--config`). This page describes how configuration is organized and where to find command-specific options.

## config.ini structure

- **Sections** are named in square brackets, e.g. `[Bot]`, `[Connection]`, `[Path_Command]`.
- **Options** are `key = value` (or `key=value`). Comments start with `#` or `;`.
- **Paths** can be relative (to the directory containing the config file) or absolute. For Docker, use absolute paths under `/data/` (see [Docker deployment](docker.md)).

The main sections include:

| Section | Purpose |
|--------|---------|
| `[Bot]` | Bot name, database path, response toggles, command prefix |
| `[Connection]` | Serial, BLE, or TCP connection to the MeshCore device |
| `[Channels]` | Channels to monitor, DM behavior, optional channel keyword whitelist |
| `[Admin_ACL]` | Admin public keys and admin-only commands |
| `[Keywords]` | Keyword → response pairs |
| `[Weather]` | Units and settings shared by `wx` / `gwx` and Weather Service |
| `[Logging]` | Log file path and level |

## Channels section

`[Channels]` controls where the bot responds:

- **`monitor_channels`** – Comma-separated channel names. The bot only responds to messages on these channels (and in DMs if enabled).
- **`respond_to_dms`** – If `true`, the bot responds to direct messages; if `false`, it ignores DMs.
- **`channel_keywords`** – Optional. When set (comma-separated command/keyword names), only those triggers are answered **in channels**; DMs always get all triggers. Use this to reduce channel traffic by making heavy triggers (e.g. `wx`, `satpass`, `joke`) DM-only. Leave empty or omit to allow all triggers in monitored channels. Per-command **`channels = `** (empty) in a command’s section also forces that command to be DM-only; see `config.ini.example` for examples (e.g. `[Joke_Command]`).

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

- **`[Path_Command]`** – Path decoding and repeater selection. See [Path Command](path-command-config.md) for all options.
- **`[Prefix_Command]`** – Prefix lookup, prefix best, range limits.
- **`[Weather]`** – Used by the `wx` / `gwx` commands and the Weather Service plugin (see [Weather Service](weather-service.md)).
- **`[Airplanes_Command]`** – Aircraft/ADS-B command (API URL, radius, limits).
- **`[Aurora_Command]`** – Aurora command (default coordinates).
- **`[Alert_Command]`** – Emergency alerts (agency IDs, etc.).
- **`[Sports_Command]`** – Sports scores (teams, leagues).
- **`[Joke_Command]`**, **`[DadJoke_Command]`** – Joke sources and options.

Full reference: see `config.ini.example` in the repository for every section and option, with inline comments.

## Path Command configuration

The Path command has many options (presets, proximity, graph validation, etc.). All are documented in:

**[Path Command](path-command-config.md)** – Presets, geographic and graph settings, and tuning.

## Service plugin configuration

Service plugins (Discord Bridge, Packet Capture, Map Uploader, Weather Service) each have their own section and are documented under [Service Plugins](service-plugins.md).

## Config validation

Before starting the bot, you can validate section names and path writability. See [Config validation](config-validation.md) for how to run `validate_config.py` or `meshcore_bot.py --validate-config`, and what is checked (required sections, typos like `[WebViewer]` → `[Web_Viewer]`, and writable paths).

## Reloading configuration

Some configuration can be reloaded without restarting the bot using the **`reload`** command (admin only). Radio/connection settings are not changed by reload; restart the bot for those.
