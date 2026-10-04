# Local plugins and services

You can add your own **command plugins** and **service plugins** without modifying the bot’s code by placing them in a **local** directory. By default that directory is **`local/`** next to your main config.ini; you can change it with **`[Bot]` `local_dir_path`** (see below). Their configuration can live in **`local/config.ini`** so it stays separate from the main `config.ini`.

## Directories

| Path | Purpose |
|------|---------|
| **local/commands/** | One Python file per command plugin (subclass of `BaseCommand`). |
| **local/service_plugins/** | One Python file per service plugin (subclass of `BaseServicePlugin`). |
| **local/config.ini** | Optional. Merged with main config; use it for your plugins’ sections. |

Local plugins are **additive**: they are loaded after built-in (and alternative) plugins. If a local plugin or service has the same logical **name** as one already loaded, it is **skipped** and a warning is logged. There is no override-by-name for local code.

**Custom local path:** In `config.ini`, under `[Bot]`, you can set **`local_dir_path`** to a directory that contains `commands/`, `service_plugins/`, and optional `config.ini`. Use a relative path (resolved from the bot root) or an absolute path so the local folder can live outside the install directory (e.g. `/home/user/meshcore-local`). Changing `local_dir_path` requires a **bot restart**.

## Minimal command plugin

Create a file in **local/commands/** (e.g. `local/commands/hello_local.py`):

```python
# local/commands/hello_local.py
from modules.commands.base_command import BaseCommand
from modules.models import MeshMessage


class HelloLocalCommand(BaseCommand):
    name = "hellolocal"
    keywords = ["hellolocal", "hi local"]
    description = "A local greeting command"

    async def execute(self, message: MeshMessage) -> bool:
        return await self.handle_keyword_match(message)
```

- The bot discovers all `.py` files in `local/commands/` (except `__init__.py`).
- Each file must define exactly one class that inherits from `BaseCommand` and is not the base class itself.
- Use `bot.config` for options; you can put your section in **local/config.ini** (e.g. `[HelloLocal_Command]`) and read with `self.get_config_value('HelloLocal_Command', 'enabled', fallback=True, value_type='bool')` or `self.bot.config.get(...)`.

Restart the bot (or ensure the directory exists and the file is in place before starting). The command will be registered like any other.

## Minimal service plugin

Create a file in **local/service_plugins/** (e.g. `local/service_plugins/my_background_service.py`):

```python
# local/service_plugins/my_background_service.py
from modules.service_plugins.base_service import BaseServicePlugin


class MyBackgroundService(BaseServicePlugin):
    config_section = "MyBackground"
    description = "A local background service"

    async def start(self) -> None:
        self._running = True
        self.logger.info("MyBackground service started")

    async def stop(self) -> None:
        self._running = False
        self.logger.info("MyBackground service stopped")
```

- The bot discovers all `.py` files in `local/service_plugins/` (excluding `__init__.py`, `base_service.py`, and `*_utils.py`).
- The class must inherit from `BaseServicePlugin` and implement `start()` and `stop()`.
- To enable it, add a section in **local/config.ini** (or main config) with `enabled = true`:

```ini
[MyBackground]
enabled = true
```

Restart the bot so the service is loaded and started.

## Base class hooks

`BaseCommand` and `BaseServicePlugin` take care of a few things for you when you set a class attribute or call a helper, instead of overriding a method:

**Commands** (`modules/commands/base_command.py`):

| Hook | What it does |
|---|---|
| `enabled_attr = "my_enabled"` | `can_execute` refuses while `self.my_enabled` is false, before any other check. Set the attribute in `__init__`, for example from `self.get_config_value('MyCommand_Command', 'enabled', fallback=True, value_type='bool')`. |
| `admin_only = True` | The command always requires admin access, whatever `[Admin_ACL] admin_commands` lists. `requires_admin_access()` is still what callers ask. |
| `keywords_format_key = "mycommand"` | `get_response_format()` returns `[Keywords] mycommand` (surrounding double quotes stripped) when it is set; `handle_keyword_match` then replies with it through `format_response`. |
| `get_help_text(self, message=None)` | The signature every bundled command uses; `help` passes the requesting message so the text can depend on it (DM vs channel). A plugin written as `get_help_text(self)` still works. |
| `self.translated_or(key, fallback, kind=list)` | The translation at `key` when it is a non-empty `kind` (a list of greetings, a dict), else your built-in `fallback`. |

**Services** (`modules/service_plugins/base_service.py`):

| Helper | What it does |
|---|---|
| `await self.run_periodic(work, interval, error_message, error_delay=60)` | Calls `await work()` every `interval()` seconds while `self._running`; an exception is logged as `"<error_message>: <error>"` and retried after `error_delay`. Run it as a task from `start()`. |
| `await self._cancel_tasks(task_a, task_b)` | Cancels each task (None is skipped) and waits for it; use it in `stop()`. |
| `self._subscribe(meshcore, event_type, handler)` / `self._unsubscribe_all()` | Subscribe to meshcore events through `_subscribe` and call `_unsubscribe_all()` in `stop()`: a health restart calls `start()` again, and a subscription left behind would deliver every event twice. |

A service that forwards channel messages to another chat system can subclass `ChannelBridgeBase` (`modules/service_plugins/channel_bridge_utils.py`), which the Discord and Telegram bridges use for start/stop, message intake and the retrying send queue; the subclass supplies `_bridge_mappings`, `_targets_for`, `_deliver`, `_throttled` and `_send_queued`. Files named `*_utils.py` are never loaded as services, so shared code like this can live next to your plugins.

## Configuration

- **Main config** is read first, then **local/config.ini** if it exists. So `bot.config` contains both; later file wins on overlapping sections/keys.
- Put options for your local plugins in **local/config.ini** to keep main `config.ini` clean. Use the same section naming as built-in plugins (e.g. `[MyCommand_Command]` for a command, or a `config_section` for a service).
- After a **config reload** (e.g. via the `reload` command), both main config and `local/config.ini` are re-read, so on-demand config in your plugins will see updates. Plugin/service instances are not reloaded; only config values.

## Duplicate names

If a local command or service has the same **name** as an already-loaded plugin or service (e.g. you add `local/commands/ping.py` with `name = "ping"`), the local one is **skipped** and a warning is logged. Choose a different name (e.g. `pinglocal`) to avoid the conflict.

## Sending multiple messages (chunking)

When a service plugin sends a long message by splitting it into chunks and calling `send_channel_message` multiple times, the bot’s **rate limiters** can block the second and later sends. You’ll see a warning like “Rate limited. Wait X seconds.”

**What’s going on**

- **Global rate limit** (`[Bot]` `rate_limit_seconds`, default 10): minimum time between *any* two bot replies. If you don’t skip it, the first send uses the “slot” and the next send within that window is blocked.
- **Bot TX rate limit** (`bot_tx_rate_limit_seconds`, default 1.0): minimum time between bot transmissions on the mesh. This is always enforced.

**What to do**

**Easiest:** use the built-in chunked helper so spacing and rate limits are handled for you:

```python
# Service plugin: send long text to your channel in chunks (no manual delay loop)
await self.bot.command_manager.send_channel_messages_chunked(self.channel, chunks)
```

Commands that reply to a message (channel or DM) can use `await self.send_response_chunked(message, chunks)`.

**Alternatively**, implement the spacing yourself:

1. **Use `skip_user_rate_limit=True`** for every chunk. That skips the global (and per-user) limits so automated service messages are not blocked by the 10 second global window. That skips the global (and per-user) limits so automated service messages aren’t blocked by the “10 second” global window.
2. **Space chunks in time** so the bot TX limit is satisfied: before each chunk after the first, wait for the bot TX rate limiter and then sleep. Same pattern as the greeter and other multi-part senders:

```python
import asyncio

# chunks = ["first part...", "second part...", ...]
for i, chunk in enumerate(chunks):
    if i > 0:
        await self.bot.bot_tx_rate_limiter.wait_for_tx()
        rate_limit = self.bot.config.getfloat('Bot', 'bot_tx_rate_limit_seconds', fallback=1.0)
        sleep_time = max(rate_limit + 0.5, 1.0)
        await asyncio.sleep(sleep_time)
    await self.bot.command_manager.send_channel_message(
        self.channel, chunk, skip_user_rate_limit=True
    )
```

So you are allowed to send multiple messages in sequence; you do **not** need 10 seconds between chunks. Use `skip_user_rate_limit=True` and about 1–1.5 seconds (or your configured `bot_tx_rate_limit_seconds` + buffer) between chunks.

## Resolving locations (shared API)

For place lookup (coords, ZIP, city, neighborhoods, optional repeater names), use **`modules.location`** rather than calling Nominatim directly:

```python
from modules.location import OPTIONS_AQI, classify_location, resolve_location

# Pure classify (no network):
normalized, location_type = classify_location("mexico city")  # -> ("mexico city, mexico", "city")

# Best-effort resolve (uses bot Nominatim rate limiter + geocode cache):
resolved = resolve_location(self.bot, "seattle", options=OPTIONS_AQI)
if resolved.error:
    ...
else:
    lat, lon = resolved.lat, resolved.lon
    label = resolved.display_name
```

**Presets** (`OPTIONS_AQI`, `OPTIONS_WX`, `OPTIONS_AURORA`, `OPTIONS_RAIN`, `OPTIONS_PREFIX`, `OPTIONS_SOLARFORECAST`) encode command-specific semantics via `ResolveOptions` flags (intl cities, neighborhoods, structured ZIP, region capitals, Zippopotam labels, repeater names, label style). Low-level helpers remain in **`modules.utils`** (`geocode_city_sync`, `geocode_zipcode_sync`, rate-limited Nominatim). Alert-style agency/street/county parsing stays command-local; only the geocode subset should call `resolve_location`.

Built-in commands other than AQI will migrate onto this API over time; new plugins should prefer it now.

## References

- [Service plugins](service-plugins.md) — built-in services and how they are enabled.
- [Check-in API](checkin-api.md) — contract for the optional check-in submission API (local check-in service).
- Built-in command plugins live in **modules/commands/** and **modules/commands/alternatives/**; you can use them as examples for `BaseCommand`, `get_config_value`, `handle_keyword_match`, etc.
- Location helpers: **modules/location.py** (high-level), **modules/utils.py** (Nominatim / geocode_*).
- Base classes: **modules/commands/base_command.py** (`BaseCommand`), **modules/service_plugins/base_service.py** (`BaseServicePlugin`).

## Check-in service (local)

The repo includes a local service plugin **`local/service_plugins/checkin_service.py`** that collects check-ins from a channel (default `#meshmonday`) on a chosen day (Monday only or daily). You can require a specific phrase (e.g. "check in") or count any message. Optionally it submits check-in data (packet hash, username, message) to a web API secured with an API key. Configuration belongs in **local/config.ini** under `[CheckIn]`. See **config.ini.example** for a commented `[CheckIn]` block and **[Check-in API](checkin-api.md)** for the API contract if you run or build a server to receive submissions.
