"""Tests for the neighbors command (modules/commands/neighbors_command.py)."""

from __future__ import annotations

import asyncio
import time
import types

import pytest

from modules.commands.neighbors_command import NeighborsCommand
from modules.neighbors_discovery import MIN_CYCLE_GAP_SECONDS
from modules.service_plugins.packet_capture_service import PacketCaptureService
from tests.conftest import mock_message


def make_service(*, neighbors_enabled=True, summary=None, hang=False,
                 raises=None, cycle_budget=5.0, last_publish=0.0,
                 last_attempt=0.0, cycle_active=False):
    """A stand-in for PacketCaptureService's neighbors surface."""
    service = types.SimpleNamespace()
    service.neighbors_enabled = neighbors_enabled
    service.neighbors_config = types.SimpleNamespace(
        discover_window=60.0, cycle_budget=cycle_budget
    )
    # 0.0 == "no cycle has ever run", so nothing is on cooldown.
    service.last_neighbors_publish = last_publish
    service.last_neighbors_attempt = last_attempt
    service.neighbors_cycle_active = cycle_active
    # The node-wide cooldown is the service's rule; mirror its arithmetic here so
    # the command is tested against the same contract the real service offers.
    service.neighbors_cooldown_remaining = lambda: PacketCaptureService.\
        neighbors_cooldown_remaining(service)
    service.calls = 0

    def claim_neighbors_cycle():
        if service.neighbors_cycle_active:
            return "a discovery cycle is already running"
        remaining = service.neighbors_cooldown_remaining()
        if remaining > 0:
            return f"another cycle may run in {remaining:.0f}s"
        service.neighbors_cycle_active = True
        return None

    def release_neighbors_cycle():
        service.neighbors_cycle_active = False

    async def run_cycle(*, already_claimed=False):
        if not already_claimed:
            reason = claim_neighbors_cycle()
            if reason is not None:
                return {
                    "ok": False, "reason": reason, "discovered": 0, "queried": 0,
                    "best_snr": None, "attempted": 0, "succeeded": 0, "recorded": 0,
                }
        service.calls += 1
        try:
            if hang:
                await asyncio.sleep(30)
            if raises is not None:
                raise raises
            return summary or {"ok": True, "queried": 0, "recorded": 0, "attempted": 0}
        finally:
            release_neighbors_cycle()

    service.claim_neighbors_cycle = claim_neighbors_cycle
    service.release_neighbors_cycle = release_neighbors_cycle
    service.run_neighbors_cycle = run_cycle
    return service


def make_command(command_mock_bot, service, *, enabled=True, settle_seconds=0.0):
    """Build the command without __init__ (which reads config and the ACL)."""
    command_mock_bot.packet_capture_service = service
    command = object.__new__(NeighborsCommand)
    command.bot = command_mock_bot
    command.logger = command_mock_bot.logger
    command.command_enabled = enabled
    command._cycle_task = None
    # No settle pause by default, so the tests that are not about it stay fast.
    command.settle_seconds = settle_seconds
    # Normally set up by BaseCommand.__init__, which make_command skips.
    command._user_cooldowns = {}
    command.cooldown_seconds = NeighborsCommand.cooldown_seconds

    sent: list[str] = []

    async def send_response(message, content, **kwargs):
        sent.append(content)
        return True

    command.send_response = send_response
    command.translate = lambda key, **kwargs: (
        key.split(".")[-1] + (" " + " ".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
                              if kwargs else "")
    )
    return command, sent


@pytest.fixture
def message():
    return mock_message(content="neighbors", is_dm=True, sender_id="TestUser")


def test_command_metadata():
    # A cycle spends airtime and takes a minute, so it is DM-only with a cooldown.
    assert NeighborsCommand.requires_dm is True
    assert NeighborsCommand.cooldown_seconds == 900
    assert "neighbours" in NeighborsCommand.keywords


async def test_reports_when_discovery_is_disabled(command_mock_bot, message):
    command, sent = make_command(command_mock_bot, make_service(neighbors_enabled=False))
    assert await command.execute(message) is True
    assert sent == ["disabled"]


