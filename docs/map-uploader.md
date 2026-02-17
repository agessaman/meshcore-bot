# Map Uploader Service

Uploads node advertisements to [map.meshcore.dev](https://map.meshcore.dev) for network visualization.

---

## Quick Start

1. **Configure Bot** - Edit `config.ini`:

```ini
[MapUploader]
enabled = true
```

2. **Restart Bot** - The service starts automatically and uploads node adverts with GPS coordinates

---

## Configuration

### Basic Settings

```ini
[MapUploader]
enabled = true                        # Enable map uploader
api_url = https://map.meshcore.dev/api/v1/uploader/node  # API endpoint
min_reupload_interval = 3600          # Minimum seconds between re-uploads (1 hour)
verbose = false                       # Detailed debug logging
```

### Private Key (Optional)

The service needs your device's private key to sign uploads. It will automatically fetch the key from your device if supported.

**Manual Configuration:**
```ini
private_key_path = /path/to/private_key.txt
```

---

## How It Works

1. **Listens** for ADVERT packets on the mesh network
2. **Verifies** packet signature using Ed25519
3. **Filters** invalid packets:
   - Missing GPS coordinates (lat/lon)
   - Coordinates exactly 0.0 (invalid)
   - CHAT adverts (only nodes are uploaded)
   - Duplicate/replay attacks
4. **Uploads** valid node adverts to the map with your radio parameters
5. **Prevents spam** - Only re-uploads the same node after `min_reupload_interval`

---

## What Gets Uploaded

### Node Types Uploaded
- Repeaters
- Room servers
- Sensors
- Other non-CHAT adverts with GPS coordinates

### Data Included
```json
{
  "params": {
    "freq": 915000000,
    "cr": 8,
    "sf": 9,
    "bw": 250000
  },
  "links": ["meshcore://DEADBEEF..."]
}
```

### What's NOT Uploaded
- CHAT adverts (companion devices)
- Nodes without GPS coordinates
- Nodes with invalid coordinates (0.0, 0.0)
- Duplicate packets (same node within `min_reupload_interval`)

---

## Signature Verification

All uploads are signed with your device's private key to ensure authenticity:

1. **Ed25519 signature** proves you received the packet
2. **Radio parameters** show your device config
3. **Raw packet data** allows independent verification

**Security:** Map operators can verify uploads came from legitimate devices.

---

## Troubleshooting

### Service Not Starting

Check logs:
```bash
tail -f meshcore_bot.log | grep MapUploader
```

Common issues:
- `enabled = false` in config
- Missing dependencies: `pip install aiohttp cryptography`
- No private key available

### No Uploads Happening

1. **Check for adverts** - Service only uploads when it receives ADVERT packets
2. **Verify coordinates** - Nodes must have valid GPS coordinates
3. **Check signature** - Service logs "signature verification failed" for invalid packets
4. **Check interval** - Same node won't re-upload within `min_reupload_interval`

### Private Key Errors

**Error:** "Could not obtain private key"

**Solutions:**
1. Ensure your device firmware supports private key export
2. Manually provide key via `private_key_path`
3. Check file permissions if using file path

---

## Advanced

### Memory Management

The service tracks seen adverts to prevent duplicates. Old entries are automatically cleaned:
- Hourly cleanup removes entries older than `2 × min_reupload_interval`
- Safety limit: Keeps only 5000 most recent entries if dictionary grows beyond 10,000

### Radio Parameters

Your device's radio settings are included in all uploads:
- **freq**: Frequency in Hz
- **cr**: Coding rate
- **sf**: Spreading factor
- **bw**: Bandwidth in Hz

### Packet Format

Uploads use the `meshcore://` URI scheme with hex-encoded raw packet data for verification.

---

## FAQ

**Q: Will this upload my location?**
A: No. Only node adverts (repeaters, sensors) with GPS coordinates are uploaded. CHAT adverts are never uploaded.

**Q: How often does it upload?**
A: Only when new node adverts are received. Same node won't re-upload within `min_reupload_interval` (default: 1 hour).

**Q: What if my device doesn't support private key export?**
A: You'll need to manually provide the private key via `private_key_path`.

**Q: Can I use a different map server?**
A: Yes, change `api_url` to point to your own map.meshcore.dev instance.

**Q: Does it upload all packets?**
A: No. Only ADVERT packets from nodes with valid GPS coordinates.
