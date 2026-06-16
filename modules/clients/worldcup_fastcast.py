#!/usr/bin/env python3
"""
Experimental ESPN "fastcast" WebSocket client.

ESPN's own live scoreboard does not poll — it opens a WebSocket to a "fastcast" pub/sub
service that streams change notifications. This client connects to that service and, on
any push for the subscribed topic, invokes an ``on_push`` callback. The caller then
re-fetches authoritative state from the REST scoreboard (fastcast is used purely as a
low-latency "something changed" signal, not parsed for scores).

IMPORTANT: this is a reverse-engineered, undocumented protocol. The handshake, message
shapes, and especially topic names can change without notice. The client fails safe —
all network/parse errors are logged and retried with backoff, never raised — so a caller
that also polls on a heartbeat keeps working if fastcast is unavailable or the topic is
wrong (in which case no pushes simply arrive).
"""

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Awaitable, Callable, Optional, Union

import aiohttp

WEBSOCKET_HOST_URL = "https://fastcast.semfs.engsvc.go.com/public/websockethost"
DEFAULT_PROFILE = 12000
# ESPN's WS gateway expects browser-like headers; without an allowed Origin the upgrade is
# rejected (HTTP 502/403). These mimic the espn.com web client.
ORIGIN = "https://www.espn.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

PushCallback = Callable[[], Union[None, Awaitable[None]]]


class WorldCupFastcastClient:
    """Connects to ESPN fastcast and calls ``on_push`` when the subscribed topic updates."""

    def __init__(
        self,
        topic: str,
        on_push: PushCallback,
        logger: Optional[logging.Logger] = None,
        profile: int = DEFAULT_PROFILE,
        connect_timeout: int = 10,
    ) -> None:
        """Initialize the client.

        Args:
            topic: Fastcast topic to subscribe to (e.g. 'gp-soccer-fifa.world').
            on_push: Called (sync or async) on each relevant push. Should be cheap and
                non-blocking; the service uses it to set a wake event.
            logger: Logger for diagnostics.
            profile: Fastcast pub/sub profile id (12000 is the standard web profile).
            connect_timeout: Seconds for the host handshake / WS connect.
        """
        self.topic = topic
        self.on_push = on_push
        self.logger = logger or logging.getLogger(__name__)
        self.profile = profile
        self.connect_timeout = connect_timeout
        self._running = False
        self._sid: Optional[str] = None

    async def run(self) -> None:
        """Connect-and-listen loop with exponential backoff. Returns when stopped.

        Fails quietly: the first failure logs a single WARNING (so the operator knows it
        fell back to polling), repeats drop to DEBUG, and backoff grows to a 5-minute cap
        so a persistently-unavailable fastcast endpoint never spams the log.
        """
        self._running = True
        backoff = 5
        failures = 0
        while self._running:
            try:
                await self._connect_once()
                backoff = 5  # reset after a clean session
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                detail = str(e)[:160]
                if failures == 1:
                    self.logger.warning(
                        "Fastcast unavailable (%s); continuing on REST polling and "
                        "retrying quietly in the background.", detail
                    )
                elif failures % 30 == 0:
                    self.logger.warning("Fastcast still unavailable after %d attempts (%s)", failures, detail)
                else:
                    self.logger.debug("Fastcast connection error: %s", detail)
            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    def stop(self) -> None:
        self._running = False

    async def _connect_once(self) -> None:
        headers = {"Origin": ORIGIN, "User-Agent": USER_AGENT}
        timeout = aiohttp.ClientTimeout(total=self.connect_timeout)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(WEBSOCKET_HOST_URL) as resp:
                resp.raise_for_status()
                host = await resp.json()

            ip = host.get("ip")
            port = host.get("securePort") or host.get("port") or 443
            if not ip:
                raise ValueError("fastcast websockethost returned no ip")

            url = f"wss://{ip}:{port}/FastcastService/pubsub/profiles/{self.profile}?TrafficId={uuid.uuid4()}"
            async with session.ws_connect(url, heartbeat=25, origin=ORIGIN) as ws:
                self.logger.info("Fastcast connected (topic=%s)", self.topic)
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle(ws, msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    if not self._running:
                        await ws.close()
                        break

    async def _handle(self, ws: aiohttp.ClientWebSocketResponse, data: str) -> None:
        """Process one fastcast frame: capture session, subscribe, or fire on publish."""
        try:
            m = json.loads(data)
        except (ValueError, TypeError):
            return
        op = m.get("op")
        if op == "C":
            # Connect ack — capture the session id and subscribe to our topic.
            self._sid = m.get("sid")
            with contextlib.suppress(Exception):
                await ws.send_json({"op": "S", "sid": self._sid, "tc": self.topic})
        elif op == "P":
            # Publish — a change on some topic. Fire if it's (for) ours.
            tc = str(m.get("tc", ""))
            if tc == self.topic or self.topic in tc:
                await self._fire()
        elif op == "H":
            # Application-level heartbeat — echo to keep the session alive.
            with contextlib.suppress(Exception):
                await ws.send_json({"op": "H", "sid": self._sid})

    async def _fire(self) -> None:
        try:
            result = self.on_push()
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            self.logger.warning("Fastcast on_push callback error: %s", e)
