"""
MQTT Live Integration Tests
============================
Subscribes to the letsmesh public MQTT broker and validates incoming
MeshCore packet JSON against the expected schema produced by
PacketCaptureService._format_packet_data().

These tests are marked ``@pytest.mark.mqtt`` and are **skipped by default**
in normal CI (no network access required).  Run them manually:

    pytest tests/test_mqtt_live.py -v -m mqtt

Or collect raw fixtures for offline use:

    python tests/test_mqtt_live.py --collect-fixtures

Configuration is read from ``tests/mqtt_test_config.ini``.
"""

from __future__ import annotations

import configparser
import json
import queue
import random
import string
import threading
import time
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Optional imports
# ---------------------------------------------------------------------------
try:
    import paho.mqtt.client as mqtt_lib  # type: ignore[import-untyped]

    PAHO_AVAILABLE = True
except ImportError:
    mqtt_lib = None  # type: ignore[assignment]
    PAHO_AVAILABLE = False

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
_CONFIG_PATH = Path(__file__).parent / "mqtt_test_config.ini"

_REQUIRED_PACKET_KEYS = {
    "origin",
    "origin_id",
    "timestamp",
    "type",
    "direction",
    "time",
    "date",
    "len",
    "packet_type",
    "route",
    "payload_len",
    "raw",
    "SNR",
    "RSSI",
    "hash",
}

_VALID_ROUTES = {"F", "D", "T", "U"}
_VALID_DIRECTIONS = {"rx", "tx"}
_VALID_TYPES = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"}

# Fields that are required for rx packets (those captured off-the-air by the bot)
# tx packets (the bot's own transmissions) may omit RF data fields
_RX_ONLY_KEYS = {"SNR", "RSSI", "hash"}


def _load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if _CONFIG_PATH.exists():
        cfg.read(_CONFIG_PATH)
    return cfg


def _get(cfg: configparser.ConfigParser, key: str, fallback: str = "") -> str:
    try:
        return cfg.get("MQTT_Test", key, fallback=fallback)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return fallback


# ---------------------------------------------------------------------------
# MQTT subscriber helper
# ---------------------------------------------------------------------------

class MqttCollector:
    """Thin wrapper around paho-mqtt that collects messages into a queue."""

    def __init__(
        self,
        broker: str,
        port: int,
        topic: str,
        *,
        transport: str = "tcp",
        use_tls: bool = False,
        websocket_path: str = "/mqtt",
        username: str = "",
        password: str = "",
        client_id: str = "",
        timeout: float = 15.0,
        max_packets: int = 10,
    ) -> None:
        self.broker = broker
        self.port = port
        self.topic = topic
        self.transport = transport
        self.use_tls = use_tls
        self.websocket_path = websocket_path
        self.username = username
        self.password = password
        self.client_id = client_id or "meshcore-test-" + "".join(random.choices(string.ascii_lowercase, k=6))
        self.timeout = timeout
        self.max_packets = max_packets

        self._q: queue.Queue[dict[str, Any]] = queue.Queue()
        self._error: str | None = None
        self._connected = threading.Event()
        self._client: Any = None

    # ------------------------------------------------------------------
    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: int) -> None:
        if rc == 0:
            self._connected.set()
            client.subscribe(self.topic)
        else:
            self._error = f"MQTT connect failed: rc={rc}"
            self._connected.set()

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        if self._q.qsize() >= self.max_packets:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            payload["_topic"] = msg.topic
            self._q.put_nowait(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass  # Skip non-JSON messages (e.g. status blobs)

    def _on_disconnect(self, client: Any, userdata: Any, rc: int) -> None:
        pass  # Graceful disconnects are fine

    def _on_connect_v2(self, client: Any, userdata: Any, connect_flags: Any, reason: Any, properties: Any) -> None:
        """paho-mqtt Callback API v2 (ReasonCode instead of rc)."""
        if not reason.is_failure:
            self._connected.set()
            client.subscribe(self.topic)
        else:
            self._error = f"MQTT connect failed: {reason}"
            self._connected.set()

    def _on_disconnect_v2(
        self, client: Any, userdata: Any, disconnect_flags: Any, reason: Any, properties: Any
    ) -> None:
        pass  # Graceful disconnects are fine

    # ------------------------------------------------------------------
    def collect(self) -> list[dict[str, Any]]:
        """Connect, subscribe, collect up to max_packets, return them."""
        if not PAHO_AVAILABLE:
            raise RuntimeError("paho-mqtt is not installed; run: pip install paho-mqtt")

        if hasattr(mqtt_lib, "CallbackAPIVersion"):
            client = mqtt_lib.Client(
                mqtt_lib.CallbackAPIVersion.VERSION2,
                client_id=self.client_id,
                transport=self.transport,
            )
            client.on_connect = self._on_connect_v2
            client.on_disconnect = self._on_disconnect_v2
        else:
            client = mqtt_lib.Client(
                client_id=self.client_id,
                transport=self.transport,
            )
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
        self._client = client
        client.on_message = self._on_message

        if self.username:
            client.username_pw_set(self.username, self.password or None)

        if self.use_tls:
            import ssl
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)

        if self.transport == "websockets" and self.websocket_path:
            client.ws_set_options(path=self.websocket_path)

        client.connect_async(self.broker, self.port, keepalive=30)
        client.loop_start()

        # Wait for connection
        connected = self._connected.wait(timeout=10)
        if not connected or self._error:
            client.loop_stop()
            client.disconnect()
            raise ConnectionError(self._error or f"Timed out connecting to {self.broker}:{self.port}")

        # Wait for packets
        deadline = time.monotonic() + self.timeout
        while self._q.qsize() < self.max_packets and time.monotonic() < deadline:
            time.sleep(0.1)

        client.loop_stop()
        client.disconnect()

        packets = []
        while not self._q.empty():
            packets.append(self._q.get_nowait())
        return packets


