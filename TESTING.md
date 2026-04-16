# TESTING

Complete reference for the meshcore-bot test suite.

---

## Quick Start

```bash
# First-time setup
make dev

# Full suite with coverage
make test

# Without coverage (faster)
make test-no-cov

# Specific file
.venv/bin/pytest tests/test_enums.py -v

# Specific class or function
.venv/bin/pytest tests/test_message_handler.py::TestShouldProcessMessage -v
.venv/bin/pytest tests/test_enums.py::TestPayloadType::test_lookup_by_value -v

# Stop on first failure
.venv/bin/pytest -x
```

---

## Configuration

**`pytest.ini`** — controls pytest behaviour:

| Setting            | Value                                                                      |
|--------------------|----------------------------------------------------------------------------|
| `testpaths`        | `tests`                                                                    |
| `asyncio_mode`     | `auto` — async tests run without `@pytest.mark.asyncio`                    |
| `addopts`          | `-v --tb=short --strict-markers --cov=modules --cov-report=term-missing`   |
| Registered markers | `unit`, `integration`, `slow`, `mqtt`                                      |

**`pyproject.toml`** — coverage settings (`[tool.coverage.*]`):

| Setting      | Value                                                           |
|--------------|-----------------------------------------------------------------|
| `source`     | `modules/`                                                      |
| `omit`       | `tests/`, `.venv/`                                              |
| `fail_under` | `35` — raised 2026-03-16; currently 36.72%; target 40%          |
|              | (hardware-dependent modules cap realistic ceiling at ~40-42%)   |

---

## Running Subsets of Tests

```bash
pytest -m unit               # unit tests only (fast, no real DB)
pytest -m integration        # integration tests (real SQLite via tmp_path)
pytest -m "not slow"         # skip slow tests
pytest tests/unit/           # tests in a subdirectory
pytest tests/commands/
pytest tests/integration/
pytest tests/regression/
pytest -x                    # stop on first failure
pytest --tb=long             # full traceback
pytest --collect-only        # list collected tests without running
```

---

## Coverage

```bash
# Terminal report (default via pytest.ini)
pytest

# HTML report — open htmlcov/index.html in a browser
pytest --cov=modules --cov-report=html

# Coverage for a single module
pytest tests/test_message_handler.py \
  --cov=modules.message_handler --cov-report=term-missing
```

---

## Linting

All lint and type-check commands are available via the Makefile (preferred):

```bash
make lint    # ruff check + mypy
make fix     # auto-fix safe ruff issues
```

Or run directly:

```bash
.venv/bin/ruff check modules/ tests/          # style/lint check
.venv/bin/ruff check --fix modules/ tests/    # auto-fix safe issues
.venv/bin/mypy modules/                        # type checking
```

---

## Shared Infrastructure

### `tests/conftest.py` — Fixtures available to all tests

| Fixture                    | Scope    | Description                                           |
|----------------------------|----------|-------------------------------------------------------|
| `mock_logger`              | function | Mock with `.info/.debug/.warning/.error` methods      |
| `minimal_config`           | function | `ConfigParser` with core sections pre-populated       |
| `command_mock_bot`         | function | Lightweight mock bot; no DB or mesh graph             |
| `command_mock_bot_with_db` | function | Same as above with mock `db_manager`                  |
| `test_config`              | function | Full `[Path_Command]` + `[Bot]` config; Seattle coords|
| `test_db`                  | function | File-based `DBManager` at `tmp_path` with test tables |
| `mock_bot`                 | function | Mock bot with logger, config, DB, and prefix helpers  |
| `mesh_graph`               | function | `MeshGraph` (immediate write, no background thread)   |
| `populated_mesh_graph`     | function | `MeshGraph` pre-loaded with 7 test edges              |

### `tests/helpers.py` — Data factories

