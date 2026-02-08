# Feed Management

The Feed Management system allows the bot to subscribe to RSS feeds and REST APIs, automatically polling for new content and posting updates to specified mesh channels.

## Overview

The feed manager supports two feed types:
- **RSS Feeds**: Standard RSS/Atom feeds
- **API Feeds**: REST API endpoints returning JSON data

Both feed types support:
- Configurable polling intervals
- Custom message formatting
- Item filtering
- Sorting
- Automatic deduplication
- Rate limiting

## Configuration

### Global Settings

Configure feed manager behavior in `config.ini`:

```ini
[Feed_Manager]
# Enable/disable feed manager
feed_manager_enabled = true

# Default check interval (seconds)
default_check_interval_seconds = 300

# Maximum items to process per check
max_items_per_check = 10

# HTTP request timeout (seconds)
feed_request_timeout = 30

# User agent for HTTP requests
feed_user_agent = MeshCoreBot/1.0 FeedManager

# Rate limit between requests to same domain (seconds)
feed_rate_limit_seconds = 5.0

# Maximum message length (characters)
max_message_length = 130

# Default output format
default_output_format = {emoji} {body|truncate:100} - {date}\n{link|truncate:50}

# Default interval between sending queued messages (seconds)
default_message_send_interval_seconds = 2.0
```

## RSS Feed Configuration

The web interface provides separate input fields for each configuration option. Below are examples showing the values to enter in each field.

### Basic RSS Feed

**Feed Type:** `rss`  
**Feed URL:** `https://example.com/rss.xml`  
**Channel:** `#alerts`  
**Feed Name (Optional):** `Example RSS Feed`  
**Check Interval (seconds):** `300`  
**Output Format:** (leave empty to use default)  
**Message Send Interval (seconds):** `2.0`  
**Filter Configuration:** (leave empty)  
**Sort Configuration:** (leave empty)

### RSS Feed with Custom Format

**Feed Type:** `rss`  
**Feed URL:** `https://example.com/rss.xml`  
**Channel:** `#alerts`  
**Feed Name (Optional):** `Emergency Alerts`  
**Check Interval (seconds):** `60`  
**Output Format:**
```
{emoji} {title|truncate:80}
{body|truncate:100}
{date}
```
**Message Send Interval (seconds):** `2.0`  
**Filter Configuration:** (leave empty)  
**Sort Configuration:** (leave empty)

## API Feed Configuration

### Basic API Feed

**Feed Type:** `api`  
**Feed URL:** `https://api.example.com/alerts`  
**Channel:** `#alerts`  
**Feed Name (Optional):** `API Alerts`  
**Check Interval (seconds):** `300`  
**Output Format:** (leave empty to use default)  
**Message Send Interval (seconds):** `2.0`  
**API Configuration (JSON):**
```json
{
  "method": "GET",
  "headers": {},
  "params": {
    "api_key": "your-api-key"
  },
  "response_parser": {
    "items_path": "data.alerts",
    "id_field": "id",
    "title_field": "title",
    "description_field": "description",
    "timestamp_field": "created_at"
  }
}
```
**Filter Configuration:** (leave empty)  
**Sort Configuration:** (leave empty)

### WSDOT Highway Alerts Example

**Feed Type:** `api`  
**Feed URL:** `https://wsdot.wa.gov/Traffic/api/HighwayAlerts/HighwayAlertsREST.svc/GetAlertsAsJson`  
**Channel:** `#traffic`  
**Feed Name (Optional):** `WSDOT Highway Alerts`  
**Check Interval (seconds):** `300`  
**Output Format:**
```
{emoji} [{raw.Priority|switch:highest:🔴:high:🟠:medium:🟡:⚪}] {title|truncate:80}
{raw.EventCategory} | {raw.Region} | {raw.EventStatus}
{body|truncate:70}
```
**Message Send Interval (seconds):** `2.0`  
**API Configuration (JSON):**
```json
{
  "method": "GET",
  "headers": {},
  "params": {
    "AccessCode": "your-access-code"
  },
  "response_parser": {
    "items_path": "",
    "id_field": "AlertID",
    "title_field": "HeadlineDescription",
    "description_field": "ExtendedDescription",
    "timestamp_field": "LastUpdatedTime"
  }
}
```
**Filter Configuration (JSON):**
```json
{
  "conditions": [
    {
      "field": "raw.EventCategory",
      "operator": "in",
      "values": ["Alert", "Closure"]
    }
  ],
  "logic": "OR"
}
```
**Sort Configuration (JSON):**
```json
{
  "field": "raw.LastUpdatedTime",
  "order": "desc"
}
```

## Output Format

The output format string controls how feed items are formatted before sending to channels.

### Placeholders

- `{title}` - Item title
- `{body}` - Item description/body text
- `{date}` - Relative time (e.g., "5m ago", "2h 30m ago")
- `{link}` - Item URL
- `{emoji}` - Auto-selected emoji based on feed name (📢, 🚨, ⚠️, ℹ️)
- `{raw.field}` - Access raw API data fields (API feeds only)
- `{raw.nested.field}` - Access nested API fields (e.g., `{raw.StartRoadwayLocation.RoadName}`)

### Shortening Functions

Apply functions to placeholders using the pipe operator:

- `{field|truncate:N}` - Truncate to N characters
- `{field|word_wrap:N}` - Wrap at N characters, breaking at word boundaries
- `{field|first_words:N}` - Take first N words

**Examples:**
```
{title|truncate:60}
{body|word_wrap:100}
{body|first_words:20}
```

### Regex Extraction

Extract specific content using regex patterns:

- `{field|regex:pattern}` - Extract using regex (uses first capture group)
- `{field|regex:pattern:group}` - Extract specific capture group (0 = whole match, 1 = first group, etc.)

