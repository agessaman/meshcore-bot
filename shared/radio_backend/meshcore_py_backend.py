"""meshcore_py adapter for the backend-neutral radio protocol."""

from __future__ import annotations

from typing import Any

from .contacts import contacts_dict_from_iterable
from .events import BackendEventType
from .protocol import BackendCapabilities, BackendCallback


class MeshcorePyBackend:
    """Thin wrapper around the existing meshcore_py object."""

    capabilities = BackendCapabilities(
        backend_name="meshcore_py",
        supports_cli_fallback=True,
        supports_device_contacts=True,
        supports_trace=True,
        supports_telemetry=True,
        supports_radio_config=True,
        supports_reboot=True,
        supports_custom_vars=True,
        supports_time=True,
    )

    def __init__(
        self,
        config: Any,
        logger: Any,
        connection_type: str,
        *,
        radio_debug: bool = False,
    ) -> None:
        self.config = config
        self.logger = logger
        self.connection_type = connection_type
        self.radio_debug = radio_debug
        self._meshcore: Any = None

    async def connect(self) -> bool:
        import meshcore

        if self.connection_type == "serial":
            serial_port = self.config.get("Connection", "serial_port", fallback="/dev/ttyUSB0")
            self.logger.info("Connecting via serial port: %s", serial_port)
            self._meshcore = await meshcore.MeshCore.create_serial(serial_port, debug=self.radio_debug)
        elif self.connection_type == "tcp":
            hostname = self.config.get("Connection", "hostname", fallback=None)
            tcp_port = self.config.getint("Connection", "tcp_port", fallback=5000)
            if not hostname:
                self.logger.error("TCP connection requires 'hostname' to be set in config")
                return False
            self.logger.info("Connecting via TCP: %s:%s", hostname, tcp_port)
            self._meshcore = await meshcore.MeshCore.create_tcp(hostname, tcp_port, debug=self.radio_debug)
        else:
            ble_device_name = self.config.get("Connection", "ble_device_name", fallback=None)
            self.logger.info("Connecting via BLE%s", f" to device: {ble_device_name}" if ble_device_name else "")
            self._meshcore = await meshcore.MeshCore.create_ble(ble_device_name, debug=self.radio_debug)
        return bool(getattr(self._meshcore, "is_connected", False))

    async def disconnect(self) -> None:
        if self._meshcore and hasattr(self._meshcore, "disconnect"):
            await self._meshcore.disconnect()

    @property
    def is_connected(self) -> bool:
        return bool(self._meshcore and getattr(self._meshcore, "is_connected", False))

    @property
    def commands(self) -> Any:
        return getattr(self._meshcore, "commands", None)

    @property
    def contacts(self) -> dict[str, dict[str, Any]]:
        return contacts_dict_from_iterable(getattr(self._meshcore, "contacts", {}))

    @contacts.setter
    def contacts(self, value: dict[str, dict[str, Any]]) -> None:
        if self._meshcore is not None:
            self._meshcore.contacts = value

    @property
    def channels(self) -> dict[int, dict[str, Any]]:
        return getattr(self._meshcore, "channels", {})

    @channels.setter
    def channels(self, value: dict[int, dict[str, Any]]) -> None:
        if self._meshcore is not None:
            self._meshcore.channels = value

    @property
    def self_info(self) -> dict[str, Any]:
        return getattr(self._meshcore, "self_info", {}) or {}

    @self_info.setter
    def self_info(self, value: dict[str, Any]) -> None:
        if self._meshcore is not None:
            self._meshcore.self_info = value

    def subscribe(self, event_type: BackendEventType | Any, callback: BackendCallback) -> Any:
        return self._meshcore.subscribe(_to_meshcore_event_type(event_type), callback)

    async def start_auto_message_fetching(self) -> None:
        if self._meshcore and hasattr(self._meshcore, "start_auto_message_fetching"):
            await self._meshcore.start_auto_message_fetching()

    async def wait_for_event(
        self,
        event_type: BackendEventType | Any,
        attribute_filters: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> Any:
        if not self._meshcore or not hasattr(self._meshcore, "wait_for_event"):
            return None
        return await self._meshcore.wait_for_event(
            _to_meshcore_event_type(event_type),
            attribute_filters=attribute_filters,
            timeout=timeout,
        )

    async def health_check(self) -> bool:
        if not self.commands or not hasattr(self.commands, "get_time"):
            return self.is_connected
        result = await self.commands.get_time()
        return result is not None

    def get_contact_by_name(self, name: str) -> dict[str, Any] | None:
        if self._meshcore and hasattr(self._meshcore, "get_contact_by_name"):
            contact = self._meshcore.get_contact_by_name(name)
            return dict(contact) if isinstance(contact, dict) else contact
        for contact in self.contacts.values():
            if contact.get("name") == name or contact.get("adv_name") == name:
                return contact
        return None

    def get_contact_by_key_prefix(self, prefix: str) -> dict[str, Any] | None:
        if self._meshcore and hasattr(self._meshcore, "get_contact_by_key_prefix"):
            contact = self._meshcore.get_contact_by_key_prefix(prefix)
            return dict(contact) if isinstance(contact, dict) else contact
        for public_key, contact in self.contacts.items():
            if public_key.startswith(prefix):
                return contact
        return None

    def __getattr__(self, name: str) -> Any:
        if self._meshcore is None:
            raise AttributeError(name)
        return getattr(self._meshcore, name)


def _to_meshcore_event_type(event_type: BackendEventType | Any) -> Any:
    if not isinstance(event_type, BackendEventType):
        return event_type
    from meshcore import EventType

    return {
        BackendEventType.CONTACT_MSG_RECV: getattr(EventType, "CONTACT_MSG_RECV", event_type),
        BackendEventType.CHANNEL_MSG_RECV: getattr(EventType, "CHANNEL_MSG_RECV", event_type),
        BackendEventType.RX_LOG_DATA: getattr(EventType, "RX_LOG_DATA", event_type),
        BackendEventType.RAW_DATA: getattr(EventType, "RAW_DATA", event_type),
        BackendEventType.NEW_CONTACT: getattr(EventType, "NEW_CONTACT", event_type),
        BackendEventType.CHANNEL_INFO: getattr(EventType, "CHANNEL_INFO", event_type),
        BackendEventType.TRACE_DATA: getattr(EventType, "TRACE_DATA", event_type),
        BackendEventType.DEVICE_INFO: getattr(EventType, "DEVICE_INFO", event_type),
        BackendEventType.MSG_SENT: getattr(EventType, "MSG_SENT", event_type),
        BackendEventType.OK: getattr(EventType, "OK", event_type),
        BackendEventType.ERROR: getattr(EventType, "ERROR", event_type),
        BackendEventType.STATS_CORE: getattr(EventType, "STATS_CORE", event_type),
        BackendEventType.STATS_RADIO: getattr(EventType, "STATS_RADIO", event_type),
    }.get(event_type, event_type)
