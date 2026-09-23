# MeshCore Bot Data Viewer

A web-based interface for viewing and analyzing data from your MeshCore Bot.

## Features

- **Dashboard**: Overview of database statistics and bot status
- **Repeater Contacts**: View active repeater contacts with location and status information
- **Contact Tracking**: Complete history of all heard contacts with signal strength and routing data
- **Config panel**: Structured settings with categorized topics and database tools
- **Plugins page**: Toggle every command and service on/off and edit their settings
  from the browser. Changes are validated, written to `config.ini` with comments
  preserved and a timestamped backup, and the bot hot-reloads command settings
  within a few seconds (service on/off still needs a restart)
- **Purging Log**: Audit trail of contact purging operations
- **Real-time Updates**: Auto-refreshes every 30 seconds
- **API Endpoints**: JSON API for programmatic access

## Quick Start

### Option 1: Standalone Mode
```bash
# Install Flask if not already installed
pip3 install flask

# Start the web viewer (reads config from config.ini)
python3 -m modules.web_viewer.app

# Or use the restart script for standalone mode
./restart_viewer.sh

# Override configuration with command line arguments
python3 -m modules.web_viewer.app --port 8080 --host 0.0.0.0
```

### Option 2: Integrated with Bot
1. Edit `config.ini` and set:
   ```ini
   [Web_Viewer]
   enabled = true
   auto_start = true
   host = 127.0.0.1
   port = 5000
   ```

2. The web viewer will start automatically with the bot

## Configuration

The web viewer can be configured in the `[Web_Viewer]` section of `config.ini`:

```ini
[Web_Viewer]
# Enable or disable the web data viewer
enabled = true

# Web viewer host address
# 127.0.0.1: Only accessible from localhost
# 0.0.0.0: Accessible from any network interface
host = 127.0.0.1

# Web viewer port
port = 5000

# Enable debug mode for the web viewer
debug = false

# Auto-start web viewer with bot
auto_start = false

# Optional: enable the multibyte monitor page and API
multibyte_monitor_enabled = false
```

## Accessing the Viewer

Once started, open your web browser and navigate to:
- **Local access**: http://localhost:5005 (or your configured port)
- **Network access**: http://YOUR_BOT_IP:5005 (if host is set to 0.0.0.0)

## Reverse Proxy With Nginx Basic Auth

If you expose the web viewer outside your local network, run it behind HTTPS and authentication. One option is to bind the viewer locally, then put Nginx in front with basic auth:

```ini
[Web_Viewer]
enabled = true
auto_start = true
host = 127.0.0.1
port = 8080
```

Example Nginx server block:

```nginx
server {
  # [...]
  auth_basic           "Login required";
  auth_basic_user_file /etc/nginx/.meshcore-bot.htpasswd;

  location / {
    # Local web viewer instance
    proxy_pass      http://127.0.0.1:8080;
    proxy_buffering off;
    include /etc/nginx/proxy_params;
  }

  # Socket.IO websocket path for live updates
  location /socket.io/ {
    if ($http_connection !~* "upgrade") {
      return 403;
    }
    if ($http_upgrade !~* "websocket") {
      return 403;
    }

    proxy_pass http://127.0.0.1:8080;
    include /etc/nginx/proxy_params;

    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400;
  }
}
```

With the config above, `/etc/nginx/proxy_params` should include the standard forwarded headers:

```nginx
proxy_set_header Host $http_host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

## Pages Overview

### Dashboard
- Health strip: bot status, database size, uptime, connected clients, radio state,
  and snapshot age with a manual refresh control
- **Mesh**: nodes heard, adverts, new nodes, nodes gone quiet, and geographic
  coverage — the count tiles carry a 30-day sparkline and a change chip
- Routing mix (flood vs direct), a hop-distance chart, and the role mix
- Path encoding: multibyte share among contacts and among incoming packets,
  plus a 30-day stacked bar whose height is the share of each day's packets that
  took a multibyte path, split by the payload type carrying them (`GRP_TXT`,
  `RESPONSE`, `REQ`, `PATH`, `TXT_MSG`, `ANON_REQ`, `GRP_DATA`, `ADVERT`, and
  `Other` for the rest). Its y-axis is the tallest bar rounded up to the next
  5%, so it rescales as the mesh changes
- Busiest repeaters, and **one-hop neighbours** (radios heard directly; 24-hour
  or 7-day window)

The live packet feed lives on the **Real-time** page rather than here; the
dashboard reads a single snapshot per poll and holds no streaming
subscriptions.

Two measurement notes for the mesh charts:

**Hops are derived, and the two path tables disagree on units.**
`observed_paths.path_length` is a *byte* count, so hops are
`path_length / bytes_per_hop` — with 2- or 3-byte encoding a three-hop path is
six or nine bytes long, and charting the raw value would overstate distance two-
to threefold on a mesh that is ~95% multibyte. `packet_stream.path_len`, by
contrast, is *already a hop count*, with its byte length carried separately as
`path_byte_length`. Applying either table's rule to the other silently rescales
the axis, so both conventions are pinned by tests.

The hops chart shows both distributions: nodes by their closest advert path (7
days) and arriving flood packets by distance travelled (whatever
`packet_stream` retains, typically 3 days). One counts nodes and the other
packets, so each is drawn as a share of its own total. Flood packets carry no
sender identity, which is why they cannot be reduced to a shortest path per
node the way adverts can.

Flood hop buckets holding under 0.1% of the series are omitted, because that
tail decays over roughly twenty hops in bars thinner than a pixel. The number of
packets withheld is printed beneath the chart, and percentages stay shares of
the full series so that hiding the tail cannot inflate the remaining bars. The
node series is shown in full.

**Neighbour membership is direct RF (MeshCore hop count 0).**
An empty path means this radio heard the originator on the air. A path whose
byte length equals `bytes_per_hop` already contains one hop hash — that
originator is one repeater away, not a neighbour. The dashboard lists empty-path
adverts in `observed_paths` plus in-window rows from `neighbor_links` (zero-hop
node-discover). `complete_contact_tracking.hop_count` is not used for
membership: on a representative database it claims 800 zero-hop contacts while
only a few dozen have any empty-path advert to corroborate it, and the SNR
stored against them clusters in a ~1.5 dB band with RSSI near -45 dBm — one
strong local link recorded against every node whose traffic arrived through it.
SNR/RSSI on the panel come from the zero-hop path row or from discover (SNR
only). A relayed packet's SNR measures the last hop into this radio, never the
link to whoever sent it.
- **Bot**: messages, commands, reply rate, and unique users, plus the top
  commands/users/channels and longest paths
- Live activity feed

The dashboard is served from a **snapshot**, not from live aggregation. A
background thread in the viewer process recomputes it every
`dashboard_snapshot_interval_seconds` (default 60) and writes two tables:

| Table | Contents |
|-------|----------|
| `daily_rollup` | One row per local date. Retains counts whose raw sources are pruned long before the dashboard's 30-day window. Signal metrics are stored as sums and counts, never means, so any window re-aggregates correctly. |
| `dashboard_snapshot` | A single JSON row describing current state. |

Two consequences worth knowing:

- **Gaps are not zeros.** A day with no source data stores NULL and renders as a
  break in the line. Days seeded when the feature was first enabled have real
  advert counts but no message, command, path, or packet figures — those raw
  rows were already pruned and cannot be recovered.
- **Window labels come from retention.** The time-window selectors are built
  from each source's configured retention, so the list cannot offer "30 days"
  against a table pruned at 7.

The **multibyte share** trends are accumulated forward, not derived.

The per-payload-type figures come from `packet_stream`, which is pruned within
days while the chart spans thirty, so each day's split is written into
`daily_rollup.packet_type_encoding` as that day is rolled up and cannot be
recomputed afterwards. Enabling the feature therefore starts an empty chart that
fills in over the following month. Packets whose denormalized dimensions have
not been backfilled yet are excluded from both sides of the ratio rather than
counted as single-byte — counting them would invent a dip in whichever type the
backfill has not reached.

**The chart measures the day, and the API serves per-type adoption.** A bar's
height is the share of that day's packets that went multibyte, and its segments
are each type's multibyte packets over that same day-wide denominator — so the
segments sum to the bar and the bar equals the figure the packet doughnut
reports for its own window. Every payload type is counted, with the uncharted
tail (`ACK`, `TRACE`, unmapped ordinals — about 0.8% of live traffic) summed into
`Other`; omitting it would leave bar heights a share of the charted types rather
than of the day.

The `multibyte_share_*` metrics answer the different question "how much of *this
type* went multibyte?", each a ratio over its own denominator. Eight such ratios
share no denominator and cannot be stacked, which is why the dashboard payload
carries the raw counts in `packet_encoding` rather than the eight percentage
series; the tooltip quotes both readings per segment.

**Two different advert shares exist, and they do not agree.** The charted
`multibyte_share_advert` counts advert *packets* off the packet stream, the same
way as every other line. The older `multibyte_share` counts a day's adverts
against a classification of the *node* that sent them — one multibyte path ever
observed marks that node multibyte for every advert it sends. Neither is wrong;
they answer different questions, and the gap between them is roughly the set of
nodes that can do multibyte but mostly do not.

That older share is also frozen for a different reason. `observed_paths` is
deduplicated with a lifetime `observation_count` and a `last_seen` that is
bumped on every re-observation, so historical per-day shares cannot be
reconstructed from it — nearly half the observation volume would be attributed
to the wrong day. Each refresh recomputes today plus a three-day trailing
window; older days stay frozen at the value recorded then.

### Repeater Contacts
- Active repeater contacts
- Location information (city/coordinates)
- Device types and status
- First/last seen timestamps
- Purge count tracking

### Contact Tracking
- Complete history of all heard contacts
- Signal strength indicators
- Hop count and routing information
- Advertisement data
- Currently tracked status

### Config
- Categorized configuration topics in a left navigation column
- Core settings such as notifications, log rotation, backup, and maintenance status
- Database operations and database information views in the same tab

### Radio
- Radio connect/disconnect and channel management (create, inspect, delete)
- **Radio Parameters**: read/write frequency, bandwidth, spreading factor, coding
  rate, and TX power on the device
- **Node Settings**: read/write companion firmware settings on the device
  - *Response Path Hashing*: the path hash size the firmware uses for each hop
    when building outgoing/response paths (mode 0–2 = 1–3 bytes per hop; larger
    hashes avoid relay collisions but need firmware 1.14+ mesh-wide)
  - *Default Region Scope*: the radio's own default region, stored in firmware
    (`NodePrefs.default_scope_name` / `default_scope_key`) and used for any send
    the bot does not scope itself. Max 30 ASCII characters including the `#`,
    which is added if you omit it; blank clears it and the radio floods
    unscoped. Firmware without the setting is reported as such rather than
    shown as an empty field, and a stored key that is not the stored name's
    hash is flagged, because the radio routes by the key. Note that once the
    bot sends a scoped message it leaves the radio in forced-unscoped mode, so
    this default stops applying until the bot scopes another send — to have the
    bot post under a region, use Region Scopes below
  - *Identity & Adverts*: node name, advertised latitude/longitude, advert
    location policy, and buttons to send a zero-hop or flood advert. The name
    field is locked when the bot manages it (`[Bot] bot_name` with
    `auto_update_device_name` on)
  - *Mesh Behavior*: extra ACK count and telemetry permissions
    (base/location/environment, each deny / per-contact flags / allow all).
    New-contact handling is shown read-only — it is owned by
    `[Bot] auto_manage_contacts` in config.ini, which the bot applies to the
    device itself
  - *Advanced Tuning*: RX delay base and airtime factor (write-only; the device
    does not report current values)
