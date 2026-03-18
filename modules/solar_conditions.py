#!/usr/bin/env python3
"""
Solar and Astronomical Conditions Module
Provides functions for HF band conditions, solar data, moon phases, and satellite passes
Adapted from MeshLink bot by K7MHI Kelly Keeton 2024
"""

import logging
import xml.dom.minidom
from datetime import datetime, timezone

import ephem
import requests

logger = logging.getLogger(__name__)

# Default values (can be overridden via config)
DEFAULT_LATITUDE = 40.7128  # New York City
DEFAULT_LONGITUDE = -74.0060
DEFAULT_URL_TIMEOUT = 10
DEFAULT_ZULU_TIME = False
DEFAULT_N2YO_API_KEY = ""

# Global config reference (will be set by bot initialization)
_config = None

def set_config(config):
    """Set the global config reference"""
    global _config
    _config = config

def get_config_value(section, key, fallback):
    """Get config value with fallback"""
    if _config and _config.has_section(section):
        value = _config.get(section, key, fallback=fallback)

        # Convert numeric values to appropriate types
        if key in ['url_timeout', 'bot_latitude', 'bot_longitude']:
            try:
                if key == 'url_timeout':
                    return int(value)
                else:
                    return float(value)
            except (ValueError, TypeError):
                return fallback

        # Handle boolean values
        if key == 'use_zulu_time':
            if isinstance(value, str):
                return value.lower() in ['true', '1', 'yes', 'on']
            return bool(value)

        return value
    return fallback

# Error message for failed data fetching
ERROR_FETCHING_DATA = "Error fetching data"

def hf_band_conditions():
    """Get ham radio HF band conditions from hamsql.com"""
    try:
        hf_cond = ""
        timeout = get_config_value('Solar_Config', 'url_timeout', DEFAULT_URL_TIMEOUT)
        band_cond = requests.get("https://www.hamqsl.com/solarxml.php", timeout=timeout)
        if band_cond.ok:
            solarxml = xml.dom.minidom.parseString(band_cond.text)
            for i in solarxml.getElementsByTagName("band"):
                hf_cond += i.getAttribute("time")[0] + i.getAttribute("name") + "=" + str(i.childNodes[0].data) + "\n"
            hf_cond = hf_cond[:-1]  # remove the last newline
        else:
            logger.error("Solar: Error fetching HF band conditions")
            hf_cond = ERROR_FETCHING_DATA

        return hf_cond
    except Exception as e:
        logger.error(f"Solar: Exception in hf_band_conditions: {e}")
        return ERROR_FETCHING_DATA

def solar_conditions():
    """Get radio related solar conditions from hamsql.com"""
    try:
        solar_cond = ""
        timeout = get_config_value('Solar_Config', 'url_timeout', DEFAULT_URL_TIMEOUT)
        solar_cond = requests.get("https://www.hamqsl.com/solarxml.php", timeout=timeout)
        if solar_cond.ok:
            solar_xml = xml.dom.minidom.parseString(solar_cond.text)
            for i in solar_xml.getElementsByTagName("solardata"):
                solar_a_index = i.getElementsByTagName("aindex")[0].childNodes[0].data
                solar_k_index = i.getElementsByTagName("kindex")[0].childNodes[0].data
                solar_xray = i.getElementsByTagName("xray")[0].childNodes[0].data
                solar_flux = i.getElementsByTagName("solarflux")[0].childNodes[0].data
                sunspots = i.getElementsByTagName("sunspots")[0].childNodes[0].data
                signalnoise = i.getElementsByTagName("signalnoise")[0].childNodes[0].data
            solar_cond = f"A-Index: {solar_a_index}\nK-Index: {solar_k_index}\nSunspots: {sunspots}\nX-Ray Flux: {solar_xray}\nSolar Flux: {solar_flux}\nSignal Noise: {signalnoise}"
        else:
            logger.error("Solar: Error fetching solar conditions")
            solar_cond = ERROR_FETCHING_DATA
        return solar_cond
    except Exception as e:
        logger.error(f"Solar: Exception in solar_conditions: {e}")
        return ERROR_FETCHING_DATA

