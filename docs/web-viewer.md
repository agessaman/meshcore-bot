# Web Viewer

A browser-based dashboard for monitoring and managing your MeshCore bot. The viewer shares the bot’s SQLite database by default and provides real-time streams, contact management, mesh graph visualization, radio control, and in-browser configuration.

## Features

- **Contacts** — Live contact list with signal, path, and location; star contacts; purge inactive contacts; export CSV/JSON
- **Mesh graph** — Interactive node graph at `/mesh`
- **Radio** — Channel management, reboot, connect/disconnect radio
- **Feeds** — RSS/API feed subscriptions per channel
- **Packets** — Raw packet monitor
- **Live activity** — Real-time packet/command/message feed at `/realtime` (pause and clear)
- **Logs** — Real-time log viewer at `/logs` with level filtering
- **Config** — SMTP, log rotation, backups, maintenance status at `/config`
- **Admin config** — Read-only effective config with secrets redacted at `/admin/config`
- **API Explorer** — Interactive API documentation at `/api-explorer`
- **Operational banners** — Initializing, zombie-radio, and radio-offline alerts when applicable
- **Version footer** — Displays resolved bot version

For SMTP and nightly maintenance email details, see the [README Web Viewer section](https://github.com/agessaman/meshcore-bot/blob/main/README.md#web-viewer).

## Quick start

### Integrated with the bot (recommended)

1. Edit `config.ini`:

   ```ini
   [Web_Viewer]
   enabled = true
   auto_start = true
   host = 127.0.0.1
   port = 8080
   web_viewer_password = yourpassword
   ```

2. Start the bot. The viewer starts automatically when `auto_start = true`.

3. Open `http://localhost:8080` (or `http://<bot-ip>:8080` if `host = 0.0.0.0`).

### Standalone mode

Use standalone mode only for debugging or when the bot is not running:

```bash
pip install flask flask-socketio
python3 -m modules.web_viewer.app --config config.ini --host 127.0.0.1 --port 8080
```

The viewer reads `[Web_Viewer]` from the config file. Override host/port on the command line if needed.

## Configuration

All options are in the **`[Web_Viewer]`** section of `config.ini`:

```ini
[Web_Viewer]
enabled = false
# web_viewer_password = changeme
host = 127.0.0.1
port = 8080
debug = false
auto_start = false
decode_hashtag_channels =
# db_path = meshcore_bot.db
```

| Option | Description |
|--------|-------------|
| `enabled` | Enable the web viewer |
| `web_viewer_password` | If set, login is required for all routes and Socket.IO. If empty, auth is disabled (not recommended when `host = 0.0.0.0`) |
| `host` | `127.0.0.1` (localhost only) or `0.0.0.0` (all interfaces) |
| `port` | HTTP port (default **8080**, range 1024–65535) |
| `debug` | Flask debug mode (development only) |
| `auto_start` | Start viewer when the bot starts |
| `decode_hashtag_channels` | Comma-separated hashtag channels to decrypt in the packet stream without adding them to the radio |
| `db_path` | Optional separate DB path; if unset, uses `[Bot] db_path` (recommended) |

**Security:** Using `host = 0.0.0.0` without `web_viewer_password` logs an error at startup. Always set a password when exposing the viewer on a LAN or the internet.

## Authentication and security

When `web_viewer_password` is set:

- A **login page** protects all HTML routes and Socket.IO connections.
- **CSRF protection** applies to mutating HTTP POST requests.
- **Security headers** are set on responses.

When the password is empty, the UI is reachable without login. Use `host = 127.0.0.1` for localhost-only access, or set a password before binding to `0.0.0.0`.

For production deployments on untrusted networks, also use a reverse proxy with TLS and restrict access by firewall.

## Pages overview

| Path | Purpose |
|------|---------|
| `/` | Dashboard — database stats, quick navigation |
| `/contacts` | Repeater contacts and contact tracking |
| `/mesh` | Interactive mesh network graph |
| `/radio` | Radio settings and control |
| `/feeds` | Feed manager subscriptions |
| `/realtime` | Live packet, command, and message activity |
| `/logs` | Real-time bot log stream |
| `/config` | Notifications, log rotation, backups, maintenance |
| `/admin/config` | Effective config (secrets redacted) |
| `/api-explorer` | API documentation and try-it UI |
| `/stats` | Message/command/path statistics |
| `/greeter` | Greeter configuration |
| `/cache` | Cache inspection |

Legacy routes such as `/cache` remain for compatibility; primary navigation is from the dashboard navbar.

## Real-time streams

The viewer uses **Socket.IO** for live data. After connecting, the client emits subscription events; the navbar indicator reflects connection state (subscriptions are silent—no per-subscribe toast).

| Client event | Data |
|--------------|------|
| `subscribe_packets` | Raw packet stream |
| `subscribe_commands` | Command invocations |
| `subscribe_messages` | Channel messages |
| `subscribe_logs` | Log lines |
| `subscribe_mesh` | Mesh graph updates |

Use **Live activity** (`/realtime`) for a combined color-coded feed, or subscribe from custom clients via the same events.

## API endpoints

JSON APIs are available for automation (authentication required when `web_viewer_password` is set). Examples:

```bash
curl http://localhost:8080/api/stats
curl http://localhost:8080/api/contacts
curl http://localhost:8080/api/mesh/nodes
```

See **API Explorer** (`/api-explorer`) for the full list of routes and request formats.

## Database

The viewer uses the same database as the bot by default (`[Bot] db_path`, typically `meshcore_bot.db`). That file holds repeater contacts, mesh graph, packet stream, and related tables.

**Dashboard stats** (message/command/path counts) come from `message_stats`, `command_stats`, and `path_stats`. To populate these when the `stats` chat command is disabled, set under `[Stats_Command]`:

```ini
collect_stats = true
```

**Packet stream retention** is controlled by `[Data_Retention] packet_stream_retention_days`. See [Data retention](data-retention.md).

## Migrating from a separate web viewer database

If you previously used a separate viewer database (e.g. `[Web_Viewer] db_path = bot_data.db`):

1. **Stop the bot and viewer** so neither has the databases open.

2. **Optionally migrate packet stream history:**
   ```bash
   python3 migrate_webviewer_db.py bot_data.db meshcore_bot.db
   ```
   Adjust paths as needed. The script copies `packet_stream` rows and skips duplicates.

3. **Remove or comment out** `[Web_Viewer] db_path` so the viewer uses `[Bot] db_path`.

4. **Start the bot** and verify the viewer shows contacts and mesh data.

## Troubleshooting

### Web viewer not accessible (e.g. Orange Pi / SBC)

1. **Confirm config** under `[Web_Viewer]`:
   - `enabled = true`
   - `auto_start = true` (if starting with the bot)
   - `host = 0.0.0.0` for access from other devices
   - `port = 8080` (or another port 1024–65535)
   - `web_viewer_password` set when using `0.0.0.0`
2. **Check the port is listening:** `ss -tlnp | grep 8080`
3. **Inspect logs:** `logs/web_viewer_stdout.log`, `logs/web_viewer_stderr.log`
4. **Bot integration:** Look for `Web viewer integration initialized` or `Web viewer integration failed` in bot logs.
5. **Firewall:** Allow TCP on your viewer port (`ufw allow 8080/tcp` or equivalent).
6. **Test locally:** `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/`
7. **Standalone debug:**
   ```bash
   python3 -m modules.web_viewer.app --config config.ini --host 0.0.0.0 --port 8080
   ```

### Flask not found

```bash
pip install flask flask-socketio
```

### Database not found

- Run the bot at least once to create the database.
- Check file permissions on the DB path.

### Port already in use

- Change `port` in `config.ini` or stop the conflicting service.
- `ss -tlnp | grep 8080` or `lsof -i :8080` to find the process.

### Login required but password forgotten

- Set a new `web_viewer_password` in `config.ini` and restart the bot (or viewer process).
