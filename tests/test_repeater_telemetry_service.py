"""Tests for the RepeaterTelemetry service and the akku command."""

import configparser
import time
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.commands.akku_command import AkkuCommand
from modules.service_plugins.repeater_telemetry_service import (
    LEVEL_CRITICAL,
    LEVEL_OK,
    LEVEL_WARN,
    RepeaterTarget,
    RepeaterTelemetryService,
    next_level,
    parse_targets,
)
from tests.conftest import mock_message

SECTION = "RepeaterTelemetry_Service"
REPEATER = {"adv_name": "Rep1", "public_key": "ab" * 32, "type": 2}


def _status(bat_mv):
    return {"bat": bat_mv, "uptime": 90000, "noise_floor": -110, "last_rssi": -80,
            "tx_queue_len": 0, "nb_recv": 10, "nb_sent": 5}


@pytest.fixture
def bot(mock_logger, test_db):
    b = MagicMock()
    b.logger = mock_logger
    b.db_manager = test_db
    b.connected = True
    cfg = configparser.ConfigParser()
    cfg.read_dict({SECTION: {
        "enabled": "true",
        "repeaters": "Rep1:guest",
        "alert_channel": "#admin",
        "offline_after_failures": "2",
        "repeat_alert_hours": "0",
    }})
    b.config = cfg
    b.meshcore.get_contact_by_name = Mock(side_effect=lambda n: REPEATER if n == "Rep1" else None)
    b.meshcore.get_contact_by_key_prefix = Mock(return_value=None)
    b.meshcore.commands.send_login_sync = AsyncMock(return_value=object())
    b.meshcore.commands.req_status_sync = AsyncMock(return_value=_status(4000))
    b.command_manager.send_channel_message = AsyncMock(return_value=True)
    b.command_manager.send_dm = AsyncMock(return_value=True)
    b.bot_tx_rate_limiter.wait_for_tx = AsyncMock()
    return b


@pytest.fixture
def service(bot):
    svc = RepeaterTelemetryService(bot)
    svc._init_db()
    return svc


def _sent(bot):
    return [c.args[1] for c in bot.command_manager.send_channel_message.await_args_list]


class TestParsing:
    def test_parse_targets(self):
        assert parse_targets(" A:pw , B ,C:p:w:x,, ") == [
            RepeaterTarget("A", "pw"), RepeaterTarget("B", ""), RepeaterTarget("C", "p:w:x"),
        ]

    def test_parse_empty(self):
        assert parse_targets("") == []

    @pytest.mark.parametrize("prev,mv,expected", [
        (LEVEL_OK, 4000, LEVEL_OK),
        (LEVEL_OK, 3450, LEVEL_WARN),
        (LEVEL_OK, 3200, LEVEL_CRITICAL),
        (LEVEL_OK, 0, LEVEL_OK),              # no battery reported
        (LEVEL_WARN, 3550, LEVEL_WARN),       # inside hysteresis band
        (LEVEL_WARN, 3650, LEVEL_OK),
        (LEVEL_CRITICAL, 3350, LEVEL_CRITICAL),
        (LEVEL_CRITICAL, 3420, LEVEL_WARN),
        (LEVEL_WARN, 3250, LEVEL_CRITICAL),   # getting worse is immediate
    ])
    def test_next_level(self, prev, mv, expected):
        assert next_level(prev, mv, 3500, 3300, 100) == expected


