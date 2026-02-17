#!/usr/bin/env python3
"""
WXSIM Plaintext Parser
Parses WXSIM plaintext.txt forecast files into structured data

Based on the PHP parser by Ken True (Saratoga-Weather.org)
https://github.com/ktrue/WXSIM-forecast/blob/master/plaintext-parser.php

Usage:
    from modules.clients.wxsim_parser import WXSIMParser
    
    # Parse from URL
    parser = WXSIMParser()
    text = parser.fetch_from_url('https://example.com/plaintext.txt')
    if text:
        forecast = parser.parse(text)
        current = parser.format_current_conditions(forecast, temp_unit='fahrenheit', wind_unit='mph')
        summary = parser.format_forecast_summary(forecast, num_days=7, temp_unit='fahrenheit', wind_unit='mph')
    
    # Or parse from file/string
    with open('plaintext.txt', 'r') as f:
        text = f.read()
    forecast = parser.parse(text)
    
    # Access structured data
    for period in forecast.periods:
        print(f"{period.day_name}: {period.conditions} {period.high_temp}°C/{period.low_temp}°C")
"""

import re
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum


class PeriodType(Enum):
    """Forecast period type"""
    DAY = "day"
    NIGHT = "night"
    UNKNOWN = "unknown"


@dataclass
class HourlyData:
    """Single hour of forecast data"""
    date: str  # e.g., "May 5"
    time: str  # e.g., "7:00 A" or "12:00 P"
    hour: int  # 0-23
    temperature: float
    wind_speed: int
    humidity: int
    sky_cover: int  # %SC
    visibility: int  # %VST
    visibility_miles: float  # VIS
    precip_chance: int  # PC/HR
    rain_total: float  # RN TOT
    weather: str  # Weather condition text


@dataclass
class ForecastPeriod:
    """A forecast period (day or night)"""
    day_name: str  # e.g., "Friday", "Today"
    date: str  # e.g., "May 5"
    period_type: PeriodType
    high_temp: Optional[float] = None
    low_temp: Optional[float] = None
    conditions: str = ""
    wind_speed: Optional[int] = None
    wind_direction: Optional[str] = None
    precip_chance: Optional[int] = None
    precip_amount: Optional[float] = None
    hourly_data: List[HourlyData] = field(default_factory=list)


@dataclass
class WXSIMForecast:
    """Complete WXSIM forecast data"""
    city: str = ""
    station: str = ""
    update_time: str = ""
    update_date: str = ""
    periods: List[ForecastPeriod] = field(default_factory=list)
    hourly_data: List[HourlyData] = field(default_factory=list)
    raw_text: str = ""


