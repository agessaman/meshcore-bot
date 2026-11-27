"""The interface a weather provider implements, and the types it exchanges.

A provider owns one forecast source: the fetch, the formatting of the reply and
the message catalog it speaks. The command owns the request: option parsing,
geocoding, location labels and sending. Providers are synchronous (blocking
HTTP); the command runs them on a worker thread.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Protocol


class ProviderHost(Protocol):
    """What a provider may ask of the command that owns it.

    Read at call time, never cached: tests and plugins replace these on the
    command after it is constructed, and the provider must see the new value.
    """

    bot: Any
    logger: Any

    def translate(self, key: str, **kwargs) -> str: ...

    def get_max_message_length(self, message: Any) -> int: ...


@dataclass(frozen=True)
class WeatherUnits:
    """The units a provider asks its source for and prints in its reply."""

    temperature: str    # fahrenheit | celsius
    wind_speed: str     # mph | kmh | ms | kn
    precipitation: str  # inch | mm


class Capability(str, Enum):
    """A forecast type a provider can answer."""

    CURRENT = "current"
    TOMORROW = "tomorrow"
    MULTIDAY = "multiday"
    HOURLY = "hourly"
    ALERTS = "alerts"


@dataclass
class ForecastReply:
    """One provider answer, before the command adds its location label."""

    text: str                      # body, or the provider's translated error reply
    ok: bool = True                # False: text is an error reply
    no_coverage: bool = False      # provider knows it does not cover this point
    extra: str | None = None       # second message (alert summary, extreme-conditions warning)
    alert_count: int | None = None  # NOAA puts this in its 4-tuple; keep the tuple shapes callers see today


class WeatherProvider(ABC):
    """One forecast source behind the weather command."""

    name: ClassVar[str]                    # registry key, the value of [Weather] weather_provider
    translation_ns: ClassVar[str]          # message catalog: noaa -> "commands.wx", openmeteo -> "commands.gwx"
    location_style: ClassVar[str]          # "us" (zip/city/state labels) or "global" (country labels)
    capabilities: ClassVar[frozenset[Capability]]
    max_days: ClassVar[int]                # multiday cap

    def __init__(self, host: ProviderHost, units: WeatherUnits):
        self.host = host
        self.units = units

    def covers(self, lat: float, lon: float) -> bool:
        """Whether this provider answers for a point (NOAA: not known-uncovered)."""
        return True

    @abstractmethod
    def forecast(
        self,
        lat: float,
        lon: float,
        forecast_type: str = "default",
        num_days: int = 7,
        body_max_length: int = 0,
        message: Any = None,
    ) -> ForecastReply:
        """The reply for a point, without the command's location label.

        *body_max_length* is the number of bytes the command reserves for its
        location prefix; the reply body gets the rest of the message budget.
        """

    def alert_list(self, lat: float, lon: float):
        """The provider's alerts for a point; only when ALERTS is a capability."""
        raise NotImplementedError