- **Region Scopes**: the bot's own regional flood scopes, edited here rather
  than by hand in `config.ini`. Unlike the cards above it, this writes
  `[Channels]` in `config.ini` and queues a hot config reload — nothing is read
  from or written to the radio. This is a different setting from the radio's
  Default Region Scope above: this one decides what the bot sends and answers,
  that one is the radio's fallback for anything the bot leaves unscoped
  - *Which scopes the bot replies to*: `flood_scopes`. Left empty (the default),
    the bot still answers channel commands, but replies go out as ordinary
    global FLOOD and do not mirror the sender's region. An allowlist of named
    regions enables mirroring for those names (for example `*,#home`); the
    "also reply to unscoped messages" box is the `*` entry that keeps unscoped
    FLOOD allowed. The bot matches inbound transport codes by HMAC against
    those names and cannot recover a region name from the 16-bit code alone
  - *Default outgoing scope*: `outgoing_flood_scope_override`, used for sends
    that carry no scope of their own (scheduled messages, feeds, webhooks) and
    for replies whose incoming scope could not be matched. Blank means global
    flood. A mirrored reply scope still wins over it
  - Per-channel `flood_scope.<channel>` entries are listed read-only, because
    they override the default on their own channel; edit those in `config.ini`
  - Names are normalized the way the bot normalizes them, so `west` is stored
    as `#west`. A name containing `,`, `%` or an inner `#` is refused: the
    first separates the list, the second breaks every later read of the section
    (config.ini is parsed with interpolation on), and the third is the scope
    marker itself
  - The page reports what the bot did with the edit, not just that the file was
    written — it polls the queued reload and says so plainly when nothing
    picked it up