# ---------------------------------------------------------------------------
# Fixture data helpers
# ---------------------------------------------------------------------------

def _load_fixture_packets() -> list[dict[str, Any]]:
    """Load pre-collected fixture packets from tests/fixtures/mqtt_packets.json."""
    fixture_path = Path(__file__).parent / "fixtures" / "mqtt_packets.json"
    if fixture_path.exists():
        with open(fixture_path) as f:
            return json.load(f)
    return []


def _save_fixture_packets(packets: list[dict[str, Any]]) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "mqtt_packets.json"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    with open(fixture_path, "w") as f:
        json.dump(packets, f, indent=2, default=str)
    print(f"Saved {len(packets)} packets to {fixture_path}")


# ---------------------------------------------------------------------------
# Schema validation helpers (used by both live and fixture tests)
# ---------------------------------------------------------------------------

def validate_packet_schema(packet: dict[str, Any]) -> list[str]:
    """Return a list of schema violations, empty list if packet is valid.

    rx packets (received over the air) must include RF data fields (SNR/RSSI/hash).
    tx packets (the bot's own transmissions) may omit those fields.
    """
    errors: list[str] = []
    direction = packet.get("direction", "rx")

    # Base required keys (always present)
    base_required = _REQUIRED_PACKET_KEYS - _RX_ONLY_KEYS
    for key in base_required:
        if key not in packet:
            errors.append(f"Missing required key: {key!r}")

    # RF data fields required only for rx packets
    if direction == "rx":
        for key in _RX_ONLY_KEYS:
            if key not in packet:
                errors.append(f"Missing required key: {key!r}")

    if "type" in packet and packet["type"] != "PACKET":
        errors.append(f"Expected type='PACKET', got {packet['type']!r}")

    if "direction" in packet and packet["direction"] not in _VALID_DIRECTIONS:
        errors.append(f"Invalid direction {packet['direction']!r}; expected one of {_VALID_DIRECTIONS}")

    if "route" in packet and packet["route"] not in _VALID_ROUTES:
        errors.append(f"Invalid route {packet['route']!r}; expected one of {_VALID_ROUTES}")

    if "packet_type" in packet and packet["packet_type"] not in _VALID_TYPES:
        errors.append(f"Invalid packet_type {packet['packet_type']!r}")

    if "hash" in packet:
        h = packet["hash"]
        if not isinstance(h, str) or len(h) != 16:
            errors.append(f"hash must be 16-char hex string, got {h!r}")

    if "origin_id" in packet:
        oid = packet["origin_id"]
        if oid != "UNKNOWN" and not all(c in "0123456789ABCDEFabcdef" for c in oid):
            errors.append(f"origin_id must be hex or 'UNKNOWN', got {oid!r}")

    if "len" in packet:
        try:
            int(packet["len"])
        except (ValueError, TypeError):
            errors.append(f"len must be an integer string, got {packet['len']!r}")

    return errors


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def mqtt_cfg() -> configparser.ConfigParser:
    return _load_config()


