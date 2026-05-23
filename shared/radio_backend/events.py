"""Backend-neutral event models for radio integrations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BackendEventType(str, Enum):
    """Event types consumed by the bot independent of the radio library."""

    CONTACT_MSG_RECV = "contact_msg_recv"
    CHANNEL_MSG_RECV = "channel_msg_recv"
    RX_LOG_DATA = "rx_log_data"
    RAW_DATA = "raw_data"
    NEW_CONTACT = "new_contact"
    CHANNEL_INFO = "channel_info"
    TRACE_DATA = "trace_data"
    DEVICE_INFO = "device_info"
    MSG_SENT = "msg_sent"
    OK = "ok"
    ERROR = "error"
    STATS_CORE = "stats_core"
    STATS_RADIO = "stats_radio"


@dataclass
class BackendEvent:
    """Small event object matching the shape used by meshcore_py callbacks."""

    type: BackendEventType
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}
