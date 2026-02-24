# Earthquake Service

Polls the USGS Earthquake API and posts alerts to a channel when earthquakes occur in a configured region. Defaults to California (M3.0+, past 10 minutes). No API key required.

---

## Quick Start

1. **Configure Bot** - Edit `config.ini`:

```ini
[Earthquake_Service]
enabled = true
channel = #general

# Optional: adjust region or magnitude (defaults are California, M3.0+)
# minlatitude = 32.5
# maxlatitude = 42.0
# minlongitude = -124.5
# maxlongitude = -114.0
# min_magnitude = 3.0
# time_window_minutes = 10
# poll_interval = 60000
```

2. **Restart Bot** - The service will start polling USGS and post to the channel when quakes are detected.

---

## Configuration

All options live under `[Earthquake_Service]`. See `config.ini.example` for the full list and comments.

| Option | Description | Default |
|--------|-------------|---------|
| `enabled` | Turn the service on or off | `false` |
| `channel` | Mesh channel for earthquake alerts | `#general` |
| `poll_interval` | How often to check USGS (milliseconds) | `60000` (1 min) |
| `time_window_minutes` | Only consider quakes in the last N minutes | `10` |
| `min_magnitude` | Minimum magnitude to report | `3.0` |
| `minlatitude`, `maxlatitude` | Latitude bounds (decimal degrees) | 32.5, 42.0 (California) |
| `minlongitude`, `maxlongitude` | Longitude bounds (decimal degrees) | -124.5, -114.0 (California) |
| `send_link` | Send USGS event link in a separate message after the alert | `true` |

---

## Features

- **Polling**: Runs in the background and checks USGS at `poll_interval`. Uses the same [USGS FDSNWS Event API](https://earthquake.usgs.gov/fdsnws/event/1/) as the standalone California earthquake script.
- **Region**: Only earthquakes inside the configured bounding box (lat/lon) are reported. Defaults match California.
- **Magnitude filter**: Only events with magnitude ≥ `min_magnitude` are sent.
- **Deduplication**: Each event is sent once. In-memory seen event IDs avoid duplicates within a run. The last posted event time is stored in the `bot_metadata` table (`earthquake_last_posted_time`) so after a restart the bot skips events that were already posted.

**Example alert (when `send_link = true`, two messages):**
```
Earthquake M3.2 mb | 12km NW of Borrego Springs, CA | 14:32:15 UTC | depth 12 km | 33.28N 116.42W
https://earthquake.usgs.gov/earthquakes/eventpage/ci40623456
```
When `send_link = false`, only the first line is sent and no link is posted.
