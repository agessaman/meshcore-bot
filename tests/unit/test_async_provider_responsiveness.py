#!/usr/bin/env python3
"""Regression tests for synchronous provider work in async entry points."""

import ast
import asyncio
import configparser
import pathlib
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Optional, TypeVar
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.commands import aqi_command as aqi_module
from modules.commands.airplanes_command import AirplanesCommand
from modules.commands.aqi_command import AqiCommand
from modules.commands.aurora_command import AuroraCommand
from modules.location import ResolvedLocation
from modules.models import MeshMessage
from modules.service_plugins import weather_service as weather_module
from modules.service_plugins.weather_service import WeatherService
from tests.test_aurora_command import _make_bot as _make_aurora_bot
from tests.unit._rain_harness import build_cmd, make_series

_T = TypeVar("_T")


class _HeartbeatGate:
    """A synchronous provider waits until an asyncio heartbeat can run."""

    def __init__(self) -> None:
        self.provider_started = threading.Event()
        self.heartbeat_ran = threading.Event()
        self.provider_saw_heartbeat = False

    def block_until_heartbeat(self) -> None:
        self.provider_started.set()
        self.provider_saw_heartbeat = self.heartbeat_ran.wait(timeout=1)


async def _run_with_heartbeat(
    operation: Callable[[], Awaitable[_T]], gate: _HeartbeatGate
) -> _T:
    async def heartbeat() -> None:
        while not gate.provider_started.is_set():
            await asyncio.sleep(0)
        gate.heartbeat_ran.set()

    heartbeat_task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    result = await asyncio.wait_for(operation(), timeout=2)
    await heartbeat_task

    assert gate.provider_saw_heartbeat, "provider work blocked the asyncio event loop"
    return result


def _weather_service(mock_logger: Mock) -> WeatherService:
    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.add_section("Weather_Service")
    config.set("Weather_Service", "my_position_lat", "47.6062")
    config.set("Weather_Service", "my_position_lon", "-122.3321")

    bot = Mock()
    bot.logger = mock_logger
    bot.config = config
    bot.db_manager = Mock()
    bot.command_manager = Mock()
    return WeatherService(bot)


@pytest.mark.asyncio
async def test_airplanes_provider_fetch_keeps_event_loop_responsive() -> None:
    command = object.__new__(AirplanesCommand)
    gate = _HeartbeatGate()
    expected = {"ac": []}

    def slow_fetch(_lat: float, _lon: float, _radius: float) -> dict[str, Any]:
        gate.block_until_heartbeat()
        return expected

    command._fetch_aircraft_data = slow_fetch  # type: ignore[method-assign]

    result = await _run_with_heartbeat(
        lambda: command._fetch_aircraft_data_async(47.6, -122.3, 25),
        gate,
    )

    assert result is expected


