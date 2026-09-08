"""Neighbors wiring inside PacketCaptureService: config, gating, publish, cycle."""

from __future__ import annotations

import asyncio
import configparser
import contextlib
import json
import logging
import sqlite3
import time
import types
from unittest.mock import MagicMock

import pytest
from meshcore import EventType

from modules import neighbors_discovery as nb
from modules.db_migrations import MigrationRunner
from modules.service_plugins.packet_capture_service import (
    NEIGHBORS_ATTEMPT_STATE_KEY,
    NEIGHBORS_RETRY_BACKOFF_SECONDS,
    NEIGHBORS_STATE_KEY,
    PacketCaptureService,
)

LOGGER = logging.getLogger("test-pc-neighbors")

SELF_KEY = "ff" * 32
KEY_A = "aa" * 32
KEY_B = "bb" * 32


class FakeEvent:
    def __init__(self, event_type, payload=None):
        self.type = event_type
        self.payload = payload or {}


class FakeRadio:
    """Minimal radio that answers a discover request with canned responses."""

    def __init__(self, responses=None, *, commands=("send_node_discover_req",)):
        self.responses = responses or []
        self.handler = None
        self.discover_requests = 0
        self.self_info = {"public_key": SELF_KEY}
        self.contacts = {}
        available = {}
        if "send_node_discover_req" in commands:
            available["send_node_discover_req"] = self._send
        if "req_regions_sync" in commands:
            available["req_regions_sync"] = self._regions
        self.commands = types.SimpleNamespace(**available)

    def subscribe(self, event_type, callback):
        self.handler = callback
        return "sub"

    def unsubscribe(self, subscription):
        return None

    async def _send(self, filter_bits, prefix_only=True, tag=None):
        self.discover_requests += 1
        little_endian = tag.to_bytes(4, "little").hex()
        for item in self.responses:
            payload = dict(item)
            payload.setdefault("tag", little_endian)
            await self.handler(FakeEvent(EventType.DISCOVER_RESPONSE, payload))
        return FakeEvent(EventType.MSG_SENT, {})

    async def _regions(self, pubkey, timeout=0, min_timeout=0):
        return "DEN"


class FakeMqttClient:
    def __init__(self, rc=0):
        self.published = []
        self._rc = rc

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append({"topic": topic, "payload": payload,
                               "qos": qos, "retain": retain})
        return types.SimpleNamespace(rc=self._rc)


def response(pubkey, snr=5.0, node_type=2):
    return {"pubkey": pubkey, "SNR": snr, "node_type": node_type}


@pytest.fixture
def db_manager(tmp_path):
    """Real migrated database plus in-memory bot_metadata."""
    path = tmp_path / "bot.db"
    conn = sqlite3.connect(path)
    MigrationRunner(conn, LOGGER).run()
    conn.close()

    class Manager:
        def __init__(self):
            self.metadata = {}

        @contextlib.contextmanager
        def connection(self):
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

        def get_metadata(self, key):
            return self.metadata.get(key)

        def set_metadata(self, key, value):
            self.metadata[key] = value

    return Manager()


def build_service(ini: str, *, db_manager=None, radio=None, connected=True):
    """Construct the service without running __init__.

    __init__ installs logging handlers and opens files; the project's other
    packet-capture unit tests use this same idiom.
    """
    config = configparser.ConfigParser()
    config.read_string(ini.strip())

    bot = MagicMock()
    bot.config = config
    bot.logger = LOGGER
    bot.connected = connected
    bot.meshcore = radio
    if db_manager is not None:
        bot.db_manager = db_manager
    else:
        bot.db_manager.get_metadata.return_value = None

    service = object.__new__(PacketCaptureService)
    service.bot = bot
    service.logger = LOGGER
    service._load_config()
    # Runtime state __init__ sets up alongside _load_config().
    service.neighbors_task = None
    service.neighbors_capability_state = None
    service.neighbors_discover_failures = 0
    service.neighbors_topic_warned = set()
    service.last_neighbors_publish = service._load_neighbors_state()
    service.last_neighbors_attempt = service._load_neighbors_attempt_state()
    service.neighbors_cycle_active = False
    service.debug = False
    # global_iata deliberately not overridden: it comes from the ini, so tests can
    # exercise both a real IATA and the unset sentinel.
    service.mqtt_clients = []
    service.background_tasks = []
    service.should_exit = False
    service._get_bot_name = lambda: "TestBot"
    return service


