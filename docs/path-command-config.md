# Path Command Configuration Guide

This document explains all configuration parameters for the Path Command, which decodes hex path data to identify repeaters in message routing paths.

## Multi-byte path support

The path command supports **1-, 2-, and 3-byte-per-hop** paths (2, 4, or 6 hex characters per node).

- **`path` with no arguments**: Uses the current message’s decoded path when available (from routing info). No re-parsing; node list and hop size come from the packet.
- **`path <hex>` with arguments**:
  - **Comma-separated** (e.g. `path 0102,5f7e`): Hop size is inferred from token length. All tokens must be the same length (2, 4, or 6 hex chars). Example: `0102,5f7e` → two 2-byte hops.
  - **Continuous hex** (e.g. `path 01025f7e`): The bot’s [Bot] **`prefix_bytes`** is used (2 hex chars = 1 byte, 4 = 2 bytes, 6 = 3 bytes). Use comma-separated input to force a multi-byte interpretation when the bot is in 1-byte mode.

## Reply prefix and repeater name gating

These options only affect the **path** command’s reply text and whether repeater names are resolved from the database.

**`reply_prefix`** (string, default empty)

- Prepended as the first line of path command RF replies (only the **first** chunk when the reply is split for length).
- Placeholders: `{sender}`, `{connection_info}`, `{path}`, `{hops}`, `{hops_label}`, `{timestamp}`, `{snr}`, `{rssi}`, `{packet_hash}`, `{path_distance}`.
- `{path_distance}` is the total distance travelled, summed sender → each resolved hop → bot (e.g. `12.4km`). It is **empty** whenever the chain cannot be measured end to end: an unresolved hop, a prefix collision, a node with no stored coordinates, an unknown sender position, or no `[Bot] bot_latitude`/`bot_longitude`. A partial sum is never reported, since it would understate the real distance.
- `{packet_hash}` is the 16-char MeshCore packet identity hash (uppercase hex) of the packet that carried the request. It is **empty** when RF correlation could not tie a heard packet to this message, so a hash from an unrelated transmission is never shown.
- Supports the same **feed-style pipe filters** as the test command's `response_format` (see `modules/response_template.py`). Use `prefix_if_nonempty` so a label disappears along with an empty distance:

```ini
reply_prefix = "{path_distance|prefix_if_nonempty:📏 }\n"
```
- `hops_min:N` clears a field unless the message actually travelled at least N hops. `{path_distance}` renders `N/A` on a direct message, which `prefix_if_nonempty` treats as a value, so gate it first: `{path_distance|hops_min:1|prefix_if_nonempty:📏 }`. Unlike `pathbytes_min:N`, which asks how the path is *encoded*, this keeps a measurable one-byte multi-hop path.
- `if_notempty:LITERAL` renders `LITERAL` when the value is non-empty after prior filters, and clears entirely otherwise — the opposite pairing of `prefix_if_nonempty`, useful when the whole output should be a fixed (or field-built) literal rather than the value with a label prepended. Since `{packet_hash}` is empty whenever RF correlation fails, gating on it hides the whole clause instead of printing a broken link:

```ini
reply_prefix = {packet_hash|if_notempty:"https://scope.example.net/#/packets/{packet_hash}"}
```
  The `LITERAL` argument may itself be a double-quoted string containing nested `{field}` placeholders (expanded before the filter runs), so the link above still carries the packet hash even though the field being gated on (`packet_hash`) and the field inside the literal are the same one.
- `shorten_url` runs the value through the shared shortener configured under `[External_Data]` (`short_url_website`, `short_url_website_service` — `gd` for v.gd/is.gd-compatible or `shlink`, and `short_url_website_api_key` where required). It falls back to the original, unshortened value if shortening fails or isn't configured, so a clause never silently disappears because of a network error. Chain it after building the link so only the final URL is sent over RF:

```ini
reply_prefix = {packet_hash|if_notempty:"https://scope.example.net/#/packets/{packet_hash}"|shorten_url}
```

**`minimum_path_bytes`** (integer `0`–`3`, default `0`)