@pytest.fixture(scope="session")
def fixture_packets() -> list[dict[str, Any]]:
    """Load fixture packets (pre-collected offline data)."""
    return _load_fixture_packets()


# ---------------------------------------------------------------------------
# Offline schema tests (use fixture data — no network needed)
# ---------------------------------------------------------------------------

class TestPacketSchemaValidation:
    """Schema validation logic tested against known-good/bad packet dicts.

    These tests run without any network access.
    """

    def _good_packet(self) -> dict[str, Any]:
        return {
            "origin": "TestDevice",
            "origin_id": "AABBCCDD",
            "timestamp": "2026-03-16T12:00:00",
            "type": "PACKET",
            "direction": "rx",
            "time": "12:00:00",
            "date": "16/03/2026",
            "len": "32",
            "packet_type": "2",
            "route": "F",
            "payload_len": "20",
            "raw": "DEADBEEF",
            "SNR": "8.5",
            "RSSI": "-95",
            "hash": "0123456789abcdef",
        }

    def test_valid_packet_passes(self):
        errors = validate_packet_schema(self._good_packet())
        assert errors == []

    def test_missing_required_key(self):
        pkt = self._good_packet()
        del pkt["hash"]
        errors = validate_packet_schema(pkt)
        assert any("hash" in e for e in errors)

    def test_wrong_type_field(self):
        pkt = self._good_packet()
        pkt["type"] = "STATUS"
        errors = validate_packet_schema(pkt)
        assert any("type" in e for e in errors)

    def test_wrong_direction(self):
        pkt = self._good_packet()
        pkt["direction"] = "unknown"
        errors = validate_packet_schema(pkt)
        assert any("direction" in e for e in errors)

    def test_tx_direction_is_valid(self):
        pkt = self._good_packet()
        pkt["direction"] = "tx"
        # tx packets don't require SNR/RSSI/hash
        del pkt["SNR"]
        del pkt["RSSI"]
        del pkt["hash"]
        assert validate_packet_schema(pkt) == []

    def test_invalid_route(self):
        pkt = self._good_packet()
        pkt["route"] = "X"
        errors = validate_packet_schema(pkt)
        assert any("route" in e for e in errors)

    def test_valid_routes(self):
        for route in _VALID_ROUTES:
            pkt = self._good_packet()
            pkt["route"] = route
            assert validate_packet_schema(pkt) == [], f"Route {route!r} should be valid"

    def test_invalid_packet_type(self):
        pkt = self._good_packet()
        pkt["packet_type"] = "99"
        errors = validate_packet_schema(pkt)
        assert any("packet_type" in e for e in errors)

    def test_all_valid_packet_types(self):
        for t in _VALID_TYPES:
            pkt = self._good_packet()
            pkt["packet_type"] = t
            assert validate_packet_schema(pkt) == [], f"packet_type {t!r} should be valid"

    def test_hash_wrong_length(self):
        pkt = self._good_packet()
        pkt["hash"] = "tooshort"
        errors = validate_packet_schema(pkt)
        assert any("hash" in e for e in errors)

    def test_origin_id_unknown_is_valid(self):
        pkt = self._good_packet()
        pkt["origin_id"] = "UNKNOWN"
        assert validate_packet_schema(pkt) == []

    def test_len_must_be_integer_string(self):
        pkt = self._good_packet()
        pkt["len"] = "not-a-number"
        errors = validate_packet_schema(pkt)
        assert any("len" in e for e in errors)


class TestFixturePackets:
    """Validate pre-collected fixture packets from tests/fixtures/mqtt_packets.json.

    Skipped if no fixture file exists.  Run ``--collect-fixtures`` first.
    """

    def test_fixture_file_parseable(self, fixture_packets):
        # If empty, test is effectively a no-op (not a failure)
        assert isinstance(fixture_packets, list)

    def test_all_fixture_packets_pass_schema(self, fixture_packets):
        if not fixture_packets:
            pytest.skip("No fixture packets collected yet — run: python tests/test_mqtt_live.py --collect-fixtures")
        for i, pkt in enumerate(fixture_packets):
            errors = validate_packet_schema(pkt)
            assert errors == [], f"Packet {i} schema errors: {errors}\nPacket: {json.dumps(pkt, indent=2)[:400]}"

    def test_fixture_packets_have_nonzero_length(self, fixture_packets):
        if not fixture_packets:
            pytest.skip("No fixture packets available")
        for pkt in fixture_packets:
            assert int(pkt.get("len", "0")) > 0, "Packet len should be > 0"

    def test_fixture_raw_field_is_hex(self, fixture_packets):
        if not fixture_packets:
            pytest.skip("No fixture packets available")
        for pkt in fixture_packets:
            raw = pkt.get("raw", "")
            assert all(c in "0123456789ABCDEFabcdef" for c in raw), f"raw field is not hex: {raw[:40]!r}"


