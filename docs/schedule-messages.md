# Scheduled Message Time Specifications

This document describes how to specify when scheduled messages should be sent, both through the web viewer UI and directly in the `config.ini` file.

## Overview

The scheduled message subsystem uses APScheduler (a Python scheduling library) to maintain schedules and trigger messages at the specified times. Messages are sent to a designated channel at the configured frequency.

You can specify schedules in three ways:
1. **Simplified modes** (web UI only) — templates for common patterns like daily, weekly, or interval-based schedules
2. **Standard cron** — the traditional 5-field cron syntax used in Unix/Linux systems
3. **Flexible cron** — a more human-readable format that allows fields in any order with intuitive suffixes

All three approaches ultimately create the same underlying schedule representation, but offer different levels of convenience and expressiveness.

---

## Simplified Schedule Modes (Web UI)

The web viewer provides pre-built templates for common scheduling patterns. When you select one of these modes, the UI builds the appropriate cron expression for you.

### Daily at Specific Time

Send a message every day at a specific time.

**Example:** Daily at 8:00 AM
- **Stored as:** `0 8 * * *`
- **When it fires:** Every day at 8:00 AM

### Weekly on Specific Days

Send a message on specific days of the week at a chosen time.

**Example:** Every Monday, Wednesday, and Friday at 7:00 PM
- **Stored as:** `0 19 * * 1,3,5`
- **When it fires:** 7:00 PM on Monday, Wednesday, and Friday

**Day numbering:** In standard cron format, days are numbered 0=Monday through 6=Sunday. The web UI handles this for you.

### Every N Hours

Send a message at regular hourly intervals.

**Example:** Every 6 hours
- **Stored as:** `0 */6 * * *`
- **When it fires:** Midnight, 6 AM, noon, 6 PM

### Every N Minutes

Send a message at regular minute intervals.

**Example:** Every 30 minutes
- **Stored as:** `*/30 * * * *`
- **When it fires:** On the hour and half-hour (e.g., 8:00, 8:30, 9:00, 9:30...)

---

## Standard Cron Format

Standard cron expressions use five fields separated by spaces:

```
minute hour day-of-month month day-of-week
```

### Field Definitions

| Field         | Valid Values        | Examples                  |
|---------------|---------------------|---------------------------|
| minute        | 0–59                | `0`, `15`, `30`, `*/5`    |
| hour          | 0–23                | `8`, `14`, `20`, `*/2`    |
| day-of-month  | 1–31                | `1`, `15`, `*/2`          |
| month         | 1–12 or jan–dec     | `jan`, `mar-jul`, `6` (discouraged) |
| day-of-week   | 0–6 or mon–sun      | `mon`, `wed,fri`, `0` (discouraged) |

**Important:**
- Day-of-week numbering is 0=Monday through 6=Sunday (APScheduler convention), not the traditional Unix cron 0=Sunday.
- **Use three-letter month and day names** (`jan`, `mon`) instead of numbers to avoid confusion and improve readability.

### Special Characters

- `*` — matches any value (e.g., `*` in the month field means "every month")
- `,` — list separator (e.g., `1,15` means "on the 1st and 15th")
- `-` — range (e.g., `mon-fri` means "Monday through Friday")
- `/` — step values (e.g., `*/15` means "every 15 units")

### Preset Aliases

Instead of five fields, you can use a preset alias:

| Preset       | Equivalent Cron | Description                |
|--------------|-----------------|----------------------------|
| `@yearly`    | `0 0 1 1 *`     | Once a year (Jan 1, midnight) |
| `@annually`  | `0 0 1 1 *`     | Same as @yearly            |
| `@monthly`   | `0 0 1 * *`     | First day of each month    |
| `@weekly`    | `0 0 * * 0`     | Every Monday at midnight   |
| `@daily`     | `0 0 * * *`     | Every day at midnight      |
| `@midnight`  | `0 0 * * *`     | Same as @daily             |
| `@hourly`    | `0 * * * *`     | Start of every hour        |

### Examples

**Every day at 9:30 AM:**
```
30 9 * * *
```

**Every weekday (Monday–Friday) at 6:00 PM:**
```
0 18 * * mon-fri
```

**First day of every month at noon:**
```
0 12 1 * *
```

**Every 15 minutes:**
```
*/15 * * * *
```

**Twice a day (6 AM and 6 PM):**
```
0 6,18 * * *
```

**Every Sunday at 8:00 AM during summer months (June–August):**
```
0 8 * jun-aug sun
```

---

## Flexible Cron Format

Flexible cron is a more readable alternative to standard cron that allows you to specify scheduling fields in any order using intuitive suffixes. This format is particularly useful when editing schedules directly in `config.ini` or when using the "Flexible (cron)" mode in the web UI.

**Key advantage:** Flexible cron can express schedules that are impossible with standard cron, such as "the 4th Tuesday of each month" or "the last Friday of the month."

### Key Features

