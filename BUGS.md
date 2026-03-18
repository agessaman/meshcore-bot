# BUGS

Tracking of known bugs, fixed issues, and outstanding defects in meshcore-bot.

---

## Fixed Bugs

### v0.8.3 and recent history

| Commit | Summary |
|--------|---------|
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

| ID | Module | Description | Workaround |
|----|--------|-------------|------------|
| BUG-001 | `web_viewer` | Web viewer has **no authentication** — exposes all contact data and packet history on the configured host/port | Set `host = 127.0.0.1` in `[Web_Viewer]` config; use firewall rules or SSH tunnel for remote access |
| BUG-002 | `db_manager` | Moving a database from an older install can cause `"no such column"` errors at startup if schema migrations haven't run yet | Start bot once to trigger migrations; if errors persist, clear stale `feed_message_queue` and `channel_operations` rows |
| BUG-003 | `repeater_manager` | Geocoding runs at most once per packet hash within a 60-second window, but concurrent adverts from the same node within that window skip geocoding silently | None; by design for rate limiting, but location may lag for rapid-fire adverts |

### Medium Priority

| ID | Module | Description | Workaround |
|----|--------|-------------|------------|
| BUG-004 | `message_handler` | RF data correlation (SNR/RSSI) can miss messages if the RF log event arrives more than `rf_data_timeout` (default 15s) after the message | Increase `rf_data_timeout` in `[Bot]` config |
| BUG-005 | `scheduler` | On Raspberry Pi Zero 2 W, bot + web viewer together use ~300 MB RAM, leaving little headroom under load | Disable web viewer (`[Web_Viewer] enabled = false`) or tune mesh graph settings (`graph_startup_load_days = 7`) |
| BUG-006 | `feed_manager` | Stale rows in `feed_message_queue` from an old install can cause repeated scheduler errors after a database migration | Clear pending queue: `DELETE FROM feed_message_queue WHERE sent_at IS NULL` |
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
