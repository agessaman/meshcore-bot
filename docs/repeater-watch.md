# Repeater watch

Unattended repeaters on rooftops and masts fail quietly. A solar battery runs flat, a mains supply drops, firmware hangs after a storm, and nobody notices until someone complains that the mesh is patchy.

The bot already hears the two signs that a repeater is working, so it can notice when they stop:

- **Adverts.** Each advert the bot hears updates the repeater's last-heard time.
- **Relaying.** Each packet path the bot sees records which repeaters forwarded it, so a repeater carrying traffic stays fresh between adverts.

Pick the repeaters you look after on **Settings → Repeater Watch** in the web viewer. When one has gone `offline_after_hours` with neither, the bot sends one notice. When it is heard again, it sends one more (unless `notify_recovery` is off). Nothing repeats while it stays down, so an outage costs at most two messages per target.

## What counts as offline

A watched repeater is offline when **no advert and no relayed traffic** has been seen for `offline_after_hours` (24 by default). Repeaters advertise every few hours, so a threshold shorter than a couple of advert intervals will report healthy repeaters that are simply quiet.

Relaying is matched by the repeater's full public key wherever the mesh graph has one. Otherwise it is matched by a 2- or 3-byte prefix, but only when no other known repeater shares that prefix. A 1-byte prefix is never trusted: on a busy mesh many repeaters share each one, and another repeater's traffic would keep a dead one looking alive.

## Only silence the bot was listening for

The bot can only vouch for what it could have heard:

- The clock a repeater is judged against starts when the bot started listening: at startup, after a radio reconnect, or after the radio recovers from being offline or unresponsive. A restart soon after the previous check keeps the old clock, so a quick restart doesn't blind it for another day.
- A check is skipped, and listening starts over, while the radio is disconnected, offline or unresponsive, or while the bot has heard **no node at all** for two hours. A dead antenna on the bot is not reported as every repeater on the mesh going down.

A repeater the bot has never heard is reported once the bot has been listening for a full `offline_after_hours`.

## Where notices go

Any mix of:

- **Channels** (`channels`), optionally on a regional `flood_scope`.
- **Direct messages** (`dm_recipients`) to contacts the radio knows.
- **Discord** webhooks and **Telegram** chats, like the other alert services.

With no target set, outages are only logged and listed on the page. When several repeaters change in one check, their notices share a message where they fit the byte budget.

## Settings

All of these are on the web viewer page; the page saves them to `[Repeater_Watch]` in `config.ini` and the bot picks them up without a restart.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Run the checks and send notices. |
| `watch` | | Public keys to watch, comma-separated. A prefix of 8 or more hex characters matches the most recently heard repeater with it. |
| `watch_starred` | `false` | Also watch every repeater starred on the Contacts page. |
| `offline_after_hours` | `24` | Silence, in hours (1–720), before a repeater is offline. |
| `check_interval_minutes` | `15` | How often to check (5–1440). |
| `notify_recovery` | `true` | Also report when an offline repeater is heard again. |
| `channels` | | Channels for notices. |
| `dm_recipients` | | Contact names (or keys) to DM. |
| `flood_scope` | | Regional scope for channel notices, e.g. `#west`. |
| `offline_message` | `Repeater offline: {name} ({prefix}), not heard for {silent_for}` | Offline notice. |
| `recovered_message` | `Repeater back: {name} ({prefix}), was offline {down_for}` | Back-online notice. |
| `discord_webhook_urls` | | Discord webhooks, comma-separated. |
| `telegram_chat_ids` | | Telegram chat IDs or `@channel` names. |
| `telegram_bot_token` | | Blank uses `TELEGRAM_BOT_TOKEN`, then `[TelegramBridge] api_token`. |

Message placeholders:

| Placeholder | Example | Meaning |
| --- | --- | --- |
| `{name}` | `Hilltop` | The repeater's advertised name. |
| `{prefix}` | `A1` | Its hop prefix, at the bot's `[Bot] prefix_bytes` width. |
| `{silent_for}` | `26h` | How long it had been quiet. `26h+` means never heard since the bot started listening. |
| `{down_for}` | `5h` | How long the outage lasted (back-online notice). |
| `{last_heard}` | `Nov 3 07:42` | When it was last heard, in the bot's local time. |

## Airtime

One check reads the database only; it transmits nothing. A notice is sent only when a repeater changes state, once per channel and DM recipient. With the defaults, a repeater that goes down overnight and comes back the next day costs two messages per target.
