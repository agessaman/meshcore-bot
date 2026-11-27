"""Weather providers: one forecast source each, behind the weather command.

``PROVIDERS`` maps the value of ``[Weather] weather_provider`` to the provider
class that answers it. Adding a provider is one module here plus one entry.
"""

from __future__ import annotations

from .base import (
    Capability,
    ForecastReply,
    ProviderHost,
    WeatherProvider,
    WeatherUnits,
)
from .openmeteo import OpenMeteoProvider

PROVIDERS: dict[str, type[WeatherProvider]] = {
    "openmeteo": OpenMeteoProvider,
}


def make_provider(name: str, host: ProviderHost, units: WeatherUnits) -> WeatherProvider:
    """The provider registered under *name*, bound to *host* and *units*."""
    return PROVIDERS[name](host=host, units=units)


__all__ = [
    "PROVIDERS",
    "Capability",
    "ForecastReply",
    "OpenMeteoProvider",
    "ProviderHost",
    "WeatherProvider",
    "WeatherUnits",
    "make_provider",
]