| Function                            | Returns                                           |
|-------------------------------------|---------------------------------------------------|
| `create_test_repeater(prefix, ...)` | Dict matching `complete_contact_tracking` schema  |
| `create_test_edge(from, to, ...)`   | Dict matching `MeshGraph` edge structure          |
| `create_test_path(node_ids, ...)`   | Normalized list of node IDs                       |
| `populate_test_graph(graph, edges)` | Populates a `MeshGraph` with edge dicts           |

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

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestRateLimiter`                     | Allow/block by interval, time-until-next, timestamps  |
| `TestPerUserRateLimiter`              | Per-key tracking, LRU eviction, empty-key bypass      |

---

#### `test_command_manager.py`
Tests `modules.command_manager` — `CommandManager` and `InternetStatusCache`.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestLoadKeywords`                    | Config parsing, quote stripping, escape decoding      |
| `TestLoadBannedUsers`                 | Banned list parsing and whitespace handling           |
| `TestIsUserBanned`                    | Exact match, prefix match, `None` sender              |
| `TestChannelTriggerAllowed`           | DM bypass, whitelist allow/block logic                |
| `TestLoadMonitorChannels`             | Channel list parsing and quote handling               |
| `TestLoadChannelKeywords`             | Per-channel keyword loading                           |
| `TestCheckKeywords`                   | Keyword matching, prefix-gating, scope, DM routing    |
| `TestGetHelpForCommand`               | Help text lookup for known/unknown commands           |
| `TestInternetStatusCache`             | Freshness check, stale detection, lock lazy-creation  |
| `TestSendChannelMessageListeners`     | Listener registration, invoke on success/skip on fail |
| `TestSendChannelMessagesChunked`      | Empty chunks, single/multi-chunk timing, failure prop |

---

#### `test_db_manager.py`
Tests `modules.db_manager` — `DBManager`.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestGeocoding`                       | Cache/retrieve geocoding, overwrite, invalid hours    |
| `TestGenericCache`                    | JSON round-trip, miss returns default, key isolation  |
| `TestCacheCleanup`                    | Expired rows deleted, valid rows preserved            |
| `TestTableManagement`                 | Allowed/disallowed names, SQL injection prevention    |
| `TestExecuteQuery`                    | Returns list of dicts, update returns row count       |
| `TestMetadata`                        | `set_metadata`/`get_metadata`, miss, bot start time   |
| `TestCacheHoursValidation`            | Boundary values 1–87600 valid; 0 and 87601 invalid    |

---

#### `test_command_prefix.py`
Tests prefix-gating across `BaseCommand.matches_keyword`, `HelloCommand`, `PingCommand`, and
`CommandManager`. Covers `.`, `!`, multi-char, whitespace, case sensitivity, and empty-prefix
edge cases — 14 test cases total.

---

#### `test_plugin_loader.py`
Tests `modules.plugin_loader` — `PluginLoader`.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestDiscover`                        | Finds command files, excludes base and `__init__`     |
| `TestValidatePlugin`                  | Rejects missing/sync `execute`; accepts valid class   |
| `TestLoadPlugin`                      | Loads `ping_command`, returns `None` for nonexistent  |
| `TestKeywordLookup`                   | By keyword, by name, miss                             |
| `TestCategoryAndFailed`               | Category filter, failed-plugins copy                  |
| `TestLocalPlugins`                    | Discovery, load from path, name-collision skip        |

---

#### `test_checkin_service.py`
Tests `local.service_plugins.checkin_service.CheckInService` (auto-skipped if not installed).
Covers channel filtering, phrase matching, `any_message_counts`, and day-of-week filtering.

---

#### `test_scheduler_logic.py`
Tests `modules.scheduler` — `MessageScheduler` pure logic (no threading/asyncio).

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestIsValidTimeFormat`               | Valid HHMM times, invalid hours/minutes/length/chars  |
| `TestGetCurrentTime`                  | Valid timezone, invalid fallback, empty timezone      |
| `TestHasMeshInfoPlaceholders`         | Detects `{total_contacts}`, `{repeaters}`; false case |

---

#### `test_channel_manager_logic.py`
Tests `modules.channel_manager` — `ChannelManager` pure logic.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestGenerateHashtagKey`              | Deterministic 16-byte key, `#` prefix, known SHA-256  |
| `TestChannelNameLookup`               | Cache hit, fallback to `"channel N"` on miss          |
| `TestChannelNumberLookup`             | Found by name (case-insensitive), miss                |
| `TestCacheManagement`                 | `invalidate_cache()` sets `_cache_valid = False`      |

---

