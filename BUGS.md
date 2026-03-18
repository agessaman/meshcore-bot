# BUGS

Tracking of known bugs, fixed issues, and outstanding defects in meshcore-bot.

---

## Fixed Bugs

### v0.9.0 and recent history

| Commit | Summary |
|--------|---------|
| (dev TASK-02 2026-03-15) | Fixed BUG-023: Realtime monitoring command stream blank on load — added 50-row history replay to `subscribe_commands` handler; fixed `last_timestamp = 0` → `time.time() - 300` in polling thread |
| (dev TASK-00 2026-03-15) | Fixed BUG-022: `IndexError` from meshcore parser silently discarded in asyncio task — installed custom loop exception handler in `core.py:start()` to suppress at DEBUG level |
| (dev) | Fixed BUG-020: Config TUI `[Scheduled_Messages]` keys showed `?` marker — dynamic sections (no fixed example keys) now suppress unknown-key marker |
| (dev) | Fixed BUG-021: Config TUI had no way to edit the time portion of a scheduled message — added `r` (rename key), `a` (add key+value), `d`/Delete (delete key with confirmation) bindings in keys pane |
| (dev) | Fixed BUG-019: `addPacketEntry` in `realtime.html` crashed with TypeError when `data.path` is a string — guarded with `Array.isArray()` |
| (dev) | Fixed BUG-018: Real-time monitoring SocketIO connections dropped due to Werkzeug 3.1 WebSocket teardown assertion (see BUG-012 fix); DB polling and subscription replay confirmed correct |
| (dev) | Fixed BUG-017: `disconnect_radio()` now uses `asyncio.wait_for(..., timeout=10)` — no longer hangs indefinitely |
| (dev) | Fixed BUG-016: `reboot_radio()` now sends `meshcore.commands.reboot()` firmware command before disconnecting/reconnecting |
| (dev) | Fixed BUG-015: scheduler thread blocked on `future.result(timeout=X)` causing `TimeoutError` spam and stalling the loop — replaced all four blocking waits with `add_done_callback` (fire-and-forget) in `run_scheduler` |
| `0ef4424` | Fixed BUG-001: web viewer now supports optional password authentication via `web_viewer_password` in `[Web_Viewer]` config |
| `0ef4424` | Fixed BUG-002: `db_manager` now runs ALTER TABLE migrations for `channel_operations` (`result_data`, `processed_at`) and `feed_message_queue` (`item_id`, `item_title`, `priority`) on startup |
| `0ef4424` | Fixed BUG-003: geocoding rate-limit skip in `repeater_manager` now logs at INFO level instead of DEBUG so it is visible in production logs |
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

---

## Outstanding Known Issues

### High Priority

| ID | Task | Module | Description | Workaround |
|----|------|--------|-------------|------------|
| BUG-022 | TASK-00 | `core.py` / `meshcore_parser` | `IndexError: index out of range` in `meshcore/meshcore_parser.py:parsePacketPayload` surfaces as "Task exception was never retrieved" WARNING spam — malformed/truncated packet payload; no custom exception handler installed | None; fix: install `loop.set_exception_handler()` to suppress `IndexError`/`struct.error` at DEBUG level |

### Medium Priority

| ID | Task | Module | Description | Workaround |
|----|------|--------|-------------|------------|
| BUG-024 | TASK-05 | `scheduler.py` | DB backup scheduler fires every second after the 5-minute startup window — `last_db_backup_run` is never updated in `run_scheduler()` loop body after calling `_maybe_run_db_backup()` | None; fix: assign `self.last_db_backup_run = time.time()` after the call |
| BUG-025 | TASK-10 | `message_handler.py` | Channel message send fails with `{'reason': 'no_event_received'}` and is not retried — seen in logs as `❌ Channel message failed to #testing` | Restart bot; fix: wrap send in retry loop (max 2 retries, 2s delay) |
| BUG-026 | TASK-11 | `command_manager.py` | Help and long bot responses are cut off — chunking logic does not send all parts | None; fix: audit `split_message`/chunking; ensure all chunks sent sequentially |
| BUG-004 | `message_handler` | RF data correlation (SNR/RSSI) can miss messages if the RF log event arrives more than `rf_data_timeout` (default 15s) after the message | Increase `rf_data_timeout` in `[Bot]` config |
| BUG-005 | `scheduler` | On Raspberry Pi Zero 2 W, bot + web viewer together use ~300 MB RAM, leaving little headroom under load | Disable web viewer (`[Web_Viewer] enabled = false`) or tune mesh graph settings (`graph_startup_load_days = 7`) |
| BUG-006 | `feed_manager` | Stale rows in `feed_message_queue` from an old install can cause repeated queue-processing errors after a database migration (note: scheduler `TimeoutError` spam from the same area is fixed — see BUG-015) | Clear pending queue: `DELETE FROM feed_message_queue WHERE sent_at IS NULL` |
| BUG-007 | `discord_bridge_service` | Discord webhook rate limit is 30 requests/minute; bot warns at 20% exhaustion but does not queue excess messages — they are dropped | Keep bridged channels low-traffic; consider rate-limiting at mesh level |
| BUG-008 | `telegram_bridge_service` | Telegram `message_thread_id` (forum/topic support) is not implemented — messages go to the main group channel only | Manual: add thread ID mapping in a future plugin iteration |

### Low Priority / By Design

| ID | Module | Description | Notes |
|----|--------|-------------|-------|
| BUG-009 | `discord_bridge_service` | DMs are never bridged to Discord or Telegram — hardcoded exclusion | By design; DMs contain private communications |
| BUG-010 | `wx_command` | Weather alerts and NOAA data are US-only | Use `wx_international.py` alternative in `modules/commands/alternatives/` for non-US deployments |
| BUG-011 | `repeater_manager` | MeshCore device hard-limits contacts to 300; auto-purge threshold is 280 — purging 20 contacts at a time may not be enough on very busy meshes | Tune `auto_purge_threshold` and ensure `auto_manage_contacts` is enabled |
| BUG-012 | `plugin_loader` | Local plugins with the same name as a built-in plugin are skipped — no override-by-name is possible | Rename your local plugin to a unique name |
| BUG-013 | `core.py` | Some older MeshCore firmware versions do not support `get_time` or `set_name` commands — bot logs a warning and continues without those features | Upgrade firmware; no functional impact on message processing |
| BUG-014 | `packet_capture_service` | Packet hash calculation silently uses a default hash value on failure (`pass  # Use default hash if calculation fails`) | Low impact; affects deduplication accuracy only |

---

## Reporting New Bugs

Open an issue at the project repository. Include:
- Bot version (`git describe --tags`)
- Relevant section of `config.ini` (redact keys/tokens)
- Log output (`logs/meshcore_bot.log`) around the time of the issue
- Steps to reproduce