- **Any order:** Fields can appear in any sequence
- **Optional fields:** Only specify what you need; omitted fields default to `*` (any value)
- **Case-insensitive:** `MON`, `Mon`, and `mon` are all valid
- **Readable time:** Use `HH:MM` format instead of separate hour/minute fields
- **Ordinal day patterns:** Specify "2nd Tuesday" or "last Friday" — patterns that require complex workarounds in standard cron

### Field Syntax

| Component       | Suffix/Format | Examples                    |
|-----------------|---------------|-----------------------------|
| Time (combined) | `HH:MM` or `HHMM` | `14:30`, `0815`        |
| Hour            | `h`           | `9h`, `14h`, `*/6h`         |
| Minute          | `m`           | `30m`, `*/15m`              |
| Day of month    | `d`           | `15d`, `1,15d`, `*/7d`      |
| Month           | (bare name)   | `jan`, `feb-may`, `jun,jul,aug` |
| Day of week     | (bare name)   | `mon`, `tue,thu`, `mon-fri` |
| ISO week        | `w`           | `1w`, `3w`, `1-52w`         |
| Ordinal + day   | `1st`–`5th`, `last` | `1st mon`, `last fri`, `4th tue` |
| Start date      | `start:YYYY-MM-DD` | `start:2026-06-01`    |
| End date        | `end:YYYY-MM-DD`   | `end:2026-12-31`      |

**Note:** Use three-letter month and day-of-week names (`jan`, `mon`) for clarity. While numeric values are technically supported, names are strongly preferred to avoid confusion.

### Time Specification

You can specify time in several ways:

**Combined HH:MM format:**
```
14:30        → 2:30 PM
08:15        → 8:15 AM
0815         → 8:15 AM (colon optional)
```

**Separate hour and minute:**
```
9h 30m       → 9:30 AM
14h 0m       → 2:00 PM
```

**Hour or minute alone:**
```
9h           → Every day at 9:00 AM (minute defaults to 0)
30m          → 30 minutes past every hour
```

### Day of Week with Ordinal

You can specify "the Nth [day] of the month" — a powerful feature that standard cron cannot express directly:

```
1st mon      → First Monday of each month
2nd tue      → Second Tuesday
4th thu      → Fourth Thursday
last fri     → Last Friday of the month
```

**Using "last" alone:**
```
last         → Last day of the month (28th, 29th, 30th, or 31st depending on the month)
```

This is particularly useful for end-of-month schedules without worrying about month length.

**Examples:**
- `last 15:00` → 3:00 PM on the last day of every month
- `last jan,mar,may` → Last day of January, March, and May

### Date Range Constraints

You can limit when a schedule is active using start and end dates. This is useful for seasonal schedules or time-limited events.

**Start date only:**
```
mon 14:00 start:2026-06-01
```
Schedule begins June 1, 2026 and continues indefinitely.

**End date only:**
```
mon 14:00 end:2026-12-31
```
Schedule runs until December 31, 2026, then stops.

**Both start and end:**
```
mon 14:00 start:2026-06-01 end:2026-12-31
```
Schedule runs only from June 1 through December 31, 2026.

**Date format:** Use ISO 8601 format: `YYYY-MM-DD`

**Examples:**
- `4th tue 19:00 jan-oct start:2026-01-01 end:2026-12-31` → 4th Tuesday at 7 PM, January–October 2026 only
- `fri 18:00 start:2026-06-01` → Every Friday at 6 PM, starting June 1, 2026
- `1st mon 09:00 end:2026-05-31` → First Monday at 9 AM, until May 31, 2026

### Examples

**Every 4th Tuesday at 7:00 PM, January through October:**
```
4th tue 19:00 jan-oct
```
or
```
4th tue 1900 jan-oct
```

**Reminder on the 1st and 15th of each month at 2:30 PM:**
```
1,15d 14:30
```

**Every Monday morning at 9:00 AM:**
```
mon 09:00
```

**Every Wednesday during summer at noon:**
```
wed jun-aug 12:00
```

**Last Saturday of the month at 10:00 AM:**
```
last sat 10:00
```

**Weekly on Monday at 6:00 PM:**
```
mon 18:00
```

**Every 6 hours:**
```
*/6h 0m
```

**Note:** `0m` needs to be specified here to trigger at the top of the
hour every 6 hours. If `0m` was not specified, then the result would be
to trigger every minute for an hour every 6 hours. This is because without
specifying the minute component it defaults to `*` which would cause the
trigger to fire every minute.

### Field Resolution Order

When the flexible cron parser processes your expression:

1. **Time** is extracted first (HH:MM or HHMM pattern)
2. If no combined time, **hour** (`Nh`) and **minute** (`Nm`) are parsed separately
3. **Month** names/numbers are identified
4. **Day of month** (with `d` suffix) is parsed
5. **Week** (with `w` suffix) is parsed
6. **Day of week** with optional ordinal prefix is parsed last

Fields not specified default to `*` (any value).

---

## Configuration File Format

### Location

Scheduled messages are stored in the `[Scheduled_Messages]` section of `config.ini`:

```ini
[Scheduled_Messages]
schedule_expression = Channel:Message text
```

### Basic Format

Each line defines one scheduled message:

