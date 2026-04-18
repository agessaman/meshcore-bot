# Changelog

All notable changes to this project are documented here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
semantic versioning.

## [0.9.0] — 2026-04-17

v0.9.0 is a large release that focuses on operational reliability, observability, and
deployment ergonomics. The headline additions are the authenticated real-time web
viewer, a full APScheduler rewrite, multi-arch Docker images, `.deb` packaging, a
migration-versioned aiosqlite DB, and numerous message-handling and radio-health
hardening fixes.

### Highlights

- **Real-time web viewer**: auth, contact management, live packet/message/log/mesh
  streaming, admin config editor, maintenance tools, DB backup UI, API Explorer tab,
  and early-start initializing banner.
- **Radio reliability**: zombie-radio detection with health probe and banner alerts,
  radio-offline fail state, send suppression during outages, `asyncio.wait_for`
  guards on `send_advert` / `disconnect_radio` / `reboot_radio`, radio debug mode
  toggle, packet-capture restart-storm prevention, auto-restart and reconnect logic.
- **Scheduler migration**: scheduler slimmed and switched to APScheduler; maintenance
  moved to its own module; signal-driven graceful shutdown and config reload; backup
  scheduler fire-window fix (BUG-024).
- **Database**: aiosqlite `AsyncDBManager`, versioned migrations in `db_manager`,
  safer ALTER-TABLE startup migrations for `channel_operations` and
  `feed_message_queue` (BUG-002), improved connection lifecycle across modules
  (BUG-017).
- **Packaging**: `.deb` build via `scripts/build-deb.sh`, multi-arch Docker images
  with SBOM + provenance, `check-package-data.sh` dist verification, ncurses config
  TUI (`scripts/config_tui.py`), bot admin HTTP server + `reload_config.sh`.
- **Rate limiting & safety**: per-channel rate limiting, per-user cooldown defaults
  tightened, thread-safe rate limiter with LRU SNR/RSSI caches, inbound webhook relay
  with bearer-token auth, SSRF hardening and log-injection sanitization, allow-local
  SMTP flag.
- **Commands**: `!schedule`, `!version`, `!path` geographic scoring toggle, airplanes
  (full list, no truncation), weather (high/low display, Open-Meteo model selection,
  MQTT weather, location fallback, multi-day forecasts), fortune (BSD format),
  RandomLine, configurable command reference URL.

### Added

- Authenticated web viewer with real-time streams (`packet_stream`, `command_stream`,
  `message_stream`, `log_stream`, `mesh_graph`) — see `93f73a1`, `a15827b`,
  `23f652f`, `4685ea7`, `da2e39c`, `ae52be4`, `9be5166`, `6246a81`.
- Web viewer admin config editor with password redaction and CSRF protection
  (`3a9f710`, `8bea10c`); live banner polling and early-start banner (`23f652f`).
- API Explorer tab and actionable error messages in the viewer (`a15827b`, `75be386`).
- Zombie-radio detection, health probe, timeout guards, and alert system (`d0ae737`,
  `8b14c40`); radio-offline fail state with send suppression and auto-restart
  (`51ab5d3`); radio debug logging mode with web UI toggle (`9ce6970`).
- APScheduler-based scheduler, maintenance module, graceful shutdown via Unix
  signals, and config-reload support (`aa2677b`, `07a2db4`, `904303f`).
- `.deb` packaging, multi-arch Docker build pipeline with SBOM + provenance, ncurses
  config TUI (`c7f2bdb`, `5b6f282`, `da1e68f`).
- Bot admin HTTP server + `reload_config.sh` CLI (`773b80f`).
- Inbound webhook relay with bearer-token authentication (`d07cca6`).
- Per-channel rate limiting (`25eb7cc`) and thread-safe rate limiter with LRU SNR
  and RSSI caches (`ea0e25d`).
- `!version` command and web-viewer footer version string (issue #91, `883b67d`,
  `fbf3995`).
- `!schedule` command listing scheduled messages and advert interval (`97e5c59`).
- `!path` geographic scoring toggle (`2a3a787`) and multibyte path chart rendering
  (`fbf3995`, `c6a7355`).
- Fortune command reading BSD fortune files (`13c10fd`) and RandomLine command
  (`a4d5f54`); `cmd_reference_url` option for `Cmd_Command` (`90fdd0c`).
- MQTT weather support, Open-Meteo model selection, location fallback, multi-day
  forecasts, and high/low temperature display (`9d768a3`, `5f6eced`,
  `206753a`, `3735f26`, `d9ea209`).
- Airplanes command sends all aircraft without truncation (`7403c1e`); keeps
  single-message output (`46d3fab`).
