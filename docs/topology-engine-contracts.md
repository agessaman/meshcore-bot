# Topology Engine Compatibility Contracts

This document locks the data/API contracts that the topology engine rewrite must
preserve while `topology_engine_mode=legacy|shadow`.

## Storage Contracts (must remain backward-compatible)

- `mesh_connections`
  - Required columns: `from_prefix`, `to_prefix`, `from_public_key`, `to_public_key`,
    `observation_count`, `first_seen`, `last_seen`, `avg_hop_position`, `geographic_distance`
- `observed_paths`
  - Required columns: `public_key`, `packet_hash`, `from_prefix`, `to_prefix`, `path_hex`,
    `path_length`, `bytes_per_hop`, `packet_type`, `first_seen`, `last_seen`, `observation_count`

## API Contracts (unchanged in shadow mode)

- `GET /api/mesh/nodes`
- `GET /api/mesh/edges`
- `GET /api/mesh/stats`
- `POST /api/mesh/resolve-path`

No schema changes are allowed on these endpoints during shadow rollout.
Additive debug endpoints are allowed.

## Command Contracts

- `path` command output format remains unchanged in `legacy` and `shadow`.
- Legacy graph/geographic resolver remains authoritative in `shadow`.
- `new` mode may change internal selection method, but must preserve response schema
  (`found`, `collision`, `confidence`, `name`, `public_key`, etc.).

## Supplemental Tables (additive only)

The rewrite may use additive tables:
- `topology_inference_shadow`
- `topology_ghost_nodes`
- `topology_model_metrics`

These tables must not be required for legacy functionality.

## Advert-Anchor Evaluation Runbook (shadow/backfill)

Use this process to evaluate `topology_advert_anchor_*` soft-prior settings without
changing live `new`-mode behavior.

1. Baseline (feature off)
   - Set `topology_advert_anchor_enabled = false`
   - Run topology backfill for a fixed window (for example 14 or 30 days)
   - Save:
     - `/api/mesh/topology-metrics?days=<window>`
     - `/api/mesh/topology-shadow?days=<window>` (includes `anchor_diagnostics`)

2. Candidate run (feature on)
   - Set `topology_advert_anchor_enabled = true`
   - Keep same `days`, `limit`, and sampling settings
   - Run the same backfill window again
   - Save the same two payloads

3. Compare pass/fail criteria
   - Required:
     - `anchor_diagnostics.anchored_ghost_rate` decreases
     - No material drop in non-collision agreement
   - Guardrails:
     - `anchor_diagnostics.average_anchor_adjustment` remains near zero/small
       (no runaway bias)
     - Confidence distributions remain stable (no broad collapse or saturation)

4. Suggested defaults for first rollout
   - `topology_advert_anchor_weight = 0.2`
   - `topology_advert_anchor_max_adjustment = 0.08`
   - `topology_advert_anchor_freshness_hours = 168`

5. Promotion guidance
   - If anchored ghost rate improves and agreement is stable, keep settings for
     continued shadow observation.
   - Only consider new-mode integration after repeated windows show stable gains.