- **`0` or `1`**: Always resolve repeater names when decoding a path (legacy behavior).
- **`2` or `3`**: Resolve names only when the packet path uses at least that many **bytes per hop** (from routing metadata or inferred from comma-separated hex width). Otherwise the bot replies with `Path: …` (hex) and a short tip, without a DB lookup.
- **Not** the same as `require_path_bytes_greater_or_equal_to`: that setting can block the path command entirely; `minimum_path_bytes` only gates naming.

## Quick Start: Presets

The Path Command supports three presets that configure multiple related settings:

- **`balanced`** (default): Balanced approach using both graph evidence and geographic proximity
- **`geographic`**: Prioritize geographic proximity over graph evidence (better for local networks)
- **`graph`**: Prioritize graph evidence over geographic proximity (better for well-connected networks)

Set the preset using:
```ini
path_selection_preset = balanced
```

## Core Settings

### Geographic Proximity

**`proximity_method`** (`simple` | `path`)
- `simple`: Use proximity to bot location only
- `path`: Use proximity to previous/next nodes in path (more realistic routing)
- Default: `simple`

**`path_proximity_fallback`** (boolean)
- When path proximity can't be calculated, fall back to simple proximity
- Default: `true`

**`max_proximity_range`** (kilometers, 0 = disabled)
- Maximum distance for geographic proximity consideration
- Repeaters beyond this distance are filtered out or have reduced confidence
- Default: `200` (long LoRa transmission range)

**`recency_weight`** (0.0 to 1.0)
- Controls recency vs proximity weighting
- `0.0` = 100% proximity (only distance matters)
- `1.0` = 100% recency (only when last heard matters)
- `0.4` = 40% recency, 60% proximity (balanced)
- Default: `0.4`

**`recency_decay_half_life_hours`** (hours)
- How quickly recency scores decay for older repeaters
- Default: `12` hours
- For 48-72 hour advert intervals, use `36-48` hours

**`max_repeater_age_days`** (days, 0 = disabled)
- Only include repeaters heard within this many days
- Helps filter out stale repeaters
- Default: `14` days

## Graph-Based Selection

**Prefix length and graph conflation**

The graph stores edges using the bot’s prefix length ([Bot] `prefix_bytes`). Paths from packets can be 1-, 2-, or 3-byte encoded (per sender); when we record edges we normalize to the bot’s prefix. If the bot uses **prefix_bytes=1** (2 hex chars) and the mesh often uses 2-byte paths, **distinct links can be merged**: e.g. 7E42→8611 and 7E99→86FF both become a single edge (7e, 86). That can overcount observations and make path resolution ambiguous when several repeaters share the same short prefix. **Recommendation:** set `prefix_bytes` to match the mesh (e.g. 2 if most traffic is 2-byte) so the graph keeps finer resolution and the mesh viewer shows one node per prefix instead of collapsing many repeaters into one.

**`graph_based_validation`** (boolean)
- Enable graph-based path validation using observed mesh connections
- Default: `true`

**`min_edge_observations`** (integer)
- Minimum edge observations required for graph confidence
- Higher values = more conservative (requires more evidence)
- Default: `3`

**`graph_edge_expiration_days`** (days)
- Edges not observed for this many days are ignored
- Default: `7` days

**`graph_use_bidirectional`** (boolean)
- Check for reverse edges for higher confidence
- Default: `true`

**`graph_use_hop_position`** (boolean)
- Validate candidates appear in expected positions based on observed routing patterns
- Default: `true`

**`graph_multi_hop_enabled`** (boolean)
- Use 2-hop or 3-hop paths to find intermediate nodes when direct edges don't exist
- Default: `true`

**`graph_multi_hop_max_hops`** (integer)
- Maximum hops for multi-hop path inference
- `2` = only 2-hop paths (A->B->C)
- `3` = also try 3-hop paths (A->B->C->D)
- Default: `2`

**`graph_prefer_stored_keys`** (boolean)
- Prioritize candidates whose public key matches stored keys in graph edges
- Stored keys indicate high confidence (+0.4 bonus)
- Default: `true`

## Graph vs Geographic Selection

**`graph_geographic_combined`** (boolean)
- Combine graph and geographic scores into weighted average
- Only combines when both methods select the same repeater
- Default: `false` (uses graph-first fallback)

**`graph_geographic_weight`** (0.0 to 1.0)
- Weight for graph score when combining (only used if `graph_geographic_combined = true`)
- `0.7` = 70% graph, 30% geographic
- Default: `0.7`

