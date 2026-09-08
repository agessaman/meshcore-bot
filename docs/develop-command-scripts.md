# Developing Command Scripts for MeshCore Bot

This guide covers how to develop custom command scripts (plugins) for the MeshCore Bot. Commands are Python classes that inherit from `BaseCommand` and respond to user messages on the mesh network.

Local commands should be placed in the `local/commands/` directory, which allows you to add custom functionality without modifying the core bot code.

## Table of Contents

- [Getting Started](#getting-started)
- [Command Class Structure](#command-class-structure)
- [Class-Level Variables Reference](#class-level-variables-reference)
- [Core Methods](#core-methods)
- [Network Communication APIs](#network-communication-apis)
- [Data Persistence APIs](#data-persistence-apis)
- [External Data Access](#external-data-access)
- [Configuration and Localization](#configuration-and-localization)
- [Error Handling](#error-handling)
- [Best Practices](#best-practices)
- [Testing Your Command](#testing-your-command)

---

## Getting Started

### Basic Command Template

Create a new file in `local/commands/` with the following structure:

```python
#!/usr/bin/env python3
"""
Your Command - Brief description of what it does
"""

from modules.models import MeshMessage
from modules.commands.base_command import BaseCommand


class YourCommand(BaseCommand):
    """Detailed description of your command."""

    # Plugin metadata
    name = "yourcommand"
    keywords = ["yourcommand", "yc"]
    description = "Brief description for help text"
    category = "general"
    
    # Documentation for website generation
    short_description = "Brief description without usage syntax"
    usage = "yourcommand [options]"
    examples = ["yourcommand", "yourcommand option"]
    parameters = [
        {"name": "option", "description": "Optional parameter description"}
    ]

    def __init__(self, bot):
        """Initialize the command."""
        super().__init__(bot)
        # Load your configuration here
        self.enabled = self.get_config_value(
            'YourCommand_Command', 
            'enabled', 
            fallback=True, 
            value_type='bool'
        )

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if command can execute."""
        if not self.enabled:
            return False
        return super().can_execute(message, skip_channel_check)

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the command logic."""
        try:
            # Your command logic here
            response = "Your response text"
            await self.send_response(message, response)
            return True
        except Exception as e:
            self.logger.error(f"Error in yourcommand: {e}")
            await self.send_response(message, "An error occurred")
            return True
```

---

## Command Class Structure

All commands must inherit from `BaseCommand` located in `modules/commands/base_command.py`.

### Required Imports

```python
from modules.models import MeshMessage
from modules.commands.base_command import BaseCommand
```

### Optional Common Imports

```python
import asyncio
import re
from typing import Any, Optional
from datetime import datetime, timezone

# For HTTP requests
import aiohttp

# For database access
# (available via self.bot.db_manager)

# For external API clients
from modules.clients.your_client import YourClient

# For utilities
from modules.utils import (
    geocode_city_sync,
    geocode_zipcode_sync,
    get_config_timezone,
    format_elapsed_display,
)
```

---

## Class-Level Variables Reference

These variables define your command's metadata and behavior:

| Variable | Type | Required | Description |
|----------|------|----------|-------------|
| `name` | `str` | **Yes** | Primary command name (lowercase, used for config section) |
| `keywords` | `list[str]` | **Yes** | Trigger words for the command (includes name and aliases) |
| `description` | `str` | **Yes** | Brief description shown in help text |
| `category` | `str` | No | Category for grouping (e.g., "weather", "entertainment", "admin") |
| `requires_dm` | `bool` | No | Set to `True` if command only works in direct messages (default: `False`) |
| `requires_internet` | `bool` | No | Set to `True` if command needs internet access (default: `False`) |
| `cooldown_seconds` | `int` | No | Per-user cooldown period in seconds (default: `0`) |
| `render_safe` | `bool` | No | Set to `True` if command can be safely rendered in scheduled messages (default: `False`) |
| `short_description` | `str` | No | Brief description for website (without usage syntax) |
| `usage` | `str` | No | Usage syntax string (e.g., `"wx <zipcode> [tomorrow]"`) |
| `examples` | `list[str]` | No | Example commands for documentation |
| `parameters` | `list[dict]` | No | Parameter definitions with `name` and `description` |
| `settings_schema` | `list[dict]` | No | Web viewer settings schema (see [Settings Schema](#settings-schema)) |

### Settings Schema

Commands can define a `settings_schema` to provide typed configuration in the web viewer:

```python
settings_schema = [
    {
        "key": "poll_interval",
        "label": "Poll interval",
        "type": "int",
        "min": 1000,
        "max": 86400000,
        "default": 60000,
        "help": "Polling cadence in milliseconds",
        "unit": "ms"
    },
    {
        "key": "enable_notifications",
        "label": "Enable notifications",
        "type": "bool",
        "default": True,
        "help": "Send notifications for new events"
    },
    {
        "key": "priority",
        "label": "Priority level",
        "type": "enum",
        "options": [
            {"value": "low", "label": "Low"},
            {"value": "medium", "label": "Medium"},
            {"value": "high", "label": "High"}
        ],
        "default": "medium",
        "help": "Event priority threshold"
    }
]
```

**Supported types:** `bool`, `int`, `float`, `str`, `enum`, `list`

---

## Core Methods

### Required Methods

#### `async def execute(self, message: MeshMessage) -> bool`

**Purpose:** Execute the command logic when triggered.

**Parameters:**
- `message`: The `MeshMessage` object containing the user's message and metadata

**Returns:** `bool` - `True` if executed successfully, `False` otherwise

**Example:**
```python
async def execute(self, message: MeshMessage) -> bool:
    """Execute the joke command."""
    try:
        # Record execution for cooldown tracking
        self.record_execution(message.sender_id)
        
        # Your command logic
        joke_data = await self.get_joke_from_api()
        
        # Format and send response
        response = f"🎭 {joke_data['joke']}"
        await self.send_response(message, response)
        
        return True
    except Exception as e:
        self.logger.error(f"Error in joke command: {e}")
        await self.send_response(message, "Sorry, couldn't fetch a joke!")
        return True
```

### Optional Override Methods

#### `def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool`

**Purpose:** Check if command can execute (permissions, cooldowns, custom checks).

**Default behavior:** Checks channel access, DM requirements, cooldowns, and admin access.

**Example:**
```python
def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
    """Check if command can execute with custom logic."""
    # Use base class checks first
    if not super().can_execute(message, skip_channel_check):
        return False
    
    # Check if enabled
    if not self.my_enabled:
        return False
    
    # Custom check: dark jokes only in DM
    if self.is_dark_joke_request(message) and not message.is_dm:
        return False
    
    return True
```

#### `def get_help_text(self, message: MeshMessage = None) -> str`

**Purpose:** Return help text for the command (shown in `help <command>`).

**Example:**
```python
def get_help_text(self, message: MeshMessage = None) -> str:
    """Get help text, excluding dark category if not in DM."""
    if message and not message.is_dm:
        return "Usage: joke [category] - Categories: programming, pun, misc"
    else:
        return "Usage: joke [category] - Categories: programming, pun, misc, dark"
```

#### `def matches_keyword(self, message: MeshMessage) -> bool`

**Purpose:** Custom keyword matching logic (default implementation usually sufficient).

#### `def matches_custom_syntax(self, message: MeshMessage) -> bool`

**Purpose:** Check for custom syntax patterns beyond simple keywords.

**Example:**
```python
def matches_custom_syntax(self, message: MeshMessage) -> bool:
    """Match lat,lon coordinate syntax."""
    if not super().matches_custom_syntax(message):
        return False
    
    content = message.content.strip()
    # Match coordinate pattern like "48.08,-121.97"
    return bool(re.match(r'^-?\d+\.?\d*\s*,\s*-?\d+\.?\d*$', content))
```

---

## Network Communication APIs

### Sending Messages

#### `async def send_response(message: MeshMessage, content: str, skip_user_rate_limit: bool = False, *, command_id: str | None = None) -> bool`

Send a single response message (channel or DM).

**Parameters:**
- `message`: Original message to respond to
- `content`: Response text (max 158 bytes for DM, varies for channels)
- `skip_user_rate_limit`: Skip user rate limiter (for follow-up messages)
- `command_id`: Optional ID for tracking/deduplication

**Returns:** `bool` - `True` if sent successfully

**Example:**
```python
await self.send_response(message, "Weather: Sunny, 72°F")
```

#### `async def send_response_chunked(message: MeshMessage, chunks: list[str], *, skip_user_rate_limit_first: bool = True) -> bool`

Send multiple messages with rate-limit spacing (automatically delays between chunks).

**Parameters:**
- `message`: Original message to respond to
- `chunks`: List of message strings to send in order
- `skip_user_rate_limit_first`: Skip rate limit for first chunk

**Returns:** `bool` - `True` if all sent successfully

**Example:**
```python
chunks = [
    "Part 1: Setup message",
    "Part 2: Delivery message"
]
await self.send_response_chunked(message, chunks)
```

### Message Length Helpers

#### `def get_max_message_length(self, message: MeshMessage) -> int`

Calculate maximum safe message length in UTF-8 bytes.

- **DM messages:** 158 bytes
- **Channel messages:** 160 - username_bytes - 2, minus 10 for regional flood scope

**Example:**
```python
max_len = self.get_max_message_length(message)
if len(response) > max_len:
    response = response[:max_len - 3] + "..."
```

### MeshMessage Object

The `MeshMessage` dataclass (from `modules/models.py`) contains:

| Field | Type | Description |
|-------|------|-------------|
| `content` | `str` | Message text content |
| `sender_id` | `Optional[str]` | Sender's node ID |
| `sender_pubkey` | `Optional[str]` | Sender's public key (for admin checks) |
| `channel` | `Optional[str]` | Channel name (e.g., `"#general"`) |
| `is_dm` | `bool` | `True` if direct message |
| `timestamp` | `Optional[int]` | Message timestamp (Unix epoch) |
| `snr` | `Optional[float]` | Signal-to-noise ratio in dB |
| `rssi` | `Optional[int]` | Received signal strength in dBm |
| `hops` | `Optional[int]` | Number of hops (may be `None`) |
| `path` | `Optional[str]` | Path string for display |
| `routing_info` | `Optional[dict]` | Detailed routing information |
| `reply_scope` | `Optional[str]` | Flood scope for reply |
| `content_lower` | `str` | Lowercased content (set by framework) |

---

## Data Persistence APIs

The bot provides a SQLite database through `self.bot.db_manager`. All database operations should use the context manager for proper connection handling.

### Database Connection

```python
with self.bot.db_manager.connection() as conn:
    cursor = conn.cursor()
    # Your database operations
    conn.commit()
```

### Common Database Operations

#### Query with Results

```python
def get_user_stats(self, user_id: str) -> Optional[dict]:
    """Get user statistics from database."""
    try:
        with self.bot.db_manager.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT command_name, COUNT(*) as count
                FROM command_stats
                WHERE user_id = ?
                GROUP BY command_name
            """, (user_id,))
            results = cursor.fetchall()
            return [{"command": row[0], "count": row[1]} for row in results]
    except Exception as e:
        self.logger.error(f"Database error: {e}")
        return None
```

#### Insert/Update Data

```python
def save_user_preference(self, user_id: str, preference: str, value: str):
    """Save user preference to database."""
    try:
        with self.bot.db_manager.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO user_preferences
                (user_id, preference, value, updated_at)
                VALUES (?, ?, ?, datetime('now'))
            """, (user_id, preference, value))
            conn.commit()
    except Exception as e:
        self.logger.error(f"Error saving preference: {e}")
```

### Caching APIs

#### Geocoding Cache

```python
# Check cache first
lat, lon = self.bot.db_manager.get_cached_geocoding("Seattle, WA")

if lat is None or lon is None:
    # Fetch from API
    lat, lon = await geocode_city("Seattle", "WA")
    
    # Cache for 30 days (720 hours)
    self.bot.db_manager.cache_geocoding("Seattle, WA", lat, lon, cache_hours=720)
```

#### Generic Cache

```python
# Get cached value
cached = self.bot.db_manager.get_cached_value(
    cache_key="weather_98101",
    cache_type="weather_data"
)

if cached is None:
    # Fetch fresh data
    data = await fetch_weather_data("98101")
    
    # Cache for 1 hour
    self.bot.db_manager.cache_value(
        cache_key="weather_98101",
        cache_type="weather_data",
        value=json.dumps(data),
        cache_hours=1
    )
```

### Execute Query Helper

```python
# Use the db_manager's execute_query for simpler queries
results = self.bot.db_manager.execute_query(
    "SELECT * FROM command_stats WHERE user_id = ? LIMIT 10",
    (user_id,)
)

for row in results:
    # row is a dict with column names as keys
    self.logger.info(f"Command: {row['command_name']}, Count: {row['count']}")
```

---

## External Data Access

### HTTP Requests with aiohttp

Use `aiohttp` for asynchronous HTTP requests:

```python
async def get_data_from_api(self, query: str) -> Optional[dict]:
    """Fetch data from external API."""
    url = f"https://api.example.com/data?q={query}"
    timeout = 10  # seconds
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, 
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
                else:
                    self.logger.error(f"API returned status {response.status}")
                    return None
    except asyncio.TimeoutError:
        self.logger.error("Timeout fetching data")
        return None
    except Exception as e:
        self.logger.error(f"Error fetching data: {e}")
        return None
```

### Blocking Operations with asyncio.to_thread

For blocking I/O operations (geocoding, file operations), use `asyncio.to_thread`:

```python
async def execute(self, message: MeshMessage) -> bool:
    """Execute with offloaded blocking operation."""
    location = "Seattle, WA"
    
    # Offload blocking geocode to thread
    lat, lon, address = await asyncio.to_thread(
        geocode_city_sync,
        self.bot,
        location,
        default_state="WA",
        default_country="US",
        timeout=10
    )
    
    if lat is None:
        await self.send_response(message, "Location not found")
        return True
    
    # Continue with result
    response = f"Coordinates: {lat:.2f}, {lon:.2f}"
    await self.send_response(message, response)
    return True
```

### Custom API Clients

Create client classes in `modules/clients/` for reusable API access:

```python
# modules/clients/my_api_client.py
class MyAPIClient:
    """Client for MyAPI service."""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.example.com"
    
    def get_data(self, param: str) -> dict:
        """Fetch data (blocking)."""
        url = f"{self.base_url}/endpoint"
        response = requests.get(url, params={"key": self.api_key, "q": param})
        response.raise_for_status()
        return response.json()
```

Use in command with thread offloading:

```python
async def execute(self, message: MeshMessage) -> bool:
    """Use custom API client."""
    client = MyAPIClient(api_key=self.api_key)
    
    # Offload blocking call
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, lambda: client.get_data("query"))
    
    # Process data...
```

---

## Configuration and Localization

### Loading Configuration

Use `get_config_value()` for type-safe config access with migration support:

```python
def __init__(self, bot):
    super().__init__(bot)
    
    # Load configuration values
    self.enabled = self.get_config_value(
        'MyCommand_Command',  # Section name: CommandName_Command
        'enabled',            # Key
        fallback=True,        # Default value
        value_type='bool'     # Type: 'bool', 'int', 'float', 'str', 'list'
    )
    
    self.timeout = self.get_config_value(
        'MyCommand_Command',
        'timeout',
        fallback=10,
        value_type='int'
    )
    
    self.categories = self.get_config_value(
        'MyCommand_Command',
        'categories',
        fallback='cat1,cat2',
        value_type='list'  # Returns ['cat1', 'cat2']
    )
```

### Configuration Section Naming

- Standard format: `CommandName_Command`
- Examples: `Joke_Command`, `Weather_Command`, `Status_Command`
- CamelCase commands: `DadJoke_Command`, `WebViewer_Command`

### Localization and Translation

Use the translation system for internationalized text:

```python
# Simple translation
message_text = self.translate('commands.mycommand.error_message')

# Translation with parameters
message_text = self.translate(
    'commands.mycommand.response',
    location="Seattle",
    temperature=72
)

# Get structured data (lists, dicts)
categories = self.translate_get_value('commands.mycommand.categories')
```

### Auto-detecting User Language

Use the context manager to respond in the user's detected language:

```python
async def execute(self, message: MeshMessage) -> bool:
    """Execute with language detection."""
    with self.respond_in_sender_language(message):
        # All translate() calls use detected language
        response = self.translate('commands.mycommand.response')
    
    await self.send_response(message, response)
    return True
```

---

## Error Handling

### General Error Handling Pattern

```python
async def execute(self, message: MeshMessage) -> bool:
    """Execute with proper error handling."""
    try:
        # Record execution early for cooldown
        self.record_execution(message.sender_id)
        
        # Main command logic
        result = await self.fetch_data()
        
        if result is None:
            await self.send_response(
                message, 
                self.translate('commands.mycommand.no_data')
            )
            return True
        
        # Format and send response
        response = self.format_response(result)
        await self.send_response(message, response)
        return True
        
    except asyncio.TimeoutError:
        self.logger.error("Timeout in mycommand")
        await self.send_response(
            message,
            self.translate('commands.mycommand.timeout')
        )
        return True
        
    except Exception as e:
        self.logger.error(f"Error in mycommand: {e}")
        await self.send_response(
            message,
            self.translate('commands.mycommand.error')
        )
        return True
```

### Logging Levels

```python
# Debug: Detailed diagnostic information
self.logger.debug(f"Processing message: {message.content}")

# Info: General informational messages
self.logger.info(f"Command executed by {message.sender_id}")

# Warning: Something unexpected but handled
self.logger.warning(f"Invalid category '{category}', using default")

# Error: Error that prevented normal operation
self.logger.error(f"Failed to fetch data: {e}")

# Critical: Serious error requiring attention
self.logger.critical(f"Database connection failed: {e}")
```

### Input Validation

```python
def validate_input(self, message: MeshMessage) -> Optional[str]:
    """Validate and parse command input."""
    content = message.content.strip()
    
    # Remove command prefix if present
    if content.startswith('!'):
        content = content[1:].strip()
    
    # Split into parts
    parts = content.split()
    
    if len(parts) < 2:
        return None
    
    # Validate parameter (e.g., zip code)
    param = parts[1]
    if not re.match(r'^\d{5}$', param):
        return None
    
    return param
```

---

## Best Practices

### 1. Always Use Cooldowns for External APIs

```python
class MyCommand(BaseCommand):
    cooldown_seconds = 5  # Prevent API abuse
    requires_internet = True
```

### 2. Record Execution Early

```python
async def execute(self, message: MeshMessage) -> bool:
    # Record before doing work (for cooldown tracking)
    self.record_execution(message.sender_id)
    
    # Then proceed with command logic
    # ...
```

### 3. Handle Message Length Limits

```python
max_len = self.get_max_message_length(message)
if len(response) > max_len:
    response = response[:max_len - 3] + "..."

await self.send_response(message, response)
```

### 4. Use Caching for Expensive Operations

```python
# Check cache first
cached = self.bot.db_manager.get_cached_value(cache_key, cache_type)

if cached:
    return json.loads(cached)

# Fetch and cache
data = await self.fetch_expensive_data()
self.bot.db_manager.cache_value(
    cache_key, 
    cache_type, 
    json.dumps(data),
    cache_hours=24
)
```

### 5. Offload Blocking Operations

```python
# DON'T: Block the event loop
lat, lon = geocode_city_sync(...)  # BAD

# DO: Offload to thread
lat, lon = await asyncio.to_thread(geocode_city_sync, ...)  # GOOD
```

### 6. Provide Helpful Error Messages

```python
# DON'T: Generic errors
await self.send_response(message, "Error")

# DO: Specific, actionable errors
await self.send_response(
    message,
    "Could not find location 'Seatle'. Did you mean 'Seattle'?"
)
```

### 7. Support Both DM and Channel Contexts

```python
def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
    """Allow in DM, restrict in channels."""
    if not super().can_execute(message, skip_channel_check):
        return False
    
    # DM always allowed
    if message.is_dm:
        return True
    
    # Channel-specific logic
    return self.is_channel_allowed(message)
```

### 8. Use Type Hints

```python
async def execute(self, message: MeshMessage) -> bool:
    """Execute with proper type hints."""
    result: Optional[dict] = await self.fetch_data()
    
    if result is None:
        return True
    
    temperature: float = result.get('temp', 0.0)
    # ...
```

### 9. Implement Admin-Only Commands Securely

```python
class AdminCommand(BaseCommand):
    requires_dm = True  # Admin commands should be DM-only
    
    def requires_admin_access(self) -> bool:
        """Mark as requiring admin access."""
        return True
    
    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check admin access."""
        if not super().can_execute(message, skip_channel_check):
            return False
        
        # BaseCommand handles admin pubkey verification
        return True
```

### 10. Document Your Command

```python
class MyCommand(BaseCommand):
    """Detailed description of what this command does.
    
    Includes information about:
    - What data it fetches
    - What APIs it uses
    - Any special requirements
    """
    
    # Complete metadata
    short_description = "Get data from service"
    usage = "mycommand <param> [option]"
    examples = ["mycommand test", "mycommand test --verbose"]
    parameters = [
        {"name": "param", "description": "Required parameter"},
        {"name": "option", "description": "Optional flag"}
    ]
```

---

## Testing Your Command

### Unit Testing

Create tests in `tests/commands/test_yourcommand_command.py`:

```python
import pytest
from modules.commands.yourcommand_command import YourCommand
from modules.models import MeshMessage


@pytest.fixture
def mock_bot():
    """Create mock bot instance."""
    # Implementation depends on your test framework
    pass


@pytest.fixture
def command(mock_bot):
    """Create command instance."""
    return YourCommand(mock_bot)


@pytest.mark.asyncio
async def test_execute_success(command, mock_bot):
    """Test successful execution."""
    message = MeshMessage(
        content="yourcommand test",
        sender_id="!12345678",
        is_dm=True
    )
    
    result = await command.execute(message)
    assert result is True


@pytest.mark.asyncio
async def test_execute_invalid_input(command, mock_bot):
    """Test with invalid input."""
    message = MeshMessage(
        content="yourcommand",
        sender_id="!12345678",
        is_dm=True
    )
    
    result = await command.execute(message)
    assert result is True  # Should handle gracefully
```

### Manual Testing

1. **Install your command**: Place the file in `local/commands/`
2. **Configure**: Add section to `config.ini`:
   ```ini
   [YourCommand_Command]
   enabled = true
   ```
3. **Restart bot**: The command will be auto-discovered from the local commands directory
4. **Test**: Send messages to the bot to trigger your command

### Testing Checklist

- [ ] Command responds to all keywords
- [ ] Cooldown works correctly
- [ ] DM vs. channel behavior is correct
- [ ] Error handling works (network failures, invalid input)
- [ ] Message length limits are respected
- [ ] Database operations don't cause errors
- [ ] Help text is accurate
- [ ] Translations work (if using i18n)
- [ ] Admin access works (if admin-only)
- [ ] Rate limiting prevents abuse

---

## Additional Resources

### Key Files to Reference

- `modules/commands/base_command.py` - Base class implementation
- `modules/commands/joke_command.py` - Simple API-based command example
- `modules/commands/status_command.py` - Simple admin command example
- `modules/commands/aurora_command.py` - Complex command with geocoding
- `modules/commands/wx_command.py` - Advanced command with multiple features
- `modules/db_manager.py` - Database operations
- `modules/utils.py` - Utility functions
- `modules/models.py` - Data models (MeshMessage)

### Common Utilities

```python
# From modules.utils
from modules.utils import (
    geocode_city_sync,           # Geocode city name
    geocode_zipcode_sync,        # Geocode US ZIP code
    get_config_timezone,         # Get timezone from config
    format_elapsed_display,      # Format elapsed time
    message_hop_count,           # Get hop count from message
    get_packet_hash_placeholder, # Get packet hash for display
)
```

### Configuration File Structure

```ini
[YourCommand_Command]
enabled = true
cooldown_queue_threshold_seconds = 5.0
channels = #general,#weather  # Optional: restrict to specific channels
aliases = yc, ycmd  # Optional: additional trigger words

# Custom settings
timeout = 10
max_results = 5
api_key = your_api_key_here
```

---

## Local Commands Directory

Custom commands should be placed in the `local/commands/` directory:

```
meshcore-bot/
├── modules/
│   └── commands/          # Core distributed commands (do not modify)
│       └── base_command.py
├── local/
│   └── commands/          # Your custom commands go here
│       ├── __init__.py
│       └── yourcommand_command.py
└── config.ini
```

The bot automatically discovers and loads commands from the `local/commands/` directory at startup, allowing you to extend functionality without modifying core bot files. This separation ensures your custom commands won't be overwritten during bot updates.

---

## Summary

Developing commands for MeshCore Bot involves:

1. **Create file in `local/commands/`** with your command class
2. **Inherit from BaseCommand** and set class-level metadata
3. **Use absolute imports** from `modules.*` packages
4. **Implement `execute()`** with your command logic
5. **Use `send_response()`** to reply to users
6. **Access database** via `self.bot.db_manager`
7. **Offload blocking I/O** with `asyncio.to_thread()`
8. **Handle errors gracefully** and log appropriately
9. **Test thoroughly** in both DM and channel contexts

Follow the patterns in existing commands and refer to this guide when implementing new functionality. The framework handles most of the complexity around message routing, rate limiting, and channel management, allowing you to focus on your command's core functionality.
