"""Weather providers: one forecast source each, behind the weather command.

``PROVIDERS`` maps the value of ``[Weather] weather_provider`` to the forecast
provider class that answers it, and ``ALERT_PROVIDERS`` does the same for
``[Weather] alerts_provider``. Adding a provider is one module here plus one
entry.
"""

from __future__ import annotations

from .alerts_nws import NwsAlertsProvider
from .base import (
    AlertsProvider,
    AlertSummary,
    Capability,
    ForecastReply,
    ProviderHost,
    WeatherProvider,
    WeatherUnits,
)
from .noaa import NoaaProvider
from .openmeteo import OpenMeteoProvider

PROVIDERS: dict[str, type[WeatherProvider]] = {
    "noaa": NoaaProvider,
    "openmeteo": OpenMeteoProvider,
}

ALERT_PROVIDERS: dict[str, type[AlertsProvider]] = {
    "nws": NwsAlertsProvider,
}


def make_provider(name: str, host: ProviderHost, units: WeatherUnits) -> WeatherProvider:
    """The forecast provider registered under *name*, bound to *host* and *units*."""
    return PROVIDERS[name](host=host, units=units)


def make_alerts_provider(name: str, host: ProviderHost) -> AlertsProvider:
    """The alerts provider registered under *name*, bound to *host*."""
    return ALERT_PROVIDERS[name](host=host)


__all__ = [
    "ALERT_PROVIDERS",
    "PROVIDERS",
    "AlertSummary",
    "AlertsProvider",
    "Capability",
    "ForecastReply",
    "NoaaProvider",
    "NwsAlertsProvider",
    "OpenMeteoProvider",
    "ProviderHost",
    "WeatherProvider",
    "WeatherUnits",
    "make_alerts_provider",
    "make_provider",
]
