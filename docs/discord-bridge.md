# Discord Bridge Service

The Discord Bridge service posts MeshCore channel messages to Discord channels via webhooks. This is a **one-way, read-only bridge** - messages only flow from MeshCore to Discord.

**Features:**
- One-way message flow (MeshCore → Discord only)
- Multi-channel mapping (map multiple MeshCore channels to different Discord channels)
- Webhook-based (simple, secure, no bot permissions needed)
- **DMs are NEVER bridged** (hardcoded for privacy)
- Rate limit monitoring (warns when approaching Discord's limits)
- Disabled by default (opt-in)

---

## Quick Start

### 1. Create Discord Webhook

1. Open Discord and navigate to your channel
2. Click **⚙️ Channel Settings** → **Integrations** → **Webhooks**
3. Click **Create Webhook**
4. Copy the webhook URL

### 2. Configure Bot

Edit `config.ini`:

```ini
[DiscordBridge]
enabled = true

# Map MeshCore channels to Discord webhooks
bridge.general = https://discord.com/api/webhooks/YOUR_WEBHOOK_URL_HERE
```

### 3. Restart Bot

```bash
sudo systemctl restart meshcore-bot
# OR if running manually: python3 meshcore_bot.py
```

### 4. Test

Send a message on the MeshCore channel - it should appear in Discord!

---

## Configuration

### Basic Setup

```ini
[DiscordBridge]
enabled = true

# Avatar style (optional)
# Options: color (default), fun-emoji, avataaars, bottts, identicon, pixel-art, adventurer, initials
avatar_style = color

# Map MeshCore channels to Discord webhooks
bridge.general = https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz1234567890ABCD
bridge.emergency = https://discord.com/api/webhooks/987654321098765432/ZYXWVUTSRQPONMLKJIHGFEDCBAzyxwvutsrqponmlkjihgf
```

**Important:** Only channels explicitly listed will be bridged. Any channel not in the config will be ignored.

### Avatar Styles

Control how user avatars appear in Discord:

- **color** (default): Discord's built-in colored avatars based on username hash (no external API)
- **fun-emoji**: Colorful emoji-based avatars
- **avataaars**: Cartoon-style avatar faces
- **bottts**: Robot-themed avatars
- **identicon**: Geometric patterns (GitHub-style)
- **pixel-art**: Retro pixel-art style
- **adventurer**: Adventure-themed characters
- **initials**: User's initials on colored background

Preview: `https://api.dicebear.com/7.x/{style}/png?seed=YourName`

---

## Security & Privacy

### Webhook URLs Contain Secrets

⚠️ **IMPORTANT**: Webhook URLs contain authentication tokens. Anyone with a webhook URL can post to your Discord channel.

- **Never commit webhook URLs to version control**
- Keep `config.ini` secure and private
- Add `config.ini` to `.gitignore` (already done)
- Rotate webhook URLs immediately if exposed

### DMs Are Never Bridged

For privacy, **DMs are NEVER bridged to Discord**. This is hardcoded and cannot be changed via configuration. Only channel messages from explicitly configured channels are posted to Discord.

---

## Rate Limits

Discord webhooks are limited to **30 messages per minute per webhook**. The service monitors rate limit headers and logs warnings when within 20% of exhaustion:

```
WARNING - Discord rate limit warning [general]: 6/30 requests remaining (20.0%). Resets at: 2026-01-03 21:15:00
```

If you have high-traffic channels:
- Use a dedicated webhook for that channel
- Filter messages at the source
- Increase `bot_tx_rate_limit_seconds` in `[Bot]` section

---

## Message Format

Messages appear in Discord with the sender's name as the webhook username:

```
[Avatar] Jade
seriously, there are some people...
```

MeshCore @ mentions are cleaned up and bolded: `@[username]` → `**@username**`

---

## Troubleshooting

### Service Not Starting

Check logs:
```bash
tail -f meshcore_bot.log | grep DiscordBridge
```

Common issues:
- `enabled = false` in config
- No channel mappings configured
- Invalid webhook URLs

### Messages Not Appearing in Discord

1. **Verify channel is configured**: Check that `bridge.<channelname>` exists in config
2. **Check webhook URL**: Ensure URL is correct and webhook exists in Discord
3. **Test webhook manually**:
   ```bash
   curl -X POST "YOUR_WEBHOOK_URL" \
     -H "Content-Type: application/json" \
     -d '{"content": "Test message"}'
   ```
4. **Check logs**: Look for errors in `meshcore_bot.log`

### Rate Limit Warnings

If you see frequent rate limit warnings:
1. Check message volume - is the channel very active?
2. Adjust bot rate limiting: increase `bot_tx_rate_limit_seconds`
3. Filter messages: only bridge important channels
4. Use multiple webhooks: each has its own rate limit

---

## FAQ

**Q: Can I bridge messages from Discord to MeshCore?**
A: No. This is intentionally a one-way bridge for security and simplicity.

**Q: Can I bridge DMs?**
A: No. DMs are never bridged for privacy. This is hardcoded and cannot be changed.

**Q: Can I use a Discord bot instead of webhooks?**
A: The service uses webhooks by design. Webhooks are simpler, more secure for one-way messaging, and don't require bot permissions.

**Q: What happens if a webhook is deleted in Discord?**
A: The service will log errors when trying to post. Remove the mapping from config or create a new webhook.

**Q: Can I use the same webhook for multiple MeshCore channels?**
A: Yes, but all messages will appear in the same Discord channel. The webhook username will show which MeshCore channel it came from.

**Q: How do I disable the bridge temporarily?**
A: Set `enabled = false` in the `[DiscordBridge]` section and restart the bot.

**Q: Does this work with Discord threads?**
A: Webhooks post to the channel, not specific threads. Consider creating separate channels for different topics.

---

## Implementation Details

**Architecture:**
- Inherits from `BaseServicePlugin`
- Event-driven: subscribes to `EventType.CHANNEL_MSG_RECV`
- Async HTTP via `aiohttp` (fallback to `requests`)
- Rate limit monitoring via `X-RateLimit-Remaining` headers

**File Locations:**
- Service: `modules/service_plugins/discord_bridge_service.py`
- Config: `config.ini` (section `[DiscordBridge]`)
- Example: `config.ini.example`
- Tests: `test_scripts/test_discord_bridge_*.py`

**Dependencies:** Uses existing `aiohttp` and `requests` libraries - no additional dependencies needed!
