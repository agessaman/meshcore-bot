# Packet Capture Service

Captures packets from the MeshCore network and publishes them to MQTT brokers.

---

## Quick Start

1. **Configure Bot** - Edit `config.ini`:

```ini
[PacketCapture]
enabled = true

# Owner info for JWT auth -- these are optional
owner_public_key = YOUR_COMPANION_PUBLIC_KEY_HERE
owner_email = your.email@example.com

# IATA code for topic routing (XYZ is invalid set it to a real IATA)
iata = XYZ

# MQTT Broker (Let's Mesh Analyzer)
mqtt1_enabled = true
mqtt1_server = mqtt-us-v1.letsmesh.net
mqtt1_port = 443
mqtt1_transport = websockets
mqtt1_use_tls = true
mqtt1_use_auth_token = true
mqtt1_token_audience = mqtt-us-v1.letsmesh.net
mqtt1_topic_status = meshcore/{IATA}/{PUBLIC_KEY}/status
mqtt1_topic_packets = meshcore/{IATA}/{PUBLIC_KEY}/packets
```

2. **Restart Bot** - The service starts automatically

---

## Configuration

### Basic Settings

```ini
[PacketCapture]
enabled = true                    # Enable packet capture
output_file = packets.json        # Optional: save to file
verbose = false                   # Detailed packet logging
debug = false                     # Debug mode
mqtt_skip_unparseable_packets = true   # Skip MQTT when content hash is all zeros (strict path reject / short buffer)

# Optional: skip MQTT for ADVERT packets whose Ed25519 signature does not verify (damaged or spoofed mesh payload).
# Does not affect file/JSONL capture.
advert_require_valid_signature = false

# Optional name reported as the MQTT observer "origin".
# Defaults to the connected MeshCore device/bot name.
observer_name = CustomObserverBot
```

### Authentication

#### Option 1: On-Device Signing (Recommended)
```ini
auth_token_method = device        # Use device's built-in signing
# No private key file needed
```

#### Option 2: Python Signing
```ini
auth_token_method = python        # Use Python signing
private_key_path = /path/to/key.txt  # Path to private key file
```

### MQTT Brokers

Configure multiple brokers using `mqttN_*` pattern:

```ini
# Broker 1
mqtt1_enabled = true
mqtt1_server = mqtt-us-v1.letsmesh.net
mqtt1_port = 443
mqtt1_transport = websockets      # tcp or websockets
mqtt1_use_tls = true
mqtt1_use_auth_token = true
mqtt1_topic_status = meshcore/{IATA}/{PUBLIC_KEY}/status
mqtt1_topic_packets = meshcore/{IATA}/{PUBLIC_KEY}/packets

# Broker 2
mqtt2_enabled = true
mqtt2_server = your.broker.com
mqtt2_port = 1883
mqtt2_transport = tcp
mqtt2_username = user
mqtt2_password = pass
```

#### Connection tuning

| Key | Default | What it does |
|-----|---------|--------------|
| `mqttN_keepalive` | `60` | Seconds between PINGREQs. Lower it to `30` for websockets through a proxy that drops idle connections. |
| `mqttN_client_id` | generated | MQTT client ID. One is generated per broker; set this only if the broker requires a fixed value. |

**Two brokers must never share a client ID.** A broker evicts an existing session
when a second connection arrives under the same ID, so brokers that sit behind one
cluster — `mqtt-a.example` and `mqtt-b.example` of the same service — will kick each
other off in a loop, seconds apart, forever. The generated IDs already differ per
broker; you only reintroduce the problem by setting `mqtt1_client_id` and
`mqtt2_client_id` to the same string.

#### Filtering by packet type

You can limit which packet types are uploaded to each broker with `mqttN_upload_packet_types`. Use a comma-separated list of type numbers; if unset or empty, all packet types are uploaded.

```ini
# Only upload text messages and adverts to this broker
mqtt1_upload_packet_types = 2, 4

# Broker 2 gets everything (default)
# mqtt2_upload_packet_types =
```

**Packet type reference:**