def solar_conditions_condensed():
    """Get condensed solar conditions optimized for 140 character limit"""
    try:
        timeout = get_config_value('Solar_Config', 'url_timeout', DEFAULT_URL_TIMEOUT)
        solar_cond = requests.get("https://www.hamqsl.com/solarxml.php", timeout=timeout)
        if solar_cond.ok:
            solar_xml = xml.dom.minidom.parseString(solar_cond.text)
            for i in solar_xml.getElementsByTagName("solardata"):
                solar_a_index = i.getElementsByTagName("aindex")[0].childNodes[0].data
                solar_k_index = i.getElementsByTagName("kindex")[0].childNodes[0].data
                solar_xray = i.getElementsByTagName("xray")[0].childNodes[0].data
                solar_flux = i.getElementsByTagName("solarflux")[0].childNodes[0].data
                sunspots = i.getElementsByTagName("sunspots")[0].childNodes[0].data
                signalnoise = i.getElementsByTagName("signalnoise")[0].childNodes[0].data

            # Condensed format: A:K:Sun:Flux:Xray:Noise
            solar_cond = f"A:{solar_a_index} K:{solar_k_index} Sun:{sunspots} Flux:{solar_flux} Xray:{solar_xray} Noise:{signalnoise}"
        else:
            logger.error("Solar: Error fetching solar conditions")
            solar_cond = ERROR_FETCHING_DATA
        return solar_cond
    except Exception as e:
        logger.error(f"Solar: Exception in solar_conditions_condensed: {e}")
        return ERROR_FETCHING_DATA

def hf_band_conditions_condensed():
    """Get condensed HF band conditions optimized for 140 character limit"""
    try:
        hf_cond = ""
        timeout = get_config_value('Solar_Config', 'url_timeout', DEFAULT_URL_TIMEOUT)
        band_cond = requests.get("https://www.hamqsl.com/solarxml.php", timeout=timeout)
        if band_cond.ok:
            solarxml = xml.dom.minidom.parseString(band_cond.text)

            # Group bands by condition and time
            day_bands = {}
            night_bands = {}

            for i in solarxml.getElementsByTagName("band"):
                time_period = i.getAttribute("time")[0]  # 'd' for day, 'n' for night
                band_name = i.getAttribute("name")
                condition = str(i.childNodes[0].data)

                if time_period == 'd':
                    if condition not in day_bands:
                        day_bands[condition] = []
                    day_bands[condition].append(band_name)
                else:
                    if condition not in night_bands:
                        night_bands[condition] = []
                    night_bands[condition].append(band_name)

            # Build condensed output
            if day_bands:
                hf_cond += "D:"
                for condition, bands in day_bands.items():
                    if len(bands) > 1:
                        hf_cond += f"{bands[0]}-{bands[-1]}{condition}"
                    else:
                        hf_cond += f"{bands[0]}{condition}"
                    hf_cond += " "

            if night_bands:
                hf_cond += "N:"
                for condition, bands in night_bands.items():
                    if len(bands) > 1:
                        hf_cond += f"{bands[0]}-{bands[-1]}{condition}"
                    else:
                        hf_cond += f"{bands[0]}{condition}"
                    hf_cond += " "

            hf_cond = hf_cond.strip()
        else:
            logger.error("Solar: Error fetching HF band conditions")
            hf_cond = ERROR_FETCHING_DATA

        return hf_cond
    except Exception as e:
        logger.error(f"Solar: Exception in hf_band_conditions_condensed: {e}")
        return ERROR_FETCHING_DATA

def drap_xray_conditions():
    """Get DRAP X-ray flux conditions from NOAA direct"""
    try:
        timeout = get_config_value('Solar_Config', 'url_timeout', DEFAULT_URL_TIMEOUT)
        drap_cond = requests.get("https://services.swpc.noaa.gov/text/drap_global_frequencies.txt", timeout=timeout)
        if drap_cond.ok:
            drap_list = drap_cond.text.split('\n')
            x_filter = '#  X-RAY Message :'
            for line in drap_list:
                if x_filter in line:
                    xray_flux = line.split(": ")[1]
                    return xray_flux
            return "No X-ray data found"
        else:
            logger.error("Error fetching DRAP X-ray flux")
            return ERROR_FETCHING_DATA
    except Exception as e:
        logger.error(f"Exception in drap_xray_conditions: {e}")
        return ERROR_FETCHING_DATA

