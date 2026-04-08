# Testing

Complete reference for the meshcore-bot test suite: how to run tests, what each file covers, and how to extend coverage.

---

## Quick Start

```bash
# Create venv and install all dependencies (first time only)
make dev

# Run the full test suite
make test

# Run without coverage (faster)
make test-no-cov

# Run a specific file
.venv/bin/pytest tests/test_enums.py -v

# Run a specific test class or function
.venv/bin/pytest tests/test_message_handler.py::TestShouldProcessMessage -v
.venv/bin/pytest tests/test_enums.py::TestPayloadType::test_lookup_by_value -v

# Run and stop on first failure
.venv/bin/pytest -x
```

---

## Configuration

Test configuration lives in two files:

**`pytest.ini`** — controls pytest behaviour:

| Setting | Value |
|---------|-------|
| `testpaths` | `tests` |
| `asyncio_mode` | `auto` — all async tests run automatically, no `@pytest.mark.asyncio` required on each |
| `addopts` | `-v --tb=short --strict-markers --cov=modules --cov-report=term-missing` |
| Registered markers | `unit`, `integration`, `slow`, `mqtt` |

**`pyproject.toml`** — coverage settings (`[tool.coverage.*]`):

| Setting | Value |
|---------|-------|
| `source` | `modules/` |
| `omit` | `tests/`, `.venv/` |
| `fail_under` | 35% (raised 2026-03-16; current coverage 36.72%; target 40%; hardware-dependent modules cap realistic ceiling at ~40–42%) |
| Setting      | Value                                                           |
|--------------|-----------------------------------------------------------------|
| `source`     | `modules/`                                                      |
| `omit`       | `tests/`, `.venv/`                                              |
| `fail_under` | `40` — raised 2026-03-22; currently **41.1%** as of 2026-04-07  |
|              | (hardware-dependent modules cap realistic ceiling at ~40-42%)   |

---

## Coverage Analysis (2026-04-07)

Full run: **2614 passed, 31 skipped — 41.1% overall** (2645 collected).

### Well-covered (≥85%) — no action needed
`i18n` 98%, `rate_limiter` 97%, `stats_command` 97%, `announcements_command` 96%,
`schedule_command` 95%, `security_utils` 93%, `graph_trace_helper` 93%,
`aurora_command` 87%, `trace_command` 87%.

### High-value improvement targets

| Module | Cover | Notes |
|--------|-------|-------|
| `maintenance.py` | 71% *(was 34%)* | Improved 2026-04-07; `send_nightly_email` SMTP path, `apply_log_rotation_config` remaining |
| `web_viewer/app.py` | 46% | Each new endpoint test adds ~0.5-1% overall; config, SocketIO replay, admin POST paths untested |
| `scheduler.py` | 50% | `_process_channel_operations` (698-808), `_firmware_read/write_op` (893-962), nightly email body untested |
| `message_handler.py` | 44% | DM/broadcast dispatch branching (473-621), admin command paths (953-1010) |
| `command_manager.py` | 42% | Dispatch paths and BLE/TCP connection paths |
| `core.py` | 40% | Bot startup/shutdown paths, most of the async run loop |

### Near-zero — hardware/external API dependent (skip for now)
`wx_command` 5%, `airplanes_command` 10%, `prefix_command` 10%,
`solarforecast_command` 8%, `alert_command` 13%, `packet_capture_service` 9%,
`weather_service` 17% — these require live radio, external APIs, or complex
integration setup and are not practical to unit-test without significant mocking.

---

## Running Subsets of Tests

```bash
# Unit tests only (fast, no real DB)
pytest -m unit

# Integration tests only (real SQLite database via tmp_path)
pytest -m integration

# Skip slow tests
pytest -m "not slow"

# Tests in a subdirectory
pytest tests/unit/
pytest tests/commands/
pytest tests/integration/
pytest tests/regression/

# Run and stop on first failure
pytest -x

# Run with full traceback
pytest --tb=long

# Show which tests were collected without running them
pytest --collect-only
```

---

## Coverage

```bash
# Terminal report (default, configured in pytest.ini)
pytest

# HTML report — open htmlcov/index.html in a browser
pytest --cov=modules --cov-report=html

# Coverage for a single module
pytest tests/test_message_handler.py --cov=modules.message_handler --cov-report=term-missing
```

---

## Linting (configured alongside tests)

All lint and type-check commands are available via the Makefile (preferred):

```bash
make lint         # ruff check + mypy
make fix          # auto-fix safe ruff issues
```

Or run directly:

```bash
# Check for style/lint issues
.venv/bin/ruff check modules/ tests/

# Auto-fix safe issues
.venv/bin/ruff check --fix modules/ tests/

# Type checking
.venv/bin/mypy modules/
```

---

## Shared Infrastructure

### `tests/conftest.py` — Fixtures available to all tests

