# Upgrade Guide

This document describes changes that may affect users upgrading from previous versions.

## Upgrading from v0.7

### Config Compatibility

Previous config files continue to work. The following legacy config formats are supported:

- **`[Jokes]`** with `joke_enabled` / `dadjoke_enabled` — Migrated to `[Joke_Command]` and `[DadJoke_Command]` with `enabled`. Both formats work; consider updating to the new format.
- **`[Stats]` / `stats_enabled`**, **`[Sports]` / `sports_enabled`**, **`[Hacker]` / `hacker_enabled`**, **`[Alert_Command]` / `alert_enabled`** — All support the legacy `*_enabled` key; the new `enabled` key is preferred.

### Banned Users: Prefix Matching

`[Banned_Users]` uses **prefix (starts-with) matching** for `banned_users` entries. A banned entry `"Awful Username"` matches both `"Awful Username"` and `"Awful Username 🍆"`. If you rely on exact matching, ensure your banned entries are specific enough.

### New Optional Sections

- **`[Feed_Manager]`** — If you use RSS/API feeds, add this section. If absent, the feed manager is disabled. New installs and minimal configs include `[Feed_Manager]` with `feed_manager_enabled = false`.
- **`[Path_Command]`** — New options like `path_selection_preset`, `enable_p_shortcut` (default: true), and graph-related settings. Omitted options use sensible defaults.
