#!/usr/bin/env python3
"""Compatibility module for the old ``wx_international`` alternative.

The global weather command is ``modules.commands.gwx_command.GwxCommand`` now,
and ``wx`` answers from Open-Meteo when ``[Weather] weather_provider`` says so.
This module keeps the names code and plugins imported from here, so an old
import or a ``[Plugin_Overrides] wx = wx_international`` line still resolves.
It defines no command class of its own, so the plugin loader does not register
it a second time.
"""

import asyncio  # noqa: F401  re-exported
import math  # noqa: F401  re-exported
import re  # noqa: F401  re-exported
from datetime import datetime, timedelta  # noqa: F401  re-exported
from typing import Any, Optional, Union  # noqa: F401  re-exported

import requests  # noqa: F401  module attribute other code patches

from ...clients.mqtt_weather import (  # noqa: F401  re-exported
    get_mqtt_weather_topic,
    load_mqtt_weather_format_config,
    mqtt_weather_display_for_topic,
)
from ...clients.wxsim_parser import WXSIMParser  # noqa: F401  re-exported
from ...location import get_bot_lat_lon, get_companion_lat_lon  # noqa: F401  re-exported
from ...models import MeshMessage  # noqa: F401  re-exported
from ...utils import (  # noqa: F401  re-exported
    format_temperature_high_low,
    geocode_city_sync,
    geocode_zipcode_sync,
    get_nominatim_geocoder,
    normalize_us_state,
    rate_limited_nominatim_reverse_sync,
)
from ...weather_common import (  # noqa: F401  re-exported
    WeatherCommandMixin,
    load_open_meteo_model,
)
from ...weather_providers.openmeteo import (  # noqa: F401  re-exported
    GWX_MULTIDAY_MAX_DAYS,
    HPA_TO_MMHG,
    MI_TO_KM,
    OPEN_METEO_TIMEOUT,
    VISIBILITY_CAP_KM,
    VISIBILITY_CAP_MI,
    OpenMeteoProvider,
    weather_emoji,
)
from ..base_command import BaseCommand  # noqa: F401  re-exported
from ..gwx_command import GwxCommand

# Kept for code that checked them; these imports used to be optional.
WXSIM_PARSER_AVAILABLE = True

# The class this module used to define.
GlobalWxCommand = GwxCommand