| Fixture | Scope | Description |
|---------|-------|-------------|
| `mock_logger` | function | Mock with `.info`, `.debug`, `.warning`, `.error` methods |
| `minimal_config` | function | `ConfigParser` with Connection / Bot / Channels / Keywords sections |
| `command_mock_bot` | function | Lightweight mock bot (no DB, no mesh graph); includes translator mock that echoes keys |
| `command_mock_bot_with_db` | function | Same as above but with a mock `db_manager` |
| `test_config` | function | Full `[Path_Command]` + `[Bot]` config (graph settings, Seattle lat/lon) |
| `test_db` | function | File-based `DBManager` at `tmp_path` with `mesh_connections` + `complete_contact_tracking` tables |
| `mock_bot` | function | Mock bot with logger, config, `test_db`, `bot_root`, `prefix_hex_chars=2`, `key_prefix` lambda |
| `mesh_graph` | function | `MeshGraph` instance (immediate write, no background thread) |
| `populated_mesh_graph` | function | `MeshGraph` pre-populated with 7 test edges of varying observations and ages |

### `tests/helpers.py` — Data factories

| Function | Returns |
|----------|---------|
| `create_test_repeater(prefix, name, ...)` | Dict matching `complete_contact_tracking` schema |
| `create_test_edge(from_prefix, to_prefix, ...)` | Dict matching `MeshGraph` edge structure |
| `create_test_path(node_ids, prefix_hex_chars)` | Normalized list of node IDs |
| `populate_test_graph(mesh_graph, edges)` | Populates a `MeshGraph` with edge dicts and sets observation counts/timestamps |

### `mock_message()` (helper in conftest)

```python
from tests.conftest import mock_message

msg = mock_message(content="ping", channel="general")
msg = mock_message(content="hello", is_dm=True, sender_id="Alice")
```

---

## Test File Reference

### Root-level tests (`tests/`)

#### `test_rate_limiter.py`
Tests `modules.rate_limiter` — `RateLimiter` and `PerUserRateLimiter`.

| Class | What it covers |
|-------|---------------|
| `TestRateLimiter` | Allow/block by interval, time-until-next, timestamp recording |
| `TestPerUserRateLimiter` | Per-key tracking, LRU eviction at `max_entries`, empty-key bypass |

---

#### `test_command_manager.py`
Tests `modules.command_manager` — `CommandManager` and `InternetStatusCache`.

| Class | What it covers |
|-------|---------------|
| `TestLoadKeywords` | Config parsing, quote stripping, escape decoding |
| `TestLoadBannedUsers` | Banned list parsing and whitespace handling |
| `TestIsUserBanned` | Exact match, prefix (starts-with) match, `None` sender |
| `TestChannelTriggerAllowed` | DM bypass, whitelist allow/block logic |
| `TestLoadMonitorChannels` | Channel list parsing and quote handling |
| `TestLoadChannelKeywords` | Per-channel keyword loading |
| `TestCheckKeywords` | Keyword matching, prefix-gating, channel scope, DM routing |
| `TestGetHelpForCommand` | Help text lookup for known/unknown commands |
| `TestInternetStatusCache` | Freshness check, stale detection, lock lazy-creation |
| `TestSendChannelMessageListeners` | Listener registration, invocation on success, skip on failure |
| `TestSendChannelMessagesChunked` | Empty chunks, single/multi-chunk timing, failure propagation |

---

#### `test_db_manager.py`
Tests `modules.db_manager` — `DBManager`.

| Class | What it covers |
|-------|---------------|
| `TestGeocoding` | Cache/retrieve geocoding, overwrite existing, invalid hours logged |
| `TestGenericCache` | JSON round-trip, miss returns default, independent keys |
| `TestCacheCleanup` | Expired rows deleted, valid rows preserved |
| `TestTableManagement` | Allowed/disallowed table names, SQL injection prevention |
| `TestExecuteQuery` | Returns list of dicts, update returns row count |
| `TestMetadata` | `set_metadata`/`get_metadata`, miss, bot start time |
| `TestCacheHoursValidation` | Boundary values 1–87600 valid; 0 and 87601 invalid |

---

#### `test_command_prefix.py`
Tests prefix-gating across `BaseCommand.matches_keyword`, `HelloCommand`, `PingCommand`, and `CommandManager`. Verifies that commands with a configured prefix require it, with 14 test cases covering `.`, `!`, multi-char, whitespace, case sensitivity, and empty-prefix edge cases.

---

#### `test_plugin_loader.py`
Tests `modules.plugin_loader` — `PluginLoader`.

| Class | What it covers |
|-------|---------------|
| `TestDiscover` | Finds command files, excludes `base_command.py` and `__init__.py` |
| `TestValidatePlugin` | Rejects missing `execute`, rejects sync `execute`, accepts valid class |
| `TestLoadPlugin` | Loads `ping_command`, returns `None` for nonexistent |
| `TestKeywordLookup` | By keyword, by name, miss |
| `TestCategoryAndFailed` | Category filter, failed-plugins copy |
| `TestLocalPlugins` | Discovery (empty / missing dir / found), load from path, collision skip |

---

#### `test_checkin_service.py`
Tests `local.service_plugins.checkin_service.CheckInService` (optional; auto-skipped if plugin not installed). Covers channel filtering, phrase matching, `any_message_counts`, and day-of-week filtering.

---

#### `test_scheduler_logic.py`
Tests `modules.scheduler` — `MessageScheduler` pure logic (no threading/asyncio).

