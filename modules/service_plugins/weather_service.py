#!/usr/bin/env python3
"""
Weather Service for MeshCore Bot
Provides scheduled weather forecasts and alert monitoring
"""

import asyncio
import time
import schedule
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Set
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import ephem
import xml.dom.minidom
import re
from urllib.parse import quote
import json
import math

# Try to import MQTT client (use paho-mqtt like packet capture service)
try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    mqtt = None

from .base_service import BaseServicePlugin


class WeatherService(BaseServicePlugin):
    """Weather service providing scheduled forecasts and alert monitoring.
    
    Manages daily weather forecasts, polls for NOAA weather alerts, and
    monitors lightning strikes via MQTT (Blitzortung).
    """
    
    config_section = 'Weather_Service'
    description = "Scheduled weather forecasts and alert monitoring"
    
    def __init__(self, bot: Any):
        """Initialize weather service.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        
        # Configuration
        self.weather_alarm_time = self.bot.config.get('Weather_Service', 'weather_alarm', fallback='6:00')
        self.my_position_lat = self.bot.config.getfloat('Weather_Service', 'my_position_lat', fallback=None)
        self.my_position_lon = self.bot.config.getfloat('Weather_Service', 'my_position_lon', fallback=None)
        self.weather_channel = self.bot.config.get('Weather_Service', 'weather_channel', fallback='general')
        self.alerts_channel = self.bot.config.get('Weather_Service', 'alerts_channel', fallback='general')
        
        # Polling intervals (in milliseconds, converted to seconds)
        self.blitz_collection_interval = self.bot.config.getint('Weather_Service', 'blitz_collection_interval', fallback=600000) / 1000.0
        self.poll_weather_alerts_interval = self.bot.config.getint('Weather_Service', 'poll_weather_alerts_interval', fallback=600000) / 1000.0
        
        # Storm detection area (optional)
        self.blitz_area = None
        if self.bot.config.has_option('Weather_Service', 'blitz_area_min_lat'):
            self.blitz_area = {
                'min_lat': self.bot.config.getfloat('Weather_Service', 'blitz_area_min_lat'),
                'min_lon': self.bot.config.getfloat('Weather_Service', 'blitz_area_min_lon'),
                'max_lat': self.bot.config.getfloat('Weather_Service', 'blitz_area_max_lat'),
                'max_lon': self.bot.config.getfloat('Weather_Service', 'blitz_area_max_lon'),
            }
        
        # Validate position
        if self.my_position_lat is None or self.my_position_lon is None:
            self.logger.warning("Weather service requires my_position_lat and my_position_lon in config")
            self.enabled = False
            return
        
        # Create retry-enabled session for API calls
        self.api_session = self._create_retry_session()
        
        # Get temperature/wind units from config (for Open-Meteo)
        self.temperature_unit = self.bot.config.get('Weather', 'temperature_unit', fallback='fahrenheit')
        self.wind_speed_unit = self.bot.config.get('Weather', 'wind_speed_unit', fallback='mph')
        self.precipitation_unit = self.bot.config.get('Weather', 'precipitation_unit', fallback='inch')
        
        # Track seen alerts to avoid duplicates
        self.seen_alert_ids: Set[str] = set()
        
        # Track last alert check time to only send new alerts
        self.last_alert_check_time: Optional[float] = None
        
        # Background tasks
        self._alerts_task: Optional[asyncio.Task] = None
        self._forecast_task: Optional[asyncio.Task] = None
        self._lightning_task: Optional[asyncio.Task] = None
        self._forecast_job = None  # Store schedule job for cleanup
        self._running = False
        
        # Track recent lightning strikes to avoid duplicates
        self.recent_lightning_strikes: Set[str] = set()
        
        # Lightning detection via MQTT
        self.blitz_buffer: List[Dict[str, Any]] = []
        self.seen_blitz_keys: Set[str] = set()
        self.mqtt_client: Optional[Any] = None  # paho.mqtt.client.Client
        self.mqtt_task: Optional[asyncio.Task] = None
        
        # Check if using sunrise/sunset
        self.use_sunrise_sunset = self.weather_alarm_time.lower() in ['sunrise', 'sunset']
        
        # Cache for location name (to avoid repeated reverse geocoding)
        self._cached_location_name: Optional[str] = None
        
        self.logger.info(f"Weather service initialized: position=({self.my_position_lat}, {self.my_position_lon}), alarm={self.weather_alarm_time}")
    
    def _create_retry_session(self) -> requests.Session:
        """Create a requests session with retry logic for API calls.
        
        Returns:
            requests.Session: Configured session with retry adapter.
        """
        session = requests.Session()
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session
    
    def _get_sunrise_sunset_time(self, event: str) -> Optional[datetime]:
        """Get sunrise or sunset time for configured position.
        
        Args:
            event: 'sunrise' or 'sunset'.
        
        Returns:
            Optional[datetime]: Datetime object with the next sunrise/sunset time, or None on error.
        """
        try:
            obs = ephem.Observer()
            obs.date = datetime.now(timezone.utc)
            obs.lat = str(self.my_position_lat)
            obs.lon = str(self.my_position_lon)
            
            sun = ephem.Sun()
            sun.compute(obs)
            
            if event.lower() == 'sunrise':
                next_event = ephem.localtime(obs.next_rising(sun))
            elif event.lower() == 'sunset':
                next_event = ephem.localtime(obs.next_setting(sun))
            else:
                return None
            
            return next_event
        except Exception as e:
            self.logger.error(f"Error calculating {event}: {e}")
            return None
    
    async def start(self) -> None:
        """Start the weather service.
        
        Initializes scheduled tasks for forecasts, alert polling, and lightning detection.
        """
        if not self.enabled:
            self.logger.info("Weather service is disabled, not starting")
            return
        
        self._running = True
        self.logger.info("Starting weather service")
        
        # Setup scheduled daily forecast
        if self.use_sunrise_sunset:
            # For sunrise/sunset, use a background task that reschedules daily
            self._forecast_task = asyncio.create_task(self._sunrise_sunset_forecast_loop())
        else:
            # For fixed times, use schedule library
            self._setup_daily_forecast()
        
        # Start background tasks
        self._alerts_task = asyncio.create_task(self._poll_weather_alerts_loop())
        
        # Start lightning detection if area is configured
        if self.blitz_area and MQTT_AVAILABLE:
            self._lightning_task = asyncio.create_task(self._poll_lightning_loop())
            self.mqtt_task = asyncio.create_task(self._connect_blitzortung_mqtt())
        else:
            self._lightning_task = None
            self.mqtt_task = None
            if self.blitz_area and not MQTT_AVAILABLE:
                self.logger.warning("Lightning detection configured but paho-mqtt not available")
        
        self.logger.info("Weather service started")
    
    async def stop(self) -> None:
        """Stop the weather service.
        
        cancels all background tasks and closes connections.
        """
        self._running = False
        self.logger.info("Stopping weather service")
        
        # Cancel background tasks
        if self._alerts_task:
            self._alerts_task.cancel()
            try:
                await self._alerts_task
            except asyncio.CancelledError:
                pass
        
        if self._forecast_task:
            self._forecast_task.cancel()
            try:
                await self._forecast_task
            except asyncio.CancelledError:
                pass
        
        if self._lightning_task:
            self._lightning_task.cancel()
            try:
                await self._lightning_task
            except asyncio.CancelledError:
                pass
        
        if self.mqtt_task:
            self.mqtt_task.cancel()
            try:
                await self.mqtt_task
            except asyncio.CancelledError:
                pass
        
        if self.mqtt_client:
            try:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            except Exception:
                pass
        
        # Clear scheduled forecast
        # Note: schedule library doesn't have a direct way to cancel a specific job
        # The job will simply not execute if _running is False
        # If needed, we could use schedule.clear() but that would clear ALL scheduled jobs
        pass
        
        self.logger.info("Weather service stopped")
    
    def _setup_daily_forecast(self) -> None:
        """Setup daily weather forecast schedule for fixed times.
        
        Configures the schedule library to trigger _send_daily_forecast at the configured time.
        """
        try:
            # Parse time (format: "HH:MM" or "H:MM")
            if ':' in self.weather_alarm_time:
                hour, minute = map(int, self.weather_alarm_time.split(':'))
            else:
                # Assume format "HHMM"
                hour = int(self.weather_alarm_time[:2])
                minute = int(self.weather_alarm_time[2:])
            
            schedule_time = f"{hour:02d}:{minute:02d}"
            self._forecast_job = schedule.every().day.at(schedule_time).do(self._send_daily_forecast)
            self.logger.info(f"Scheduled daily weather forecast at {schedule_time}")
        except Exception as e:
            self.logger.error(f"Error setting up daily forecast schedule: {e}")
    
    async def _sunrise_sunset_forecast_loop(self) -> None:
        """Background task for sunrise/sunset-based forecasts.
        
        Calculates daily sunrise/sunset times and schedules the forecast accordingly.
        """
        event_type = self.weather_alarm_time.lower()
        self.logger.info(f"Starting {event_type}-based forecast loop")
        
        while self._running:
            try:
                # Calculate next sunrise/sunset time
                next_event = self._get_sunrise_sunset_time(event_type)
                
                if not next_event:
                    self.logger.error(f"Failed to calculate {event_type} time, retrying in 1 hour")
                    await asyncio.sleep(3600)
                    continue
                
                # Calculate seconds until next event
                now = datetime.now()
                if next_event.tzinfo:
                    # Convert to local time if timezone-aware
                    if now.tzinfo:
                        next_event = next_event.astimezone(now.tzinfo).replace(tzinfo=None)
                    else:
                        next_event = next_event.replace(tzinfo=None)
                
                wait_seconds = (next_event - now).total_seconds()
                
                # If the event already passed today, wait until tomorrow's calculation
                if wait_seconds < 0:
                    # Wait until after midnight, then recalculate
                    wait_seconds = 3600  # Wait 1 hour and recalculate
                    self.logger.debug(f"{event_type} already passed today, waiting to recalculate")
                else:
                    self.logger.info(f"Next {event_type} at {next_event.strftime('%H:%M:%S')}, waiting {wait_seconds:.0f} seconds")
                
                # Wait until the event time (or 1 hour if already passed)
                await asyncio.sleep(max(1, min(wait_seconds, 86400)))  # Cap at 24 hours
                
                # Check if we should send forecast (only if we waited for the actual event)
                if wait_seconds > 0 and wait_seconds < 86400:
                    await self._send_daily_forecast_async()
                    # Small delay after sending to avoid immediate recalculation
                    await asyncio.sleep(60)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in {event_type} forecast loop: {e}")
                await asyncio.sleep(3600)  # Wait 1 hour on error
    
    def _send_daily_forecast(self) -> None:
        """Send daily weather forecast (called by schedule library).
        
        Wrapper to run the async forecast sender from the synchronous schedule job.
        """
        if not self._running:
            return
        
        self.logger.info(f"📅 Sending daily weather forecast at {datetime.now().strftime('%H:%M:%S')}")
        
        # Use the main event loop if available, otherwise create a new one
        # This prevents deadlock when the main loop is already running
        if hasattr(self.bot, 'main_event_loop') and self.bot.main_event_loop and self.bot.main_event_loop.is_running():
            # Schedule coroutine in the running main event loop
            future = asyncio.run_coroutine_threadsafe(
                self._send_daily_forecast_async(),
                self.bot.main_event_loop
            )
            # Wait for completion (with timeout to prevent indefinite blocking)
            try:
                future.result(timeout=120)  # 2 minute timeout for weather forecast
            except Exception as e:
                self.logger.error(f"Error sending daily weather forecast: {e}")
        else:
            # Fallback: create new event loop if main loop not available
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            loop.run_until_complete(self._send_daily_forecast_async())
    
    async def _send_daily_forecast_async(self) -> None:
        """Send daily weather forecast (async implementation).
        
        Fetches the forecast and sends it to the configured channel.
        Uses Open-Meteo for weather data and manages its own error logging.
        """
        try:
            # Get weather forecast
            forecast_text = await self._get_weather_forecast()
            
            if forecast_text and forecast_text != "Error fetching weather data":
                # Send to configured channel
                await self.bot.command_manager.send_channel_message(
                    self.weather_channel,
                    f"🌤️ Daily Weather: {forecast_text}"
                )
                self.logger.info(f"Daily weather forecast sent to {self.weather_channel}")
            else:
                self.logger.warning("Failed to get weather forecast for daily update")
        except Exception as e:
            self.logger.error(f"Error sending daily weather forecast: {e}")
    
    async def _get_weather_forecast(self) -> str:
        """Get weather forecast for configured position using Open-Meteo API.
        
        Returns:
            str: Formatted forecast string or error message.
        """
        try:
            # Open-Meteo API endpoint
            api_url = "https://api.open-meteo.com/v1/forecast"
            
            params = {
                'latitude': self.my_position_lat,
                'longitude': self.my_position_lon,
                'current': 'temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m',
                'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max',
                'temperature_unit': self.temperature_unit,
                'wind_speed_unit': self.wind_speed_unit,
                'precipitation_unit': self.precipitation_unit,
                'timezone': 'auto',
                'forecast_days': 2  # Today and tomorrow
            }
            
            try:
                response = self.api_session.get(api_url, params=params, timeout=10)
                if not response.ok:
                    self.logger.warning(f"Error fetching weather from Open-Meteo: HTTP {response.status_code}")
                    return "Error fetching weather data"
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                self.logger.warning(f"Timeout/connection error fetching weather: {e}")
                return "Error fetching weather data"
            
            data = response.json()
            
            # Extract current conditions
            current = data.get('current', {})
            daily = data.get('daily', {})
            
            if not current or not daily:
                return "No forecast data available"
            
            # Current conditions
            temp = int(current.get('temperature_2m', 0))
            weather_code = current.get('weather_code', 0)
            wind_speed = int(current.get('wind_speed_10m', 0))
            wind_direction = self._degrees_to_direction(current.get('wind_direction_10m', 0))
            
            # Get weather description and emoji
            weather_desc = self._get_weather_description(weather_code)
            weather_emoji = self._get_weather_emoji(weather_code)
            
            # Temperature unit symbol
            temp_symbol = "°F" if self.temperature_unit == 'fahrenheit' else "°C"
            
            # Get location name (cached to avoid repeated API calls)
            if self._cached_location_name is None:
                try:
                    from ..utils import rate_limited_nominatim_reverse, format_location_for_display
                    coordinates_str = f"{self.my_position_lat}, {self.my_position_lon}"
                    location = await rate_limited_nominatim_reverse(self.bot, coordinates_str, timeout=5)
                    
                    if location and hasattr(location, 'raw'):
                        address = location.raw.get('address', {})
                        city = (address.get('city') or 
                               address.get('town') or 
                               address.get('village') or 
                               address.get('municipality') or
                               address.get('suburb') or
                               None)
                        state = (address.get('state') or 
                                address.get('province') or 
                                address.get('region') or
                                None)
                        country = address.get('country')
                        location_name = format_location_for_display(city, state, country)
                        if not location_name:
                            location_name = f"{self.my_position_lat:.2f},{self.my_position_lon:.2f}"
                    else:
                        location_name = f"{self.my_position_lat:.2f},{self.my_position_lon:.2f}"
                    self._cached_location_name = location_name
                except Exception as e:
                    self.logger.debug(f"Error reverse geocoding location: {e}")
                    location_name = f"{self.my_position_lat:.2f},{self.my_position_lon:.2f}"
                    self._cached_location_name = location_name
            else:
                location_name = self._cached_location_name
            
            # Format current forecast
            forecast_text = f"{location_name}: {weather_emoji}{weather_desc} {temp}{temp_symbol}"
            if wind_speed > 0:
                wind_dir_str = f"{wind_direction}" if wind_direction else ""
                forecast_text += f" {wind_dir_str}{wind_speed}{self.wind_speed_unit}"
            
            # Add tomorrow's forecast
            daily_times = daily.get('time', [])
            daily_codes = daily.get('weather_code', [])
            daily_max = daily.get('temperature_2m_max', [])
            daily_min = daily.get('temperature_2m_min', [])
            
            if len(daily_times) > 1 and len(daily_codes) > 1:
                tomorrow_code = daily_codes[1]
                tomorrow_max = int(daily_max[1]) if len(daily_max) > 1 else None
                tomorrow_min = int(daily_min[1]) if len(daily_min) > 1 else None
                tomorrow_desc = self._get_weather_description(tomorrow_code)
                tomorrow_emoji = self._get_weather_emoji(tomorrow_code)
                
                if tomorrow_max is not None:
                    if tomorrow_min is not None and tomorrow_min != tomorrow_max:
                        forecast_text += f" | Tomorrow: {tomorrow_emoji}{tomorrow_desc} {tomorrow_min}-{tomorrow_max}{temp_symbol}"
                    else:
                        forecast_text += f" | Tomorrow: {tomorrow_emoji}{tomorrow_desc} {tomorrow_max}{temp_symbol}"
            
            return forecast_text
            
        except Exception as e:
            self.logger.error(f"Error getting weather forecast: {e}")
            import traceback
            self.logger.debug(traceback.format_exc())
            return "Error fetching weather data"
    
    def _degrees_to_direction(self, degrees: float) -> str:
        """Convert wind direction in degrees to compass direction.
        
        Args:
            degrees: Wind direction in degrees (0-360).
            
        Returns:
            str: Compass direction (e.g., 'N', 'NE', 'SW').
        """
        if degrees is None:
            return ""
        
        directions = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                     'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
        index = int((degrees + 11.25) / 22.5) % 16
        return directions[index]
    
    def _get_weather_description(self, code: int) -> str:
        """Get weather description from WMO weather code.
        
        Args:
            code: WMO weather code integer.
            
        Returns:
            str: Human-readable weather description.
        """
        # WMO Weather interpretation codes (WW)
        codes = {
            0: "Clear", 1: "Mostly Clear", 2: "Partly Cloudy", 3: "Overcast",
            45: "Foggy", 48: "Depositing Rime Fog",
            51: "Light Drizzle", 53: "Moderate Drizzle", 55: "Dense Drizzle",
            56: "Light Freezing Drizzle", 57: "Dense Freezing Drizzle",
            61: "Slight Rain", 63: "Moderate Rain", 65: "Heavy Rain",
            66: "Light Freezing Rain", 67: "Heavy Freezing Rain",
            71: "Slight Snow", 73: "Moderate Snow", 75: "Heavy Snow",
            77: "Snow Grains", 80: "Slight Rain Showers", 81: "Moderate Rain Showers",
            82: "Violent Rain Showers", 85: "Slight Snow Showers", 86: "Heavy Snow Showers",
            95: "Thunderstorm", 96: "Thunderstorm w/Hail", 99: "Severe Thunderstorm"
        }
        return codes.get(code, "Unknown")
    
    def _get_weather_emoji(self, code: int) -> str:
        """Get weather emoji from WMO weather code.
        
        Args:
            code: WMO weather code integer.
            
        Returns:
            str: Emoji character representing the weather.
        """
        if code == 0:
            return "☀️"
        elif code in [1, 2]:
            return "🌤️"
        elif code == 3:
            return "☁️"
        elif code in [45, 48]:
            return "🌫️"
        elif code in [51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82]:
            return "🌧️"
        elif code in [71, 73, 75, 77, 85, 86]:
            return "❄️"
        elif code in [95, 96, 99]:
            return "⛈️"
        else:
            return "🌤️"
    
    async def _poll_weather_alerts_loop(self) -> None:
        """Background task to poll for weather alerts.
        
        Runs periodically based on configured interval.
        """
        self.logger.info(f"Starting weather alerts polling (interval: {self.poll_weather_alerts_interval}s)")
        
        while self._running:
            try:
                await self._check_weather_alerts()
                await asyncio.sleep(self.poll_weather_alerts_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in weather alerts polling loop: {e}")
                await asyncio.sleep(60)  # Wait 1 minute on error before retrying
    
    async def _check_weather_alerts(self) -> None:
        """Check for new weather alerts (US-only via NOAA API).
        
        Note: Open-Meteo doesn't provide weather alerts, so we use NOAA which is US-only.
        For international locations, alerts will not be available.
        Only sends alerts that were issued since the last check.
        """
        try:
            # Get current time for this check
            current_check_time = time.time()
            
            # Calculate time window: only alerts issued since last check (or last polling interval if first check)
            if self.last_alert_check_time is None:
                # First check: only get alerts from the last polling interval
                time_window_start = current_check_time - self.poll_weather_alerts_interval
            else:
                # Subsequent checks: only get alerts since last check
                time_window_start = self.last_alert_check_time
            
            # Round coordinates
            lat_rounded = round(self.my_position_lat, 4)
            lon_rounded = round(self.my_position_lon, 4)
            
            # NOAA alerts API (US-only)
            alert_url = f"https://api.weather.gov/alerts/active.atom?point={lat_rounded},{lon_rounded}"
            
            try:
                alert_data = self.api_session.get(alert_url, timeout=10)
                if not alert_data.ok:
                    self.logger.debug(f"Error fetching alerts: HTTP {alert_data.status_code}")
                    return
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                self.logger.debug(f"Timeout/connection error fetching alerts: {e}")
                return
            
            # Parse ATOM feed with full metadata extraction (same as wx_command)
            alerts = []
            alertxml = xml.dom.minidom.parseString(alert_data.text)
            
            for entry in alertxml.getElementsByTagName("entry"):
                try:
                    # Get alert ID
                    alert_id_elem = entry.getElementsByTagName("id")
                    if not alert_id_elem or not alert_id_elem[0].childNodes:
                        continue
                    alert_id = alert_id_elem[0].childNodes[0].nodeValue
                    
                    # Skip if we've already seen this alert
                    if alert_id in self.seen_alert_ids:
                        continue
                    
                    # Get entry updated timestamp (most reliable - when alert was last updated/issued)
                    entry_updated_time = None
                    updated_elem = entry.getElementsByTagName("updated")
                    if updated_elem and updated_elem[0].childNodes:
                        updated_str = updated_elem[0].childNodes[0].nodeValue
                        entry_updated_time = self._parse_iso_time(updated_str)
                    
                    # Extract full alert metadata (same logic as wx_command)
                    alert_dict = self._parse_alert_entry(entry, alert_id)
                    if not alert_dict:
                        continue
                    
                    # Determine alert issued time (prefer entry updated time, then effective time)
                    alert_issued_time = entry_updated_time
                    if alert_issued_time is None:
                        alert_issued_time = self._parse_alert_time(alert_dict.get('effective', ''))
                    
                    if alert_issued_time is None:
                        # If we can't parse any time, use current time as fallback
                        # This means we'll send it, but it's better than missing new alerts
                        alert_issued_time = current_check_time
                        self.logger.debug(f"Could not parse time for alert {alert_id}, using current time")
                    
                    # Only include alerts issued since last check
                    if alert_issued_time >= time_window_start:
                        alerts.append(alert_dict)
                        self.seen_alert_ids.add(alert_id)
                        self.logger.debug(f"New alert {alert_id} issued at {datetime.fromtimestamp(alert_issued_time)}")
                    else:
                        # Alert is older than our window, mark as seen but don't send
                        self.seen_alert_ids.add(alert_id)
                        self.logger.debug(f"Skipping old alert {alert_id} (issued {datetime.fromtimestamp(alert_issued_time)} before time window start {datetime.fromtimestamp(time_window_start)})")
                    
                except Exception as e:
                    self.logger.debug(f"Error parsing alert entry: {e}")
                    continue
            
            # Send new alerts with compact formatting
            for alert in alerts:
                try:
                    # Format alert using compact formatter (same as wx_command)
                    alert_text = await self._format_alert_compact(alert, include_details=True)
                    
                    await self.bot.command_manager.send_channel_message(
                        self.alerts_channel,
                        alert_text
                    )
                    self.logger.info(f"Weather alert sent: {alert.get('title', 'Unknown')}")
                    
                    # Small delay between alerts
                    await asyncio.sleep(2)
                    
                except Exception as e:
                    self.logger.error(f"Error sending weather alert: {e}")
            
            # Update last check time
            self.last_alert_check_time = current_check_time
            
            # Clean up old alert IDs (keep last 100)
            if len(self.seen_alert_ids) > 100:
                self.seen_alert_ids = set(list(self.seen_alert_ids)[-100:])
            
        except Exception as e:
            self.logger.error(f"Error checking weather alerts: {e}")
    
    async def _connect_blitzortung_mqtt(self) -> None:
        """Connect to Blitzortung MQTT broker and subscribe to lightning data.
        
        Maintains a connection to the MQTT broker for real-time lightning strikes.
        """
        if not self.blitz_area or not MQTT_AVAILABLE:
            return
        
        broker_host = "blitzortung.ha.sed.pl"
        broker_port = 1883
        topic = "blitzortung/1.1/#"
        
        self.logger.info(f"Connecting to Blitzortung MQTT broker: {broker_host}:{broker_port}")
        
        while self._running:
            try:
                # Create paho-mqtt client
                client_id = f"meshcore_weather_{int(time.time())}"
                client = mqtt.Client(client_id=client_id)
                self.mqtt_client = client
                
                # Set up message callback
                def on_message(client, userdata, msg):
                    try:
                        # Decode message
                        payload = msg.payload.decode('utf-8')
                        blitz_data = json.loads(payload)
                        
                        # Check if strike is within our area
                        lat = blitz_data.get('lat')
                        lon = blitz_data.get('lon')
                        
                        if lat is None or lon is None:
                            return
                        
                        if (self.blitz_area['min_lat'] <= lat <= self.blitz_area['max_lat'] and
                            self.blitz_area['min_lon'] <= lon <= self.blitz_area['max_lon']):
                            # Schedule async processing
                            asyncio.create_task(self._handle_lightning_strike(blitz_data))
                            
                    except json.JSONDecodeError:
                        self.logger.debug("Invalid JSON in lightning MQTT message")
                    except Exception as e:
                        self.logger.debug(f"Error processing lightning MQTT message: {e}")
                
                client.on_message = on_message
                
                # Connect and subscribe (non-blocking to avoid blocking event loop)
                loop = asyncio.get_event_loop()
                try:
                    await loop.run_in_executor(None, client.connect, broker_host, broker_port, 60)
                except Exception as connect_error:
                    # Connection failed, but don't block - will retry on next cycle
                    self.logger.debug(f"Initial connect() call failed (non-blocking): {connect_error}")
                    raise  # Re-raise to trigger retry logic
                
                # Subscribe is non-blocking, but wrap it anyway for consistency
                try:
                    client.subscribe(topic)
                except Exception as subscribe_error:
                    self.logger.debug(f"Subscribe() call failed: {subscribe_error}")
                    raise
                
                client.loop_start()
                
                self.logger.info(f"Connected to Blitzortung MQTT, subscribed to {topic}")
                
                # Keep connection alive
                while self._running:
                    await asyncio.sleep(1)
                    if not client.is_connected():
                        self.logger.warning("Blitzortung MQTT disconnected, reconnecting...")
                        break
                
                client.loop_stop()
                client.disconnect()
                
                                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in Blitzortung MQTT connection: {e}")
                if self._running:
                    self.logger.info("Reconnecting to Blitzortung MQTT in 30 seconds...")
                    await asyncio.sleep(30)
    
    async def _handle_lightning_strike(self, blitz_data: Dict[str, Any]) -> None:
        """Handle a single lightning strike from MQTT.
        
        Calculates distance and adds to buffer if within range.
        
        Args:
            blitz_data: Dictionary containing lightning strike data.
        """
        lat = blitz_data.get('lat')
        lon = blitz_data.get('lon')
        
        if lat is None or lon is None:
            return
        
        # Calculate heading and distance from bot position
        heading, distance = self._calculate_heading_and_distance(
            self.my_position_lat, self.my_position_lon, lat, lon
        )
        
        # Create bucket key (same as original: heading|distance/10)
        distance_bucket = int(distance / 10)
        key = f"{heading}|{distance_bucket}"
        
        # Add to buffer
        self.blitz_buffer.append({
            'key': key,
            'heading': heading,
            'distance': distance,
            'lat': lat,
            'lon': lon,
            'timestamp': blitz_data.get('time', time.time())
        })
    
    def _calculate_heading_and_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> tuple:
        """Calculate heading and distance between two points (same as original implementation).
        
        Args:
            lat1: Latitude of point 1.
            lon1: Longitude of point 1.
            lat2: Latitude of point 2.
            lon2: Longitude of point 2.
            
        Returns:
            tuple: (heading_degrees, distance_km)
        """
        # Convert to radians
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        dlon_rad = math.radians(lon2 - lon1)
        
        # Calculate distance using Haversine formula
        a = math.sin((lat2_rad - lat1_rad) / 2)**2 + \
            math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon_rad / 2)**2
        c = 2 * math.asin(math.sqrt(a))
        distance_km = 6371 * c  # Earth radius in km
        
        # Calculate bearing/heading
        y = math.sin(dlon_rad) * math.cos(lat2_rad)
        x = math.cos(lat1_rad) * math.sin(lat2_rad) - \
            math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon_rad)
        heading_rad = math.atan2(y, x)
        heading_deg = (math.degrees(heading_rad) + 360) % 360
        
        return (int(heading_deg), distance_km)
    
    async def _poll_lightning_loop(self) -> None:
        """Background task to aggregate and report lightning strikes.
        
        Periodically processes the lightning buffer and sends alerts.
        """
        self.logger.info(f"Starting lightning aggregation (interval: {self.blitz_collection_interval}s)")
        
        while self._running:
            try:
                await self._process_lightning_buffer()
                await asyncio.sleep(self.blitz_collection_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in lightning aggregation loop: {e}")
                await asyncio.sleep(60)  # Wait 1 minute on error before retrying
    
    async def _process_lightning_buffer(self) -> None:
        """Process buffered lightning strikes and send alerts if threshold met.
        
        Groups strikes by location bucket and sends alerts if count exceeds threshold.
        """
        if not self.blitz_buffer:
            return
        
        # Count strikes by bucket key
        counter = {}
        for blitz in self.blitz_buffer:
            key = blitz['key']
            counter[key] = counter.get(key, 0) + 1
        
        # Check each bucket
        for key, count in counter.items():
            # Only alert if 10+ strikes in bucket and we haven't seen this bucket before
            if count >= 10 and key not in self.seen_blitz_keys:
                # Find a representative strike from this bucket
                bucket_strikes = [b for b in self.blitz_buffer if b['key'] == key]
                if not bucket_strikes:
                    continue
                
                data = bucket_strikes[0]
                heading = data['heading']
                distance = data['distance']
                
                # Get compass direction name
                compass_name = self._heading_to_compass(heading)
                
                # Try to geocode location (optional, may fail)
                location_name = await self._geocode_location(data['lat'], data['lon'])
                
                # Format message
                if location_name:
                    message = f"🌩️ {location_name} ({int(distance)}km {compass_name})"
                else:
                    message = f"🌩️ Lightning activity ({int(distance)}km {compass_name})"
                
                await self.bot.command_manager.send_channel_message(
                    self.alerts_channel,
                    message
                )
                self.logger.info(f"Lightning alert sent: {message}")
                
                # Mark this bucket as seen
                self.seen_blitz_keys.add(key)
                
                # Small delay between alerts
                await asyncio.sleep(2)
        
        # Clear buffer
        self.blitz_buffer = []
        
        # Clean up old seen keys (keep last 1000)
        if len(self.seen_blitz_keys) > 1000:
            self.seen_blitz_keys = set(list(self.seen_blitz_keys)[-1000:])
    
    def _heading_to_compass(self, heading: int) -> str:
        """Convert heading in degrees to compass direction name.
        
        Args:
            heading: Heading in degrees.
            
        Returns:
            str: Compass direction abbreviation (e.g., 'N', 'NW').
        """
        compass_points = [
            'N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
            'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'
        ]
        index = int((heading + 11.25) / 22.5) % 16
        return compass_points[index]
    
    async def _geocode_location(self, lat: float, lon: float) -> Optional[str]:
        """Geocode coordinates to location name (optional, may return None).
        
        Args:
            lat: Latitude.
            lon: Longitude.
            
        Returns:
            Optional[str]: City/town name or None if lookup fails.
        """
        try:
            # Use reverse geocoding if available in utils
            from ..utils import rate_limited_nominatim_reverse_sync
            location = rate_limited_nominatim_reverse_sync(self.bot, f"{lat}, {lon}", timeout=5)
            if location:
                # Extract city/town name
                if isinstance(location, dict):
                    return location.get('city') or location.get('town') or location.get('village') or None
                return str(location)
        except Exception:
            pass
        return None
    
    def _parse_alert_entry(self, entry: Any, alert_id: str) -> Optional[Dict[str, Any]]:
        """Parse alert XML entry and extract full metadata (same logic as wx_command).
        
        Args:
            entry: XML DOM entry element.
            alert_id: Alert ID string.
        
        Returns:
            Optional[Dict[str, Any]]: Alert dict with event, event_type, severity, expires, office, etc., or None on error.
        """
        try:
            # Extract title
            title_elem = entry.getElementsByTagName("title")
            title = title_elem[0].childNodes[0].nodeValue if title_elem and title_elem[0].childNodes else ""
            
            if not title:
                return None
            
            # Extract link URL (ATOM feeds have <link> elements)
            # Prefer HTML links over CAP XML links
            link_url = ""
            html_link_url = ""
            cap_link_url = ""
            
            link_elems = entry.getElementsByTagName("link")
            for link_elem in link_elems:
                href = ""
                if link_elem.hasAttribute("href"):
                    href = link_elem.getAttribute("href")
                elif link_elem.childNodes and link_elem.firstChild:
                    href = link_elem.firstChild.nodeValue
                
                if not href:
                    continue
                
                # Check link type and rel attributes
                link_type = link_elem.getAttribute("type") or ""
                link_rel = link_elem.getAttribute("rel") or ""
                
                # Prefer HTML links
                if "text/html" in link_type or link_rel == "alternate":
                    html_link_url = href
                # Track CAP XML links as fallback
                elif "cap+xml" in link_type or href.endswith(".cap") or "/alerts/" in href:
                    cap_link_url = href
            
            # Use HTML link if available, otherwise fall back to first link or CAP link
            if html_link_url:
                link_url = html_link_url
            elif cap_link_url:
                # Convert CAP XML URL to HTML view URL
                link_url = self._convert_cap_url_to_html(cap_link_url)
            elif link_elems:
                # Fallback: use first link found
                first_link = link_elems[0]
                if first_link.hasAttribute("href"):
                    href = first_link.getAttribute("href")
                    if href.endswith(".cap") or "/alerts/" in href:
                        link_url = self._convert_cap_url_to_html(href)
                    else:
                        link_url = href
                elif first_link.childNodes and first_link.firstChild:
                    href = first_link.firstChild.nodeValue
                    if href.endswith(".cap") or "/alerts/" in href:
                        link_url = self._convert_cap_url_to_html(href)
                    else:
                        link_url = href
            
            # Extract summary/content
            summary = ""
            summary_elem = entry.getElementsByTagName("summary")
            if summary_elem and summary_elem[0].childNodes:
                summary = summary_elem[0].childNodes[0].nodeValue if summary_elem[0].childNodes[0].nodeValue else ""
            if not summary:
                content_elem = entry.getElementsByTagName("content")
                if content_elem and content_elem[0].childNodes:
                    summary = content_elem[0].childNodes[0].nodeValue if content_elem[0].childNodes[0].nodeValue else ""
            
            # Extract NWS headline parameter
            nws_headline = ""
            params = entry.getElementsByTagName("cap:parameter")
            if not params:
                params = entry.getElementsByTagName("parameter")
            
            for param in params:
                value_name_elem = param.getElementsByTagName("valueName")
                value_elem = param.getElementsByTagName("value")
                if value_name_elem and value_elem and value_name_elem[0].childNodes and value_elem[0].childNodes:
                    value_name = value_name_elem[0].childNodes[0].nodeValue if value_name_elem[0].childNodes[0].nodeValue else ""
                    if value_name == "NWSheadline":
                        nws_headline = value_elem[0].childNodes[0].nodeValue if value_elem[0].childNodes[0].nodeValue else ""
                        break
            
            # Extract CAP metadata
            event = ""
            severity = "Unknown"
            urgency = "Unknown"
            certainty = "Unknown"
            effective = ""
            expires = ""
            area_desc = ""
            office = ""
            
            # Parse title to extract key info
            title_lower = title.lower()
            
            # Extract event type from title
            if "warning" in title_lower:
                event_type = "Warning"
                event_match = re.search(r'^([^W]+?)\s+Warning', title, re.IGNORECASE)
                if event_match:
                    event = event_match.group(1).strip()
            elif "watch" in title_lower:
                event_type = "Watch"
                event_match = re.search(r'^([^W]+?)\s+Watch', title, re.IGNORECASE)
                if event_match:
                    event = event_match.group(1).strip()
            elif "advisory" in title_lower:
                event_type = "Advisory"
                event_match = re.search(r'^([^A]+?)\s+Advisory', title, re.IGNORECASE)
                if event_match:
                    event = event_match.group(1).strip()
            elif "statement" in title_lower:
                event_type = "Statement"
                event_match = re.search(r'^([^S]+?)\s+Statement', title, re.IGNORECASE)
                if event_match:
                    event = event_match.group(1).strip()
                else:
                    event = "Special"
                
                # For Special Statements, extract meaningful description from NWS headline
                if event.lower() in ["special", "special weather"] and nws_headline:
                    headline_lower = nws_headline.lower()
                    if any(phrase in headline_lower for phrase in ['debris flow', 'mudslide']):
                        event = "Debris Flow"
                    elif 'landslide' in headline_lower:
                        event = "Landslide (Burn)" if ('burn' in headline_lower or 'burned area' in headline_lower) else "Landslide"
                    elif any(phrase in headline_lower for phrase in ['flash flood', 'river flood', 'flood', 'flooding']):
                        event = "Flood"
                    elif any(phrase in headline_lower for phrase in ['high wind', 'strong wind', 'damaging wind', 'wind', 'gust']):
                        event = "Wind"
                    elif any(phrase in headline_lower for phrase in ['heavy rain', 'excessive rain', 'rain', 'rainfall', 'precipitation']):
                        if not any(word in headline_lower for word in ['landslide', 'flood', 'wind', 'snow']):
                            event = "Rainfall"
                    elif any(phrase in headline_lower for phrase in ['heavy snow', 'blizzard', 'winter storm', 'snow', 'winter']):
                        event = "Snow"
                    elif any(phrase in headline_lower for phrase in ['dense fog', 'low visibility', 'fog', 'visibility']):
                        event = "Fog" if 'fog' in headline_lower else "Visibility"
                    elif any(phrase in headline_lower for phrase in ['extreme heat', 'excessive heat', 'heat', 'temperature']):
                        event = "Heat" if 'heat' in headline_lower else "Temperature"
                    elif any(phrase in headline_lower for phrase in ['storm surge', 'coastal flood', 'marine', 'coastal']):
                        event = "Marine"
                    else:
                        # Extract first meaningful word
                        headline_words = headline_lower.split()
                        skip_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'will', 'lead', 'to', 'an', 'increased', 'threat', 'remains', 'in', 'effect', 'until', 'during', 'last', 'week', 'including', 'today'}
                        meaningful_words = [w for w in headline_words if w not in skip_words and len(w) > 3]
                        if meaningful_words:
                            event = meaningful_words[0].capitalize()
                
                # Fallback to summary if still generic
                if event.lower() in ["special", "special weather"] and summary:
                    summary_lower = summary.lower()
                    if any(word in summary_lower for word in ['landslide', 'debris flow', 'mudslide']):
                        event = "Landslide"
                    elif any(word in summary_lower for word in ['hydrologic', 'river', 'flood', 'stream']):
                        event = "Hydrologic"
                    elif any(word in summary_lower for word in ['marine', 'coastal', 'beach', 'surf']):
                        event = "Marine"
                    elif any(word in summary_lower for word in ['wind', 'gust']):
                        event = "Wind"
                    elif any(word in summary_lower for word in ['rain', 'precipitation', 'shower', 'rainfall']):
                        event = "Rainfall"
                
                if event.lower() in ["special", "special weather"]:
                    event = "Weather" if "weather" in title_lower else "Special"
            else:
                event_type = "Unknown"
                event = title.split()[0] if title else ""
            
            # Extract times from title
            issued_match = re.search(r'issued\s+([^u]+?)\s+until\s+(.+?)\s+by', title, re.IGNORECASE)
            if issued_match:
                effective = issued_match.group(1).strip()
                expires = issued_match.group(2).strip()
            else:
                until_match = re.search(r'until\s+(.+?)\s+by', title, re.IGNORECASE)
                if until_match:
                    expires = until_match.group(1).strip()
            
            # Extract office from title
            office_match = re.search(r'by\s+(.+?)$', title, re.IGNORECASE)
            if office_match:
                office = office_match.group(1).strip()
            
            # Try to extract CAP elements
            def get_node_value(node):
                if not node or not node.childNodes:
                    return ""
                text_parts = []
                for child in node.childNodes:
                    if child.nodeType == child.TEXT_NODE:
                        text_parts.append(child.nodeValue)
                    elif hasattr(child, 'nodeValue') and child.nodeValue:
                        text_parts.append(child.nodeValue)
                return " ".join(text_parts).strip()
            
            for child in entry.childNodes:
                if hasattr(child, 'tagName'):
                    tag_name = child.tagName
                    tag_lower = tag_name.lower()
                    
                    if ('event' in tag_lower or tag_name.endswith(':event')) and not event:
                        event_val = get_node_value(child)
                        if event_val:
                            event = event_val
                    elif 'severity' in tag_lower or tag_name.endswith(':severity'):
                        severity_val = get_node_value(child)
                        if severity_val:
                            severity = severity_val
                    elif 'urgency' in tag_lower or tag_name.endswith(':urgency'):
                        urgency_val = get_node_value(child)
                        if urgency_val:
                            urgency = urgency_val
                    elif 'certainty' in tag_lower or tag_name.endswith(':certainty'):
                        certainty_val = get_node_value(child)
                        if certainty_val:
                            certainty = certainty_val
                    elif 'effective' in tag_lower or tag_name.endswith(':effective'):
                        effective_val = get_node_value(child)
                        if effective_val:
                            effective = effective_val
                    elif 'expires' in tag_lower or tag_name.endswith(':expires'):
                        expires_val = get_node_value(child)
                        if expires_val:
                            expires = expires_val
                    elif ('areadesc' in tag_lower or 'area' in tag_lower or 
                          tag_name.endswith(':areadesc') or tag_name.endswith(':area')):
                        area_val = get_node_value(child)
                        if area_val:
                            area_desc = area_val
            
            # Infer severity if not found
            if severity == "Unknown":
                if any(word in event.lower() for word in ['extreme', 'tornado', 'hurricane', 'blizzard']):
                    severity = "Extreme"
                elif any(word in event.lower() for word in ['severe', 'warning']):
                    severity = "Severe"
                elif any(word in event.lower() for word in ['advisory', 'moderate']):
                    severity = "Moderate"
                else:
                    severity = "Minor"
            
            # Infer urgency if not found
            if urgency == "Unknown":
                if event_type == "Warning":
                    urgency = "Immediate"
                elif event_type == "Watch":
                    urgency = "Expected"
                else:
                    urgency = "Future"
            
            return {
                'id': alert_id,
                'title': title,
                'summary': summary,
                'nws_headline': nws_headline,
                'event': event,
                'event_type': event_type,
                'severity': severity,
                'urgency': urgency,
                'certainty': certainty,
                'effective': effective,
                'expires': expires,
                'area_desc': area_desc,
                'office': office,
                'link': link_url
            }
            
        except Exception as e:
            self.logger.debug(f"Error parsing alert entry: {e}")
            return None
    
    async def _format_alert_compact(self, alert: Dict[str, Any], include_details: bool = True) -> str:
        """Format a single alert compactly (same as wx_command).
        
        Args:
            alert: Alert dict with event, event_type, severity, expires, office, etc.
            include_details: If True, include expiration time and office.
        
        Returns:
            str: Formatted alert string.
        """
        event = alert.get('event', '')
        event_type = alert.get('event_type', '')
        severity = alert.get('severity', 'Unknown')
        expires = alert.get('expires', '')
        office = alert.get('office', '')
        link_url = alert.get('link', '')
        area_desc = alert.get('area_desc', '')
        
        # Get severity emoji
        severity_emoji = {
            'Extreme': '🔴',
            'Severe': '🟠',
            'Moderate': '🟡',
            'Minor': '⚪',
            'Unknown': '⚪'
        }.get(severity, '⚪')
        
        # Format event type abbreviation
        event_type_abbrev = {
            'Warning': 'Warn',
            'Watch': 'Watch',
            'Advisory': 'Adv',
            'Statement': 'Stmt'
        }.get(event_type, event_type)
        
        # Build compact alert string
        if include_details:
            result = severity_emoji
            
            # Add event and type
            if event:
                event_lower = event.lower()
                event_type_lower = event_type.lower()
                if event_type_lower in event_lower:
                    event_short = event
                    if len(event) > 15:
                        words = event.split()
                        if len(words) > 2:
                            event_short = ' '.join(words[:2])
                        else:
                            event_short = event[:15]
                    result += event_short
                else:
                    event_short = event
                    if len(event) > 15:
                        words = event.split()
                        if len(words) > 2:
                            event_short = ' '.join(words[:2])
                        else:
                            event_short = event[:15]
                    result += f"{event_short} {event_type_abbrev}"
            else:
                result += event_type_abbrev
            
            # Add location (area description) if available - compact format
            if area_desc:
                # Extract first location from area_desc (often contains multiple locations)
                # Format: "Seattle, WA" or "King County; Snohomish County" etc.
                locations = [loc.strip() for loc in area_desc.split(';')]
                first_location = locations[0]
                
                # Try to extract just city/area name if it's long
                # e.g., "Seattle, WA" -> "Seattle" or "King County" -> "King"
                if ',' in first_location:
                    # Has state/country - take just the city part
                    location_parts = first_location.split(',')
                    location_short = location_parts[0].strip()
                else:
                    # No comma, might be "King County" -> take first word
                    location_words = first_location.split()
                    if len(location_words) > 1 and location_words[-1].lower() in ['county', 'parish', 'borough']:
                        location_short = location_words[0]
                    else:
                        location_short = first_location
                
                # Limit location length to keep message compact
                if len(location_short) > 20:
                    location_short = location_short[:20]
                
                result += f" {location_short}"
            
            # Add expiration time if available
            if expires:
                expires_compact = self._compact_time(expires)
                if any(month in expires_compact for month in ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]):
                    time_match = re.search(r'(\d+)(AM|PM)', expires_compact, re.IGNORECASE)
                    if time_match:
                        hour = time_match.group(1)
                        am_pm = time_match.group(2)
                        expires_short = f" til {hour}{am_pm}"
                    else:
                        expires_short = f" til {expires_compact[:15]}"
                else:
                    time_match = re.search(r'(\d+):?(\d+)?(AM|PM)', expires_compact, re.IGNORECASE)
                    if time_match:
                        hour = time_match.group(1)
                        am_pm = time_match.group(3)
                        expires_short = f" til {hour}{am_pm}"
                    else:
                        expires_short = f" til {expires_compact[:15]}"
                result += expires_short
            
            # Add office if available (abbreviate city name)
            if office:
                office_parts = office.split()
                if len(office_parts) >= 2:
                    office_org = office_parts[0]
                    city = office_parts[1] if len(office_parts) > 1 else ""
                    city_abbrev = self._abbreviate_city_name(city)
                    office_short = f" by {office_org} {city_abbrev}"
                else:
                    office_short = f" by {office[:10]}"
                result += office_short
            
            # Add shortened URL if available and there's space (within 130 char limit)
            if link_url and len(result) < 100:  # Leave ~30 chars for shortened URL
                short_url = await self._shorten_url(link_url)
                if short_url:
                    test_result = result + f" {short_url}"
                    if len(test_result) <= 130:  # Mesh message limit
                        result = test_result
                    # If even shortened doesn't fit, try with just a link indicator
                    elif len(result) < 120:
                        result = result + " 🔗"
            
            return result
        else:
            return f"{severity_emoji}{event} {event_type_abbrev}" if event else f"{severity_emoji}{event_type_abbrev}"
    
    def _compact_time(self, time_str: str) -> str:
        """Compact time format (same as wx_command).
        
        Args:
            time_str: Time string to format.
            
        Returns:
            str: Compact formatted time string.
        """
        if not time_str:
            return time_str
        
        # Check if it's ISO format
        if 'T' in time_str and re.match(r'\d{4}-\d{2}-\d{2}T', time_str):
            try:
                dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                month_abbrevs = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                month = month_abbrevs[dt.month - 1]
                day = dt.day
                hour = dt.hour
                
                if hour == 0:
                    hour_12 = 12
                    am_pm = "AM"
                elif hour < 12:
                    hour_12 = hour
                    am_pm = "AM"
                elif hour == 12:
                    hour_12 = 12
                    am_pm = "PM"
                else:
                    hour_12 = hour - 12
                    am_pm = "PM"
                
                return f"{month} {day} {hour_12}{am_pm}"
            except Exception:
                pass
        
        # Remove leading zeros from hours
        time_str = re.sub(r'(\d+):00(AM|PM)', r'\1\2', time_str)
        
        # Abbreviate month names
        month_abbrevs = {
            "January": "Jan", "February": "Feb", "March": "Mar", "April": "Apr",
            "May": "May", "June": "Jun", "July": "Jul", "August": "Aug",
            "September": "Sep", "October": "Oct", "November": "Nov", "December": "Dec"
        }
        for full, abbrev in month_abbrevs.items():
            time_str = time_str.replace(full, abbrev)
        
        # Remove "at" before time
        time_str = re.sub(r'\s+at\s+', ' ', time_str)
        
        return time_str
    
    def _abbreviate_city_name(self, city: str) -> str:
        """Abbreviate city names for compact display (same as wx_command).
        
        Args:
            city: Full city name.
            
        Returns:
            str: Abbreviated city name.
        """
        if not city:
            return city
        
        city_abbrevs = {
            "Seattle": "SEA", "Portland": "PDX", "San Francisco": "SF",
            "Los Angeles": "LA", "New York": "NYC", "Chicago": "CHI",
            "Houston": "HOU", "Phoenix": "PHX", "Philadelphia": "PHL",
            "San Antonio": "SAT", "San Diego": "SAN", "Dallas": "DAL",
            "San Jose": "SJC", "Austin": "AUS", "Jacksonville": "JAX",
            "Columbus": "CMH", "Fort Worth": "FTW", "Charlotte": "CLT",
            "Denver": "DEN", "Washington": "DC", "Boston": "BOS",
            "El Paso": "ELP", "Detroit": "DTW", "Nashville": "BNA",
            "Oklahoma City": "OKC", "Las Vegas": "LAS", "Memphis": "MEM",
            "Louisville": "SDF", "Baltimore": "BWI", "Milwaukee": "MKE",
            "Albuquerque": "ABQ", "Tucson": "TUS", "Fresno": "FAT",
            "Sacramento": "SAC", "Kansas City": "KC", "Mesa": "MSC",
            "Atlanta": "ATL", "Omaha": "OMA", "Colorado Springs": "COS",
            "Raleigh": "RDU", "Virginia Beach": "ORF", "Miami": "MIA",
            "Oakland": "OAK", "Minneapolis": "MSP", "Tulsa": "TUL",
            "Cleveland": "CLE", "Wichita": "ICT", "Arlington": "ARL",
            "Tampa": "TPA", "New Orleans": "MSY", "Honolulu": "HNL",
            "Anchorage": "ANC", "Bellingham": "BLI", "Everett": "EVE",
            "Spokane": "GEG", "Tacoma": "TAC", "Yakima": "YKM",
            "Olympia": "OLM", "Vancouver": "YVR", "Victoria": "YYJ"
        }
        
        if city in city_abbrevs:
            return city_abbrevs[city]
        
        for full_name, abbrev in city_abbrevs.items():
            if full_name in city:
                return abbrev
        
        words = city.split()
        if len(words) > 1:
            abbrev = ''.join([w[0].upper() for w in words[:3]])
            if len(abbrev) <= 4:
                return abbrev
        
        return city[:4].upper() if len(city) >= 4 else city.upper()
    
    def _parse_iso_time(self, time_str: str) -> Optional[float]:
        """Parse ISO 8601 timestamp to Unix timestamp.
        
        Args:
            time_str: ISO 8601 time string (e.g., "2025-12-16T15:12:00-08:00" or "2025-12-16T15:12:00Z").
        
        Returns:
            Optional[float]: Unix timestamp (seconds since epoch), or None if parsing fails.
        """
        if not time_str:
            return None
        
        try:
            # Handle ISO format with timezone
            dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            return dt.timestamp()
        except (ValueError, AttributeError):
            return None
    
    def _parse_alert_time(self, time_str: str) -> Optional[float]:
        """Parse alert effective/issued time string to Unix timestamp.
        
        Args:
            time_str: Time string from alert (e.g., "December 16 at 3:12PM PST" or ISO format).
        
        Returns:
            Optional[float]: Unix timestamp (seconds since epoch), or None if parsing fails.
        """
        if not time_str:
            return None
        
        # Try ISO format first (e.g., "2025-12-16T15:12:00-08:00")
        if 'T' in time_str or time_str.startswith('202'):
            try:
                dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                return dt.timestamp()
            except (ValueError, AttributeError):
                pass
        
        # Try parsing from title format: "issued December 16 at 3:12PM PST"
        # This is a fallback for when effective time is in text format
        try:
            # Look for date and time patterns
            # Pattern: "December 16 at 3:12PM" or "Dec 16 3:12PM"
            date_match = re.search(r'(\w+)\s+(\d+)', time_str)
            time_match = re.search(r'(\d+):?(\d+)?(AM|PM)', time_str, re.IGNORECASE)
            
            if date_match and time_match:
                # For simplicity, assume it's recent (within last 7 days)
                # This is a rough estimate - we'll use current time as fallback
                # The important thing is we can compare relative times
                now = datetime.now()
                # Try to extract day
                day = int(date_match.group(2))
                hour_str = time_match.group(1)
                am_pm = time_match.group(3).upper()
                
                hour = int(hour_str)
                if am_pm == 'PM' and hour != 12:
                    hour += 12
                elif am_pm == 'AM' and hour == 12:
                    hour = 0
                
                # Estimate: assume it's today or yesterday if day matches
                # This is approximate but good enough for filtering
                if day == now.day:
                    # Today
                    dt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                elif day == (now.day - 1) or (now.day == 1 and day >= 28):
                    # Yesterday or last month
                    dt = (now - timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)
                else:
                    # Rough estimate: assume within last week
                    dt = now.replace(day=day, hour=hour, minute=0, second=0, microsecond=0)
                    if dt > now:
                        dt = dt - timedelta(days=30)  # Probably last month
                
                return dt.timestamp()
        except Exception:
            pass
        
        # If all parsing fails, return None (will use current time as fallback)
        return None
    
    def _convert_cap_url_to_html(self, cap_url: str) -> str:
        """Convert CAP XML URL to a more readable format.
        
        For NOAA alerts, converts CAP XML URLs to API URLs that return JSON format.
        According to NWS API documentation (https://www.weather.gov/documentation/services-web-api),
        the API supports content negotiation and returns GeoJSON by default, which browsers
        can display in a readable format with syntax highlighting.
        
        Note: The NWS alerts webpage has been decommissioned, so there is no direct HTML
        view of individual alerts. The API JSON format is the most readable option available.
        
        Args:
            cap_url: CAP XML URL (e.g., https://api.weather.gov/alerts/urn:oid:....cap)
        
        Returns:
            str: API URL that returns JSON format (more readable than XML).
        """
        if not cap_url:
            return cap_url
        
        # Check if this is a NOAA API alert URL
        if "api.weather.gov/alerts/" in cap_url:
            # Extract alert identifier from URL
            # Pattern: https://api.weather.gov/alerts/urn:oid:... or ...urn:oid:....cap
            parts = cap_url.split("/alerts/")
            if len(parts) > 1:
                alert_id = parts[1].split("?")[0].split("#")[0]  # Remove query params and fragments
                # Remove .cap extension if present
                if alert_id.endswith(".cap"):
                    alert_id = alert_id[:-4]
                # Use the API URL without .cap extension
                # Per NWS API docs, this returns GeoJSON by default (application/geo+json)
                # which browsers can display with syntax highlighting, making it readable
                # This is the best available option since the alerts webpage was decommissioned
                return f"https://api.weather.gov/alerts/{alert_id}"
        
        # Check if URL ends with .cap or contains alert identifier
        if cap_url.endswith(".cap") or "urn:oid" in cap_url or "urn_oid" in cap_url:
            # Try to extract the alert identifier
            # Pattern: urn:oid:... or urn_oid_... (may include .cap extension)
            # First try to extract from the path
            if "/alerts/" in cap_url:
                parts = cap_url.split("/alerts/")
                if len(parts) > 1:
                    alert_id = parts[1].split("?")[0].split("#")[0]
                    if alert_id.endswith(".cap"):
                        alert_id = alert_id[:-4]
                    # Convert underscores to colons if needed
                    alert_id = alert_id.replace("_", ":")
                    # Use the API URL without .cap extension
                    # Returns GeoJSON format which browsers display nicely
                    return f"https://api.weather.gov/alerts/{alert_id}"
            
            # Fallback: extract using regex
            match = re.search(r'urn[:_]oid[:_]([^./?&#]+)', cap_url)
            if match:
                alert_id = match.group(1).replace("_", ":")
                # Remove .cap if it was captured
                if alert_id.endswith(".cap"):
                    alert_id = alert_id[:-4]
                # Use the API URL - returns GeoJSON format
                return f"https://api.weather.gov/alerts/{alert_id}"
        
        # If we can't convert it, return the original URL
        # The URL shortener might still work, but users will get XML
        return cap_url
    
    async def _shorten_url(self, url: str) -> str:
        """Shorten URL using is.gd service.
        
        Args:
            url: Full URL to shorten.
        
        Returns:
            str: Shortened URL string, or empty string on error.
        """
        if not url:
            return ""
        
        try:
            # Use is.gd URL shortener API
            # Run the synchronous request in a thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            encoded_url = quote(url, safe='')
            shortener_url = f"https://is.gd/create.php?format=simple&url={encoded_url}"
            
            # Run the synchronous request in executor to avoid blocking
            response = await loop.run_in_executor(
                None,
                lambda: self.api_session.get(shortener_url, timeout=5)
            )
            
            if response.ok:
                short_url = response.text.strip()
                # is.gd returns the shortened URL or an error message
                # Check if it looks like a valid URL
                if short_url.startswith('http'):
                    return short_url
                else:
                    # Error message from is.gd - log it
                    self.logger.debug(f"is.gd returned error: {short_url}")
                    # Try alternative: use v.gd (is.gd's alternative)
                    try:
                        alt_url = f"https://v.gd/create.php?format=simple&url={encoded_url}"
                        alt_response = await loop.run_in_executor(
                            None,
                            lambda: self.api_session.get(alt_url, timeout=5)
                        )
                        if alt_response.ok:
                            alt_short = alt_response.text.strip()
                            if alt_short.startswith('http'):
                                return alt_short
                    except Exception:
                        pass
                    return ""
            else:
                self.logger.debug(f"Error shortening URL: HTTP {response.status_code}")
                return ""
                
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            self.logger.debug(f"Error shortening URL: {e}")
            return ""
        except Exception as e:
            self.logger.debug(f"Unexpected error shortening URL: {e}")
            return ""