**Examples:**
```
{body|regex:Temperature:\s*([^\n]+):1}
{body|regex:Conditions:\s*([^\n]+):1}
```

### Conditional Formatting

- `{field|if_regex:pattern:then:else}` - If pattern matches, return "then", else return "else"
- `{field|switch:value1:result1:value2:result2:...:default}` - Multi-value conditional

**Examples:**
```
{raw.Priority|switch:highest:🔴:high:🟠:medium:🟡:⚪}
{body|if_regex:No restrictions:👍:Restrictions apply}
```

### Extract and Check

- `{field|regex_cond:extract_pattern:check_pattern:then:group}` - Extract text, check if it matches pattern, return "then" if match, else return extracted text

**Example:**
```
{body|regex_cond:Northbound\s*\n([^\n]+):No restrictions:👍:1}
```

## Filter Configuration

Filter configuration determines which items are sent to channels.

### Filter Structure

```json
{
  "conditions": [
    {
      "field": "raw.Priority",
      "operator": "in",
      "values": ["highest", "high"]
    },
    {
      "field": "raw.EventStatus",
      "operator": "equals",
      "value": "open"
    }
  ],
  "logic": "AND"
}
```

### Operators

- `equals` - Exact match
- `not_equals` - Not equal
- `in` - Value in list
- `not_in` - Value not in list
- `matches` - Regex match
- `not_matches` - Regex does not match
- `contains` - String contains value
- `not_contains` - String does not contain value

### Logic

- `AND` - All conditions must match (default)
- `OR` - Any condition matches

### Field Paths

For API feeds, use `raw.field` or `raw.nested.field` to access API response fields:
- `raw.Priority`
- `raw.EventStatus`
- `raw.StartRoadwayLocation.RoadName`

## Sort Configuration

Sort items before processing:

```json
{
  "field": "raw.LastUpdatedTime",
  "order": "desc"
}
```

### Sort Options

- `field` - Field path to sort by (e.g., `raw.LastUpdatedTime`, `raw.Priority`, `published`)
- `order` - `asc` (ascending) or `desc` (descending)

### Date Format Support

The sort function supports:
- ISO format dates
- Microsoft JSON date format: `/Date(timestamp-offset)/` (e.g., WSDOT API)
- Unix timestamps
- Common date string formats

## Message Queuing

Messages are queued and sent at configured intervals to prevent rate limiting:

- `message_send_interval_seconds` - Time between sending messages from the same feed (default: 2.0 seconds)
- Messages are automatically queued and processed in order
- Each feed maintains its own send interval

## Deduplication

The system automatically prevents duplicate posts:

- Items are tracked by ID in the database
- Previously processed items are skipped
- Works correctly even when sorting changes item order
- Database-backed deduplication ensures reliability across restarts

## Rate Limiting

The feed manager implements rate limiting:

- Per-domain rate limiting (default: 5 seconds between requests to same domain)
- Configurable via `feed_rate_limit_seconds`
- Prevents overwhelming feed sources

## Web Interface

The feed management system includes a web interface accessible at `/feeds`:

- View all feed subscriptions
- Add/edit/delete feeds
- Preview output format with live feed data
- View feed statistics and activity
- Monitor errors

## Command Interface

Feeds can be managed via mesh commands. The feed command requires admin access and must be sent as a direct message (DM) to the bot. The command is enabled by default.

**Command Format:** `feed <subcommand> [arguments]` (DM only)

### Available Commands

- `feed subscribe <rss|api> <url> <channel> [name] [api_config]` - Subscribe to a feed
- `feed unsubscribe <id|url> [channel]` - Unsubscribe from a feed (by ID or URL)
- `feed list [channel]` - List all feed subscriptions (optionally filtered by channel)
- `feed status <id>` - Show detailed status for a feed
- `feed enable <id>` - Enable a feed subscription
- `feed disable <id>` - Disable a feed subscription
- `feed update <id> [interval_seconds]` - Update feed settings
- `feed test <url>` - Test/validate a feed URL

### Examples

```
feed subscribe rss https://alerts.example.com/rss emergency "Emergency Alerts"
feed subscribe api https://api.example.com/alerts emergency "API Alerts" '{"headers": {"Authorization": "Bearer TOKEN"}}'
feed list
feed list #alerts
feed status 1
feed enable 1
feed disable 1
feed unsubscribe 1
feed update 1 60
```

**Note:** The feed command requires admin access. API feeds require JSON configuration as the last argument when subscribing.

## Best Practices

1. **Check Intervals**: Set appropriate intervals based on feed update frequency (60-300 seconds typical)

2. **Message Formatting**: Keep messages under 130 characters for mesh compatibility

3. **Filtering**: Use filters to reduce noise and only send relevant items

4. **Rate Limiting**: Respect feed source rate limits by configuring appropriate intervals

5. **Error Handling**: Monitor feed errors in the web interface and adjust configuration as needed

6. **Testing**: Use the preview feature in the web interface to test output formats before enabling feeds

## Troubleshooting

### Feeds Not Polling

- Verify `feed_manager_enabled = true` in config
- Check that feeds are enabled in the database
- Review bot logs for errors
- Ensure bot is connected to mesh network

### Items Not Appearing

- Check filter configuration - items may be filtered out
- Verify output format is correct
- Check feed activity log in web interface
- Review error log for parsing issues

### Duplicate Messages

- Deduplication is automatic - check if item IDs are changing
- Verify `last_item_id` is being updated correctly
- Check database for processed items

### Rate Limiting Issues

- Increase `feed_rate_limit_seconds` in config
- Increase `message_send_interval_seconds` for specific feeds
- Reduce `check_interval_seconds` to poll less frequently

