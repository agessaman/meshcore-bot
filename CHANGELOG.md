# Changelog

All notable changes to this project are documented here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
semantic versioning.

## [Unreleased]

### Fixed

- Refactor response_template parsing to use finite state machine when processing
  templates adding additional flexibility such as the use of nested fields. Added
  support for shlink to External_Data configuration. Added url_shortener filter and
  if_notempty filter to response_template.

- `path` no longer answers "No path information available in current message" on a
  busy mesh (#255). Verifying a channel message against the RF cache only ever
  checked the newest row, which assumes the RF log row and the decoded CHAN event
  for one reception arrive back to back with nothing in between. On a dense mesh
  they do not: a repeater's echo of the very same packet is routinely logged in the
  gap. The reporter's message was heard directly (`SNR 13.25`, 0 hops) and again via
  repeater `f0` 185 ms later (`SNR 12.0`, 1 hop); both rows carry packet hash
  `392926C85DCB87D0`, but the check saw only the echo, disagreed on path length and
  SNR, and left the route unresolved — so the bot withheld a path it had decoded
  correctly. The cache is now searched for the row the payload matches instead of
  testing just the most recent one. Rows that agree must resolve to a single packet
  hash, so two unrelated packets that happen to agree stay a fallback and #80's
  guarantee is unchanged: a route is still only ever attributed on evidence. SNR and
  RSSI now come from the message's own reception too, rather than from whichever
  packet was heard last.

- MQTT brokers no longer flap in a reconnect storm (#248). Three things stacked up.
  First, the packet-capture watchdog ran `client.reconnect()` from its own thread
  every 30 seconds whenever `is_connected()` was false — which includes every moment
  paho's network thread is inside its own backoff. Two threads driving one client's
  socket produced duplicate CONNACKs, spurious `MQTT_ERR_PROTOCOL` disconnects, and a
  fixed-interval retry that flattened paho's 1→120s backoff into a hot loop. The
  watchdog now observes, refreshes an expiring auth token so paho's next attempt can
  succeed, and only intervenes when the network thread is gone and nothing is
  retrying at all. Second, the generated client ID had no per-broker component, so
  every broker in the process connected under the same ID; two hostnames belonging to
  one cluster (`mqtt-a` and `mqtt-b` of the same service) evicted each other's session
  on a six-second cycle. IDs are now distinct per broker. Third, a disconnect logged a
  bare `rc=`, which reads against the CONNACK table even though paho reports
  `MQTT_ERR_*` there — `rc=2` is a protocol error, not "client identifier rejected" —
  so it now names the code.

- A renewed MQTT auth token is now actually put in force. MQTT presents credentials
  once, at CONNECT, so `username_pw_set` on a live session changed nothing and the
  connection kept running on the token it was opened with until the broker evicted it
  at that token's `exp`. Renewal is now followed by a clean, serialized reconnect
  (`disconnect` → `loop_stop` → `reconnect` → `loop_start`, in that order, so the
  network thread is joined before anything else touches the socket). Set
  `mqttN_jwt_reconnect_on_renew = false` for a broker that ignores expiry on live
  sessions.

- Startup config-lint findings now go to the log file. They were printed to stderr
  before the bot (and therefore its logger) existed, so under systemd they reached
  only the journal and never `logs/meshcore_bot.log`. The linter had correctly
  reported `[Test_Command] unknown key 'alias'. Did you mean 'aliases'?` on three
  consecutive startups without that ever being visible to the operator reading logs.
  Findings are still collected before anything opens the database, and stderr remains
  the fallback when bot construction fails, which is when a config problem is the
  likeliest cause.

- `{hops}` and `{hops_label}` in a `[Keywords]` response now report the same hop count
  a command response would for the same packet. The keyword formatter carried its own
  implementation that consulted only `message.hops` and the path display string, so it
  answered `?` whenever the count was known only from `routing_info`, and preferred a
  stale path string over the packet's own `path_length` when both were present. All
  three formatters now share `utils.message_hop_count`.

- `pathbytes_min` no longer treats a direct message as a multi-byte path, so
  `{path_distance|pathbytes_min:2|prefix_if_nonempty: | Path Dist: }` stops printing
  `| Path Dist: N/A` on a hopless packet. `bytes_per_hop` describes how a path is
  encoded, and a direct packet has no path for it to describe, so
  `bytes_per_hop_from_routing_and_nodes` now returns 1 whenever the packet reports no
  hops—which is what its docstring already claimed. The bug was latent until channel
  messages started carrying routing info.

- Channel messages are now tied to their own RF packet, so `{packet_hash}`, `{path}`
  and `{connection_info}` resolve on a channel instead of coming back empty or
  `Unknown`. MeshCore's CHAN event carries neither `raw_hex` nor a pubkey prefix, so
  none of the prefix strategies in `find_recent_rf_data` can fire and every channel
  message fell through to the most-recent-packet fallback, which #80 correctly
  refuses to attribute a route from. The decoded payload does restate three things
  the RF row records independently—payload type, path length and SNR—so the fallback
  can now be checked instead of assumed. SNR is the discriminating field: it is one
  reception's measured value, quantised to 0.25 dB. Across 55 channel messages in my
  logs the immediately preceding RF row agreed on all three every time, and a row
  that disagrees, or that is older than the correlation window, stays a fallback.
  Side effect worth knowing: verified channel messages now contribute their path to
  the mesh graph, which they never did before.

- `flood_scopes = *` no longer drops every channel message. Requiring RF data
  correlated to the message before `*` could authorize a reply looked reasonable,
  but MeshCore's CHAN payload carries neither `raw_hex` nor a pubkey prefix, so a
  channel message has no correlation key at all and always lands on the
  most-recent-packet fallback. The gate could therefore never pass: across three
  log files every one of 45 received channel messages was rejected. `*` now also
  accepts the window-wide absence of scope-eligible traffic as proof: a scoped
  message travels as TRANSPORT_FLOOD GRP_TXT, so if the radio heard no such packet
  while the message arrived, the message cannot have been scoped. Unlike a route
  type read off a fallback row, that doesn't depend on having picked the right
  cached packet, so genuinely ambiguous cases—a TC_FLOOD GRP_TXT heard alongside
  the message—still fail closed.

- The web viewer's radio **Disconnect** button is now **Stop Bot** and asks for
  confirmation first (#240). It never was a radio-only disconnect: the main loop runs
  while the bot is connected, so disconnecting ended the process, which surprised at
  least one operator running under `tmux` with nothing to restart it. The confirmation
  spells out that the bot stops completely and only returns if systemd or Docker
  restarts it.
- Corrected the `[External_Data] repeater_prefix_api_url` comment, which claimed that
  leaving it empty "disables prefix command functionality" (#70). Empty is the normal
  setup: the prefix command answers from the bot's own database. The option only adds
  an optional external dataset, and its JSON contract is now documented.
- `message_stats.path` no longer reports another packet's route (#80). When RF
  correlation failed, `find_recent_rf_data` fell back to the most recent packet in the
  cache, and the caller attributed that packet's route to the message — which is how a
  multi-hop message was occasionally recorded as a single direct hop. Correlation
  results are now tagged with how they were matched, and an uncorrelated fallback is no
  longer allowed to supply a route for either channel messages or DMs. The route is
  left unresolved instead of being fabricated. This also stopped a wrong edge being
  written to the mesh graph, and stopped the `path` command receiving another packet's
  `routing_info`. SNR and RSSI still use the fallback as before.
- Dashboard **One-hop neighbours** now lists radios this node heard directly
  (MeshCore hop count 0: empty RF path), not originators of 1-hop relayed
  adverts. Empty-path adverts are stored in `observed_paths` with SNR/RSSI;
  neighbor-discover cycles refresh SNR on those rows. A one-time backfill
  copies recent zero-hop ADVERTs out of `packet_stream`.
- **Airplanes / ADS-B** no longer depends on the public airplanes.live API,
  which now returns HTTP 403 for unregistered clients (#244). Default
  endpoint is `https://api.adsb.lol/v2/`; existing `api_url` values pointing
  at `api.airplanes.live` are remapped automatically. Local readsb URLs are
  unchanged.
- `cmd` no longer lists commands that are disabled in `config.ini`. Commands
  with no `[<Name>_Command]` section at all are still listed, as before.
- Stale-contact cleanup no longer retries forever (#176). When the device refuses to
  remove a contact the contact stays in the list, so every sweep re-selected it and
  logged the same failure again — hundreds of `Failed to remove stale contact` warnings
  that only a restart cleared. A contact is now dropped from cleanup after
  3 consecutive refusals, with one summary warning explaining that the list may stay
  near its limit. A successful removal clears the count.
- Contacts whose device clock was never set are no longer treated as stale. MeshCore
  seeds an unset clock with a hardcoded time — `1715770351` (15 May 2024) or
  `1772323200` (1 Mar 2026) — so a never-synced node advertises that seed rather than
  a real observation. The bot read it as extreme staleness, which put unsynced but
  perfectly active contacts at the top of the removal list, consuming the whole
  per-sweep budget and repeatedly trying to evict live nodes. This is what the
  `722 days ago` entries in #176 were: the 15 May 2024 seed, not contacts last heard
  in 2024. A raw `0` (decoding to 1970) is covered too, and genuine adverts near a
  seed are unaffected.
- The same unset-clock check now guards every purge path, not just stale-contact
  cleanup. `_get_repeaters_for_purging`, `_get_companions_for_purging` and
  `purge_old_repeaters` all ranked an unset clock as maximum age, so an active node
  that had never been time-synced was the *first* candidate for eviction. Unknown
  staleness is no longer grounds for removal.

### Changed

- Web viewer navigation is grouped: Radio, Scheduled Messages, Greeter, Feeds, Plugins
  and Configuration now sit under a single **Settings** gear menu, leaving Dashboard,
  Real-time, Contacts, Mesh Graph and Logs on the bar. The current page is highlighted,
  including the gear when a settings page is open.

### Added

- `mqttN_keepalive` (default 60) sets the MQTT PINGREQ interval per broker. It was
  hardcoded at 60 before, which is long for websockets through a proxy that drops
  idle connections.

- `hops_min:N` response-template filter, alongside `pathbytes_min:N`. It clears a
  field unless the message actually travelled at least N hops, so
  `{firstlast_distance|hops_min:1|prefix_if_nonempty: | F/L Dist: }` drops the whole
  clause on a direct message. The distance placeholders render `N/A` when there is no
  path, and `prefix_if_nonempty` treats that as a value and prints its label, so a
  gate was needed; `pathbytes_min` was the only one available and it asks how the path
  is *encoded*, which meant throwing away a measurable one-byte multi-hop distance to
  suppress the direct case. `hops_min` asks about the route instead. An unknown hop
  count clears the field rather than guessing.

- `{packet_hash}` placeholder for `[Keywords]` responses, the test command's
  `response_format` and the path command's `reply_prefix`: the 16-char MeshCore
  packet identity hash (uppercase hex) of the packet that carried the request, so a
  reply can be tied back to a specific transmission when comparing paths. It comes
  only from the routing info of an RF packet actually correlated to the message, and
  renders empty otherwise, so a hash from an unrelated transmission is never shown.

- **Scheduled messages can be managed from the web viewer** (#174). A new Schedule page
  lists every `[Scheduled_Messages]` entry with its next run time and offers add, edit
  and delete. Changes are written to `config.ini` and applied by a queued config reload,
  so no restart is needed. The schedule builder composes the cron key from plain-language
  options and previews the next five runs; entries the bot cannot run are shown as
  **Not scheduled** with the reason rather than hidden. It edits the same config section
  the bot already uses, so there is no second source of truth.
- Documented installing with `pipx`, which sidesteps PEP 668 on Debian 12+, Ubuntu
  23.04+, Fedora and Arch (#222), including where `config.ini`, the database and
  `local/` live — everything resolves relative to the config file's directory, so an
  absolute `--config` is what makes a pipx install deterministic.
- Migration 23: nullable `snr` / `rssi` columns on `observed_paths` for
  zero-hop advert rows.
- `{cmd:<command> [args]}` placeholders in `[Scheduled_Messages]`: a scheduled message
  can embed the reply of any bot command, so a recurring forecast is
  `0 6,12,18 * * * = Public:{cmd:wx Seattle}` rather than a per-service schedule
  setting. The command runs for its text only and transmits nothing itself
  (`CommandManager.render_command_output`); unknown, disabled, admin-only, timing-out
  and silent commands expand to nothing rather than airing raw placeholder text.
  Bounded by the new `[Bot] scheduled_command_timeout_seconds` (default 30). Two
  non-configurable airtime guards apply: a schedule using `{cmd:...}` must not fire
  more often than every 15 minutes (rejected at startup, measured by the tightest gap
  so `0,1 * * * *` counts as 60 seconds), and the command's own `cooldown_seconds` is
  still enforced.
- `{path_distance}` is now available in the path command's `[Path_Command] reply_prefix`,
  reporting total distance travelled (sender → hops → bot, e.g. `12.4km`) and rendering
  empty when any node in the chain has no usable coordinates. The prefix now supports the
  same pipe filters as the test command's `response_format`, so
  `{path_distance|prefix_if_nonempty:📏 }` drops the label along with the value.
- `install-service.sh --install-extras` installs the optional profanity-filter and
  geocoding packages without prompting, for unattended installs and upgrades. It
  takes precedence over the in-place `--update-venv` path, so the two can be
  combined.
- `[PacketCapture] observer_name` — an optional name reported as the `origin` of
  MQTT packet and status payloads. It lets the observer/analyzer identity differ
  from the MeshCore RF node, which is useful when one bot name is already taken
  by the radio's advertised name. Unset (the default) keeps the previous
  behavior: the connected device name, falling back to `[Bot] bot_name`.

## [1.0.0] — 2026-08-07

v1.0.0 marks the first stable release. It adds zero-hop neighbor discovery, a
rebuilt snapshot-backed web-viewer dashboard, transport recovery, location and rain
improvements, World Cup support, safer feed and outbound HTTP handling,
command-prefix enhancements, and substantial web-viewer performance and security
work.

The configuration format, command syntax, service layout, and web-viewer API are now
considered stable; breaking changes to them will come with a major version bump.

### Added

- Zero-hop neighbor discovery in the packet capture service, ported from
  `meshcore-packet-capture` (itself a port of the observer firmware's neighbors
  feature). On a long interval (12–336 h, default 24) the bot asks which
  repeaters it hears **directly** and records each confirmed link with its
  measured SNR. `[PacketCapture] neighbors_enabled` is the single switch and is
  off by default; every enabled broker publishes the snapshot once it is on
  (`mqttN_neighbors` defaults true, so set it false to hold a broker back). The
  neighbors topic is derived from each broker's packets topic by swapping the
  last segment, so a templated broker gets
  `meshcore/{IATA}/{PUBLIC_KEY}/neighbors` — the topic the firmware uses. A
  derived location-routed topic is skipped with a warning when no `iata` is set,
  rather than publishing into `meshcore/XYZ/...`. Snapshots are non-retained,
  because `heard_secs_ago` is relative to publish time.
- Confirmed direct links are now the strongest evidence class in the database:
  two full 32-byte public keys plus a first-party RF measurement, where path
  inference has only 1–3 byte prefixes and no keys. Stored in `neighbor_links`
  (adjacency, migration 22) and `neighbor_observations` (per-cycle history,
  pruned by `neighbor_observations_retention_days`, default 365).
- Mesh graph integration: a **Neighbors Only** evidence mode on the mesh page
  and `GET /api/mesh/edges?evidence=neighbors`, deriving edges purely from
  `neighbor_links`. Unlike the multi-byte mode these edges carry populated public
  keys and real SNR, and they render as heavier lines. Confirmed neighbors are
  also labelled as such in the combined view, and count as provenance-trusted
  when framing the initial map. `neighbors_feed_mesh_graph` (default on) also
  writes them to `mesh_connections`.
- `neighbors` DM command to run one cycle on demand — the scheduled interval has
  a 12 h floor, which makes testing impractical otherwise. Acknowledges
  immediately and reports in a second DM once the listen window closes. Worth
  adding to `[Admin_ACL] admin_commands`, since a cycle spends airtime.
- Optional region-scope collection (`neighbors_collect_scopes`), **off by
  default**: each request holds the bot's single radio command lock for up to
  ~25 s, and for a repeater with no stored path the meshcore library reaches
  zero-hop by temporarily rewriting that contact's path on the device. The
  default cycle costs one radio command plus a passive listen window, during
  which the bot stays fully responsive.
- Automatic serial, BLE, and TCP transport reconnect handling, including service
  plugin re-subscription after reconnect.
- Minute-level rain and precipitation nowcasts with optional proactive notifications.
- World Cup command and live event announcement service.
- Opt-in sender-language detection for localized greeting replies, with
  keyword-first detection and an optional `langdetect` extra for longer text.
- Centralized location resolution and geocoding helpers shared by weather, AQI,
  path, and related commands.
- Web-viewer plugin settings, node settings, multi-byte evidence views, and
  paginated contacts APIs.
- Database restore tooling and hardened service-layout migration for configuration,
  state, logs, and local plugins.
- Flexible command prefixes: single, multiple, or decorative prefixes, with
  permissive and strict matching modes and optional bare commands.
- Per-channel flood scope configuration for more granular message routing.
- Optional packet-capture payload decoding — `GRP_TXT` channel messages are
  decrypted and `ADVERT`s parsed into a nested `decoded` object. Publishing it to
  MQTT is off by default and set per broker via `mqttN_include_decoded`.
  Packet-log rotation (off/size/time) is configurable.
- `{hops}` and `{hops_label}` placeholders for path command replies, and an RSSI
  placeholder for test command responses.
- Configuration is validated on every startup, surfacing misspelled sections and
  keys that previously failed silently. `validate_config.py --strict` checks a
  config before upgrading.
- DARC MOWAS alerts map German region IDs (*Regionalschlüssel*) to MeshCore scopes,
  limiting each alert to the regions it was issued for.
- NWS gridpoint data as the US precipitation-nowcast source.
- A tracked `LICENSE` file (MIT) and matching `pyproject.toml` license metadata, so
  built wheels and packages carry the license the README has always declared.

### Changed

- `meshcore` minimum raised from 2.3.6 to 2.3.8. Required for
  `send_node_discover_req` / `req_regions_sync`, and for the bounded, serialized
  BLE write.
- Rebuilt web-viewer dashboard, served from a background snapshot instead of
  recomputing statistics on every request. A refresher thread in the viewer
  process writes `daily_rollup` (one row per local date) and
  `dashboard_snapshot` (a single JSON row), so a page load reads one row rather
  than running ~50 aggregate queries five times over.
- Daily trends that outlive retention: `message_stats` and friends are pruned at
  7 days and `packet_stream` at 3, so a 30-day chart had nothing to draw from
  until now. Signal metrics are stored as sums and counts, never means, so any
  window re-aggregates correctly.
- New `/api/dashboard/{summary,series,top,windows,refresh}` endpoints. `summary`
  sends a strong `ETag`, so the page's 30-second poll is normally a bodyless
  `304`, and polling stops entirely while the tab is hidden.
- New dashboard tiles and charts: routing mix (flood vs direct), hop-count and
  path-length histograms, a 30-day multibyte adoption trend, busiest repeaters,
  and a role mix.
- One-hop neighbors panel, with a 24-hour / 7-day selector: the nodes whose
  advert reached this radio in a single hop, weakest measured link first.
  Membership is derived from observed path evidence rather than from
  `complete_contact_tracking.hop_count`, which is not trustworthy — it claims
  800 zero-hop contacts on the live database while only 68 have any one-hop
  path to corroborate it, their stored SNR piles up in a 1.5 dB band (655 of
  800 between 11.25 and 12.75 dB), and their RSSI clusters near -45 dBm. That
  is the signature of one strong local link being recorded against every node
  whose traffic arrived through it, not of hundreds of separate radios. SNR is
  therefore shown only where the path evidence and the stored hop count agree;
  the rest read "no signal reading" rather than borrowing another link's
  measurement.
- Payload-type mix beside the routing mix, over the same packets — what the
  traffic is, next to how it is routed. Category lists roll their tail into
  "Other" rather than truncating, so the bars still sum to the total printed
  beside them.
- Hop-distance chart carrying two distributions: nodes by their closest advert
  path, and arriving flood packets by how far they had already travelled. One
  counts nodes and the other packets, so both are drawn as a share of their own
  total with raw counts in the tooltip. On the live mesh nodes peak at 2-3 hops
  and fall away quickly while flood traffic peaks at 5 with a much longer tail —
  a nearby neighborhood absorbing flood from well beyond it.

  Note that the two source tables measure paths in **different units**:
  `observed_paths.path_length` is a byte count, so hops are
  `path_length / bytes_per_hop` (a 3-hop multibyte path is 6 or 9), while
  `packet_stream.path_len` is already a hop count with its byte length kept
  separately as `path_byte_length`. Applying either rule to the other table
  silently rescales an axis; both are pinned by tests. This replaces the
  earlier raw path-length chart, which read bytes as hops, and the chart built
  on the untrustworthy stored hop count.

  Hop buckets holding under 0.1% of the flood series are not drawn — the tail
  decays for around twenty hops in bars under a pixel tall — and the amount
  withheld is stated beneath the chart (1,467 packets, 0.9%, on the live mesh,
  taking the axis from 64 buckets to 44). Percentages remain shares of the full
  series rather than of the drawn subset, so hiding the tail cannot inflate the
  bars that remain. The node series is never thresholded.

  The chart still computes the full protocol range: a 64-byte path is 64 hops
  at one byte per hop. The old dashboard's `BETWEEN 0 AND 32` filters, carried forward
  at first, discarded 5,654 flood packets arriving from as far as 63 hops, and
  because that limit applies after the per-node minimum it would erase a node
  whose closest path was longer than 32 hops rather than plotting it at the far
  end.
- New `[Web_Viewer]` settings: `dashboard_snapshot_enabled`,
  `dashboard_snapshot_interval_seconds`, `dashboard_snapshot_history_days`, and
  `dashboard_packet_backfill_rows`.
- Partial index `idx_packet_stream_undimensioned` as the backfill worklist.
  Probing for remaining un-dimensioned rows was otherwise a full table scan,
  and it cost the same 4.6 seconds *after* the backfill finished as during it,
  because finding nothing still meant reading everything.
- Time-window selectors are now built from each source's configured retention.
  The dashboard previously offered "30d" and "All" against tables pruned at 7
  days, so three of the four choices returned the same number under a label
  that claimed otherwise.
- The incoming-packet chart no longer claims to show 7 days. `packet_stream` is
  pruned at 3 days, so the covered window is now measured and labelled from the
  data — and shown beside a genuinely 7-day contacts chart it can be compared
  to.
- `packet_stream` gained denormalized `route_type_name`, `payload_type_name`,
  `path_len`, and `bytes_per_hop` columns, written at capture time. Aggregating
  these via `json_extract` cost 3–6 seconds per query on a large database.
  Existing rows are converted a bounded batch per refresh rather than in one
  table-rewriting migration.
- Dashboard JavaScript and CSS moved to static files, removing the CSP nonce
  requirement for the bulk of the page's code.
- `cleanup_old_stats` now also deletes rows dated implausibly far in the future.
  Such rows are never older than the retention cutoff, so they were immortal —
  observed in the wild dated 2103.
- Multi-byte mesh graph path splitting and aggregation now run in SQLite
  instead of materializing every retained path in Python. Time windows are
  applied after lifetime edge coalescing to preserve existing graph identity
  and count semantics. Graph startup also skips an unnecessary sort, and a
  table-specific `mesh_connections(last_seen)` index supports window and
  retention queries.
- Graph persistence defaults to batched writes for new installations. A flush
  uses one upsert batch and transaction instead of probing every edge before
  writing it, reducing WAL churn and SD-card writes while preserving immediate
  and hybrid strategies as explicit options.
- Feed polling now has bounded response and item limits, duplicate queue protection,
  per-feed serialization, and configurable post limits.
- Direct-message responses are split at MeshCore byte limits without breaking UTF-8.
- Mesh graph and contacts queries scope enrichment work to the requested page or
  visible data.
- Service installs keep executable code root-owned while configuration and runtime
  state remain writable only by the service account.
- The help command now respects its own `channels` override, falling back to the
  global `monitor_channels` only when no help command is loaded.
- The webhook service starts before the radio connection and returns HTTP 503
  until the bot is connected, narrowing the window for connection refusals.
- Weather alerts recognize NWS responses that mean "no coverage here" and stop
  reporting them as errors.

### Deprecated

- `GET /api/stats`. Every key name is preserved and the response now carries
  `Deprecation` and `Sunset` headers; use `/api/dashboard/*` instead. It will be
  removed at the next major version.

### Removed

- The orphaned `/stats` page, which was unreachable from the navigation and
  rendered stub charts that never populated.
- The dashboard's Live Activity feed. It opened three SocketIO subscriptions
  and re-rendered on every packet to duplicate a page that already exists at
  `/realtime`, so the dashboard now costs one snapshot read per poll and
  nothing else.

### Fixed

- Service installers no longer leave `venv/bin/pip` (and other console scripts)
  with shebangs pointing at the temporary `.venv-build-$$` path after the atomic
  virtualenv swap. Optional package prompts and documented pip invocations now
  use `venv/bin/python -m pip`. `--update-venv` rewrites shebangs in place so
  already-broken installs heal without a full rebuild, and the previous venv is
  kept until rewrite succeeds (issue #229).
- Data retention now runs shortly after startup and then daily. It no longer
  requires 24 hours of uninterrupted uptime before the first cleanup, and its
  timer remains independent from the nightly maintenance email.
- Retention deletes now commit in configurable chunks and yield between
  batches, preventing a large first cleanup from monopolizing SQLite's writer
  lock on SD-card installations.
- Linux service installers now use 1GB memory and 200% CPU ceilings, providing
  Raspberry Pi graph and web-viewer workloads with practical headroom.
- The systemd restart limit now takes effect. `StartLimitInterval`/
  `StartLimitBurst` were set under `[Service]`, where systemd 230 and later
  ignore them, so the unit silently fell back to the system defaults of 5 starts
  in 10 s. Paired with `RestartSec=10`, which spaces attempts further apart than
  that window, the limiter could never trip and a bot that could not reach its
  radio restarted every 10 seconds indefinitely. Moved to `[Unit]` as
  `StartLimitIntervalSec=60` / `StartLimitBurst=3`, so a persistent failure now
  stops in `failed` state after three attempts. Fixed in both the shipped
  `meshcore-bot.service` and the unit generated by `scripts/build-deb.sh`.
- The mesh map now coalesces live edge events, serializes graph reloads, pauses
  hidden-tab refreshes, and caches concurrent multi-byte aggregation requests,
  preventing an open graph page from creating a CPU and SQLite I/O request
  storm on busy meshes.
- The web-viewer rotating-file handler now honors `[Logging] log_level`, while
  its journal handler retains an INFO floor to avoid duplicate debug writes.
- Closed outbound HTTP SSRF bypasses, including IPv4-mapped IPv6 and redirect/DNS
  rebinding cases.
- Hardened configuration reload rollback, scheduler operation claims, feed queue
  deduplication, and blocking weather-provider calls.
- Escaped user-controlled web-viewer content and neutralized Discord mentions.
- Restored Python 3.10 compatibility and expanded CI coverage through Python 3.13.
- `NEW_CONTACT` adverts are classified as known or new instead of always being
  logged as newly discovered.
- The standalone installer preserves custom alternative commands and symlinks, and
  rolls back a partial executable sync rather than restarting a half-updated tree.
- Startup validation now actually reports unknown and misspelled keys — including
  in `*_Command` sections — with a "did you mean" suggestion, instead of only
  checking section names and a hardcoded `[Connection]` pair.
- `!aqi`, `!rain`, `!snow`, `!aurora`, `!prefix`, `!alert`, and `!gwx` no longer
  block the event loop while geocoding; location resolution runs off-thread like
  the forecast fetch already did. `!prefix` was the worst case, reverse-geocoding
  once per matching repeater with the loop stalled throughout.
- The Nominatim rate limiter reserves its slot before the request instead of
  recording it afterwards, so concurrent geocodes can no longer clear the gate
  together and breach the 1 req/s policy. The geocode caches are locked against
  concurrent eviction.
- The web viewer footer and the `!version` command agree on dev and detached-tag
  checkouts; a detached checkout on a release tag reports that tag rather than
  `HEAD-<sha>`.
- `[Feed_Manager]` numeric limits are clamped to sane minimums. `max_items_per_check`
  below 1 no longer takes Python's negative-slice meaning; `max_posts_per_check` is
  enforced before an item is sent rather than after, while configured values below
  1 are clamped to 1; `feed_request_timeout` below 1 no longer disables the HTTP
  timeout outright; and `max_message_length` below 4 no longer lengthens the message
  it is meant to cap.

### Notes for downgrades

All schema changes are additive — two new tables, four new nullable columns, and
new indexes — so the data itself is safe to read with older code. However,
`MigrationRunner` fails startup on encountering an applied migration version it
does not know about, so downgrading below this release requires deleting the
corresponding `schema_version` rows.

### Contributors

Thanks to [@rlwilliamson-dev](https://github.com/rlwilliamson-dev) for the rain
nowcast work and the NWS gridpoint source, and to
[@fmoessbauer](https://github.com/fmoessbauer) for the MOWAS region-scope mapping
and code-style fixes.

## [0.9.3] — 2026-05-30

### Changed

- Bridged Discord messages set `allowed_mentions` to an empty list, so `@everyone`,
  `@here`, and role mentions arrive as plain text instead of pinging the channel.

### Documentation

- Expanded the command reference for `cmd`, `version`, `weather`, and `path` with
  usage examples and configuration options, and documented the `RandomLine`
  configurable triggers.
- Marked the global `[Aliases]` section deprecated in favor of per-command
  `aliases =` keys, and clarified the `[Rate_Limits]` and `[Webhook]` sections.
- Emphasized web-viewer security practices in the viewer documentation.

## [0.9.2] — 2026-05-17

### Fixed

- Webhook channel lookup strips a leading `#`, so posts match hashtag channels
  cached from the radio.
- The webhook endpoint returns HTTP 500 when the mesh send fails, instead of
  reporting success.

### Changed

- Packet capture applies log levels from its own verbose/debug settings rather
  than setting the global logger level, and logs a per-packet summary whose level
  follows those flags.
- Clarified how `outgoing_flood_scope_override` and `flood_scopes` interact, with
  more informative scope-resolution logging and RF-correlation eligibility checks.
- Corrected the documentation URL in the systemd unit and the command User-Agent.

## [0.9.1] — 2026-05-16

The theme of this release is flood-scope control: which slice of the mesh a given
outgoing message is flooded to.

### Added

- Optional regional `TC_FLOOD` scope configuration across services (weather,
  earthquake, webhook). `CommandManager` resolves the scope from the incoming
  message, the owning config section, or an explicit parameter.
- Optional flood scope for scheduled channel messages via `channel:#scope:body`
  in `[Scheduled_Messages]`.
- Five-field cron expressions and preset aliases for `[Scheduled_Messages]`. The
  legacy `HHMM` form is still parsed and warns.
- `reply_prefix` and `minimum_path_bytes` settings for the path command.
- `[Test_Command] response_format` supports piped path filters (`pathbytes_min`,
  `prefix_if_nonempty`) and takes priority over `[Keywords]`.
- Global and per-broker MQTT JWT settings: `jwt_ttl_seconds` and
  `jwt_renewal_interval`.
- `send_channel_message` accepts an explicit timestamp, enabling bit-identical
  message replication and chronological display ordering.
- DARC MOWAS retransmits bit-identical messages when a repeater ack is missing,
  so an emergency alert is not lost to a dropped ack.

### Fixed

- Direct-message responses route by `sender_pubkey` rather than `sender_id`,
  preventing misrouting when several nodes share a display name.
- Keyword and `RandomLine` channel replies now carry their configured flood scope.
- Scheduled sends are staggered by a deterministic delay
  (`scheduled_message_max_stagger_seconds`, default 1.5) and skip the global user
  rate limit, so simultaneous jobs are no longer dropped. Per-channel and
  `bot_tx` limits still apply.
- Mesh graph pending-update flushing no longer deadlocks.
- The feed manager checks lock status before acquiring it to prevent coroutine
  pileup, and the scheduler processes messages without blocking its main thread.
- Advert flag parsing uses bitwise operations so invalid flag values degrade to a
  warning instead of failing to parse.
- DARC MOWAS message chunks get ascending timestamps, giving receivers correct
  ordering and deduplication.
- Service names strip leading and trailing underscores, so `<foo>_Service`
  resolves to `<foo>` rather than `<foo>_`.

### Contributors

Thanks to [@fmoessbauer](https://github.com/fmoessbauer) for the MOWAS
reliability work and the service-name fix (#182, #183).

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

[1.0.0]: https://github.com/agessaman/meshcore-bot/compare/v0.9.3...v1.0.0
[0.9.3]: https://github.com/agessaman/meshcore-bot/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/agessaman/meshcore-bot/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/agessaman/meshcore-bot/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/agessaman/meshcore-bot/compare/v0.8.3...v0.9.0
