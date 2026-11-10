# MeshCore Bot

The add-on runs the MeshCore radio bot and opens its web viewer from the Home Assistant sidebar.

## Install

1. In Home Assistant, open **Settings**, then **Add-ons**, then **Add-on store**.
2. Open the menu in the upper corner and choose **Repositories**.
3. Add `https://github.com/agessaman/meshcore-bot` and install **MeshCore Bot**.
4. Start the add-on. The first start copies the example configuration if you do not already have one.

The add-on is built for 64-bit systems (amd64 and aarch64). The image is `ghcr.io/agessaman/meshcore-bot-ha`.

## Connection

Choose one connection type. Leave the other connection fields empty so they do not overwrite `config.ini`.

**Serial.** Set the connection type to `serial` and pick the USB radio. The device list shows tty devices. A path under `/dev/serial/by-id/` stays pointed at the same radio when the machine assigns a different `/dev/ttyUSB*` name. The add-on has UART access for this.

**TCP.** Set the connection type to `tcp` and enter the hostname (or IP address) and port of a MeshCore TCP companion. The port field maps to `tcp_port`. The host field maps to `hostname`. The companion's usual port is 5000.

**Bluetooth Low Energy.** Experimental. Set the connection type to `ble` and, if you want a specific radio, its device name or address. Leave the device name empty to let the bot discover one. The add-on sets `host_dbus` so it can talk to the host BlueZ daemon. BLE depends on that daemon and does not work on every Home Assistant install.

## Startup failure

If the add-on cannot reach the radio when it starts, it exits with a failure status instead of stopping cleanly. Turn on the add-on's **Watchdog** toggle so Home Assistant restarts it automatically, for example when the radio is not ready yet after a reboot.

## config.ini

Inside the add-on the file is `/config/config.ini`. On the host it is `/addon_configs/<prefix>_meshcore_bot/config.ini`. Open it with the File Editor add-on or the Samba add-on. `<prefix>` is the id Home Assistant assigns this repository.

The first start copies `config.ini.example` into that path when the file is missing, so the commented example is what you edit.

Options you set in the add-on configuration are written into the matching keys on every start. An option you leave empty is not written, so a value you changed in `config.ini` stays. Fixed add-on paths (the database, local plugins, and an empty log file so logs go to the add-on log) are applied on every start.

The database is `/data/databases/meshcore_bot.db`. The web viewer uses that same file. Local plugins, their translation overrides, and the optional local config overlay live in `/config/local`, which you can open with the File Editor or Samba add-on. If the local overlay `/config/local/config.ini` sets a key the add-on manages, the add-on removes that key from the overlay so its own value wins. Home Assistant includes `/data` in add-on backups. Configuration backups the add-on takes before it rewrites `config.ini` are in `/data/backups`.

## Sidebar

The add-on adds a sidebar entry that opens the web viewer through Home Assistant's login. The viewer listens on port 8080 inside the add-on. The sidebar works with that port left unpublished.

## Publishing the viewer port

The **Network** section lists port 8080, turned off until you enable it. Publish it only after you set a web viewer password. The published port is on your network and is not covered by your Home Assistant login. The sidebar is.

Until a password is set, the log reports that the viewer is bound to every interface without one. Set the password in the add-on options (or in `config.ini`) before you publish the port.

## Sharing a USB radio

Sharing one USB radio with the meshcore-ha integration is not supported. One serial port has one owner. Use a TCP companion, a second radio, or the community proxy add-on at <https://github.com/vipje/Meshcore-bot-for-Home-Assistant>.
