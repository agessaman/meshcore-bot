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

# IATA code for topic routing
iata = LOC

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

### Topic Templates

Placeholders:
- `{IATA}` - Your IATA code (e.g., SEA)
- `{iata}` - Lowercase IATA code
- `{PUBLIC_KEY}` - Device public key (uppercase)
- `{public_key}` - Device public key (lowercase)

### Status Publishing

```ini
stats_in_status_enabled = true    # Include device stats in status
stats_refresh_interval = 300      # Publish status every 5 minutes
jwt_renewal_interval = 86400      # Renew JWT every 24 hours
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
  "client_version": "meshcore-bot/1.0.0",
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

### No Packets Being Published

1. **Verify MQTT connection** - Check logs for "Connected to MQTT broker"
2. **Check packet count** - Service logs "Captured packet #N" for each packet
3. **Verify topics** - Ensure topics match broker expectations

---

## Advanced

### Multiple Brokers

Configure up to 10 brokers (mqtt1_* through mqtt10_*). Each broker has independent connection tracking and auto-reconnection.

### Health Monitoring

```ini
health_check_interval = 30        # Check connection every 30s
health_check_grace_period = 2     # Allow 2 failures before warning
```

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