BASE_INI = """
[PacketCapture]
enabled = true
neighbors_enabled = true
"""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_neighbors_defaults_to_disabled():
    service = build_service("[PacketCapture]\nenabled = true\n")
    assert service.neighbors_enabled is False
    # Scope collection is separately off, for radio-lock and contact-path reasons.
    assert service.neighbors_config.collect_scopes is False
    # Graph feeding is on: the data is only useful if something consumes it.
    assert service.neighbors_feed_mesh_graph is True


def test_neighbors_config_reads_every_key():
    service = build_service("""
[PacketCapture]
enabled = true
neighbors_enabled = true
neighbors_interval_hours = 48
neighbors_discover_window = 30
neighbors_command_timeout = 11
neighbors_collect_scopes = true
neighbors_scope_timeout = 4
neighbors_scope_min_timeout = 6
neighbors_scope_gap = 1.5
neighbors_cycle_timeout = 300
neighbors_max = 4
neighbors_self_scopes = DEN,APRS
neighbors_feed_mesh_graph = false
""")
    cfg = service.neighbors_config
    assert (cfg.interval_hours, cfg.discover_window, cfg.command_timeout) == (48, 30.0, 11.0)
    assert cfg.collect_scopes is True
    assert (cfg.scope_timeout, cfg.scope_min_timeout, cfg.scope_gap) == (4.0, 6.0, 1.5)
    assert (cfg.cycle_timeout, cfg.max_neighbors) == (300.0, 4)
    assert cfg.self_scopes == "DEN,APRS"
    assert service.neighbors_feed_mesh_graph is False


def test_out_of_range_interval_is_clamped_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        service = build_service(BASE_INI + "neighbors_interval_hours = 3\n")
    assert service.neighbors_config.interval_hours == nb.MIN_INTERVAL_HOURS
    assert "outside the supported" in caplog.text


def test_in_range_interval_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        service = build_service(BASE_INI + "neighbors_interval_hours = 24\n")
    assert service.neighbors_config.interval_hours == 24
    assert "outside the supported" not in caplog.text


def test_disabled_feature_does_not_warn_about_the_interval(caplog):
    """A stale value in a disabled section is not worth a startup warning."""
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        build_service("[PacketCapture]\nenabled = true\nneighbors_interval_hours = 1\n")
    assert "outside the supported" not in caplog.text


# ---------------------------------------------------------------------------
# Per-broker opt-in and topic resolution
# ---------------------------------------------------------------------------

BROKER_INI = BASE_INI + """
iata = SEA
mqtt1_enabled = true
mqtt1_server = one.example.com
mqtt2_enabled = true
mqtt2_server = two.example.com
mqtt2_neighbors = false
mqtt3_enabled = true
mqtt3_server = three.example.com
mqtt3_topic_neighbors = custom/{IATA}/{PUBLIC_KEY}/nbrs
"""


def test_broker_neighbors_flag_defaults_on():
    """neighbors_enabled is the single switch; brokers opt out, not in."""
    service = build_service(BROKER_INI)
    flags = {b["broker_num"]: b["neighbors"] for b in service.mqtt_brokers}
    assert flags == {1: True, 2: False, 3: True}


def test_neighbors_topic_derives_from_the_broker_prefix():
    service = build_service(BROKER_INI)
    broker = service.mqtt_brokers[0]
    # Mirrors how topic_packets falls back to <prefix>/packet.
    assert service._resolve_neighbors_topic(broker) == "meshcore/packets/neighbors"


def test_neighbors_topic_follows_a_templated_packets_topic():
    """Brokers publish by default, so the derived topic has to be *right*.

    A broker configured with meshcore/{IATA}/{PUBLIC_KEY}/packets should get the
    matching neighbors topic, not an unrelated flat one.
    """
    service = build_service(BASE_INI + """
iata = SEA
mqtt1_enabled = true
mqtt1_server = one.example.com
mqtt1_topic_packets = meshcore/{IATA}/{PUBLIC_KEY}/packets
""", radio=FakeRadio())
    broker = service.mqtt_brokers[0]
    assert service._neighbors_topic_template(broker) == "meshcore/{IATA}/{PUBLIC_KEY}/neighbors"
    assert service._resolve_neighbors_topic(broker) == (
        f"meshcore/SEA/{SELF_KEY.upper()}/neighbors"
    )


