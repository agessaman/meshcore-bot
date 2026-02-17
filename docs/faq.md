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
