# Repeater Prefix Collision Service

The **Repeater Prefix Collision Service** watches for newly discovered repeaters and posts an alert when the repeater’s prefix collides with an existing repeater prefix.

It listens for meshcore `NEW_CONTACT` events, then waits briefly for the bot’s normal contact persistence + geolocation to complete before querying the `complete_contact_tracking` table.

## Configuration

Add this section to your `config.ini` (or `local/config.ini`):

```ini
[RepeaterPrefixCollision_Service]
enabled = false
channels = #general
notify_on_prefix_bytes = 1
heard_window_days = 30
prefix_free_days = 30
post_process_delay_seconds = 0.5
post_process_timeout_seconds = 15.0
post_process_poll_interval_seconds = 0.2
include_prefix_free_hint = true
cooldown_minutes_per_prefix = 60
```

## Options

- **enabled**: Set `true` to start the service (default: `false`).
- **channels**: Comma-separated list of channels to post to. Example: `#general,#repeaters`.
- **channel**: Single-channel fallback (used only if `channels` is not set).
- **notify_on_prefix_bytes**: Which prefix match lengths trigger an alert. Supports `1`, `2`, `3`, or a comma-separated list like `1,2,3`.
  - 1 byte = 2 hex chars (`01`)
  - 2 bytes = 4 hex chars (`0101`)
  - 3 bytes = 6 hex chars (`010101`)
- **heard_window_days**: Only consider an existing prefix a “duplicate” if that repeater was heard within this many days.
- **prefix_free_days**: Window used to compute how many prefixes are “free” (unused) in the message. Set `0` to count all historical data.
- **post_process_delay_seconds / post_process_timeout_seconds**: Controls how long the service waits for DB + geolocation to be ready after the event.
- **post_process_poll_interval_seconds**: How often to poll the database while waiting for the row (default `0.2`).

### When an alert is allowed (strict gate)

Alerts run only when **all** of the following hold:

1. **`first_heard` is today** (local calendar date) on `complete_contact_tracking` — the bot first started tracking this `public_key` today.
2. **`unique_advert_packets` has exactly one row** for this `public_key` for today’s date. That table stores one row per distinct **`packet_hash`** per day (`UNIQUE(date, public_key, packet_hash)`), so a second distinct advert the same day (e.g. boot then manual re-advert, or two ingest paths) **suppresses** the alert. The design favors **missing a warning** over **duplicate-prefix noise**.

There is **no** config knob to loosen this rule.

**Troubleshooting:** If you expect an alert but get none, set log level to **DEBUG** and look for `RepeaterPrefixCollision:` lines. A common skip is `unique_advert_packets today=2`: the parsed ADVERT path and the `NEW_CONTACT` path often each record a **different** `packet_hash` in the same second, so the strict “exactly one hash today” rule does not run—even for a brand-new repeater.

- **include_prefix_free_hint**: When notifying on 1-byte collisions, appends: `Type 'prefix free' to find one.`
- **cooldown_minutes_per_prefix**: Cooldown to reduce repeat alerts for the same prefix.

## Message format

The service posts messages like:

`Heard new repeater {name} with prefix {prefix} near {location}. {prefixes_free} free prefixes remain. Type 'prefix free' to find one.`