async def test_reports_when_the_service_is_absent(command_mock_bot, message):
    command, sent = make_command(command_mock_bot, None)
    command_mock_bot.services = {}
    assert await command.execute(message) is True
    assert sent == ["disabled"]


async def test_disabled_refusal_does_not_burn_the_senders_own_cooldown(
    command_mock_bot, message
):
    """Enabling the feature a minute later must not still be blocked for 14 more."""
    command, sent = make_command(
        command_mock_bot, make_service(neighbors_enabled=False)
    )
    command.record_execution(message.sender_id)
    await command.execute(message)
    assert sent == ["disabled"]

    can_execute, remaining = command.check_cooldown(message.sender_id)
    assert can_execute is False
    assert remaining == pytest.approx(60, abs=5)


async def test_acknowledges_before_running_the_cycle(command_mock_bot, message):
    """The listen window is ~60s, far too long to hold the reply open."""
    service = make_service(summary={"ok": True, "queried": 2, "best_snr": 8.0,
                                    "recorded": 2, "attempted": 0})
    command, sent = make_command(command_mock_bot, service)

    assert await command.execute(message) is True
    assert sent == ["started seconds=60"]
    assert service.calls == 0  # not awaited inline
    # Claimed before the ack so the scheduler cannot sneak in during send_response.
    assert service.neighbors_cycle_active is True

    await command._cycle_task
    assert len(sent) == 2
    assert sent[1].startswith("success")
    assert service.neighbors_cycle_active is False


async def test_claim_before_ack_survives_a_scheduler_race(command_mock_bot, message):
    """If another trigger starts during send_response, we already hold the lock."""
    service = make_service(summary={"ok": True, "queried": 0, "recorded": 0, "attempted": 0})
    command, sent = make_command(command_mock_bot, service)

    raced = {}

    async def send_and_race(message, content, **kwargs):
        sent.append(content)
        if content.startswith("started"):
            # Stands in for the scheduler waking during the ack await.
            raced["summary"] = await service.run_neighbors_cycle()
        return True

    command.send_response = send_and_race
    await command.execute(message)
    await command._cycle_task

    assert sent[0].startswith("started")
    assert raced["summary"]["ok"] is False
    assert "already running" in raced["summary"]["reason"]
    assert service.calls == 1


async def test_summary_reports_count_snr_and_records(command_mock_bot, message):
    service = make_service(summary={"ok": True, "queried": 3, "best_snr": 8.25,
                                    "recorded": 3, "attempted": 0})
    command, sent = make_command(command_mock_bot, service)
    await command.execute(message)
    await command._cycle_task
    assert "count=3" in sent[1]
    assert "recorded=3" in sent[1]
    assert "best_snr=8.2dB" in sent[1]


async def test_summary_mentions_brokers_only_when_one_was_tried(command_mock_bot, message):
    """A operator with no MQTT should not be shown a confusing 0/0."""
    without = make_service(summary={"ok": True, "queried": 1, "best_snr": 1.0,
                                    "recorded": 1, "attempted": 0})
    command, sent = make_command(command_mock_bot, without)
    await command.execute(message)
    await command._cycle_task
    assert "published" not in sent[1]

    with_broker = make_service(summary={"ok": True, "queried": 1, "best_snr": 1.0,
                                        "recorded": 1, "attempted": 2, "succeeded": 1})
    command2, sent2 = make_command(command_mock_bot, with_broker)
    await command2.execute(message)
    await command2._cycle_task
    assert "published" in sent2[1]
    assert "succeeded=1" in sent2[1]
    assert "attempted=2" in sent2[1]


async def test_reports_when_nothing_answered(command_mock_bot, message):
    service = make_service(summary={"ok": True, "queried": 0, "recorded": 0, "attempted": 0})
    command, sent = make_command(command_mock_bot, service)
    await command.execute(message)
    await command._cycle_task
    assert sent[1] == "none"