#### `test_channel_manager.py`
Expanded coverage of `modules.channel_manager` — 47 tests.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestGenerateHashtagKey`              | Prefix normalisation, case-folding, SHA-256 identity  |
| `TestGetChannelName`                  | Cache hit, fallback label, missing field, ch-0 edge   |
| `TestGetChannelNumber`                | Index lookup, case-insensitive, not-found `None`      |
| `TestGetChannelKey`                   | Returns hex, missing channel, missing key field       |
| `TestGetChannelInfo`                  | Full dict shape, missing fallback, full cache entry   |
| `TestGetChannelByName`                | Found, case-insensitive, not found, empty cache       |
| `TestGetConfiguredChannels`           | Filters empty/whitespace names, missing field         |
| `TestInvalidateCache`                 | Sets `_cache_valid = False`; does not clear data      |
| `TestGetCachedChannels`               | Sorted by index, empty cache, single-item             |
| `TestAddChannelValidation`            | Not connected, falsy meshcore, negative index,        |
|                                       | index at/beyond max, missing key, invalid hex,        |
|                                       | wrong byte length, index-0 boundary                   |

---

#### `test_i18n.py`
Tests `modules.i18n` — `Translator` class. All tests use `tmp_path`-based JSON files.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestExtractBaseLanguage`             | Simple/hyphen/underscore locale parsing               |
| `TestMergeTranslations`               | Empty primary, override, recursive nested merge       |
| `TestTranslatorWithRealFiles`         | English fallback, missing key, kwargs, format error,  |
|                                       | locale chain (en→es→es-MX), reload, invalid JSON,     |
|                                       | non-string, PermissionError, nested key miss,         |
|                                       | format KeyError, get_value fallback break             |

---

#### `test_announcements_command.py`
Tests `modules.commands.announcements_command` — `AnnouncementsCommand`.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestParseCommand`                    | No args, trigger-only, trigger+channel, all three     |
| `TestRecordTrigger`                   | Sets cooldown, fresh not locked, old unlocked         |
| `TestExecute`                         | No trigger, list, unknown trigger, cooldown/override, |
|                                       | successful/failed send, custom channel, exception     |

---

#### `test_aurora_command.py`
Tests `modules.commands.aurora_command` — `AuroraCommand`.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestProbIndicator`                   | 0/50/100 bar chars, all values 0–100                  |
| `TestFormatKpTime`                    | Empty/whitespace → dash, ISO formats, invalid → dash  |
| `TestGetBotLocation`                  | Returns lat/lon from config, `None` when missing      |
| `TestResolveLocation`                 | Coord parse, invalid lat/lon, bot fallback, error     |
| `TestAuroraCanExecute`                | Enabled/disabled                                      |
| `TestResolveLocationExtended`         | Companion DB location, default coords, ValueError     |
| `TestAuroraExecute`                   | No location, bot location, KP G3/G2/G1/unsettled,    |
|                                       | fetch exception, coords arg, response truncation      |

---

#### `test_help_command.py`
Tests `modules.commands.help_command` — `HelpCommand`.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestFormatCommandsListToLength`      | No max, zero max, empty, truncation, suffix, negative |
| `TestIsCommandValidForChannel`        | No message, allow/block, no attr, trigger checks      |
| `TestGetSpecificHelp`                 | Known command, TypeError fallback, alias, no get_help |
| `TestCanExecute`                      | Enabled true/false                                    |
| `TestGetHelpText`                     | Returns string                                        |
| `TestGetGeneralHelp`                  | Returns string, includes `commands.help` key          |
| `TestGetAvailableCommandsListFiltered`| Channel filter excludes invalid; no keyword_mappings  |
| `TestFormatCommandsListSuffix`        | Suffix fits within max, some fit                      |
| `TestExecute`                         | Returns True                                          |
| `TestGetAvailableCommandsList`        | Empty, with commands, max_length, message filter,     |
|                                       | stats table present, DB exception fallback            |

---

#### `test_moon_command.py`
Tests `modules.commands.moon_command` — `MoonCommand`.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestTranslatePhaseName`              | No translation, strips emoji, unknown, all 8 phases   |
| `TestFormatMoonResponse`              | Valid parsed, partial/empty/malformed fallback        |
| `TestMoonCommandEnabled`              | `can_execute` enabled/disabled                        |
| `TestGetHelpTextMoon`                 | Returns description                                   |
| `TestFormatMoonPhaseNoAt`             | Phase without `@:` sign, exception falls back         |
| `TestTranslatePhaseNameFound`         | Translation returned when key not found               |
| `TestMoonExecute`                     | Success (mocked `get_moon`), error returns False      |

---