| Class | What it covers |
|-------|---------------|
| `TestIsValidTimeFormat` | Valid HHMM times, invalid hours/minutes/length/non-numeric |
| `TestGetCurrentTime` | Valid timezone, invalid timezone fallback, empty timezone |
| `TestHasMeshInfoPlaceholders` | Detects `{total_contacts}`, `{repeaters}`, returns false for plain text |

---

#### `test_channel_manager_logic.py`
Tests `modules.channel_manager` — `ChannelManager` pure logic.

| Class | What it covers |
|-------|---------------|
| `TestGenerateHashtagKey` | Deterministic 16-byte key, `#` prepended if missing, known SHA-256 value |
| `TestChannelNameLookup` | Cache hit, fallback to `"channel N"` on miss |
| `TestChannelNumberLookup` | Found by name (case-insensitive), miss |
| `TestCacheManagement` | `invalidate_cache()` sets `_cache_valid = False` |

---

#### `test_channel_manager.py`
Expanded coverage of `modules.channel_manager` — 47 tests.

| Class | What it covers |
|-------|---------------|
| `TestGenerateHashtagKey` | Prefix normalisation, case-folding, output length, SHA-256 identity, distinct names |
| `TestGetChannelName` | Cache hit, fallback label, missing field, channel-zero edge case |
| `TestGetChannelNumber` | Index lookup, case-insensitive, not-found `None`, empty cache |
| `TestGetChannelKey` | Returns hex, missing channel, missing key field |
| `TestGetChannelInfo` | Full dict shape, missing channel fallback, info carries full cache entry |
| `TestGetChannelByName` | Found, case-insensitive, not found, invalid cache, empty cache |
| `TestGetConfiguredChannels` | Filters empty/whitespace names, invalid cache, all-blank, missing field |
| `TestInvalidateCache` | Sets `_cache_valid = False`, does not clear data, idempotent |
| `TestGetCachedChannels` | Sorted by index, empty cache, single-item |
| `TestAddChannelValidation` | Not connected, meshcore falsy, negative index, index at/beyond max, custom channel missing key, invalid/short hex, wrong byte length, index-0 boundary |

---

#### `test_i18n.py`
Tests `modules.i18n` — `Translator` class. All tests use `tmp_path`-based JSON translation files.

| Class | What it covers |
|-------|---------------|
| `TestExtractBaseLanguage` | Simple/hyphen/underscore locale parsing |
| `TestMergeTranslations` | Empty primary, primary override, recursive nested merge, flat-wins-over-nested |
| `TestTranslatorWithRealFiles` | English fallback, missing key returns key, format kwargs, format failure, locale override chain (en→es→es-MX), available languages, get_value, reload, invalid JSON, non-string value, PermissionError in _load_file, fallback inner loop (nested key miss), format KeyError returns unformatted, get_value fallback break |

---

#### `test_announcements_command.py`
Tests `modules.commands.announcements_command` — `AnnouncementsCommand`.

| Class | What it covers |
|-------|---------------|
| `TestParseCommand` | No args, trigger-only, trigger+channel, trigger+override, all three |
| `TestRecordTrigger` | Sets cooldown timestamp, fresh trigger not locked, just-sent locked, old trigger unlocked |
| `TestExecute` | No trigger shows list, no configured triggers, list subcommand with/without triggers, unknown trigger, locked/cooldown/override, successful send, failed send, custom channel, exception returns False |

---

#### `test_aurora_command.py`
Tests `modules.commands.aurora_command` — `AuroraCommand`.

| Class | What it covers |
|-------|---------------|
| `TestProbIndicator` | 0/50/100 bar chars, all values 0–100 |
| `TestFormatKpTime` | Empty/whitespace → dash, space-separated/ISO formats, invalid → dash |
| `TestGetBotLocation` | Returns lat/lon from config, returns None when not configured |
| `TestResolveLocation` | Coord string parse, invalid lat/lon, no-location uses bot, no-location no-bot returns error |
| `TestAuroraCanExecute` | Enabled/disabled |
| `TestResolveLocationExtended` | Companion location from DB, default lat/lon from config, ValueError returns error |
| `TestAuroraExecute` | No location error, bot location success, KP G3-severe/G1-G2/unsettled, fetch exception, coords arg, error key variants, response truncated at max length |

---

#### `test_help_command.py`
Tests `modules.commands.help_command` — `HelpCommand`.

| Class | What it covers |
|-------|---------------|
| `TestFormatCommandsListToLength` | No max, zero max, empty, truncation, all-fit, suffix, single item, single exceeds max, negative max |
| `TestIsCommandValidForChannel` | No message, channel allowed/not, no is_channel_allowed attr, trigger allowed/not, no trigger-check attr |
| `TestGetSpecificHelp` | Known command, TypeError fallback, unknown command, alias, no get_help_text |
| `TestCanExecute` | Enabled true/false |
| `TestGetHelpText` | Returns string |
| `TestGetGeneralHelp` | Returns string, includes commands.help key |
| `TestGetAvailableCommandsListFiltered` | Channel filter excludes invalid, command not in keyword_mappings |
| `TestFormatCommandsListSuffix` | Suffix fits within max, some fit |
| `TestExecute` | Returns True |
| `TestGetAvailableCommandsList` | Empty, with commands, max_length, message filter, stats table present, DB exception fallback |