async def test_reports_the_reason_a_cycle_did_not_run(command_mock_bot, message):
    service = make_service(summary={"ok": False, "reason": "radio not connected"})
    command, sent = make_command(command_mock_bot, service)
    await command.execute(message)
    await command._cycle_task
    assert sent[1] == "failed reason=radio not connected"


async def test_missing_snr_does_not_break_the_summary(command_mock_bot, message):
    service = make_service(summary={"ok": True, "queried": 1, "best_snr": None,
                                    "recorded": 1, "attempted": 0})
    command, sent = make_command(command_mock_bot, service)
    await command.execute(message)
    await command._cycle_task
    assert "best_snr=n/a" in sent[1]


async def test_cycle_errors_are_reported(command_mock_bot, message):
    service = make_service(raises=RuntimeError("radio exploded"))
    command, sent = make_command(command_mock_bot, service)
    await command.execute(message)
    await command._cycle_task
    assert sent[1].startswith("error")
    assert "radio exploded" in sent[1]


async def test_a_stalled_cycle_is_abandoned(command_mock_bot, message):
    service = make_service(hang=True, cycle_budget=0.05)
    command, sent = make_command(command_mock_bot, service)
    await command.execute(message)
    await command._cycle_task
    assert sent[1].startswith("error")
    assert "timed out" in sent[1]


async def test_a_second_request_will_not_overlap_the_first(command_mock_bot, message):
    """Two concurrent discover rounds would collect into each other's window."""
    service = make_service(hang=True, cycle_budget=5.0)
    command, sent = make_command(command_mock_bot, service)

    await command.execute(message)
    await command.execute(message)
    assert sent == ["started seconds=60", "busy"]

    command._cycle_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command._cycle_task


async def test_busy_refusal_does_not_burn_the_senders_own_cooldown(
    command_mock_bot, message
):
    """A mid-cycle 'busy' clears with the discover window, not after 15 minutes."""
    service = make_service(cycle_active=True)
    command, sent = make_command(command_mock_bot, service)

    command.record_execution(message.sender_id)
    await command.execute(message)
    assert sent == ["busy"]

    can_execute, remaining = command.check_cooldown(message.sender_id)
    assert can_execute is False
    assert remaining == pytest.approx(60, abs=5)


async def test_a_finished_cycle_does_not_block_the_next_request(command_mock_bot, message):
    service = make_service(summary={"ok": True, "queried": 0, "recorded": 0, "attempted": 0})
    command, sent = make_command(command_mock_bot, service)

    await command.execute(message)
    await command._cycle_task
    await command.execute(message)
    await command._cycle_task

    assert service.calls == 2
    assert "busy" not in sent


async def test_a_cycle_the_service_is_running_reads_as_busy(command_mock_bot, message):
    """The scheduler's cycle is invisible to this command's own task guard."""
    service = make_service(cycle_active=True)
    command, sent = make_command(command_mock_bot, service)

    assert await command.execute(message) is True
    assert sent == ["busy"]
    assert service.calls == 0


async def test_the_cooldown_applies_across_senders(command_mock_bot):
    """The base class rations per user; airtime has to be rationed per node.

    Otherwise N users can take turns and keep the radio discovering nonstop.
    """
    service = make_service(last_publish=time.time() - 60)
    command, sent = make_command(command_mock_bot, service)

    for sender in ("UserOne", "UserTwo"):
        result = await command.execute(
            mock_message(content="neighbors", is_dm=True, sender_id=sender)
        )
        assert result is True

    assert sent == ["cooldown_active minutes=14"] * 2
    assert service.calls == 0
    assert command._cycle_task is None


async def test_a_failed_cycle_still_starts_the_cooldown(command_mock_bot):
    """The discover broadcast may have gone out even when the cycle failed, so a
    second sender must not be able to transmit another round immediately."""
    service = make_service(last_publish=0.0, last_attempt=time.time() - 60)
    command, sent = make_command(command_mock_bot, service)

    result = await command.execute(
        mock_message(content="neighbors", is_dm=True, sender_id="SomeoneElse")
    )
    assert result is True
    assert sent == ["cooldown_active minutes=14"]
    assert service.calls == 0


