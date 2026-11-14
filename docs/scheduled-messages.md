# Scheduled messages (`[Scheduled_Messages]`)

Each line of `[Scheduled_Messages]` posts one message to a channel on a schedule. The format is summarized in the reference below; the sections after it explain each part with examples. The **`schedule`** command lists every job, with `(#scope)` when one is set.

## Format reference

```ini
<schedule key> = [start=YYYY-MM-DD] [end=YYYY-MM-DD] [week=<weeks>] <channel>:[#<scope>:]<message>
```

```ini
[Scheduled_Messages]
0 8 * * *         = Public:Good morning
30 12 * * mon-fri = Public:#sea:Lunch net in 30 minutes
0 19 last-fri * * = start=2027-01-01 end=2027-03-31 Public:Winter net starts now
0 20 * * tue      = week=*/2 Public:#sea:Fortnightly net
```

Times are in `[Bot] timezone`. Each schedule key can appear only once in the section.

### Schedule key

A key is one of:

| Form | Example | Meaning |
| --- | --- | --- |
| Five crontab fields | `30 12 * * mon` | `minute hour day-of-month month day-of-week` |
| Preset | `@daily` | `@yearly`/`@annually` `0 0 1 1 *`, `@monthly` `0 0 1 * *`, `@weekly` `0 0 * * 0` (Monday), `@daily`/`@midnight` `0 0 * * *`, `@hourly` `0 * * * *` |
| `HHMM` (deprecated) | `0800` | Every day at 08:00. Still works, logs a warning; write `0 8 * * *` instead. |

The five fields:

| Field | Values | Names | Also |
| --- | --- | --- | --- |
| minute | `0`–`59` | | |
| hour | `0`–`23` | | |
| day-of-month | `1`–`31` | | `last` (last day of the month); `1st`…`5th` or `last`, a `-` or `_`, and a weekday: `last-fri`, `2nd-wed` |
| month | `1`–`12` | `jan`–`dec` | |
| day-of-week | `0`–`6`, **`0` is Monday** | `mon`–`sun` | |

Every field takes `*` (any), a value (`5`), a range (`1-5`, `mon-fri`), a step (`*/15`, `0-30/10`) and a comma list of any of these (`0,30`, `1st-tue,3rd-tue`). Names and positional days are case-insensitive.

A positional day (`last-fri`, `4th-tue`) already names its weekday, so day-of-week must be `*` when you use one. It must stand alone between commas, and it is only valid in the day-of-month field. A month without a fifth Tuesday skips `5th-tue`.

### Value

| Part | Required | Format | Meaning |
| --- | --- | --- | --- |
| `start=` | no | `YYYY-MM-DD` | First day the schedule may run |
| `end=` | no | `YYYY-MM-DD` | Last day it may run, through the end of that day |
| `week=` | no | ISO week numbers `1`–`53`, with the field syntax above | `*/2` odd weeks, `2-53/2` even weeks, `1-26` first half of the year |
| channel | yes | channel name, no `:` | Where the message goes |
| `#scope` | no | `#name`, no `:` | Regional flood scope for this send only; leave it out for the channel's usual scope |
| message | yes | text, may contain `:` | What is sent |

The three options come first, in any order, each followed by a space, and each at most once. The option names are case-insensitive. The first `:` ends the channel. When the next segment starts with `#` and is followed by another `:`, it is the scope; everything after is the message.

### Message text

