"""Shared backend event bus utilities."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import defaultdict
from typing import Any

from .events import BackendEvent, BackendEventType
from .protocol import BackendCallback


class BackendEventBus:
    """Small async-aware event bus used by backend adapters."""

    def __init__(self) -> None:
        self._subscribers: dict[BackendEventType, list[BackendCallback]] = defaultdict(list)
        self._recent_events: list[BackendEvent] = []

    def subscribe(self, event_type: BackendEventType, callback: BackendCallback) -> tuple[BackendEventType, BackendCallback]:
        self._subscribers[event_type].append(callback)
        return (event_type, callback)

    async def emit(self, event: BackendEvent) -> None:
        event.metadata.setdefault("_emitted_at", time.time())
        self._recent_events.append(event)
        if len(self._recent_events) > 100:
            self._recent_events = self._recent_events[-100:]
        for callback in list(self._subscribers.get(event.type, [])):
            result = callback(event, event.metadata)
            if inspect.isawaitable(result):
                await result

    async def wait_for_event(
        self,
        event_type: BackendEventType,
        attribute_filters: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> BackendEvent | None:
        attribute_filters = attribute_filters or {}
        for event in reversed(self._recent_events):
            if self._matches(event, event_type, attribute_filters):
                return event

        loop = asyncio.get_running_loop()
        future: asyncio.Future[BackendEvent] = loop.create_future()

        async def _capture(event: BackendEvent, metadata: dict[str, Any] | None = None) -> None:
            if not future.done() and self._matches(event, event_type, attribute_filters):
                future.set_result(event)

        subscription = self.subscribe(event_type, _capture)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            callbacks = self._subscribers.get(subscription[0], [])
            if subscription[1] in callbacks:
                callbacks.remove(subscription[1])

    @staticmethod
    def _matches(event: BackendEvent, event_type: BackendEventType, attribute_filters: dict[str, Any]) -> bool:
        if event.type != event_type:
            return False
        for key, expected in attribute_filters.items():
            if event.payload.get(key) != expected and event.metadata.get(key) != expected:
                return False
        return True
