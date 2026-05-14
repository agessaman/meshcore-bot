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

### Logging and log rotation

- **Startup (config.ini):** Under `[Logging]`, `log_file`, `log_max_bytes`, and `log_backup_count` are read when the bot starts. They control the initial `RotatingFileHandler` for the bot log file (see `config.ini.example`).

- **Live changes (web viewer):** The Config tab can store **`maint.log_max_bytes`** and **`maint.log_backup_count`** in the database (`bot_metadata`). The scheduler’s maintenance loop applies those values to the existing rotating file handler **without restarting** the bot—**but only after** you save rotation settings from the web UI (which writes the metadata keys). Editing `config.ini` alone does not update `bot_metadata`, so hot-apply will not see a change until you save from the viewer (or set the keys another way).

If you rely on config-file-only workflows, restart the bot after changing `[Logging]` rotation options.

## Channels section

`[Channels]` controls where the bot responds:

- **`monitor_channels`** – Comma-separated channel names. The bot only responds to messages on these channels (and in DMs if enabled).
- **`respond_to_dms`** – If `true`, the bot responds to direct messages; if `false`, it ignores DMs.
- **`channel_keywords`** – Optional. When set (comma-separated command/keyword names), only those triggers are answered **in channels**; DMs always get all triggers. Use this to reduce channel traffic by making heavy triggers (e.g. `wx`, `satpass`, `joke`) DM-only. Leave empty or omit to allow all triggers in monitored channels. Per-command **`channels = `** (empty) in a command’s section also forces that command to be DM-only; see `config.ini.example` for examples (e.g. `[Joke_Command]`).
- **`max_response_hops`** - Default: 64 (code fallback); 7 in the shipped config templates. The bot will ignore messages that have traveled more than this number of hops. A value at or below 10 is recommended — in most meshes, anything higher is almost never an intentional message meant to trigger this bot, so lowering it keeps the bot from amplifying long flood traffic (#161).
- **`outgoing_flood_scope_override`** – Optional. Overrides the scope the bot uses for all outbound channel message sends. When **not set** (default), the bot automatically mirrors the scope of each incoming TC_FLOOD message: a reply to a `#west`-scoped message is sent with `#west` scope, and a reply to a plain (unscoped) FLOOD message is sent as classic global flood. When **set** to a region name like `#west`, the bot always uses that fixed scope for every outbound send, ignoring the incoming message's scope.
- **`flood_scopes`** – Optional. Comma-separated list of named scopes the bot will **accept and reply to**. When set, this acts as an allowlist: only TC_FLOOD messages matching one of these scopes receive a reply, and the reply is sent using the same scope as the incoming message (auto-mirror). Regular (unscoped) FLOOD messages are blocked unless `*` is included in the list. Leave empty or omit to accept all messages regardless of scope.

### outgoing_flood_scope_override vs flood_scopes

These two options are independent and serve different purposes:

| Option | Controls |
|--------|----------|
| `outgoing_flood_scope_override` | What scope the bot *sends replies with* (fixed outbound override; omit for auto-mirror) |
| `flood_scopes` | Which incoming scopes the bot *accepts* (allowlist + per-message scope mirroring) |

**Example — auto-mirror incoming scope (default, no override needed):**
```ini
flood_scopes = #west, #east
```
Only TC_FLOOD messages scoped to `#west` or `#east` receive a reply; unscoped FLOOD is silently ignored. Replies automatically use the same scope as the incoming message (`#west` → reply with `#west`, etc.).

**Example — accept specific regions plus unscoped FLOOD:**
```ini
flood_scopes = #west, #east, *
```
Same as above, but `*` opts in to also accepting regular (unscoped) FLOOD messages.

**Example — fixed outbound scope regardless of incoming scope:**
```ini
outgoing_flood_scope_override = #west
```
The bot always sends replies using the `#west` scope. All incoming messages (scoped or not) are accepted.

**Example — fixed outbound scope, restricted to a matching inbound scope:**
```ini
outgoing_flood_scope_override = #west
flood_scopes                  = #west
```

### Public channel guard

The bot **refuses to start** if `monitor_channels` includes the Public channel, unless an explicit override key is set in `[Bot]`. This prevents accidental bot deployments on the shared channel that is visible to all mesh users by default.

If you genuinely intend to run the bot on Public, add to `[Bot]`:

```ini
i_understand_that_running_the_bot_on_the_public_channel_is_potentially_disruptive_to_other_users_enjoyment_of_the_mesh_and_i_would_like_to_do_it_anyway = true
```

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
- **`[Test_Command]`** – `test` / `t` behavior. Optional **`response_format`** overrides the legacy **`[Keywords] test`** string. Templates support the same placeholders as Keywords, plus **feed-style pipe filters** on placeholders (e.g. `{path_distance|pathbytes_min:2}`) implemented in `modules/response_template.py`—see comments under `[Test_Command]` in `config.ini.example`.
- **`[Prefix_Command]`** – Prefix lookup, prefix best, range limits.
- **`[Cmd_Command]`** – `cmd` behavior. Set `cmd_reference_url` to return `Full command reference: <url>` instead of the generated compact command list.
- **`[Weather]`** – Used by the `wx` / `gwx` commands and the Weather Service plugin (see [Weather Service](weather-service.md)).
- **`[Airplanes_Command]`** – Aircraft/ADS-B command (API URL, radius, limits).
- **`[Aurora_Command]`** – Aurora command (default coordinates).
- **`[Alert_Command]`** – Emergency alerts (agency IDs, etc.).
- **`[Sports_Command]`** – Sports scores (teams, leagues).
- **`[Joke_Command]`**, **`[DadJoke_Command]`** – Joke sources and options.
- **`[RandomLine]`** – Trigger-based random-line responses via `triggers.<key>`, `file.<key>`, optional `prefix.<key>`, optional channel restriction (`channel.<key>`/`channels.<key>`), and optional website category override (`category.<key>`). Website command reference groups RandomLine entries under **Fun Commands** by default unless `category.<key>` is set.

Common per-command options (when supported by that command):

- **`channels`** – Restrict where that command runs in channels:
  - Omit key: follow global `[Channels] monitor_channels`
  - Empty (`channels =`): DM-only
  - Comma list: only those channels
- **`aliases`** – Extra trigger words for that command, comma-separated **stems only** (e.g. `aliases = weather, w`). Do not put the bot's **`command_prefix`** or punctuation in this value (no `!` or `.`)

Full reference: see `config.ini.example` in the repository for every section and option, with inline comments.

## Data retention

Database tables (packet stream, stats, repeater data, mesh graph) are pruned automatically. Retention periods and defaults are described in **[Data retention](data-retention.md)**. The bot’s scheduler runs cleanup daily even when the standalone web viewer is not running.

## Path Command configuration

The Path command has many options (presets, proximity, graph validation, etc.). All are documented in:

**[Path Command](path-command-config.md)** – Presets, geographic and graph settings, and tuning.

## Service plugin configuration

Service plugins (Discord Bridge, Telegram Bridge, Packet Capture, Map Uploader, Weather Service, Earthquake Service, Repeater Prefix Collision Service, and Webhook Service) each have their own section and are documented under [Service Plugins](service-plugins.md). The MQTT weather relay uses the `MqttWeather` section plus custom topic keys under `[Weather]`.

## Config validation

Before starting the bot, you can validate section names and path writability. See [Config validation](config-validation.md) for how to run `validate_config.py` or `meshcore_bot.py --validate-config`, and what is checked (required sections, typos like `[WebViewer]` → `[Web_Viewer]`, and writable paths).

## Reloading configuration

Some configuration can be reloaded without restarting the bot using the **`reload`** command (admin only). Radio/connection settings are not changed by reload; restart the bot for those.

## Pausing channel responses (remote)

Admins can DM **`channelpause`** or **`channelresume`** (see `[Admin_ACL]` in `config.ini`) to stop or resume bot reactions on **public channels** only—greeter, keywords, and commands on channels are skipped; DMs still work. The setting is **in memory only** (back to responding on channels after restart). Scheduled channel posts from the scheduler are **not** blocked by this toggle.

## Scheduled messages (`[Scheduled_Messages]`)

Each entry is `<schedule_key> = <value>` where the value is normally **`channel:message`** (first colon separates channel from body). For **regional flood scope** on that send only, use **`channel:#scope:message`**: the middle segment must start with `#` (same convention as `flood_scopes` / `outgoing_flood_scope_override`). The message body may contain more colons. Omit the middle field for classic global flood. See `config.ini.example` under `[Scheduled_Messages]` for examples. The **`schedule`** command lists each job with `(#scope)` when set.
