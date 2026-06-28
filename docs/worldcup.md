# World Cup

The bot has two World Cup features that work together, both driven by ESPN data and active **only while a FIFA World Cup (men's or women's) is actually in progress**. The active tournament is auto-detected from the ESPN schedule, so no dates need to be configured — outside a tournament both features idle.

- **`wc` / `worldcup` command** — on-demand scores and fixtures (see the [Command Reference](command-reference.md#wc-or-worldcup)).
- **World Cup Live Service** — proactive match announcements posted to a channel.

World Cup scores are also available year-round through the regular [`sports` command](command-reference.md#sports), e.g. `sports fifa`.

## Command (`[Worldcup_Command]`)

```ini
[Worldcup_Command]
# Enable or disable the World Cup command (true/false). Commands: wc / worldcup
enabled = true

# ESPN API timeout in seconds
api_timeout = 10

# How long (minutes) to cache season detection and nation roster lookups,
# to avoid repeated ESPN requests. Default: 360 (6 hours).
cache_ttl_minutes = 360
```

## Live Service (`[Worldcup_Service]`)

Posts proactive messages to a channel as matches progress, for example:

```
Group E: Côte d'Ivoire 0, Ecuador 0 (half-time)
Group J: Argentina 1, Algeria 0 — 23' Lionel Messi
```

The service only runs while a tournament is in progress and idles otherwise. It pairs with the `wc`/`worldcup` command. Enable it in `[Worldcup_Service]`:

```ini
[Worldcup_Service]
enabled = false

# Optional regional TC_FLOOD scope for mesh channel posts from this service.
# flood_scope = #west

# Channel to post live updates to
channel = #general

# Poll interval (ms) while a tournament is active but no match is in progress. Default: 60000
poll_interval = 60000

# Faster poll interval (ms) used while at least one match is LIVE. ESPN's scoreboard is
# edge-cached ~15-20s, so polling faster yields no fresher data. Default: 20000
live_poll_interval = 20000

# Idle interval (seconds) used when no tournament is in progress. Default: 1800 (30 min)
idle_interval = 1800

# Which match events to announce (each true/false)
announce_kickoff = true
announce_goals = true
# Post a follow-up when a previously-announced goal is overturned by VAR. Requires announce_goals.
announce_disallowed = true
# Announce red cards
announce_red_cards = true
# Announce yellow cards too. OFF by default — yellows are frequent and can flood a mesh channel.
announce_yellow_cards = false
```

See `config.ini.example` for the full annotated list of options.
