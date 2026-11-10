# MeshCore Bot

The add-on runs the MeshCore radio bot and opens its web viewer from the Home Assistant sidebar.

## Install

1. In Home Assistant, open **Settings**, then **Add-ons**, then **Add-on store**.
2. Open the menu in the upper corner and choose **Repositories**.
3. Add `https://github.com/agessaman/meshcore-bot` and install **MeshCore Bot**.
4. Start the add-on. The first start copies `config.ini.example` to `/config/config.ini` if the file is not there yet.

The add-on is built for 64-bit systems (amd64 and aarch64). The image is `ghcr.io/agessaman/meshcore-bot-ha`.

## Options

| Option | `config.ini` key |
|--------|------------------|
| `connection_type` | `[Connection] connection_type` (`serial`, `tcp`, or `ble`) |
| `serial_port` | `[Connection] serial_port` |
| `tcp_host` | `[Connection] hostname` |
| `tcp_port` | `[Connection] tcp_port` |
| `ble_device_name` | `[Connection] ble_device_name` |
| `bot_name` | `[Bot] bot_name` |
| `log_level` | `[Logging] log_level` |
| `web_viewer_password` | `[Web_Viewer] web_viewer_password` |

Options you set are applied to `config.ini` on every start. An option you leave empty is not written, so the key in `config.ini` is left alone. The add-on also applies fixed values on every start: the database path, `local_dir_path`, an empty log file so logs go to the add-on log, and the viewer settings that run it at startup behind ingress on port 8080.

## Connection

Choose one connection type. Leave the fields for the other types empty so they do not overwrite `config.ini`.

**Serial.** Set the connection type to `serial` and pick the USB radio. The device list shows tty devices. A path under `/dev/serial/by-id/` stays pointed at the same radio when the machine assigns a different `/dev/ttyUSB*` name. The add-on has UART access for this.

**TCP.** Set the connection type to `tcp` and enter the hostname (or IP address) and port of a MeshCore TCP companion. The host option is saved as `[Connection] hostname`, and the port option as `[Connection] tcp_port`. The usual port is 5000.

**Bluetooth Low Energy.** Experimental. Set the connection type to `ble` and, if you want a specific radio, its device name or address. Leave the device name empty to let the bot discover one. The add-on sets `host_dbus` so it can reach the host BlueZ daemon. BLE depends on that daemon and does not work on every Home Assistant install.

## Startup failure

If the add-on cannot reach the radio when it starts, the bot exits with status 1 instead of stopping cleanly. Turn on the add-on's **Watchdog** toggle so Home Assistant restarts it automatically, for example when the radio is not ready yet after a reboot.

## config.ini

Inside the add-on the file is `/config/config.ini`. On the host it is `/addon_configs/<prefix>_meshcore_bot/config.ini`. Open it with the File Editor add-on or the Samba add-on. `<prefix>` is the id Home Assistant assigns this repository.

## Data and local files

The database is `/data/databases/meshcore_bot.db`. The web viewer uses the same file. Home Assistant includes `/data` in add-on backups. The add-on also writes a backup of `config.ini` to `/data/backups` before it changes the file.

Local plugins, their translation overrides, and an optional local config overlay live in `/config/local`, which you can open with the File Editor or Samba add-on. If `/config/local/config.ini` sets a key the add-on manages, the add-on removes that key from the overlay on start so its own value wins.

## Sidebar

The add-on adds a sidebar entry that opens the web viewer. Home Assistant users are signed in automatically, so the viewer password is not needed there. The viewer listens on port 8080 inside the add-on, and the sidebar works with that port left unpublished.

## Publishing the viewer port

The **Network** section lists port 8080, turned off until you enable it. The sidebar does not need it: Home Assistant reaches the viewer through ingress, and those requests are already signed in. Requests to the published port are not covered by your Home Assistant login. The add-on sets `[Web_Viewer] ha_ingress`, so the viewer fails closed for anything that does not come through the Supervisor: until you set `web_viewer_password` in the add-on options, direct requests get 403 and the sidebar keeps working. Set a password before publishing the port to sign in directly.

## Sharing a USB radio

Sharing one USB radio with the meshcore-ha integration is not supported. One serial port has one owner. Use a TCP companion, a second radio, or the community proxy add-on at <https://github.com/vipje/Meshcore-bot-for-Home-Assistant>.
