# MeshCore Bot Data Viewer

A web-based interface for viewing and analyzing data from your MeshCore Bot.

## Features

- **Dashboard**: Overview of database statistics and bot status
- **Repeater Contacts**: View active repeater contacts with location and status information
- **Contact Tracking**: Complete history of all heard contacts with signal strength and routing data
- **Cache Data**: View cached geocoding and API responses
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
```

## Accessing the Viewer

Once started, open your web browser and navigate to:
- **Local access**: http://localhost:5005 (or your configured port)
- **Network access**: http://YOUR_BOT_IP:5005 (if host is set to 0.0.0.0)

## Pages Overview

### Dashboard
- Database status and statistics
- Contact counts and cache information
- Quick navigation to other sections

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

### Cache Data
- Geocoding cache entries
- Generic cache entries (weather, sports, etc.)
- Expiration status
- Cache value previews

### Purging Log
- Audit trail of contact purging operations
- Timestamps and reasons
- Contact names and public keys

## API Endpoints

The viewer also provides JSON API endpoints:

- `GET /api/stats` - Database statistics
- `GET /api/contacts` - Repeater contacts data
- `GET /api/tracking` - Contact tracking data

Example usage:
```bash
curl http://localhost:5000/api/stats
```

## Database Requirements

The viewer uses the same database as the bot by default (`[Bot] db_path`, typically `meshcore_bot.db`). That single file holds repeater contacts, mesh graph, packet stream, and other data so the viewer can show everything.

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
- No authentication is implemented - consider firewall rules for production use

## Future Enhancements

- Live packet streaming
- Real-time message monitoring
- Interactive contact management
- Export functionality
- Authentication system
- Mobile-responsive design improvements
