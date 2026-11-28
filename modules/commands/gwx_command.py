#!/usr/bin/env python3
"""Global weather command for the MeshCore Bot.

gwx is a weather command like wx: it answers from the provider its config
selects, and defaults to Open-Meteo, which covers the whole world.
"""

from ..weather_providers.compat import OpenMeteoCompatMixin
from .wx_command import WxCommand, command_override_fields


class GwxCommand(OpenMeteoCompatMixin, WxCommand):
    """gwx: the global weather command, answering from Open-Meteo by default.

    Everything is wx's: one execute, one request, one set of providers. This
    class keeps gwx's triggers, its message catalog and its documentation, and
    the Open-Meteo forwarders plugins reach on the command.
    """

    # Plugin metadata
    # Every reply goes through send_response, so a scheduled {cmd:gwx ...} renders
    # without transmitting, as wx already does.
    render_safe = True
    name = "gwx"
    translation_ns = "commands.gwx"
    keywords = ['gwx', 'globalweather', 'gwxa']
    description = "Get weather information for any global location (usage: gwx Tokyo)"
    global_description = "Get weather information for any global location (usage: gwx Tokyo)"
    category = "weather"
    cooldown_seconds = 5  # 5 second cooldown per user to prevent API abuse
    # Open-Meteo/geocoding need the network; custom MQTT/WXSIM may be LAN-only.
    requires_internet = False

    # Documentation
    short_description = "Get weather for any global location using Open-Meteo API"
    usage = "gwx <location> [tomorrow|<N>d|hourly]"
    examples = ["gwx Tokyo", "gwx Paris, France"]
    parameters = [
        {"name": "location", "description": "City name, country, or coordinates"},
        {"name": "option", "description": "tomorrow, Nd (e.g. 7d, 10d), or hourly (optional)"}
    ]

    # gwx answers from Open-Meteo unless its own section or [Weather] says otherwise.
    default_weather_provider = "openmeteo"

    # The shared [Weather] settings, then gwx's own overrides of them.
    settings_schema = [
        *(field for field in WxCommand.settings_schema if field.get("section")),
        *command_override_fields("gwx", default_weather_provider),
    ]