def test_explicit_topic_wins_over_the_packets_topic():
    service = build_service(BASE_INI + """
iata = SEA
mqtt1_enabled = true
mqtt1_server = one.example.com
mqtt1_topic_packets = meshcore/{IATA}/{PUBLIC_KEY}/packets
mqtt1_topic_neighbors = my/own/topic
""", radio=FakeRadio())
    assert service._resolve_neighbors_topic(service.mqtt_brokers[0]) == "my/own/topic"


def test_location_routed_topic_is_refused_without_an_iata():
    """meshcore/XYZ/... would pollute a shared namespace on a community broker."""
    service = build_service(BASE_INI + """
mqtt1_enabled = true
mqtt1_server = one.example.com
mqtt1_topic_packets = meshcore/{IATA}/{PUBLIC_KEY}/packets
""", radio=FakeRadio())
    assert service.global_iata == "xyz"
    assert service._iata_is_unset() is True
    assert service._resolve_neighbors_topic(service.mqtt_brokers[0]) is None


def test_location_routed_topic_is_refused_with_an_explicitly_empty_iata():
    """An empty configured value is just as unset as the XYZ fallback."""
    service = build_service(BASE_INI + """
iata =
mqtt1_enabled = true
mqtt1_server = one.example.com
mqtt1_topic_packets = meshcore/{IATA}/{PUBLIC_KEY}/packets
""", radio=FakeRadio())
    # Blank stays blank for packet/status topic resolution; neighbors still
    # treats it as unset and refuses location-routed publish.
    assert service.global_iata == ""
    assert service._iata_is_unset() is True
    assert service._resolve_neighbors_topic(service.mqtt_brokers[0]) is None


def test_empty_iata_keeps_historical_packet_topic_resolution():
    """Empty must not become XYZ for packets — that would publish into XYZ."""
    service = build_service(BASE_INI + """
iata =
mqtt1_enabled = true
mqtt1_server = one.example.com
mqtt1_topic_packets = meshcore/{IATA}/{PUBLIC_KEY}/packets
""", radio=FakeRadio())
    topic = service._resolve_topic_template(
        "meshcore/{IATA}/{PUBLIC_KEY}/packets", "packet"
    )
    assert topic == f"meshcore//{SELF_KEY.upper()}/packets"


def test_a_flat_derived_topic_still_works_without_an_iata():
    """The guard is about location routing, not about having an IATA at all."""
    service = build_service(BASE_INI + """
mqtt1_enabled = true
mqtt1_server = one.example.com
""")
    assert service.global_iata == "xyz"
    assert service._resolve_neighbors_topic(service.mqtt_brokers[0]) == "meshcore/packets/neighbors"


def test_neighbors_topic_follows_a_flat_packets_topic():
    service = build_service(BASE_INI + """
iata = SEA
mqtt1_enabled = true
mqtt1_server = one.example.com
mqtt1_topic_packets = packets
""")
    broker = service.mqtt_brokers[0]
    assert service._neighbors_topic_template(broker) == "neighbors"
    assert service._resolve_neighbors_topic(broker) == "neighbors"


def test_an_explicit_topic_is_honoured_even_without_an_iata():
    """An operator naming the topic themselves has made the call."""
    service = build_service(BASE_INI + """
mqtt1_enabled = true
mqtt1_server = one.example.com
mqtt1_topic_neighbors = my/{IATA}/nbrs
""", radio=FakeRadio())
    assert service._resolve_neighbors_topic(service.mqtt_brokers[0]) == "my/XYZ/nbrs"


def test_explicit_neighbors_topic_resolves_placeholders():
    service = build_service(BROKER_INI, radio=FakeRadio())
    topic = service._resolve_neighbors_topic(service.mqtt_brokers[2])
    assert topic == f"custom/SEA/{SELF_KEY.upper()}/nbrs"


def test_unroutable_broker_yields_no_topic():
    service = build_service(BROKER_INI)
    assert service._resolve_neighbors_topic({"topic_prefix": None, "topic_neighbors": None}) is None