---

#### `test_moon_command.py`
Tests `modules.commands.moon_command` — `MoonCommand`.

| Class | What it covers |
|-------|---------------|
| `TestTranslatePhaseName` | No translation, strips emoji, unknown phase, all 8 known phases |
| `TestFormatMoonResponse` | Valid parsed, partial fallback, empty fallback, malformed fallback, with dates format, without full/new moon |
| `TestMoonCommandEnabled` | can_execute enabled/disabled |
| `TestGetHelpTextMoon` | Returns description |
| `TestFormatMoonPhaseNoAt` | Phase without @: sign, exception falls back |
| `TestTranslatePhaseNameFound` | Translation returned when not key |
| `TestMoonExecute` | Success (mocked get_moon), error returns False |

---

#### `test_trace_command.py`
Tests `modules.commands.trace_command` — `TraceCommand`.

| Class | What it covers |
|-------|---------------|
| `TestExtractPathFromMessage` | No path, Direct, zero hops, single/multi hop, route type stripped, parenthesis stripped, invalid hex ignored |
| `TestParsePathArg` | No arg, comma-separated, contiguous hex, invalid hex, odd length, tracer prefix |
| `TestFormatTraceInline` | Basic inline, no SNR |
| `TestFormatTraceVertical` | Basic two nodes, single node |
| `TestBuildReciprocalPath` | Empty, single, two-node, three-node |
| `TestMatchesKeyword` | trace/tracer/trace+path, !trace/!tracer, non-match |
| `TestCanExecuteTrace` | Enabled/disabled |
| `TestGetHelpTextTrace` | Returns string with "trace" |
| `TestExtractPathEdgeCases` | 3-char invalid length, 2-char non-hex |
| `TestParseBangPrefix` | !trace stripped |
| `TestFormatTraceResult` | Failed shows error, success inline/vertical |
| `TestFormatTraceVerticalThreeNodes` | Middle hop present, no SNR shows — |
| `TestTraceExecute` | No path sends error, not connected, no meshcore commands |

---

#### `test_stats_command.py`
Tests `modules.commands.stats_command` — `StatsCommand`. 66 tests.

| Class | What it covers |
|-------|---------------|
| `TestIsValidPathFormat` | None/empty, hex with commas, continuous hex, descriptive text, single node |
| `TestFormatPathForDisplay` | None/empty → Direct, commas unchanged, continuous chunked, single unchanged, descriptive as-is |
| `TestStatsCommandEnabled` | enabled/disabled |
| `TestRecordMessage` | Inserts row, disabled collect_stats, disabled track_all, anonymize, no sender_id |
| `TestRecordCommand` | Inserts row, disabled, no track_details, anonymize |
| `TestRecordPathStats` | Valid path, no hops, None hops, no path, descriptive path skipped, disabled |
| `TestExecuteStats` | Disabled returns False, enabled returns True, messages/channels/paths/adverts/adverts-hashes/unknown subcommands, !-prefix strip, exception returns False |
| `TestGetHelpText` | Returns string |
| `TestFormatPathEdgeCases` | hex_chars=0 uses default, legacy fallback for non-divisible length |
| `TestRecordExceptionPaths` | record_message exception, record_command exception, record_path_stats anonymize+exception |
| `TestGetBasicStatsWithData` | top_command/top_user format lines covered with real data |
| `TestGetUserLeaderboardWithData` | Long name truncation, exception returns error key |
| `TestGetChannelLeaderboardWithData` | Channel data, exception |
| `TestGetPathLeaderboardWithData` | Path data, exception |
| `TestGetAdvertsLeaderboard` | No table → exception, empty → none, fallback no daily_stats (no/with hashes), with daily_stats (no/with hashes), singular count, name truncated, >10 hashes truncated, exception |
| `TestCleanupOldStats` | Runs without error, exception handled |
| `TestGetStatsSummary` | Returns dict with 4 keys, exception returns empty dict |

---

#### `test_feed_manager_formatting.py`
Tests `modules.feed_manager` — `FeedManager` pure formatting (networking disabled).

| Class | What it covers |
|-------|---------------|
| `TestApplyShortening` | `truncate:N`, `word_wrap:N`, `first_words:N`, `regex:`, `if_regex:`, empty input |
| `TestGetNestedValue` | Simple field, dotted path, missing field default |
| `TestShouldSendItem` | No filter, `equals`, `in`, `and` logic |
| `TestFormatTimestamp` | Recent timestamp string, `None` returns empty |

---

#### `test_profanity_filter.py`
Tests `modules.profanity_filter` — `censor()` and `contains_profanity()`.

| Class | What it covers |
|-------|---------------|
| `TestProfanityFilterEdgeCases` | `None`, empty, whitespace, non-string input; hate symbol detection |
| `TestProfanityFilterWithLibrary` | (skipped if `better_profanity` absent) — full censoring, `unidecode` homoglyph detection |
| `TestProfanityFilterFallbackWhenLibraryUnavailable` | Graceful degradation, hate symbols still filtered, one-time warning log |

