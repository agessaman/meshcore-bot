# Weather Service

Provides scheduled weather forecasts, weather alerts, and lightning detection.

---

## Quick Start

1. **Configure Bot** - Edit `config.ini`:

```ini
[Weather_Service]
enabled = true

# Your location (required)
my_position_lat = 47.6062
my_position_lon = -122.3321

# Daily forecast time
weather_alarm = 6:00              # Or "sunrise" / "sunset"

# Channels
weather_channel = #weather
alerts_channel = #weather
```

2. **Restart Bot** - Daily forecasts start automatically

---

## Configuration

### Basic Settings

```ini
[Weather_Service]
enabled = true
my_position_lat = 47.6062         # Your latitude (required)
my_position_lon = -122.3321       # Your longitude (required)
weather_alarm = 6:00              # Time for daily forecast (HH:MM or sunrise/sunset)
weather_channel = #weather        # Channel for forecasts
alerts_channel = #weather         # Channel for weather alerts
```

### Alert Polling

```ini
poll_weather_alerts_interval = 600000  # Check for alerts every 10 minutes (milliseconds)
```

### Rain Nowcast (Proactive)

Automatically posts a heads-up when rain is about to start at your position:

```ini
rain_nowcast_enabled = true            # Auto-announce incoming rain (opt-in; default: false)
# rain_channel = #weather              # Defaults to weather_channel
poll_rain_nowcast_interval = 900000    # Check every 15 minutes (milliseconds)
rain_nowcast_lead_minutes = 60         # Only announce if rain starts within 60 min
rain_nowcast_renotify_minutes = 30     # Cooldown between pushes
# rain_nowcast_announce_ending = true  # Also announce when rain is about to stop
# rain_nowcast_threshold_mm = 0.1      # Sensitivity (mm per 15-min bucket)
```

Posts both a **starting** heads-up (rain incoming) and, by default, an **ending**
one (rain about to stop):

```
🌧️ Heads up — Rain starting in ~25min near Nashville, TN
🌧️ Heads up — Rain ending in ~20min near Nashville, TN
```

Each fires once per rain episode. Set `rain_nowcast_announce_ending = false` to
only announce incoming rain.

### Lightning Detection (Optional)

Requires `paho-mqtt` library.

```ini
blitz_collection_interval = 600000     # Aggregate lightning every 10 minutes

# Define detection area (optional)
blitz_area_min_lat = 47.0
blitz_area_min_lon = -123.0
blitz_area_max_lat = 48.0
blitz_area_max_lon = -121.0
```

---

## Features

### Daily Weather Forecast

Sends forecast to `weather_channel` at configured time:

**Example Output:**
```
🌤️ Daily Weather: Seattle: ☀️Clear 68°F NNE8mph | Tomorrow: 🌧️Light Rain 55-72°F
```

**Data Includes:**
- Current conditions with emoji
- Temperature
- Wind speed and direction
- Tomorrow's forecast

**Scheduling Options:**
- Fixed time: `weather_alarm = 6:00` (24-hour format)
- Sunrise: `weather_alarm = sunrise`
- Sunset: `weather_alarm = sunset`

### Rain Nowcast (Proactive)

Watches your position and posts a heads-up to `rain_channel` (default: `weather_channel`) when precipitation is about to start, using Open-Meteo's 15-minutely forecast — the same engine as the [`rain`/`nowcast` command](command-reference.md#rain-location).

**Example Output:**
```
🌧️ Heads up — Rain starting in ~25min near Seattle
🌨️ Heads up — Snow starting in ~40min (steady) near Seattle
```

**How It Works:**
1. Polls every `poll_rain_nowcast_interval` (default 15 min)
2. Announces when rain is expected within `rain_nowcast_lead_minutes` (default 60)
3. Fires **once per rain episode** — re-arms only after the forecast clears
4. A `rain_nowcast_renotify_minutes` cooldown (default 30) absorbs forecast flapping
5. `(steady)` marks prolonged rain (continues past the look-ahead window)

