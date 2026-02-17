# Config validation

The bot can validate your `config.ini` for section names and path writability before you run it. Use this to catch typos (e.g. `[WebViewer]` instead of `[Web_Viewer]`) and missing required sections.

## How to run

**Standalone script** (no bot startup):

```bash
python validate_config.py [--config config.ini]
```

**At bot startup** (validate then exit):

```bash
python meshcore_bot.py --validate-config [--config config.ini]
```

- **Exit 0** – No errors (warnings and info may still be printed).
- **Exit 1** – One or more errors; fix them before starting the bot.

Warnings and info do not change the exit code. Only **errors** cause exit 1.

## What is checked

### Required sections

The bot will not start without these sections. The validator reports them as **errors** if missing:

| Section        | Purpose                                      |
|----------------|----------------------------------------------|
| `[Connection]` | Serial, BLE, or TCP connection parameters   |
| `[Bot]`        | Database path, bot name, rate limits, etc.   |
| `[Channels]`   | Monitor channels, DM behavior                |

### Section names

- **Canonical sections** (e.g. `[Web_Viewer]`, `[Feed_Manager]`) and any section ending in **`_Command`** (e.g. `[Path_Command]`, `[Wx_Command]`) are valid.
- **Known typos** are reported as **warnings** with a suggestion, for example:
  - `[WebViewer]` → use `[Web_Viewer]`
  - `[FeedManager]` → use `[Feed_Manager]`
  - `[Jokes]` → use `[Joke_Command]` / `[DadJoke_Command]` (see [Configuration](configuration.md) and [Upgrade](upgrade.md) for legacy support).
- **Unknown sections** (not in the canonical list and not a `*_Command` section) are reported as **info**; the validator may suggest a similar section name if it looks like a command.

### Optional sections (info only)

If these are absent, the validator reports **info** (no error):

- **`[Admin_ACL]`** – Absent means admin commands (repeater, webviewer, reload) are disabled.
- **`[Banned_Users]`** – Absent means no users are banned.
- **`[Localization]`** – Absent means defaults (e.g. `language=en`, `translation_path=translations/`) are used.

### Path writability

The validator checks that paths for **database**, **log file**, and **Web_Viewer db_path** (when set) are writable. Problems are reported as **warnings** (e.g. directory does not exist or is not writable). Relative paths are resolved from the directory containing the config file.

## Severity levels

| Level   | Effect on exit code | Typical meaning                    |
|---------|----------------------|------------------------------------|
| Error   | Exit 1               | Must fix (e.g. missing section)     |
| Warning | Exit 0               | Likely mistake (e.g. section typo) |
| Info    | Exit 0               | Informational (e.g. optional section absent) |

## Example

```bash
$ python validate_config.py --config config.ini
Warning: Non-standard section [WebViewer]; did you mean [Web_Viewer]?
Info: Section [Localization] absent; using defaults (language=en, translation_path=translations/).
```

Fix the `[WebViewer]` section name, then re-run. After fixing errors, the bot can start normally; you can also run with `--validate-config` before each start if you prefer.
