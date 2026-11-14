# Home Assistant

MeshCore Bot is available as a Home Assistant add-on for Home Assistant OS and Supervised installs. It runs the bot and keeps its database and settings in Home Assistant's add-on storage. Turn on **Show in sidebar** on the add-on's Info page to open the web viewer from the sidebar.

## Add the repository

1. In Home Assistant, open Settings, then Add-ons, then Add-on store.
2. Open the menu in the upper corner and choose Repositories.
3. Add `https://github.com/agessaman/meshcore-bot`, then install MeshCore Bot.
4. On the add-on's Info page, turn on **Show in sidebar**, then start the add-on. The **Open Web UI** button works whether the sidebar entry is on or not.

The add-on image is built for 64-bit systems only (amd64 and aarch64). On a 32-bit install, use the regular [Docker image](docker.md) or a [normal install](installation.md) instead.

The add-on's Documentation tab covers the connection options, `config.ini`, backups, the sidebar, and publishing the viewer port. It is also on GitHub: [MeshCore Bot add-on documentation](https://github.com/agessaman/meshcore-bot/blob/main/homeassistant/meshcore_bot/DOCS.md).
