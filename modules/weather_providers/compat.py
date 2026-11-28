"""The Open-Meteo methods gwx used to own, kept for plugins that call them.

``GwxCommand`` answers from Open-Meteo by default, and the work lives in
``OpenMeteoProvider``. These forwarders stay on the command so a plugin that
reached ``cmd.get_open_meteo_weather(...)`` keeps working, but each one is
deprecated: it warns and hands the call to ``cmd.provider``.
"""

from __future__ import annotations

import warnings
from dataclasses import replace
from typing import Any, Optional

from .openmeteo import OpenMeteoProvider


class OpenMeteoCompatMixin:
    """Deprecated Open-Meteo forwarders, for ``GwxCommand`` only."""

    provider: OpenMeteoProvider

    def _open_meteo_compat(self, method: str) -> Any:
        """The provider's *method*, after warning that the command's copy is going away.

        The forwarders only mean something while the command answers from
        Open-Meteo, so a command configured for another provider says so rather
        than failing somewhere inside the provider.
        """
        warnings.warn(
            f"{type(self).__name__}.{method} is deprecated, use cmd.provider.{method}; removed in 1.4",
            DeprecationWarning,
            stacklevel=3,
        )
        provider = self.provider
        if not isinstance(provider, OpenMeteoProvider):
            raise AttributeError(
                f"{type(self).__name__}.{method} needs the Open-Meteo provider; "
                f"this command answers from '{provider.name}'"
            )
        return getattr(provider, method)

    @property
    def temperature_unit(self) -> str:
        """Deprecated: use ``cmd.provider.units.temperature``."""
        return self._open_meteo_compat("units").temperature

    @temperature_unit.setter
    def temperature_unit(self, value: str) -> None:
        self._open_meteo_compat("units")
        self.provider.units = replace(self.provider.units, temperature=value)

    @property
    def wind_speed_unit(self) -> str:
        """Deprecated: use ``cmd.provider.units.wind_speed``."""
        return self._open_meteo_compat("units").wind_speed

    @wind_speed_unit.setter
    def wind_speed_unit(self, value: str) -> None:
        self._open_meteo_compat("units")
        self.provider.units = replace(self.provider.units, wind_speed=value)

    @property
    def precipitation_unit(self) -> str:
        """Deprecated: use ``cmd.provider.units.precipitation``."""
        return self._open_meteo_compat("units").precipitation

    @precipitation_unit.setter
    def precipitation_unit(self, value: str) -> None:
        self._open_meteo_compat("units")
        self.provider.units = replace(self.provider.units, precipitation=value)

    @property
    def weather_model(self) -> Optional[str]:
        """Deprecated: use ``cmd.provider.weather_model``."""
        return self._open_meteo_compat("weather_model")

    @weather_model.setter
    def weather_model(self, value: Optional[str]) -> None:
        self._open_meteo_compat("weather_model")
        self.provider.weather_model = value

    @property
    def metric_distance(self) -> bool:
        """Deprecated: use ``cmd.provider.metric_distance``."""
        return self._open_meteo_compat("metric_distance")

    def get_open_meteo_weather(self, lat: float, lon: float, forecast_type: str = "default",
                               num_days: int = 7, message: Any = None,
                               location_prefix_len: int = 0) -> str:
        """Deprecated: use ``cmd.provider.get_open_meteo_weather``."""
        return self._open_meteo_compat("get_open_meteo_weather")(
            lat, lon, forecast_type, num_days, message, location_prefix_len
        )

    def format_tomorrow_forecast(self, data: Any, max_length: Optional[int] = None) -> str:
        """Deprecated: use ``cmd.provider.format_tomorrow_forecast``."""
        return self._open_meteo_compat("format_tomorrow_forecast")(data, max_length)

    def format_multiday_forecast(self, data: Any, num_days: int = 7,
                                 max_length: Optional[int] = None) -> str:
        """Deprecated: use ``cmd.provider.format_multiday_forecast``.

        *max_length* is wx's NOAA byte budget; Open-Meteo's multi-day summary
        takes none, so it is accepted and ignored.
        """
        return self._open_meteo_compat("format_multiday_forecast")(data, num_days)
