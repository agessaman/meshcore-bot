# Authenticated Time Sync

MeshCore-Bot can act as a source for authenticated repeater time synchronisation. This is source-side only: MeshCore repeater firmware performs verification and clock policy.

The feature is disabled by default and sends binary MeshCore group datagrams. It can optionally send a human-readable channel text companion for operator visibility, but repeater firmware uses only the signed binary datagram.

## Configuration

Add or update `[Time_Sync]` in `config.ini`:

```ini
[Time_Sync]
enabled = true
channel = #time
sequence = 0
interval_seconds = 604800
flood_scope = #west
full_flood_enabled = false
text_broadcast_enabled = false
text_channel =
```

`channel` must match a MeshCore group/public channel already configured on the radio. The bot does not create the channel at time-sync startup, because the signed datagram must still use the normal MeshCore channel encryption/MAC send path. For hashtag channels, include the leading `#`, for example `#time`; `time` without `#` is treated as a different custom/private channel name. For the default public channel, use `Public`.

The example default is `#time`, but that channel must still exist on the connected radio. If the startup log says `channel '#time' was not found in the MeshCore channel cache`, either add the `#time` channel to the radio or change `[Time_Sync] channel` to an existing configured channel.

The Tv1 display-name field is taken from the existing bot/radio identity name, not from `[Time_Sync]`. In normal deployments this is `[Bot] bot_name`, because MeshCore-Bot already synchronises that value to the radio device name at startup when `auto_update_device_name = true`. The repeater firmware must be configured with that exact identity name. It is case-sensitive, must be 1..20 UTF-8 bytes, and must not contain carriage return or line feed.

Time sync signs with the existing MeshCore bot/radio identity. It does not use a separate time-sync private key. The default interval is `604800` seconds, which is one week.

`flood_scope` is required by default and must be a regional MeshCore flood scope such as `#west`. The bot applies this scope to the signed binary datagram and to the optional text companion. This prevents time broadcasts from full-flooding the whole mesh unless that is explicitly requested.

Set `full_flood_enabled = true` only when this source should send global/full-flood time broadcasts. When enabled, the service ignores `flood_scope` and sends with the global `*` scope.

`text_broadcast_enabled` optionally sends a human-readable companion message after the binary datagram has been accepted by the MeshCore send API. This text message is not used by repeater firmware for time synchronisation, is not signed as a protocol payload, and should be treated as operator-visible context only. If `text_channel` is blank, the companion text is sent to the same channel as `channel`. If `text_channel` is set, it must also be a MeshCore group/public channel already known to the connected radio.

To authorise the admin command, include `timesync` in `[Admin_ACL] admin_commands` and set `admin_pubkeys`.

## Public Key Export

Repeaters must be configured with the full 32-byte Ed25519 public key corresponding to the bot identity.

DM the bot as an admin:

```text
timesync status
timesync send
timesync publickey
```

`timesync send` broadcasts one signed time-sync datagram immediately. The periodic sender still runs on `interval_seconds`.

The response is exactly 64 lowercase hexadecimal characters. Normal status output shows only a public-key fingerprint.

## Wire Format

The outer packet is `PAYLOAD_TYPE_GRP_DATA` and uses MeshCore's existing channel encryption/MAC path.

The group-datagram plaintext is:

```text
data_type: uint16 little-endian
data_len:  uint8
data:      data_len bytes
```

For time sync, `data_type = 0x0121`. The `data` field is the binary `Tv1` payload:

```text
offset  size  field
0       3     marker: "Tv1"
3       4     timestamp, uint32 little-endian Unix UTC seconds
7       2     sequence, uint16 little-endian
9       1     display_name_len
10      N     display_name bytes
10+N    64    raw Ed25519 signature
```

The signature is raw binary. It is not hex, Base64, JSON, or a C structure. The public key is not sent on the wire.

## Signed Bytes

The Ed25519 signature covers this exact UTF-8 byte string:

```text
MeshCore-Time-v1
<channel-id-hex>
<display-name>
<unix-seconds>
<sequence>
```

There is a final newline after the sequence line.

`channel-id-hex` is the lowercase SHA-256 digest of the raw 16-byte MeshCore channel secret:

```text
channel-id = SHA-256(raw 16-byte channel secret)
```

For hashtag channels, the configured channel name must include the leading `#`, and the raw channel secret is the first 16 bytes of `SHA-256(lowercase "#channel")`. For example, configure `channel = #time`, not `channel = time`, when the repeater is using the `#time` hashtag channel. For the Public channel, MeshCore-Bot uses MeshCore's fixed public-channel secret. Do not use MeshCore's one-byte channel hash for signatures; that hash is only a transport lookup value.

## Sequence Persistence

The service starts from `bot_metadata` key `time_sync.sequence` when available, otherwise from `[Time_Sync] sequence`. After a send is accepted by the MeshCore send API, the sequence increments modulo 65536 and is persisted through the existing metadata store. It is also persisted during service shutdown.

If the bot restarts before a sequence write, firmware can still accept later timestamps. Equal-timestamp replays remain rejected by the firmware consumer.

## Security Model

The MeshCore group/public channel is transport only. The channel MAC proves knowledge of the channel secret, not sender identity.

The display name is a usability and filtering field only. Source authentication is the Ed25519 signature over the canonical bytes. Repeaters verify with one pinned full 32-byte public key.

Protect the MeshCore bot identity. Anyone with the bot identity private key can sign time announcements accepted by repeaters pinned to that public key.

## Deterministic Vector

For `#time`, bot identity name `TimeBot`, timestamp `1783862400`, sequence `12345`, and the deterministic test key in `tests/test_time_sync.py`:

```text
channel secret = 5d13043d9a5e61bc61aeb63208f5c64e
channel ID     = e4235ac672871e72d2aeaf8654e0a39893de9fca7c06ccb5c3a788f95a6aeada
signature      = c9e0785bc99e910c813b8984df76c45cab7b1b6157594cb710b457eefedd0ede66f568cbdfc117fb57266283b9d5bbe3d4ac5d331bbd44b190c14e35bd712108
Tv1 payload    = 5476318094536a39300754696d65426f74c9e0785bc99e910c813b8984df76c45cab7b1b6157594cb710b457eefedd0ede66f568cbdfc117fb57266283b9d5bbe3d4ac5d331bbd44b190c14e35bd712108
group data     = 2101515476318094536a39300754696d65426f74c9e0785bc99e910c813b8984df76c45cab7b1b6157594cb710b457eefedd0ede66f568cbdfc117fb57266283b9d5bbe3d4ac5d331bbd44b190c14e35bd712108
```
