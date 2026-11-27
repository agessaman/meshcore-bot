"""The interfaces a weather provider implements, and the types they exchange.

A forecast provider owns one forecast source: the fetch, the formatting of the
reply and the message catalog it speaks. An alerts provider owns one alert
source, chosen independently of the forecast source. The command owns the
request: option parsing, geocoding, location labels and sending. Providers are
synchronous (blocking HTTP); the command runs them on a worker thread.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, Optional, Protocol


class ProviderHost(Protocol):
    """What a provider may ask of the command that owns it.

    Read at call time, never cached: tests and plugins replace these on the
    command after it is constructed, and the provider must see the new value.

    ``bot``, ``logger``, ``translate``, ``response_translator``,
    ``send_response`` and ``get_max_message_length`` come from ``BaseCommand``;
    the formatting helpers (``_count_display_width``, ``_format_high_low``,
    ``_hour_label``, ``_without_arrow``, ``_short_hourly_description``,
    ``_pack_hourly_lines``, ``_pace_reply``, ``_send_multiday_forecast``) come
    from ``WeatherCommandMixin``.
    """

    bot: Any
    logger: Any
    response_translator: Any

    def translate(self, key: str, **kwargs) -> str: ...

    def get_max_message_length(self, message: Any) -> int: ...

    async def send_response(self, message: Any, content: str, **kwargs) -> bool: ...

    def _count_display_width(self, text: str) -> int: ...

    def _format_high_low(self, high: Any, low: Any, temp_symbol: str) -> str: ...

    def _hour_label(self, start_time_str: str) -> str: ...

    def _without_arrow(self, direction: str) -> str: ...

    def _short_hourly_description(self, description: str) -> str: ...

    def _pack_hourly_lines(self, hourly_lines: Any, max_length: int,
                           namespace: Optional[str] = None) -> str: ...

    async def _pace_reply(self, message: Any, seconds: float) -> None: ...

    async def _send_multiday_forecast(self, message: Any, forecast_text: str) -> bool: ...


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


@dataclass
class ForecastReply:
    """One provider answer, before the command adds its location label."""

    text: str                      # body, or the provider's translated error reply
    ok: bool = True                # False: text is an error reply
    no_coverage: bool = False      # provider knows it does not cover this point
    extra: str | None = None       # second message (extreme-conditions warning)


@dataclass
class AlertSummary:
    """The compact alert text that rides along with a forecast reply."""

    text: str
    count: int


class WeatherProvider(ABC):
    """One forecast source behind the weather command."""

    name: ClassVar[str]                    # registry key, the value of [Weather] weather_provider
    translation_ns: ClassVar[str]          # message catalog: noaa -> "commands.wx", openmeteo -> "commands.gwx"
    location_style: ClassVar[str]          # "us" (zip/city/state labels) or "global" (country labels)
    capabilities: ClassVar[frozenset[Capability]]
    max_days: ClassVar[int]                # multiday cap
    # The alerts provider ``alerts_provider = auto`` picks for this source, if any.
    default_alerts_provider: ClassVar[Optional[str]] = None

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
        location_prefix_len: int = 0,
        message: Any = None,
    ) -> ForecastReply:
        """The reply for a point, without the command's location label.

        *location_prefix_len* is the display width of the location prefix the
        command prepends to the body. Each provider works out its own body
        budget from that and the frame size, so the assembled reply fits.
        """


class Coverage(str, Enum):
    """Whether an alerts provider answers for a point, or cannot tell right now."""

    COVERED = "covered"
    NOT_COVERED = "not_covered"


class AlertsProvider(ABC):
    """One alert source, chosen independently of the forecast source."""

    name: ClassVar[str]                    # registry key, the value of [Weather] alerts_provider
    translation_ns: ClassVar[str]          # message catalog: nws -> "commands.wx"

    def __init__(self, host: ProviderHost):
        self.host = host

    def coverage_for(self, lat: float, lon: float) -> Coverage:
        """Whether this provider answers for a point.

        A provider whose coverage lookup can fail overrides this to answer
        COVERED when it cannot tell, rather than claiming the point is not
        covered.
        """
        return Coverage.COVERED if self.covers(lat, lon) else Coverage.NOT_COVERED

    def covers(self, lat: float, lon: float) -> bool:
        """Whether this provider answers for a point (NWS: US only)."""
        return True

    @abstractmethod
    def summary(self, lat: float, lon: float, max_length: int) -> AlertSummary | None:
        """The compact text and count for the second message; None without active alerts."""

    @abstractmethod
    async def send_full_list(self, message: Any, lat: float, lon: float) -> bool:
        """Send the full alert list for a point; whether every message went out."""