def test_neighbors_brokers_requires_connected_and_opted_in():
    service = build_service(BROKER_INI)
    service.mqtt_clients = [
        {"client": FakeMqttClient(), "config": service.mqtt_brokers[0], "connected": True},
        {"client": FakeMqttClient(), "config": service.mqtt_brokers[1], "connected": True},
        {"client": FakeMqttClient(), "config": service.mqtt_brokers[2], "connected": False},
    ]
    assert [i["config"]["broker_num"] for i in service.neighbors_brokers()] == [1]


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def test_publish_is_non_retained_and_only_to_opted_in_brokers():
    service = build_service(BROKER_INI)
    service.mqtt_enabled = True
    opted_in, not_opted_in = FakeMqttClient(), FakeMqttClient()
    service.mqtt_clients = [
        {"client": opted_in, "config": service.mqtt_brokers[0], "connected": True},
        {"client": not_opted_in, "config": service.mqtt_brokers[1], "connected": True},
    ]
    metrics = service.publish_neighbors_mqtt({"neighbors": []})

    assert metrics == {"attempted": 1, "succeeded": 1}
    assert not_opted_in.published == []
    assert len(opted_in.published) == 1
    # A snapshot taken every 12-336h is not a useful last-will value.
    assert opted_in.published[0]["retain"] is False
    assert opted_in.published[0]["qos"] == 0


def test_publish_warns_once_for_an_unroutable_broker(caplog):
    service = build_service(BROKER_INI)
    service.mqtt_enabled = True
    broker = dict(service.mqtt_brokers[0])
    broker["topic_prefix"] = None
    broker["topic_neighbors"] = None
    service.mqtt_clients = [{"client": FakeMqttClient(), "config": broker, "connected": True}]

    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        first = service.publish_neighbors_mqtt({"neighbors": []})
        second = service.publish_neighbors_mqtt({"neighbors": []})

    assert first == {"attempted": 0, "succeeded": 0}
    assert second == {"attempted": 0, "succeeded": 0}
    # Warned once, not on every cycle forever.
    assert caplog.text.count("no neighbors topic could be resolved") == 1


def test_publish_warning_names_a_missing_iata_as_the_cause(caplog):
    """The generic "set a topic prefix" advice would be misleading here."""
    service = build_service(BASE_INI + """
mqtt1_enabled = true
mqtt1_server = one.example.com
mqtt1_topic_packets = meshcore/{IATA}/{PUBLIC_KEY}/packets
""", radio=FakeRadio())
    service.mqtt_enabled = True
    service.mqtt_clients = [
        {"client": FakeMqttClient(), "config": service.mqtt_brokers[0], "connected": True}
    ]
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        assert service.publish_neighbors_mqtt({"neighbors": []}) == {
            "attempted": 0, "succeeded": 0
        }
    assert "no IATA is set" in caplog.text


def test_publish_counts_a_broker_failure():
    service = build_service(BROKER_INI)
    service.mqtt_enabled = True
    service.mqtt_clients = [
        {"client": FakeMqttClient(rc=4), "config": service.mqtt_brokers[0], "connected": True}
    ]
    assert service.publish_neighbors_mqtt({"neighbors": []}) == {"attempted": 1, "succeeded": 0}


def test_publish_noops_when_mqtt_is_disabled():
    service = build_service(BROKER_INI)
    service.mqtt_enabled = False
    service.mqtt_clients = [
        {"client": FakeMqttClient(), "config": service.mqtt_brokers[0], "connected": True}
    ]
    assert service.publish_neighbors_mqtt({"neighbors": []}) == {"attempted": 0, "succeeded": 0}


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------

def test_capability_needs_only_discover_when_scopes_are_off():
    service = build_service(BASE_INI, radio=FakeRadio(commands=("send_node_discover_req",)))
    assert service.neighbors_commands_available() is True


def test_capability_needs_regions_when_scopes_are_on():
    service = build_service(
        BASE_INI + "neighbors_collect_scopes = true\n",
        radio=FakeRadio(commands=("send_node_discover_req",)),
    )
    assert service.neighbors_commands_available() is False

    both = build_service(
        BASE_INI + "neighbors_collect_scopes = true\n",
        radio=FakeRadio(commands=("send_node_discover_req", "req_regions_sync")),
    )
    assert both.neighbors_commands_available() is True


def test_capability_false_without_a_radio():
    service = build_service(BASE_INI, radio=None)
    assert service.neighbors_commands_available() is False


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

