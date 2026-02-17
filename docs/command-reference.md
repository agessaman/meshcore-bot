# MeshCore Bot Commands

This document provides a comprehensive list of all available commands in the MeshCore Bot, with examples and detailed explanations.

## Table of Contents

- [Basic Commands](#basic-commands)
- [Information Commands](#information-commands)
- [Emergency Commands](#emergency-commands)
- [Gaming Commands](#gaming-commands)
- [Entertainment Commands](#entertainment-commands)
- [Sports Commands](#sports-commands)
- [MeshCore Utility Commands](#meshcore-utility-commands)
- [Admin Commands](#admin-commands)

---

## Basic Commands

### `test` or `t`

Test message response to verify bot connectivity and functionality.

**Usage:**
- `test` - Basic test response
- `test <phrase>` - Test with optional phrase

**Examples:**
```
test
t hello world
```

**Response:** Confirms message receipt with connection information.

---

### `ping`

Simple ping/pong response to test bot responsiveness.

**Usage:**
```
ping
```

**Response:** `Pong!`

---

### `help`

Show available commands or get detailed help for a specific command.

**Usage:**
- `help` - List all available commands
- `help <command>` - Get detailed help for a specific command

**Examples:**
```
help
help wx
help repeater
```

**Response:** Lists commands or provides detailed information about the specified command.

---

### `hello`

Greeting response. Also responds to various greeting keywords.

**Usage:**
```
hello
hi
hey
howdy
greetings
```

**Response:** Friendly greeting message.

---

### `cmd`

List available commands in compact format.

**Usage:**
```
cmd
```

**Response:** Compact list of all available commands.

---

## Information Commands

### `channels`

List and manage hashtag channels on the MeshCore network.

**Usage:**
- `channels` - List general channels
- `channels list` - List all channel categories
- `channels <category>` - List channels in a specific category
- `channels #channel` - Get information about a specific channel

**Examples:**
```
channels
channels list
channels emergency
channels #general
```

**Response:** Lists available channels with their descriptions and usage statistics.

---

### `wx <zipcode>`

Get weather information for a US zip code using NOAA data.

**Aliases:** `weather`, `wxa`, `wxalert`

**Usage:**
```
wx <zipcode>
weather <zipcode>
wxa <zipcode>
wxalert <zipcode>
```

**Examples:**
```
wx 98101
weather 90210
wxa 10001
```

**Response:** Current weather conditions, forecast for tonight/tomorrow, and active weather alerts. Includes:
- Current conditions (temperature, humidity, wind, etc.)
- Short-term forecast (tonight, tomorrow)
- Weather alerts if any are active

**Note:** Weather alerts are automatically included when available.

---

### `gwx <location>`

Get global weather information for any location worldwide using Open-Meteo API.

**Aliases:** `globalweather`, `gwxa`

**Usage:**
```
gwx <location>
globalweather <location>
gwxa <location>
```

**Examples:**
```
gwx Tokyo
gwx Paris, France
gwx London
globalweather New York
gwx 35.6762,139.6503
```

**Response:** Current weather conditions and forecast for the specified location, including:
- Current temperature and conditions
- Feels-like temperature
- Wind speed and direction
- Humidity
- Dew point
- Visibility
- Pressure
- Forecast for today/tonight and tomorrow

---

### `aqi <location>`

Get Air Quality Index (AQI) information for a location.

**Usage:**
```
aqi <location>
aqi <city>
aqi <city>, <state>
aqi <city>, <country>
aqi <latitude>,<longitude>
aqi help
```

**Examples:**
```
aqi seattle
aqi greenwood
aqi vancouver canada
aqi 47.6,-122.3
aqi help
```

**Response:** Air quality data including:
- US AQI value and category
- European AQI (if available)
- Pollutant concentrations (PM2.5, PM10, Ozone, etc.)
- Health recommendations

---

### `airplanes [location] [options]` / `overhead [lat,lon]`

Get aircraft tracking information using ADS-B data from airplanes.live or compatible APIs.

**Aliases:** `aircraft`, `planes`, `adsb`, `overhead`

**Usage:**
```
airplanes
airplanes here
airplanes <latitude>,<longitude>
airplanes [location] [options]
overhead
overhead <latitude>,<longitude>
```

**Special Command: `overhead`**
- Returns the single closest aircraft directly overhead
- Uses companion location from database (if available)
- If companion location not available, prompts user to specify coordinates
- Always returns one aircraft sorted by distance (closest first)

**Location Options:**
- No location: Uses companion location (if available), otherwise bot location
- `here`: Uses bot location from config
- `<lat>,<lon>`: Uses specified coordinates (e.g., `47.6,-122.3`)

**Filter Options:**
- `radius=<nm>` - Search radius in nautical miles (default: 25, max: 250)
- `alt=<min>-<max>` - Filter by altitude range in feet (e.g., `alt=1000-5000`)
- `speed=<min>-<max>` - Filter by ground speed range in knots
- `type=<code>` - Filter by aircraft type code (e.g., `B738`, `A321`)
- `callsign=<pattern>` - Filter by callsign pattern
- `military` - Show only military aircraft
- `ladd` - Show only LADD aircraft
- `pia` - Show only PIA aircraft
- `squawk=<code>` - Filter by transponder squawk code
- `limit=<n>` - Limit number of results (default: 10, max: 50)
- `closest` - Sort by distance (closest first)
- `highest` - Sort by altitude (highest first)
- `fastest` - Sort by speed (fastest first)

**Examples:**
```
airplanes
airplanes here
airplanes 47.6,-122.3
airplanes radius=50
airplanes alt=10000-40000
airplanes type=B738
airplanes military
airplanes callsign=UAL limit=5
airplanes 47.6,-122.3 radius=25 closest
```

**Response:**
- **Single aircraft**: Detailed format with callsign, type, altitude, speed, track, distance, bearing, vertical rate, and registration
- **Multiple aircraft**: Compact list format with callsign, altitude, speed, distance, and bearing

**Configuration:**
The command can be configured in `config.ini` under `[Airplanes_Command]`:
- `enabled` - Enable/disable the command
- `api_url` - API endpoint URL (default: `http://api.airplanes.live/v2/`)
- `default_radius` - Default search radius in nautical miles
- `max_results` - Maximum number of results to return
- `url_timeout` - API request timeout in seconds

**Note:** Uses companion location from database if available, otherwise falls back to bot location from config. The API is rate-limited to 1 request per second.

---

### `sun`

Get sunrise and sunset times for the bot's configured location.

**Usage:**
```
sun
```

**Response:** Sunrise and sunset times, day length, and solar noon.

**Note:** Uses the bot's configured default location (`bot_latitude` and `bot_longitude` in config.ini), not the user's location from their advert.

---

### `moon`

Get moon phase information and moonrise/moonset times for the bot's configured location.

**Usage:**
```
moon
```

**Response:** Current moon phase, moonrise/moonset times, and illumination percentage.

**Note:** Uses the bot's configured default location (`bot_latitude` and `bot_longitude` in config.ini), not the user's location from their advert.

---

### `solar`

Get solar conditions and HF band status.

**Usage:**
```
solar
```

**Response:** Solar activity information including:
- Solar flux
- Sunspot number
- A-index and K-index
- HF band conditions (Open/Closed/Marginal)
- Solar activity summary

---

### `solarforecast` or `sf`

Get solar panel production forecast for a location.

**Usage:**
```
sf <location|repeater_name|coordinates|zipcode> [panel_size] [azimuth] [angle]
solarforecast <location> [panel_size] [azimuth] [angle]
```

**Parameters:**
- `location` - Location name, repeater name, coordinates (lat,lon), or zipcode
- `panel_size` - Panel size in watts (optional, default: 100W)
- `azimuth` - Panel azimuth in degrees, 0=south (optional, default: 180)
- `angle` - Panel tilt angle in degrees (optional, default: 30)

**Examples:**
```
sf seattle
sf seattle 200
sf seattle 200 180 45
sf 47.6,-122.3 150
sf repeater1 100 180 30
```

**Response:** Solar panel production forecast including:
- Daily production estimate
- Hourly production breakdown
- Peak production time
- Total daily kWh estimate

---

### `hfcond`

Get HF band conditions for amateur radio.

**Usage:**
```
hfcond
```

**Response:** HF band conditions including:
- Band status (Open/Closed/Marginal)
- Solar flux
- A-index and K-index
- Propagation conditions

---

### `satpass <NORAD>`

Get satellite pass information for a satellite by NORAD ID.

**Usage:**
- `satpass <NORAD>` - Get radio passes (all passes above horizon)
- `satpass <NORAD> visual` - Get visual passes only (must be visually observable)
- `satpass <shortcut>` - Use predefined shortcuts

**Shortcuts:**
- **Weather Satellites:** `noaa15`, `noaa18`, `noaa19`, `metop-a`, `metop-b`, `metop-c`, `goes16`, `goes17`, `goes18`
- **Space Stations:** `iss`, `tiangong`, `tiangong1`, `tiangong2`
- **Telescopes:** `hst`, `hubble`
- **Other:** `starlink`

**Examples:**
```
satpass 25544
satpass iss
satpass iss visual
satpass noaa19
satpass hubble visual
```

**Response:** Upcoming satellite passes including:
- Pass start/end times
- Maximum elevation
- Duration
- Azimuth at start/end
- Visual pass indicator (if applicable)

**Note:** Requires `n2yo_api_key` to be configured in `config.ini`.

---

## Emergency Commands

### `alert <location> [all]`

Get active emergency incidents for a location.

**Usage:**
```
alert <city|zipcode|street city|lat,lon|county> [all]
```

**Parameters:**
- `location` - City name, zipcode, street address with city, coordinates, or county name
- `all` - Show all incidents (default: shows most relevant incidents)

**Examples:**
```
alert seattle
alert 98101
alert main street seattle
alert 47.6,-122.3
alert seattle all
alert king county
```

**Response:** Active emergency incidents including:
- Incident type and description
- Location
- Agency
- Time
- Severity level

**Note:** Requires `Alert_Command` configuration in `config.ini` with agency IDs for your area.

---

## Gaming Commands

### `dice`

Roll dice with various configurations.

**Usage:**
- `dice` - Roll a standard 6-sided die (d6)
- `dice d<N>` - Roll a die with N sides
- `dice <X>d<N>` - Roll X dice with N sides each

**Examples:**
```
dice
dice d20
dice 2d6
dice 3d10
```

**Response:** Shows the dice roll result(s) and total.

---

### `roll`

Roll a random number within a specified range.

**Usage:**
- `roll` - Roll a number between 1 and 100
- `roll <max>` - Roll a number between 1 and max

**Examples:**
```
roll
roll 50
roll 1000
```

**Response:** Shows the random number result.

---

## Entertainment Commands

### `joke`

Get a random joke from various categories.

**Usage:**
- `joke` - Get a random joke
- `joke <category>` - Get a joke from a specific category

**Examples:**
```
joke
joke programming
joke pun
```

**Response:** A random joke from the selected category.

---

### `dadjoke`

Get a dad joke from icanhazdadjoke.com.

**Usage:**
```
dadjoke
```

**Response:** A random dad joke.

---

### `hacker`

Responds to Linux/Unix commands with supervillain mainframe error messages.

**Usage:**
```
sudo
ps aux
grep
ls -l
cat
rm -rf
```

**Examples:**
```
sudo make me a sandwich
ps aux | grep evil
ls -l /secret/base
```

**Response:** Humorous error messages in the style of a supervillain's mainframe system.

---

## Sports Commands

### `sports`

Get sports scores for configured teams or leagues.

**Usage:**
- `sports` - Get scores for default teams
- `sports <team>` - Get scores for a specific team
- `sports <league>` - Get scores for a league (nfl, mlb, nba, nhl, etc.)

**Examples:**
```
sports
sports seahawks
sports nfl
sports mlb
```

**Response:** Current scores and game information for the requested teams or league.

---

## MeshCore Utility Commands

### `path` or `decode` or `route`

Decode and display the routing path of a message.

**Usage:**
```
path
decode
route
```

**Response:** Shows the complete routing path the message took through the mesh network, including all intermediate nodes.

---

### `prefix <XX>`

Look up repeaters by two-character prefix.

**Usage:**
- `prefix <XX>` - Look up repeaters with the specified prefix
- `prefix <XX> all` - Include all repeaters (not just active ones)
- `prefix refresh` - Refresh the prefix cache
- `prefix free` or `prefix available` - Show available prefixes

**Examples:**
```
prefix 1A
prefix 2B all
prefix refresh
prefix free
```

**Response:** List of repeaters matching the prefix, including:
- Repeater name
- Status (active/inactive)
- Last seen time
- Location (if available)

---

### `stats`

Show bot usage statistics for the past 24 hours.

**Usage:**
- `stats` - Overall statistics
- `stats messages` - Message statistics
- `stats channels` - Channel statistics
- `stats paths` - Path statistics

**Examples:**
```
stats
stats messages
stats channels
stats paths
```

**Response:** Usage statistics including:
- Total messages processed
- Commands executed
- Channel activity
- Routing path information

---

### `multitest` or `mt`

Listen for 6 seconds and collect all unique paths from incoming messages.

**Usage:**
```
multitest
mt
```

**Response:** List of all unique routing paths discovered during the 6-second listening period.

---

## Command Syntax

### Prefix

Commands can be used with or without the `!` prefix:
- `test` or `!test` - Both work
- `wx 98101` or `!wx 98101` - Both work

### Direct Messages

Some commands work in both public channels and direct messages, while admin commands typically require direct messages for security.

### Rate Limiting

The bot implements rate limiting to prevent spam. If you send commands too quickly, you may receive a rate limit message. Wait a few seconds before trying again.

### Case Sensitivity

Most commands are case-insensitive:
- `TEST` and `test` work the same
- `WX 98101` and `wx 98101` work the same

### Getting Help

Use the `help` command to get more information:
- `help` - List all commands
- `help <command>` - Get detailed help for a specific command

---

## Additional Notes

### Location Requirements

Some commands use location data:
- **`sun` and `moon`** - Use the bot's configured default location (`bot_latitude` and `bot_longitude` in config.ini), not the user's location from their advert
- **`solar`** - Does not require location (provides global solar conditions)
- **`solarforecast`** - Requires a location parameter (location name, repeater name, coordinates, or zipcode)
- **`wx`** - Requires a zipcode parameter
- **`gwx`** - Requires a location parameter
- **`aqi`** - Requires a location parameter

### API Keys

Some commands require API keys to be configured in `config.ini`:
- `satpass` - Requires `n2yo_api_key`
- `aqi` - Requires `airnow_api_key` (optional, uses fallback if not configured)

### Weather Data

- `wx` uses NOAA API (US locations only)
- `gwx` uses Open-Meteo API (global locations)
- Both provide current conditions and forecasts

### Command Categories

Commands are organized into categories for easier discovery:
- **Basic** - Essential commands for testing and help
- **Information** - Weather, astronomy, and data queries
- **Emergency** - Emergency and safety information
- **Gaming** - Fun commands for games and random numbers
- **Entertainment** - Jokes and humorous responses
- **Sports** - Sports scores and information
- **MeshCore Utility** - Network-specific utilities
- **Admin** - Administrative commands (DM only)

---

## Admin Commands

**Note:** Admin commands are only available via Direct Message (DM) and may require ACL permissions.

### `repeater` or `repeaters` or `rp`

Manage repeater contacts and contact list capacity.

**Usage:**
```
repeater <subcommand> [options]
repeaters <subcommand> [options]
rp <subcommand> [options]
```

#### Repeater Management Subcommands

- `scan` - Scan current contacts and catalog new repeaters
- `list` - List repeater contacts (use `--all` to show purged ones)
- `locations` - Show location data status for repeaters
- `update-geo` - Update missing geolocation data (state/country) from coordinates
  - `update-geo dry-run` - Preview what would be updated without making changes
  - `update-geo <N>` - Update up to N repeaters (default: 10)
  - `update-geo dry-run <N>` - Preview updates for up to N repeaters
- `purge all` - Purge all repeaters
- `purge all force` - Force purge all repeaters (uses multiple removal methods)
- `purge <days>` - Purge repeaters older than specified days
- `purge <name>` - Purge specific repeater by name
- `restore <name>` - Restore a previously purged repeater
- `stats` - Show repeater management statistics

#### Contact List Management Subcommands

- `status` - Show contact list status and limits
- `manage` - Manage contact list to prevent hitting limits
- `manage --dry-run` - Show what management actions would be taken
- `add <name> [key]` - Add a discovered contact to contact list
- `auto-purge` - Show auto-purge status and controls
- `auto-purge trigger` - Manually trigger auto-purge
- `auto-purge enable/disable` - Enable/disable auto-purge
- `purge-status` - Show detailed purge status and recommendations
- `test-purge` - Test the improved purge system
- `discover` - Discover companion contacts
- `auto <on|off>` - Toggle manual contact addition setting
- `test` - Test meshcore-cli command functionality

**Examples:**
```
repeater scan
repeater status
repeater manage
repeater manage --dry-run
repeater add "John"
repeater discover
repeater auto-purge
repeater auto-purge trigger
repeater purge-status
repeater purge all
repeater purge 30
repeater stats
```

**Response:** Varies by subcommand. Provides information about repeater contacts, contact list capacity, and management actions.

**Note:** This system helps manage both repeater contacts and overall contact list capacity. It automatically removes stale contacts and old repeaters when approaching device limits.

**Automatic Features:**
- NEW_CONTACT events are automatically monitored
- Repeaters are automatically cataloged when discovered
- Contact list capacity is monitored in real-time
- `auto_manage_contacts = device`: Device handles auto-addition, bot manages capacity
- `auto_manage_contacts = bot`: Bot automatically adds companion contacts and manages capacity
- `auto_manage_contacts = false`: Manual mode - use `!repeater` commands to manage contacts

---

### `advert`

Send a network flood advert to announce the bot's presence on the mesh network.

**Usage:**
```
advert
```

**Response:** Confirms that the advert was sent.

**Note:** 
- DM only command
- 1-hour cooldown period between uses
- Sends a flood advert to all nodes on the network

---

### `feed`

Manage RSS feed and API feed subscriptions (Admin only).

**Usage:**
```
feed <subcommand> [options]
```

#### Subcommands

- `subscribe <rss|api> <url> <channel> [name]` - Subscribe to a feed
- `unsubscribe <id|url> <channel>` - Unsubscribe from a feed
- `list [channel]` - List all subscriptions
- `status <id>` - Get status of a specific subscription
- `test <url>` - Test a feed URL
- `enable <id>` - Enable a subscription
- `disable <id>` - Disable a subscription
- `update <id> [interval_seconds]` - Update subscription interval

**Examples:**
```
feed subscribe rss https://alerts.example.com/rss emergency "Emergency Alerts"
feed subscribe api https://api.example.com/alerts emergency "API Alerts" '{"headers": {"Authorization": "Bearer TOKEN"}}'
feed list
feed status 1
feed test https://example.com/feed.xml
```

**Response:** Confirmation of the action or list of subscriptions.

**Note:** Admin access required. Feeds are automatically checked and posted to the specified channel.

---

For more information about configuring the bot, see the main [README](https://github.com/agessaman/meshcore-bot/blob/main/README.md) file.

