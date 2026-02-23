# FAQ

Frequently asked questions about meshcore-bot.

## Installation and upgrades

### Will using `--upgrade` on the install script move over the settings file as well as upgrade the bot?

No. The install script **never overwrites** an existing `config.ini` in the installation directory. Whether you run it with or without `--upgrade`, your current `config.ini` is left as-is. So your settings are preserved when you upgrade.

With `--upgrade`, the script also updates the service definition (systemd unit or launchd plist) and reloads the service so the new code and any changed paths take effect.

### If I don't use `--upgrade`, is the bot still upgraded after `git pull` and running the install script?

**Partially.** The script still copies repo files into the install directory and only overwrites when the source file is newer (and it never overwrites `config.ini`). So the **installed code** is upgraded.

Without `--upgrade`, the script does *not* update the service file (systemd/launchd) and does *not* reload the service. So:

- New bot code is on disk.
- The running service may still be using the old code until you restart it (e.g. `sudo systemctl restart meshcore-bot` or equivalent).
- Any changes to the service definition (paths, user, etc.) in the script are not applied.

**Recommendation:** Use `./install-service.sh --upgrade` after `git pull` when you want to upgrade; that updates files, dependencies, and the service, and reloads the service, while keeping your `config.ini` intact.

### I moved a previous database into a new install; the bot runs but I see "Error processing message queue" or "Error processing channel operations". What should I do?

Moving an old database into a new install can cause those errors when:

1. **Schema mismatch** — The old DB may be missing tables or columns (e.g. `feed_message_queue`, `channel_operations`, or `message_send_interval_seconds` on `feed_subscriptions`). The bot runs `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE` on startup, so missing tables/columns are usually added. If the exception in the log is `no such column`, the schema in the copied DB is older than expected; ensure the bot has started at least once so migrations run.
2. **Stale queue/ops** — Pending rows in `feed_message_queue` or `channel_operations` from the old install may reference channels or feeds that don’t exist or differ on the new install. You can clear them so the scheduler stops hitting errors (with the bot stopped). If `sqlite3` is not installed, use Python instead:
   - Clear unsent queue and pending channel ops (Python; no extra packages):
     ```bash
     sudo -u meshcore /opt/meshcore-bot/venv/bin/python -c "
     import sqlite3
     p = '/opt/meshcore-bot/meshcore_bot.db'
     c = sqlite3.connect(p)
     c.execute('DELETE FROM feed_message_queue WHERE sent_at IS NULL')
     c.execute(\"DELETE FROM channel_operations WHERE status = 'pending'\")
     c.commit()
     print('Cleared pending queue and channel ops')
     c.close()
     "
     ```
   - Or with the `sqlite3` CLI if available:
     `sqlite3 /path/to/meshcore_bot.db "DELETE FROM feed_message_queue WHERE sent_at IS NULL; DELETE FROM channel_operations WHERE status = 'pending';"`
   - To clear only pending channel ops:
     `sudo -u meshcore /opt/meshcore-bot/venv/bin/python -c "import sqlite3; c=sqlite3.connect('/opt/meshcore-bot/meshcore_bot.db'); c.execute(\"DELETE FROM channel_operations WHERE status = 'pending'\"); c.commit(); print('Cleared pending channel ops'); c.close()"`
3. **Timeout** — If the log line has nothing after the colon, the exception is often a 30s timeout (scheduler runs queue/ops with a 30s limit). A large backlog or slow DB can trigger it; clearing pending queue/ops as above usually fixes it.

After pulling the latest code, the next time an error occurs the log will include a full traceback (exception type and message), which makes the cause clear.

## Command reference and website

### How can I generate a custom command reference for my bot users?

See [Custom command reference website](command-reference-website.md): it explains how to use `generate_website.py` to build a single-page HTML from your config (with optional styles) and upload it to your site.

## Hardware and performance

### How do I run meshcore-bot on a Raspberry Pi Zero 2 W?

The Pi Zero 2 W has 512 MB of RAM. The bot and the web viewer are two separate
Python processes; together they use roughly 300 MB on a busy mesh, which leaves
little headroom. Follow the two steps below to keep things comfortable.

#### Step 1 — Run the bot only (saves ~150 MB)

The web viewer is optional. If you don't need the browser-based dashboard on
the Pi itself, disable it and access it from another machine instead:

```ini
[Web_Viewer]
enabled = false
auto_start = false
```

The bot continues to work normally; the web viewer just won't start on the Pi.
If you still want the dashboard, run the viewer on a desktop or server that
shares the same database file (see [MeshCore Bot Data Viewer](web-viewer.md)).

#### Step 2 — Tune the Mesh Graph (saves another 50–100 MB on busy meshes)

Even with the web viewer off, the Mesh Graph can grow large. Add the following
to the `[Path_Command]` section of your `config.ini`:

```ini
[Path_Command]
# Limit startup memory: only load edges seen in the last 7 days.
# Edges older than this have near-zero path confidence anyway.
graph_startup_load_days = 7

# Evict edges from RAM after 7 days without a new observation.
graph_edge_expiration_days = 7

# Write graph updates in batches rather than on every packet.
graph_write_strategy = batched

# If you don't use the !path command at all, disable graph capture
# entirely to eliminate the background thread and all graph overhead.
# graph_capture_enabled = false
```

These settings do not affect path prediction accuracy: edges older than a few
days carry negligible confidence due to the 48-hour recency half-life used by
the scoring algorithm.
