#!/usr/bin/env python3
"""
Default configuration template for MeshCore Bot.

Call ``create_default_config(path)`` to write config.ini when none exists.
"""

DEFAULT_CONFIG = """[Connection]
# Connection type: serial, ble, or tcp
# serial: Connect via USB serial port
# ble: Connect via Bluetooth Low Energy
# tcp: Connect via TCP/IP
connection_type = serial

# Serial port (for serial connection)
# Common ports: /dev/ttyUSB0, /dev/tty.usbserial-*, COM3 (Windows)
serial_port = /dev/ttyUSB0

# BLE device name (for BLE connection)
# Leave commented out for auto-detection, or specify exact device name
#ble_device_name = MeshCore

# TCP hostname or IP address (for TCP connection)
#hostname = 192.168.1.60
# TCP port (for TCP connection)
#tcp_port = 5000

# Connection timeout in seconds
timeout = 30

[Bot]
# Bot name for identification and logging
bot_name = MeshCoreBot

# RF Data Correlation Settings
# Time window for correlating RF data with messages (seconds)
rf_data_timeout = 15.0

# Time to wait for RF data correlation (seconds)
message_correlation_timeout = 10.0

# Enable enhanced correlation strategies
enable_enhanced_correlation = true

# Bot node ID (leave empty for auto-assignment)
node_id =

# Enable/disable bot responses
# true: Bot will respond to keywords and commands
# false: Bot will only listen and log messages
enabled = true

# Passive mode (only listen, don't respond)
# true: Bot will not send any messages
# false: Bot will respond normally
passive_mode = false

# Rate limiting in seconds between messages
# Prevents spam by limiting how often the bot can send messages
rate_limit_seconds = 2

# Bot transmission rate limit in seconds between bot messages
# Prevents bot from overwhelming the mesh network
bot_tx_rate_limit_seconds = 1.0

# Transmission delay in milliseconds before sending messages
# Helps prevent message collisions on the mesh network
# Recommended: 100-500ms for busy networks, 0 for quiet networks
tx_delay_ms = 250

# DM retry settings for improved reliability (meshcore-2.1.6+)
# Maximum number of retry attempts for failed DM sends
dm_max_retries = 3

# Maximum flood attempts (when path reset is needed)
dm_max_flood_attempts = 2

# Number of attempts before switching to flood mode
dm_flood_after = 2

# Timezone for bot operations
# Use standard timezone names (e.g., "America/New_York", "Europe/London", "UTC")
# Leave empty to use system timezone
timezone =

# Bot location for geographic proximity calculations and astronomical data
# Default latitude for bot location (decimal degrees)
# Example: 40.7128 for New York City, 48.50 for Victoria BC
bot_latitude = 40.7128

# Default longitude for bot location (decimal degrees)
# Example: -74.0060 for New York City, -123.00 for Victoria BC
bot_longitude = -74.0060

# Interval-based advertising settings
# Send periodic flood adverts at specified intervals
# 0: Disabled (default)
# >0: Send flood advert every N hours
advert_interval_hours = 0

# Send startup advert when bot finishes initializing
# false: No startup advert (default)
# zero-hop: Send local broadcast advert
# flood: Send network-wide flood advert
startup_advert = false

# Auto-manage contact list when new contacts are discovered
# device: Device handles auto-addition using standard auto-discovery mode, bot manages contact list capacity (purge old contacts when near limits)
# bot: Bot automatically adds new companion contacts to device, bot manages contact list capacity (purge old contacts when near limits)
# false: Manual mode - no automatic actions, use !repeater commands to manage contacts (default)
auto_manage_contacts = false

[Admin_ACL]
# Admin Access Control List (ACL) for restricted commands
# Only users with public keys listed here can execute admin commands
# Format: comma-separated list of public keys (without spaces)
# Example: f5d2b56d19b24412756933e917d4632e088cdd5daeadc9002feca73bf5d2b56d,another_key_here
admin_pubkeys =

# Commands that require admin access (comma-separated)
# These commands will only work for users in the admin_pubkeys list
admin_commands = repeater

[Keywords]
# Keyword-response pairs (keyword = response format)
# Available fields: {sender}, {connection_info}, {snr}, {rssi}, {timestamp}, {path}, {path_distance}, {firstlast_distance}
# {sender}: Name/ID of message sender
# {connection_info}: Path info, SNR, and RSSI combined (e.g., "01,5f (2 hops) | SNR: 15 dB | RSSI: -120 dBm")
# {snr}: Signal-to-noise ratio in dB
# {rssi}: Received signal strength indicator in dBm
# {timestamp}: Message timestamp in HH:MM:SS format
# {path}: Message routing path (e.g., "01,5f (2 hops)")
# {hops}: Total hop count only (e.g., "2" or "0"); same value as in path/connection_info
# {hops_label}: Same as hops with "hop"/"hops" and pluralization (e.g., "1 hop", "2 hops")
# {path_distance}: Total distance between all hops in path with locations (e.g., "123.4km (3 segs, 1 no-loc)")
# {firstlast_distance}: Distance between first and last repeater in path (e.g., "45.6km" or empty if locations missing)
test = "ack [@{sender}]{phrase_part} | {connection_info} | Received at: {timestamp}"
ping = "Pong!"
pong = "Ping!"
help = "Bot Help: test, ping, help, hello, cmd, advert, t phrase, @string, wx, aqi, sun, moon, solar, hfcond, satpass | Use 'help <command>' for details"
cmd = "Available commands: test, ping, help, hello, cmd, advert, t phrase, @string, wx, aqi, sun, moon, solar, hfcond, satpass"

[Channels]
# Channels to monitor (comma-separated)
# Bot will only respond to messages on these channels
# Use exact channel names as configured on your MeshCore node
monitor_channels = general,test,emergency

# Enable DM responses
# true: Bot will respond to direct messages
# false: Bot will ignore direct messages
respond_to_dms = true

[Banned_Users]
# List of banned sender names (comma-separated). Matching is prefix (starts-with):
# "Awful Username" also matches "Awful Username 🍆". No bot responses in channels or DMs.
banned_users =

[Feed_Manager]
# Enable or disable RSS/API feed subscriptions
# true: Feed manager polls configured feeds and sends updates to channels
# false: Feed manager disabled (default)
feed_manager_enabled = false

[Scheduled_Messages]
# Scheduled message format: HHMM = channel:message
# Time format: HHMM (24-hour, no colon)
# Bot will send these messages at the specified times daily
0800 = general:Good morning! Bot is online and ready.
1200 = general:Midday status check - all systems operational.
1800 = general:Evening update - bot status: Good

[Logging]
# Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL
# DEBUG: Most verbose, shows all details
# INFO: Standard logging level
# WARNING: Only warnings and errors
# ERROR: Only errors
# CRITICAL: Only critical errors
log_level = INFO

# Log file path (leave empty for console only)
# Bot will write logs to this file in addition to console
# Use absolute path for Docker compatibility (e.g., /data/logs/meshcore_bot.log)
# Relative paths will resolve relative to the config file directory
log_file = meshcore_bot.log

# Enable colored console output
# true: Use colors in console output
# false: Plain text output
colored_output = true

# MeshCore library log level (separate from bot log level)
# Controls debug output from the meshcore library itself
# Options: DEBUG, INFO, WARNING, ERROR, CRITICAL
meshcore_log_level = INFO

[Custom_Syntax]
# Custom syntax patterns for special message formats
# Format: pattern = "response_format"
# Available fields: {sender}, {phrase}, {connection_info}, {snr}, {timestamp}, {path}
# {phrase}: The text after the trigger (for t_phrase syntax)
#
# Special syntax: Messages starting with "t " or "T " followed by a phrase
# Example: "t hello world" -> "ack {sender}: hello world | {connection_info}"
t_phrase = "ack {sender}: {phrase} | {connection_info}"


[External_Data]
# Weather API key (future feature)
weather_api_key =

# Weather update interval in seconds (future feature)
weather_update_interval = 3600

# Tide API key (future feature)
tide_api_key =

# Tide update interval in seconds (future feature)
tide_update_interval = 1800

# N2YO API key for satellite pass information
# Get free key at: https://www.n2yo.com/login/
n2yo_api_key =

# AirNow API key for AQI data
# Get free key at: https://docs.airnowapi.org/
airnow_api_key =

# Repeater prefix API URL for prefix command
# Leave empty to disable prefix command functionality
# Configure your own regional API endpoint
repeater_prefix_api_url =

# Repeater prefix cache duration in hours
# How long to cache prefix data before refreshing from API
# Recommended: 1-6 hours (data doesn't change frequently)
repeater_prefix_cache_hours = 1

[Prefix_Command]
# Enable or disable repeater geolocation in prefix command
# true: Show city names with repeaters when location data is available
# false: Show only repeater names without location information
show_repeater_locations = true

# Use reverse geocoding for coordinates without city names
# true: Automatically look up city names from GPS coordinates
# false: Only show coordinates if no city name is available
use_reverse_geocoding = true

# Hide prefix source information
# true: Hide "Source: domain.com" line from prefix command output
# false: Show source information (default)
hide_source = false

# Prefix heard time window (days)
# Number of days to look back when showing prefix results (default command behavior)
# Only repeaters heard within this window will be shown by default
# Use "prefix XX all" to show all repeaters regardless of time
prefix_heard_days = 7

# Prefix free time window (days)
# Number of days to look back when determining which prefixes are "free"
# Only repeaters heard within this window will be considered as using a prefix
# Repeaters not heard in this window will be excluded from used prefixes list
prefix_free_days = 30

[Weather]
# Default state for city name disambiguation
# When users type "wx seattle", it will search for "seattle, WA, USA"
# Use 2-letter state abbreviation (e.g., WA, CA, NY, TX)
default_state = WA

# Default country for city name disambiguation (for international weather plugin)
# Use 2-letter country code (e.g., US, CA, GB, AU)
default_country = US

# Temperature unit for weather display
# Options: fahrenheit, celsius
# Default: fahrenheit
temperature_unit = fahrenheit

# Wind speed unit for weather display
# Options: mph, kmh, ms (meters per second)
# Default: mph
wind_speed_unit = mph

# Precipitation unit for weather display
# Options: inch, mm
# Default: inch
precipitation_unit = inch

[Path_Command]
# Optional prefix on path command replies: {sender}, {connection_info}, {path}, {timestamp}, {snr}, {rssi}
# reply_prefix =
# Bytes per hop before repeater name lookup (0/1 = always; 2/3 = gate to hex + tip if shorter)
# minimum_path_bytes = 0
# Geographic proximity calculation method
# simple: Use proximity to bot location (default)
# path: Use proximity to previous/next nodes in the path for more realistic routing
proximity_method = simple

# Enable path proximity fallback
# When path proximity can't be calculated (missing location data), fall back to simple proximity
# true: Fall back to bot location proximity when path data unavailable
# false: Show collision warning when path proximity unavailable
path_proximity_fallback = true

# Maximum range for geographic proximity guessing (kilometers)
# Repeaters beyond this distance will have reduced confidence or be rejected
# Set to 0 to disable range limiting
max_proximity_range = 200

# Maximum age for repeater data in path matching (days)
# Only include repeaters that have been heard within this many days
# Helps filter out stale or inactive repeaters from path decoding
# Set to 0 to disable age filtering
max_repeater_age_days = 14

# Confidence indicator symbols for path command
# High confidence (>= 0.9): Shows when path decoding is very reliable
high_confidence_symbol = 🎯

# Medium confidence (>= 0.8): Shows when path decoding is reasonably reliable
medium_confidence_symbol = 📍

# Low confidence (< 0.8): Shows when path decoding has uncertainty
low_confidence_symbol = ❓

[Solar_Config]
# URL timeout for external API calls (seconds)
url_timeout = 10

# Use Zulu/UTC time for astronomical data
# true: Use 24-hour UTC format
# false: Use 12-hour local format
use_zulu_time = false

[Joke_Command]
# Enable or disable the joke command (true/false)
enabled = true

# Enable seasonal joke defaults (October: spooky, December: Christmas)
# true: Seasonal defaults are applied (default)
# false: No seasonal defaults (always random)
seasonal_jokes = true

# Handle long jokes (over 130 characters)
# false: Fetch new jokes until we get a short one (default)
# true: Split long jokes into multiple messages
long_jokes = false

[DadJoke_Command]
# Enable or disable the dad joke command (true/false)
enabled = true

# Handle long jokes (over 130 characters)
# false: Fetch new jokes until we get a short one (default)
# true: Split long jokes into multiple messages
long_jokes = false

"""


def create_default_config(config_file: str) -> None:
    """Write DEFAULT_CONFIG to ``config_file``.

    Uses print() instead of logger since this is called before logging is set up.
    """
    with open(config_file, 'w') as f:
        f.write(DEFAULT_CONFIG)
    print(f"Created default config file: {config_file}")
