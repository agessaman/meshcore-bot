"""pyMC_core CompanionRadio backend."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import sys
import time
from pathlib import Path
from typing import Any

from .base import BackendEventBus
from .contacts import contacts_dict_from_iterable, normalize_contact_dict
from .events import BackendEvent, BackendEventType
from .protocol import BackendCapabilities, BackendCallback
from .results import BackendResult


class PyMcCoreBackend:
    """RadioBackend implementation backed by pyMC_core CompanionRadio."""

    capabilities = BackendCapabilities(
        backend_name="pymc_core",
        unlimited_contacts=True,
        supports_cli_fallback=False,
        supports_device_contacts=False,
        supports_trace=True,
        supports_telemetry=True,
        supports_radio_config=True,
        supports_reboot=True,
        supports_custom_vars=True,
        supports_time=True,
    )

    def __init__(self, config: Any, db_manager: Any, logger: Any) -> None:
        self.config = config
        self.db_manager = db_manager
        self.logger = logger
        self._bus = BackendEventBus()
        self._radio: Any = None
        self._companion: Any = None
        self._connected = False
        self.commands = PyMcCommands(self)
        self.contacts: dict[str, dict[str, Any]] = {}
        self.channels: dict[int, dict[str, Any]] = {}
        self.self_info: dict[str, Any] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected and bool(self._companion and getattr(self._companion, "is_running", False))

    async def connect(self) -> bool:
        try:
            radio_type = self.config.get("Connection", "pymc_radio_type", fallback="kiss-modem").strip().lower()
            if radio_type != "kiss-modem":
                self.logger.error("Unsupported pyMC radio type for v1: %s", radio_type)
                return False

            _ensure_pymc_core_importable()
            from pymc_core.companion import ADV_TYPE_CHAT, CompanionRadio
            from pymc_core.hardware.kiss_modem_wrapper import KissModemWrapper
            from pymc_core.protocol import LocalIdentity

            radio_config = self._radio_config()
            port = self.config.get("Connection", "pymc_serial_port", fallback="/dev/ttyUSB0")
            baudrate = self.config.getint("Connection", "pymc_baudrate", fallback=115200)
            self.logger.info("Connecting to pyMC KISS modem on %s at %s baud", port, baudrate)
            self._loop = asyncio.get_running_loop()
            self._radio = KissModemWrapper(
                port=port,
                baudrate=baudrate,
                radio_config=radio_config,
                auto_configure=True,
                lbt_enabled=self.config.getboolean("Connection", "pymc_lbt_enabled", fallback=False),
            )
            if not self._radio.connect():
                self.logger.error("Failed to connect pyMC KISS modem")
                return False
            if hasattr(self._radio, "set_event_loop"):
                self._radio.set_event_loop(self._loop)

            identity = LocalIdentity(seed=self._load_or_create_identity_seed())
            node_name = self.config.get("Bot", "bot_name", fallback="MeshCoreBot")
            max_contacts = self.config.getint("Connection", "pymc_max_contacts", fallback=100000)
            max_channels = self.config.getint("Bot", "max_channels", fallback=40)
            initial_contacts = self._load_contacts_from_db()
            self._companion = CompanionRadio(
                self._radio,
                identity,
                node_name=node_name,
                adv_type=ADV_TYPE_CHAT,
                max_contacts=max_contacts,
                max_channels=max_channels,
                radio_config=radio_config,
                initial_contacts=initial_contacts,
            )
            self._register_companion_callbacks()
            self._apply_configured_channels()
            await self._companion.start()
            self._connected = True
            self._refresh_contacts()
            self._refresh_channels()
            self._refresh_self_info()
            return True
        except ImportError as exc:
            self.logger.error(
                "pyMC backend selected but pymc-core is not importable. Install it or set PYTHONPATH=/Users/adam/pymc_core/src: %s",
                exc,
            )
            return False
        except Exception as exc:
            self.logger.error("pyMC backend connection failed: %s", exc, exc_info=True)
            return False

    async def disconnect(self) -> None:
        if self._companion:
            await self._companion.stop()
        if self._radio and hasattr(self._radio, "disconnect"):
            self._radio.disconnect()
        self._connected = False

    async def start_auto_message_fetching(self) -> None:
        # CompanionRadio callback delivery is live; no polling task required.
        return None

    def subscribe(self, event_type: BackendEventType | Any, callback: BackendCallback) -> Any:
        return self._bus.subscribe(_to_backend_event_type(event_type), callback)

    async def wait_for_event(
        self,
        event_type: BackendEventType | Any,
        attribute_filters: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> BackendEvent | None:
        return await self._bus.wait_for_event(_to_backend_event_type(event_type), attribute_filters=attribute_filters, timeout=timeout)

    async def health_check(self) -> bool:
        if not self._radio:
            return False
        if hasattr(self._radio, "ping"):
            result = self._radio.ping()
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        return self.is_connected

    def get_contact_by_name(self, name: str) -> dict[str, Any] | None:
        for contact in self.contacts.values():
            if contact.get("name") == name or contact.get("adv_name") == name:
                return contact
        return None

    def get_contact_by_key_prefix(self, prefix: str) -> dict[str, Any] | None:
        prefix = (prefix or "").strip().lower()
        for public_key, contact in self.contacts.items():
            if public_key.lower().startswith(prefix):
                return contact
        return None

    async def _emit(self, event_type: BackendEventType, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
        await self._bus.emit(BackendEvent(event_type, payload, metadata or {}))

    def _schedule_emit(self, event_type: BackendEventType, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(lambda: loop.create_task(self._emit(event_type, payload, metadata)))

    def _radio_config(self) -> dict[str, Any]:
        cfg = {
            "frequency": self.config.getint("Connection", "pymc_frequency", fallback=910525000),
            "bandwidth": self.config.getint("Connection", "pymc_bandwidth", fallback=62500),
            "spreading_factor": self.config.getint("Connection", "pymc_spreading_factor", fallback=7),
            "coding_rate": self.config.getint("Connection", "pymc_coding_rate", fallback=5),
            "power": self.config.getint("Connection", "pymc_tx_power", fallback=22),
            "tx_power": self.config.getint("Connection", "pymc_tx_power", fallback=22),
            "tx_delay_ms": self.config.getint("Connection", "pymc_tx_delay_ms", fallback=50),
        }
        optional_ints = {
            "kiss_persistence": "pymc_kiss_persistence",
            "kiss_slottime_ms": "pymc_kiss_slottime_ms",
            "kiss_txtail_ms": "pymc_kiss_txtail_ms",
        }
        for key, config_key in optional_ints.items():
            if self.config.has_option("Connection", config_key):
                cfg[key] = self.config.getint("Connection", config_key)
        if self.config.has_option("Connection", "pymc_kiss_full_duplex"):
            cfg["kiss_full_duplex"] = self.config.getboolean("Connection", "pymc_kiss_full_duplex")
        return cfg

    def _identity_file(self) -> Path:
        raw = self.config.get("Connection", "pymc_identity_file", fallback="pymc_identity.key").strip()
        path = Path(raw).expanduser()
        if path.is_absolute():
            return path
        bot = getattr(self.db_manager, "bot", None)
        root = Path(getattr(bot, "bot_root", os.getcwd()))
        return root / path

    def _load_or_create_identity_seed(self) -> bytes:
        path = self._identity_file()
        if path.exists():
            data = path.read_bytes().strip()
            try:
                return bytes.fromhex(data.decode("ascii"))
            except ValueError:
                return bytes(data)
        seed = os.urandom(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(seed.hex(), encoding="ascii")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self.logger.info("Created pyMC identity file at %s", path)
        return seed

    def _load_contacts_from_db(self) -> list[Any]:
        if hasattr(self.db_manager, "get_pymc_contacts_for_backend"):
            records = self.db_manager.get_pymc_contacts_for_backend()
        else:
            records = []
        try:
            from pymc_core.companion.models import Contact

            return [Contact.from_dict(record) for record in records]
        except Exception as exc:
            self.logger.warning("Unable to hydrate pyMC contacts from DB: %s", exc)
            return []

    def _refresh_contacts(self) -> None:
        if self._companion:
            self.contacts = contacts_dict_from_iterable(self._companion.get_contacts())

    def _refresh_channels(self) -> None:
        if not self._companion:
            return
        max_channels = self.config.getint("Bot", "max_channels", fallback=40)
        channels: dict[int, dict[str, Any]] = {}
        for idx in range(max_channels):
            channel = self._companion.get_channel(idx)
            if channel:
                secret = getattr(channel, "secret", b"")
                channels[idx] = {
                    "channel_idx": idx,
                    "channel_name": getattr(channel, "name", ""),
                    "name": getattr(channel, "name", ""),
                    "channel_secret": secret,
                    "channel_key_hex": secret.hex() if isinstance(secret, bytes) else "",
                }
        self.channels = channels

    def _refresh_self_info(self) -> None:
        if not self._companion:
            return
        public_key = self._companion.get_public_key().hex()
        info = self._companion.get_self_info()
        name = getattr(info, "node_name", self.config.get("Bot", "bot_name", fallback="MeshCoreBot"))
        self.self_info = {
            "name": name,
            "adv_name": name,
            "public_key": public_key,
            "backend": "pymc_core",
        }

    def _register_companion_callbacks(self) -> None:
        c = self._companion

        def on_message(sender_key: bytes, text: str, timestamp: int, txt_type: int = 0, *extra: Any) -> None:
            public_key = sender_key.hex() if isinstance(sender_key, bytes) else str(sender_key)
            contact = self.contacts.get(public_key) or self.get_contact_by_key_prefix(public_key[:2]) or {}
            snr, rssi = _extract_signal(extra)
            payload = {
                "text": text,
                "public_key": public_key,
                "pubkey_prefix": public_key[:2],
                "sender_timestamp": timestamp,
                "snr": snr,
                "rssi": rssi,
                "path_len": contact.get("out_path_len", -1),
            }
            self._schedule_emit(BackendEventType.CONTACT_MSG_RECV, payload, {"pubkey_prefix": public_key[:2], "snr": snr, "rssi": rssi})

        def on_channel(channel_name: str, sender_name: str, text: str, timestamp: int, path_len: int, channel_idx: int, *extra: Any) -> None:
            snr, rssi = _extract_signal(extra)
            payload = {
                "channel_idx": channel_idx,
                "channel_name": channel_name,
                "text": f"{sender_name}: {text}",
                "sender": sender_name,
                "sender_timestamp": timestamp,
                "path_len": path_len,
                "snr": snr,
                "rssi": rssi,
            }
            self._schedule_emit(BackendEventType.CHANNEL_MSG_RECV, payload, {"snr": snr, "rssi": rssi})

        def on_rx_log(snr: float, rssi: int, raw_bytes: bytes) -> None:
            raw_hex = raw_bytes.hex() if isinstance(raw_bytes, bytes) else str(raw_bytes)
            self._schedule_emit(BackendEventType.RX_LOG_DATA, {"snr": snr, "rssi": rssi, "raw_hex": raw_hex})

        def on_raw(payload: bytes, snr: float, rssi: int) -> None:
            raw_hex = payload.hex() if isinstance(payload, bytes) else str(payload)
            self._schedule_emit(BackendEventType.RAW_DATA, {"snr": snr, "rssi": rssi, "raw_hex": raw_hex, "payload": raw_hex})

        def on_advert(contact: Any) -> None:
            normalized = normalize_contact_dict(contact)
            self.contacts[normalized["public_key"]] = normalized
            if hasattr(self.db_manager, "upsert_pymc_contact_from_advert"):
                self.db_manager.upsert_pymc_contact_from_advert(
                    contact,
                    raw_advert_packet=getattr(contact, "last_advert_packet", None),
                )
            self._schedule_emit(BackendEventType.NEW_CONTACT, normalized)

        def on_path_updated(contact: Any) -> None:
            normalized = normalize_contact_dict(contact)
            self.contacts[normalized["public_key"]] = normalized
            if hasattr(self.db_manager, "update_pymc_contact_path"):
                self.db_manager.update_pymc_contact_path(
                    normalized["public_key"],
                    normalized.get("out_path", ""),
                    normalized.get("out_path_len", -1),
                )

        def on_channel_updated(idx: int, channel: Any) -> None:
            self._refresh_channels()
            payload = self.channels.get(idx, {"channel_idx": idx, "channel_name": ""})
            self._schedule_emit(BackendEventType.CHANNEL_INFO, payload)

        c.on_message_received(on_message)
        c.on_channel_message_received(on_channel)
        c.on_rx_log_data(on_rx_log)
        c.on_raw_data_received(on_raw)
        c.on_advert_received(on_advert)
        c.on_node_discovered(lambda data: self._schedule_emit(BackendEventType.NEW_CONTACT, dict(data or {})))
        c.on_contact_path_updated(on_path_updated)
        c.on_channel_updated(on_channel_updated)
        c.on_send_confirmed(lambda ack_crc: self._schedule_emit(BackendEventType.MSG_SENT, {"ack_crc": ack_crc}))
        c.on_telemetry_response(lambda data: self._schedule_emit(BackendEventType.DEVICE_INFO, dict(data or {})))

    def _apply_configured_channels(self) -> None:
        if not self._companion or not self.config.has_section("Channels"):
            return
        index = 0
        monitor_channels = self.config.get("Channels", "monitor_channels", fallback="")
        for raw_name in [part.strip() for part in monitor_channels.split(",") if part.strip()]:
            secret = _channel_secret_for_name(raw_name)
            self._companion.set_channel(index, raw_name, secret)
            index += 1
        private_channels = self.config.get("Channels", "private_channels", fallback="")
        for item in [part.strip() for part in private_channels.split(",") if part.strip()]:
            if ":" not in item:
                continue
            name, secret_hex = item.split(":", 1)
            try:
                secret = bytes.fromhex(secret_hex.strip())
            except ValueError:
                self.logger.warning("Invalid private channel key for %s", name)
                continue
            self._companion.set_channel(index, name.strip(), secret)
            index += 1


class PyMcCommands:
    """meshcore_py-like commands facade backed by CompanionRadio."""

    def __init__(self, backend: PyMcCoreBackend) -> None:
        self.backend = backend

    @property
    def companion(self) -> Any:
        return self.backend._companion

    async def get_contacts(self) -> BackendResult:
        self.backend._refresh_contacts()
        return BackendResult.ok({"contacts": list(self.backend.contacts.values())})

    async def send_msg(self, contact: dict[str, Any], content: str) -> BackendResult:
        public_key = _public_key_bytes(contact)
        if not public_key:
            return BackendResult.error("missing_public_key")
        result = await self.companion.send_text_message(public_key, content, wait_for_ack=True)
        if getattr(result, "success", False):
            return BackendResult.sent({"is_flood": getattr(result, "is_flood", False), "expected_ack": getattr(result, "expected_ack", None)})
        return BackendResult.error("no_event_received")

    async def send_msg_with_retry(self, contact: dict[str, Any], content: str, **kwargs: Any) -> BackendResult:
        max_attempts = int(kwargs.get("max_attempts", 3) or 3)
        public_key = _public_key_bytes(contact)
        if not public_key:
            return BackendResult.error("missing_public_key")
        last_result = None
        for attempt in range(1, max_attempts + 1):
            last_result = await self.companion.send_text_message(public_key, content, attempt=attempt, wait_for_ack=True)
            if getattr(last_result, "success", False):
                return BackendResult.sent({"attempt": attempt, "is_flood": getattr(last_result, "is_flood", False)})
        return BackendResult.error("no_event_received", attempts=max_attempts, result=repr(last_result))

    async def send_chan_msg(self, channel_idx: int, content: str, timestamp: int | None = None) -> BackendResult:
        success = await self.companion.send_channel_message(channel_idx, content)
        return BackendResult.sent({"channel_idx": channel_idx}) if success else BackendResult.error("send_failed")

    async def send_advert(self, flood: bool = False) -> BackendResult:
        success = await self.companion.advertise(flood=flood)
        return BackendResult.ok({"flood": flood}) if success else BackendResult.error("send_failed")

    async def get_channel(self, channel_idx: int) -> BackendResult:
        channel = self.companion.get_channel(channel_idx)
        if not channel:
            return BackendResult.ok({"channel_idx": channel_idx, "channel_name": "", "channel_secret": b""})
        secret = getattr(channel, "secret", b"")
        return BackendResult.ok({"channel_idx": channel_idx, "channel_name": getattr(channel, "name", ""), "channel_secret": secret})

    async def set_channel(self, channel_idx: int, name: str, secret: bytes | str | None = None) -> BackendResult:
        if secret is None and name.startswith("#"):
            secret = _channel_secret_for_name(name)
        elif isinstance(secret, str):
            secret = bytes.fromhex(secret) if secret else b"\x00" * 16
        elif secret is None:
            secret = b"\x00" * 16
        success = self.companion.set_channel(channel_idx, name, secret)
        self.backend._refresh_channels()
        return BackendResult.ok({"channel_idx": channel_idx}) if success else BackendResult.error("set_channel_failed")

    async def set_flood_scope(self, scope: str) -> BackendResult:
        scope = (scope or "").strip()
        if not scope or scope in ("*", "0", "None"):
            self.companion.set_flood_scope(None)
        elif scope.startswith("#"):
            self.companion.set_flood_region(scope[1:])
        else:
            self.companion.set_flood_region(scope)
        return BackendResult.ok({"scope": scope})

    async def get_time(self) -> BackendResult:
        return BackendResult.ok({"time": self.companion.get_time()})

    async def set_time(self, timestamp: int) -> BackendResult:
        return BackendResult.ok() if self.companion.set_time(timestamp) else BackendResult.error("set_time_failed")

    async def set_name(self, name: str) -> BackendResult:
        self.companion.set_advert_name(name)
        self.backend._refresh_self_info()
        return BackendResult.ok({"name": name})

    async def reboot(self) -> BackendResult:
        radio = self.backend._radio
        if radio and hasattr(radio, "reboot"):
            result = radio.reboot()
            if inspect.isawaitable(result):
                result = await result
            return BackendResult.ok({"rebooted": bool(result)})
        return BackendResult.error("unsupported")

    async def send_device_query(self) -> BackendResult:
        return BackendResult.ok(
            {
                "backend": "pymc_core",
                "max_contacts": self.companion.contacts.max_contacts,
                "contacts_count": self.companion.contacts.get_count(),
                "max_channels": self.companion.channels.max_channels,
            }
        )

    async def add_contact(self, *args: Any, **kwargs: Any) -> BackendResult:
        from pymc_core.companion.models import Contact

        if args and isinstance(args[0], dict):
            contact = Contact.from_dict(args[0])
        elif len(args) >= 2:
            contact = Contact.from_dict({"name": args[0], "public_key": args[1]})
        else:
            contact = Contact.from_dict(kwargs)
        success = self.companion.add_update_contact(contact)
        self.backend._refresh_contacts()
        return BackendResult.ok() if success else BackendResult.error("contact_add_failed")

    async def remove_contact(self, public_key: str) -> BackendResult:
        success = self.companion.remove_contact(bytes.fromhex(public_key))
        self.backend._refresh_contacts()
        return BackendResult.ok() if success else BackendResult.error("not_found", error_code=2)

    async def change_contact_flags(self, contact: dict[str, Any], flags: int) -> BackendResult:
        public_key = _public_key_bytes(contact)
        if not public_key:
            return BackendResult.error("missing_public_key")
        stored = self.companion.get_contact_by_key(public_key)
        if not stored:
            return BackendResult.error("not_found", error_code=2)
        stored.flags = flags
        self.companion.add_update_contact(stored)
        self.backend._refresh_contacts()
        return BackendResult.ok({"flags": flags})

    async def set_manual_add_contacts(self, enabled: bool) -> BackendResult:
        current = self.companion.prefs
        telemetry_modes = (
            current.telemetry_mode_base
            | (current.telemetry_mode_location << 2)
            | (current.telemetry_mode_environment << 4)
        )
        self.companion.set_other_params(1 if enabled else 0, telemetry_modes, current.advert_loc_policy, current.multi_acks)
        return BackendResult.ok({"manual_add_contacts": enabled})

    async def set_autoadd_config(self, value: int) -> BackendResult:
        self.companion.set_autoadd_config(value)
        return BackendResult.ok({"autoadd_config": value})

    async def set_radio(self, frequency: int, bandwidth: int, spreading_factor: int, coding_rate: int) -> BackendResult:
        ok = self.companion.set_radio_params(frequency, bandwidth, spreading_factor, coding_rate)
        return BackendResult.ok() if ok else BackendResult.error("set_radio_failed")

    async def set_tx_power(self, power_dbm: int) -> BackendResult:
        ok = self.companion.set_tx_power(power_dbm)
        return BackendResult.ok() if ok else BackendResult.error("set_tx_power_failed")

    async def get_stats_core(self) -> BackendResult:
        from pymc_core.companion.constants import STATS_TYPE_CORE

        return BackendResult(BackendEventType.STATS_CORE, self.companion.get_stats(STATS_TYPE_CORE))

    async def get_stats_radio(self) -> BackendResult:
        from pymc_core.companion.constants import STATS_TYPE_RADIO

        return BackendResult(BackendEventType.STATS_RADIO, self.companion.get_stats(STATS_TYPE_RADIO))

    async def get_path_hash_mode(self) -> BackendResult:
        return BackendResult.ok({"path_hash_mode": self.companion.prefs.path_hash_mode})

    async def set_path_hash_mode(self, mode: int) -> BackendResult:
        self.companion.set_path_hash_mode(mode)
        return BackendResult.ok({"path_hash_mode": mode})

    async def get_custom_vars(self) -> BackendResult:
        return BackendResult.ok({"custom_vars": self.companion.get_custom_vars()})

    async def set_custom_var(self, name: str, value: str) -> BackendResult:
        return BackendResult.ok() if self.companion.set_custom_var(name, value) else BackendResult.error("set_custom_var_failed")

    async def send_appstart(self) -> BackendResult:
        # pyMC CompanionRadio has no distinct app-start packet; an advert is the closest firmware-visible signal.
        success = await self.companion.advertise(flood=False)
        return BackendResult.ok({"sent_as": "advert"}) if success else BackendResult.error("send_failed")

    async def send_status_request(self, contact: dict[str, Any] | str, timeout: float = 15.0) -> BackendResult:
        result = await self.companion.send_status_request(_public_key_bytes(contact), timeout=timeout)
        return BackendResult.ok(result) if result else BackendResult.error("timeout")

    async def send_telemetry_request(self, contact: dict[str, Any] | str, timeout: float = 15.0) -> BackendResult:
        result = await self.companion.send_telemetry_request(_public_key_bytes(contact), timeout=timeout)
        return BackendResult.ok(result) if result else BackendResult.error("timeout")

    async def send_trace(
        self,
        auth_code: int = 0,
        tag: int = 0,
        flags: int = 0,
        path: str | None = None,
    ) -> BackendResult:
        path_bytes = b""
        if path:
            try:
                path_bytes = bytes(int(part.strip(), 16) for part in path.split(",") if part.strip())
            except ValueError:
                return BackendResult.error("invalid_path")
        success = await self.companion.send_trace_path_raw(tag=tag, auth_code=auth_code, flags=flags, path_bytes=path_bytes)
        return BackendResult.ok({"tag": tag}) if success else BackendResult.error("send_trace_failed")


def _channel_secret_for_name(name: str) -> bytes:
    if not name.startswith("#"):
        name = "#" + name
    return hashlib.sha256(name.lower().encode("utf-8")).digest()[:16]


def _ensure_pymc_core_importable() -> None:
    try:
        import pymc_core  # noqa: F401
        return
    except ImportError:
        local_src = Path("/Users/adam/pymc_core/src")
        if local_src.exists():
            sys.path.insert(0, str(local_src))


def _to_backend_event_type(event_type: Any) -> BackendEventType:
    if isinstance(event_type, BackendEventType):
        return event_type
    name = getattr(event_type, "name", str(event_type).split(".")[-1]).upper()
    try:
        return BackendEventType[name]
    except KeyError:
        value = str(getattr(event_type, "value", event_type)).lower()
        for candidate in BackendEventType:
            if candidate.value == value:
                return candidate
        raise ValueError(f"Unsupported backend event type: {event_type!r}")


def _public_key_bytes(contact: dict[str, Any] | str | bytes | bytearray) -> bytes:
    if isinstance(contact, bytes):
        return contact
    if isinstance(contact, bytearray):
        return bytes(contact)
    public_key = contact if isinstance(contact, str) else contact.get("public_key", "")
    try:
        return bytes.fromhex(public_key)
    except (TypeError, ValueError):
        return b""


def _extract_signal(values: tuple[Any, ...]) -> tuple[float | None, int | None]:
    snr = None
    rssi = None
    for value in values:
        if isinstance(value, float) and snr is None:
            snr = value
        elif isinstance(value, int) and rssi is None:
            rssi = value
    return snr, rssi