# _load_neighbors_timestamp rejects anything outside now-400d .. now+300s, so
# both of these have to be relative: an absolute epoch literal drifts out of
# the accepted range and fails on a date nobody picked.
STORED_EPOCH = float(round(time.time()) - 3600)
FAR_FUTURE_EPOCH = round(time.time()) + 10 * 365 * 86400


def test_state_round_trips_through_bot_metadata(db_manager):
    service = build_service(BASE_INI, db_manager=db_manager)
    assert service.last_neighbors_publish == 0.0

    service.last_neighbors_publish = STORED_EPOCH
    service._save_neighbors_state()
    assert db_manager.metadata[NEIGHBORS_STATE_KEY] == str(STORED_EPOCH)

    reloaded = build_service(BASE_INI, db_manager=db_manager)
    assert reloaded.last_neighbors_publish == STORED_EPOCH


def test_attempt_state_round_trips_through_bot_metadata(db_manager):
    service = build_service(BASE_INI, db_manager=db_manager)
    assert service.last_neighbors_attempt == 0.0

    service.last_neighbors_attempt = STORED_EPOCH
    service._save_neighbors_attempt_state()
    assert db_manager.metadata[NEIGHBORS_ATTEMPT_STATE_KEY] == str(STORED_EPOCH)

    reloaded = build_service(BASE_INI, db_manager=db_manager)
    assert reloaded.last_neighbors_attempt == STORED_EPOCH


def test_malformed_state_is_ignored(db_manager):
    db_manager.metadata[NEIGHBORS_STATE_KEY] = "not-a-number"
    assert build_service(BASE_INI, db_manager=db_manager).last_neighbors_publish == 0.0


def test_far_future_state_is_ignored(db_manager):
    """A clock jump forward would otherwise suppress cycles indefinitely."""
    db_manager.metadata[NEIGHBORS_STATE_KEY] = str(FAR_FUTURE_EPOCH)
    assert build_service(BASE_INI, db_manager=db_manager).last_neighbors_publish == 0.0


# ---------------------------------------------------------------------------
# Full cycle
# ---------------------------------------------------------------------------

@pytest.fixture
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(nb.asyncio, "sleep", instant)


async def test_cycle_records_feeds_graph_and_publishes(db_manager, no_sleep):
    radio = FakeRadio([response(KEY_A, 7.5), response(KEY_B, -2.0)])
    service = build_service(BROKER_INI, db_manager=db_manager, radio=radio)
    service.mqtt_enabled = True
    client = FakeMqttClient()
    service.mqtt_clients = [
        {"client": client, "config": service.mqtt_brokers[0], "connected": True}
    ]
    graph_calls = []
    service.bot.mesh_graph.add_edge.side_effect = lambda *a, **k: graph_calls.append((a, k))

    summary = await service.run_neighbors_cycle()

    assert summary["ok"] is True
    assert summary["discovered"] == 2
    assert summary["queried"] == 2
    assert summary["best_snr"] == 7.5
    assert summary["recorded"] == 2
    assert summary["attempted"] == 1
    assert summary["succeeded"] == 1

    # Persisted, both tables.
    with db_manager.connection() as conn:
        links = [dict(r) for r in conn.execute(
            "SELECT neighbor_public_key, best_snr FROM neighbor_links ORDER BY neighbor_public_key"
        )]
        history = conn.execute("SELECT COUNT(*) FROM neighbor_observations").fetchone()[0]
    assert [row["neighbor_public_key"] for row in links] == [KEY_A, KEY_B]
    assert history == 2

    # Graph: both directions per link.
    assert len(graph_calls) == 4
    assert {call[0] for call in graph_calls} == {
        (SELF_KEY[:6], KEY_A[:6]), (KEY_A[:6], SELF_KEY[:6]),
        (SELF_KEY[:6], KEY_B[:6]), (KEY_B[:6], SELF_KEY[:6]),
    }
    # Full public keys, which path-derived edges cannot supply.
    assert all(len(call[1]["from_public_key"]) == 64 for call in graph_calls)

    # Published payload.
    message = json.loads(client.published[0]["payload"])
    assert message["origin"] == "TestBot"
    assert message["origin_id"] == SELF_KEY.upper()
    assert [n["pubkey"] for n in message["neighbors"]] == [KEY_A.upper(), KEY_B.upper()]

    # State stamped.
    assert float(db_manager.metadata[NEIGHBORS_STATE_KEY]) > 0