- `\n` is a line break, `\t` a tab, `\r` a carriage return, and `\\` a literal backslash (`\\n` sends a backslash and an `n`).
- `{cmd:<command> [args]}` inserts a command's reply. See [Broadcasting a command's output](#broadcasting-a-commands-output-cmd) for its limits, including the 15-minute minimum between runs.
- Mesh counts can be inserted by name:
    - Ever heard: `{total_contacts}`, `{total_repeaters}`, `{total_companions}`, `{total_roomservers}`, `{total_sensors}`
    - Heard in the last 30 days: `{total_contacts_30d}`, `{total_repeaters_30d}`, `{total_companions_30d}`, `{total_roomservers_30d}`, `{total_sensors_30d}`
    - First heard in the last 7 days: `{new_companions_7d}`, `{new_repeaters_7d}`, `{new_roomservers_7d}`, `{new_sensors_7d}`
    - Unique users active in the last 24 hours: `{recent_activity_24h}`
    - Old names: `{repeaters}` and `{companions}`, the same as `{total_repeaters}` and `{total_companions}`
- A message longer than one channel message is split into parts and sent as several messages.

### What is rejected

The bot logs a warning at startup and does not schedule an entry it can't run as written. The web viewer refuses the same entries when you save, with the reason.

| Written | Problem | Write instead |
| --- | --- | --- |
| `0 12 * * 7` | Day-of-week stops at `6` (Sunday) | `0 12 * * sun` |
| `0 8 * *` | Four fields | Five fields: `0 8 * * *` |
| `0 19 lastfri * *`, `0 19 last--fri * *` | Not a day-of-month | `0 19 last-fri * *` |
| `0 19 1st-tue-3rd-tue * *` | Two positional days joined | `0 19 1st-tue,3rd-tue * *` |
| `0 19 last-fri/2 * *` | Positional days take no step | `week=*/2` on the value |
| `0 19 1st-tue * mon` | Positional day with a day-of-week (would never fire) | `0 19 1st-tue * *` |
| `0 19 * * last-fri` | Positional day in the day-of-week field | `0 19 last-fri * *` |
| `start=20270101` | Date not written `YYYY-MM-DD` | `start=2027-01-01` |
| `start=2027-02-01 end=2027-01-01` | End before start | Swap the dates |
| `start=…` given twice | Option repeated | Give it once |
| `start=2027-01-01` alone | No channel or message after the options | `start=2027-01-01 Public:Hello` |
| `week=60` | ISO weeks stop at `53` | `week=1-53/2` or similar |
| A schedule whose runs are all past | Nothing left to run | Move or remove `end=` |
| `{cmd:...}` in a schedule that runs more often than every 15 minutes | Airtime guard | Run it every 15 minutes or less often |

Two entries only the web viewer can catch, since by hand they are valid but mean something else:

- A message with no scope that starts with `#name:`, such as `Public:#net: tonight at 7`, is read as the scope `#net` with the message `tonight at 7`. Set a scope, or reword the start.
- A channel whose name starts with `start=`, `end=` or `week=` followed by a space is read as an option.

## Managing schedules from the web viewer

The **Schedule** page in the web viewer edits this section for you. It writes `config.ini` and queues a config reload, so new and changed schedules take effect **without restarting the bot**.

It is an editor for the same `[Scheduled_Messages]` section, not a separate store, so entries added by hand and entries added in the UI are the same thing and the `schedule` command lists both.

The editor builds the cron key from plain-language options (every day at a time, several times a day, certain days of the week, one day a month such as the 2nd Wednesday or the last day, every N hours or minutes) and shows the resolved expression plus the next five run times before you save. "Advanced (cron)" accepts a raw expression. An existing entry opens in the plain-language option that reproduces it exactly, or in Advanced when none does, so a stored expression is never silently rewritten. Start and end dates and the every-other-week setting sit below the schedule.

Schedules that the bot cannot run are listed rather than hidden, marked **Not scheduled** with the reason, so a typo that stops a message from airing is visible instead of silent. The 15-minute floor for `{cmd:...}` messages is enforced in the editor too, so it is refused when you save rather than dropped later at startup.

## Broadcasting a command's output (`{cmd:...}`)

A scheduled message can embed the reply of any bot command with **`{cmd:<command> [args]}`**. The command runs for its text only — it transmits nothing itself — and the scheduled message carries the result. This is how you put a recurring forecast (or any other command) on the air without that command needing its own scheduling settings:

```ini
[Scheduled_Messages]
0 6,12,18 * * * = Public:{cmd:wx Seattle}
*/30 * * * *    = Public:Conditions now: {cmd:wx 98101}
@hourly         = Public:{cmd:aqi Tacoma}
```

The trigger is matched against command **names and their keywords**, so `{cmd:weather Seattle}` and `{cmd:wx Seattle}` are the same command. Arguments are passed through exactly as a user would type them.

A placeholder expands to **nothing** (and logs a warning) when the command is unknown, disabled in config, admin-only, times out, or returns no output — the literal `{cmd:...}` is never transmitted. If a message is empty after expansion, nothing is sent at all. Command output is not re-scanned, so a reply that happens to contain `{cmd:...}` cannot recurse.

### Airtime guards

Every firing is a transmission on a shared medium, and a command placeholder makes it easy to write a cron that airs several times an hour. Two guards apply, and neither is configurable:

- **A 15-minute floor.** A schedule containing `{cmd:...}` may not fire more often than every 15 minutes. An entry that does is **rejected at startup** with an error and is not scheduled at all — it does not silently run at a slower rate. The check measures the *tightest* gap between firings, so `0,1 * * * *` is treated as a 60-second schedule rather than an hourly one. Schedules with no command placeholder are unaffected.
- **The command's own cooldown still applies.** `[<Name>_Command] cooldown_seconds` is not bypassed by scheduling. If the command is on cooldown when the schedule fires, the placeholder expands to nothing for that round and logs a warning.

Other limits worth knowing:

- **Only commands marked `render_safe` can be rendered.** Capture intercepts `send_response` and `send_response_chunked`, so a command that transmits by other means (`advert`), posts its own messages (`announcements`), or is DM-only (`schedule`, which would broadcast configuration) is refused. This is opt-in rather than a denylist, so a new command is never renderable by accident. The read-only informational commands (`wx`, `gwx`, `aqi`, `sun`, `moon`, `solar`, `hfcond`, `satpass`, `rain`, `stats`, and similar) are marked safe.
- `[Bot] scheduled_command_timeout_seconds` (default `30`) bounds each render. Network-backed commands like `wx` need the headroom.

A rendered reply longer than one message is split to the RF body budget and sent as several messages rather than failing at the device.

Even within the floor, mind the cost: a `*/15 * * * *` forecast is 96 transmissions a day.

## Schedule keys (APScheduler cron, not Vixie)

Schedule keys become **APScheduler** cron triggers, built the way `CronTrigger.from_crontab` builds them, with positional days of the month, `@` presets and the deprecated `HHMM` form added on top. Field order is the usual five: `minute hour day-of-month month day-of-week`.

**Day-of-week numbering differs from classic Vixie / crontab(5):**

| | APScheduler (this bot) | Vixie cron |
| --- | --- | --- |
| `0` | Monday | Sunday |
| `1` … `6` | Tuesday … Sunday | Monday … Saturday |
| `7` | Invalid | Often accepted as Sunday |

Prefer **`mon`–`sun`** names in the DOW field so expressions stay unambiguous. Example: Monday 12:30 is `30 12 * * mon` or `30 12 * * 0` — **not** Vixie’s `30 12 * * 1` (that is Tuesday here).

Months take three-letter names too (`0 9 1 jan,jul *`, `0 19 last-fri may-sep *`), and `last` alone in the day-of-month field is the last day of the month.

Preset aliases expand to those same APScheduler forms. In particular **`@weekly`** is Monday 00:00 (`0 0 * * 0`), not Sunday midnight as on many Unix crons.

### Limiting a schedule to a date range

Crontab has no field for a date range, so `start=` / `end=` bounds live on the **value**, ahead of the channel:

```ini
[Scheduled_Messages]
0 19 last-fri * * = start=2027-01-01 end=2027-03-31 Public:Winter net starts now
0 8 * * *         = end=2026-12-25 Public:#sea:Countdown to the holidays
```

Either bound may be omitted and they may appear in either order. `=` keeps them clear of the `:` that separates channel from message, and because they are anchored to the front of the value a message body mentioning `start=` is never mistaken for one.

The end date is **inclusive** — `end=2027-03-31` runs through the whole of the 31st, not up to its midnight. Once a bounded schedule has no runs left the bot skips it at startup with a log line saying so, and the web viewer shows it as **Finished** rather than hiding it.

### Nth and last weekday of the month

APScheduler's day-of-month field understands positional expressions like `last fri` and `4th tue`, which plain crontab cannot express. They contain a space, so write it as `-` (or `_`) to keep the five fields intact:

| Key | Fires |
| --- | --- |
| `0 19 last-fri * *` | 19:00 on the last Friday of every month |
| `0 19 4th-tue * *` | 19:00 on the fourth Tuesday |
| `0 19 1st-tue,3rd-tue * *` | 19:00 on the first and third Tuesday |
| `0 19 last * *` | 19:00 on the last day of the month (no weekday needed) |

Prefixes are `1st` `2nd` `3rd` `4th` `5th` `last`, followed by `mon`–`sun`. They are valid **only in the day-of-month field** — `0 19 * * last-fri` is rejected.

Two things to keep in mind. A month may have no fifth Tuesday, so `5th tue` simply skips those months. And `1st-tue,3rd-tue` is a 14/14/21-day cadence across a month boundary, not a strict fortnight: crontab matches calendar patterns and has no way to say "every 14 days". For a strict fortnight, use `week=` (next section).

### Every other week

Plain crontab cannot say "every other Tuesday", and `1st-tue,3rd-tue` drifts to a three-week gap whenever a month has five Tuesdays. For a strict fortnight, add `week=` to the value with an ISO week-number expression:

```ini
[Scheduled_Messages]
0 20 * * tue = week=*/2 Public:#sea:Fortnightly net
```

`week=*/2` runs in odd ISO weeks (1, 3, 5 …) and `week=2-53/2` in even ones (a schedule key can appear only once in the section, so the two alternations need different keys); any APScheduler week expression works, such as `week=1-26` for the first half of the year. It goes with `start=` and `end=` ahead of the channel, in any order. The web viewer's **Weeks** setting offers both alternations.

ISO years have 52 or 53 weeks, so in a 53-week year (2026, 2032) the odd setting runs in week 53 and again in week 1, two weeks running at New Year. The even setting skips from week 52 to week 2, a three-week gap.