**`graph_confidence_override_threshold`** (0.0 to 1.0)
- When graph confidence >= this value, graph overrides geographic selection
- Lower values = geographic gets more consideration
- `1.0` = always prefer geographic when available
- `0.0` = always prefer graph
- Default: `0.7`

## Distance Penalties (Intermediate Hops)

**`graph_distance_penalty_enabled`** (boolean)
- Penalize graph scores for candidates creating long-distance hops
- Prevents selecting very distant repeaters even with strong graph evidence
- Default: `true`

**`graph_max_reasonable_hop_distance_km`** (kilometers)
- Maximum reasonable hop distance before applying penalty
- Typical LoRa transmission: < 30km
- Long LoRa transmission: up to 200km
- Default: `30` (typical transmission range)

**`graph_distance_penalty_strength`** (0.0 to 1.0)
- How much to penalize graph scores for long-distance hops
- `0.3` = 30% penalty for hops beyond max_reasonable_hop_distance
- Default: `0.3`

## Zero-Hop Bonus

**`graph_zero_hop_bonus`** (0.0 to 1.0)
- Bonus for repeaters heard directly by the bot (zero-hop adverts)
- Strong evidence the repeater is close, even for intermediate hops
- Based on actual observed direct communication, not proximity guessing
- Default: `0.4`

## Final Hop Proximity (Advanced)

The final hop (last repeater before bot) gets special proximity consideration. These settings are advanced and typically don't need adjustment.

**`graph_final_hop_proximity_enabled`** (boolean)
- Enable bot location proximity consideration for final hop
- Default: `true`

**`graph_final_hop_proximity_weight`** (0.0 to 1.0)
- Base weight for proximity when combining with graph score for final hop
- `0.25` = 25% proximity, 75% graph score
- Default: `0.25`

**`graph_final_hop_max_distance`** (kilometers, 0 = no limit)
- Maximum distance for final hop proximity consideration
- Repeaters beyond this distance don't receive proximity bonus
- Default: `0` (no limit)

**`graph_final_hop_proximity_normalization_km`** (kilometers)
- Distance normalization for final hop proximity scoring
- Lower values = more aggressive scoring
- Default: `200` (long LoRa range)

**`graph_final_hop_very_close_threshold_km`** (kilometers)
- Repeaters within this distance get 2x proximity weight boost
- Default: `10` km

**`graph_final_hop_close_threshold_km`** (kilometers)
- Repeaters within this distance get 1.5x proximity weight boost
- Default: `30` km (typical transmission range)

**`graph_final_hop_max_proximity_weight`** (0.0 to 1.0)
- Maximum proximity weight for very close repeaters
- Default: `0.6`

## Path Validation Bonus

**`graph_path_validation_max_bonus`** (0.0 to 1.0)
- Maximum bonus for path validation matches
- Helps resolve prefix collisions by matching stored path patterns
- Default: `0.3`

**`graph_path_validation_obs_divisor`** (float)
- Divisor for observation count bonus
- Lower values = stronger bonus from observation count
- `50.0` means 50 observations = 0.15 bonus
- Default: `50.0`

## Graph Persistence (Advanced)

These settings control how graph edges are stored in the database.

**`graph_write_strategy`** (`immediate` | `batched` | `hybrid`)
- `immediate`: Write each edge update immediately (safer, higher I/O)
- `batched`: Accumulate updates, flush periodically (better performance)
- `hybrid`: Immediate for new edges, batched for increments (balanced)
- Default: `batched` (recommended for SD-card installations)

**`graph_batch_interval_seconds`** (seconds)
- How often to flush pending edge updates (only for batched/hybrid)
- Default: `30`

**`graph_batch_max_pending`** (integer)
- Maximum pending updates before forcing a flush
- Default: `100`

**`graph_startup_load_days`** (days, 0 = load all)
- Load only edges seen in last N days on startup
- `0` = load all edges (use on servers with ample RAM)
- Default: `14` (set to `0` in `config.ini` to load all)

**`graph_capture_enabled`** (boolean)
- When `false`, no new edge data is collected from packets and the background
  batch writer thread is not started — reducing CPU and RAM overhead
