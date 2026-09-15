# Upgrade Guide

This document describes changes that may affect users upgrading from previous versions. Read the section that matches the version you are upgrading **from** (not the version you are installing).

For the full release history, see [CHANGELOG.md](https://github.com/agessaman/meshcore-bot/blob/main/CHANGELOG.md).

## Upgrading from v1.0.0 to v1.1.0

v1.1.0 adds features without removing or renaming the stable configuration,
command, service-plugin, or web-viewer interfaces introduced in v1.0.0. Existing
configuration files continue to work. Back up `config.ini`, `local/`, and the bot
database before upgrading, then use the normal installer or container update path.

### Database

Migration 23 adds nullable `snr` and `rssi` columns to `observed_paths`. The bot
applies it automatically and records it in `schema_version`; existing path rows and
rows without a direct RF measurement retain `NULL` values. The migration is
transactional and safe to run more than once.

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
