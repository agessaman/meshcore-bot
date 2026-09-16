# Alert Services

The `alert` command is a thin interface over one or more **alert services**.
An alert service is a plugin that knows how to talk to a single external
incident source — PulsePoint, USGS, a weather-alert feed, a local CAD export —
and turn it into short, LoRa-friendly incident lines.

This document explains the architecture and walks through building a new alert
service from scratch.

- [Overview](#overview)
- [Using the alert command](#using-the-alert-command)
- [Architecture](#architecture)
- [Writing a new alert service](#writing-a-new-alert-service)
- [Query parameter mapping](#query-parameter-mapping)
- [Configuration reference](#configuration-reference)
- [Polling mode](#polling-mode)
- [Troubleshooting](#troubleshooting)

---

## Overview

An alert service supports two independent modes:

- **Query mode** — a user runs the `alert` command (e.g. `alert seattle`). The
  command asks every *connected* service for matching incidents and stitches
  the answers together into one or more messages.
- **Polling mode** — the service periodically fetches *new* incidents on its
  own and posts them to one or more channels. This runs in the background and
  does not involve the `alert` command.

A service can implement one or both modes. PulsePoint (the bundled reference
implementation) does both.

The important pieces:

| Piece | File | Role |
|-------|------|------|
| Alert command | `modules/commands/alert_command.py` | User interface / orchestrator |
| Base alert service | `modules/service_plugins/base_alert_service.py` | Abstract base class |
| PulsePoint service | `modules/service_plugins/pulsepoint_alert_service.py` | Reference implementation |

---

## Using the alert command

```
alert seattle          # incidents in/near Seattle
alert 98101            # incidents near a US ZIP code
alert 178th seattle    # incidents on a street in a city
alert king             # incidents in a configured county
alert 47.6,-122.3      # incidents near coordinates
```

The command queries every service listed in `[Alert_Command] services` and
merges the results. Each service's output is prefixed with its short label:

```
[PP]:
Medical: 123 Main St, Seattle (5m ago) [E1🚗]
Structure Fire: 456 Oak Ave, Seattle (12m ago) [E2📍]
(3 more)
```

When several services respond, the incident budget
(`[Alert_Command] max_incidents_total`, default 10) is split as evenly as
possible between them. If one service has fewer matches than its share, the
unused budget is handed to the others so the total shown is maximized.

If the command is run **in a channel that a service polls**, only that service
answers there — so an `#alerts` channel that a PulsePoint service posts to will
only ever show PulsePoint results, regardless of what else is connected.

---

## Architecture

```
                 alert command  (modules/commands/alert_command.py)
                        │
          ┌─────────────┼──────────────┐         query mode
          ▼             ▼              ▼
   PulsePointService  NoaaService   LocalFeedService     ← BaseAlertService
          │             │              │                   subclasses
          ▼             ▼              ▼
     PulsePoint API   NOAA API     local file / API

   (polling mode: each service posts to its own channels directly,
    bypassing the alert command entirely)
```

### Responsibilities

**Alert command** (`AlertCommand`):
- Discovers connected services from `[Alert_Command] services`.
- Chooses which services answer a message (all connected, or the single
  service that polls the message's channel).
- Distributes the incident budget across responding services.
- Formats messages with each service's `[LABEL]:` header and sends them.

The command contains **no** API logic, query parsing, or geocoding.

**Alert service** (a `BaseAlertService` subclass):
- Parses a raw query string into a query type (city / zip / coordinates / ...).
- Talks to its data source and returns formatted incident lines.
- Optionally polls for new incidents and posts them to channels.
- Owns all of its own configuration (agencies, regions, API keys, ...).

### How services are loaded

Alert services are ordinary service plugins. They are discovered and loaded at
bot startup by `ServicePluginLoader`, exactly like the earthquake or weather
services:

- **Bundled** services live in `modules/service_plugins/`.
- **Local / custom** services live in `local/service_plugins/` (see
  [local-plugins.md](local-plugins.md)).

A service is only loaded if its config section has `enabled = true`.

> **Note:** commands are constructed *before* services are loaded, so the
> alert command resolves its services lazily at execute time (via
> `bot.services`). You do not need to do anything special for this to work.

---

## Writing a new alert service

A new alert service subclasses `BaseAlertService` and implements a small
interface. This section builds a complete minimal service step by step.

### 1. Create the file

Bundled services go in `modules/service_plugins/`; custom services go in
`local/service_plugins/`. Name the file `<something>_alert_service.py`.

### 2. Set the required class attributes

```python
from modules.service_plugins.base_alert_service import (
    BaseAlertService,
    QUERY_TYPE_CITY,
    QUERY_TYPE_ZIPCODE,
)


class MyAlertService(BaseAlertService):
    # Short id used in [Alert_Command] services = ... and for lookup. REQUIRED.
    alert_service_id = "myalerts"

    # config.ini section holding this service's settings. REQUIRED.
    config_section = "MyAlerts_Alert_Service"

    # Human-readable description (shown in service metadata).
    description = "Example alert service"
```

> The class name must end in `Service` (the loader uses this) and be the only
> `BaseAlertService` subclass in the file.

### 3. Declare capabilities

Tell the framework which query types your service understands. The alert
command uses this for documentation and future routing; your `query_alerts`
should be prepared to receive any type it advertises.

```python
    def get_capabilities(self) -> dict[str, bool]:
        return {
            QUERY_TYPE_CITY: True,
            QUERY_TYPE_ZIPCODE: True,
        }
```

Available query-type constants (from `base_alert_service`):

| Constant | Meaning | Example query |
|----------|---------|---------------|
| `QUERY_TYPE_COORDINATES` | latitude/longitude | `47.6,-122.3` |
| `QUERY_TYPE_ZIPCODE` | 5-digit US ZIP | `98101` |
| `QUERY_TYPE_CITY` | city / place name | `seattle` |
| `QUERY_TYPE_COUNTY` | county / region name | `king` |
| `QUERY_TYPE_STREET_CITY` | street + city | `178th seattle` |
| `QUERY_TYPE_NATIVE` | a service-specific identifier | `agency:1234` |

### 4. Parse queries

Classify the raw text after the command keyword. Return
`(query_type, location, lat, lon)`.

```python
    def parse_query(self, query):
        q = query.strip()
        if q.isdigit() and len(q) == 5:
            return (QUERY_TYPE_ZIPCODE, q, None, None)
        return (QUERY_TYPE_CITY, q, None, None)
```

### 5. Implement query mode

`query_alerts` is the entry point the `alert` command calls. Return a list of
**formatted incident strings**, each no longer than ~130 characters and
**without** the `[LABEL]:` prefix (the command adds that). Return `[]` when
there are no matches or the query type isn't something you handle.

Keep blocking I/O (HTTP, geocoding) off the event loop with
`asyncio.to_thread`.

```python
    async def query_alerts(self, query):
        query_type, location, lat, lon = self.parse_query(query)

        # Fetch over the network without blocking the event loop.
        raw = await asyncio.to_thread(self._fetch, location)

        return [self._format_incident(inc) for inc in raw]
```

Each incident line should be self-contained and compact, e.g.
`"Fire: 123 Main St, Seattle (5m ago)"`.

### 6. (Optional) implement polling mode

To support polling, implement `fetch_new_incidents`, which returns
`(incident_id, line)` pairs for every currently-active incident. The base
class handles deduplication (so you return everything active; already-posted
ids are filtered out), channel posting, and persistence.

```python
    async def fetch_new_incidents(self):
        raw = await asyncio.to_thread(self._fetch, None)
        return [(str(inc["id"]), self._format_incident(inc)) for inc in raw]
```

`incident_id` must be **stable** across polls for a given incident. The base
class tracks which ids it has already posted (in memory and persisted to the
`bot_metadata` table) so an incident is announced exactly once, even across a
restart.

If you do not override `fetch_new_incidents`, the service is query-only.

### 7. Register it in config

```ini
[Alert_Command]
enabled = true
services = pulsepoint, myalerts

[MyAlerts_Alert_Service]
enabled = true
label = MYA
```

Restart the bot. The service is now queried by `alert`, and if you set
`polling_enabled = true` with `polling_channels`, it will also post
automatically.

### Complete skeleton

```python
#!/usr/bin/env python3
"""Example alert service."""

import asyncio

from modules.service_plugins.base_alert_service import (
    BaseAlertService,
    QUERY_TYPE_CITY,
    QUERY_TYPE_ZIPCODE,
)


class MyAlertService(BaseAlertService):
    alert_service_id = "myalerts"
    config_section = "MyAlerts_Alert_Service"
    description = "Example alert service"

    def get_capabilities(self):
        return {QUERY_TYPE_CITY: True, QUERY_TYPE_ZIPCODE: True}

    def parse_query(self, query):
        q = query.strip()
        if q.isdigit() and len(q) == 5:
            return (QUERY_TYPE_ZIPCODE, q, None, None)
        return (QUERY_TYPE_CITY, q, None, None)

    async def query_alerts(self, query):
        _type, location, _lat, _lon = self.parse_query(query)
        raw = await asyncio.to_thread(self._fetch, location)
        return [self._format(inc) for inc in raw]

    async def fetch_new_incidents(self):
        raw = await asyncio.to_thread(self._fetch, None)
        return [(str(i["id"]), self._format(i)) for i in raw]

    # --- your API-specific helpers ---
    def _fetch(self, location):
        # ... call your API here (runs in a worker thread) ...
        return []

    def _format(self, inc):
        return f"{inc['type']}: {inc['address']}"
```

---

## Query parameter mapping

Different data sources accept very different search parameters: PulsePoint uses
opaque *agency IDs*, USGS uses a lat/lon bounding box, a weather feed might use
a state or zone code. The design goal is that **users never see any of this** —
they type a familiar location (`seattle`, `98101`, `king`) and each service
maps that to whatever its API needs.

The framework uses a **capability + self-parsing** model:

1. The user query string is passed to each connected service **verbatim**.
2. Each service parses it with its own `parse_query` and decides whether it can
   answer. A service that can't handle a query returns `[]`.
3. Each service maps the parsed location to its native parameters internally.

This keeps the command source-agnostic and lets each service support exactly
the query types that make sense for it.

### Recommendations for mapping location → native parameters

These are the patterns the PulsePoint service uses and that new services are
encouraged to follow:

- **Config-driven named regions.** Let operators map a friendly name to native
  identifiers in the service's config section. PulsePoint does this with
  `agency.city.<name>` / `agency.county.<name>` keys. A weather service might
  use `zone.<name> = WAZ001`. This is the most reliable mapping because a human
  curated it.

- **Geocode, then translate — never expose lat/lon to users.** If your API
  needs coordinates, geocode a city/zip using the shared helpers
  (`modules.utils.geocode_city_sync`, `geocode_zipcode_sync`) and convert the
  result to whatever your API wants (a bounding box, a radius, a nearest
  station). Users should always type a place name, never coordinates — the
  bounding-box math stays inside the service.

- **Fall back gracefully.** If a named region isn't configured, fall back to
  geocoding or to a default region rather than erroring. PulsePoint falls back
  from a city-specific agency to "all configured agencies".

- **Return `[]` for unsupported queries.** If a service only supports ZIP codes
  and the user typed a street address, return an empty list. The command simply
  omits that service from the results; other services can still answer.

### Why not a shared/normalized query format?

An earlier option was to normalize every query to `(lat, lon, radius)` in the
command and hand that to services. This was rejected because:

- It forces coordinate math into the user interface, which the project
  explicitly wants to avoid.
- It erases source-specific concepts (PulsePoint agencies, weather zones) that
  produce far better results than a radius search.

Letting each service parse and map the query itself is more flexible and keeps
source-specific knowledge where it belongs.

---

## Configuration reference

### `[Alert_Command]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Enable the alert command. (`alert_enabled` is accepted as a legacy alias.) |
| `services` | list | `pulsepoint` | Comma-separated alert service ids to query. |
| `max_incidents_total` | int | `10` | Total incidents shown across all services per query. |

### Common alert-service keys

Every alert service inherits these keys (read from its own `config_section`):

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Load and enable the service. |
| `label` | str | service id (upper) | Short label (≤6 chars) prefixed to messages as `[LABEL]:`. |
| `polling_enabled` | bool | `false` | Enable background polling. |
| `polling_interval` | int (ms) | `300000` | Poll frequency; clamped to a 30s minimum. |
| `polling_channels` | list | (empty) | Channels to auto-post new incidents to. |
| `flood_scope` | str | (inherit) | Optional regional TC_FLOOD scope for posts. |
| `discord_webhook_urls` | list | (empty) | Optional external notification targets. |
| `telegram_chat_ids` | list | (empty) | Optional external notification targets. |

Individual services add their own keys (e.g. PulsePoint's `max_distance_km`,
`max_incident_age_hours`, and `agency.*` mappings).

### `[PulsePoint_Alert_Service]` (reference)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_distance_km` | float | `20.0` | Only report incidents within this distance of the location. |
| `max_incident_age_hours` | float | `24.0` | Ignore incidents older than this. |
| `agency.city.<name>` | list | — | PulsePoint agency IDs for a city. |
| `agency.county.<name>` | list | — | PulsePoint agency IDs for a county. |

Find agency IDs at <https://web.pulsepoint.org/>. Use underscores for
multi-word names (e.g. `agency.city.federal_way`).

---

## Polling mode

When `polling_enabled = true` and `polling_channels` is non-empty, the base
class starts a background loop on service start:

1. Every `polling_interval` ms it calls your `fetch_new_incidents`.
2. It filters out incident ids already posted (in memory + persisted).
3. It prefixes each new incident with `[LABEL]:` and posts it to every channel
   in `polling_channels`, respecting `flood_scope` if set.
4. It persists the seen ids to the `bot_metadata` table so a restart does not
   repost incidents that were already announced.

Deduplication is keyed on the `incident_id` you return, so make it stable.

Polling and query mode are independent: a service can poll `#alerts` while
still answering `alert seattle` in a DM. If a user runs `alert` **in** a polled
channel, only that service answers there.

---

## Troubleshooting

**The `alert` command says "No alert services configured".**
Check that `[Alert_Command] services` lists your service id (its
`alert_service_id`) and that the service's own section has `enabled = true`.
Services with `enabled = false` are not loaded.

**A service isn't being queried.**
The id in `services` must match the service's `alert_service_id` exactly, not
its class name or config section. Check the startup log for
`Loaded N service(s): [...]`.

**Polling isn't posting anything.**
Both `polling_enabled = true` **and** a non-empty `polling_channels` are
required. The log warns if polling is enabled without channels. Also confirm
`fetch_new_incidents` returns stable ids — if the id changes every poll, the
dedup filter can't recognize repeats (or, conversely, a never-changing id means
an incident is only ever posted once).

**Incidents reappear after a restart.**
Persistence uses the `bot_metadata` table via `bot.db_manager`. If the bot has
no database manager, dedup is in-memory only and won't survive a restart.

**Messages are truncated oddly.**
Each incident line must fit within the LoRa payload budget (~130 chars) on its
own, since the command packs whole lines into messages. Keep individual lines
short.