---

#### `test_config_validation.py`
Tests `modules.config_validation` — `validate_config` and helpers.

| Class | What it covers |
|-------|---------------|
| `TestStripOptionalQuotes` | Single/double quote stripping, mismatch handling |
| `TestValidateConfig` | Missing sections, minimal valid, optional absent, typo detection |
| `TestPathValidation` | Non-existent parent warns, relative paths resolved, non-writable directory warns |
| `TestResolvePath` | Absolute/relative path resolution |
| `TestCheckPathWritable` | Empty path, non-existent parent, writable dir |
| `TestSuggestSimilarCommand` | Fuzzy match hit/miss |
| `TestGetCommandPrefixToSection` | Returns expected dict |

---

#### `test_utils.py`
Tests `modules.utils` — utility functions.

| Class | What it covers |
|-------|---------------|
| `TestAbbreviateLocation` | US/CA abbreviations, truncation with ellipsis |
| `TestTruncateString` | Under/over max, custom ellipsis |
| `TestDecodeEscapeSequences` | `\n`, `\t`, `\r`, literal backslash-n, mixed |
| `TestParseLocationString` | No comma, zip-only, city/state, city/country |
| `TestCalculateDistance` | Same point = 0, known distances (Seattle–Portland) |
| `TestFormatElapsedDisplay` | `None`/`unknown`/invalid input, recent timestamp, future timestamp, translator integration |
| `TestDecodePathLenByte` | 1/2/3 bytes-per-hop encoding, size code, path length fallback |
| `TestParsePathString` | Comma/space/continuous hex, hop-count suffix, 4-char nodes, legacy fallback |
| `TestCalculatePacketHashPathLength` | Single/multi-byte path hashes, different sizes produce different hashes |
| `TestMultiBytePathDisplayContract` | Format contract for 1-byte and 2-byte node paths |
| `TestIsValidTimezone` | Valid IANA zones, invalid zone, empty/whitespace, leading/trailing whitespace |
| `TestGetConfigTimezone` | Valid zone returned, invalid falls back to UTC, empty falls back, logger warning |
| `TestFormatLocationForDisplay` | `None`/empty city, city-only, city+state, no duplicate parts, max_length respected |
| `TestGetMajorCityQueries` | Known city returns queries, unknown city returns empty, case-insensitive, multiple results |
| `TestResolvePath` | Absolute path unchanged, relative resolved to base_dir, Path objects, `"."` base uses cwd |
| `TestCheckInternetConnectivity` | Returns True on successful socket, False when all fail, HTTP fallback on socket failure |
| `TestCalculatePathDistances` | Empty/direct path, no db_manager, single node, two nodes with/without locations |
| `TestFormatKeywordResponseWithPlaceholders` | `{sender}`, `{hops_label}`, `{connection_info}`, `{total_contacts}`, no-message defaults, bad placeholder fallback |

---

#### `test_bridge_bot_responses.py`
Tests `modules.service_plugins.discord_bridge_service` and `telegram_bridge_service` — `channel_sent_listeners` lifecycle.

Both `TestDiscordBridgeBotResponses` and `TestTelegramBridgeBotResponses` verify:
- `start()` registers a listener when `bridge_bot_responses = true`
- `stop()` unregisters the listener
- `start()` does NOT register a listener when `bridge_bot_responses = false`

---

#### `test_config_merge.py`
Tests `modules.core.MeshCoreBot` — local config merging. Verifies that `local/config.ini` is merged on `load_config()` and `reload_config()`, and that absent local configs are handled gracefully.

---

#### `test_randomline.py`
Tests `modules.command_manager.CommandManager.match_randomline` — `[RandomLine]` trigger matching. Covers case/whitespace normalisation, extra-word rejection, channel filtering, and channel-override allowing non-monitored channels.

---

#### `test_security_utils.py`
Tests `modules.security_utils`.

| Class | What it covers |
|-------|---------------|
| `TestValidatePubkeyFormat` | Valid 64-char hex, wrong length, invalid chars, non-string |
| `TestValidateSafePath` | Relative resolution, path traversal rejection, absolute path policy |
| `TestValidateExternalUrl` | `file://` rejected, `http(s)` allowed, localhost policy, missing netloc |
| `TestSanitizeInput` | Max-length truncation, control character stripping, newline/tab preserved |
| `TestValidateApiKeyFormat` | Valid key, too-short key, placeholder strings rejected |
| `TestValidatePortNumber` | Valid port, privileged port policy, out-of-range |

---

#### `test_service_plugin_loader.py`
Tests `modules.service_plugin_loader` — `ServicePluginLoader`. Covers local service discovery (empty/missing dir, finds `.py` files), loading (enabled/disabled/invalid/missing-enabled-key), and name-collision skipping.

---

#### `test_enums.py`
Tests `modules.enums` — all enum and flag types.