async def test_a_refusal_does_not_burn_the_senders_own_cooldown(command_mock_bot, message):
    """The command manager records the execution before calling execute().

    So a request refused for the shared cooldown has already spent this sender's
    15 minutes. Telling them to wait one minute and then refusing for fourteen
    more — on their personal cooldown this time — would make the reply a lie.
    """
    service = make_service(last_attempt=time.time() - 840)  # 60s left
    command, sent = make_command(command_mock_bot, service)

    command.record_execution(message.sender_id)  # what the manager does first
    await command.execute(message)
    assert sent == ["cooldown_active minutes=1"]

    can_execute, remaining = command.check_cooldown(message.sender_id)
    assert can_execute is False
    # Expires with the shared cooldown, not 15 minutes from the refusal.
    assert remaining == pytest.approx(60, abs=5)


async def test_a_refusal_still_blocks_an_immediate_retry(command_mock_bot, message):
    """Rewound, not cleared: a refusal reply is airtime too."""
    service = make_service(last_attempt=time.time() - 60)
    command, _ = make_command(command_mock_bot, service)

    command.record_execution(message.sender_id)
    await command.execute(message)

    assert command.check_cooldown(message.sender_id)[0] is False


async def test_the_cooldown_expires(command_mock_bot, message):
    service = make_service(last_publish=time.time() - (MIN_CYCLE_GAP_SECONDS + 1),
                           summary={"ok": True, "queried": 0, "recorded": 0, "attempted": 0})
    command, sent = make_command(command_mock_bot, service)

    await command.execute(message)
    assert sent[0] == "started seconds=60"
    await command._cycle_task
    assert service.calls == 1


def test_disabled_command_cannot_execute(command_mock_bot, message):
    command, _ = make_command(command_mock_bot, make_service(), enabled=False)
    assert command.can_execute(message) is False


# ---------------------------------------------------------------------------
# Settle pause before discovery (GitHub #385)
# ---------------------------------------------------------------------------

async def test_discovery_waits_for_the_settle_pause(command_mock_bot, message):
    """The discover request must not follow the reply straight onto the air."""
    service = make_service()
    command, sent = make_command(command_mock_bot, service, settle_seconds=0.2)

    await command.execute(message)
    await asyncio.sleep(0.05)
    assert sent == ["started seconds=60"]
    assert service.calls == 0
    # Still claimed, so the scheduler cannot take the gap.
    assert service.neighbors_cycle_active is True

    await command._cycle_task
    assert service.calls == 1
    assert service.neighbors_cycle_active is False


async def test_the_settle_pause_does_not_eat_the_cycle_budget(command_mock_bot, message):
    """The budget bounds the radio work; a pause longer than it must not time out."""
    service = make_service(cycle_budget=0.05)
    command, sent = make_command(command_mock_bot, service, settle_seconds=0.1)
    await command.execute(message)
    await command._cycle_task
    assert service.calls == 1
    assert not sent[1].startswith("error")


async def test_cancelling_during_the_pause_releases_the_claim(command_mock_bot, message):
    """The cycle never ran, so nothing else would release the claim."""
    service = make_service()
    command, _ = make_command(command_mock_bot, service, settle_seconds=5.0)
    await command.execute(message)
    assert service.neighbors_cycle_active is True

    command._cycle_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command._cycle_task
    assert service.neighbors_cycle_active is False
    assert service.calls == 0


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, 5.0),          # unset: the default
        (2.5, 2.5),
        (0, 0.0),             # 0 turns the pause off
        (-3, 0.0),
        (120, 30.0),          # capped
        (float("nan"), 5.0),
        ("junk", 5.0),
    ],
)
def test_settle_seconds_config(command_mock_bot, configured, expected):
    command = object.__new__(NeighborsCommand)

    def get_config_value(section, key, fallback=None, value_type="str"):
        assert (section, key) == ("Neighbors_Command", "settle_seconds")
        return fallback if configured is None else configured

    command.get_config_value = get_config_value
    assert command._read_settle_seconds() == expected
