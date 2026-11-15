# Matrix Bridge Service

The Matrix Bridge service posts MeshCore channel messages to Matrix rooms via the [Matrix Client-Server API](https://spec.matrix.org/latest/client-server-api/). This is a **one-way, read-only bridge**: messages only flow from MeshCore to Matrix. No mesh airtime is spent, and inbound Matrix messages are not relayed to the mesh.

**Features:**

- One-way message flow (MeshCore → Matrix only)
- Multi-channel mapping to room IDs or `#alias:server` aliases, with fan-out to several rooms
- Access token checked with `GET /account/whoami` in the background at startup; a rejected token stops the bridge, an unreachable homeserver does not
- `m.text` events with an HTML-formatted body (`org.matrix.custom.html`)
- **DMs are NEVER bridged** (hardcoded for privacy)
- Per-room and overall rate limiting, a pause across all rooms on HTTP 429, retries with exponential backoff, and idempotent resends
- Disabled by default (opt-in)

---

## Quick Start

### 1. Create a bot account

Create a dedicated Matrix account for the bot, or reuse an account you control. A dedicated account keeps the bot's display name, avatar, and room membership separate from a person's.

- **Public homeserver (matrix.org)**: register through your usual client, or use the homeserver's registration flow.
- **Self-hosted Synapse**: create the user locally, for example:

  ```bash
  register_new_matrix_user -c /etc/synapse/homeserver.yaml http://localhost:8008
  ```

### 2. Get an access token

Use whichever method you prefer:

- **Element**: log in as the bot account, open **Settings → Help & About**, expand **Advanced**, and copy the **Access token**.
- **Login API**:

  ```bash
  curl -X POST https://matrix.org/_matrix/client/v3/login \
    -H 'Content-Type: application/json' \
    -d '{"type":"m.login.password","user":"@mybot:matrix.org","password":"…"}'
  ```

  The `access_token` field in the response is what the bridge needs.

Treat the token like a password: it grants full access to the bot account.

### 3. Join the bot to each room

The bridge does not invite itself. Invite the bot account to every target room and accept the invitation as the bot (in Element, or with `POST /_matrix/client/v3/join/{roomIdOrAlias}`). Rooms must be **unencrypted**: the bridge sends plain `m.room.message` events and does not support end-to-end encryption.

To find a room's ID, open the room in Element and look under **Room settings → Advanced → Internal room ID** (`!abcdef:server`). Aliases (`#room:server`) are resolved at startup; one that fails to resolve is retried at most once a minute as messages arrive. The bot must still be joined to the room.

### 4. Configure the bot

Edit `config.ini`:

```ini
[MatrixBridge]
enabled = true
homeserver = https://matrix.org
access_token = syt_your_access_token_here

# Map MeshCore channel names to Matrix rooms
bridge.Public = !abcdef:matrix.org
# Fan out to several rooms:
# bridge.emergency = #emergency:matrix.org, !ghijkl:matrix.org
```

You can also set the token via the `MATRIX_ACCESS_TOKEN` environment variable (takes precedence over config).

### 5. Restart the bot

```bash
sudo systemctl restart meshcore-bot
# OR if running manually: python3 meshcore_bot.py
```

Check the log for `Matrix bridge authenticated as @mybot:matrix.org` and `Subscribed to CHANNEL_MSG_RECV events`.

### 6. Test

Send a message on the bridged MeshCore channel. It should appear in the Matrix room as `SenderName: message` with the sender in bold.

---

## Configuration

### Config Keys

| Key | Required | Description |
|-----|----------|-------------|
| `enabled` | Yes | `true` to enable the bridge, `false` to disable (default: false) |
| `homeserver` | Yes | Base URL of the homeserver, e.g. `https://matrix.org`. A trailing slash is stripped. `http://` is accepted only for `localhost` and `127.0.0.1`. |
| `access_token` | Yes* | Token for the bot account. Can use env var `MATRIX_ACCESS_TOKEN` instead. Never place it in a `bridge.*` row. |
| `bridge.<channel>` | At least one | MeshCore channel name → room ID (`!abc:server`) or alias (`#room:server`). Comma-separate several rooms to fan out. Use the channel name **without** `#` (e.g. `bridge.Public`). Matching is case-insensitive and ignores a leading `#`. |
| `max_message_length` | No | 1–4000; longer messages are truncated with `…` (default: 4000) |
| `filter_profanity` | No | Profanity handling: `drop` (default), `censor`, or `off`. Word list via `better-profanity`; hate symbols (e.g. 卐/卍) are always blocked/censored. |
| `bridge_bot_responses` | No | `true` (default) also bridges the bot's own command replies; `false` bridges only other users' messages |

\* Either `access_token` in config or `MATRIX_ACCESS_TOKEN` in the environment must be set when the bridge is enabled.

### Example

```ini
[MatrixBridge]
enabled = true
homeserver = https://matrix.org
access_token = syt_abcdefghijklmnopqrstuvwxyz
# max_message_length = 4000
# filter_profanity = drop
# bridge_bot_responses = true

bridge.Public = !abcdefghijklmnop:matrix.org
bridge.emergency = #emergency:matrix.org, !qrstuvwxyz:matrix.org
```

---

## Security & Privacy

### Token Security

- **Do not log or expose the access token.** The service masks it in logs (first/last four characters).
- The token is read only from `[MatrixBridge] access_token` or `MATRIX_ACCESS_TOKEN`. It is never read from a `bridge.*` mapping row, which the settings UI renders as plain text.
- Prefer the `MATRIX_ACCESS_TOKEN` environment variable in shared environments.
- **Rotate the token** (log the bot out of all sessions, then log in again) if it was ever exposed.

### DMs Are Never Bridged

For privacy, **DMs are NEVER bridged to Matrix**. Only channel messages from explicitly configured MeshCore channels are posted. This is hardcoded and cannot be changed via configuration.

### Unencrypted rooms only

The bridge posts to unencrypted rooms. End-to-end encrypted rooms are not supported. If a room has encryption enabled, its messages may be unreadable to members and Matrix clients may reject or hide them; use an unencrypted room for bridged traffic.

---

## Rate Limits

Matrix rate limits are enforced per user, not per room. The service keeps a conservative default of about **one message per second overall**, plus a per-room minimum interval, so a busy mesh cannot exhaust the bot account's limits. It:

- Queues messages per room and spaces sends out
- On HTTP 429 / `M_LIMIT_EXCEEDED`, pauses sends to every room for the homeserver's `retry_after_ms` (or the `Retry-After` header) without spending the retry budget
- Uses exponential backoff on other transient failures and drops messages after max retries or max queue age (5 minutes)
- Drops permanent failures (`M_FORBIDDEN`, `M_NOT_FOUND`, HTTP 403/404) immediately instead of retrying, and stops the bridge on a rejected token (HTTP 401, `M_UNKNOWN_TOKEN`)

Each queued message gets one transaction ID per room, reused across retries, so a retry after an ambiguous timeout does not post a duplicate.

---

## Message Format

Bridged messages are sent as `m.text` events with an HTML body:

```json
{
  "msgtype": "m.text",
  "body": "SenderName: message",
  "format": "org.matrix.custom.html",
  "formatted_body": "<b>SenderName</b>: message"
}
```

- Matrix has no per-message username override, so the sender is included in the body.
- Sender and message text are HTML-escaped in `formatted_body` (`&`, `<`, `>`, quotes).
- MeshCore mentions `@[username]` are rendered as plain text `@username`; no Matrix pills or notifications are created.
- Messages longer than `max_message_length` (at most 4000 characters) are truncated with `…`. The cap keeps the event, which carries the text twice, well under Matrix's 64 KiB limit.

---

## Troubleshooting

### Service Not Starting

- Ensure `[MatrixBridge]` exists and `enabled = true`
- Ensure `access_token` (or `MATRIX_ACCESS_TOKEN`) and `homeserver` are set
- Look for `Matrix bridge: access token rejected` or `Matrix bridge: homeserver is missing or invalid`
- `could not check the access token ... starting anyway` means the homeserver was unreachable at startup; the bridge runs and sends retry once it answers
- Check logs: `grep -i matrix meshcore_bot.log`

### Token Rejected (`M_UNKNOWN_TOKEN`)

The token was revoked, expired, or belongs to a different homeserver. Whether that shows up at startup or later while sending, the bridge logs `access token rejected` at ERROR and stays stopped until the bot restarts. Create a new token, update the config, and restart.

### Messages Not Appearing in Matrix

1. **Room mapping**: The config key is the MeshCore channel name **without** `#` (e.g. `bridge.Public` for channel `#public`). Matching is case-insensitive.
2. **Membership**: The bot account must already be joined to the room. Invite it and accept the invitation as the bot.
3. **Alias resolution**: Look for `Could not resolve Matrix room alias` in the log. The bridge retries about once a minute; if the alias never resolves, replace it with the `!roomid:server` form.
4. **Permissions**: The bot needs permission to send messages in the room (`M_FORBIDDEN` means it does not).
5. **Encryption**: Use an unencrypted room; encrypted rooms are not supported.

### 429 Rate Limit

The service pauses all rooms for the homeserver's advertised `retry_after_ms` and then retries. If this happens often, bridge fewer channels or reduce mesh traffic on them.

---

## FAQ

**Q: Can I bridge messages from Matrix to MeshCore?**
A: No. This is a one-way bridge (MeshCore → Matrix only).

**Q: Can I bridge DMs?**
A: No. DMs are never bridged for privacy. This is hardcoded.

**Q: Does the bridge use mesh airtime?**
A: No. It only listens to channel messages the bot already received and posts them over HTTP.

**Q: Can I use end-to-end encrypted rooms?**
A: No. The bridge posts plain `m.room.message` events to unencrypted rooms.

**Q: Can I use a generic Matrix webhook instead of an access token?**
A: No. The bridge uses the Client-Server API with a bot account's access token.

**Q: How do I disable the bridge temporarily?**
A: Set `enabled = false` in `[MatrixBridge]` and restart the bot.

---

## Implementation Details

- **Base**: `BaseServicePlugin` (`modules/service_plugins/base_service.py`) via `ChannelBridgeBase`
- **Event**: Subscribes to `EventType.CHANNEL_MSG_RECV` only
- **HTTP**: `aiohttp` with fallback to `requests` in an executor; timeout ~10 s
- **Startup checks**: run in the background after subscribing, so a slow homeserver doesn't hold up other services: `GET /_matrix/client/v3/account/whoami`, then one `GET /_matrix/client/v3/directory/room/{alias}` per configured alias; failed aliases are retried on delivery at most once per minute
- **Send**: `PUT /_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}`
- **Config**: `config.ini` section `[MatrixBridge]`; example in `config.ini.example`
- **Service file**: `modules/service_plugins/matrix_bridge_service.py`
- **Loader**: Auto-discovered; no changes to `service_plugin_loader.py` (loads when the section exists and `enabled = true`)

**Dependencies:** Uses existing `aiohttp` and `requests`; no new pip dependencies.