| Class | What it covers |
|-------|---------------|
| `TestAdvertFlags` | Type flag values, feature flag values, legacy aliases, `\|` combination, membership test |
| `TestPayloadType` | All 16 values, lookup by value, uniqueness |
| `TestPayloadVersion` | Four version values, lookup |
| `TestRouteType` | Four route type values, lookup |
| `TestDeviceRole` | String values, lookup, member count |

---

#### `test_models.py`
Tests `modules.models` — `MeshMessage` dataclass.

| Class | What it covers |
|-------|---------------|
| `TestMeshMessageDefaults` | Required `content`, all optional fields default to `None`, `is_dm=False` |
| `TestMeshMessageConstruction` | Channel msg, DM, routing_info dict, numeric fields, path, elapsed |
| `TestMeshMessageEquality` | Equal messages, different content/channel |

---

#### `test_transmission_tracker.py`
Tests `modules.transmission_tracker` — `TransmissionRecord` and `TransmissionTracker`.

| Class | What it covers |
|-------|---------------|
| `TestTransmissionRecord` | Default fields, custom fields |
| `TestRecordTransmission` | Returns record, stores in pending dict, multiple per second, command_id |
| `TestMatchPacketHash` | Null/zero hash → None, matches pending, confirmed cached, outside window → None |
| `TestRecordRepeat` | Null hash, increment count, no-prefix `_unknown` key, multiple repeats same repeater, unmatched → False |
| `TestGetRepeatInfo` | Unknown hash, by packet_hash, by command_id |
| `TestExtractRepeaterPrefixes` | Path string last hop, path_nodes last node, own-prefix filter, empty path, `via` annotation, single node |
| `TestCleanupOldRecords` | Removes old pending, keeps recent, removes confirmed without repeats, keeps confirmed with repeats |

---

#### `test_message_handler.py`
Tests `modules.message_handler` — `MessageHandler` pure logic.

| Class | What it covers |
|-------|---------------|
| `TestIsOldCachedMessage` | No connection time, `None`/`"unknown"`/0/negative/far-future timestamps, old vs. recent, invalid string |
| `TestPathBytesToNodes` | 1/2-byte-per-hop, remainder fallback, empty bytes, zero prefix_hex_chars |
| `TestPathHexToNodes` | 2/4-char chunks, empty/short string, remainder fallback |
| `TestFormatPathString` | Empty → `"Direct"`, legacy 1-byte, bytes_per_hop 1/2, remainder fallback, `None` input, invalid hex |
| `TestGetRouteTypeName` | All 4 known types, unknown type |
| `TestGetPayloadTypeName` | Known types, unknown type |
| `TestShouldProcessMessage` | Bot disabled, banned user, monitored channel, unmonitored, DM on/off, command-override channel |
| `TestCleanupStaleCacheEntries` | Removes old timestamp/pubkey/recent_rf entries, skips full cleanup within interval |

---

#### `test_repeater_manager.py`
Tests `modules.repeater_manager` — `RepeaterManager` pure logic. Uses a real test DB for construction.

| Class | What it covers |
|-------|---------------|
| `TestDetermineContactRole` | `mode` field priority, device type fallback, name-based detection (rpt, roomserver, sensor, bot, gateway), default companion |
| `TestDetermineDeviceType` | `advert_data.mode` priority, numeric type codes, name-based detection |
| `TestIsRepeaterDevice` | Type 2/3, role fields, name patterns (repeater, gateway), companion → False |
| `TestIsCompanionDevice` | Type 1 → True, type 2 → False, empty data → True |
| `TestIsInAcl` | No section, key present, key absent, empty list, exact-match-only, auto_purge_enabled config |

---

#### `test_core.py`
Tests `modules.core.MeshCoreBot` — config, radio settings, reload, and key helpers. Instantiates a real `MeshCoreBot` from temp config files.

| Class | What it covers |
|-------|---------------|
| `TestBotRoot` | `bot_root` property returns config file directory |
| `TestGetRadioSettings` | Returns dict with all keys, reads connection_type/serial_port/timeout, defaults for missing fields |
| `TestReloadConfig` | Success on same settings, failure on changed serial_port, missing file → False |
| `TestKeyPrefixHelpers` | `key_prefix()` truncates to `prefix_hex_chars`, `is_valid_prefix()` length check, config-driven `prefix_bytes` |

---

#### `test_web_viewer.py`
Tests `modules.web_viewer.app` — Flask routes and SocketIO handlers via Flask test client. 224 tests total.

| Class | What it covers |
|-------|---------------|
| `TestWebViewerAuth` | Password-protected and open endpoints; session management |
| `TestApiRoutes` | `/api/contacts`, `/api/stats`, `/api/maintenance/status`, radio status/reboot/connect endpoints |
| `TestChannelRoutes` | `GET/POST /api/channels`, validate, add, update, delete operations |
| `TestUpdateChannelRoute` | `PUT /api/channels/<n>` — update name/key/number; missing body → 400 |
| `TestCreateChannelValidation` | Channel creation input validation (index bounds, hex key length, missing fields) |
| `TestChannelValidateRoute` | `POST /api/channels/validate` — valid/invalid key/index combos |
| `TestStreamDataTypes` | SocketIO stream type-filter; `data-type` attribute on packet/command/message entries |
| `TestMaintenanceStatusFields` | `GET /api/maintenance/status` response schema — all expected status fields present |
| `TestDbPathResolutionFromConfigDir` | TASK-16/BUG-029: `db_path` resolves relative to config file directory; absolute path unchanged; differs from code root when config is elsewhere; startup INFO log captured via patched `_setup_logging` |

