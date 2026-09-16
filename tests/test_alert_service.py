"""Tests for the alert service plugin architecture.

Covers:
* BaseAlertService polling loop, dedup, label handling.
* PulsePointAlertService query parsing and agency resolution.
* AlertCommand orchestration: service selection, incident distribution,
  message building.
"""

import configparser
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.service_plugins.base_alert_service import (
    QUERY_TYPE_CITY,
    QUERY_TYPE_COORDINATES,
    BaseAlertService,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_bot(section="PulsePoint_Alert_Service", **overrides):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section(section)
    config.set(section, "enabled", "true")
    for k, v in overrides.items():
        config.set(section, k, v)
    bot.config = config
    store = {}
    bot.db_manager = MagicMock()
    bot.db_manager.get_metadata = Mock(side_effect=lambda k: store.get(k))
    bot.db_manager.set_metadata = Mock(side_effect=lambda k, v: store.update({k: v}))
    bot.command_manager = MagicMock()
    bot.command_manager.send_channel_message = AsyncMock(return_value=True)
    bot._metadata_store = store
    return bot


class _StubAlertService(BaseAlertService):
    """Minimal concrete alert service for exercising base-class behavior."""

    alert_service_id = "stub"
    config_section = "PulsePoint_Alert_Service"  # reuse section for _make_bot

    def __init__(self, bot, incidents=None):
        super().__init__(bot)
        self._incidents = incidents or []

    def get_capabilities(self):
        return {QUERY_TYPE_CITY: True}

    async def query_alerts(self, query):
        return list(self._incidents)

    async def fetch_new_incidents(self):
        return list(self._incidents)


# --------------------------------------------------------------------------- #
# BaseAlertService
# --------------------------------------------------------------------------- #
class TestBaseAlertServiceLabel:
    def test_label_defaults_to_service_id_uppercased(self):
        svc = _StubAlertService(_make_bot())
        assert svc.label == "STUB"

    def test_label_from_config_truncated_to_six(self):
        svc = _StubAlertService(_make_bot(label="verylonglabel"))
        assert svc.label == "verylo"

    def test_prefix_with_label(self):
        svc = _StubAlertService(_make_bot(label="PP"))
        assert svc._prefix_with_label("Fire: Main St") == "[PP]:\nFire: Main St"


class TestBaseAlertServicePolling:
    def test_polling_disabled_by_default(self):
        svc = _StubAlertService(_make_bot())
        assert svc.polling_enabled is False

    def test_polling_interval_floor(self):
        # Even a tiny configured interval is clamped to 30s.
        svc = _StubAlertService(_make_bot(polling_interval="1000"))
        assert svc.polling_interval_seconds == 30.0

    def test_polling_channels_parsed(self):
        svc = _StubAlertService(_make_bot(polling_channels="#alerts, #emergency"))
        assert svc.polling_channels == ["#alerts", "#emergency"]

    @pytest.mark.asyncio
    async def test_post_new_incidents_dedups(self):
        bot = _make_bot(polling_channels="#alerts")
        svc = _StubAlertService(bot, incidents=[("id1", "Fire: A"), ("id2", "Medical: B")])
        svc.polling_channels = ["#alerts"]
        svc.get_mesh_flood_scope = lambda: None

        await svc._post_new_incidents()
        assert bot.command_manager.send_channel_message.await_count == 2

        # Second run: same incidents, nothing new posted.
        bot.command_manager.send_channel_message.reset_mock()
        await svc._post_new_incidents()
        assert bot.command_manager.send_channel_message.await_count == 0

    @pytest.mark.asyncio
    async def test_post_new_incidents_prefixes_label(self):
        bot = _make_bot(label="PP", polling_channels="#alerts")
        svc = _StubAlertService(bot, incidents=[("id1", "Fire: A")])
        svc.polling_channels = ["#alerts"]
        svc.get_mesh_flood_scope = lambda: None

        await svc._post_new_incidents()
        args = bot.command_manager.send_channel_message.await_args
        assert args[0][0] == "#alerts"
        assert args[0][1] == "[PP]:\nFire: A"

    def test_seen_ids_persist_roundtrip(self):
        bot = _make_bot()
        svc = _StubAlertService(bot)
        svc._seen_incident_ids = {"a", "b"}
        svc._save_seen_ids()
        # New instance reloads persisted ids.
        svc2 = _StubAlertService(bot)
        assert svc2._load_seen_ids() == {"a", "b"}


# --------------------------------------------------------------------------- #
# PulsePointAlertService — agency resolution
# --------------------------------------------------------------------------- #
def _pulsepoint(**agency_overrides):
    from modules.service_plugins.pulsepoint_alert_service import PulsePointAlertService
    bot = _make_bot(
        **{
            "agency.city.seattle": "1234",
            "agency.county.king": "5678",
            **agency_overrides,
        }
    )
    return PulsePointAlertService(bot)


class TestPulsePointAgencies:
    def test_city_agency_resolution(self):
        svc = _pulsepoint()
        assert svc._get_agency_ids("seattle", "city") == "1234"

    def test_county_agency_resolution(self):
        svc = _pulsepoint()
        assert svc._get_agency_ids("king", "county") == "5678"

    def test_unknown_city_returns_none(self):
        svc = _pulsepoint()
        assert svc._get_agency_ids("tacoma", "city") is None

    def test_default_combines_all_agencies(self):
        svc = _pulsepoint()
        ids = svc._get_agency_ids()
        assert "1234" in ids and "5678" in ids

    def test_no_agencies_returns_none(self):
        from modules.service_plugins.pulsepoint_alert_service import PulsePointAlertService
        bot = _make_bot()  # no agency.* keys
        svc = PulsePointAlertService(bot)
        assert svc._get_agency_ids() is None

    def test_capabilities(self):
        svc = _pulsepoint()
        caps = svc.get_capabilities()
        assert caps[QUERY_TYPE_CITY] is True
        assert caps[QUERY_TYPE_COORDINATES] is True


# --------------------------------------------------------------------------- #
# AlertCommand — orchestration
# --------------------------------------------------------------------------- #
def _command_bot(services=None, **alert_cfg):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Alert_Command")
    config.set("Alert_Command", "enabled", "true")
    for k, v in alert_cfg.items():
        config.set("Alert_Command", k, v)
    bot.config = config
    bot.services = services or {}
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.translator.get_value = Mock(return_value=None)
    bot.meshcore = None
    return bot


class _FakeService:
    """Stand-in alert service for command-level tests."""

    def __init__(self, service_id, label, incidents, polling_channels=None):
        self.alert_service_id = service_id
        self.label = label
        self.polling_channels = polling_channels or []
        self._incidents = incidents

    async def query_alerts(self, query):
        return list(self._incidents)


def _make_command(bot):
    from modules.commands.alert_command import AlertCommand
    return AlertCommand(bot)


def _msg(content="alert seattle", is_dm=True, channel=None):
    m = MagicMock()
    m.content = content
    m.content_lower = content.lower()
    m.is_dm = is_dm
    m.channel = channel
    m.sender_id = "user1"
    return m


class TestDistribution:
    def test_equal_distribution(self):
        cmd = _make_command(_command_bot())
        # Two services, 5 incidents each, budget 10 -> 5/5.
        assert cmd._distribute_incidents([5, 5], 10) == [5, 5]

    def test_uneven_redistribution(self):
        cmd = _make_command(_command_bot())
        # Service A only has 2; its unused budget flows to B.
        assert cmd._distribute_incidents([2, 20], 10) == [2, 8]

    def test_budget_smaller_than_services(self):
        cmd = _make_command(_command_bot())
        alloc = cmd._distribute_incidents([5, 5, 5], 2)
        assert sum(alloc) == 2

    def test_single_service_gets_full_budget(self):
        cmd = _make_command(_command_bot())
        assert cmd._distribute_incidents([20], 10) == [10]

    def test_all_fit_under_budget(self):
        cmd = _make_command(_command_bot())
        assert cmd._distribute_incidents([2, 3], 10) == [2, 3]


class TestMessageBuilding:
    def test_label_prefix_present(self):
        cmd = _make_command(_command_bot())
        msgs = cmd._build_service_messages("PP", ["Fire: A", "Medical: B"], 2, 130)
        assert msgs[0].startswith("[PP]:")

    def test_more_note_when_truncated(self):
        cmd = _make_command(_command_bot())
        msgs = cmd._build_service_messages("PP", ["a", "b", "c", "d", "e"], 2, 130)
        joined = "\n".join(msgs)
        assert "(3 more)" in joined

    def test_no_more_note_when_all_shown(self):
        cmd = _make_command(_command_bot())
        msgs = cmd._build_service_messages("PP", ["a", "b"], 2, 130)
        joined = "\n".join(msgs)
        assert "more)" not in joined

    def test_respects_max_length(self):
        cmd = _make_command(_command_bot())
        long_lines = [f"Incident number {i} at some long street address" for i in range(6)]
        msgs = cmd._build_service_messages("PP", long_lines, 6, 130)
        assert all(len(m) <= 130 for m in msgs)
        # Every message repeats the header.
        assert all(m.startswith("[PP]:") for m in msgs)


class TestServiceSelection:
    def test_connected_services_from_config(self):
        svc_a = _FakeService("pulsepoint", "PP", [])
        svc_b = _FakeService("other", "OTH", [])
        bot = _command_bot(services={"pulsepointalert": svc_a, "otheralert": svc_b})
        bot.config.set("Alert_Command", "services", "pulsepoint,other")
        cmd = _make_command(bot)
        selected = cmd._get_services_for_message(_msg(is_dm=True))
        assert selected == [svc_a, svc_b]

    def test_channel_specific_overrides_connected(self):
        svc_a = _FakeService("pulsepoint", "PP", [], polling_channels=["#alerts"])
        svc_b = _FakeService("other", "OTH", [])
        bot = _command_bot(services={"a": svc_a, "b": svc_b})
        bot.config.set("Alert_Command", "services", "pulsepoint,other")
        cmd = _make_command(bot)
        # In #alerts, only the polling service answers.
        selected = cmd._get_services_for_message(_msg(is_dm=False, channel="#alerts"))
        assert selected == [svc_a]

    def test_non_polling_channel_uses_connected(self):
        svc_a = _FakeService("pulsepoint", "PP", [], polling_channels=["#alerts"])
        bot = _command_bot(services={"a": svc_a})
        bot.config.set("Alert_Command", "services", "pulsepoint")
        cmd = _make_command(bot)
        selected = cmd._get_services_for_message(_msg(is_dm=False, channel="#general"))
        assert selected == [svc_a]


class TestExecute:
    @pytest.mark.asyncio
    async def test_no_query_shows_usage(self):
        bot = _command_bot(services={})
        cmd = _make_command(bot)
        await cmd.execute(_msg(content="alert"))
        sent = bot.command_manager.send_response.await_args[0][1]
        assert "Usage" in sent

    @pytest.mark.asyncio
    async def test_no_services_message(self):
        bot = _command_bot(services={})
        bot.config.set("Alert_Command", "services", "pulsepoint")
        cmd = _make_command(bot)
        await cmd.execute(_msg(content="alert seattle"))
        sent = bot.command_manager.send_response.await_args[0][1]
        assert "No alert services" in sent

    @pytest.mark.asyncio
    async def test_no_incidents_message(self):
        svc = _FakeService("pulsepoint", "PP", [])
        bot = _command_bot(services={"a": svc})
        bot.config.set("Alert_Command", "services", "pulsepoint")
        cmd = _make_command(bot)
        await cmd.execute(_msg(content="alert seattle"))
        sent = bot.command_manager.send_response.await_args[0][1]
        assert "No active incidents" in sent

    @pytest.mark.asyncio
    async def test_incidents_sent_with_label(self):
        svc = _FakeService("pulsepoint", "PP", ["Fire: Main St", "Medical: Oak Ave"])
        bot = _command_bot(services={"a": svc})
        bot.config.set("Alert_Command", "services", "pulsepoint")
        cmd = _make_command(bot)
        await cmd.execute(_msg(content="alert seattle"))
        # First (and only) message carries the label and incidents.
        first = bot.command_manager.send_response.await_args_list[0][0][1]
        assert first.startswith("[PP]:")
        assert "Fire: Main St" in first

    @pytest.mark.asyncio
    async def test_all_suffix_stripped(self):
        captured = {}

        class _Svc(_FakeService):
            async def query_alerts(self, query):
                captured["query"] = query
                return ["Fire: A"]

        svc = _Svc("pulsepoint", "PP", [])
        bot = _command_bot(services={"a": svc})
        bot.config.set("Alert_Command", "services", "pulsepoint")
        cmd = _make_command(bot)
        await cmd.execute(_msg(content="alert seattle all"))
        assert captured["query"] == "seattle"