@pytest.mark.asyncio
async def test_airplanes_provider_fetch_await_is_cancellable() -> None:
    command = object.__new__(AirplanesCommand)
    provider_started = threading.Event()
    release_provider = threading.Event()

    def slow_fetch(_lat: float, _lon: float, _radius: float) -> dict[str, Any]:
        provider_started.set()
        release_provider.wait(timeout=1)
        return {"ac": []}

    command._fetch_aircraft_data = slow_fetch  # type: ignore[method-assign]
    task = asyncio.create_task(command._fetch_aircraft_data_async(47.6, -122.3, 25))

    try:
        assert await asyncio.to_thread(provider_started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_provider.set()


@pytest.mark.asyncio
async def test_weather_forecast_fetch_keeps_event_loop_responsive(mock_logger: Mock) -> None:
    service = _weather_service(mock_logger)
    service._cached_location_name = "Seattle, WA"
    gate = _HeartbeatGate()
    response = Mock()
    response.ok = True
    response.json.return_value = {
        "current": {
            "temperature_2m": 70,
            "weather_code": 1,
            "wind_speed_10m": 5,
            "wind_direction_10m": 180,
        },
        "daily": {
            "time": ["2026-07-16", "2026-07-17"],
            "weather_code": [1, 2],
            "temperature_2m_max": [72, 73],
            "temperature_2m_min": [58, 59],
        },
    }

    def slow_get(*_args: Any, **_kwargs: Any) -> Mock:
        gate.block_until_heartbeat()
        return response

    service.api_session = Mock()
    service.api_session.get = slow_get

    result = await _run_with_heartbeat(service._get_weather_forecast, gate)

    assert result.startswith("Seattle, WA:")
    response.json.assert_called_once_with()


@pytest.mark.asyncio
async def test_weather_alert_fetch_keeps_event_loop_responsive(mock_logger: Mock) -> None:
    service = _weather_service(mock_logger)
    gate = _HeartbeatGate()
    response = Mock(ok=True, status_code=200, text="<feed />")

    def slow_get(*_args: Any, **_kwargs: Any) -> Mock:
        gate.block_until_heartbeat()
        return response

    service.api_session = Mock()
    service.api_session.get = slow_get

    await _run_with_heartbeat(service._check_weather_alerts, gate)

    assert service._nws_alerts_available is True


@pytest.mark.asyncio
async def test_weather_alert_xml_parse_keeps_event_loop_responsive(
    mock_logger: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _weather_service(mock_logger)
    response = Mock(ok=True, status_code=200, text="<feed />")
    service.api_session = Mock()
    service.api_session.get.return_value = response
    gate = _HeartbeatGate()
    real_parse = weather_module.xml.dom.minidom.parseString

    def slow_parse(value: str) -> Any:
        gate.block_until_heartbeat()
        return real_parse(value)

    monkeypatch.setattr(weather_module.xml.dom.minidom, "parseString", slow_parse)

    await _run_with_heartbeat(service._check_weather_alerts, gate)

    assert service._nws_alerts_available is True


@pytest.mark.asyncio
async def test_weather_reverse_geocode_keeps_event_loop_responsive(
    mock_logger: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _weather_service(mock_logger)
    gate = _HeartbeatGate()

    def slow_reverse(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        gate.block_until_heartbeat()
        return {"city": "Seattle"}

    monkeypatch.setattr(
        "modules.utils.rate_limited_nominatim_reverse_sync",
        slow_reverse,
    )

    result = await _run_with_heartbeat(
        lambda: service._geocode_location(47.6, -122.3),
        gate,
    )

    assert result == "Seattle"


# ---------------------------------------------------------------------------
# Location commands: geocoding is a blocking HTTP call on the request path, so
# each of these would stall every other coroutine (message handling, reconnect)
# for the geocoder timeout if it ran inline. One gate per blocking seam.
# ---------------------------------------------------------------------------


def _aqi_command(mock_logger: Mock) -> AqiCommand:
    """AqiCommand without __init__ — it builds real HTTP sessions we don't want."""
    command = object.__new__(AqiCommand)
    command.bot = Mock()
    command.logger = mock_logger
    command.default_state = "WA"
    command.default_country = "US"
    return command


def _resolved_seattle() -> ResolvedLocation:
    return ResolvedLocation(
        lat=47.6062,
        lon=-122.3321,
        location_type="city",
        query="seattle",
        display_name="Seattle, WA",
        address_info=None,
    )


@pytest.mark.asyncio
async def test_aqi_geocode_keeps_event_loop_responsive(
    mock_logger: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = _aqi_command(mock_logger)
    gate = _HeartbeatGate()

    def slow_resolve(*_args: Any, **_kwargs: Any) -> ResolvedLocation:
        gate.block_until_heartbeat()
        return _resolved_seattle()

    monkeypatch.setattr(aqi_module, "resolve_location", slow_resolve)
    command.get_openmeteo_aqi = lambda _lat, _lon: "20 (Good)"  # type: ignore[method-assign]

    result = await _run_with_heartbeat(
        lambda: command.get_aqi_for_location("seattle"), gate
    )

    assert result == "Seattle, WA: 20 (Good)"


@pytest.mark.asyncio
async def test_aqi_openmeteo_fetch_keeps_event_loop_responsive(
    mock_logger: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = _aqi_command(mock_logger)
    gate = _HeartbeatGate()

    monkeypatch.setattr(
        aqi_module, "resolve_location", lambda *a, **kw: _resolved_seattle()
    )

    def slow_fetch(_lat: float, _lon: float) -> str:
        gate.block_until_heartbeat()
        return "20 (Good)"

    command.get_openmeteo_aqi = slow_fetch  # type: ignore[method-assign]

    result = await _run_with_heartbeat(
        lambda: command.get_aqi_for_location("seattle"), gate
    )

    assert result == "Seattle, WA: 20 (Good)"


@pytest.mark.asyncio
async def test_rain_location_resolution_keeps_event_loop_responsive() -> None:
    command, captured = build_cmd(make_series())
    gate = _HeartbeatGate()

    def slow_resolve(
        _message: Any, _location: Optional[str]
    ) -> tuple[float, float, str, None]:
        gate.block_until_heartbeat()
        return (36.1627, -86.7816, "Nashville, TN", None)

    command._resolve_location = slow_resolve  # type: ignore[method-assign]
    message = MeshMessage(
        content="!rain seattle", channel="general", is_dm=False, sender_id="U1"
    )

    result = await _run_with_heartbeat(lambda: command.execute(message), gate)

    assert result is True
    assert captured, "rain command sent no reply"


@pytest.mark.asyncio
async def test_aurora_location_resolution_keeps_event_loop_responsive() -> None:
    bot = _make_aurora_bot(with_location=True)
    command = AuroraCommand(bot)
    command.send_response = AsyncMock(return_value=True)
    bot.db_manager.execute_query.return_value = []
    gate = _HeartbeatGate()

    def slow_resolve(
        _message: Any, _location: Optional[str]
    ) -> tuple[float, float, str, None]:
        gate.block_until_heartbeat()
        return (47.6, -122.3, "Seattle, WA", None)

    command._resolve_location = slow_resolve  # type: ignore[method-assign]
    aurora_data = MagicMock(
        kp_index=2.5, kp_timestamp="2026-01-21 05:13:00", aurora_probability=15.0
    )
    message = MeshMessage(
        content="aurora seattle", channel="general", is_dm=False, sender_id="U1"
    )

    with patch("modules.commands.aurora_command.NOAAAuroraClient") as mock_client:
        mock_client.return_value.get_aurora_data.return_value = aurora_data
        result = await _run_with_heartbeat(lambda: command.execute(message), gate)

    assert result is True
    command.send_response.assert_called_once()


# ---------------------------------------------------------------------------
# Concurrency: offloading location work to worker threads means two commands can
# now be in flight at once. Anything they share has to survive that.
# ---------------------------------------------------------------------------


def test_nominatim_sync_gate_serializes_concurrent_worker_threads() -> None:
    """The 1 req/s policy must hold across threads, not just across awaits.

    Before this, `wait_for_request_sync()` only *waited* and the caller recorded
    the request after its HTTP call returned — so several threads cleared the
    gate together and hit Nominatim simultaneously, which is what gets an
    instance rate-limited or IP-banned by OSM.
    """
    from modules.rate_limiter import NominatimRateLimiter

    limiter = NominatimRateLimiter(0.05)
    hits: list[float] = []
    hits_lock = threading.Lock()

    def geocode() -> None:
        limiter.wait_and_request_sync()
        with hits_lock:
            hits.append(time.monotonic())
        time.sleep(0.02)  # stand-in for the HTTP round trip

    threads = [threading.Thread(target=geocode) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(hits) == 4
    gaps = [b - a for a, b in zip(sorted(hits), sorted(hits)[1:], strict=False)]
    assert all(g >= 0.05 for g in gaps), f"requests bunched up: {gaps}"


def test_geocode_cache_eviction_is_thread_safe() -> None:
    """Concurrent evictions used to raise KeyError / 'changed size during
    iteration' out of the command, leaving the user with no reply at all."""
    from modules.location import GEOCODE_CACHE_CAP, cache_put

    cache = {f"seed{i}": i for i in range(GEOCODE_CACHE_CAP)}
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def hammer(worker: int) -> None:
        barrier.wait()
        try:
            for i in range(60):
                cache_put(cache, f"w{worker}-{i}", i)
        except BaseException as exc:  # noqa: BLE001 - recording for the assert
            errors.append(exc)

    original = sys.getswitchinterval()
    sys.setswitchinterval(1e-9)  # widen the check-then-act window
    try:
        threads = [threading.Thread(target=hammer, args=(w,)) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    finally:
        sys.setswitchinterval(original)

    assert errors == [], f"cache_put raced: {errors[:3]}"
    assert len(cache) <= GEOCODE_CACHE_CAP, f"cap exceeded: {len(cache)}"


# ---------------------------------------------------------------------------
# Class-wide guard. The original report named aqi/rain/aurora, but prefix, alert
# and wx_international had the identical bug — !prefix worst of all, geocoding
# once per matching repeater row. A per-command fix list does not hold; this
# fails the build if a blocking geocode reappears anywhere on an async path.
# ---------------------------------------------------------------------------

# Module-level helpers that block on network I/O.
_BLOCKING_FUNCTIONS = frozenset({
    "geocode_city_sync",
    "geocode_zipcode_sync",
    "geocode_city_best_effort",
    "geocode_zipcode_best_effort",
    "rate_limited_nominatim_geocode_sync",
    "rate_limited_nominatim_reverse_sync",
    "resolve_location",
    "reverse_geocode_region",
    "zip_to_city_string",
    "location_zip_to_city_string",
    "wait_for_request_sync",
    "wait_and_request_sync",
})

# Methods whose bodies reach the helpers above.
_BLOCKING_METHODS = frozenset({
    "_resolve_location",
    "_coordinates_to_location_string",
    "_get_city_from_coordinates",
    "_get_state_country_from_coordinates",
    "_get_full_location_from_coordinates",
    "_zip_to_city_string",
    "_suffix_for_coords",
    "_reverse_geocode",
    "geocode_location",
})

# Calls whose arguments are the offload payload, not inline work.
_OFFLOADERS = frozenset({"to_thread", "run_in_executor"})


class _InlineBlockingCallFinder(ast.NodeVisitor):
    """Collect blocking calls that execute directly on the event loop."""

    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []
        self._async_depth = 0

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._async_depth += 1
        self.generic_visit(node)
        self._async_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # A plain def nested in an async def does not itself run on the loop.
        saved, self._async_depth = self._async_depth, 0
        self.generic_visit(node)
        self._async_depth = saved

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in _OFFLOADERS:
            return  # everything inside is deliberately off-thread
        if self._async_depth > 0 and name in (_BLOCKING_FUNCTIONS | _BLOCKING_METHODS):
            self.hits.append((node.lineno, name))
        self.generic_visit(node)


def test_no_blocking_geocode_on_async_paths() -> None:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    paths = [p for root in ("modules/commands", "modules/service_plugins") for p in sorted((repo_root / root).rglob("*.py"))]
    paths.append(repo_root / "modules" / "repeater_manager.py")
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        finder = _InlineBlockingCallFinder()
        finder.visit(tree)
        for lineno, name in finder.hits:
            rel = path.relative_to(repo_root)
            offenders.append(f"{rel}:{lineno} calls {name}() inline")

    assert offenders == [], (
        "blocking geocode/network work on an async path — wrap it in "
        "asyncio.to_thread (or move it into an offloaded sync helper):\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.asyncio
async def test_prefix_db_lookup_keeps_event_loop_responsive() -> None:
    """`!prefix` reverse-geocodes once per matching repeater row.

    On a cold cache a dozen matches meant a dozen sequential 1.1s rate-limit
    waits plus 10s socket timeouts, all on the event loop — the worst instance
    of this bug in the codebase.
    """
    from modules.commands.prefix_command import PrefixCommand

    command = object.__new__(PrefixCommand)
    gate = _HeartbeatGate()
    expected = {"node_count": 1, "node_names": ["R1"], "source": "database"}

    def slow_lookup(_prefix: str, _include_all: bool = False) -> dict[str, Any]:
        gate.block_until_heartbeat()
        return expected

    command._get_prefix_data_from_db_sync = slow_lookup  # type: ignore[method-assign]

    result = await _run_with_heartbeat(
        lambda: command.get_prefix_data_from_db("ab"), gate
    )

    assert result is expected


@pytest.mark.asyncio
async def test_global_wx_lookup_keeps_event_loop_responsive() -> None:
    """gwx geocodes *and* fetches the forecast; both were inline."""
    from modules.commands.alternatives.wx_international import GlobalWxCommand

    command = object.__new__(GlobalWxCommand)
    gate = _HeartbeatGate()

    def slow_lookup(
        _location: str,
        _forecast_type: str = "default",
        _num_days: int = 7,
        _message: Any = None,
    ) -> str:
        gate.block_until_heartbeat()
        return "Tokyo: 20C clear"

    command._get_weather_for_location_sync = slow_lookup  # type: ignore[method-assign]

    result = await _run_with_heartbeat(
        lambda: command.get_weather_for_location("Tokyo"), gate
    )

    assert result == "Tokyo: 20C clear"