#### `test_trace_command.py`
Tests `modules.commands.trace_command` — `TraceCommand`.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestExtractPathFromMessage`          | No path, Direct, zero hops, single/multi hop,         |
|                                       | route type stripped, parentheses, invalid hex         |
| `TestParsePathArg`                    | No arg, comma-sep, contiguous hex, invalid, odd length|
| `TestFormatTraceInline`               | Basic inline, no SNR                                  |
| `TestFormatTraceVertical`             | Basic two nodes, single node                          |
| `TestBuildReciprocalPath`             | Empty, single, two-node, three-node                   |
| `TestMatchesKeyword`                  | trace/tracer/trace+path, `!trace`/`!tracer`, no-match |
| `TestCanExecuteTrace`                 | Enabled/disabled                                      |
| `TestGetHelpTextTrace`                | Returns string containing "trace"                     |
| `TestExtractPathEdgeCases`            | 3-char invalid length, 2-char non-hex                 |
| `TestParseBangPrefix`                 | `!trace` stripped                                     |
| `TestFormatTraceResult`               | Failed shows error, success inline/vertical           |
| `TestFormatTraceVerticalThreeNodes`   | Middle hop present, no SNR shows —                    |
| `TestTraceExecute`                    | No path sends error, not connected, no commands       |

---

#### `test_stats_command.py`
Tests `modules.commands.stats_command` — `StatsCommand`. 66 tests.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestIsValidPathFormat`               | None/empty, hex+commas, continuous hex, single node   |
| `TestFormatPathForDisplay`            | None/empty → Direct, commas unchanged, chunked        |
| `TestStatsCommandEnabled`             | Enabled/disabled                                      |
| `TestRecordMessage`                   | Inserts row, disabled, no track_all, anonymize        |
| `TestRecordCommand`                   | Inserts row, disabled, no track_details, anonymize    |
| `TestRecordPathStats`                 | Valid path, no/None hops, descriptive path skipped    |
| `TestExecuteStats`                    | Disabled/enabled; all subcommands; `!`-prefix strip;  |
|                                       | exception returns False                               |
| `TestGetHelpText`                     | Returns string                                        |
| `TestFormatPathEdgeCases`             | `hex_chars=0` uses default, legacy fallback           |
| `TestRecordExceptionPaths`            | record_message/command/path_stats exceptions          |
| `TestGetBasicStatsWithData`           | top_command/top_user format lines with real data      |
| `TestGetUserLeaderboardWithData`      | Long name truncation, exception returns error key     |
| `TestGetChannelLeaderboardWithData`   | Channel data, exception                               |
| `TestGetPathLeaderboardWithData`      | Path data, exception                                  |
| `TestGetAdvertsLeaderboard`           | No table, empty, fallback daily_stats variants,       |
|                                       | singular count, name/hash truncation, exception       |
| `TestCleanupOldStats`                 | Runs without error, exception handled                 |
| `TestGetStatsSummary`                 | Returns dict with 4 keys, exception returns empty     |

---

#### `test_feed_manager_formatting.py`
Tests `modules.feed_manager` — `FeedManager` pure formatting (networking disabled).

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestApplyShortening`                 | `truncate:N`, `word_wrap:N`, `first_words:N`,         |
|                                       | `regex:`, `if_regex:`, empty input                    |
| `TestGetNestedValue`                  | Simple field, dotted path, missing field default      |
| `TestShouldSendItem`                  | No filter, `equals`, `in`, `and` logic                |
| `TestFormatTimestamp`                 | Recent timestamp string, `None` returns empty         |

---

#### `test_profanity_filter.py`
Tests `modules.profanity_filter` — `censor()` and `contains_profanity()`.

| Class                                        | What it covers                                  |
|----------------------------------------------|-------------------------------------------------|
| `TestProfanityFilterEdgeCases`               | None/empty/whitespace/non-string; hate symbols  |
| `TestProfanityFilterWithLibrary`             | (skipped if absent) censoring, homoglyph detect |
| `TestProfanityFilterFallbackWhenLibrary`     | Graceful degradation, hate symbols, one warning |

---

#### `test_config_validation.py`
Tests `modules.config_validation` — `validate_config` and helpers.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestStripOptionalQuotes`             | Single/double quote stripping, mismatch handling      |
| `TestValidateConfig`                  | Missing sections, minimal valid, typo detection       |
| `TestPathValidation`                  | Non-existent parent, relative resolved, non-writable  |
| `TestResolvePath`                     | Absolute/relative path resolution                     |
| `TestCheckPathWritable`               | Empty path, non-existent parent, writable dir         |
| `TestSuggestSimilarCommand`           | Fuzzy match hit/miss                                  |
| `TestGetCommandPrefixToSection`       | Returns expected dict                                 |

