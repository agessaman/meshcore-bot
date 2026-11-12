# Upgrade Guide

This document describes changes that may affect users upgrading from previous versions. Read the section that matches the version you are upgrading **from** (not the version you are installing).

For the full release history, see [CHANGELOG.md](https://github.com/agessaman/meshcore-bot/blob/main/CHANGELOG.md).

## Upgrading from v1.1.0 to v1.2.0

v1.2.0 adds features and fixes without removing or renaming any configuration key, command or service plugin. Existing configuration files continue to work. Back up `config.ini`, `local/` and the bot database before upgrading, then use the normal installer or container update path. Read **Web viewer access** and **Behavior changes** below before you upgrade: one changes who can see the viewer, and a few change what goes on air.

### Dependencies and database

No dependency changes: `meshcore` 2.3.14 or newer is still required. There are no new database migrations.

### Web viewer access

- **With `web_viewer_password` set, the Dashboard, Contacts, Mesh Graph and Multibyte pages (and the read-only APIs behind them) are now readable without logging in.** In v1.1.0 a password put every page behind the login. Real-time, Radio, Config, Logs, the settings pages and every API that changes something still require an admin login, and Real-time drops out of an anonymous visitor's navigation because its streams carry decrypted traffic and channel keys. If node names, positions and the mesh graph shouldn't be public on your network, keep the viewer off the internet or put it behind your own proxy authentication.
- Sessions from v1.1.0 are not carried over, so everyone logs in again once after the upgrade.
- The viewer serves its own Bootstrap, Font Awesome, Socket.IO, Chart.js, Leaflet, MapLibre and font files, and its Content-Security-Policy no longer allows any CDN host. Every page works on a LAN without internet; only map tiles still need it. A local template or customization that loads a script or stylesheet from a CDN is now blocked by the browser.

### Home Assistant

The bot is available as an official Home Assistant add-on for Home Assistant OS and Supervised installs. Add `https://github.com/agessaman/meshcore-bot` as an add-on repository; see [Home Assistant](home-assistant.md). The add-on image is 64-bit only (amd64 and aarch64). The regular Docker image keeps its 32-bit ARM build.

`[Web_Viewer] ha_ingress` (default `false`) is what serves the viewer in Home Assistant's sidebar. Leave it off outside the add-on.

### Configuration

All new keys are optional, and leaving them unset keeps v1.1.0 behavior unless noted under **Behavior changes**.

- `[Repeater_Watch]` is new and off by default: it reports watched repeaters that go quiet, from **Settings → Repeater Watch** or by hand. See [Repeater watch](repeater-watch.md).
- `[Scheduled_Messages]` values take optional `start=`, `end=` and `week=` options ahead of the channel, and the day-of-month field takes `last-fri`, `4th-tue` and similar. See [Scheduled messages](scheduled-messages.md).
- `[Weather] openmeteo_fallback` (default `false`) lets `wx` on NOAA answer places outside NWS coverage from Open-Meteo. `[Weather] always_show_location` (default `false`) names the place in every `wx` and `gwx` reply.
- `[Weather_Service] weather_alerts_enabled` (default `true`) turns NOAA alert polling off without disabling the rest of the weather service.
- `[Bot] dm_min_ack_timeout`, `[Localization] place_name_language` and `[Region_Warnings] flood_scope` are new; their defaults change behavior and are covered below.

### Behavior changes

- **A radio that can't be reached at startup no longer stops the bot.** The bot retries with the same backoff as a dropped connection (`[Connection] reconnect_max_retries`, `reconnect_delay_seconds`, `reconnect_max_delay_seconds`) while the web viewer keeps serving. With the default `reconnect_max_retries = 0` it retries forever. When a nonzero limit runs out, the bot now exits with status 1 instead of 0, so the shipped systemd unit and launchd plist restart it. A startup failure that escapes the bot (a Bluetooth adapter error, for example) also exits with status 1 now.
- **`last` in a schedule key now means what it says.** APScheduler used to read `last` as a prefix, so a key written as `0 19 last-fri * *` ran on the last day of the month. It now runs on the last Friday. Malformed positional days (`last--fri`, `lastfri`, `last-fri/2`), a positional day combined with a day of the week (`0 19 1st-tue * mon`, which never fires) and a schedule with no runs left are now rejected with a warning at startup instead of being scheduled. After upgrading, check the log for `Invalid schedule` and `not scheduled` lines.
- **A DM that is never acknowledged takes longer to report failure.** `[Bot] dm_min_ack_timeout` (default 8 seconds) is a floor on each attempt's ACK wait, because the radio's own timeout gave up on fast presets before the recipient's ACK could arrive. An ACK still ends the wait at once, so delivered DMs are no slower, but an unacknowledged DM now takes at least 24 seconds with the default three attempts. Set it to `0` for the old timing.
- **Region warnings follow the channel's flood scope.** With `delivery = channel` they used to go out at global scope; they now use `[Region_Warnings] flood_scope`, then `flood_scope.<channel>`, then `outgoing_flood_scope_override`. Set `[Region_Warnings] flood_scope = *` to keep sending them globally.
- **Place names are in the bot's language.** `wx`, `gwx` and other location replies name places in `[Localization] language` (falling back to English) instead of each place's local language, so `gwx Tokyo` says `Chiyoda, JP` rather than `千代田区, JP`. Set `place_name_language = local` for the old names. Cached reverse lookups are now kept per language, so places are looked up again once after the upgrade unless you set `local`.
- **`wx` follows `[Weather] temperature_unit` and `wind_speed_unit`** on NOAA, as `gwx` already did; it used to answer in °F and mph whatever they said. Defaults are unchanged. A `[Wx_Command]` unit setting still overrides `[Weather]` for `wx`, so remove any you copied there if you want `[Weather]` to apply to both.
- **`gwx` names the place only when that adds information**, as `wx` does: a city in your `default_country` (and `default_state` in the US) is no longer prefixed with its own name. `gwx` with no location forecasts the sender's or the bot's coordinates instead of a geocoded town name.
- **Channel messages are sized to 155 bytes** of `"<name>: <text>"` instead of 160, which saves a whole extra encrypted block of airtime. Long replies split a little sooner. Any channel send over the budget (from a service, a scheduled message or a plugin) is now split into parts tagged ` (1/2)`, ` (2/2)` instead of being handed to the radio whole.
- **`trace` waits longer by default:** 2.0 s base and 1.5 s per hop (were 1.0 s and 0.5 s), and a reply to any attempt counts until the last attempt times out. Configured timeouts are kept.
- **Webhook posts longer than one message are split** across several channel messages. `[Webhook] max_message_length` is now the total text accepted per request, and a successful response reports the part count: `{"ok": true, "parts": 2}`.
- A channel message or DM repeated within 5 seconds is dropped as a duplicate, which stops double replies after a Bluetooth reconnect.

### Local plugins and scripts

- `MessageHandler`'s pending-message correlation was removed, including `store_message_for_correlation`, `correlate_message_with_rf_data`, `cleanup_old_messages`, `try_correlate_pending_messages`, the `pending_messages` attribute and `PendingMessageEntry`. A local plugin that called them needs updating; nothing read their results.
- `truncate_to_bytes` moved to `modules.utils`. `modules.region_warning` still re-exports it.
- `send_channel_message` now splits an over-budget body itself, and `channel_body_budget` reports the byte budget for a channel and scope.

## Upgrading from v1.0.0 to v1.1.0

v1.1.0 adds features without removing or renaming the stable configuration,
command, service-plugin, or web-viewer interfaces introduced in v1.0.0. Existing
configuration files continue to work. Back up `config.ini`, `local/`, and the bot
database before upgrading, then use the normal installer or container update path.

### Dependencies

`meshcore` must be 2.3.14 or newer. This is a hard requirement, not a
recommendation: before 2.3.13, `send_msg_with_retry` reported ACKed DMs as
failures, so a bot on an older library logs "no ACK received after retries" for
messages the recipient did receive, and skips the delivery bookkeeping for them.
The normal installer and container paths pick this up on their own; a manual venv
needs `pip install -U -r requirements.txt`.

### Database

Migration 23 adds nullable `snr` and `rssi` columns to `observed_paths`. The bot
applies it automatically and records it in `schema_version`; existing path rows and
rows without a direct RF measurement retain `NULL` values. The migration is
transactional and safe to run more than once.

Migration 24 creates `region_scope_daily` and `region_warning_events` for the
region-scope tallies described below. Both are new tables, so nothing existing is
rewritten, and the migration is safe to run more than once. `region_scope_daily`
is written for every channel message once `[Region_Warnings] track_traffic` is on
(the default); it holds one row per channel per local date, so the growth is
bounded by channel count rather than traffic. `region_warning_events` is pruned by
`[Data_Retention] region_warning_retention_days` (default 90).

### Configuration and local extensions

- `[Test_Command] distance_unit` controls printed path distances. Its default,
  `auto`, uses miles for `en` and `en-US` replies and kilometres for other locales.
- `[PacketCapture] observer_name` can give MQTT payloads an observer identity that
  differs from the connected MeshCore node name. Leaving it unset preserves the
  v1.0.0 behavior.
- `[Localization] local_translation_path` loads operator-owned catalog overrides
  from the configured local directory. Existing installations need no new file.
- `mqttN_keepalive` and `mqttN_jwt_reconnect_on_renew` control broker keepalive and
  renewed-token reconnect behavior. Their defaults preserve ordinary brokers while
  making expiring-token sessions reconnect before the broker rejects them.
- The Plugins page now discovers local commands and services and saves their new
  sections to `local/config.ini`. A section already present in the base config keeps
  its existing owner.
- `[Region_Warnings]` is new. Scope **tallying** is on by default and costs no
  airtime; the **warning** is off by default (`enabled = false`) and, when enabled,
  starts in dry run (`dry_run = true`). Nothing is transmitted on upgrade unless you
  turn both of those around. See `docs/region-warnings.md`.
- `[Data_Retention] region_warning_retention_days` (default 90) prunes the warning
  event log.
- `[Hello_Command] include_sender` (default off) names the user the hello reply is
  answering. Existing hello replies are unchanged.
- `[Contact_Command] enabled` (default on) adds a `contact` command that replies with
  the bot's own contact card. Set it to `false` if you do not want it.

### Behavior changes

- Packet-capture `timestamp`, `date`, and `time` fields now describe the same UTC
  instant. Consumers that interpreted `date` or `time` as host-local values must
  switch to UTC.
- Airplanes uses `adsb.lol` by default because the former public endpoint rejects
  unauthenticated clients. Existing `airplanes.live` defaults are remapped; custom
  and local readsb URLs are unchanged.
- The web viewer's former Disconnect action is labelled Stop Bot and asks for
  confirmation because the action terminates the process rather than only closing
  the radio connection.
- Response templates support nested placeholders, quoted filter arguments,
  `hops_min`, `shorten`, and `if_nonempty`. Existing templates retain their prior
  syntax, and substituted values are not reparsed as template source.
- Scheduled messages can be edited in the web viewer and can include bounded
  `{cmd:...}` output. Schedules using command output must leave at least 15 minutes
  between runs.
- Proactive weather output, `!wx`, and `!gwx` share localized alert formatting.
  Russian is included, and local catalogs can override individual strings.
- MQTT brokers on `waev.app` now default to a 3600s JWT TTL with renewal at 3500s,
  because waev rejects a longer-lived token. An explicit `mqttN_jwt_ttl_seconds` or
  global `jwt_ttl_seconds` still wins, so existing configurations are unchanged; the
  substitution is logged when it applies. No other broker's defaults move.
- The Radio page gains **Region Scopes** and **Default Region Scope** cards, so the
  bot's own flood scopes and the radio's firmware default can be set from the web
  viewer instead of by hand. Note that the bot leaves the radio in forced-unscoped
  mode after any scoped send, so the firmware default stops applying until the bot
  scopes another one — the card says so too.
- A DM waiting for its ACK no longer holds the radio, so channel replies, other DMs,
  and scheduled sends are no longer stalled behind one DM's retries. A region-scoped
  channel send does still hold the radio from setting the scope through restoring it,
  so nothing else goes out under that scope.

## Upgrading from v0.8 → v0.9

v0.9 is a large release focused on operational reliability, observability, and deployment ergonomics. Your existing `config.ini` continues to work; review the items below after pulling the new code.

### Python and dependencies

- **Python 3.10+** is required (Python 3.9 is no longer supported). Rebuild your virtual environment or re-run `./install-service.sh --upgrade` with a system Python 3.10 or newer.
- **`meshcore >= 2.3.6`** is required. This fixes negative `out_path_len` encoding (#126) and `KeyError('msg_hash')` parser spam (#83).

### Config changes

- **Command aliases** — The global **`[Aliases]`** section is removed. Move each alias list to the corresponding command section as `aliases = stem1, stem2` (stems only; no command prefix). See [Configuration](configuration.md#command-specific-sections).
- **`max_response_hops`** — Shipped config templates now default to **7** (was 10). The code fallback when unset is still 64. Review this if you relied on the old template default.
- **New optional sections** (safe to omit):
  - **`[Rate_Limits]`** — Per-channel minimum seconds between bot messages. See [README](https://github.com/agessaman/meshcore-bot#per-channel-rate-limiting).
  - **`[Webhook]`** — Inbound HTTP POST relay to channels or DMs. See [README](https://github.com/agessaman/meshcore-bot#inbound-webhook).
  - Radio reliability options under **`[Bot]`** (zombie-radio detection, send suppression during outages, etc.) — see `config.ini.example`.

### Database

- Schema upgrades are handled automatically via versioned migrations (`MigrationRunner` / `AsyncDBManager`). Start the bot once after upgrading; migrations run at startup.
- If you see migration errors, ensure you are on the latest code and restart once. See [FAQ](faq.md) for database troubleshooting.

### Web viewer

- Set **`web_viewer_password`** when exposing the viewer beyond localhost (`host = 0.0.0.0`). Password is optional on localhost but strongly recommended on a LAN or the internet.
- Mutating routes use **CSRF** protection when authenticated.
- New pages and real-time streams (packets, commands, messages, logs, mesh graph). See [Web Viewer](web-viewer.md).

### Scheduler and config reload

- The scheduler uses **APScheduler**; maintenance tasks live in a separate module.
- Some configuration can be reloaded without a full restart via **`reload_config.sh`**, the admin HTTP server, or the admin **`reload`** command. Radio/connection settings still require a full bot restart.

### Packaging

- **Debian package:** `make deb` (see README).
- **Docker:** Multi-architecture images (amd64, arm64, armv7) on GHCR. See [Docker deployment](docker.md).

### Security review

- Outbound HTTP uses SSRF hardening; review integrations that fetch URLs.
- SMTP: private and local SMTP hosts are blocked unless you tick **Allow a local or private-network SMTP server** (`allow_local_smtp`) in the web viewer's Email & Notifications settings. It is stored in the bot database, not `config.ini`; turn it on only if you intentionally relay to a local mail server.
- User-supplied strings in logs are sanitized to reduce log-injection risk.

### New commands and behavior

- **`version`** / **`ver`** — Reports bot software version.
- **`schedule`** — Lists scheduled messages and advert interval (admin).
- **`path`** — Multi-byte path support; **`geographic_scoring_enabled`** in `[Path_Command]` toggles proximity guessing (config only, not a chat subcommand).
- **Weather** — High/low temperatures, Open-Meteo model selection, MQTT weather, location fallback, multi-day forecasts.
- **Airplanes** — Sends all matching aircraft in one RF-bounded message (see [Command Reference](command-reference.md)).
- **RandomLine** — Trigger-based random lines (including fortunes via `[RandomLine]`); no separate `fortune` command.
- **Discord bridge** — Multiple webhook URLs per channel (comma-separated).

---

## Upgrading from v0.7 → v0.8

If you are coming from v0.7 and skipping v0.8, also read [Upgrading from v0.8 → v0.9](#upgrading-from-v08-v09) above.

### Path command and mesh graph

- **Multi-byte paths** — Path decoding supports 1-, 2-, and 3-byte hop encodings. Configure **`prefix_bytes`** and graph options under **`[Path_Command]`**. See [Path Command](path-command-config.md).

### Flood scopes and regional messaging

- **`flood_scopes`** — Allowlist of regional TC_FLOOD scopes the bot accepts.
- **`outgoing_flood_scope_override`** — Optional fixed outbound scope for proactive sends.
- **Scheduled messages** — Support scoped channel posts (`channel:#scope:message` syntax).

### Local plugins

- Drop custom command modules in **`modules/local/`** and enable via **`local_plugins`** in config. See [Local plugins](local-plugins.md).

### Web viewer and database

- The web viewer can share the bot’s SQLite database (`[Bot] db_path`) so contacts, mesh graph, and packet stream appear in one place.
- Optional **`collect_stats = true`** under `[Stats_Command]` populates dashboard stats when the `stats` chat command is disabled.

### Bridges and services

- Discord and Telegram bridges gained bot-response bridging and additional options. See [Discord Bridge](discord-bridge.md) and [Telegram Bridge](telegram-bridge.md).

### Service installation

- Chunked message sends for long responses; improved shutdown and scheduler hardening when running under systemd.

---

## Upgrading from v0.9.3 to v1.0.0

### Service Layout and Ownership

The standalone installer and Debian package now keep executable code root-owned and
separate mutable runtime data:

| Component | Linux | macOS |
|-----------|-------|-------|
| Code and virtual environment | `/opt/meshcore-bot` | `/usr/local/meshcore-bot` |
| Configuration | `/etc/meshcore-bot/config.ini` | `/usr/local/etc/meshcore-bot/config.ini` |
| Database and local plugins | `/var/lib/meshcore-bot` | `/usr/local/var/lib/meshcore-bot` |
| Logs | `/var/log/meshcore-bot` | `/usr/local/var/log/meshcore-bot` |

Run `sudo ./install-service.sh --upgrade` from an updated source checkout. The
installer requires Python 3.10+ and `rsync`. It stops an active service before the
database migration, copies relative-path databases coherently, rewrites the migrated
configuration, builds a fresh virtual environment, and restarts a service that was
active before the upgrade.

Add `--install-extras` to install the optional profanity-filter and geocoding
packages without being prompted, which is what you want for an unattended upgrade.

Before upgrading, keep a separate backup of your configuration and database. The
installer preserves:

- the active configuration and absolute custom paths;
- relative-path databases, logs, and the `local/` plugin tree;
- installed-only files under `modules/commands/alternatives/`.

Trusted source versions still replace shipped alternative commands. If the upgrade
fails after stopping an active service, the installer attempts one best-effort restart
and retains the original failure exit status. Inspect the reported error before
retrying.

### Configuration Compatibility

v0.9.3 shipped configuration examples pass the v1.0.0 strict validator. Normal startup
warns about unknown sections or keys without refusing to start. You can check a config
before upgrading:

```bash
python3 validate_config.py --config /path/to/config.ini --strict
```

### Outbound HTTP and Feeds

Outbound HTTP now validates every resolved address and redirect. Private feed URLs
remain disabled by default; set `allow_private_urls = true` only for intentional
private-network feeds. Cloud metadata and non-unicast destinations remain blocked.
Feed responses are bounded by `max_response_bytes` and `max_parsed_items`.

`[Feed_Manager]` numeric limits are now clamped to sane minimums instead of being
used as written. **If you set `max_items_per_check = 0` to pause posting, that no
longer works** — it now scans and posts one item per poll. Use
`feed_manager_enabled = false` to stop the feed manager, or `feed disable <id>` for a
single feed.

### Optional Geocoding Data

`pycountry` and `us` remain optional under the `geo` extra. Install them for improved
country and US-state normalization:

```bash
pip install -e ".[geo]"
```

The standalone installer also offers the geocoding extras interactively.

### Optional Sender-Language Detection

Greeting-style commands can reply in the sender's language. The feature is off by
default and changes no behavior until you enable it:

```ini
[Localization]
auto_detect_language = true
```

Detection is keyword-first, so short greetings work with no extra dependency. For
statistical detection of longer messages, install the optional `lang` extra:

```bash
pip install -e ".[lang]"
```

Replies fall back to the configured `language` whenever the message is ambiguous,
the detector is unavailable, or that translation catalog is not installed.

## Upgrading from v0.8 to v0.9

- Python 3.10+ is required.
- The global `[Aliases]` section was replaced by per-command `aliases =` keys.
- Database migrations run automatically at startup.
- Review web-viewer authentication before binding beyond localhost.
- Connection and radio settings still require a process restart after changes.

## Upgrading from v0.7

If you upgraded through v0.8, see the sections above for v0.8 and v0.9 changes. The notes below apply specifically to configs that have not been updated since v0.7.

### Config compatibility

Previous config files continue to work. The following legacy config formats are supported:

- **`[Jokes]`** with `joke_enabled` / `dadjoke_enabled` — Migrated to `[Joke_Command]` and `[DadJoke_Command]` with `enabled`. Both formats work; consider updating to the new format.
- **`[Stats]` / `stats_enabled`**, **`[Sports]` / `sports_enabled`**, **`[Hacker]` / `hacker_enabled`**, **`[Alert_Command]` / `alert_enabled`** — All support the legacy `*_enabled` key; the new `enabled` key is preferred.

### Banned users: prefix matching

`[Banned_Users]` uses **prefix (starts-with) matching** for `banned_users` entries. A banned entry `"Awful Username"` matches both `"Awful Username"` and `"Awful Username 🍆"`. If you rely on exact matching, ensure your banned entries are specific enough.

### New optional sections

- **`[Feed_Manager]`** — If you use RSS/API feeds, add this section. If absent, the feed manager is disabled. New installs and minimal configs include `[Feed_Manager]` with `feed_manager_enabled = false`.
- **`[Path_Command]`** — Options like `path_selection_preset`, `enable_p_shortcut` (default: true), and graph-related settings. Omitted options use sensible defaults. See [Path Command](path-command-config.md).