Works worldwide (no API key). Set `rain_nowcast_enabled = false` to disable.

### Weather Alerts (US Only)

Monitors NOAA weather alerts and posts new alerts to `alerts_channel`:

**Example Output:**
```
🟡Wind Adv Seattle til 9PM by NWS SEA https://is.gd/abc123
```

**Alert Types:**
- Warnings (tornado, severe thunderstorm, etc.)
- Watches (winter storm, flood, etc.)
- Advisories (wind, fog, etc.)
- Statements (special weather)

**Compact Format:**
- Severity emoji (🔴🟠🟡⚪)
- Event type and location
- Expiration time
- Issuing office
- Shortened URL for details

### Lightning Detection (Optional)

Monitors real-time lightning strikes via Blitzortung MQTT:

**Example Output:**
```
🌩️ Bellevue (15km NE)
```

**How It Works:**
1. Connects to Blitzortung MQTT broker
2. Filters strikes within configured `blitz_area`
3. Aggregates strikes every `blitz_collection_interval`
4. Reports areas with 10+ strikes

---

## Weather Data Source

Uses [Open-Meteo API](https://open-meteo.com/) (free, no API key required).

**Temperature Units:**
Inherited from `[Weather]` section (see Weather command docs):
```ini
[Weather]
temperature_unit = fahrenheit     # fahrenheit or celsius
wind_speed_unit = mph             # mph, ms, kn
precipitation_unit = inch         # inch or mm
```

---

## Alerts (US Only)

Weather alerts use NOAA API which is **US-only**. For other countries:
- Daily forecasts work worldwide via Open-Meteo
- Weather alerts won't be available
- Lightning detection works worldwide via Blitzortung

---

## Troubleshooting

### Service Not Starting

Check logs:
```bash
tail -f meshcore_bot.log | grep WeatherService
```

Common issues:
- Missing `my_position_lat` or `my_position_lon`
- Invalid coordinates
- `enabled = false`

### No Daily Forecasts

1. **Check alarm time** - Service logs "Next forecast at HH:MM:SS"
2. **Check channel** - Verify `weather_channel` exists
3. **Check position** - Coordinates must be valid

### No Weather Alerts

1. **US only** - NOAA alerts only work in the United States
2. **Check polling** - Service logs "Starting weather alerts polling"
3. **New alerts only** - Only alerts issued since last check are sent

### Lightning Not Working

1. **Check dependencies**: `pip install paho-mqtt`
2. **Check area config** - All 4 coordinates required (min/max lat/lon)
3. **Check MQTT connection** - Service logs "Connected to Blitzortung MQTT"

---

## Advanced

### Sunrise/Sunset Forecasts

When using `weather_alarm = sunrise` or `sunset`:
- Calculates time based on your coordinates
- Updates daily for seasonal changes
- Uses local timezone automatically

### Alert Deduplication

Alerts are tracked by ID to prevent duplicates. The service maintains a list of seen alert IDs and only sends new alerts.

### Lightning Strike Bucketing

Strikes are grouped by:
- **Direction** (heading from your location)
- **Distance** (grouped in 10km buckets)

Example: All strikes 50-60km to the NE are counted as one area.

---

## FAQ

**Q: Do I need an API key?**
A: No. Open-Meteo is free and doesn't require an API key.

**Q: Can I get alerts for other countries?**
A: Daily forecasts work worldwide. Weather alerts are currently US-only (NOAA). If you would like added, let me know.

**Q: How accurate are the forecasts?**
A: Open-Meteo uses data from national weather services (NOAA, DWD, etc.). Accuracy varies by location.

**Q: Can I change temperature units?**
A: Yes, set `temperature_unit` in the `[Weather]` section (used by wx command too).

**Q: Does lightning detection work worldwide?**
A: Yes. Blitzortung has global coverage.

**Q: Why do I need to define a lightning detection area?**
A: To filter strikes. Without an area, you'd get alerts for the entire globe.
