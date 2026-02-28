# Data retention

The bot stores data in a SQLite database for the web viewer, stats, repeater management, and path routing. To limit database size, **data retention** controls how long rows are kept. Cleanup runs **daily** from the bot’s scheduler, so retention is enforced even when the standalone web viewer is not running.

## Configuration

All retention options live in the **`[Data_Retention]`** section of `config.ini`. Example (see `config.ini.example` for full comments):

```ini
[Data_Retention]
packet_stream_retention_days = 3
daily_stats_retention_days = 90
observed_paths_retention_days = 90
purging_log_retention_days = 90
mesh_connections_retention_days = 7
```

Stats tables (message_stats, command_stats, path_stats) use **`[Stats_Command]`** `data_retention_days` (default 7); the scheduler runs that cleanup daily as well.

## Tables and defaults

| Table / data | Purpose | Default retention |
|--------------|---------|--------------------|
| **packet_stream** | Real-time packets, commands, routing in the web viewer; transmission_tracker repeat counts | 3 days |
| **daily_stats** | Daily repeater/advert stats | 90 days |
| **unique_advert_packets** | Unique packet hashes for advert stats | 90 days (same as daily_stats) |
| **observed_paths** | Path strings from adverts and messages | 90 days |
| **purging_log** | Audit trail for repeater purges | 90 days |
| **mesh_connections** | Path graph edges (in-memory + DB); should be ≥ Path_Command `graph_edge_expiration_days` | 7 days |
| **message_stats, command_stats, path_stats** | Stats command data | 7 days (`[Stats_Command]` `data_retention_days`) |
| **geocoding_cache, generic_cache** | Expired entries removed by scheduler | By expiry time |

Shorter retention (e.g. 2–3 days for `packet_stream`) is enough for the web viewer and transmission_tracker; longer retention is only needed if you want more history.

## How cleanup runs

1. The **scheduler** (in the main bot process) runs a single data-retention task once every 24 hours.
2. That task:
   - Cleans **packet_stream** (via web viewer integration when enabled).
   - Cleans **purging_log**, **daily_stats**, **unique_advert_packets**, and **observed_paths** (repeater manager).
   - Cleans **message_stats**, **command_stats**, **path_stats** (stats command’s `cleanup_old_stats`).
   - Removes expired rows from **geocoding_cache** and **generic_cache** (DB manager).
   - Deletes old rows from **mesh_connections** (mesh graph).

So as long as the bot is running, the database is pruned on a schedule regardless of whether you run the standalone web viewer or the stats command.