async def test_cycle_never_touches_the_contact_cache(db_manager, no_sleep):
    """Stage 1 is a broadcast; nothing here may add a contact."""
    radio = FakeRadio([response(KEY_A, 1.0)])
    service = build_service(BASE_INI, db_manager=db_manager, radio=radio)
    service.mqtt_enabled = False
    await service.run_neighbors_cycle()
    assert radio.contacts == {}


async def test_cycle_records_without_any_broker(db_manager, no_sleep):
    """The database is a legitimate consumer on its own."""
    radio = FakeRadio([response(KEY_A, 1.0)])
    service = build_service(BASE_INI, db_manager=db_manager, radio=radio)
    service.mqtt_enabled = False

    summary = await service.run_neighbors_cycle()
    assert summary["ok"] is True
    assert summary["recorded"] == 1
    assert summary["attempted"] == 0
    with db_manager.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM neighbor_links").fetchone()[0] == 1


async def test_cycle_respects_the_max_cap(db_manager, no_sleep):
    radio = FakeRadio([response(KEY_A, 9.0), response(KEY_B, 1.0)])
    service = build_service(BASE_INI + "neighbors_max = 1\n",
                            db_manager=db_manager, radio=radio)
    service.mqtt_enabled = False

    summary = await service.run_neighbors_cycle()
    assert summary["discovered"] == 2
    assert summary["queried"] == 1
    # Kept the strongest, and said so in the payload contract.
    with db_manager.connection() as conn:
        kept = conn.execute("SELECT neighbor_public_key FROM neighbor_links").fetchone()[0]
    assert kept == KEY_A


async def test_cycle_skips_graph_when_disabled(db_manager, no_sleep):
    radio = FakeRadio([response(KEY_A, 1.0)])
    service = build_service(BASE_INI + "neighbors_feed_mesh_graph = false\n",
                            db_manager=db_manager, radio=radio)
    service.mqtt_enabled = False
    await service.run_neighbors_cycle()
    service.bot.mesh_graph.add_edge.assert_not_called()


async def test_cycle_marks_entries_responded_with_scopes_off(db_manager, no_sleep):
    """Reporting a live neighbour as `timeout` would be wrong."""
    radio = FakeRadio([response(KEY_A, 1.0)])
    service = build_service(BASE_INI, db_manager=db_manager, radio=radio)
    service.mqtt_enabled = False
    await service.run_neighbors_cycle()
    with db_manager.connection() as conn:
        row = conn.execute("SELECT last_status, scopes FROM neighbor_links").fetchone()
    assert row["last_status"] == nb.STATUS_RESPONDED
    assert row["scopes"] == ""


@pytest.mark.parametrize("ini,radio,connected,expected", [
    ("[PacketCapture]\nenabled = true\n", FakeRadio(), True, "disabled"),
    (BASE_INI, None, True, "not connected"),
    (BASE_INI, FakeRadio(), False, "not connected"),
    (BASE_INI, FakeRadio(commands=()), True, "does not support"),
])
async def test_cycle_bails_with_a_reason(db_manager, ini, radio, connected, expected):
    service = build_service(ini, db_manager=db_manager, radio=radio, connected=connected)
    service.mqtt_enabled = False
    summary = await service.run_neighbors_cycle()
    assert summary["ok"] is False
    assert expected in summary["reason"]


async def test_cycle_discards_when_the_session_resets_mid_cycle(db_manager, monkeypatch):
    """Recording partial data from a torn-down session would be misleading."""
    radio = FakeRadio([response(KEY_A, 1.0)])
    service = build_service(BASE_INI, db_manager=db_manager, radio=radio)
    service.mqtt_enabled = False

    async def swap_radio(_seconds):
        # Simulate a reconnect replacing the meshcore object mid-window.
        service.bot.meshcore = FakeRadio([])

    monkeypatch.setattr(nb.asyncio, "sleep", swap_radio)
    summary = await service.run_neighbors_cycle()
    assert summary["ok"] is False
    with db_manager.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM neighbor_links").fetchone()[0] == 0


