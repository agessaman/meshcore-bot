# Region warnings

MeshCore puts a channel message on the air in one of two ways.

An ordinary `FLOOD` is rebroadcast by every repeater that hears it, anywhere on the mesh. A `TC_FLOOD` carries a transport code derived from a region key — what the apps call a **region code** — and only repeaters holding that key pass it on.

A client with no region configured therefore floods the entire mesh with every message it sends. On a busy mesh that is most of the airtime nobody asked for.

This feature does two separate things:

- **Counts** how many of the channel messages your bot hears were unscoped, per channel, per day. Free, on by default, transmits nothing.
- **Warns** the senders, when you explicitly turn that on. Spends airtime automatically, so it is off by default and starts in dry run.

The Web Viewer page at **Settings → Region Warnings** shows both.

## How a message is classified

Each channel message gets one of three verdicts.

| Verdict | Meaning | Evidence |
| --- | --- | --- |
| `scoped` | A region code was set | The message matched a configured `flood_scopes` entry, or the correlated RF packet was a `TC_FLOOD` carrying a transport code |
| `global` | No region code | The correlated RF packet was an ordinary `FLOOD`, or no scope-eligible packet was heard anywhere in the correlation window |
| `unknown` | The radio could not tell | Anything else |

Only `global` can earn a warning, and only on positive evidence. If the radio did not witness enough to decide, the message counts as `unknown` and no warning is possible. That matters: absence of correlation is not proof that a sender omitted a region, and this feature answers that ambiguity by staying quiet.

`unknown` is excluded from the unscoped percentage rather than counted as clean, so a mesh the bot cannot classify reads as "no data" instead of "no problem".

A channel message that arrives with no `Name: ` prefix has no attributable sender. It is still counted, but it can never earn anyone a warning — every such message would otherwise share one synthetic identity.

Messages the radio cached from before the current connection are skipped entirely — a reconnect replays them as a burst, and counting them would both distort the tallies and let stale traffic earn someone a warning.

## Reading the page

**Channel traffic by scope** breaks the window down per channel. Each bar is unscoped / scoped / couldn't-tell, and the numbers are printed beside it.

**Daily volume** is the same data per day, plus the headline share.

A channel sitting at a high unscoped percentage is where a warning would do the most good. A channel dominated by "couldn't tell" means your radio is not hearing enough RF detail to judge, and warnings there will rarely fire.

## Turning warnings on

The **What the bot does** control has three positions:

- **Count only** — tallies for the page, nothing transmitted. The default.
- **Dry run** — warnings are decided and logged exactly as they would be sent, including consuming the cooldowns and the daily cap, but nothing is transmitted. The log underneath shows precisely what going live would put on the air.
- **Send warnings** — transmits.

Run it in dry run for a few days first. Because dry run spends the same budget, the log is a true preview rather than an upper bound.

### Delivery

- **Direct message** (default) — the sender alone sees it, and it is the cheaper of the two.
- **Channel reply** — sent at **global** scope on purpose. The recipient is by definition outside any region your bot replies under, so a scoped reply would never reach them. Everyone on the channel sees it.

### Limits

| Setting | Default | What it does |
| --- | --- | --- |
| `min_unscoped_messages` | 3 | Confirmed-unscoped messages from one sender before they earn a warning. The run resets after 24h of quiet, when the sender posts a scoped message, and on restart. |
| `per_sender_cooldown_hours` | 168 | Do not warn the same sender again within this many hours. 0 disables. |
| `mesh_cooldown_minutes` | 30 | Minimum gap between warnings to anyone. 0 disables. |
| `max_warnings_per_day` | 6 | Hard ceiling per local day. 0 means unlimited. |

Banned users are never warned, and the bot never warns itself (identified by public key, falling back to `[Bot] bot_name`). `channelpause` silences warnings along with everything else on channels.

The daily cap counts **attempts**, failures included: its job is to bound how much unprompted activity this feature can produce in a day, and a send that reported failure may still have put something on the air before it did.

A failed send does not start the mesh cooldown — usually nothing was transmitted, so it should not silence the next sender — but it does spend that sender's run, so a permanently unreachable contact is not retried on their every message.

Both cooldowns and the cap read from the database rather than from memory, so restarting the bot does not release a burst of warnings.

### Message

`{sender}` and `{channel}` are substituted. Keep it short: a DM body is 158 UTF-8 bytes and a channel reply is smaller still (160 minus your bot's name), and anything longer is truncated. The page shows the byte count live against whichever limit applies.

For channel delivery, include `@[{sender}]` so the person you are addressing sees it.

## Configuration

Everything the page writes lives in `[Region_Warnings]`:

```ini
[Region_Warnings]
track_traffic = true
enabled = false
dry_run = true
delivery = dm
# channels = general, public
# message = Heads up: your messages have no region code, ...
min_unscoped_messages = 3
per_sender_cooldown_hours = 168
mesh_cooldown_minutes = 30
max_warnings_per_day = 6
```

`channels` is an allowlist; empty means every channel the bot hears. The leading `#` is optional and case does not matter.

Saving from the Web Viewer queues a hot config reload, so changes take effect without a restart.

## Storage and retention

Two small tables:

- `region_scope_daily` — one row per channel per local date holding the three counts.
- `region_warning_events` — one row per warning decision that reached the send stage (sent, failed, or withheld by dry run). Suppressions by cooldown or cap are deliberately not rows; they are the common case and would bury the log.

Both are pruned by `[Data_Retention] region_warning_retention_days` (default 90).

Set `track_traffic = false` to stop writing tallies. Warnings still work; you just lose the evidence the page is built on.

## Relationship to `flood_scopes`

`[Channels] flood_scopes` decides which messages the bot will *reply* to. Region warnings observe every channel message regardless, and the observation runs before that allowlist — an unscoped message is exactly what a scoped allowlist drops, so measuring after the gate would blind the monitor to the traffic it exists to measure.

If you have `flood_scopes` configured with region names and no `*`, unscoped messages get no command replies at all. Region warnings still see them, and can still tell the sender why the bot is ignoring them.