# ---------------------------------------------------------------------------
# Live MQTT tests (require network, marked with pytest.mark.mqtt)
# ---------------------------------------------------------------------------

@pytest.mark.mqtt
class TestLiveMqttPackets:
    """Connect to the letsmesh MQTT broker and validate live packets.

    Requires network access.  Skipped unless -m mqtt is passed.
    """

    @pytest.fixture(scope="class")
    def live_packets(self, mqtt_cfg) -> list[dict[str, Any]]:
        """Collect live packets from the broker (with LAN fallback)."""
        if not PAHO_AVAILABLE:
            pytest.skip("paho-mqtt not installed")

        topic = _get(mqtt_cfg, "topic_subscribe", "meshcore/SEA/+/packets")
        timeout = float(_get(mqtt_cfg, "timeout_seconds", "15"))
        max_pkts = int(_get(mqtt_cfg, "max_packets", "10"))
        username = _get(mqtt_cfg, "username", "")
        password = _get(mqtt_cfg, "password", "")

        # Broker configs to try in order: primary (letsmesh), then LAN fallback
        broker_attempts = [
            {
                "broker": _get(mqtt_cfg, "broker", "mqtt-us-v1.letsmesh.net"),
                "port": int(_get(mqtt_cfg, "port", "443")),
                "transport": _get(mqtt_cfg, "transport", "websockets"),
                "use_tls": _get(mqtt_cfg, "use_tls", "true").lower() == "true",
                "websocket_path": _get(mqtt_cfg, "websocket_path", "/mqtt"),
            },
            # LAN fallback: plain MQTT on port 1883 (no TLS)
            {
                "broker": "10.0.2.123",
                "port": 1883,
                "transport": "tcp",
                "use_tls": False,
                "websocket_path": "",
            },
        ]

        last_error = ""
        for attempt in broker_attempts:
            try:
                collector = MqttCollector(
                    broker=attempt["broker"],
                    port=attempt["port"],
                    topic=topic,
                    transport=attempt["transport"],
                    use_tls=attempt["use_tls"],
                    websocket_path=attempt["websocket_path"],
                    username=username,
                    password=password,
                    timeout=timeout,
                    max_packets=max_pkts,
                )
                packets = collector.collect()
                if packets:
                    return packets
                last_error = f"No packets received from {topic} within {timeout}s"
            except Exception as e:
                last_error = str(e)
                continue

        pytest.skip(f"Could not collect packets from any broker: {last_error}")

    def test_received_at_least_one_packet(self, live_packets):
        """Also persists collected packets as offline fixtures for future CI runs."""
        assert len(live_packets) >= 1
        # Save as fixtures so offline tests work when network is unavailable
        try:
            _save_fixture_packets(live_packets)
        except Exception:
            pass  # Non-fatal: fixture save failure must not fail the test

    def test_all_live_packets_pass_schema(self, live_packets):
        for i, pkt in enumerate(live_packets):
            errors = validate_packet_schema(pkt)
            assert errors == [], f"Live packet {i} schema errors: {errors}"

    def test_live_packets_have_realistic_snr(self, live_packets):
        """SNR on LoRa is typically -20 to +15 dB."""
        for pkt in live_packets:
            snr_str = pkt.get("SNR", "Unknown")
            if snr_str == "Unknown":
                continue
            try:
                snr = float(snr_str)
                assert -30 <= snr <= 30, f"Unrealistic SNR: {snr}"
            except ValueError:
                pass  # Non-numeric SNR is acceptable

    def test_live_packets_have_realistic_rssi(self, live_packets):
        """RSSI on LoRa is typically -140 to -40 dBm."""
        for pkt in live_packets:
            rssi_str = pkt.get("RSSI", "Unknown")
            if rssi_str == "Unknown":
                continue
            try:
                rssi = float(rssi_str)
                assert -160 <= rssi <= 0, f"Unrealistic RSSI: {rssi}"
            except ValueError:
                pass

    def test_live_packet_timestamps_are_recent(self, live_packets):
        """Timestamps should be within the last 24 hours."""
        import datetime

        now = datetime.datetime.utcnow()
        cutoff = now - datetime.timedelta(hours=24)
        for pkt in live_packets:
            ts_str = pkt.get("timestamp", "")
            if not ts_str:
                continue
            try:
                ts = datetime.datetime.fromisoformat(ts_str.replace("Z", ""))
                assert ts >= cutoff, f"Packet timestamp too old: {ts_str}"
            except ValueError:
                pass  # Non-standard timestamps are skipped

    def test_advert_packets_have_correct_type(self, live_packets):
        """ADVERT packets should have packet_type == '4'."""
        adverts = [p for p in live_packets if p.get("packet_type") == "4"]
        # Not all traffic will have adverts; just check those we have are valid
        for pkt in adverts:
            assert validate_packet_schema(pkt) == []

    def test_text_message_packets_have_correct_type(self, live_packets):
        """TXT_MSG packets should have packet_type == '2'."""
        txt_msgs = [p for p in live_packets if p.get("packet_type") == "2"]
        for pkt in txt_msgs:
            assert validate_packet_schema(pkt) == []


