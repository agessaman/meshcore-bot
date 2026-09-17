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
| `[Localization]` | Default response language and optional sender-language detection |
| `[Admin_ACL]` | Admin public keys and admin-only commands |
| `[Keywords]` | Keyword → response pairs |
| `[Weather]` | Units and settings shared by `wx` / `gwx` and Weather Service |
| `[Logging]` | Log file path and level |

### Connection: type and precedence

`connection_type` in `[Connection]` selects the transport. **Only the matching keys are read**; other keys in the section are ignored at runtime (no error).

| `connection_type` | Keys used | Notes |
|-------------------|-----------|--------|
| `serial` | `serial_port` | USB serial device path |
| `ble` | `ble_device_name` | Empty = auto-detect first BLE device |
| `tcp` | `hostname`, `tcp_port` | `hostname` required; `tcp_port` defaults to 5000 |

Do not use `host` or `port` under `[Connection]` — those names are for `[Web_Viewer]` and `[Webhook]` listen addresses. TCP client connect uses `hostname` and `tcp_port`.

`config.ini.example` lists all connection keys uncommented so the config TUI and migrate tool recognize them. Lean templates (`minimal-example`, `quickstart`) comment out BLE/TCP keys by default because they ship with `connection_type = serial`.

### Connection: transport reconnect

`[Connection]` options `reconnect_max_retries` (0 = unlimited), `reconnect_delay_seconds`, and `reconnect_max_delay_seconds` apply to **serial, BLE, and TCP**. When the meshcore transport drops, the bot schedules reconnect with exponential backoff.

- **TCP** — meshcore emits `DISCONNECTED` on socket loss; the bot reconnects immediately (the main loop also polls every 5s as a backup). If `radio_probe_fail_threshold` consecutive `get_time` probes fail or time out, the bot reconnects the TCP session instead of declaring a zombie radio (serial/BLE probes still use zombie detection for unresponsive firmware).
- **Serial** — USB unplug triggers the same reconnect path; zombie detection remains for “port open but firmware dead” cases.

See `config.ini.example` for defaults and `radio_probe_*` / `radio_offline_*` alert options.

### Logging and log rotation

- **Startup (config.ini):** Under `[Logging]`, `log_file`, `log_max_bytes`, and `log_backup_count` are read when the bot starts. They control the initial `RotatingFileHandler` for the bot log file (see `config.ini.example`).

- **Live changes (web viewer):** The Config tab can store **`maint.log_max_bytes`** and **`maint.log_backup_count`** in the database (`bot_metadata`). The scheduler’s maintenance loop applies those values to the existing rotating file handler **without restarting** the bot—**but only after** you save rotation settings from the web UI (which writes the metadata keys). Editing `config.ini` alone does not update `bot_metadata`, so hot-apply will not see a change until you save from the viewer (or set the keys another way).

If you rely on config-file-only workflows, restart the bot after changing `[Logging]` rotation options.

### Localization

`[Localization] language` selects the bot's default translation catalog.
Set `auto_detect_language = true` to let greeting-style commands reply in the
sender's detected language when that translation is installed. Detection is
keyword-first so short mesh greetings such as `hola`, `bonjour`, and `hallo`
work without another dependency.

For statistical detection of longer messages, install the optional extra:

```bash
pip install "meshcore-bot[lang]"
```

Detection is opt-in and falls back to the configured default language whenever
the message is ambiguous, the detector is unavailable, or the corresponding
translation catalog is absent.

## Channels section

`[Channels]` controls where the bot responds:

