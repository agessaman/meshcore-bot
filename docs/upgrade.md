# Upgrade Guide

This document describes changes that may affect users upgrading from previous versions. Read the section that matches the version you are upgrading **from** (not the version you are installing).

For a full list of v0.9 changes, see [CHANGELOG.md](https://github.com/agessaman/meshcore-bot/blob/main/CHANGELOG.md).

## Upgrading from v0.8 → v0.9

v0.9 is a large release focused on operational reliability, observability, and deployment ergonomics. Your existing `config.ini` continues to work; review the items below after pulling the new code.

### Python and dependencies

- **Python 3.10+** is required (Python 3.9 is no longer supported). Rebuild your virtual environment or re-run `./install-service.sh --upgrade` with a system Python 3.10 or newer.
- **`meshcore >= 2.3.6`** is required. This fixes negative `out_path_len` encoding (#126) and `KeyError('msg_hash')` parser spam (#83).

### Config changes

- **Command aliases** — The global **`[Aliases]`** section is removed. Move each alias list to the corresponding command section as `aliases = stem1, stem2` (stems only; no command prefix). See [Configuration](configuration.md#per-command-aliases-v09).
- **`max_response_hops`** — Shipped config templates now default to **7** (was 10). The code fallback when unset is still 64. Review this if you relied on the old template default.
- **New optional sections** (safe to omit):
  - **`[Rate_Limits]`** — Per-channel minimum seconds between bot messages. See [Configuration](configuration.md#rate-limiting).
  - **`[Webhook]`** — Inbound HTTP POST relay to channels or DMs. See [Configuration](configuration.md#inbound-webhook).
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
- SMTP: use **`allow_local_smtp`** only if you intentionally relay to local mail servers.
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

If you are coming from v0.7 and skipping v0.8, also read [Upgrading from v0.8 → v0.9](#upgrading-from-v08--v09) above.

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