- CI log-injection regression check (`ce4fa8e`); lint gates for ruff, mypy, eslint,
  and shellcheck (`e1cf2eb` / `a12797f`).

### Changed

- **Upgraded `meshcore` to `>= 2.3.6`**, which also supplies upstream fixes for:
  - `can't convert negative int to unsigned` on flood contacts (issue #126) — the
    library now converts `out_path_len == -1` to `255` before packing. Commit
    `ba52c3b` adds belt-and-braces defensive wire-field rebuilding in
    `_ensure_contact_meshcore_path_encoding`.
  - `KeyError('msg_hash')` asyncio parser spam (issue #83) — the new
    `meshcore_parser.py` guards with `'msg_hash' in l`.
- `max_response_hops` default in shipped config templates lowered from 10 → 7
  (issue #161).
- `requires-python` raised to `>= 3.10` (Python 3.9 dropped; `meshcore >= 2.3.6`
  requires 3.10+). Ruff target bumped to `py310`, CI matrix now covers 3.11, 3.12,
  and 3.13.
- Web-viewer subscription handlers are silent; the navbar indicator reflects socket
  state (`1ee84f2`).
- Scheduler now uses `add_done_callback` (fire-and-forget) instead of blocking
  `future.result(timeout=X)` to avoid TimeoutError spam and loop stalls (BUG-015).
- Command aliases moved from global `[Aliases]` section to per-command `aliases =`
  keys (`14d3c0c`).
- Channel messages now reserve an extra 10-byte budget for regional flood scope
  (`4ee2079`).
- Web-viewer password is emphasized but no longer strictly required (`8b6ccc9`).
- Configuration docs clarified for monitored channels, `max_response_hops`, and
  public-channel guard (`20c4ea4`, `4bf0929`).
- Discord bridge supports multiple webhooks per channel (`0cd23e8`).

### Fixed

- **#126** (negative `out_path_len`): fixed via `meshcore >= 2.3.6` dep bump plus
  defensive handling in `_ensure_contact_meshcore_path_encoding`.
- **#83** (`KeyError('msg_hash')` asyncio spam): fixed via `meshcore >= 2.3.6` dep
  bump.
- Web-viewer status-ack tests now assert the silent UX instead of the removed
  `emit('status', …)` calls (`tests/test_web_viewer.py`).
- `send_advert()` guarded with `asyncio.wait_for(timeout=30)` to prevent event-loop
  lockup (`22e1b2b`, `329905d`).
- `packetcapture` restart storm during radio reconnect (`f09b214`).
- Scheduler `RuntimeError` on threadsafe `future.result` handled (`7b01242`).
- Web-viewer config-item retrieval no longer triggers interpolation errors
  (`ad09e8b`).
- Path length calculation and hash mode in `MessageHandler` corrected (`ba52c3b`).
- Mention handling, reply-match base function, and command-class inheritance fixes
  (`8bea10c`, `9d4b142`, `56be1e7`, `277491f`).
- Path validation hardened (`6e8204c`); scheduler duplicate run + mypy fallback
  types (`8b68644`).
- Shutdown hardened — single stop, viewer cleanup, MQTT log teardown, scheduler
  drain (`e058da4`).
- Discord-bridge channel-key normalization test alignment (`4178371`, `f971e97`).
- BUG-001 .. BUG-029 — see `BUGS.md` v0.9.0 section for the full list.

### Security

- SSRF hardening in outbound HTTP (`54aeb28`) with explicit CGN-network check in
  `validate_external_url` (`2a80f76`).
- Log-injection sanitization applied to user-supplied log lines (`54aeb28`); CI
  regression check added (`ce4fa8e`).
- `allow_local_smtp` flag for opt-in local SMTP relay usage (`54aeb28`).
- SMTP SSRF guard import restored in `scheduler.py` (`c543cac`).
- CSRF protection in the web viewer (`3a9f710`).

### Infrastructure

- Initial test suite, pytest timeouts, coverage threshold, and tracking files
  (`9de9230`, `ba32acc`, `c95ddf6`).
- Test-coverage expansion for commands, web viewer, and infrastructure (`9be5166`).
- MQTT live-test framework and packet fixtures (`a667e3c`).
- Per-test timeout in `pytest.ini` to prevent CI hangs (`d7cf0d5`).
- Makefile + virtual-environment bootstrap (`c2149bc`).

### Documentation

- README, config example, `docs/configuration.md`, and BUGS.md updated throughout
  v0.9.0.
- Discord integration, kg7qin integration notes (`f2936be`, `de6279c`).

[0.9.0]: https://github.com/agessaman/meshcore-bot/compare/v0.8.3...v0.9.0