| Type | Name       | Description        |
|------|------------|--------------------|
| 0    | REQ        | Request            |
| 1    | RESPONSE   | Response           |
| 2    | TXT_MSG    | Text message       |
| 3    | ACK        | Acknowledgment     |
| 4    | ADVERT     | Advertisement      |
| 5    | GRP_TXT    | Group text         |
| 6    | GRP_DATA   | Group data         |
| 7    | ANON_REQ   | Anonymous request  |
| 8    | PATH       | Path               |
| 9    | TRACE      | Trace              |
| 10   | MULTIPART  | Multipart          |
| 11–15| Type11–RAW_CUSTOM | Other types |

Packets that are excluded by this filter are still written to the output file (if configured) and still counted; they are only skipped for MQTT upload to that broker. Debug logs will show "Skipping" for those packets.

### Topic Templates

Placeholders:
- `{IATA}` - Your IATA code (e.g., SEA)
- `{iata}` - Lowercase IATA code
- `{PUBLIC_KEY}` - Device public key (uppercase)
- `{public_key}` - Device public key (lowercase)

### Status Publishing and MQTT auth (JWT)

Two separate settings:

- **`jwt_ttl_seconds`** (global) / **`mqttN_jwt_ttl_seconds`** (per broker): lifetime of the JWT in the `exp` claim (`exp = iat + ttl`). Use this when the broker enforces a maximum token lifetime (e.g. 60 minutes → `3600`).
- **`jwt_renewal_interval`** (global) / **`mqttN_jwt_renewal_interval`** (per broker): how often the bot refreshes the MQTT password for that broker. Set **less than** the TTL (e.g. TTL 3600s and renewal every 1800s) so the connection does not outlive the token.

- **`mqttN_jwt_reconnect_on_renew`** (per broker, default `true`): reconnect right
  after minting a new token. MQTT presents credentials once, at CONNECT, so a
  renewed token does nothing for a session that is already open — brokers that
  enforce the JWT's `exp` drop that session the moment the *original* token
  expires. Reconnecting on renewal turns that eviction into one clean, scheduled
  reconnect. Turn it off only if your broker ignores expiry on live sessions.

Per-broker keys override the global values for that broker only. Omit them to inherit globals.

```ini
stats_in_status_enabled = true    # Include device stats in status
stats_refresh_interval = 300      # Publish status every 5 minutes

jwt_ttl_seconds = 86400           # Default JWT exp − iat (24 hours) for all brokers unless overridden
jwt_renewal_interval = 43200      # Default proactive refresh cadence (12 hours); 0 = no renewal task

# Example on a broker that requires 60-minute tokens and refresh halfway through:
# mqtt1_jwt_ttl_seconds = 3600
# mqtt1_jwt_renewal_interval = 1800
```

**Note**: When connecting to waev.app brokers the default settings will cause the connection not to authenticate properly. Please use the following settings on the MQTT connection for the waev.app brokers.

```ini
mqttN_jwt_ttl_seconds = 3600
mqttN_jwt_renewal_interval = 3500
```

---

## Packet Format

### Packet Message
```json
{
  "origin": "MyBot",
  "origin_id": "ABCD1234...",
  "timestamp": "2026-01-04T12:34:56",
  "type": "PACKET",
  "direction": "rx",
  "len": "42",
  "packet_type": "2",
  "route": "D",
  "payload_len": "32",
  "raw": "DEADBEEF...",
  "SNR": "8.5",
  "RSSI": "-42",
  "hash": "ABC123..."
}
```

### Decoded Payloads

When `decode_payloads = true`, each packet gains a nested `decoded` object with plain-text /
structured fields, in addition to the unchanged raw fields above. This makes dumps easy to
process with tools like `jq` (e.g. `jq 'select(.decoded.kind=="GRP_TXT") | .decoded.text'`).

```json
{
  "type": "PACKET",
  "packet_type": "5",
  "route": "F",
  "raw": "1540CAB3...",
  "decoded": {
    "kind": "GRP_TXT",
    "channel_hash": "ca",
    "channel": "#bot",
    "sender": "Alice",
    "text": "hello mesh",
    "msg_timestamp": "2026-07-08T21:22:31Z",
    "decrypted": true,
    "path": ["A1", "B2"]
  }
}
```

The `decoded` object holds only payload-specific content — it does not restate header fields
(`packet_type`, `route`) that already exist at the top level.

What can be decoded:

- **GRP_TXT** (channel messages) are decrypted when a matching channel key is available.
  Keys come from the bot's own configured radio channels automatically, plus
  `decode_hashtag_channels` (keys derived from the `#name`), `decode_channel_keys`
  (`name=hexOrBase64` pairs), and the built-in default **Public** channel key
  (`decode_include_public = true`).
