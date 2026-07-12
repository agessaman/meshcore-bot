# Authenticated Time Sync

MeshCore-Bot can act as a source for authenticated repeater time synchronisation. This is source-side only: MeshCore repeater firmware performs verification and clock policy.

The feature is disabled by default and sends binary MeshCore group datagrams only. It does not send group text fallbacks.

## Configuration

Add or update `[Time_Sync]` in `config.ini`:

```ini
[Time_Sync]
enabled = true
channel = #time
display_name = TimeBot
sequence = 0
interval_seconds = 604800
```

`channel` must match a configured MeshCore group/public channel. `display_name` is exact and case-sensitive, must be 1..20 UTF-8 bytes, and must not contain carriage return or line feed.

Time sync signs with the existing MeshCore bot/radio identity. It does not use a separate time-sync private key. The default interval is `604800` seconds, which is one week.

To authorise the admin command, include `timesync` in `[Admin_ACL] admin_commands` and set `admin_pubkeys`.

## Public Key Export

Repeaters must be configured with the full 32-byte Ed25519 public key corresponding to the bot identity.

DM the bot as an admin:

```text
timesync publickey
```

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

For hashtag channels, the raw channel secret is the first 16 bytes of `SHA-256(lowercase "#channel")`. For the Public channel, MeshCore-Bot uses MeshCore's fixed public-channel secret. Do not use MeshCore's one-byte channel hash for signatures; that hash is only a transport lookup value.

## Sequence Persistence

The service starts from `bot_metadata` key `time_sync.sequence` when available, otherwise from `[Time_Sync] sequence`. After a send is accepted by the MeshCore send API, the sequence increments modulo 65536 and is persisted through the existing metadata store. It is also persisted during service shutdown.

If the bot restarts before a sequence write, firmware can still accept later timestamps. Equal-timestamp replays remain rejected by the firmware consumer.

## Security Model

The MeshCore group/public channel is transport only. The channel MAC proves knowledge of the channel secret, not sender identity.

The display name is a usability and filtering field only. Source authentication is the Ed25519 signature over the canonical bytes. Repeaters verify with one pinned full 32-byte public key.

Protect the MeshCore bot identity. Anyone with the bot identity private key can sign time announcements accepted by repeaters pinned to that public key.

## Deterministic Vector

For `#time`, display name `TimeBot`, timestamp `1783862400`, sequence `12345`, and the deterministic test key in `tests/test_time_sync.py`:

```text
channel secret = 5d13043d9a5e61bc61aeb63208f5c64e
channel ID     = e4235ac672871e72d2aeaf8654e0a39893de9fca7c06ccb5c3a788f95a6aeada
signature      = c9e0785bc99e910c813b8984df76c45cab7b1b6157594cb710b457eefedd0ede66f568cbdfc117fb57266283b9d5bbe3d4ac5d331bbd44b190c14e35bd712108
Tv1 payload    = 5476318094536a39300754696d65426f74c9e0785bc99e910c813b8984df76c45cab7b1b6157594cb710b457eefedd0ede66f568cbdfc117fb57266283b9d5bbe3d4ac5d331bbd44b190c14e35bd712108
group data     = 2101515476318094536a39300754696d65426f74c9e0785bc99e910c813b8984df76c45cab7b1b6157594cb710b457eefedd0ede66f568cbdfc117fb57266283b9d5bbe3d4ac5d331bbd44b190c14e35bd712108
```