- **`monitor_channels`** – Comma-separated channel names. The bot only responds to messages on these channels (and in DMs if enabled).
- **`respond_to_dms`** – If `true`, the bot responds to direct messages; if `false`, it ignores DMs.
- **`channel_keywords`** – Optional. When set (comma-separated command/keyword names), only those triggers are answered **in channels**; DMs always get all triggers. Use this to reduce channel traffic by making heavy triggers (e.g. `wx`, `satpass`, `joke`) DM-only. Leave empty or omit to allow all triggers in monitored channels. Per-command **`channels = `** (empty) in a command’s section also forces that command to be DM-only; see `config.ini.example` for examples (e.g. `[Joke_Command]`).
- **`max_response_hops`** - Default: 64 (code fallback); 7 in the shipped config templates. The bot will ignore messages that have traveled more than this number of hops. A value at or below 10 is recommended — in most meshes, anything higher is almost never an intentional message meant to trigger this bot, so lowering it keeps the bot from amplifying long flood traffic (#161).
- **`outgoing_flood_scope_override`** – Optional. Fixed regional scope for outbound channel sends when no per-message scope is passed to `send_channel_message`. When **not set** (default), replies use **`reply_scope`** from inbound TC_FLOOD correlation (auto-mirror). When **set** (e.g. `#west`), that scope is used for proactive sends (webhooks, scheduled messages, feeds) and whenever `reply_scope` is unset. It does **not** override an explicit `reply_scope` on a reply. Unscoped FLOOD uses global flood unless this override or `reply_scope` applies.
- **`flood_scopes`** – Optional. Comma-separated list of named scopes the bot will **accept and reply to**. When set, this acts as an allowlist: only TC_FLOOD messages matching one of these scopes receive a reply, and the reply is sent using the same scope as the incoming message (auto-mirror via `reply_scope`). Regular (unscoped) FLOOD messages are blocked unless `*` is included in the list. Leave empty or omit to accept all messages regardless of scope. **Auto-mirror requires correct RF correlation** (TC_FLOOD / GRP_TXT in the RF cache); if correlation fails, the bot will not use a stale ADVERT or other packet for scope and may ignore the message when `*` is not listed.

### outgoing_flood_scope_override vs flood_scopes

These two options are independent and serve different purposes:

| Option | Controls |
|--------|----------|
| `outgoing_flood_scope_override` | Default/fallback outbound scope when `reply_scope` is unset (proactive sends); does not override `reply_scope` on replies |
| `flood_scopes` | Which incoming scopes the bot *accepts* (allowlist + per-message `reply_scope` from RF correlation) |

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

**Example — fixed outbound scope for proactive sends and when mirror fails:**
```ini
outgoing_flood_scope_override = #west
```
Channel replies still prefer `reply_scope` from correlated TC_FLOOD when present. Override applies when `reply_scope` is unset (webhooks, scheduled jobs, or failed RF scope correlation).

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
- **`[Test_Command]`** – `test` / `t` behavior. Optional **`response_format`** overrides the legacy **`[Keywords] test`** string. Templates support the same placeholders as Keywords, plus **feed-style pipe filters** on placeholders (e.g. `{path_distance|pathbytes_min:2}`, `{firstlast_distance|hops_min:1}`) implemented in `modules/response_template.py`—see comments under `[Test_Command]` in `config.ini.example`.
- **`[Prefix_Command]`** – Prefix lookup, prefix best, range limits.
- **`[Cmd_Command]`** – `cmd` behavior. Set `cmd_reference_url` to return `Full command reference: <url>` instead of the generated compact command list.
- **`[Weather]`** – Used by the `wx` / `gwx` commands and the Weather Service plugin (see [Weather Service](weather-service.md)).
- **`[Airplanes_Command]`** – Aircraft/ADS-B command (API URL, radius, limits). Default `api_url` is `https://api.adsb.lol/v2/`.
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

### Command prefix

Under `[Bot]`:

- **`command_prefix`** – Optional global prefix(es) for commands. A single value (`!`, `abc`), comma-separated list (`!, ~, .`), or concatenated decorative characters (`!~.`) where the **first** entry is shown in help/docs. Leave empty for bare commands (legacy leading `!` is still accepted).
- **`require_command_prefix`** – When `true` (default), messages must start with a configured prefix. When `false`, configured prefix(es) are stripped when present but bare commands also work. Ignored when `command_prefix` is empty.

Full reference: see `config.ini.example` in the repository for every section and option, with inline comments.

### Config templates

- **`config.ini.example`** – Authoritative full reference; edit this when adding options or sections.
- **`config.ini.minimal-example`** – Lean config for core testing commands only (ping, version, test, path, prefix, multitest). Hand-maintained; see its header for purpose. Point users to `config.ini.example` for full options when enabling more features.
- **`config.ini.quickstart`** – Short easy-start config with a few common commands enabled. Hand-maintained.
- **`scripts/config_tui.py`** (`make config`) – Uses documented keys from `config.ini.example` (including commented `#key =` lines) for validation and migrate.

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

### Managing schedules from the web viewer

The **Schedule** page in the web viewer edits this section for you. It writes `config.ini` and queues a config reload, so new and changed schedules take effect **without restarting the bot**.

It is an editor for the same `[Scheduled_Messages]` section, not a separate store, so entries added by hand and entries added in the UI are the same thing and the `schedule` command lists both.

The editor builds the cron key from plain-language options (every day at a time, several times a day, certain days of the week, every N hours or minutes) and shows the resolved expression plus the next five run times before you save. "Advanced (cron)" accepts a raw expression. Existing entries always open in Advanced so a stored expression is never silently rewritten.

Schedules that the bot cannot run are listed rather than hidden, marked **Not scheduled** with the reason, so a typo that stops a message from airing is visible instead of silent. The 15-minute floor for `{cmd:...}` messages is enforced in the editor too, so it is refused when you save rather than dropped later at startup.

### Broadcasting a command's output (`{cmd:...}`)

A scheduled message can embed the reply of any bot command with **`{cmd:<command> [args]}`**. The command runs for its text only — it transmits nothing itself — and the scheduled message carries the result. This is how you put a recurring forecast (or any other command) on the air without that command needing its own scheduling settings:

```ini
[Scheduled_Messages]
0 6,12,18 * * * = Public:{cmd:wx Seattle}
*/30 * * * *    = Public:Conditions now: {cmd:wx 98101}
@hourly         = Public:{cmd:aqi Tacoma}
```

The trigger is matched against command **names and their keywords**, so `{cmd:weather Seattle}` and `{cmd:wx Seattle}` are the same command. Arguments are passed through exactly as a user would type them.

A placeholder expands to **nothing** (and logs a warning) when the command is unknown, disabled in config, admin-only, times out, or returns no output — the literal `{cmd:...}` is never transmitted. If a message is empty after expansion, nothing is sent at all. Command output is not re-scanned, so a reply that happens to contain `{cmd:...}` cannot recurse.

#### Airtime guards

Every firing is a transmission on a shared medium, and a command placeholder makes it easy to write a cron that airs several times an hour. Two guards apply, and neither is configurable:

- **A 15-minute floor.** A schedule containing `{cmd:...}` may not fire more often than every 15 minutes. An entry that does is **rejected at startup** with an error and is not scheduled at all — it does not silently run at a slower rate. The check measures the *tightest* gap between firings, so `0,1 * * * *` is treated as a 60-second schedule rather than an hourly one. Schedules with no command placeholder are unaffected.
- **The command's own cooldown still applies.** `[<Name>_Command] cooldown_seconds` is not bypassed by scheduling. If the command is on cooldown when the schedule fires, the placeholder expands to nothing for that round and logs a warning.

Other limits worth knowing:

- **Only commands marked `render_safe` can be rendered.** Capture intercepts `send_response` and `send_response_chunked`, so a command that transmits by other means (`advert`), posts its own messages (`announcements`), or is DM-only (`schedule`, which would broadcast configuration) is refused. This is opt-in rather than a denylist, so a new command is never renderable by accident. The read-only informational commands (`wx`, `aqi`, `sun`, `moon`, `solar`, `hfcond`, `satpass`, `rain`, `stats`, and similar) are marked safe.
- `[Bot] scheduled_command_timeout_seconds` (default `30`) bounds each render. Network-backed commands like `wx` need the headroom.

A rendered reply longer than one message is split to the RF body budget and sent as several messages rather than failing at the device.

Even within the floor, mind the cost: a `*/15 * * * *` forecast is 96 transmissions a day.

### Schedule keys (APScheduler cron, not Vixie)

Schedule keys are parsed by **APScheduler** `CronTrigger.from_crontab` (plus `@` presets and deprecated `HHMM`). Field order is the usual five: `minute hour day-of-month month day-of-week`.

**Day-of-week numbering differs from classic Vixie / crontab(5):**

| | APScheduler (this bot) | Vixie cron |
| --- | --- | --- |
| `0` | Monday | Sunday |
| `1` … `6` | Tuesday … Sunday | Monday … Saturday |
| `7` | Invalid | Often accepted as Sunday |

Prefer **`mon`–`sun`** names in the DOW field so expressions stay unambiguous. Example: Monday 12:30 is `30 12 * * mon` or `30 12 * * 0` — **not** Vixie’s `30 12 * * 1` (that is Tuesday here).

Preset aliases expand to those same APScheduler forms. In particular **`@weekly`** is Monday 00:00 (`0 0 * * 0`), not Sunday midnight as on many Unix crons.

#### Limiting a schedule to a date range

Crontab has no field for a date range, so `start=` / `end=` bounds live on the **value**,
ahead of the channel:

```ini
[Scheduled_Messages]
0 19 last-fri * * = start=2027-01-01 end=2027-03-31 Public:Winter net starts now
0 8 * * *         = end=2026-12-25 Public:#sea:Countdown to the holidays
```

Either bound may be omitted and they may appear in either order. `=` keeps them clear of
the `:` that separates channel from message, and because they are anchored to the front of
the value a message body mentioning `start=` is never mistaken for one.

The end date is **inclusive** — `end=2027-03-31` runs through the whole of the 31st, not up
to its midnight. Once a bounded schedule has no runs left the bot skips it at startup with
a log line saying so, and the web viewer shows it as **Finished** rather than hiding it.

#### Nth and last weekday of the month

APScheduler's day-of-month field understands positional expressions like `last fri` and
`4th tue`, which plain crontab cannot express. They contain a space, so write it as `-`
(or `_`) to keep the five fields intact:

| Key | Fires |
| --- | --- |
| `0 19 last-fri * *` | 19:00 on the last Friday of every month |
| `0 19 4th-tue * *` | 19:00 on the fourth Tuesday |
| `0 19 1st-tue,3rd-tue * *` | 19:00 on the first and third Tuesday |
| `0 19 last * *` | 19:00 on the last day of the month (no weekday needed) |

Prefixes are `1st` `2nd` `3rd` `4th` `5th` `last`, followed by `mon`–`sun`. They are valid
**only in the day-of-month field** — `0 19 * * last-fri` is rejected.

Two things to keep in mind. A month may have no fifth Tuesday, so `5th tue` simply skips
those months. And `1st-tue,3rd-tue` is a 14/14/21-day cadence across a month boundary, not
a strict fortnight: crontab matches calendar patterns and has no way to say "every 14
days". If you need an exact fortnight, note that anchoring one drifts an hour across a DST
change, which is usually worse for an announced net time than the 21-day gap.
