# Path Command Configuration Guide

This document explains all configuration parameters for the Path Command, which decodes hex path data to identify repeaters in message routing paths.

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
- Default: `hybrid`

**`graph_batch_interval_seconds`** (seconds)
- How often to flush pending edge updates (only for batched/hybrid)
- Default: `30`

**`graph_batch_max_pending`** (integer)
- Maximum pending updates before forcing a flush
- Default: `100`

**`graph_startup_load_days`** (days, 0 = load all)
- Load only edges seen in last N days on startup
- `0` = load all edges (recommended for development)
- Default: `0`

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

## Typical LoRa Transmission Ranges

- **Typical transmission**: < 30km
- **Long transmission**: up to 200km
- **Very close**: < 10km (often direct line-of-sight)

These ranges inform the default distance thresholds used throughout the path selection algorithm.