---

#### `test_utils.py`
Tests `modules.utils` — utility functions.

| Class                                       | What it covers                                  |
|---------------------------------------------|-------------------------------------------------|
| `TestAbbreviateLocation`                    | US/CA abbreviations, truncation with ellipsis   |
| `TestTruncateString`                        | Under/over max, custom ellipsis                 |
| `TestDecodeEscapeSequences`                 | `\n`, `\t`, `\r`, literal backslash-n, mixed    |
| `TestParseLocationString`                   | No comma, zip-only, city/state, city/country    |
| `TestCalculateDistance`                     | Same point = 0, Seattle–Portland known distance |
| `TestFormatElapsedDisplay`                  | None/unknown/invalid, recent, future, translator|
| `TestDecodePathLenByte`                     | 1/2/3 bytes-per-hop, size code, fallback        |
| `TestParsePathString`                       | Comma/space/continuous hex, hop suffix, legacy  |
| `TestCalculatePacketHashPathLength`         | Single/multi-byte hashes, different sizes       |
| `TestMultiBytePathDisplayContract`          | Format contract for 1-byte and 2-byte nodes     |
| `TestIsValidTimezone`                       | Valid IANA zones, invalid, empty, whitespace    |
| `TestGetConfigTimezone`                     | Valid returned, invalid→UTC, empty→UTC, warning |
| `TestFormatLocationForDisplay`              | None/empty, city-only, city+state, max_length   |
| `TestGetMajorCityQueries`                   | Known city, unknown city, case-insensitive      |
| `TestResolvePath`                           | Absolute unchanged, relative to base_dir, `"."`|
| `TestCheckInternetConnectivity`             | True on socket, False all fail, HTTP fallback   |
| `TestCalculatePathDistances`                | Empty/direct, no db_manager, single/two nodes   |
| `TestFormatKeywordResponseWithPlaceholders` | `{sender}`, `{hops_label}`, `{connection_info}`,|
|                                             | `{total_contacts}`, defaults, bad placeholder   |

---

#### `test_bridge_bot_responses.py`
Tests `modules.service_plugins.discord_bridge_service` and `telegram_bridge_service` —
`channel_sent_listeners` lifecycle.

Both `TestDiscordBridgeBotResponses` and `TestTelegramBridgeBotResponses` verify:
- `start()` registers a listener when `bridge_bot_responses = true`
- `stop()` unregisters the listener
- `start()` does NOT register when `bridge_bot_responses = false`

---

#### `test_config_merge.py`
Tests `modules.core.MeshCoreBot` — local config merging. Verifies that `local/config.ini` is
merged on `load_config()` and `reload_config()`, and that absent local configs are handled
gracefully.

---

#### `test_randomline.py`
Tests `modules.command_manager.CommandManager.match_randomline` — `[RandomLine]` trigger
matching. Covers case/whitespace normalisation, extra-word rejection, channel filtering, and
channel-override allowing non-monitored channels.

---

#### `test_security_utils.py`
Tests `modules.security_utils`.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestValidatePubkeyFormat`            | Valid 64-char hex, wrong length, invalid chars        |
| `TestValidateSafePath`                | Relative resolution, path traversal rejection         |
| `TestValidateExternalUrl`             | `file://` rejected, `http(s)` allowed, localhost      |
| `TestSanitizeInput`                   | Max-length truncation, control char stripping         |
| `TestValidateApiKeyFormat`            | Valid key, too-short, placeholder strings rejected    |
| `TestValidatePortNumber`              | Valid port, privileged port policy, out-of-range      |

---

#### `test_service_plugin_loader.py`
Tests `modules.service_plugin_loader` — `ServicePluginLoader`. Covers local service discovery
(empty/missing dir, finds `.py` files), loading (enabled/disabled/invalid/missing key), and
name-collision skipping.

---