```ini
schedule = channel:message
```

**Example:**
```ini
0 8 * * * = General:Good morning everyone!
```

This sends "Good morning everyone!" to the General channel every day at 8:00 AM.

### Scoped Messages (Regional Flood)

To limit message propagation to a specific region, use the scoped format:

```ini
schedule = channel:#scope:message
```

**Example:**
```ini
0 18 * * 5 = Public:#sea:ARES net starts in 30 minutes
```

This sends the message only within the `#sea` flood scope.

### Using Flexible Cron in config.ini

When using flexible cron with `HH:MM` time format, the colon (`:`) conflicts with the INI file's `key:value` separator. To work around this, the bot automatically encodes colons in schedule keys as exclamation marks (`!`) when writing to disk.

**What you enter in the web UI:**
```
4th tue 14:00 jan-oct
```

**How it appears in config.ini:**
```ini
4th tue 14!00 jan-oct
```

**Important:** When manually editing `config.ini`, you must use `!` instead of `:` in the schedule key (the left side of the `=`). The bot will decode it back to `:` when loading. The message text (right side) can contain colons normally.

If you use the `HHMM` format (without a colon), no encoding is needed:

```ini
4th tue 1400 jan-oct
```

### Escape Sequences

The bot supports escape sequences in message text:

| Sequence | Result          |
|----------|-----------------|
| `\n`     | Line break      |
| `\t`     | Tab character   |
| `\\`     | Literal backslash |

**Example:**
```ini
0 9 * * * = General:Good morning!\nHave a great day!
```

This sends a two-line message.

### Complete Examples

**Daily morning message:**
```ini
0 8 * * * = General:Good morning! Today is a new day.
```

**Weekly ARES net reminder:**
```ini
0 18 * * 2 = ARES:#local:Net starts at 7 PM tonight
```

**Monthly first-of-month announcement:**
```ini
0 12 1 * * = Announcements:First of the month - time to check in!
```

**Flexible cron - 2nd and 4th Thursday at 7:30 PM:**
```ini
2nd,4th thu 19!30 = ARES:Meeting tonight
```

**Hourly status update:**
```ini
@hourly = Status:Hourly system check - all OK
```

---

## Timezone Handling

All scheduled messages use the timezone configured in the `[Bot]` section of `config.ini`:

```ini
[Bot]
timezone = US/Eastern
```

If no timezone is set, the bot uses the system's local timezone.

---

## Command Placeholders in Scheduled Messages

Scheduled messages can include dynamic content using command placeholders like `{cmd:wx}` or `{cmd:solar}`. However, these commands consume airtime on the mesh network, so there's a minimum interval restriction:

**Minimum interval for messages with `{cmd:...}` placeholders: 15 minutes**

If you try to schedule a message with a command placeholder more frequently than every 15 minutes, the web UI will reject it with an error.

**Valid:**
```ini
*/15 * * * * = General:Current weather: {cmd:wx Seattle}
```

**Invalid (too frequent):**
```ini
*/5 * * * * = General:Weather: {cmd:wx Seattle}
```

---

## Tips and Best Practices

1. **Use the web UI for initial setup** — it validates your schedule and shows the next 5 run times
2. **Start with simplified modes** for common patterns, then switch to Advanced/Flexible for complex schedules
3. **Avoid overly frequent schedules** — remember that every message uses mesh airtime
4. **Use scoped floods** for regional messages to avoid unnecessary network load
5. **Use flexible cron for readability** — `4th tue 19:00 jan-oct` is clearer than `0 19 * jan-oct tue` when editing config files
6. **Use HHMM (no colon) in config.ini** if manually editing flexible cron, to avoid the `!` encoding

---

## Troubleshooting

### My schedule isn't firing

1. Check that the schedule expression is valid using the web UI preview
2. Verify the timezone setting in `[Bot]` matches your expectation
3. Look for warnings in the bot logs about invalid schedules
4. Ensure the message value contains a colon (e.g., `Channel:Message`)

### My flexible cron key has `!` instead of `:`

This is normal for flexible cron entries created through the web UI. The bot automatically converts `!` back to `:` when loading. If manually editing, use `!` in the schedule key or use `HHMM` format without punctuation.

### Schedule works in the UI but not after restarting the bot

The bot reloads schedules when the web viewer saves changes, but you may need to restart the bot if you manually edited `config.ini` while it was running.

### Day-of-week isn't working as expected

Remember that day-of-week uses APScheduler's numbering: 0=Monday, 6=Sunday. This differs from traditional Unix cron (0=Sunday). Use day names (`mon`, `tue`, etc.) to avoid confusion.

---

## Summary Reference

| Format Type | Use When | Example |
|-------------|----------|---------|
| **Simplified** | Setting up common patterns via web UI | "Daily at 8:00 AM" |
| **Standard cron** | You're familiar with cron syntax | `0 8 * * *` |
| **Flexible cron** | You want readable, order-independent syntax | `4th tue 19:00 jan-oct` |
| **Presets** | Very simple recurring schedules | `@daily`, `@weekly` |