async def test_concurrent_cycles_are_refused(db_manager, monkeypatch):
    """The scheduler and the command trigger independently, so the guard lives here.

    Two overlapping cycles would each collect the other's discover responses and
    spend twice the airtime for no extra information.
    """
    radio = FakeRadio([response(KEY_A, 1.0)])
    service = build_service(BASE_INI, db_manager=db_manager, radio=radio)
    service.mqtt_enabled = False

    second = {}

    async def run_second_cycle_mid_window(_seconds):
        # Stands in for the other trigger firing during the discover window.
        second["summary"] = await service.run_neighbors_cycle()

    monkeypatch.setattr(nb.asyncio, "sleep", run_second_cycle_mid_window)
    first = await service.run_neighbors_cycle()

    assert first["ok"] is True
    assert second["summary"]["ok"] is False
    assert "already running" in second["summary"]["reason"]
    # One discover request, one set of links.
    assert radio.discover_requests == 1
    with db_manager.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM neighbor_links").fetchone()[0] == 1
    # And the guard is released for the next trigger.
    assert service.neighbors_cycle_active is False


def _lost_ack_radio():
    """A radio whose discover broadcast goes out but is never acknowledged.

    The airtime is spent; the host just never learns that it was, which is what
    makes this different from a build that rejects the command outright.
    """
    radio = FakeRadio([])

    async def unacknowledged(filter_bits, prefix_only=True, tag=None):
        radio.discover_requests += 1
        return FakeEvent(EventType.ERROR, {"reason": "no_event_received"})

    radio.commands.send_node_discover_req = unacknowledged
    return radio


async def test_a_failed_cycle_still_records_the_attempt(db_manager, caplog, no_sleep):
    """A lost acknowledgement spends the airtime without completing the cycle.

    last_neighbors_publish stays unset in that case, so callers that ration
    airtime need the attempt stamp or another sender could transmit immediately.
    """
    radio = _lost_ack_radio()
    service = build_service(BASE_INI, db_manager=db_manager, radio=radio)
    service.mqtt_enabled = False

    summary = await service.run_neighbors_cycle()
    assert summary["ok"] is False
    assert service.last_neighbors_publish == 0
    assert service.last_neighbors_attempt > 0
    assert float(db_manager.metadata[NEIGHBORS_ATTEMPT_STATE_KEY]) == pytest.approx(
        service.last_neighbors_attempt
    )


async def test_a_transmitted_failure_remains_on_cooldown_after_restart(
    db_manager, no_sleep
):
    """A watchdog restart must not turn a lost acknowledgement into another burst."""
    first_radio = _lost_ack_radio()
    service = build_service(BASE_INI, db_manager=db_manager, radio=first_radio)
    service.mqtt_enabled = False

    assert (await service.run_neighbors_cycle())["ok"] is False
    assert first_radio.discover_requests == 1

    restarted_radio = _lost_ack_radio()
    restarted = build_service(BASE_INI, db_manager=db_manager, radio=restarted_radio)
    restarted.mqtt_enabled = False
    summary = await restarted.run_neighbors_cycle()

    assert summary["ok"] is False
    assert "may run in" in summary["reason"]
    assert restarted_radio.discover_requests == 0


async def test_a_cycle_that_never_transmits_records_no_attempt(db_manager):
    """Refusing before the radio is touched must not lock out a retry."""
    service = build_service(BASE_INI, db_manager=db_manager, radio=None)
    service.mqtt_enabled = False

    summary = await service.run_neighbors_cycle()
    assert summary["ok"] is False
    assert service.last_neighbors_attempt == 0.0


async def test_a_cycle_inside_the_airtime_cooldown_is_refused(db_manager, no_sleep):
    """The guard belongs to the service, so it covers the scheduler too — not just
    the DM command, which is one caller among several."""
    radio = FakeRadio([response(KEY_A, 1.0)])
    service = build_service(BASE_INI, db_manager=db_manager, radio=radio)
    service.mqtt_enabled = False

    assert (await service.run_neighbors_cycle())["ok"] is True
    assert radio.discover_requests == 1

    summary = await service.run_neighbors_cycle()
    assert summary["ok"] is False
    assert "may run in" in summary["reason"]
    # Refused before touching the radio.
    assert radio.discover_requests == 1


async def test_the_cooldown_covers_a_failure_that_transmitted(db_manager, no_sleep):
    """The failing cycle never stamps last_neighbors_publish, so a guard reading
    only that would let the next trigger transmit immediately."""
    radio = _lost_ack_radio()
    service = build_service(BASE_INI, db_manager=db_manager, radio=radio)
    service.mqtt_enabled = False

    assert (await service.run_neighbors_cycle())["ok"] is False
    assert radio.discover_requests == 1

    assert "may run in" in (await service.run_neighbors_cycle())["reason"]
    assert radio.discover_requests == 1