- Device writes are queued through the bot process (`channel_operations` table),
  so the bot must be running and connected to the radio for reads/writes to
  complete

### Purging Log
- Audit trail of contact purging operations
- Timestamps and reasons
- Contact names and public keys

## API Endpoints

The viewer also provides JSON API endpoints:

- `GET /api/region-warnings` - Region-code settings, per-channel scope tallies,
  the daily series, today's warning budget, and recent warning decisions
- `POST /api/region-warnings/settings` - Save `[Region_Warnings]` and queue a
  hot config reload
- `GET /api/region-scopes` - Regional flood scopes as the bot resolves them:
  the `[Channels] flood_scopes` allowlist split into named scopes and the
  global flag, the default outgoing scope, any per-channel overrides, and which
  file a save would write
- `POST /api/radio/firmware/config/write` - Queue a firmware write.
  `path_hash_mode` (0-2) and `default_flood_scope` (a region name, or empty to
  clear the radio's default) may be sent together or alone.
  `GET`-side values come back from `POST /api/radio/firmware/config/read`
- `POST /api/region-scopes` - Save `[Channels] flood_scopes` and
  `outgoing_flood_scope_override`, and queue a hot config reload. Returns the
  reload's `channel_operations` id so the caller can poll
  `/api/channel-operations/<id>` for what the bot actually did
- `GET /api/dashboard/summary` - Snapshot-backed dashboard payload, including
  30-day sparkline series and change figures, plus `packet_encoding`: 30 days of
  raw per-payload-type multibyte/total counts for the stacked encoding chart.
  Sends a strong `ETag`; poll with `If-None-Match` to get a bodyless `304` while
  the snapshot is unchanged.
- `GET /api/dashboard/series?metric=<m>&days=<n>` - Full-history points for one
  metric. `metric` is one of `messages`, `commands`, `adverts`, `nodes`,
  `new_nodes`, `packets`, `multibyte_share` (adverts), or the per-payload-type
  packet shares `multibyte_share_grp_txt`, `multibyte_share_response`,
  `multibyte_share_req`, `multibyte_share_path`, `multibyte_share_txt_msg`,
  `multibyte_share_anon_req`, `multibyte_share_grp_data`,
  `multibyte_share_advert`.
- `GET /api/dashboard/top?kind=<k>&window=<w>&limit=<n>` - One leaderboard.
  `kind` is one of `users`, `commands`, `channels`, `paths`, `repeaters`. The
  response carries `window_label`, `retention_days`, and
  `truncated_by_retention`.
- `GET /api/dashboard/windows` - Selector options derived from each source's
  retention.
- `POST /api/dashboard/refresh` - Force a snapshot recomputation.
- `GET /api/stats` - **Deprecated.** The whole-database statistics payload the
  dashboard used to call five times per page load. Every key name is preserved
  for external consumers, and the response carries `Deprecation` and `Sunset`
  headers. Use the `/api/dashboard/*` endpoints instead; this one is removed at
  the next major version.
- `GET /api/contacts` - Repeater contacts data. The contacts page uses optional
  `page`, `page_size` (maximum 200), `search`, `sort`, and `direction` parameters;
  callers that omit pagination retain the legacy full-list response.
- `GET /api/tracking` - Contact tracking data

Example usage:
```bash
curl http://localhost:5000/api/dashboard/summary
```

## Database Requirements

The viewer uses the same database as the bot by default (`[Bot] db_path`, typically `meshcore_bot.db`). That single file holds repeater contacts, mesh graph, packet stream, and other data so the viewer can show everything.

**Dashboard stats** (message/command counts, top users, etc.) come from the stats tables (`message_stats`, `command_stats`, `path_stats`). Stats collection is enabled by default with `[Stats_Command] collect_stats = true`, even if the user-facing `stats` chat command is disabled with `enabled = false`. Set `collect_stats = false` only if you want to stop writing those dashboard stats tables.

## Migrating from a separate web viewer database

If you previously had the web viewer using a **separate** database (e.g. `[Web_Viewer] db_path = bot_data.db`), you can switch to the shared database so the viewer shows repeater/graph data and uses one file.

1. **Stop the bot and web viewer** so neither has the databases open.

2. **Optionally preserve packet stream history** from the old viewer DB into the main DB:
   - From the project root, run:
     ```bash
     python3 migrate_webviewer_db.py bot_data.db meshcore_bot.db
     ```
     Use your actual paths if they differ (e.g. full paths or different filenames). The script copies the `packet_stream` table from the first file into the second and skips rows that would duplicate IDs.
   - If you don’t care about old packet stream data, skip this step; the viewer will create a new `packet_stream` table in the main DB.

3. **Point the viewer at the main database** in `config.ini`:
   ```ini
   [Web_Viewer]
   db_path = meshcore_bot.db
   ```
   (Or the same value as `[Bot] db_path` if you use a different path.)

4. **Start the bot (and viewer as usual)**. The viewer will now read and write to the same database as the bot.

You can keep or remove the old `bot_data.db` file after verifying the viewer works with the shared DB.

## Troubleshooting

### Web viewer not accessible (e.g. Orange Pi / SBC)

If the viewer does not load from another device (e.g. from your phone or PC while the bot runs on an Orange Pi), work through these steps on the Pi.

1. **Confirm config**
   - In `config.ini` under `[Web_Viewer]`:
     - `enabled = true`
     - `auto_start = true` (if you want it to start with the bot)
     - `host = 0.0.0.0` (required for access from other devices; `127.0.0.1` is localhost only)
     - `port = 8080` (or another port 1024–65535)
   - Restart the bot after changing config.

2. **Check that the viewer process is running**
   ```bash
   # From project root on the Pi
   ss -tlnp | grep 8080
   # or
   netstat -tlnp | grep 8080
   ```
   If nothing listens on your port, the viewer did not start or has exited.

3. **Inspect viewer logs**
   - When run by the bot, the viewer writes to:
     - `logs/web_viewer_stdout.log`
     - `logs/web_viewer_stderr.log`
   - Look for Python tracebacks, "Address already in use", or missing dependencies (e.g. Flask, flask-socketio).
   - Optional: run the viewer manually to see errors in the terminal:
     ```bash
     cd /path/to/meshcore-bot
     python3 modules/web_viewer/app.py --config config.ini --host 0.0.0.0 --port 8080
     ```

4. **Check integration startup**
   - Bot logs may show: `Web viewer integration failed: ...` or `Web viewer integration initialized`.
   - If integration failed, the viewer subprocess is never started; fix the error shown (e.g. invalid `host` or `port` in config).

5. **Firewall**
   - Many SBC images (e.g. Orange Pi, Armbian minimal) do **not** ship with a firewall; if `curl` to localhost works and `host = 0.0.0.0`, the blocker may be network (Wi‑Fi client isolation, different subnet, or router). Check from a device on the same LAN using `http://<PI_IP>:8080`.
   - If your system uses **ufw**:
     ```bash
     sudo ufw status
     sudo ufw allow 8080/tcp
     sudo ufw reload
     ```
   - If `ufw` is not installed (e.g. `sudo: ufw: command not found`), you may have no host firewall—that’s common on embedded images. To allow the port with **iptables** (often available when ufw is not):
     ```bash
     sudo iptables -I INPUT -p tcp --dport 8080 -j ACCEPT
     ```
     (Rules may not persist across reboots unless you use a persistence method for your distro.)
   - If you prefer ufw, install it (e.g. `sudo apt install ufw`) and use the ufw commands above.

6. **Test from the Pi first**
   ```bash
   curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/
   ```
   If this returns `200`, the viewer is running and the issue is binding or firewall. If you use `host = 0.0.0.0`, then try from another device: `http://<PI_IP>:8080`.

7. **Standalone run (no bot)**
   - To rule out bot integration issues, start the viewer by itself (same config path so it finds the DB):
     ```bash
     python3 modules/web_viewer/app.py --config config.ini --host 0.0.0.0 --port 8080
     ```
   - If `restart_viewer.sh` is used, note it binds to `127.0.0.1` by default; for network access run the command above with `--host 0.0.0.0` or edit the script.

### Flask Not Found
```bash
pip3 install flask flask-socketio
```

### Database Not Found
- Ensure the bot has been run at least once to create the databases
- Check file permissions on database files

### Port Already in Use
- Change the port in `config.ini` or stop the conflicting service
- Use `ss -tlnp | grep 8080` or `lsof -i :8080` (if available) to find what's using the port

### Permission Denied
```bash
chmod +x restart_viewer.sh
```

## Security Notes

- The web viewer is designed for local network use
- Set `host = 127.0.0.1` for localhost-only access
- Set `host = 0.0.0.0` for network access (use with caution)
- For network access, set `web_viewer_password` or use a reverse proxy with authentication and firewall rules

## Future Enhancements

- Live packet streaming
- Real-time message monitoring
- Interactive contact management
- Export functionality
- Additional authentication options
- Mobile-responsive design improvements
