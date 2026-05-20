# Installation

Choose how to run the bot:

| Method | Best for |
|--------|----------|
| **[Docker](docker.md)** | Containers, consistent environments, easy updates |
| **[Service (systemd)](service-installation.md)** | Linux servers, run at boot, no containers |
| **Debian package** | `make deb` in the repo — see [README](https://github.com/agessaman/meshcore-bot/blob/main/README.md) |

## Requirements

- **Python 3.10+**
- MeshCore-compatible device (USB, BLE, or TCP)

## Development setup

See [Getting started](getting-started.md) for a quick development setup (run from the repo with `python meshcore_bot.py`).

## Upgrading

If you are upgrading from an older release, read the [Upgrade guide](upgrade.md) before restarting the bot.