---

#### `test_mqtt_live.py`
Tests MeshCore MQTT packet parsing — schema validation against live and pre-collected fixture packets.
Config: `tests/mqtt_test_config.ini`. Fixtures: `tests/fixtures/mqtt_packets.json`.

| Class | Marker | What it covers |
|-------|--------|---------------|
| `TestPacketSchemaValidation` | (always) | Schema logic: required keys, valid direction/route/type values, rx-only SNR/RSSI/hash, tx packets allowed without RF fields |
| `TestFixturePackets` | (always) | Loads `mqtt_packets.json` (auto-skipped if absent); validates all fixture packets against schema; checks timestamp plausibility, packet_type and route ranges |
| `TestLiveMqttPackets` | `@pytest.mark.mqtt` | Connects to LAN broker; collects ≥1 live packet; validates schema, SNR/RSSI ranges (−200…+30 / −200…0), timestamp plausibility, route/direction/type values; auto-saves to fixtures |

Run live tests: `pytest tests/test_mqtt_live.py -v -m mqtt`
Collect fixtures offline: `python tests/test_mqtt_live.py --collect-fixtures`

---

### Command tests (`tests/commands/`)

| File | Command tested | Key scenarios |
|------|---------------|---------------|
| `test_base_command.py` | `BaseCommand` (via concrete commands) | `derive_config_section_name()`, `is_channel_allowed()`, `get_config_value()` legacy migration for 7 command types |
| `test_help_command.py` | `HelpCommand` | Enabled/disabled, async execute |
| `test_cmd_command.py` | `CmdCommand` | Command list building, truncation with `(N more)` suffix |
| `test_ping_command.py` | `PingCommand` | Keyword response, enabled/disabled |
| `test_dice_command.py` | `DiceCommand` | `d20`, `2d6`, decade (`d10`), mixed notation, value ranges, formatting, default d6, invalid input |
| `test_hello_command.py` | `HelloCommand` | Emoji-only detection, time-seeded greeting, emoji response, async execute |
| `test_magic8_command.py` | `Magic8Command` | Valid 🎱 response, sender mention in channel |
| `test_roll_command.py` | `RollCommand` | Parse notation, keyword matching, default 1–100, specific max |

---

### Unit tests (`tests/unit/`)

#### `test_mesh_graph.py` and `test_mesh_graph_*.py`
These six files provide comprehensive unit coverage of `modules.mesh_graph`:

| File | Focus |
|------|-------|
| `test_mesh_graph.py` | Edge management, multi-resolution prefix, path validation, candidate scoring, multi-hop, persistence |
| `test_mesh_graph_scoring.py` | `get_candidate_score()` — prev/next edge, bidirectional bonus, hop-position match and tolerance, disable flags |
| `test_mesh_graph_edges.py` | Add/update/get/has, public key merging, 1→2→3 byte edge promotion |
| `test_mesh_graph_multihop.py` | `find_intermediate_nodes()` — 2/3-hop paths, no path, min observations, bidirectional bonus, multiple candidates, self-loop prevention |
| `test_mesh_graph_validation.py` | `validate_path_segment()` and `validate_path()` — confidence, recency, bidirectional, empty/single paths |
| `test_mesh_graph_optimizations.py` | Adjacency indexes, public-key interning, edge expiration/pruning, notification throttle, capture_enabled flag |

#### `test_path_command_graph.py` and `test_path_command_graph_selection.py`
Both test `PathCommand._select_repeater_by_graph()`: no-graph fallback, direct edge selection, stored-key bonus, star bias, multi-hop, hop-position weighting, confidence conversion, missing key handling.

#### `test_path_command_multibyte.py`
Tests `PathCommand._decode_path()` and `_extract_path_from_recent_messages()` for multi-byte prefix support: 2-byte comma-separated, 1-byte, continuous hex, hop-count suffix stripping, routing_info.path_nodes priority.

---

### Integration tests (`tests/integration/`)

Both files test `PathCommand` + `MeshGraph` end-to-end with a real SQLite database (`test_db` fixture). Each scenario uses `mock_bot`, `mesh_graph`, and helper factories.

| File | Scenarios |
|------|-----------|
| `test_path_graph_integration.py` | Graph data resolution, prefix collision + disambiguation, starred preference, stored-key priority, 2-hop inference, edge persistence across restart, 5-node real-world scenario |
| `test_path_resolution.py` | Same scenarios plus sync graph validation, geographic vs. graph selection, direct SQLite inserts for setup |

All test methods use `@pytest.mark.integration` and `@pytest.mark.asyncio` where async.

---

### Regression tests (`tests/regression/`)

#### `test_keyword_escapes.py`
Regression guard for `modules.utils.decode_escape_sequences` — verifies that `\n` in config values produces a real newline, `\\n` produces a literal backslash-n, and `\t` produces a real tab. Prevents regressions in escape handling after any utils refactor.

