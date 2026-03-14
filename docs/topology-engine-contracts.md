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
