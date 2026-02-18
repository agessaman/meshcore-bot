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

## Command reference and website

### How can I generate a custom command reference for my bot users?

See [Custom command reference website](command-reference-website.md): it explains how to use `generate_website.py` to build a single-page HTML from your config (with optional styles) and upload it to your site.

## Hardware and performance

### How do I run meshcore-bot on a Raspberry Pi Zero 2 W?

The Pi Zero 2 W has 512 MB of RAM, which is enough to run the bot, but the Mesh Graph can grow large on a busy mesh. Add the following to the `[Path_Command]` section of your `config.ini` to keep memory usage in check:

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

These settings do not affect path prediction accuracy: edges older than a few days carry negligible confidence due to the 48-hour recency half-life used by the scoring algorithm.