#### `test_enums.py`
Tests `modules.enums` — all enum and flag types.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestAdvertFlags`                     | Type/feature flag values, legacy aliases, `\|` combo  |
| `TestPayloadType`                     | All 16 values, lookup by value, uniqueness            |
| `TestPayloadVersion`                  | Four version values, lookup                           |
| `TestRouteType`                       | Four route type values, lookup                        |
| `TestDeviceRole`                      | String values, lookup, member count                   |

---

#### `test_models.py`
Tests `modules.models` — `MeshMessage` dataclass.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestMeshMessageDefaults`             | Required `content`, all optional fields default `None`|
| `TestMeshMessageConstruction`         | Channel msg, DM, routing_info dict, path, elapsed     |
| `TestMeshMessageEquality`             | Equal messages, different content/channel             |

---

#### `test_transmission_tracker.py`
Tests `modules.transmission_tracker` — `TransmissionRecord` and `TransmissionTracker`.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestTransmissionRecord`              | Default fields, custom fields                         |
| `TestRecordTransmission`              | Returns record, pending dict, multiple, command_id    |
| `TestMatchPacketHash`                 | Null/zero→None, matches pending, confirmed, timeout   |
| `TestRecordRepeat`                    | Null hash, increment, `_unknown` key, multiple        |
| `TestGetRepeatInfo`                   | Unknown hash, by packet_hash, by command_id           |
| `TestExtractRepeaterPrefixes`         | Path last hop, path_nodes, own-prefix filter, `via`   |
| `TestCleanupOldRecords`               | Removes old pending, keeps recent/confirmed+repeats   |

---

#### `test_message_handler.py`
Tests `modules.message_handler` — `MessageHandler` pure logic.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestIsOldCachedMessage`              | No connection time, None/unknown/0/negative/future,   |
|                                       | old vs. recent, invalid string                        |
| `TestPathBytesToNodes`                | 1/2-byte-per-hop, remainder fallback, empty, zero     |
| `TestPathHexToNodes`                  | 2/4-char chunks, empty/short, remainder fallback      |
| `TestFormatPathString`                | Empty→Direct, legacy, bytes_per_hop 1/2, None, invalid|
| `TestGetRouteTypeName`                | All 4 known types, unknown type                       |
| `TestGetPayloadTypeName`              | Known types, unknown type                             |
| `TestShouldProcessMessage`            | Bot disabled, banned, monitored/unmonitored, DM on/off|
| `TestCleanupStaleCacheEntries`        | Removes old timestamp/pubkey/rf entries, skip interval|

---

#### `test_repeater_manager.py`
Tests `modules.repeater_manager` — `RepeaterManager` pure logic. Uses a real test DB.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestDetermineContactRole`            | `mode` priority, device type fallback, name patterns  |
|                                       | (rpt, roomserver, sensor, bot, gateway)               |
| `TestDetermineDeviceType`             | `advert_data.mode` priority, numeric codes, name-based|
| `TestIsRepeaterDevice`                | Type 2/3, role fields, name patterns, companion→False |
| `TestIsCompanionDevice`               | Type 1→True, type 2→False, empty data→True            |
| `TestIsInAcl`                         | No section, key present/absent, empty list, exact-only|

---

#### `test_core.py`
Tests `modules.core.MeshCoreBot` — config, radio settings, reload, key helpers.
Instantiates a real `MeshCoreBot` from temp config files.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestBotRoot`                         | `bot_root` returns config file directory              |
| `TestGetRadioSettings`                | Returns dict with all keys, reads from config         |
| `TestReloadConfig`                    | Success same settings, fail changed port, missing file|
| `TestKeyPrefixHelpers`                | `key_prefix()` truncates, `is_valid_prefix()` length  |

---

#### `test_web_viewer.py`
Tests `modules.web_viewer.app` — Flask routes and SocketIO handlers. 224 tests total.

| Class                                 | What it covers                                        |
|---------------------------------------|-------------------------------------------------------|
| `TestWebViewerAuth`                   | Password-protected and open endpoints, sessions       |
| `TestApiRoutes`                       | `/api/contacts`, `/api/stats`, maintenance, radio     |
| `TestChannelRoutes`                   | `GET/POST /api/channels`, add/update/delete ops       |
| `TestUpdateChannelRoute`              | `PUT /api/channels/<n>` — name/key/number; 400 on bad |
| `TestCreateChannelValidation`         | Index bounds, hex key length, missing fields          |
| `TestChannelValidateRoute`            | `POST /api/channels/validate` — valid/invalid combos  |
| `TestStreamDataTypes`                 | SocketIO type-filter; `data-type` on entries          |
| `TestMaintenanceStatusFields`         | `/api/maintenance/status` schema — all fields present |
| `TestDbPathResolutionFromConfigDir`   | BUG-029: `db_path` resolves relative to config dir;   |
|                                       | absolute unchanged; startup log via `_setup_logging`  |