- **ADVERT** packets are parsed into `name`, `mode` (role), `lat`/`lon`, and `public_key`.
- The decoded **path** hop list is included in `decoded.path` when it isn't already present at the
  top level (the top-level `path` is only added for `route=D`), so flood-route paths are captured
  without duplication.
- **Direct messages (TXT_MSG)** are ECDH-encrypted between two nodes and **cannot** be decrypted
  by a passive observer — they appear as `{"kind": "TXT_MSG", "encrypted": true}`.

Publishing of the `decoded` object to MQTT is **off by default** (`include_decoded = false`) — opt
in per broker with `mqttN_include_decoded = true`, or set `include_decoded = true` to publish it to
all brokers. This lets you, e.g., send decoded text to a private broker while public brokers receive
only raw packets. The log file always includes the `decoded` object when `decode_payloads = true`,
independent of this setting.

### Status Message
```json
{
  "status": "online",
  "timestamp": "2026-01-04T12:34:56",
  "origin": "MyBot",
  "origin_id": "ABCD1234...",
  "model": "Heltec V3",
  "firmware_version": "v3.1.2",
  "radio": "915000000,250,9,8",
  "client_version": "meshcore-bot/v1.0.0",
  "stats": {
    "rx_packets": 1234,
    "tx_packets": 567
  }
}
```

---

## Troubleshooting

### Service Not Starting

Check logs:
```bash
tail -f meshcore_bot.log | grep PacketCapture
```

Common issues:
- `enabled = false` in config
- Missing `paho-mqtt` library: `pip install paho-mqtt`

### MQTT Not Connecting

1. **Check broker settings** - Verify hostname and port
2. **Test connection manually**:
   ```bash
   mosquitto_pub -h mqtt-us-v1.letsmesh.net -p 443 -t test -m "test"
   ```
3. **Check authentication** - Verify JWT token generation
4. **Check logs** - Look for connection errors

### MQTT Connecting and Disconnecting in a Loop

Repeated `Disconnected from MQTT broker ... (rc=7: The connection was lost.)`
followed immediately by `✓ Connected to MQTT broker`, over and over:

1. **Check for a shared client ID** — if two brokers alternate (one connects as the
   other drops, every few seconds), they are almost certainly one cluster behind two
   hostnames, evicting each other's session. Give them distinct `mqttN_client_id`
   values, or leave the key empty so one is generated per broker.
2. **Check whether it lines up with your JWT TTL** — roughly one disconnect per
   broker per `mqttN_jwt_ttl_seconds` means the broker is enforcing token expiry.
   Leave `mqttN_jwt_reconnect_on_renew` on and set the renewal interval below the
   TTL so the reconnect happens on your schedule instead of theirs.
3. **Check for a second client on the same identity** — another capture tool running
   against the same brokers with the same node public key can compete with the bot.
4. **Lower `mqttN_keepalive`** to `30` if you are on websockets through a proxy.

Note that `rc=` on a *disconnect* is a paho `MQTT_ERR_*` code, not a CONNACK code:
`rc=7` is a lost connection and `rc=2` is a protocol error. They do not mean the same
thing as the numbers in a `Failed to connect` line.

### No Packets Being Published

1. **Verify MQTT connection** - Check logs for "Connected to MQTT broker"
2. **Check packet count** - Service logs "Captured packet #N" (or "Skipping packet #N" when filtered) for each packet
3. **Verify topics** - Ensure topics match broker expectations
4. **Check upload filter** - If `mqttN_upload_packet_types` is set, only those types are uploaded. DEBUG Logs show "packet type X not in [Y, Z]" when a packet is skipped

---

## Advanced

### Multiple Brokers

Configure up to 10 brokers (mqtt1_* through mqtt10_*). Each broker has independent connection tracking and auto-reconnection.

### Health Monitoring

```ini
health_check_interval = 30        # Check connection every 30s
health_check_grace_period = 2     # Allow 2 failures before warning
```

### Log Rotation

By default `output_file` is a single file that is appended to forever. To keep historical dumps
manageable, enable rotation:

```ini
# Size-based: roll at 50 MB, keep 5 backups (packets.jsonl.1 ... .5)
log_rotation = size
log_max_bytes = 50MB
log_backup_count = 5

# Or time-based: roll daily at midnight, keep 14 days
log_rotation = time
log_rotation_when = midnight
log_backup_count = 14
```

`log_rotation = off` (default) keeps the original single-file behavior. `log_max_bytes` accepts
plain bytes or suffixes like `10M` / `1G`. `log_rotation_when` uses Python's
`TimedRotatingFileHandler` values (`midnight`, `H`, `D`, `W0`–`W6`).

### JWT Authentication

Tokens are valid for 24 hours and auto-renewed. The service tries on-device signing first (if `auth_token_method = device`), then falls back to Python signing.

**Token Format:**
```json
{
  "iat": 1234567890,
  "exp": 1234654290,
  "aud": "mqtt-us-v1.letsmesh.net",
  "publicKey": "DEVICE_PUBLIC_KEY",
  "owner": "OWNER_PUBLIC_KEY",
  "email": "your@email.com",
  "iata": "SEA"
}
```

---

## Neighbour Discovery (zero-hop)

Periodically asks which repeaters this node can hear **directly**, and records each
confirmed link with its measured SNR. Ported from the observer firmware's neighbours
feature by way of `meshcore-packet-capture`. **Off by default.**

This is the strongest link evidence the bot collects. Everything else is weaker:
path inference works from 1–3 byte prefixes with no public keys, and
`complete_contact_tracking.hop_count` over-claims zero-hop (it asserts ~800 zero-hop
contacts where only ~68 have corroborating path evidence). A discover response is a
first-party RF measurement between two full 32-byte public keys.

```ini
[PacketCapture]
enabled = true
neighbors_enabled = true          # the only switch you need
neighbors_interval_hours = 24     # clamped to 12-336
```

That one setting turns the whole feature on. Every enabled broker publishes the
snapshot by default (`mqttN_neighbors` defaults to true) — set it false on any broker
you want to hold back:

```ini
mqtt2_neighbors = false
# mqtt1_topic_neighbors = meshcore/{IATA}/{PUBLIC_KEY}/neighbors   # optional override
```

The neighbours topic is derived from the broker's packets topic by swapping its last
segment, so a broker configured with `meshcore/{IATA}/{PUBLIC_KEY}/packets` publishes to
`meshcore/{IATA}/{PUBLIC_KEY}/neighbors` — the same topic the firmware uses. Brokers
with only a `topic_prefix` get `<prefix>/neighbors`. If a derived topic is
location-routed but no `iata` is set, that broker is skipped with a warning rather than
publishing into `meshcore/XYZ/...` on a shared namespace.

Each cycle sends one zero-hop node-discover request, then listens for
`neighbors_discover_window` seconds (60 by default). **The bot stays fully responsive
during the window** — it is a passive listen, and only the single discover command
touches the radio.

Results go three places, independently of each other:

- **`neighbor_links`** — current adjacency (full public keys, observation count,
  best/last/mean SNR). This is the source of truth.
- **`neighbor_observations`** — one row per neighbour per cycle, for signal history.
  Pruned by `[Data_Retention] neighbor_observations_retention_days` (default 365).
- **The mesh graph** — as edges, when `neighbors_feed_mesh_graph = true` (default),
  plus a dedicated **Neighbours Only** evidence mode on the mesh page and
  `GET /api/mesh/edges?evidence=neighbors`. Confirmed neighbours render as heavier
  lines and show their SNR.

  `mesh_connections` cannot record *why* an edge exists, so the combined view
  re-derives the `neighbors` label from `neighbor_links` — matching on the 3-byte
  prefix pair *or* the full public-key pair, since the graph deliberately keeps
  some edges at a 1-byte prefix while still filling in the keys discovery gave it.
  The label honours the view's `days` window: `neighbor_links` is never pruned, so
  without that a link last heard years ago would keep claiming a recent
  path-derived edge is a current direct neighbour.

Snapshots are published **non-retained**. `heard_secs_ago` is relative to publish time,
so a retained copy replayed days later would still claim the neighbour was heard seconds
ago. Consumers that want the current picture should subscribe and wait for the next
cycle, or read `timestamp` and correct for the age. No broker is required at all — the
database is a perfectly good consumer on its own.

### Triggering a cycle manually

The minimum interval is 12 hours, so use the DM command to test:

```
neighbors
```