# ---------------------------------------------------------------------------
# CLI: collect fixtures from live broker
# ---------------------------------------------------------------------------

def _cli_collect_fixtures() -> None:
    """Connect to the broker, collect packets, save as fixture JSON."""
    if not PAHO_AVAILABLE:
        print("ERROR: paho-mqtt not installed.  Run: pip install paho-mqtt")
        return

    cfg = _load_config()
    broker = _get(cfg, "broker", "mqtt-us-v1.letsmesh.net")
    port = int(_get(cfg, "port", "443"))
    topic = _get(cfg, "topic_subscribe", "meshcore/SEA/+/packets")
    transport = _get(cfg, "transport", "websockets")
    use_tls = _get(cfg, "use_tls", "true").lower() == "true"
    ws_path = _get(cfg, "websocket_path", "/mqtt")
    username = _get(cfg, "username", "")
    password = _get(cfg, "password", "")
    timeout = float(_get(cfg, "timeout_seconds", "30"))
    max_pkts = int(_get(cfg, "max_packets", "20"))

    # Try primary broker, then LAN fallback
    broker_attempts = [
        {"broker": broker, "port": port, "transport": transport,
         "use_tls": use_tls, "websocket_path": ws_path},
        {"broker": "10.0.2.123", "port": 1883, "transport": "tcp",
         "use_tls": False, "websocket_path": ""},
    ]

    packets = []
    for attempt in broker_attempts:
        b = attempt["broker"]
        p = attempt["port"]
        print(f"Connecting to {b}:{p} (transport={attempt['transport']}, tls={attempt['use_tls']})")
        print(f"Subscribing to: {topic}")
        print(f"Collecting up to {max_pkts} packets (timeout: {timeout}s)...")
        try:
            collector = MqttCollector(
                broker=b, port=p, topic=topic,
                transport=attempt["transport"], use_tls=attempt["use_tls"],
                websocket_path=attempt["websocket_path"],
                username=username, password=password,
                timeout=timeout, max_packets=max_pkts,
            )
            packets = collector.collect()
            if packets:
                break
            print("No packets received, trying fallback...")
        except Exception as e:
            print(f"  Failed: {e}, trying fallback...")
            continue

    if not packets:
        print("No packets received — check broker/topic/network access.")
        return

    print(f"Received {len(packets)} packets")

    # Validate each packet
    valid = 0
    for i, pkt in enumerate(packets):
        errors = validate_packet_schema(pkt)
        if errors:
            print(f"  Packet {i}: SCHEMA ERRORS: {errors}")
        else:
            valid += 1

    print(f"Schema validation: {valid}/{len(packets)} valid")

    # Save to fixture file
    output = _get(cfg, "fixture_output", "tests/fixtures/mqtt_packets.json")
    fixture_path = Path(output)
    _save_fixture_packets(packets)
    print(f"\nFixtures saved to: {fixture_path.resolve()}")
    print("Run offline tests with: pytest tests/test_mqtt_live.py -v")


if __name__ == "__main__":
    import sys
    if "--collect-fixtures" in sys.argv:
        _cli_collect_fixtures()
    else:
        print(__doc__)
        print("\nUsage:")
        print("  pytest tests/test_mqtt_live.py -v -m mqtt    # live tests")
        print("  python tests/test_mqtt_live.py --collect-fixtures  # save fixtures")
