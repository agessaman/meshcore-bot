"""BaseServicePlugin.run_periodic: the shared poll loop used by several services."""

import asyncio
from unittest.mock import MagicMock, patch

from modules.service_plugins.base_service import BaseServicePlugin


class _Service(BaseServicePlugin):
    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False


def _service():
    bot = MagicMock()
    service = _Service(bot)
    service.logger = MagicMock()
    service._running = True
    return service


def test_runs_work_then_sleeps_the_current_interval_until_stopped():
    service = _service()
    calls, sleeps, intervals = [], [], iter([5.0, 7.0])

    async def work():
        calls.append(1)

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            service._running = False

    with patch("asyncio.sleep", fake_sleep):
        asyncio.run(service.run_periodic(work, lambda: next(intervals), "Error in loop"))
    assert calls == [1, 1]
    assert sleeps == [5.0, 7.0]  # the interval is read again each round


def test_an_error_is_logged_and_waits_the_error_delay():
    service = _service()
    sleeps = []

    async def work():
        raise RuntimeError("feed down")

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        service._running = False

    with patch("asyncio.sleep", fake_sleep):
        asyncio.run(service.run_periodic(work, lambda: 5.0, "Error in loop"))
    assert sleeps == [60]
    service.logger.error.assert_called_once()
    args = service.logger.error.call_args.args
    assert args[0] % args[1:] == "Error in loop: feed down"


def test_cancellation_ends_the_loop_quietly():
    service = _service()

    async def work():
        raise asyncio.CancelledError

    asyncio.run(service.run_periodic(work, lambda: 5.0, "Error in loop"))
    service.logger.error.assert_not_called()


def test_does_nothing_when_not_running():
    service = _service()
    service._running = False
    called = []

    async def work():
        called.append(1)

    asyncio.run(service.run_periodic(work, lambda: 5.0, "Error in loop"))
    assert called == []


def test_cancel_tasks_cancels_in_order_and_skips_none():
    async def main():
        started = asyncio.Event()

        async def forever():
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(forever())
        await started.wait()
        await BaseServicePlugin._cancel_tasks(None, task)
        return task

    task = asyncio.run(main())
    assert task.cancelled()


def test_cancel_tasks_reraises_a_finished_tasks_error_like_awaiting_it():
    import pytest

    async def main():
        async def boom():
            raise ValueError("failed earlier")

        task = asyncio.create_task(boom())
        await asyncio.sleep(0)
        with pytest.raises(ValueError):
            await BaseServicePlugin._cancel_tasks(task)

    asyncio.run(main())