---

## Writing New Tests

### Conventions

- **Class-based:** Use `class TestFeatureName:` grouping.
- **Async:** `asyncio_mode = auto` is set — just write `async def test_...` without the mark.
- **Fixtures:** Prefer conftest fixtures (`mock_logger`, `mock_bot`, `test_db`, `minimal_config`). Define local fixtures at the top of the file for module-specific setup.
- **Factories:** Use `create_test_repeater()`, `create_test_edge()`, `mock_message()` for consistent test data.
- **Database:** Use `tmp_path` (file-based SQLite, not `:memory:`) to avoid cross-connection isolation issues.
- **Mocking:** `MagicMock` for sync, `AsyncMock` for async methods.
- **Marks:** Tag with `@pytest.mark.unit` or `@pytest.mark.integration` for filtering.

### Example skeleton

```python
"""Tests for modules/my_module.py — MyClass."""

import pytest
from unittest.mock import Mock, AsyncMock
from modules.my_module import MyClass


@pytest.fixture
def my_obj(mock_logger):
    bot = Mock()
    bot.logger = mock_logger
    return MyClass(bot)


class TestMyFeature:

    def test_pure_logic(self, my_obj):
        result = my_obj.some_method("input")
        assert result == "expected"

    async def test_async_method(self, my_obj):
        my_obj.bot.send = AsyncMock(return_value=True)
        result = await my_obj.async_method("msg")
        assert result is True
```

### Adding coverage for a new module

1. Create `tests/test_<module_name>.py`.
2. Add a local fixture that constructs the class under test with mocked dependencies.
3. Start with pure-logic methods (no network, no DB) — these are fastest to write and run.
4. Add integration tests (with `test_db`) for database-touching methods.
5. Run `pytest tests/test_<module_name>.py --cov=modules.<module_name> --cov-report=term-missing` to see gaps.

---

## MQTT Test Framework

Live and offline tests for MeshCore packet parsing using real broker data.

### Architecture

```
tests/
  mqtt_test_config.ini        # broker / topic / timeout settings
  test_mqtt_live.py           # TestPacketSchemaValidation + TestFixturePackets + TestLiveMqttPackets
  fixtures/
    mqtt_packets.json         # pre-collected packets (auto-refreshed on live run)
```

### Running

```bash
# Offline schema + fixture tests (no network required)
pytest tests/test_mqtt_live.py -v -m "not mqtt"

# Live integration tests (requires LAN broker at 10.0.2.123:1883)
pytest tests/test_mqtt_live.py -v -m mqtt

# Collect fresh fixtures and exit
python tests/test_mqtt_live.py --collect-fixtures
```

### Broker configuration (`tests/mqtt_test_config.ini`)

| Key | Default | Notes |
|-----|---------|-------|
| `broker` | `10.0.2.123` | LAN MQTT broker (plain TCP, no auth) |
| `port` | `1883` | — |
| `transport` | `tcp` | Use `websockets` for letsmesh TLS broker |
| `topic_subscribe` | `meshcore/SEA/+/packets` | `+` wildcard matches any station |
| `timeout_seconds` | `15` | Seconds to wait for packets |
| `max_packets` | `10` | Collection limit |

**letsmesh alternative** (commented out in config): `mqtt-us-v1.letsmesh.net:443` WebSocket/TLS — requires JWT auth; not suitable for anonymous CI runs.

### Packet schema

| Field | rx | tx | Notes |
|-------|----|----|-------|
| `origin` | ✓ | ✓ | Sender display name |
| `origin_id` | ✓ | ✓ | 64-char hex pubkey |
| `timestamp` | ✓ | ✓ | Unix epoch (int or float) |
| `type` | ✓ | ✓ | Always `"PACKET"` |
| `direction` | ✓ | ✓ | `"rx"` or `"tx"` |
| `packet_type` | ✓ | ✓ | `"0"`–`"15"` string |
| `route` | ✓ | ✓ | `"F"` flood / `"D"` direct / `"T"` tunnel / `"U"` unknown |
| `SNR` | ✓ | ✗ | String float, −200…+30 dB |
| `RSSI` | ✓ | ✗ | String int, −200…0 dBm |
| `hash` | ✓ | ✗ | 16-char uppercase hex |

### Fixture auto-refresh

`test_received_at_least_one_packet` (live test) calls `_save_fixture_packets()` after each successful run — so `mqtt_packets.json` is updated automatically whenever live tests pass.

---

## CI Integration

Tests run automatically on push/PR via GitHub Actions. Jobs include:

| Job | Command |
|-----|---------|
| `lint` | `ruff check modules/ tests/` |
| `typecheck` | `mypy modules/` |
| `lint-frontend` | ESLint + HTMLHint on `modules/web_viewer/templates/` |
| `lint-shell` | ShellCheck `--severity=warning` on all `.sh` files |
| `test` | `pytest tests/ -v --tb=short` with coverage (excludes `mqtt` marker) |

To keep `TODO.md` in sync locally, run:

```bash
python scripts/update_todos.py
```

Or wire it up as a pre-commit hook (see `TODO.md` → Auto-Update section).
