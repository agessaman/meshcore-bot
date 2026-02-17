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

### Flask Not Found
```bash
pip3 install flask
```

### Database Not Found
- Ensure the bot has been run at least once to create the databases
- Check file permissions on database files

### Port Already in Use
- Change the port in `config.ini` or stop the conflicting service
- Use `lsof -i :5000` to find what's using the port

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