---

#### `test_mqtt_live.py`
Tests MeshCore MQTT packet parsing — schema validation against live and fixture packets.
Config: `tests/mqtt_test_config.ini`. Fixtures: `tests/fixtures/mqtt_packets.json`.

| Class                       | Marker   | What it covers                                      |
|-----------------------------|----------|-----------------------------------------------------|
| `TestPacketSchemaValidation`| (always) | Required keys, valid direction/route/type values,   |
|                             |          | rx-only SNR/RSSI/hash, tx allowed without RF fields |
| `TestFixturePackets`        | (always) | Loads `mqtt_packets.json` (skip if absent);         |
|                             |          | validates schema, timestamp/type/route ranges       |
| `TestLiveMqttPackets`       | mqtt     | Connects to LAN broker; validates schema,           |
|                             |          | SNR/RSSI ranges, plausibility; auto-saves fixtures  |

Run live tests: `pytest tests/test_mqtt_live.py -v -m mqtt`
Collect fixtures offline: `python tests/test_mqtt_live.py --collect-fixtures`

---

### Command tests (`tests/commands/`)

| File                       | Command        | Key scenarios                                          |
|----------------------------|----------------|--------------------------------------------------------|
| `test_base_command.py`     | `BaseCommand`  | `config_section_name`, `channel_allowed`,              |
|                            |                | `get_config_value` legacy migration, 7 command types   |
| `test_help_command.py`     | `HelpCommand`  | Enabled/disabled, async execute                        |
| `test_cmd_command.py`      | `CmdCommand`   | Command list building, truncation with `(N more)`      |
| `test_ping_command.py`     | `PingCommand`  | Keyword response, enabled/disabled                     |
| `test_dice_command.py`     | `DiceCommand`  | `d20`, `2d6`, decade, mixed notation, default d6       |
| `test_hello_command.py`    | `HelloCommand` | Emoji-only detection, time-seeded greeting, execute    |
| `test_magic8_command.py`   | `Magic8Command`| Valid 🎱 response, sender mention in channel           |
| `test_roll_command.py`     | `RollCommand`  | Parse notation, keyword match, default 1–100, max      |

---

### Unit tests (`tests/unit/`)

#### `test_mesh_graph.py` and `test_mesh_graph_*.py`
Six files providing comprehensive unit coverage of `modules.mesh_graph`:

| File                                 | Focus                                                    |
|--------------------------------------|----------------------------------------------------------|
| `test_mesh_graph.py`                 | Edge management, prefix, path validation, scoring,       |
|                                      | multi-hop, persistence                                   |
| `test_mesh_graph_scoring.py`         | `get_candidate_score()`: prev/next edge, bidirectional,  |
|                                      | hop-position match, tolerance, disable flags             |
| `test_mesh_graph_edges.py`           | Add/update/get/has, key merging, 1→2→3 byte promotion    |
| `test_mesh_graph_multihop.py`        | `find_intermediate_nodes()`: 2/3-hop, no path,           |
|                                      | min observations, bidirectional, multi-candidate         |
| `test_mesh_graph_validation.py`      | `validate_path_segment/path()`: confidence, recency,     |
|                                      | bidirectional, empty/single path edge cases              |
| `test_mesh_graph_optimizations.py`   | Adjacency indexes, key interning, edge expiration,       |
|                                      | pruning, notification throttle, `capture_enabled`        |

#### `test_path_command_graph.py` and `test_path_command_graph_selection.py`
Both test `PathCommand._select_repeater_by_graph()`: no-graph fallback, direct edge selection,
stored-key bonus, star bias, multi-hop, hop-position weighting, confidence conversion, missing
key handling.

#### `test_path_command_multibyte.py`
Tests `PathCommand._decode_path()` and `_extract_path_from_recent_messages()` for multi-byte
prefix support: 2-byte comma-separated, 1-byte, continuous hex, hop-count suffix stripping,
`routing_info.path_nodes` priority.

---

### Integration tests (`tests/integration/`)

Both files test `PathCommand` + `MeshGraph` end-to-end with a real SQLite database.
Each scenario uses `mock_bot`, `mesh_graph`, and helper factories.
All methods use `@pytest.mark.integration`.