async def test_the_cooldown_does_not_delay_a_cycle_that_never_transmitted(db_manager):
    """Re-checking a disconnected radio is free, so it must stay quick."""
    service = build_service(BASE_INI, db_manager=db_manager, radio=None)
    service.mqtt_enabled = False

    await service.run_neighbors_cycle()
    assert service.neighbors_cooldown_remaining() == 0.0


def _record_scheduler_waits(service, monkeypatch):
    """Capture what the scheduler waits, and stop it after one pass."""
    waits: list[float] = []

    async def record_wait(timeout):
        waits.append(timeout)
        service.should_exit = True
        return True

    monkeypatch.setattr(service, "_wait_with_shutdown", record_wait)
    return waits


async def test_scheduler_waits_out_the_cooldown_before_retrying(
    db_manager, monkeypatch, no_sleep
):
    """Retrying on the short failure backoff alone would triple the airtime."""
    radio = _lost_ack_radio()
    service = build_service(BASE_INI, db_manager=db_manager, radio=radio)
    service.mqtt_enabled = False

    waits = _record_scheduler_waits(service, monkeypatch)
    await service.neighbors_scheduler()

    assert radio.discover_requests == 1
    assert waits == [pytest.approx(nb.MIN_CYCLE_GAP_SECONDS, abs=5)]
    assert waits[0] > NEIGHBORS_RETRY_BACKOFF_SECONDS


async def test_scheduler_retries_quickly_when_nothing_transmitted(db_manager, monkeypatch):
    """A disconnected radio costs nothing to re-test, so keep the short backoff."""
    service = build_service(BASE_INI, db_manager=db_manager, radio=None)
    service.mqtt_enabled = False

    waits = _record_scheduler_waits(service, monkeypatch)
    await service.neighbors_scheduler()

    assert waits == [NEIGHBORS_RETRY_BACKOFF_SECONDS]


async def test_the_guard_is_released_when_a_cycle_fails(db_manager, no_sleep):
    radio = FakeRadio([])

    async def boom(filter_bits, prefix_only=True, tag=None):
        raise RuntimeError("serial write failed")

    radio.commands.send_node_discover_req = boom
    service = build_service(BASE_INI, db_manager=db_manager, radio=radio)
    service.mqtt_enabled = False

    with contextlib.suppress(RuntimeError):
        await service.run_neighbors_cycle()
    assert service.neighbors_cycle_active is False


async def test_repeated_discover_failure_warns_only_once(db_manager, caplog, no_sleep):
    radio = FakeRadio([])

    async def failing(filter_bits, prefix_only=True, tag=None):
        return FakeEvent(EventType.ERROR, {"reason": "unsupported"})

    radio.commands.send_node_discover_req = failing
    service = build_service(BASE_INI, db_manager=db_manager, radio=radio)
    service.mqtt_enabled = False

    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        for _ in range(3):
            # Stands in for the scheduler waiting out the airtime cooldown between
            # attempts; without it the guard would refuse cycles 2 and 3.
            service.last_neighbors_attempt = 0.0
            assert (await service.run_neighbors_cycle())["ok"] is False

    assert caplog.text.count("discovery request failed; will keep retrying") == 1
    assert service.neighbors_discover_failures == 3


# ---------------------------------------------------------------------------
# Scheduler registration
# ---------------------------------------------------------------------------

async def _drain(service):
    service.should_exit = True
    for task in service.background_tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _quiet_other_tasks(service):
    service.stats_status_enabled = False
    service.stats_refresh_interval = 0
    service.health_check_interval = 0
    service.mqtt_enabled = False


async def test_scheduler_starts_only_when_enabled(db_manager):
    disabled = build_service("[PacketCapture]\nenabled = true\n", db_manager=db_manager)
    _quiet_other_tasks(disabled)
    await disabled.start_background_tasks()
    assert disabled.neighbors_task is None
    assert disabled.background_tasks == []

    enabled = build_service(BASE_INI, db_manager=db_manager)
    _quiet_other_tasks(enabled)
    await enabled.start_background_tasks()
    assert enabled.neighbors_task is not None
    assert enabled.neighbors_task in enabled.background_tasks
    await _drain(enabled)
