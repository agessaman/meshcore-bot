# TODO

Task list for meshcore-bot development. Auto-updated sections are regenerated
by running `python scripts/update_todos.py` (see [Auto-Update](#auto-update)).

**Last updated:** 2026-03-16 — coverage at 36.72% (2,140 passed / 29 skipped); `fail_under=35`; target 40%

---

## In Progress

- [ ] TASK-14: Push test coverage to ≥40% (currently **36.72%**, 2,140 passed / 29 skipped; `fail_under=35`; hardware-dependent modules cap realistic ceiling at ~40–42%)
  - [x] (2026-03-15) `tests/test_enums.py` — enum values and flag combinations
  - [x] (2026-03-15) `tests/test_models.py` — MeshMessage dataclass
  - [x] (2026-03-15) `tests/test_transmission_tracker.py` — full TransmissionTracker
  - [x] (2026-03-15) `tests/test_message_handler.py` — path parsing, cache, message routing
  - [x] (2026-03-15) `tests/test_repeater_manager.py` — role detection, ACL, device type
  - [x] (2026-03-15) `tests/test_core.py` — config loading, radio settings, reload
  - [x] (2026-03-15) `tests/test_feed_manager.py` — queue insert, deduplication via feed_activity, interval due-check
  - [x] (2026-03-15) `tests/test_scheduler_logic.py` — scheduled message dispatch, interval advertising setup
  - [x] (2026-03-15) `tests/test_command_manager.py` — full command dispatch, keyword matching
  - [x] (2026-03-15) `tests/test_channel_manager_logic.py` — cache lifecycle, fetch-all, sorted cache, connectivity guard
  - [x] (2026-03-16) `tests/test_channel_manager.py` — generate_hashtag_key, cache lookups, add_channel validation (47 tests)
  - [x] (2026-03-16) `tests/test_web_viewer.py` — 19 new tests for stream_data, update_channel, maintenance status (220 total)
  - [x] (2026-03-16) Fixed failing `test_weekly_on_wrong_day_does_not_run` — was patching `now` instead of `fake_now`

  **TASK-14 sub-tasks — prioritized target list (2026-03-16 coverage scan):**

  **Tier 1 — High impact, core logic:**
  - [x] T1-A: Realtime viewer panels blank bug — FIXED (third pass: root cause was `<script type="module">` creating a competing Socket.IO manager; fixed by converting to regular `<script>` with dynamic `import()`, removing `forceNew: true` from base.html, raising `ping_timeout` 5→20 s, and adding missing `subscribed_messages` key)
  - [x] T1-B: `message_handler.py` — extended (139 total tests); `decode_meshcore_packet`, `parse_advert`, RF correlation, `_get_path_from_rf_data`, `handle_rf_log_data`; BUG-028 discovered (`byte_data` UnboundLocalError in except handler)
  - [x] T1-C: `repeater_manager.py` — extended (131 total tests); `track_contact_advertisement`, `_track_daily_advertisement`, `_determine_device_type` gaps, `_auto_purge_repeaters`, `_get_companions_for_purging`
  - [x] T1-D: `scheduler.py` — extended (106 total tests); `_get_mesh_info`, `_send_scheduled_message_async`, `_run_data_retention`, `_collect_email_stats`
  - [ ] T1-E: `feed_manager.py` — partially done; polling loop still needed
  - [ ] T1-F: `web_viewer/app.py` (41%, ~2,269 uncovered) — greeter, bans, packets/messages endpoints, export, SocketIO, firmware routes
  - [x] T1-G: `web_viewer/integration.py` — new `tests/test_web_viewer_integration.py` (circuit breaker, JSON serializer, packet capture, channel message)

  **Tier 2 — Medium impact, mostly testable:**
  - [ ] T2-A: `utils.py` (60%, ~403 uncovered) — `format_keyword_response`, `calculate_path_distances`, `get_major_city_queries`
  - [ ] T2-B: `graph_trace_helper.py` (2%, ~159 uncovered) — pure graph/trace algorithm, zero hardware deps
  - [ ] T2-C: `db_manager.py` (54%, ~147 uncovered) — `AsyncDBManager` async methods, write queue, `executemany` batch
  - [ ] T2-D: `discord_bridge_service.py` (31%, ~239 uncovered) — message formatting, webhook dispatch, rate-limit warn
  - [ ] T2-E: `telegram_bridge_service.py` (36%, ~195 uncovered) — message relay, topic routing, listener lifecycle
  - [ ] T2-F: `greeter_command.py` (15%, ~557 uncovered) — greeting detection, per-channel greetings, new-contact detection
  - [x] T2-G: `rate_limiter.py` — extended to 98%; only actual sleep lines remain
  - [x] T2-H: `stats_command.py` — extended (66 total tests); `_get_adverts_leaderboard`, `get_stats_summary`, `cleanup_old_stats`, exception paths, data-populated leaderboards (61% coverage)
  - [x] T2-I: `i18n.py` — new `tests/test_i18n.py` (98% coverage); fallback loops, format failure, PermissionError, get_value break

  **Tier 3 — Smaller commands, good test bang-for-buck:**
  - [x] T3-A: `trace_command.py` — new `tests/test_trace_command.py` (88% coverage); path extract, parse, format inline/vertical, reciprocal, execute paths
  - [x] T3-B: `announcements_command.py` — new `tests/test_announcements_command.py`; parse, record_trigger, execute all paths
  - [ ] T3-C: `channels_command.py` (86%, ~33 uncovered) — remaining paths
  - [x] T3-D: `help_command.py` — new `tests/test_help_command.py`; format list, channel filter, general/specific help, execute
  - [x] T3-E: `aurora_command.py` — new `tests/test_aurora_command.py`; KP index parsing, alert level logic, execute paths
  - [x] T3-F: `joke_command.py` — new `tests/test_joke_command.py` (seasonal, format, split, dark, execute)
  - [x] T3-G: `dadjoke_command.py` — new `tests/test_dadjoke_command.py` (format, split, length, execute)
  - [x] T3-H: `webviewer_command.py` — 100% coverage (no test file needed)
  - [ ] T3-I: `trace_runner.py` (24%, ~50 uncovered) — trace execution, path assembly
  - [ ] T3-J: `earthquake_service.py` (16%, ~119 uncovered) — alert threshold, message format (USGS API mockable)
  - [x] T3-K: `moon_command.py` — new `tests/test_moon_command.py`; phase calc, execute success/error
  - [ ] T4-A: `multitest_command.py` (33%, ~220 uncovered) — multi-channel test sequences; pure logic + async
  - [ ] T3-L: `hacker_command.py` (17%, ~101 uncovered) — text transform logic
  - [ ] T3-M: `sports_command.py` (16%, ~325 uncovered) — score formatting, schedule display
  - [ ] T3-O: `repeater_command.py` (10%, ~363 uncovered) — repeater list/info formatting

  **Tier 4 — API/hardware heavy, skip for now:**
  - `wx_command.py` (6%), `weather_service.py` (6%), `solar_conditions.py` (7%), `solarforecast_command.py` (8%), `packet_capture_service.py` (5%), `map_uploader_service.py` (10%), `airplanes_command.py` (10%), `aqi_command.py` (11%), `alert_command.py` (13%), `prefix_command.py` (10%), `packet_capture_utils.py` (12%)

---

## MQTT Test Framework (NEW 2026-03-16)

- [x] `tests/test_mqtt_live.py` — schema validation + live MQTT integration tests
  - Connects to LAN broker (`10.0.2.123:1883`) or letsmesh (`mqtt-us-v1.letsmesh.net:443/ws`)
  - Subscribes to `meshcore/SEA/+/packets`; validates packet JSON against known schema
  - Live tests: `pytest tests/test_mqtt_live.py -m mqtt`
  - Offline fixture tests: `pytest tests/test_mqtt_live.py -m "not mqtt"`
  - Collect fixtures: `python tests/test_mqtt_live.py --collect-fixtures`
  - Auto-saves fixtures when live tests succeed (for offline fallback)
- [x] `tests/mqtt_test_config.ini` — broker/topic/timeout config (primary: LAN, fallback: letsmesh)
- [x] `tests/fixtures/mqtt_packets.json` — 8 real packets from SEA region (offline fixtures)
- [ ] Add packet content parser tests using fixture data (decode raw hex, validate payload types)

---

## Planned Features

### Bridges

- [ ] **Two-way Discord bridge** — receive messages from Discord and relay to MeshCore
- [ ] **Two-way Telegram bridge** — relay Telegram messages back into MeshCore channels
- [ ] **Telegram `message_thread_id` support** — route bridged messages to forum topics
- [ ] **Bridge DM support** — optional, opt-in bridging of DMs (requires consent mechanism)

### Web Viewer

- [x] (2026-03-15) **Authentication** — `web_viewer_password` in `[Web_Viewer]`; login page + session auth + SocketIO guard
- [x] (2026-03-15) **Radio reboot button** — disconnect + reconnect bot-to-radio from web UI (confirmation modal, operation queue)
- [x] (2026-03-15) **Radio connect/disconnect button** — toggle bot connection from web UI (live status polling via `bot_metadata`)
- [x] (2026-03-15) **Live packet streaming** — Live Activity panel on dashboard; SocketIO packet/command/message feed; pause/clear
- [x] (2026-03-15) **Real-time message monitoring** — `capture_channel_message()` → `packet_stream`; `message_data` SocketIO event; Live Channel Messages panel
- [x] (2026-03-15) **Interactive contact management** — star any contact type; Purge Inactive modal with threshold selector + preview
- [x] (2026-03-15) **Export functionality** — `GET /api/export/contacts` and `/api/export/paths`; CSV/JSON with time-range; Export dropdown in toolbar
- [x] (2026-03-15) **Configuration tab** — `/config` page; SMTP + nightly email toggle; log rotation; DB backup; stored in `bot_metadata`
- [x] (2026-03-15) **Real-time log viewer** — `/logs` page; SocketIO `subscribe_logs`/`log_line`; level-based coloring; pause/clear/filter; log tail thread; "Logs" nav link
- [ ] **Mobile-responsive improvements** — optimize layout for small screens
- [x] (TASK-01 2026-03-15) **Remove firmware config + reboot UI** — radio.html: Firmware Configuration card and Reboot Radio button removed; JS handlers removed; 4 tests added
- [x] (TASK-02 2026-03-15) **Fix realtime stream blank on load** — added 50-row history replay to `subscribe_commands`; fixed `last_timestamp = 0` → `time.time() - 300` in polling thread; 5 tests added (BUG-023 fixed)
- [x] (TASK-03 2026-03-15) **Dashboard: connected agents popup** — `GET /api/connected_clients`; count is clickable link; Bootstrap modal with client table; 5 tests added  ⏸ paused 2026-03-15 20:10 — see SESSION_RESUME.md
- [x] (TASK-04 2026-03-15) **DB backup dir validation on save** — `POST /api/config/maintenance` returns 400 with error if `db_backup_dir` does not exist; inline error in config.html; 5 tests added
- [x] (TASK-06 2026-03-15) **DB backup: Backup Now button** — `POST /api/maintenance/backup_now`; spinner + status in Config tab; 4 tests added
- [x] (TASK-07 2026-03-15) **DB backup: Restore button** — `POST /api/maintenance/restore`; `GET /api/maintenance/list_backups`; SQLite magic-byte validation; modal with path input + backup list; 7 tests added
- [x] (TASK-08 2026-03-15) **Database Operations: purge by age** — `POST /api/maintenance/purge`; keep all/1/7/14/30/60/90 days; confirmation dialog + results table; 7 tests added
- [x] (2026-03-15) (TASK-12) **Dashboard live activity controls** — scroll top/bottom buttons (`live-scroll-top`/`live-scroll-bottom`); type-filter checkboxes (Packets/Commands/Messages) with `data-type` attributes; `applyFilters()` logic hides/shows entries; 1 test added
- [x] (2026-03-16) (TASK-13) **Realtime page scroll/filter** — scroll top/bottom on each stream panel; `[#channel] message` format; type-filter checkboxes; 1 test added
- [x] (2026-03-16) (TASK-16) **Fix blank realtime monitor (BUG-029)** — `app.py` resolved `db_path` relative to hardcoded code root instead of config file directory; fixed via `config_base = Path(config_path).parent.resolve()`; subscribe replay errors elevated DEBUG→WARNING; INFO log of resolved db_path; 4 new tests in `TestDbPathResolutionFromConfigDir`; pre-existing mypy errors fixed in `app.py` and `mesh_graph.py`
- [x] (2026-03-16) (TASK-16b) **BUG-029 follow-up** — `config_base` stored as `self._config_base` instance attribute; dead `_get_db_path()` removed; `subscribe_logs` + `_start_log_tailing` now resolve via `self._config_base`; realtime status badges start as "Connecting…" and update on actual SocketIO connect

### Maintenance and Notifications

- [x] (2026-03-15) **Log rotation configuration** — `log_max_bytes`/`log_backup_count` in `[Logging]`; Config tab Log Rotation card; live-apply via scheduler
- [x] (2026-03-15) **Nightly maintenance email dispatch** — digest every 24h; uptime, contact counts, DB size, log error counts, rotation detection
- [x] (2026-03-15) **Pre-rotation email hook** — `maint.email_attach_log = true` attaches log file (≤ 5 MB) to nightly email
- [x] (2026-03-15) **DB backup scheduling** — `sqlite3.Connection.backup()`; daily/weekly/manual; retention pruning; Config tab Database Backup card
- [x] (2026-03-15) **Maintenance task status API** — `GET /api/maintenance/status`; Maintenance Status card in Config tab

### Commands and Features

- [x] (2026-03-15) **Inbound webhook** — `POST /webhook` relays HTTP payloads to MeshCore channels/DMs; bearer token auth
- [x] (2026-03-15) **Per-channel rate limiting** — `ChannelRateLimiter` in `rate_limiter.py`; `[Rate_Limits] channel.<name>_seconds`; checked in `_check_rate_limits(channel=)`
- [x] (2026-03-15) **Command aliases** — `[Aliases]` config section injects shorthands into command keywords
- [x] (2026-03-15) **Scheduled message preview** — `!schedule` command (DM-only); shows times, channels, message previews, advert interval
- [ ] **`!wx` non-US improvement** — promote `wx_international.py` to default with US fallback
- [x] (2026-03-15) (TASK-11) **Fix help + long response truncation** — `split_text_into_chunks` + `get_max_message_length` in `CommandManager`; keyword dispatch chunks long responses via `send_response_chunked`; mypy fixes across 7 modules + `check_untyped_defs` added to 4 more modules (BUG-026)
- [x] (2026-03-15) **`!path` geographic scoring toggle** — `[Path_Command] geographic_scoring_enabled = true/false` config flag; no restart required

### Infrastructure

- [x] (2026-03-15) **Virtual environment / Makefile** — `make install/dev/test/test-no-cov/lint/fix/deb/config/clean`
- [x] (2026-03-15) **`ruff check` CI gate** — `lint` job in CI; 9262 auto-fixed, legacy patterns in ignore list
- [x] (2026-03-15) **`mypy` strict mode** — incremental: global safe options + per-module `disallow_untyped_defs`; `typecheck` CI job
- [x] (2026-03-15) **HTML/JS test framework** — `package.json` + ESLint (`eslint-plugin-html`) + HTMLHint; `lint-frontend` CI job
- [x] (2026-03-15) **ShellCheck CI gate** — `lint-shell` job checks all `.sh` files at `--severity=warning`
- [x] (2026-03-15) **Database migration versioning** — `MigrationRunner`; 5 numbered migrations; `schema_version` table
- [x] (2026-03-15) **Docker multi-arch build** — `linux/amd64` + `linux/arm64` + `linux/arm/v7`; SBOM + provenance
- [x] (2026-03-15) **Structured JSON logging** — `json_logging = true` in `[Logging]`; `_JsonFormatter`; Loki/ES/Splunk compatible
- [x] (2026-03-15) **aiosqlite async DB** — `AsyncDBManager` in `db_manager.py`; `bot.async_db_manager` in core; `aiosqlite>=0.19.0` dep
- [x] (2026-03-15) **.deb packaging** — `scripts/build-deb.sh`; `DEBIAN/control/postinst/prerm/postrm`; systemd unit; `make deb`
- [x] (2026-03-15) **ncurses config TUI** — `scripts/config_tui.py`; browse/edit/save; validate; migrate from example; `make config`; `r` rename key, `a` add key, `d`/Delete remove key; dynamic sections suppress `?` marker
- [x] (2026-03-15) **APScheduler migration** — `BackgroundScheduler` + `CronTrigger`; replaces `schedule` lib
- [x] (2026-03-15) **Rate-limiter observability** — `GET /api/stats/rate_limiters`; exposes stats for all 4 limiter types
- [x] (2026-03-15) **Map uploader configurable interval** — `min_reupload_interval` in config (fallback 3600 s)
- [x] (2026-03-15) **Per-channel greeter messages** — `channel_greetings`/`per_channel_greetings` config keys
- [x] (2026-03-15) **Radio firmware config UI** — Migration 6 (`payload_data`); `firmware_read`/`firmware_write` op types; `POST /api/radio/firmware/config/read|write`; Firmware Configuration card in web UI
- [x] (2026-03-15) **Werkzeug WebSocket fix** — `_apply_werkzeug_websocket_fix()` patches `SimpleWebSocketWSGI.__call__` at import time; 5 tests
- [x] (2026-03-15) **pytest-timeout runaway prevention** — `pytest-timeout>=2.1.0`; `timeout=30` per test; `asyncio_mode="auto"`
- [x] (2026-03-15) **SMTP timeout** — `SMTP`/`SMTP_SSL` constructed with `timeout=30`; nightly email never hangs
- [x] (2026-03-15) **Real-time monitoring history replay** — `subscribe_packets`/`subscribe_messages`/`subscribe_logs` replay last 50/50/200 items on connect
- [x] **Coverage threshold enforcement** — `fail_under=35` (raised 2026-03-16); raise to 40 once 40% confirmed; target 40% (TASK-14)
- [x] (TASK-09 2026-03-15) **Message processing performance** — write queue + background drain thread; per-packet `sqlite3.connect()` eliminated; `executemany` batch insert every 0.5s; shutdown flushes remaining rows; 6 tests added
- [x] (TASK-05 2026-03-15) **Fix DB backup scheduler interval guard** — `last_db_backup_run` now updated after each call; added 2-min fire window (won't trigger on late startup); seeds last-run from DB on restart; 8 tests added (BUG-024 fixed)
- [ ] (TASK-00) **Fix meshcore IndexError crash** — asyncio exception handler for `IndexError` from meshcore parser (BUG-022)  ⏸ paused 2026-03-15 19:19 — see SESSION_RESUME.md
- [x] (TASK-10 2026-03-15) **Retry `no_event_received` channel sends** — `_is_no_event_received()` helper + retry loop in `send_channel_message` (max 2 retries, 2s delay); 5 tests added (BUG-025 fixed)
- [x] (TASK-INFRA 2026-03-15) **Context checkpoint system** — `scripts/context_checkpoint.sh`, `scripts/post_tool_counter.sh`, `.claude/hooks.json`; cron every 15 min

---

## Backlog

- [ ] Evaluate moving web viewer to a separate installable package
- [ ] Repeater auto-purge dry-run mode — log what would be purged without acting
- [ ] Feed manager: add support for JSON API feeds (not just RSS/Atom)
- [ ] Mobile-responsive web viewer improvements — optimize layout for small screens

---

## Recently Completed

- [x] (2026-03-15) Radio firmware config UI — Migration 6 (`payload_data`); `firmware_read`/`firmware_write` op types; `POST /api/radio/firmware/config/read|write`; Firmware Configuration card (path.hash.mode + loop.detect)
- [x] (2026-03-15) APScheduler migration — `BackgroundScheduler` + `CronTrigger`; removes `schedule` lib dependency
- [x] (2026-03-15) Rate-limiter observability — `GET /api/stats/rate_limiters`; all 4 limiter types exposed
- [x] (2026-03-15) Map uploader configurable interval — `min_reupload_interval` config key (fallback 3600 s)
- [x] (2026-03-15) Per-channel greeter messages — `channel_greetings`/`per_channel_greetings` config keys
- [x] (2026-03-15) `!path` geographic scoring toggle — `[Path_Command] geographic_scoring_enabled = true/false`; tests in `test_path_geo_toggle.py`
- [x] (2026-03-15) Real-time monitoring history replay — last 50/50/200 items replayed on `subscribe_packets`/`subscribe_messages`/`subscribe_logs`
- [x] (2026-03-15) Werkzeug WebSocket fix — `_apply_werkzeug_websocket_fix()` patches `SimpleWebSocketWSGI.__call__`; 5 tests
- [x] (2026-03-15) Radio reboot firmware command — sends `meshcore.commands.reboot()` before disconnect; 8 s wait; 10 s disconnect timeout
- [x] (2026-03-15) pytest-timeout — `pytest-timeout>=2.1.0`; `timeout=30` per test; `asyncio_mode="auto"`
- [x] (2026-03-15) SMTP timeout — `timeout=30` on all `SMTP`/`SMTP_SSL` constructors; nightly email no longer hangs
- [x] (2026-03-15) Per-channel rate limiting — `ChannelRateLimiter`; `[Rate_Limits]` config section; integrated into `_check_rate_limits` and `send_channel_message`
- [x] (2026-03-15) Real-time log viewer — `/logs` page; SocketIO `subscribe_logs`/`log_line`; log tail background thread; "Logs" nav link; toggle from `/realtime`
- [x] (2026-03-15) HTML/JS test framework — `package.json`, ESLint + `eslint-plugin-html`, HTMLHint; `lint-frontend` CI job
- [x] (2026-03-15) ShellCheck CI gate — `lint-shell` job; all `.sh` files checked at `--severity=warning`
- [x] (2026-03-15) .deb packaging — `scripts/build-deb.sh`; DEBIAN control/postinst/prerm/postrm; systemd unit; `make deb`
- [x] (2026-03-15) aiosqlite `AsyncDBManager` — `db_manager.py`; `aiosqlite>=0.19.0`; `bot.async_db_manager` in core
- [x] (2026-03-15) ncurses config TUI — `scripts/config_tui.py`; read/create/edit/save/validate/migrate; `make config`; `r`/`a`/`d` key management; dynamic-section `?` fix
- [x] (2026-03-15) Makefile — added `make deb` and `make config` targets; `clean` now removes `dist/deb-build/`
- [x] (2026-03-15) .gitignore — added `node_modules/`, `.npm`, `package-lock.json`, `dist/deb-build/`
- [x] (2026-03-15) Export functionality — `GET /api/export/contacts` + `/api/export/paths`; CSV/JSON with time-range; Export dropdown in contacts.html
- [x] (2026-03-15) Live packet streaming — Live Activity panel in `index.html`; SocketIO color-coded feed with pause/clear
- [x] (2026-03-15) Real-time message monitoring — `capture_channel_message()` → `packet_stream`; `message_data` SocketIO event; Live Channel Messages panel
- [x] (2026-03-15) Maintenance status API — `GET /api/maintenance/status`; Maintenance Status card in Config tab
- [x] (2026-03-15) DB backup scheduling — `scheduler._run_db_backup()`; daily/weekly/manual; retention pruning; Config tab card; status in `maint.status.*`
- [x] (2026-03-15) Pre-rotation email hook — `maint.email_attach_log = true` attaches log file (≤ 5 MB) to nightly digest
- [x] (2026-03-15) Log rotation configuration — `log_max_bytes`/`log_backup_count` in `[Logging]`; Config tab card; live-apply via scheduler
- [x] (2026-03-15) Nightly email dispatch — `_send_nightly_email()` every 24h; uptime, contacts, DB size, log errors; STARTTLS/SSL/plain
- [x] (2026-03-15) Configuration tab — `/config` page; `GET/POST /api/config/notifications`; SMTP settings stored as `notif.*` in `bot_metadata`
- [x] (2026-03-15) Interactive contact management — star all contact types; `GET /api/contacts/purge-preview` + `POST /api/contacts/purge`; Purge Inactive modal
- [x] (2026-03-15) Structured JSON logging — `json_logging = true`; `_JsonFormatter`; Loki/Elasticsearch/Splunk compatible
- [x] (2026-03-15) Radio connect/disconnect button — `GET /api/radio/status`; `POST /api/radio/connect`; live status from `bot_metadata`
- [x] (2026-03-15) Radio reboot button — `POST /api/radio/reboot` queues `radio_reboot` op; scheduler calls `reconnect_radio()`
- [x] (2026-03-15) Docker multi-arch — QEMU; `linux/amd64` + `linux/arm64` + `linux/arm/v7`; SBOM + provenance
- [x] (2026-03-15) mypy incremental strict mode — global safe options + per-module `disallow_untyped_defs`; `typecheck` CI job
- [x] (2026-03-15) ruff CI gate — clean pass; `lint` CI job; 9262 auto-fixed
- [x] (2026-03-15) Database migration versioning — `MigrationRunner`; 5 numbered migrations; `schema_version` table
- [x] (2026-03-15) Command aliases — `[Aliases]` config section injects shorthands into keywords
- [x] (2026-03-15) Inbound webhook service — `POST /webhook`; bearer token auth; relay to channel or DM
- [x] (2026-03-15) Makefile — `make install/dev/test/test-no-cov/lint/fix/clean`
- [x] (2026-03-15) Fixed BUG-001 web viewer authentication (Flask session auth, login/logout, SocketIO guard)
- [x] (2026-03-15) Fixed BUG-002 DB migration missing columns (channel_operations, feed_message_queue)
- [x] (2026-03-15) Fixed BUG-003 geocoding rate-limit skip logged at INFO with full context
- [x] (2026-03-15) Fixed `RepeaterManager` ignoring `auto_manage_contacts = false`
- [x] (2026-03-15) Fixed timezone handling in `format_elapsed_display` (issue #75)
- [x] (2026-03-15) Fixed `TraceCommand` reversed path nodes and truncated return paths
- [x] (2026-03-15) Fixed shutdown log spam after streams closed
- [x] (2026-03-15) Added Discord and Telegram one-way bridges
- [x] (2026-03-15) Added chunked message sending for rate-limit-aware large responses
- [x] (2026-03-15) Multi-byte prefix (2-byte) support throughout codebase
- [x] (2026-03-15) Added `ScheduleCommand` — lists scheduled messages and advert interval (DM-only by default)
- [x] (2026-03-15) Created 10 test modules covering enums, models, transmission_tracker, message_handler, repeater_manager, core, feed_manager, scheduler_logic, command_manager, channel_manager_logic

---

## Auto-Update

The **Inline TODOs** section below is auto-generated by scanning source files for
`# TODO`, `# FIXME`, and `# HACK` markers. Regenerate it with:

```bash
python scripts/update_todos.py
```

The script also updates the `**Last updated:**` date at the top of this file.

Or run it as part of a pre-commit hook by adding to `.pre-commit-config.yaml`:

```yaml
- repo: local
  hooks:
    - id: update-todos
      name: Update TODO.md inline scan
      language: python
      entry: python scripts/update_todos.py
      pass_filenames: false
```

---

## Inline TODOs (auto-generated)

> _Last scanned: 2026-03-15. No `# TODO`, `# FIXME`, or `# HACK` markers
> found in `modules/` or `tests/`. Run `python scripts/update_todos.py` to refresh._

