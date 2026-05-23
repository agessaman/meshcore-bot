"""Backend-neutral radio integration for meshcore-bot."""

from .events import BackendEvent, BackendEventType
from .factory import create_radio_backend
from .protocol import BackendCapabilities, RadioBackend, RadioCommands
from .results import BackendResult, is_error, is_ok, is_sent

__all__ = [
    "BackendCapabilities",
    "BackendEvent",
    "BackendEventType",
    "BackendResult",
    "RadioBackend",
    "RadioCommands",
    "create_radio_backend",
    "is_error",
    "is_ok",
    "is_sent",
]
