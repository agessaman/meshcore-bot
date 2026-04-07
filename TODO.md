# TODO

Task list for meshcore-bot development. Auto-updated sections are regenerated
by running `python scripts/update_todos.py` (see [Auto-Update](#auto-update)).

**Last updated:** 2026-04-05 — 6 PRs open against agessaman:dev (#138–#143); CI green on ruff/mypy/ShellCheck; coverage at 41.01% (`fail_under=40` enforced)

---

## In Progress

- [x] TASK-14: Push test coverage to ≥40% — **achieved** (41.01%); `fail_under=40` enforced

  **Remaining optional coverage sub-tasks (backlog — not blocking):**

  **Tier 1:**
  - [ ] T1-F: `web_viewer/app.py` (46%, ~2,280 uncovered) — greeter, bans, packets/messages endpoints, export, SocketIO, firmware routes

  **Tier 2:**
  - [ ] T2-A: `utils.py` (63%, ~419 uncovered) — `format_keyword_response`, `calculate_path_distances`, `get_major_city_queries`
  - [ ] T2-C: `db_manager.py` (53%, ~170 uncovered) — `AsyncDBManager` async methods, write queue, `executemany` batch
  - [ ] T2-D: `discord_bridge_service.py` (46%, ~195 uncovered) — message formatting, webhook dispatch, rate-limit warn
  - [ ] T2-E: `telegram_bridge_service.py` (36%, ~195 uncovered) — message relay, topic routing, listener lifecycle
  - [ ] T2-F: `greeter_command.py` (15%, ~563 uncovered) — greeting detection, per-channel greetings, new-contact detection

  **Tier 3:**
  - [ ] T3-I: `trace_runner.py` (24%, ~50 uncovered) — trace execution, path assembly
  - [ ] T3-J: `earthquake_service.py` (16%, ~119 uncovered) — alert threshold, message format (USGS API mockable)
  - [ ] T3-M: `sports_command.py` (16%, ~325 uncovered) — score formatting, schedule display
  - [ ] T3-O: `repeater_command.py` (10%, ~357 uncovered) — repeater list/info formatting
  - [ ] T4-A: `multitest_command.py` (52%, ~336 uncovered) — multi-channel test sequences; pure logic + async

  **Tier 4 — API/hardware heavy, skip for now:**
  - `weather_service.py` (17%), `solar_conditions.py` (7%), `solarforecast_command.py` (8%), `packet_capture_service.py` (9%), `map_uploader_service.py` (10%), `airplanes_command.py` (10%), `aqi_command.py` (11%), `alert_command.py` (13%), `prefix_command.py` (10%), `packet_capture_utils.py` (12%)

---

## MQTT Test Framework

- [x] `tests/test_mqtt_live.py` — schema validation + live MQTT integration tests
- [x] `tests/mqtt_test_config.ini` — broker/topic/timeout config
- [x] `tests/fixtures/mqtt_packets.json` — 8 real packets from SEA region (offline fixtures)
- [ ] Add packet content parser tests using fixture data (decode raw hex, validate payload types)

---

## Planned Features

### Bridges

- [ ] **Telegram `message_thread_id` support** — route bridged messages to forum topics
- [ ] **Bridge DM support** — optional, opt-in bridging of DMs (requires consent mechanism)

---

## Backlog

- [ ] Evaluate moving web viewer to a separate installable package
- [ ] Repeater auto-purge dry-run mode — log what would be purged without acting (geolocation `dry_run` exists; purge path needs it)

---

## Completed by upstream (agessaman/meshcore-bot:dev)

Items below were open in this TODO and have since been completed by upstream commits.

- [x] **Fix meshcore IndexError crash** (TASK-00 / BUG-022) — `core.py` asyncio exception handler now suppresses `IndexError`/`struct.error` from meshcore parser (`ce884ce`)
- [x] **Mobile-responsive web viewer** — contacts page fully redesigned with mobile-friendly toolbar, search, and touch controls (`4685ea7`, `da2e39c`)
- [x] **`!wx` non-US improvement** — `wx_command.py` delegates to `wx_international` when `weather_provider = openmeteo` in config (`5f6eced`, `9d768a3`)
- [x] **Feed manager JSON API feeds** — `feed_manager.py` handles `feed_type = api` with configurable headers, params, and body (`5a04c1c`)
- [x] **Feed manager polling loop** (T1-E) — `poll_all_feeds()` with per-feed interval checking; `process_message_queue()` for rate-controlled dispatch; driven by scheduler every 60 s / 2 s
- [x] **graph_trace_helper.py coverage** (T2-B) — `test_graph_trace_helper.py` now at 93%; upstream added comprehensive test suite
- [x] **channels_command.py coverage** (T3-C) — 97% covered; upstream `test_channels_command.py` covers all remaining paths
- [x] **hacker_command.py coverage** (T3-L) — 100% covered by upstream `test_hacker_command.py`
- [x] **Version command** — `!version` command + `modules/version_info.py` shared runtime resolver for bot and web viewer (`883b67d`)
- [x] **Channel pause command** — `!channelpause`/`!channelresume` admin DM commands to suspend bot responses on public channels (`7a3bb56`)
- [x] **CSRF protection in web viewer** — CSRF tokens + security headers added to `app.py` (`3a9f710`)
- [x] **Reconnect logic** — automatic radio reconnect on connection loss (`6a9e0ec`, merged `#134`)
- [x] **URL shortening in feed manager** — configurable URL shortener for feed message output (`5a04c1c`)
- [x] **Feed filter date-based operators** — `within_days` and related date operators in filter config (`e0fc2ac`)
- [x] **Unix signal handling** — SIGHUP triggers in-process config reload; SIGTERM/SIGINT graceful shutdown (`aa2677b`)
- [x] **Per-user rate limiting** — per-sender rate limit (default 30 s) across all commands (`883b67d`)
- [x] **MQTT weather support** — `weather_service.py` and `wx_command.py` support MQTT broker as weather data source (`9d768a3`, `48fd462`)
- [x] **Open-Meteo model selection** — `[Wx_Command] weather_model` config key for model selection (`5f6eced`)
- [x] **Bot location fallback for weather** — uses bot's configured location when no user location provided (`022053a`)
- [x] **Extended multiday forecasts** — `!wx` supports configurable multi-day forecast output (`5020120`)
- [x] **max_response_hops** — `[Bot] max_response_hops` limits relay depth for bot replies (`8966be8`)
- [x] **New commands** — `!catfact` (easter egg), `!dice` (D&D dice roller), `!cmd` (compact command list), `!advert` (manual flood advert)

---

## Auto-Update

The **Inline TODOs** section below is auto-generated by scanning source files for
`# TODO`, `# FIXME`, and `# HACK` markers. Regenerate it with:

```bash
python scripts/update_todos.py
```

---

## Inline TODOs (auto-generated)

> _Last scanned: 2026-04-05. No `# TODO`, `# FIXME`, or `# HACK` markers
> found in `modules/` or `tests/`. Run `python scripts/update_todos.py` to refresh._