def get_sun(lat=None, lon=None):
    """Get sunrise and sunset times using specified location or defaults"""
    try:
        obs = ephem.Observer()
        obs.date = datetime.now(timezone.utc)
        sun = ephem.Sun()

        if lat is not None and lon is not None:
            obs.lat = str(lat)
            obs.lon = str(lon)
        else:
            lat = get_config_value('Bot', 'bot_latitude', DEFAULT_LATITUDE)
            lon = get_config_value('Bot', 'bot_longitude', DEFAULT_LONGITUDE)
            obs.lat = str(lat)
            obs.lon = str(lon)

        sun.compute(obs)
        sun_table = {}

        # Get the sun azimuth and altitude
        sun_table['azimuth'] = sun.az
        sun_table['altitude'] = sun.alt

        # Sun is up include altitude
        if sun_table['altitude'] > 0:
            sun_table['altitude'] = sun.alt
        else:
            sun_table['altitude'] = 0

        # Get the next rise and set times
        local_sunrise = ephem.localtime(obs.next_rising(sun))
        local_sunset = ephem.localtime(obs.next_setting(sun))

        use_zulu = get_config_value('Solar_Config', 'use_zulu_time', DEFAULT_ZULU_TIME)
        if use_zulu:
            sun_table['rise_time'] = local_sunrise.strftime('%a %d %H:%M')
            sun_table['set_time'] = local_sunset.strftime('%a %d %H:%M')
        else:
            sun_table['rise_time'] = local_sunrise.strftime('%a %d %I:%M%p')
            sun_table['set_time'] = local_sunset.strftime('%a %d %I:%M%p')

        # If sunset is before sunrise, then data will be for tomorrow format sunset first and sunrise second
        if local_sunset < local_sunrise:
            sun_data = f"SunSet: {sun_table['set_time']}\nRise: {sun_table['rise_time']}"
        else:
            sun_data = f"SunRise: {sun_table['rise_time']}\nSet: {sun_table['set_time']}"

        daylight_seconds = (local_sunset - local_sunrise).seconds
        daylight_hours = daylight_seconds // 3600
        daylight_minutes = (daylight_seconds // 60) % 60
        sun_data += f"\nDaylight: {daylight_hours}h {daylight_minutes}m"

        if sun_table['altitude'] > 0:
            remaining_seconds = (local_sunset - datetime.now()).seconds
            remaining_hours = remaining_seconds // 3600
            remaining_minutes = (remaining_seconds // 60) % 60
            sun_data += f"\nRemaining: {remaining_hours}h {remaining_minutes}m"

        sun_data += f"\nAzimuth: {sun_table['azimuth'] * 180 / ephem.pi:.2f}°"
        if sun_table['altitude'] > 0:
            sun_data += f"\nAltitude: {sun_table['altitude'] * 180 / ephem.pi:.2f}°"

        return sun_data
    except Exception as e:
        logger.error(f"Exception in get_sun: {e}")
        return ERROR_FETCHING_DATA

def get_moon(lat=None, lon=None):
    """Get moon phase and rise/set times using specified location or defaults"""
    try:
        obs = ephem.Observer()
        moon = ephem.Moon()

        if lat is not None and lon is not None:
            obs.lat = str(lat)
            obs.lon = str(lon)
        else:
            lat = get_config_value('Bot', 'bot_latitude', DEFAULT_LATITUDE)
            lon = get_config_value('Bot', 'bot_longitude', DEFAULT_LONGITUDE)
            obs.lat = str(lat)
            obs.lon = str(lon)

        obs.date = datetime.now(timezone.utc)
        moon.compute(obs)
        moon_table = {}
        illum = moon.phase  # 0 = new, 50 = first/last quarter, 100 = full

        if illum < 1.0:
            moon_phase = 'New Moon🌑'
        elif illum < 49:
            moon_phase = 'Waxing Crescent🌒'
        elif 49 <= illum < 51:
            moon_phase = 'First Quarter🌓'
        elif illum < 99:
            moon_phase = 'Waxing Gibbous🌔'
        elif illum >= 99:
            moon_phase = 'Full Moon🌕'
        elif illum > 51:
            moon_phase = 'Waning Gibbous🌖'
        elif 51 >= illum > 49:
            moon_phase = 'Last Quarter🌗'
        else:
            moon_phase = 'Waning Crescent🌘'

        moon_table['phase'] = moon_phase
        moon_table['illumination'] = moon.phase
        moon_table['azimuth'] = moon.az
        moon_table['altitude'] = moon.alt

        local_moonrise = ephem.localtime(obs.next_rising(moon))
        local_moonset = ephem.localtime(obs.next_setting(moon))

        use_zulu = get_config_value('Solar_Config', 'use_zulu_time', DEFAULT_ZULU_TIME)
        if use_zulu:
            moon_table['rise_time'] = local_moonrise.strftime('%a %d %H:%M')
            moon_table['set_time'] = local_moonset.strftime('%a %d %H:%M')
        else:
            moon_table['rise_time'] = local_moonrise.strftime('%a %d %I:%M%p')
            moon_table['set_time'] = local_moonset.strftime('%a %d %I:%M%p')

        local_next_full_moon = ephem.localtime(ephem.next_full_moon(obs.date))
        local_next_new_moon = ephem.localtime(ephem.next_new_moon(obs.date))

        if use_zulu:
            moon_table['next_full_moon'] = local_next_full_moon.strftime('%a %b %d %H:%M')
            moon_table['next_new_moon'] = local_next_new_moon.strftime('%a %b %d %H:%M')
        else:
            moon_table['next_full_moon'] = local_next_full_moon.strftime('%a %b %d %I:%M%p')
            moon_table['next_new_moon'] = local_next_new_moon.strftime('%a %b %d %I:%M%p')

        moon_data = f"MoonRise:{moon_table['rise_time']}\nSet:{moon_table['set_time']}\nPhase:{moon_table['phase']} @:{moon_table['illumination']:.2f}%\nFullMoon:{moon_table['next_full_moon']}\nNewMoon:{moon_table['next_new_moon']}"

        # If moon is in the sky, add azimuth and altitude
        if moon_table['altitude'] > 0:
            moon_data += f"\nAz: {moon_table['azimuth'] * 180 / ephem.pi:.2f}°\nAlt: {moon_table['altitude'] * 180 / ephem.pi:.2f}°"

        return moon_data
    except Exception as e:
        logger.error(f"Exception in get_moon: {e}")
        return ERROR_FETCHING_DATA

def get_next_satellite_pass(satellite, lat=None, lon=None, use_visual=False):
    """Get the next satellite pass for a given satellite

    Args:
        satellite: NORAD ID or satellite identifier
        lat: Observer latitude (optional, uses config if not provided)
        lon: Observer longitude (optional, uses config if not provided)
        use_visual: If True, use visualpasses endpoint (only visually observable passes)
                   If False, use radiopasses endpoint (all passes above horizon, default)
    """
    try:
        pass_data = ''

        if lat is None and lon is None:
            lat = get_config_value('Bot', 'bot_latitude', DEFAULT_LATITUDE)
            lon = get_config_value('Bot', 'bot_longitude', DEFAULT_LONGITUDE)

        # API URL
        n2yo_key = get_config_value('External_Data', 'n2yo_api_key', DEFAULT_N2YO_API_KEY)
        if not n2yo_key:
            logger.error("System: Missing API key free at https://www.n2yo.com/login/")
            return "not configured, bug your sysop"

        # Choose endpoint based on use_visual parameter
        if use_visual:
            # Visual passes: requires satellite to be visually observable (illuminated, dark sky)
            # Format: /visualpasses/{id}/{lat}/{lng}/{alt}/{days}/{min_visibility_seconds}
            api_base = "https://api.n2yo.com/rest/v1/satellite/visualpasses/"
            url = f"{api_base}{satellite}/{lat}/{lon}/0/10/60/&apiKey={n2yo_key}"
            endpoint_type = "visual"
        else:
            # Radio passes: all passes above horizon (default, matches N2YO website)
            # Format: /radiopasses/{id}/{lat}/{lng}/{alt}/{days}/{min_elevation_degrees}
            api_base = "https://api.n2yo.com/rest/v1/satellite/radiopasses/"
            url = f"{api_base}{satellite}/{lat}/{lon}/0/10/0/&apiKey={n2yo_key}"
            endpoint_type = "radio"

        logger.debug(f"Satellite pass request: NORAD {satellite}, {endpoint_type} passes, location: {lat}, {lon}")

        # Get the next pass data
        try:
            if not int(satellite):
                raise Exception("Invalid satellite number")
            next_pass_data = requests.get(url, timeout=DEFAULT_URL_TIMEOUT)
            if next_pass_data.ok:
                pass_json = next_pass_data.json()
                passes_count = pass_json.get('info', {}).get('passescount', 0)
                logger.debug(f"N2YO API response: {passes_count} {endpoint_type} passes found")

                if 'info' in pass_json and passes_count > 0:
                    satname = pass_json['info']['satname']

                    # Find the first pass that hasn't occurred yet (startUTC is in the future)
                    current_time_utc = datetime.now(timezone.utc).timestamp()

                    # Find the first future pass
                    next_pass = None
                    for idx, pass_entry in enumerate(pass_json['passes']):
                        pass_utc_time = pass_entry['startUTC']
                        if pass_utc_time >= current_time_utc:
                            next_pass = pass_entry
                            logger.debug(f"Selected pass {idx} for {satname}: {datetime.fromtimestamp(pass_utc_time, tz=timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M')}, maxEl={pass_entry['maxEl']}°")
                            break

                    # If no future pass found, use the first one (shouldn't happen with 10-day lookahead)
                    if next_pass is None:
                        next_pass = pass_json['passes'][0]
                        logger.warning(f"All passes for {satname} appear to be in the past, using first pass")

                    pass_time = next_pass['startUTC']
                    # Calculate duration from endUTC - startUTC if duration field not present (radiopasses)
                    if 'duration' in next_pass:
                        pass_duration = next_pass['duration']
                    else:
                        pass_duration = next_pass['endUTC'] - next_pass['startUTC']
                    pass_max_el = next_pass['maxEl']
                    pass_start_az_compass = next_pass['startAzCompass']
                    pass_end_az_compass = next_pass['endAzCompass']

                    # Validate duration - LEO passes are typically 5-15 minutes, max under 1 hour
                    # Geostationary satellites don't have traditional passes
                    MAX_REASONABLE_PASS_DURATION = 7200  # 2 hours in seconds (very generous upper bound)
                    if pass_duration > MAX_REASONABLE_PASS_DURATION:
                        logger.warning(f"Satellite {satname} has unreasonable pass duration: {pass_duration}s. May be geostationary or invalid data.")
                        pass_data = f"{satname} appears to be geostationary or has invalid pass data. Geostationary satellites don't have traditional passes - they remain in a fixed position relative to Earth."
                        return pass_data

                    # Convert UTC timestamp to local time
                    pass_utc = datetime.fromtimestamp(pass_time, tz=timezone.utc)
                    pass_local = pass_utc.astimezone()
                    set_utc = datetime.fromtimestamp(pass_time + pass_duration, tz=timezone.utc)
                    set_local = set_utc.astimezone()

                    # Format times based on zulu time preference
                    use_zulu = get_config_value('Solar_Config', 'use_zulu_time', DEFAULT_ZULU_TIME)
                    if use_zulu:
                        pass_rise_time = pass_local.strftime('%a %d %H:%M')
                        pass_set_time = set_local.strftime('%a %d %H:%M')
                    else:
                        pass_rise_time = pass_local.strftime('%a %d %I:%M%p')
                        pass_set_time = set_local.strftime('%a %d %I:%M%p')

                    # Format duration nicely (duration is in seconds from N2YO)
                    duration_minutes = pass_duration // 60
                    duration_seconds = pass_duration % 60
                    if duration_minutes > 0:
                        duration_str = f"{duration_minutes}m{duration_seconds}s"
                    else:
                        duration_str = f"{duration_seconds}s"

                    pass_data = f"{satname} @{pass_rise_time} Az:{pass_start_az_compass} for{duration_str}, MaxEl:{pass_max_el}° Set@{pass_set_time} Az:{pass_end_az_compass}"
                elif passes_count == 0:
                    satname = pass_json['info']['satname']
                    pass_type = "visible" if use_visual else ""
                    logger.debug(f"Satellite {satname} (NORAD {satellite}) has no {pass_type} passes in next 10 days")
                    pass_data = f"{satname} has no {pass_type} passes in the next 10 days from this location"
            else:
                logger.error(f"System: Error fetching satellite pass data {satellite}")
                pass_data = ERROR_FETCHING_DATA
        except Exception:
            logger.warning(f"System: User supplied value {satellite} unknown or invalid")
            pass_data = "Provide NORAD# example use:🛰️satpass 25544,33591"

        return pass_data
    except Exception as e:
        logger.error(f"Exception in get_next_satellite_pass: {e}")
        return ERROR_FETCHING_DATA