class WXSIMParser:
    """Parser for WXSIM plaintext.txt files"""
    
    # Weather condition mappings (abbreviated to full descriptions)
    WEATHER_CONDITIONS = {
        'CLEAR': 'Clear',
        'SUNNY': 'Sunny',
        'FAIR': 'Fair',
        'FAIR-P.C.': 'Fair to Partly Cloudy',
        'P.CLOUDY': 'Partly Cloudy',
        'P.-M.CLDY': 'Partly to Mostly Cloudy',
        'M.CLOUDY': 'Mostly Cloudy',
        'M.C.-CLDY': 'Mostly Cloudy',
        'CLOUDY': 'Cloudy',
        'DNS.OVCST': 'Dense Overcast',
        'OVCST': 'Overcast',
        'FOGGY': 'Foggy',
        'DRIZZLE': 'Drizzle',
        'DRZL': 'Drizzle',
        'CHNC. DRZL': 'Chance Drizzle',
        'CHNC. SHWR': 'Chance Showers',
        'SHOWERS': 'Showers',
        'RAIN': 'Rain',
        'CHNC. RAIN': 'Chance Rain',
        'SNOW': 'Snow',
        'CHNC. SNOW': 'Chance Snow',
        'T-STM': 'Thunderstorm',
        'CHNC. T-STM': 'Chance Thunderstorm',
    }
    
    def __init__(self):
        """Initialize the parser"""
        self.current_year = datetime.now().year
        self.current_date = datetime.now()
    
    def parse(self, text: str) -> WXSIMForecast:
        """Parse WXSIM plaintext content.
        
        Args:
            text: The plaintext content from WXSIM plaintext.txt file
            
        Returns:
            WXSIMForecast: Parsed forecast data
        """
        forecast = WXSIMForecast()
        forecast.raw_text = text
        
        lines = text.split('\n')
        
        # Find FORECAST RUN section (skip calibration)
        forecast_start = self._find_forecast_start(lines)
        if forecast_start == -1:
            return forecast
        
        # Parse header info (city, station, date)
        self._parse_header(lines[:forecast_start], forecast)
        
        # Parse forecast data
        forecast_lines = lines[forecast_start:]
        hourly_data = self._parse_hourly_data(forecast_lines)
        forecast.hourly_data = hourly_data
        
        # Extract forecast date/time from first data point
        if hourly_data:
            first_data = hourly_data[0]
            forecast.update_date = first_data.date  # e.g., "May 5"
            forecast.update_time = first_data.time   # e.g., "7:00 A"
        
        # Group into periods (days)
        forecast.periods = self._group_into_periods(hourly_data, forecast_lines)
        
        return forecast
    
    def _find_forecast_start(self, lines: List[str]) -> int:
        """Find the start of the FORECAST RUN section.
        
        Args:
            lines: All lines from the file
            
        Returns:
            int: Index of first forecast data line, or -1 if not found
        """
        for i, line in enumerate(lines):
            if 'FORECAST RUN:' in line.upper():
                # Find the header line "DATE    TIME   TEMP..."
                for j in range(i, min(i + 10, len(lines))):
                    if 'DATE' in lines[j] and 'TIME' in lines[j] and 'TEMP' in lines[j]:
                        # Return line after header
                        return j + 2  # Skip header and blank line
        return -1
    
    def _parse_header(self, lines: List[str], forecast: WXSIMForecast) -> None:
        """Parse header information (city, station, date).
        
        Args:
            lines: Header lines (before FORECAST RUN)
            forecast: Forecast object to populate
        """
        # Look for city/station info in header
        # WXSIM format may vary, so we'll extract what we can
        for line in lines:
            # Look for common patterns
            if 'FORECAST FOR' in line.upper():
                # Extract city name
                parts = line.split('FORECAST FOR', 1)
                if len(parts) > 1:
                    forecast.city = parts[1].strip()
            elif 'BY' in line.upper() and not forecast.station:
                # Extract station/forecaster name
                parts = line.split('BY', 1)
                if len(parts) > 1:
                    forecast.station = parts[1].strip()
    
    def _parse_hourly_data(self, lines: List[str]) -> List[HourlyData]:
        """Parse hourly forecast data rows.
        
        Args:
            lines: Lines from FORECAST RUN section onwards
            
        Returns:
            List[HourlyData]: Parsed hourly data
        """
        hourly_data = []
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Skip non-data lines
            if (line.startswith('DATE') or 
                line.startswith('FORECAST') or
                line.startswith('CALIBRATION') or
                line.startswith('SURFACE WIND') or
                line.startswith('AUTO CLOUDS') or
                line.startswith('Press') or
                line.startswith('(Automatically') or
                line.startswith('-') or
                line.startswith('(')):
                continue
            
            # Try to parse as data row
            # Format: "May 5   7:00 A    9.3    0   73    95   50  42.2   3   0.0   M.C.-CLDY CHNC. DRZL"
            data = self._parse_data_row(line)
            if data:
                hourly_data.append(data)
        
        return hourly_data
    
    def _parse_data_row(self, line: str) -> Optional[HourlyData]:
        """Parse a single data row.
        
        Args:
            line: Single line of forecast data
            
        Returns:
            Optional[HourlyData]: Parsed data or None if invalid
        """
        # Pattern: Month Day  Time  Temp  Wind  Hum  %SC  %VST  VIS  PC/HR  RN TOT  Weather
        # Example: "May 5   7:00 A    9.3    0   73    95   50  42.2   3   0.0   M.C.-CLDY CHNC. DRZL"
        
        # Match month and day at start
        match = re.match(r'^([A-Za-z]+)\s+(\d+)\s+(\d{1,2}):(\d{2})\s+([AP])\s+', line)
        if not match:
            return None
        
        month_name = match.group(1)
        day = int(match.group(2))
        hour_12 = int(match.group(3))
        minute = int(match.group(4))
        am_pm = match.group(5)
        
        # Convert to 24-hour
        hour_24 = hour_12
        if am_pm == 'P' and hour_12 != 12:
            hour_24 = hour_12 + 12
        elif am_pm == 'A' and hour_12 == 12:
            hour_24 = 0
        
        # Extract remaining fields (split by whitespace)
        rest = line[match.end():].strip()
        parts = rest.split()
        
        if len(parts) < 8:
            return None
        
        try:
            # Parse numeric fields
            temp = float(parts[0])
            wind = int(parts[1])
            humidity = int(parts[2])
            sky_cover = int(parts[3])
            visibility_pct = int(parts[4])
            visibility_miles = float(parts[5])
            precip_chance = int(parts[6])
            rain_total = float(parts[7])
            
            # Weather condition is everything after the numeric fields
            weather = ' '.join(parts[8:]) if len(parts) > 8 else ''
            
            # Format date string
            date_str = f"{month_name} {day}"
            time_str = f"{hour_12}:{minute:02d} {am_pm}"
            
            return HourlyData(
                date=date_str,
                time=time_str,
                hour=hour_24,
                temperature=temp,
                wind_speed=wind,
                humidity=humidity,
                sky_cover=sky_cover,
                visibility=visibility_pct,
                visibility_miles=visibility_miles,
                precip_chance=precip_chance,
                rain_total=rain_total,
                weather=weather
            )
        except (ValueError, IndexError):
            return None
    
    def _group_into_periods(self, hourly_data: List[HourlyData], lines: List[str]) -> List[ForecastPeriod]:
        """Group hourly data into forecast periods (days).
        
        Args:
            hourly_data: List of hourly data points
            lines: Original lines (to find day separators)
            
        Returns:
            List[ForecastPeriod]: Forecast periods
        """
        periods = []
        
        if not hourly_data:
            return periods
        
        # Find day separators in original lines
        day_separators = self._find_day_separators(lines)
        
        # Group hourly data by day
        current_day = None
        current_period_data = []
        
        for data in hourly_data:
            # Check if this is a new day (by date string or hour reset)
            if current_day is None or data.date != current_day:
                # Save previous period if exists
                if current_period_data:
                    period = self._create_period_from_hourly(current_day, current_period_data, day_separators)
                    if period:
                        periods.append(period)
                
                # Start new day
                current_day = data.date
                current_period_data = [data]
            else:
                current_period_data.append(data)
        
        # Add final period
        if current_period_data:
            period = self._create_period_from_hourly(current_day, current_period_data, day_separators)
            if period:
                periods.append(period)
        
        return periods
    
    def _find_day_separators(self, lines: List[str]) -> Dict[str, str]:
        """Find day name separators in the file.
        
        Args:
            lines: All lines from the file
            
        Returns:
            Dict[str, str]: Mapping of date string to day name
        """
        separators = {}
        
        # Look for lines like "                                Friday"
        for i, line in enumerate(lines):
            line_stripped = line.strip()
            # Check if line contains a day name
            day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
            for day_name in day_names:
                if day_name in line_stripped:
                    # Try to find the date from nearby lines
                    # Look at next few lines for date pattern
                    for j in range(i + 1, min(i + 5, len(lines))):
                        date_match = re.search(r'([A-Za-z]+)\s+(\d+)', lines[j])
                        if date_match:
                            date_str = f"{date_match.group(1)} {date_match.group(2)}"
                            separators[date_str] = day_name
                            break
                    break
        
        return separators
    
    def _create_period_from_hourly(self, date: str, hourly_data: List[HourlyData], 
                                   day_separators: Dict[str, str]) -> Optional[ForecastPeriod]:
        """Create a forecast period from hourly data.
        
        Args:
            date: Date string (e.g., "May 5")
            hourly_data: Hourly data for this day
            day_separators: Mapping of dates to day names
            
        Returns:
            Optional[ForecastPeriod]: Forecast period or None
        """
        if not hourly_data:
            return None
        
        # Get day name - try to determine from date or use separator
        day_name = day_separators.get(date)
        if not day_name:
            # Try to determine day name from date
            try:
                # Parse date (e.g., "May 5")
                month_name, day_num = date.split()
                month_map = {
                    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
                }
                month = month_map.get(month_name[:3], 1)
                day = int(day_num)
                
                # Create datetime and get day name
                forecast_date = datetime(self.current_year, month, day)
                day_name = forecast_date.strftime('%A')
                
                # If it's today, use "Today" instead
                today = datetime.now()
                if forecast_date.date() == today.date():
                    day_name = "Today"
                elif forecast_date.date() == (today + timedelta(days=1)).date():
                    day_name = "Tomorrow"
            except (ValueError, KeyError):
                day_name = "Today"
        
        # Determine period type (day vs night)
        # Day: roughly 6 AM to 6 PM, Night: 6 PM to 6 AM
        day_hours = [d for d in hourly_data if 6 <= d.hour < 18]
        night_hours = [d for d in hourly_data if d.hour < 6 or d.hour >= 18]
        
        # Use the period with more data, or default to day
        if len(day_hours) >= len(night_hours):
            period_type = PeriodType.DAY
            primary_data = day_hours if day_hours else hourly_data
        else:
            period_type = PeriodType.NIGHT
            primary_data = night_hours if night_hours else hourly_data
        
        # Calculate high/low temps
        temps = [d.temperature for d in hourly_data]
        high_temp = max(temps) if temps else None
        low_temp = min(temps) if temps else None
        
        # Get most common weather condition
        conditions = self._get_primary_condition(hourly_data)
        
        # Get average wind speed
        wind_speeds = [d.wind_speed for d in hourly_data if d.wind_speed > 0]
        avg_wind = int(sum(wind_speeds) / len(wind_speeds)) if wind_speeds else None
        
        # Get max precip chance
        precip_chances = [d.precip_chance for d in hourly_data]
        max_precip_chance = max(precip_chances) if precip_chances else None
        
        # Get total precipitation
        total_precip = sum(d.rain_total for d in hourly_data)
        
        period = ForecastPeriod(
            day_name=day_name,
            date=date,
            period_type=period_type,
            high_temp=high_temp,
            low_temp=low_temp,
            conditions=conditions,
            wind_speed=avg_wind,
            precip_chance=max_precip_chance,
            precip_amount=total_precip if total_precip > 0 else None,
            hourly_data=hourly_data
        )
        
        return period
    
    def _get_primary_condition(self, hourly_data: List[HourlyData]) -> str:
        """Get the primary weather condition from hourly data.
        
        Args:
            hourly_data: List of hourly data points
            
        Returns:
            str: Primary weather condition description
        """
        if not hourly_data:
            return "Unknown"
        
        # Count condition occurrences
        condition_counts = {}
        for data in hourly_data:
            # Normalize condition text
            condition = data.weather.strip().upper()
            if condition:
                # Expand abbreviations
                for abbrev, full in self.WEATHER_CONDITIONS.items():
                    if abbrev in condition:
                        condition = full
                        break
                condition_counts[condition] = condition_counts.get(condition, 0) + 1
        
        if not condition_counts:
            return "Unknown"
        
        # Return most common condition
        return max(condition_counts.items(), key=lambda x: x[1])[0]
    
    def format_current_conditions(self, forecast: WXSIMForecast, 
                                  temp_unit: str = 'celsius',
                                  wind_unit: str = 'kph') -> str:
        """Format current conditions for display.
        
        Args:
            forecast: Parsed forecast data
            temp_unit: Temperature unit ('celsius' or 'fahrenheit')
            wind_unit: Wind speed unit ('kph', 'mph', or 'ms')
            
        Returns:
            str: Formatted current conditions string
        """
        if not forecast.hourly_data:
            return "No current data available"
        
        # Get the first hour from FORECAST RUN (this is the current/starting conditions)
        # The last hour would be in the future, so we use the first one
        current = forecast.hourly_data[0]
        
        # Convert temperature
        temp = self._convert_temp(current.temperature, temp_unit)
        temp_symbol = "°F" if temp_unit == 'fahrenheit' else "°C"
        
        # Convert wind speed
        wind = self._convert_wind(current.wind_speed, wind_unit)
        wind_unit_str = self._get_wind_unit_str(wind_unit)
        
        # Format condition
        condition = self._normalize_condition(current.weather)
        
        # Build string
        result = f"{condition} {temp}{temp_symbol}"
        
        if current.wind_speed > 0:
            result += f" Wind {wind}{wind_unit_str}"
        
        if current.humidity > 0:
            result += f" {current.humidity}%RH"
        
        if current.precip_chance > 0:
            result += f" {current.precip_chance}% PoP"
        
        return result
    
    def format_forecast_summary(self, forecast: WXSIMForecast, num_days: int = 7,
                               temp_unit: str = 'celsius',
                               wind_unit: str = 'kph') -> str:
        """Format forecast summary for display.
        
        Args:
            forecast: Parsed forecast data
            num_days: Number of days to include
            temp_unit: Temperature unit ('celsius' or 'fahrenheit')
            wind_unit: Wind speed unit ('kph', 'mph', or 'ms')
            
        Returns:
            str: Formatted forecast summary
        """
        if not forecast.periods:
            return "No forecast data available"
        
        parts = []
        for period in forecast.periods[:num_days]:
            # Convert temps
            high = self._convert_temp(period.high_temp, temp_unit) if period.high_temp else None
            low = self._convert_temp(period.low_temp, temp_unit) if period.low_temp else None
            temp_symbol = "°F" if temp_unit == 'fahrenheit' else "°C"
            
            # Format day
            day_abbrev = period.day_name[:3] if len(period.day_name) > 3 else period.day_name
            
            # Build period string
            period_str = f"{day_abbrev}: {period.conditions}"
            if high is not None and low is not None:
                period_str += f" {high}{temp_symbol}/{low}{temp_symbol}"
            elif high is not None:
                period_str += f" {high}{temp_symbol}"
            elif low is not None:
                period_str += f" {low}{temp_symbol}"
            
            if period.precip_chance and period.precip_chance > 30:
                period_str += f" {period.precip_chance}% PoP"
            
            parts.append(period_str)
        
        return "\n".join(parts)
    
    def _convert_temp(self, temp_c: float, unit: str) -> float:
        """Convert temperature from Celsius to requested unit.
        
        Args:
            temp_c: Temperature in Celsius
            unit: Target unit ('celsius' or 'fahrenheit')
            
        Returns:
            float: Converted temperature
        """
        if unit == 'fahrenheit':
            return round((temp_c * 9/5) + 32, 1)
        return round(temp_c, 1)
    
    def _convert_wind(self, wind_kph: int, unit: str) -> float:
        """Convert wind speed from km/h to requested unit.
        
        Args:
            wind_kph: Wind speed in km/h (WXSIM default unit)
            unit: Target unit ('kph', 'mph', or 'ms')
            
        Returns:
            float: Converted wind speed
        """
        # WXSIM outputs wind in km/h, but the values might be in different units
        # depending on configuration. We'll assume km/h as default.
        if unit == 'mph':
            return round(wind_kph * 0.621371, 1)
        elif unit == 'ms':
            return round(wind_kph / 3.6, 1)
        return float(wind_kph)
    
    def _get_wind_unit_str(self, unit: str) -> str:
        """Get wind speed unit string.
        
        Args:
            unit: Wind unit ('kph', 'mph', or 'ms')
            
        Returns:
            str: Unit string
        """
        unit_map = {
            'kph': 'km/h',
            'mph': 'mph',
            'ms': 'm/s'
        }
        return unit_map.get(unit, 'km/h')
    
    def _normalize_condition(self, condition: str) -> str:
        """Normalize weather condition text.
        
        Args:
            condition: Raw condition text from WXSIM
            
        Returns:
            str: Normalized condition description
        """
        condition_upper = condition.strip().upper()
        
        # Try to match abbreviations
        for abbrev, full in self.WEATHER_CONDITIONS.items():
            if abbrev in condition_upper:
                return full
        
        # Return original if no match
        return condition.strip() if condition else "Unknown"
    
    def get_forecast_date(self, forecast: WXSIMForecast) -> Optional[datetime]:
        """Get the forecast date as a datetime object.
        
        Args:
            forecast: Parsed forecast data
            
        Returns:
            Optional[datetime]: Forecast date/time or None if unavailable
        """
        if not forecast.update_date:
            return None
        
        try:
            # Parse date string like "May 5"
            month_name, day_num = forecast.update_date.split()
            month_map = {
                'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
            }
            month = month_map.get(month_name[:3], 1)
            day = int(day_num)
            
            # Parse time if available
            hour = 0
            minute = 0
            if forecast.update_time:
                # Parse time like "7:00 A" or "12:30 P"
                time_match = re.match(r'(\d{1,2}):(\d{2})\s+([AP])', forecast.update_time)
                if time_match:
                    hour_12 = int(time_match.group(1))
                    minute = int(time_match.group(2))
                    am_pm = time_match.group(3)
                    
                    # Convert to 24-hour
                    hour = hour_12
                    if am_pm == 'P' and hour_12 != 12:
                        hour = hour_12 + 12
                    elif am_pm == 'A' and hour_12 == 12:
                        hour = 0
            
            # Create datetime - try current year first, but if that's in the future,
            # it's likely from last year
            forecast_datetime = datetime(self.current_year, month, day, hour, minute)
            
            # If forecast date is in the future, assume it's from last year
            # (WXSIM forecasts are typically generated for the current/upcoming period)
            if forecast_datetime > self.current_date:
                # Try previous year
                forecast_datetime = datetime(self.current_year - 1, month, day, hour, minute)
                # If that's also in the future (shouldn't happen), keep original
                if forecast_datetime > self.current_date:
                    forecast_datetime = datetime(self.current_year, month, day, hour, minute)
            
            return forecast_datetime
        except (ValueError, KeyError, AttributeError):
            return None
    
    def is_forecast_stale(self, forecast: WXSIMForecast, max_age_hours: int = 48) -> Tuple[bool, Optional[str]]:
        """Check if forecast is stale (too old).
        
        Args:
            forecast: Parsed forecast data
            max_age_hours: Maximum age in hours before considered stale (default: 48)
            
        Returns:
            Tuple[bool, Optional[str]]: (is_stale, reason_message)
        """
        forecast_date = self.get_forecast_date(forecast)
        if not forecast_date:
            return True, "Could not determine forecast date"
        
        now = datetime.now()
        age = now - forecast_date
        age_hours = age.total_seconds() / 3600
        
        if age_hours > max_age_hours:
            return True, f"Forecast is {age_hours:.1f} hours old (max: {max_age_hours}h)"
        
        if age_hours < 0:
            # Forecast is in the future (shouldn't happen, but handle gracefully)
            return True, f"Forecast date is in the future: {forecast_date}"
        
        return False, None
    
    @staticmethod
    def fetch_from_url(url: str, timeout: int = 10) -> Optional[str]:
        """Fetch WXSIM plaintext data from a URL.
        
        Args:
            url: URL to fetch plaintext.txt from
            timeout: Request timeout in seconds
            
        Returns:
            Optional[str]: Plaintext content or None on error
        """
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            text = response.text
            # Verify it looks like WXSIM data
            if 'FORECAST RUN' in text.upper() or 'DATE' in text:
                return text
            return None
        except (requests.RequestException, ValueError, AttributeError):
            return None
