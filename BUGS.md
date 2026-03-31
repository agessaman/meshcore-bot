# BUGS

Tracking of known bugs, fixed issues, and outstanding defects in meshcore-bot.

---

## Fixed Bugs

### v0.9.0 and recent history

| Commit | Summary |
|--------|---------|
| `e0eae09` (PR #123) | Fixed CI failures introduced by v0.9.0 push: (1) ruff — fixed import order, `Dict`→`dict`, and unused variable in `discord_bridge_service.py`; (2) mypy — added `types-requests` stub package to test deps, added `ignore_errors = true` per-module overrides for 19 not-yet-annotated modules; (3) ShellCheck SC2034 (unused vars) in `install-service.sh` / `uninstall-service.sh`, SC2155 (declare+assign) in `install-service.sh` / `restart_viewer.sh`, SC2010 (`ls\|grep`) replaced with glob loops in `docker-setup.sh`, SC2320 (`$?` capture after heredoc) in `docker-setup.sh` |
| `92c5910` (PR #122) | Removed Python 3.9 from CI test matrix — `meshcore >=2.2.31` requires Python >=3.10 and is not installable on 3.9 |
| `164dbae` | Refactored command aliases from global `[Aliases]` config section to per-command `aliases =` key in each command's own config section; `BaseCommand._load_aliases_from_config()` reads and injects aliases at startup; `CommandManager.load_aliases()` and `_apply_aliases()` removed |
| `f971e97` | Fixed pre-existing test failure in `test_discord_bridge_multi_webhooks.py`: `ConfigParser` lowercases all config keys so `bridge.Public` is stored as `"public"` — test assertions updated to match actual lowercase key behaviour; runtime matching was already case-insensitive |
| `26d18c1` | Fixed BUG-029 (third pass): Realtime monitor panels stuck at "Connecting…" — root cause was `<script type="module">` in `realtime.html` creating a second Socket.IO manager that raced with `base.html`'s `forceNew: true` manager; the module-socket's `connect` event never fired. Also: `ping_timeout=5` (5 s) was too short for subscribe handlers that replay DB history; `subscribed_messages` key was missing from `connected_clients` initial dict. Fixed: changed `<script type="module">` to regular `<script>` with dynamic `import()` for the decoder; removed `forceNew: true` from `base.html` so both pages share one Socket.IO manager; raised `ping_timeout` 5→20 s; added `subscribed_messages: False` to client tracking dict. |
| `26d18c1` | Fixed BUG-029 (second pass): `config_base` was a local variable — stored as `self._config_base` instance attribute; removed dead `_get_db_path()` method that still used `self.bot_root`; fixed `subscribe_logs` and `_start_log_tailing` to resolve log file path via `self._config_base` instead of `self.bot_root`; fixed misleading hardcoded "Connected"/"Active" status badges in `realtime.html` (now start as "Connecting…" and update dynamically on actual SocketIO connect). |
| `26d18c1` | Fixed BUG-029 (first pass): `app.py` resolved `db_path` relative to the code root (2 dirs above `app.py`) instead of relative to the config file's parent directory, causing the web viewer and bot to open different database files. Fixed: `config_base = Path(config_path).parent.resolve()` used as base for `resolve_path()`; also elevated subscribe-handler replay errors from DEBUG to WARNING and added INFO log of resolved db_path on startup. 4 new tests in `TestDbPathResolutionFromConfigDir`. |
| `26d18c1` | Fixed BUG-027: `test_weekly_on_wrong_day_does_not_run` was patching `get_current_time` with real `now` (Monday) instead of `fake_now` (mocked Tuesday) — test always passed on non-Monday but failed on Mondays; fixed in `tests/test_scheduler_logic.py:430` |
| `26d18c1` | Fixed BUG-025: `send_channel_message` did not retry on `no_event_received` — added retry loop in `command_manager.py` (max 2 retries, 2s delay); `_is_no_event_received()` helper; 5 tests |
| `ab72be9` | Fixed BUG-024: DB backup scheduler fired every second — `last_db_backup_run` now updated after each call; 2-min fire window prevents triggering on late startup; last-run seeded from DB on restart |
| `ab72be9` | Fixed BUG-023: Realtime monitoring command stream blank on load — added 50-row history replay to `subscribe_commands` handler; fixed `last_timestamp = 0` → `time.time() - 300` in polling thread |
| `ab72be9` | Fixed BUG-022: `IndexError` from meshcore parser silently discarded in asyncio task — installed custom loop exception handler in `core.py:start()` to suppress at DEBUG level |
| `ab72be9` | Fixed BUG-020: Config TUI `[Scheduled_Messages]` keys showed `?` marker — dynamic sections (no fixed example keys) now suppress unknown-key marker |
| `ab72be9` | Fixed BUG-021: Config TUI had no way to edit the time portion of a scheduled message — added `r` (rename key), `a` (add key+value), `d`/Delete (delete key with confirmation) bindings in keys pane |
| `ab72be9` | Fixed BUG-019: `addPacketEntry` in `realtime.html` crashed with TypeError when `data.path` is a string — guarded with `Array.isArray()` |
| `ab72be9` | Fixed BUG-018: Real-time monitoring SocketIO connections dropped due to Werkzeug 3.1 WebSocket teardown assertion (see BUG-012 fix); DB polling and subscription replay confirmed correct |
| `ab72be9` | Fixed BUG-017: `disconnect_radio()` now uses `asyncio.wait_for(..., timeout=10)` — no longer hangs indefinitely |
| `ab72be9` | Fixed BUG-016: `reboot_radio()` now sends `meshcore.commands.reboot()` firmware command before disconnecting/reconnecting |
| `ab72be9` | Fixed BUG-015: scheduler thread blocked on `future.result(timeout=X)` causing `TimeoutError` spam and stalling the loop — replaced all four blocking waits with `add_done_callback` (fire-and-forget) in `run_scheduler` |
| `ab72be9` | Fixed BUG-001: web viewer now supports optional password authentication via `web_viewer_password` in `[Web_Viewer]` config |
| `ab72be9` | Fixed BUG-002: `db_manager` now runs ALTER TABLE migrations for `channel_operations` (`result_data`, `processed_at`) and `feed_message_queue` (`item_id`, `item_title`, `priority`) on startup |
| `ab72be9` | Fixed BUG-003: geocoding rate-limit skip in `repeater_manager` now logs at INFO level instead of DEBUG so it is visible in production logs |
| `1264f49` | Fixed repeater manager auto-purge ignoring `auto_manage_contacts` config — purge ran unconditionally regardless of setting (issue #50) |
| `1264f49` | Fixed web viewer responses returning stale or incorrect repeater data |
| `5c8ee35` | Fixed timezone handling in `format_elapsed_display` — elapsed times displayed incorrectly in non-UTC timezones (issue #75) |
| `1cc41bc` | Fixed repeater usage and web viewer response formatting (PR #67) |
| `1474174` | Fixed `TraceCommand` path truncation — return paths were being cut short |
| `1a576e8` | Fixed reversed path nodes in `TraceCommand` — trace direction was backwards |
| `5a96dec` | Fixed incorrect hop labeling logic in `TraceCommand` |
| `2178a80` | Fixed log spam during shutdown — cleanup methods logged errors after streams were closed |
| `e9f17ec` | Fixed incomplete shutdown — scheduler thread and meshcore disconnect were not always joined cleanly |
| `217d2a4` | Fixed database connection handling across modules — connections were not always properly closed |
| `d084c6b` | Fixed `PrefixCommand` not supporting multi-byte hex prefix lookups |
| `6c81513` | Fixed `MeshGraph` edge promotion logic — edges were not promoted correctly under some conditions |
| `36a8a67` | Fixed prefix handling incompatibility when transitioning from 1-byte to 2-byte prefixes |
| `0c060a5` | Fixed chunked message sending race with rate limiter — second chunk could be blocked |
| `58deb12` | Fixed `RepeaterManager` ignoring `auto_manage_contacts = false` |
| `unreleased` | Fixed BUG-028: `decode_meshcore_packet()` no longer throws `UnboundLocalError` when `bytes.fromhex()` fails (invalid hex input now cleanly returns `None`) |

---

## Outstanding Known Issues

### High Priority

| ID | Task | Module | Description | Workaround |
|----|------|--------|-------------|------------|
| ~~BUG-025~~ | TASK-10 ✅ | Fixed — see Fixed section above |

### Medium Priority

| ID | Task | Module | Description | Workaround |
|----|------|--------|-------------|------------|
| BUG-004 | `message_handler` | RF data correlation (SNR/RSSI) can miss messages if the RF log event arrives more than `rf_data_timeout` (default 15s) after the message | Increase `rf_data_timeout` in `[Bot]` config |
| BUG-005 | `scheduler` | On Raspberry Pi Zero 2 W, bot + web viewer together use ~300 MB RAM, leaving little headroom under load | Disable web viewer (`[Web_Viewer] enabled = false`) or tune mesh graph settings (`graph_startup_load_days = 7`) |
| BUG-006 | `feed_manager` | Stale rows in `feed_message_queue` from an old install can cause repeated queue-processing errors after a database migration (note: scheduler `TimeoutError` spam from the same area is fixed — see BUG-015) | Clear pending queue: `DELETE FROM feed_message_queue WHERE sent_at IS NULL` |
| ~~BUG-007~~ | `discord_bridge_service` | Closed / won’t fix — no changes planned | Non-issue; Discord webhook rate limit is expected behavior—keep bridged channels low-traffic or rate-limit upstream |
| BUG-008 | `telegram_bridge_service` | Telegram `message_thread_id` (forum/topic support) is not implemented — messages go to the main group channel only | Manual: add thread ID mapping in a future plugin iteration |

### Low Priority / By Design

| ID | Module | Description | Notes |
|----|--------|-------------|-------|
| ~~BUG-029~~ | TASK-16/T1-A ✅ | Fixed (third pass) — see Fixed section above | |
| ~~BUG-028~~ | `message_handler` | Fixed — see Fixed section above | |
| BUG-026 | `message_handler` | Keyword-dispatched help/command responses are sent as a single message (no auto-chunking). Long responses may be truncated by transport limits to avoid sending extra multipart messages. | Design choice. Commands can explicitly use `send_response_chunked()` if they want multi-part replies. |
| ~~BUG-009~~ | `discord_bridge_service` | Closed / won’t fix — no changes planned | Non-issue (intentionally excluded); DMs contain private communications |
| ~~BUG-010~~ | `wx_command` | Closed / won’t fix — no changes planned | Non-issue; use `wx_international.py` alternative in `modules/commands/alternatives/` for non-US deployments |
| BUG-011 | `repeater_manager` | MeshCore device hard-limits contacts to 300; auto-purge threshold is 280 — purging 20 contacts at a time may not be enough on very busy meshes | Tune `auto_purge_threshold` and ensure `auto_manage_contacts` is enabled |
| ~~BUG-012~~ | `plugin_loader` | Closed / won’t fix — no changes planned | Non-issue; rename local plugin to a unique name |
| ~~BUG-013~~ | `core.py` | Closed / won’t fix — no changes planned | Non-issue; upgrade firmware if you need those features |
| BUG-014 | `packet_capture_service` | Packet hash calculation silently uses a default hash value on failure (`pass  # Use default hash if calculation fails`) | Low impact; affects deduplication accuracy only |

---

## Reporting New Bugs

Open an issue at the project repository. Include:
- Bot version (`git describe --tags`)
- Relevant section of `config.ini` (redact keys/tokens)
- Log output (`logs/meshcore_bot.log`) around the time of the issue
- Steps to reproduce