class TestPolling:
    async def test_ok_poll_stores_sample_without_alert(self, service, bot):
        await service.poll_all()
        rows = service.latest_samples()
        assert rows[0]["name"] == "Rep1" and rows[0]["bat_mv"] == 4000
        bot.meshcore.commands.send_login_sync.assert_awaited_once()
        assert bot.meshcore.commands.send_login_sync.await_args.args[1] == "guest"
        assert _sent(bot) == []

    async def test_low_battery_alerts_once_then_recovers(self, service, bot):
        bot.meshcore.commands.req_status_sync.return_value = _status(3400)
        await service.poll_all()
        await service.poll_all()  # unchanged -> no repeat (repeat_alert_hours = 0)
        bot.meshcore.commands.req_status_sync.return_value = _status(3200)
        await service.poll_all()
        bot.meshcore.commands.req_status_sync.return_value = _status(3900)
        await service.poll_all()
        sent = _sent(bot)
        assert len(sent) == 3
        assert "niedrig" in sent[0] and "3.40" in sent[0]
        assert "KRITISCH" in sent[1]
        assert "wieder ok" in sent[2]
        assert bot.command_manager.send_channel_message.await_args.args[0] == "#admin"

    async def test_offline_after_n_failures_and_back_online(self, service, bot):
        bot.meshcore.commands.send_login_sync.return_value = None
        await service.poll_all()
        assert _sent(bot) == []
        await service.poll_all()
        await service.poll_all()
        assert len(_sent(bot)) == 1 and "antwortet nicht (2x)" in _sent(bot)[0]
        bot.meshcore.commands.send_login_sync.return_value = object()
        await service.poll_all()
        assert "wieder erreichbar" in _sent(bot)[-1]

    async def test_repeat_alert_after_interval(self, service, bot):
        bot.config.set(SECTION, "repeat_alert_hours", "1")
        service._load_settings()
        bot.meshcore.commands.req_status_sync.return_value = _status(3400)
        await service.poll_all()
        # Pretend the last alert was two hours ago
        bot.db_manager.execute_update("UPDATE repeater_telemetry_state SET last_alert_ts = ?",
                                      (int(time.time()) - 7200,))
        await service.poll_all()
        assert len(_sent(bot)) == 2

    async def test_unknown_contact_counts_as_failure(self, service, bot):
        bot.config.set(SECTION, "repeaters", "Nope")
        service._load_settings()
        results = await service.poll_all()
        assert results[0].ok is False and "not in contacts" in results[0].error

    async def test_not_connected_skips(self, service, bot):
        bot.connected = False
        assert await service.poll_all() == []

    async def test_silence_mesh_output(self, service, bot):
        bot.config.set(SECTION, "silence_mesh_output", "true")
        bot.config.set(SECTION, "alert_dm", "Admin")
        service._load_settings()
        service.send_external_notifications = AsyncMock()
        bot.meshcore.commands.req_status_sync.return_value = _status(3400)
        await service.poll_all()
        assert _sent(bot) == []
        bot.command_manager.send_dm.assert_not_awaited()
        service.send_external_notifications.assert_awaited_once()

    async def test_dm_alerts(self, service, bot):
        bot.config.set(SECTION, "alert_channel", "")
        bot.config.set(SECTION, "alert_dm", "Admin, Other")
        service._load_settings()
        bot.meshcore.commands.req_status_sync.return_value = _status(3400)
        await service.poll_all()
        assert [c.args[0] for c in bot.command_manager.send_dm.await_args_list] == ["Admin", "Other"]

    def test_custom_template_and_bad_template_fallback(self, service, bot):
        bot.config.set(SECTION, "template_warn", "LOW {name} {mv}")
        service._load_settings()
        assert service._fmt("warn", "R", 3400, 0) == "LOW R 3400"
        bot.config.set(SECTION, "template_warn", "LOW {nope}")
        service._load_settings()
        assert "niedrig" in service._fmt("warn", "R", 3400, 0)

    def test_prune(self, service, bot):
        bot.db_manager.execute_update(
            "INSERT INTO repeater_telemetry (ts, name, ok) VALUES (?, 'old', 1)", (1,))
        service._prune_old_samples()
        assert bot.db_manager.execute_query("SELECT * FROM repeater_telemetry WHERE name='old'") == []


