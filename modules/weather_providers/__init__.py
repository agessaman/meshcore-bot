"""Weather providers: one forecast source each, behind the weather command.

``PROVIDERS`` maps the value of ``[Weather] weather_provider`` to the forecast
provider class that answers it, and ``ALERT_PROVIDERS`` does the same for
``[Weather] alerts_provider``.

Adding a provider:

1. Write the module here: a ``WeatherProvider`` subclass for a forecast source,
   or an ``AlertsProvider`` subclass for an alert source. It reads the command
   through ``self.host`` at call time and never caches what it reads.
2. Declare ``name`` (the registry key and the config value), ``translation_ns``
   (the message catalog it speaks), and for a forecast provider also
   ``location_style`` (``"us"`` or ``"global"``), ``capabilities`` and
   ``max_days``.
3. Register the class in ``PROVIDERS`` or ``ALERT_PROVIDERS`` below.
4. Nothing else: the settings enum is built from the registry, so the new value
   appears in the web viewer's settings page on its own.
5. Document the value in ``config.ini.example`` under ``[Weather]``.
"""

from __future__ import annotations

from typing import Any

from .alerts_dwd import DwdAlertsProvider
from .alerts_eccc import EcccAlertsProvider
from .alerts_meteoalarm import MeteoalarmAlertsProvider
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
from .eccc import EcccProvider
from .metno import MetNorwayProvider
from .noaa import NoaaProvider
from .openmeteo import OpenMeteoProvider

PROVIDERS: dict[str, type[WeatherProvider]] = {
    "eccc": EcccProvider,
    "metno": MetNorwayProvider,
    "noaa": NoaaProvider,
    "openmeteo": OpenMeteoProvider,
}

ALERT_PROVIDERS: dict[str, type[AlertsProvider]] = {
    "dwd": DwdAlertsProvider,
    "eccc": EcccAlertsProvider,
    "meteoalarm": MeteoalarmAlertsProvider,
    "nws": NwsAlertsProvider,
}


def make_provider(name: str, host: ProviderHost, units: WeatherUnits, **kwargs) -> WeatherProvider:
    """The forecast provider registered under *name*, bound to *host* and *units*.

    *kwargs* go to the provider's constructor, for the state a command shares
    between its forecast and alerts providers (one session, one worker lock,
    one coverage cache).
    """
    return PROVIDERS[name](host=host, units=units, **kwargs)


def make_alerts_provider(name: str, host: ProviderHost, **kwargs) -> AlertsProvider:
    """The alerts provider registered under *name*, bound to *host*."""
    return ALERT_PROVIDERS[name](host=host, **kwargs)


def resolve_alerts_provider_name(
    config_value: str | None,
    forecast_name: str,
    logger: Any = None,
) -> str | None:
    """The alerts provider for a ``[Weather] alerts_provider`` value, or None.

    ``auto`` (the default, and an empty value) follows the forecast provider:
    NWS alerts with NOAA, Environment Canada alerts with ECCC, none with
    Open-Meteo or MET Norway. ``none`` asks for no alerts.
    Any key of ``ALERT_PROVIDERS`` names that provider. An unknown value warns
    once, when a *logger* is given, and behaves like ``auto``.
    """
    value = (config_value or "").strip().lower()
    if value in ("", "auto"):
        return _default_alerts_provider(forecast_name)
    if value == "none":
        return None
    if value in ALERT_PROVIDERS:
        return value
    if logger is not None:
        logger.warning(
            f"Unknown [Weather] alerts_provider '{value}'; using 'auto'. "
            f"Valid values: auto, none, {', '.join(sorted(ALERT_PROVIDERS))}"
        )
    return _default_alerts_provider(forecast_name)


def _default_alerts_provider(forecast_name: str) -> str | None:
    """The alerts provider a forecast source declares for ``auto``, if any."""
    provider = PROVIDERS.get(forecast_name)
    return getattr(provider, "default_alerts_provider", None) if provider else None


__all__ = [
    "ALERT_PROVIDERS",
    "PROVIDERS",
    "AlertSummary",
    "AlertsProvider",
    "Capability",
    "DwdAlertsProvider",
    "EcccAlertsProvider",
    "EcccProvider",
    "ForecastReply",
    "MetNorwayProvider",
    "NoaaProvider",
    "NwsAlertsProvider",
    "OpenMeteoProvider",
    "ProviderHost",
    "WeatherProvider",
    "WeatherUnits",
    "make_alerts_provider",
    "make_provider",
    "resolve_alerts_provider_name",
]
