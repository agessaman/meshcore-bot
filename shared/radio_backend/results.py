"""Backend-neutral command result helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import BackendEventType


@dataclass
class BackendResult:
    """Command result with the same `.type` and `.payload` surface as meshcore_py."""

    type: Any
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.type = ComparableEventType(self.type)

    @classmethod
    def ok(cls, payload: dict[str, Any] | None = None) -> "BackendResult":
        return cls(BackendEventType.OK, payload or {})

    @classmethod
    def sent(cls, payload: dict[str, Any] | None = None) -> "BackendResult":
        return cls(BackendEventType.MSG_SENT, payload or {})

    @classmethod
    def error(cls, reason: str, **payload: Any) -> "BackendResult":
        return cls(BackendEventType.ERROR, {"reason": reason, **payload})


def is_ok(result: Any) -> bool:
    return _event_type_name(getattr(result, "type", None)) == "OK"


def is_sent(result: Any) -> bool:
    return _event_type_name(getattr(result, "type", None)) == "MSG_SENT"


def is_error(result: Any) -> bool:
    return _event_type_name(getattr(result, "type", None)) == "ERROR"


def _event_type_name(event_type: Any) -> str:
    if isinstance(event_type, ComparableEventType):
        return event_type.name
    if isinstance(event_type, BackendEventType):
        return event_type.name
    name = getattr(event_type, "name", None)
    if name:
        return str(name).upper()
    value = getattr(event_type, "value", event_type)
    return str(value).split(".")[-1].replace("-", "_").upper()


class ComparableEventType:
    """Event type wrapper that compares equal to meshcore and backend enums by name."""

    def __init__(self, event_type: Any) -> None:
        self.name = _event_type_name(event_type)
        self.value = self.name.lower()

    def __eq__(self, other: object) -> bool:
        return self.name == _event_type_name(other)

    def __hash__(self) -> int:
        return hash(self.name)

    def __repr__(self) -> str:
        return f"BackendEventType.{self.name}"