- Edges already in the database are still used for path validation
- Set to `false` on devices that don't use the path command
- Default: `true`

## Raspberry Pi / Low-Power Profile

For a Raspberry Pi 4 using an SD card, start with:

```ini
[Path_Command]
graph_based_validation = true
graph_capture_enabled = true
min_edge_observations = 5
graph_edge_expiration_days = 7
graph_startup_load_days = 7
graph_write_strategy = batched
graph_batch_interval_seconds = 60
graph_batch_max_pending = 250
graph_use_bidirectional = true
graph_use_hop_position = true
graph_multi_hop_enabled = true
graph_multi_hop_max_hops = 2
graph_prefer_stored_keys = true

[Data_Retention]
packet_stream_retention_days = 3
observed_paths_retention_days = 30
mesh_connections_retention_days = 7
daily_stats_retention_days = 90
purging_log_retention_days = 90
retention_delete_batch_size = 1000
retention_delete_pause_seconds = 0.1
```

`batched` persistence groups dirty edges into one transaction, reducing WAL
and SD-card transaction churn. A 60-second interval can lose at most roughly
one minute of not-yet-flushed graph updates after abrupt power loss. Use
`hybrid` if immediate persistence of newly discovered edges matters more than
write volume.

`observed_paths_retention_days` is the main bound on multi-byte graph
derivation work because lifetime edge identity and counts are derived from all
retained path evidence before the selected browser timeframe is applied.
Thirty days is a practical Pi baseline; use 14 days for very busy meshes or 90
days when historical fidelity matters more than database size and query cost.

Retention deletes are committed in chunks and pause briefly between batches.
This prevents a large first cleanup from holding SQLite's only writer lock for
minutes. Reduce `retention_delete_batch_size` or increase
`retention_delete_pause_seconds` if maintenance still causes visible I/O-wait
spikes; doing so lengthens the cleanup.

For the web graph, use **Multi-byte Only**, a **72-hour edge window**, a
**7-day node window**, and a minimum observation threshold around **5**. These
settings reduce response and rendering work; retained-history length controls
the underlying multi-byte aggregation cost.

The installed systemd service allows up to 1GB of memory and 200% CPU (two
cores). These are upper limits rather than reserved resources. A USB SSD is
still the most effective way to reduce SD-card wear on high-volume nodes.

The web viewer caches the lifetime multi-byte edge aggregate for 30 seconds and
coalesces live mesh events into one refresh per 30 seconds. On unusually large
meshes, increase `[Web_Viewer] mesh_graph_cache_seconds` to 60. Keep
`[Logging] log_level = INFO` during normal operation; `DEBUG` emits multiple
records per observed edge and can create substantial SD-card write traffic.

## Preset Configurations

### `balanced` (Default)
- Uses both graph evidence and geographic proximity
- Graph confidence threshold: 0.7
- Distance penalties: enabled (30km threshold)
- Final hop proximity: enabled
- Good for: Most networks with mixed connectivity

### `geographic`
- Prioritizes geographic proximity
- Graph confidence threshold: 0.5 (lower, gives geographic more weight)
- Distance penalties: enabled (30km threshold, stronger penalty)
- Final hop proximity: enabled with higher weight
- Good for: Local networks where repeaters are close together

### `graph`
- Prioritizes graph evidence
- Graph confidence threshold: 0.9 (higher, graph wins more often)
- Distance penalties: enabled (50km threshold, weaker penalty)
- Final hop proximity: enabled with lower weight
- Good for: Well-connected networks with strong graph evidence

## Geographic scoring toggle

**`geographic_scoring_enabled`** in `[Path_Command]` (default `true`):

- When **`true`**, geographic proximity scoring is used during path decode (subject to other preset and graph settings).
- When **`false`**, geographic proximity guessing is disabled entirely for path decode.

This is a **configuration** option only — there is no chat subcommand to toggle it at runtime. Restart the bot (or reload config if supported) after changing it.

See also the [`path` command](command-reference.md#path-or-decode-or-route) in the command reference.

## Typical LoRa Transmission Ranges

- **Typical transmission**: < 30km
- **Long transmission**: up to 200km
- **Very close**: < 10km (often direct line-of-sight)

These ranges inform the default distance thresholds used throughout the path selection algorithm.
