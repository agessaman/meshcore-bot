"""Greeter reservation, warmup, and human-mention behavior."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from modules.commands.greeter_command import GreeterCommand
from modules.db_manager import DBManager
from tests.conftest import mock_message


def _greeter(bot, tmp_path, **settings: str) -> GreeterCommand:
    bot.db_manager = DBManager(bot, str(tmp_path / "greeter.db"))
    bot.channel_responses_enabled = True
    bot.config.add_section("Greeter_Command")
    values = {
        "enabled": "true",
        "rollout_days": "0",
        "auto_backfill": "false",
        "include_mesh_info": "false",
        "dead_air_delay_seconds": "15",
        "defer_to_human_greeting": "false",
        "per_channel_greetings": "false",
        "channels": "Public",
    }
    values.update(settings)
    for key, value in values.items():
        bot.config.set("Greeter_Command", key, value)
    return GreeterCommand(bot)


def _first_message(sender_id: str = "Chemex"):
    return mock_message(content=".", channel="Public", sender_id=sender_id, timestamp=1_700_000_000)


async def _finish_delayed(cmd: GreeterCommand, message):
    """Run a scheduled wait without sleeping, and return the send mock."""
    send = AsyncMock(return_value=True)
    with (
        patch.object(cmd, "_send_greeting", send),
        patch("modules.commands.greeter_command.asyncio.sleep", new_callable=AsyncMock),
    ):
        scheduled = await cmd.execute(message)
        task = cmd.pending_greetings.get((message.sender_id, message.channel))
        if task is not None:
            await task
    return scheduled, send


class TestDeadAirDelay:
    @pytest.mark.parametrize("per_channel", ["false", "true"])
    async def test_delay_sends_the_greeting_it_reserved(self, command_mock_bot, tmp_path, per_channel):
        cmd = _greeter(command_mock_bot, tmp_path, per_channel_greetings=per_channel)
        message = _first_message()

        scheduled, send = await _finish_delayed(cmd, message)

        assert scheduled is True
        send.assert_awaited_once_with(message)
        assert cmd.has_been_greeted("Chemex", "Public") is True
        assert cmd.pending_greetings == {}

    async def test_second_message_during_the_wait_does_not_send_again(self, command_mock_bot, tmp_path):
        cmd = _greeter(command_mock_bot, tmp_path)
        message = _first_message()
        send = AsyncMock(return_value=True)
        with (
            patch.object(cmd, "_send_greeting", send),
            patch("modules.commands.greeter_command.asyncio.sleep", new_callable=AsyncMock),
        ):
            assert await cmd.execute(message) is True
            assert await cmd.execute(message) is False
            await cmd.pending_greetings[(message.sender_id, message.channel)]

        send.assert_awaited_once()

    async def test_human_mention_cancels_the_wait(self, command_mock_bot, tmp_path):
        cmd = _greeter(command_mock_bot, tmp_path, defer_to_human_greeting="true")
        message = _first_message()
        send = AsyncMock(return_value=True)
        with (
            patch.object(cmd, "_send_greeting", send),
            patch("modules.commands.greeter_command.asyncio.sleep", new_callable=AsyncMock),
        ):
            assert await cmd.execute(message) is True
            task = cmd.pending_greetings[(message.sender_id, message.channel)]
            cmd.check_message_for_human_greeting(
                mock_message(content="welcome Chemex", channel="Public", sender_id="Ada", timestamp=1_700_000_001)
            )
            with pytest.raises(asyncio.CancelledError):
                await task

        send.assert_not_awaited()
        assert cmd.has_been_greeted("Chemex", "Public") is True

    async def test_human_mention_found_after_the_wait_skips_the_send(self, command_mock_bot, tmp_path):
        cmd = _greeter(command_mock_bot, tmp_path, defer_to_human_greeting="true")
        message = _first_message()
        send = AsyncMock(return_value=True)
        with (
            patch.object(cmd, "_send_greeting", send),
            patch.object(cmd, "_check_human_greeting", return_value=True),
            patch("modules.commands.greeter_command.asyncio.sleep", new_callable=AsyncMock),
        ):
            assert await cmd.execute(message) is True
            await cmd.pending_greetings[(message.sender_id, message.channel)]

        send.assert_not_awaited()
        assert cmd.has_been_greeted("Chemex", "Public") is True

    async def test_already_greeted_user_is_not_scheduled(self, command_mock_bot, tmp_path):
        cmd = _greeter(command_mock_bot, tmp_path)
        assert cmd.mark_as_greeted("Chemex", "Public") is True
        send = AsyncMock(return_value=True)
        with patch.object(cmd, "_send_greeting", send):
            assert await cmd.execute(_first_message()) is False
        send.assert_not_awaited()
        assert cmd.pending_greetings == {}

    async def test_zero_delay_sends_immediately(self, command_mock_bot, tmp_path):
        cmd = _greeter(command_mock_bot, tmp_path, dead_air_delay_seconds="0")
        message = _first_message()
        send = AsyncMock(return_value=True)
        with patch.object(cmd, "_send_greeting", send):
            assert await cmd.execute(message) is True
        send.assert_awaited_once_with(message)
        assert cmd.pending_greetings == {}


class TestReservationRelease:
    async def test_paused_replies_clear_the_reservation(self, command_mock_bot, tmp_path):
        cmd = _greeter(command_mock_bot, tmp_path)
        message = _first_message()

        async def pause(_seconds):
            cmd.bot.channel_responses_enabled = False

        send = AsyncMock(return_value=True)
        with (
            patch.object(cmd, "_send_greeting", send),
            patch("modules.commands.greeter_command.asyncio.sleep", side_effect=pause),
        ):
            assert await cmd.execute(message) is True
            await cmd.pending_greetings[(message.sender_id, message.channel)]

        send.assert_not_awaited()
        assert cmd.has_been_greeted("Chemex", "Public") is False
        assert cmd.pending_greetings == {}

    async def test_failed_send_can_be_retried(self, command_mock_bot, tmp_path):
        cmd = _greeter(command_mock_bot, tmp_path, dead_air_delay_seconds="0")
        message = _first_message()
        send = AsyncMock(return_value=False)
        with patch.object(cmd, "_send_greeting", send):
            assert await cmd.execute(message) is False
            assert cmd.has_been_greeted("Chemex", "Public") is False
            send.return_value = True
            assert await cmd.execute(message) is True
        assert cmd.has_been_greeted("Chemex", "Public") is True
        assert send.await_count == 2

    async def test_error_during_the_wait_clears_the_reservation(self, command_mock_bot, tmp_path):
        cmd = _greeter(command_mock_bot, tmp_path, defer_to_human_greeting="true")
        message = _first_message()
        send = AsyncMock(return_value=True)
        with (
            patch.object(cmd, "_send_greeting", send),
            patch.object(cmd, "_check_human_greeting", side_effect=RuntimeError("db")),
            patch("modules.commands.greeter_command.asyncio.sleep", new_callable=AsyncMock),
        ):
            assert await cmd.execute(message) is True
            await cmd.pending_greetings[(message.sender_id, message.channel)]

        send.assert_not_awaited()
        assert cmd.has_been_greeted("Chemex", "Public") is False


class TestRolloutOnce:
    def test_a_completed_rollout_is_not_started_again(self, command_mock_bot, tmp_path):
        command_mock_bot.channel_responses_enabled = True
        command_mock_bot.db_manager = DBManager(command_mock_bot, str(tmp_path / "greeter.db"))
        command_mock_bot.config.add_section("Greeter_Command")
        command_mock_bot.config.set("Greeter_Command", "enabled", "true")
        command_mock_bot.config.set("Greeter_Command", "rollout_days", "7")
        command_mock_bot.config.set("Greeter_Command", "auto_backfill", "false")
        command_mock_bot.config.set("Greeter_Command", "channels", "Public")

        GreeterCommand(command_mock_bot)
        with command_mock_bot.db_manager.connection() as conn:
            started = conn.execute("SELECT rollout_completed FROM greeter_rollout").fetchall()
        assert [row[0] for row in started] == [0]

        with command_mock_bot.db_manager.connection() as conn:
            conn.execute("UPDATE greeter_rollout SET rollout_completed = 1")
            conn.commit()

        GreeterCommand(command_mock_bot)
        with command_mock_bot.db_manager.connection() as conn:
            again = conn.execute(
                "SELECT rollout_completed FROM greeter_rollout ORDER BY id"
            ).fetchall()
        assert [row[0] for row in again] == [1]


class TestHumanMention:
    def test_fuzziness_adds_typos_without_dropping_an_exact_mention(self, command_mock_bot, tmp_path):
        cmd = _greeter(command_mock_bot, tmp_path)
        cmd.levenshtein_distance = 1
        assert cmd._mentions_user("welcome Chemx", "Chemex") is True
        assert cmd._mentions_user("welcome Chemex", "Chemex") is True
        assert cmd._mentions_user("welcome Bob", "Chemex") is False

        cmd.levenshtein_distance = 0
        assert cmd._mentions_user("welcome Chemx", "Chemex") is False
        assert cmd._mentions_user("welcome Chemex", "Chemex") is True

    async def test_realtime_cancel_is_case_insensitive_and_fuzzy(self, command_mock_bot, tmp_path):
        cmd = _greeter(command_mock_bot, tmp_path, defer_to_human_greeting="true")
        cmd.levenshtein_distance = 1
        message = _first_message()
        send = AsyncMock(return_value=True)
        with (
            patch.object(cmd, "_send_greeting", send),
            patch("modules.commands.greeter_command.asyncio.sleep", new_callable=AsyncMock),
        ):
            assert await cmd.execute(message) is True
            task = cmd.pending_greetings[(message.sender_id, message.channel)]
            cmd.check_message_for_human_greeting(
                mock_message(content="welcome Chemx", channel="public", sender_id="Ada", timestamp=1_700_000_001)
            )
            with pytest.raises(asyncio.CancelledError):
                await task

        send.assert_not_awaited()
        assert cmd.has_been_greeted("Chemex", "Public") is True