class TestWebSettings:
    async def test_web_settings_override_config_and_admins_capped(self, service, bot):
        from modules import repeater_telemetry_store as store
        with bot.db_manager.connection() as conn:
            store.save_settings(conn, {"admins": ["A1", "A2"], "alert_channel": "", "warn_mv": 3700})
        service._load_settings()
        assert service.admins == ["A1", "A2"] and service.warn_mv == 3700
        bot.meshcore.commands.req_status_sync.return_value = _status(3650)
        await service.poll_all()
        assert [c.args[0] for c in bot.command_manager.send_dm.await_args_list] == ["A1", "A2"]
        assert _sent(bot) == []

    async def test_paused_skips_polling(self, service, bot):
        from modules import repeater_telemetry_store as store
        with bot.db_manager.connection() as conn:
            store.save_settings(conn, {"paused": True})
        service._load_settings()
        assert service.paused is True

    async def test_web_poll_request_wakes_wait(self, service, bot, monkeypatch):
        import modules.service_plugins.repeater_telemetry_service as mod
        from modules import repeater_telemetry_store as store
        monkeypatch.setattr(mod, "SETTINGS_TICK_SECONDS", 0.05)
        service._handled_poll_request = 0
        with bot.db_manager.connection() as conn:
            store.request_poll(conn)
        started = time.monotonic()
        stopping = await service._wait(30)
        assert stopping is False and time.monotonic() - started < 5
        assert service._handled_poll_request > 0


class TestAkkuCommand:
    @pytest.fixture
    def cmd(self, command_mock_bot, service):
        command_mock_bot.services = {"repeatertelemetry": service}
        return AkkuCommand(command_mock_bot)

    def _reply(self, cmd):
        return cmd.bot.command_manager.send_response.await_args.args[1]

    async def test_no_service(self, command_mock_bot):
        command_mock_bot.services = {}
        cmd = AkkuCommand(command_mock_bot)
        await cmd.execute(mock_message("akku"))
        assert "nicht aktiv" in self._reply(cmd)

    async def test_no_data(self, cmd):
        await cmd.execute(mock_message("akku"))
        assert "Noch keine" in self._reply(cmd)

    async def test_list_and_detail(self, cmd, service, bot):
        bot.meshcore.commands.req_status_sync.return_value = _status(3400)
        await service.poll_all()
        await cmd.execute(mock_message("akku"))
        assert self._reply(cmd).startswith("Rep1 3.40V ⚠️")
        await cmd.execute(mock_message("akku rep"))
        assert "Laufzeit 1d" in self._reply(cmd)
        await cmd.execute(mock_message("akku xyz"))
        assert "Kein Repeater" in self._reply(cmd)

    async def test_many_repeaters_split_into_mesh_sized_messages(self, cmd, bot, test_db):
        now = int(time.time())
        for i in range(12):
            test_db.execute_update(
                "INSERT INTO repeater_telemetry (ts, name, ok, bat_mv) VALUES (?, ?, 1, 3900)",
                (now, f"Repeater-Nummer-{i:02d}"))
        cmd.get_max_message_length = Mock(return_value=130)
        cmd.bot.command_manager.send_response_chunked = AsyncMock(return_value=True)
        await cmd.execute(mock_message("akku"))
        chunks = cmd.bot.command_manager.send_response_chunked.await_args.args[1]
        assert len(chunks) > 1
        assert all(len(c.encode("utf-8")) <= 130 for c in chunks)
        assert sum(c.count("Repeater-Nummer") for c in chunks) == 12

    async def test_state_keyed_by_config_label_when_contact_vanishes(self, service, bot):
        await service.poll_all()
        bot.meshcore.get_contact_by_name.side_effect = lambda n: None
        await service.poll_all()
        await service.poll_all()
        assert "Rep1" in _sent(bot)[-1] and "antwortet nicht" in _sent(bot)[-1]

    async def test_poll_now(self, cmd, service):
        await cmd.execute(mock_message("akku jetzt"))
        assert service._poll_now.is_set()