| File                              | Scenarios                                              |
|-----------------------------------|--------------------------------------------------------|
| `test_path_graph_integration.py`  | Graph resolution, prefix disambiguation, starred/      |
|                                   | stored-key priority, 2-hop inference, persistence,     |
|                                   | 5-node real-world scenario                             |
| `test_path_resolution.py`         | Same scenarios plus sync graph validation,             |
|                                   | geographic vs. graph selection, direct SQLite inserts  |

---

### Regression tests (`tests/regression/`)

#### `test_keyword_escapes.py`
Regression guard for `modules.utils.decode_escape_sequences`. Verifies `\n` in config values
produces a real newline, `\\n` produces a literal backslash-n, and `\t` produces a real tab.
Prevents regressions in escape handling after any utils refactor.

---

## Writing New Tests

### Conventions

- **Class-based:** Use `class TestFeatureName:` grouping.
- **Async:** `asyncio_mode = auto` is set — write `async def test_...` without the mark.
- **Fixtures:** Prefer conftest fixtures (`mock_logger`, `mock_bot`, `test_db`, `minimal_config`).
- **Factories:** Use `create_test_repeater()`, `create_test_edge()`, `mock_message()`.
- **Database:** Use `tmp_path` (file-based SQLite) to avoid cross-connection isolation issues.
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
5. Check coverage gaps:
   ```bash
   pytest tests/test_<module_name>.py \
     --cov=modules.<module_name> --cov-report=term-missing
   ```

---

## MQTT Test Framework

Live and offline tests for MeshCore packet parsing using real broker data.

### Architecture

```
tests/
  mqtt_test_config.ini        # broker / topic / timeout settings
  test_mqtt_live.py           # schema + fixture + live test classes
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

| Key               | Default           | Notes                                              |
|-------------------|-------------------|----------------------------------------------------|
| `broker`          | `10.0.2.123`      | LAN MQTT broker (plain TCP, no auth)               |
| `port`            | `1883`            | —                                                  |
| `transport`       | `tcp`             | Use `websockets` for letsmesh TLS broker           |
| `topic_subscribe` | `meshcore/SEA/+/packets` | `+` wildcard matches any station             |
| `timeout_seconds` | `15`              | Seconds to wait for packets                        |
| `max_packets`     | `10`              | Collection limit                                   |

letsmesh alternative (commented out): `mqtt-us-v1.letsmesh.net:443` WebSocket/TLS — requires
JWT auth; not suitable for anonymous CI runs.

### Packet schema

| Field         | rx | tx | Notes                              |
|---------------|----|----|------------------------------------|
| `origin`      | ✓  | ✓  | Sender display name                |
| `origin_id`   | ✓  | ✓  | 64-char hex pubkey                 |
| `timestamp`   | ✓  | ✓  | Unix epoch (int or float)          |
| `type`        | ✓  | ✓  | Always `"PACKET"`                  |
| `direction`   | ✓  | ✓  | `"rx"` or `"tx"`                   |
| `packet_type` | ✓  | ✓  | `"0"`–`"15"` string                |
| `route`       | ✓  | ✓  | `"F"` flood / `"D"` direct / `"T"` tunnel / `"U"` unknown |
| `SNR`         | ✓  | ✗  | String float, −200…+30 dB          |
| `RSSI`        | ✓  | ✗  | String int, −200…0 dBm             |
| `hash`        | ✓  | ✗  | 16-char uppercase hex              |

### Fixture auto-refresh

`test_received_at_least_one_packet` (live test) calls `_save_fixture_packets()` after each
successful run — so `mqtt_packets.json` is updated automatically whenever live tests pass.

---

## CI Integration

Tests run automatically on push/PR via GitHub Actions.

| Job             | Command                                                   |
|-----------------|-----------------------------------------------------------|
| `lint`          | `ruff check modules/ tests/`                              |
| `typecheck`     | `mypy modules/`                                           |
| `lint-frontend` | ESLint + HTMLHint on `modules/web_viewer/templates/`      |
| `lint-shell`    | ShellCheck `--severity=warning` on all `.sh` files        |
| `test`          | `pytest tests/ -v --tb=short` with coverage (no `mqtt`)   |

To keep `TODO.md` in sync locally:

```bash
python scripts/update_todos.py
```

Or wire it up as a pre-commit hook (see `TODO.md` → Auto-Update section).
