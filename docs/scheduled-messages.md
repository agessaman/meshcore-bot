# Scheduled messages (`[Scheduled_Messages]`)

Each entry is `<schedule_key> = <value>` where the value is normally **`channel:message`** (first colon separates channel from body). For **regional flood scope** on that send only, use **`channel:#scope:message`**: the middle segment must start with `#` (same convention as `flood_scopes` / `outgoing_flood_scope_override`). The message body may contain more colons. Omit the middle field for classic global flood. See `config.ini.example` under `[Scheduled_Messages]` for examples. The **`schedule`** command lists each job with `(#scope)` when set.

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

Schedule keys are parsed by **APScheduler** `CronTrigger.from_crontab` (plus `@` presets and deprecated `HHMM`). Field order is the usual five: `minute hour day-of-month month day-of-week`.

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

Two things to keep in mind. A month may have no fifth Tuesday, so `5th tue` simply skips those months. And `1st-tue,3rd-tue` is a 14/14/21-day cadence across a month boundary, not a strict fortnight: crontab matches calendar patterns and has no way to say "every 14 days". If you need an exact fortnight, note that anchoring one drifts an hour across a DST change, which is usually worse for an announced net time than the 21-day gap.

### Every other week

Plain crontab cannot say "every other Tuesday", and `1st-tue,3rd-tue` drifts to a three-week gap whenever a month has five Tuesdays. For a strict fortnight, add `week=` to the value with an ISO week-number expression:

```ini
[Scheduled_Messages]
0 20 * * tue = week=*/2 Public:#sea:Fortnightly net
0 20 * * tue = week=2-53/2 Public:#sea:The other fortnight
```

`week=*/2` runs in odd ISO weeks (1, 3, 5 …) and `week=2-53/2` in even ones; any APScheduler week expression works, such as `week=1-26` for the first half of the year. It goes with `start=` and `end=` ahead of the channel, in any order. The web viewer's **Weeks** setting offers both alternations.

ISO years have 52 or 53 weeks, so in a 53-week year (2026, 2032) the odd setting runs in week 53 and again in week 1, two weeks running at New Year. The even setting skips from week 52 to week 2, a three-week gap.
