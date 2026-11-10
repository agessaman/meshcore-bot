# Home Assistant

MeshCore Bot runs as a Home Assistant add-on. The store entry lives in this repository, and the sidebar opens the web viewer.

## Add the repository

In Home Assistant, open Settings, then Add-ons, then Add-on store. Open the menu in the upper corner and choose Repositories. Add:

```text
https://github.com/agessaman/meshcore-bot
```

Install MeshCore Bot and start it. The image is `ghcr.io/agessaman/meshcore-bot-ha`, built for 64-bit Intel and ARM (amd64 and aarch64).

The add-on's Documentation tab has the connection choices, the sidebar, and the published viewer port. Short version:

- **Serial:** pick the USB device. A `/dev/serial/by-id/` path stays on the same radio across reboots.
- **TCP:** point the add-on at a MeshCore TCP companion.
- **Bluetooth Low Energy:** experimental, and it needs the host Bluetooth daemon.

## Configuration

On each start the add-on writes `/config/config.ini`. On the host that file is `/addon_configs/<prefix>_meshcore_bot/config.ini`. Edit it with the File Editor or Samba add-on.

Options you set in the add-on configuration replace the matching keys on every start. An option you leave empty leaves that key in `config.ini` alone.

The database is `/data/databases/meshcore_bot.db`. Local plugins are in `/data/local`. Logs go to the add-on log.

The sidebar uses your Home Assistant login. Publish the viewer port from the add-on's Network section when you also want it on the LAN, and set a web viewer password first.

## One radio, one serial owner

Sharing one USB radio with the meshcore-ha integration is not supported. A serial port has one owner. Use a TCP companion, a second radio, or the community proxy add-on at <https://github.com/vipje/Meshcore-bot-for-Home-Assistant>.