It acknowledges immediately and reports the result in a second DM once the window
closes. Enabled via `[Neighbors_Command]`; add `neighbors` to
`[Admin_ACL] admin_commands` to restrict it, since a cycle spends airtime.

Two guards keep the airtime bounded. Both live in the service rather than in the
command, because what is being rationed belongs to the whole mesh and every
trigger reaches the same radio — the scheduler included:

- **Only one cycle at a time.** An overlapping cycle is refused whichever trigger
  asks, so two discover rounds can never collect into each other's window.
- **At most one cycle every 15 minutes.** Measured from the last cycle that
  reached the radio, including one that failed *after* the discover request went
  out, since a lost acknowledgement spends the airtime just the same. Users
  cannot take turns and keep the radio discovering continuously, and the
  scheduler's own retry-after-failure backoff waits this out rather than
  re-transmitting every five minutes. A cycle that bailed out *before*
  transmitting (radio down, unsupported build) does not start the clock, so
  re-checking those stays quick.

The DM command reports the wait instead of failing opaquely, and rewinds the
sender's personal cooldown to expire with the shared one — the command manager
records an execution before the command runs, so otherwise being told "wait one
more minute" would be followed by fourteen more minutes of personal cooldown.

### Region scopes are opt-in, and why

`neighbors_collect_scopes` additionally asks each neighbour for its region scopes.
It defaults to **false** for two reasons specific to running inside the bot:

1. **It stalls bot replies.** Every bot radio command is serialised through one lock
   (`modules/core.py` `_SerializedCommands`), and `req_regions_sync` waits for its
   reply *inside* the call — so one request holds the radio for up to ~25 s. With 32
   neighbours the bot's own messages stall in bursts for minutes.
2. **It mutates device contact state.** The zero-hop probe relies on the neighbour
   *not* being a known contact. The bot does track contacts, and for a repeater with
   no stored path the meshcore library reaches zero-hop by calling
   `change_contact_path()` and then `reset_path()` — temporarily rewriting that
   contact's path on the device. Those two calls are not paired by a
   `try`/`finally` upstream, and one error path returns between them, so a request
   cut short — or one whose path change was applied but not acknowledged — would
   leave the contact pinned to zero-hop and every later message to it sent
   direct-only. `modules/neighbors_discovery.py` restores the path itself in each
   of those cases, and warns if the device rejects the restore (which it reports
   as an error event rather than an exception), since that contact's routing is
   then wrong until something else fixes it.

With it off, the snapshot reports every neighbour it heard with empty `scopes` and
`status: responded`. Enable it on a bench radio first.

### Published payload

```json
{
  "timestamp": "2026-08-04T12:00:00.000000+00:00",
  "origin": "MeshCore-HOWL",
  "origin_id": "A1B2C3D4E5F67890...",
  "total_neighbors": 6,
  "queried_neighbors": 6,
  "truncated": false,
  "self": { "scopes": "" },
  "neighbors": [
    {
      "pubkey": "0011223344556677...",
      "snr": 9.75,
      "heard_secs_ago": 42,
      "scopes": "",
      "status": "responded"
    }
  ]
}
```

`total_neighbors` is how many were discovered, `queried_neighbors` how many were kept
after the `neighbors_max` cap, and `truncated` is true when either that cap or the
10 KB payload budget dropped entries. Entries are ordered most- to least-useful (most
recently heard, then stronger SNR). `status` is `responded`, `timeout`, or
`send_failed`.

Requires `meshcore >= 2.3.8` and a firmware build exposing `CMD_SEND_CONTROL_DATA`;
the service logs once and skips the feature if either is missing.

---

## FAQ

**Q: Do I need to provide a private key?**
A: Not if using on-device signing (`auth_token_method = device`). The service will fetch the key from your device automatically.

**Q: Can I publish to my own MQTT broker?**
A: Yes. Set `mqtt1_use_auth_token = false` and provide `mqtt1_username` and `mqtt1_password`.

**Q: What's the difference between TCP and WebSockets?**
A: WebSockets work through firewalls better (uses HTTPS port 443). TCP is lighter but may be blocked.

**Q: How do I disable packet capture but keep status publishing?**
A: You can't disable just packet capture - it's all or nothing. Consider filtering on the broker side.

**Q: Can I capture TX (outgoing) packets?**
A: Currently only RX (incoming) packets are captured.
