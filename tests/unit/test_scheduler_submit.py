"""MessageScheduler._submit_to_main_loop: fire-and-forget scheduling with failure logging."""

import asyncio
import threading
from unittest.mock import MagicMock

from modules.scheduler import MessageScheduler


def _scheduler_with_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    sched = object.__new__(MessageScheduler)
    sched.bot = MagicMock(main_event_loop=loop)
    sched.logger = MagicMock()
    return sched, loop, thread


async def _boom():
    raise RuntimeError("nope")


async def _ok():
    return 5


def test_failure_is_logged_with_traceback_by_default():
    sched, loop, thread = _scheduler_with_loop()
    try:
        fut = sched._submit_to_main_loop(_boom(), "Error processing radio operations")
        try:
            fut.result(timeout=5)
        except RuntimeError:
            pass
        loop.call_soon_threadsafe(lambda: None)
        # done callbacks run in the loop thread right after completion
        for _ in range(50):
            if sched.logger.exception.called:
                break
            threading.Event().wait(0.01)
        args = sched.logger.exception.call_args[0]
        assert args[0] == "Error processing radio operations: %s"
        assert str(args[1]) == "nope"
        sched.logger.error.assert_not_called()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)


def test_plain_error_when_traceback_off_and_silence_on_success():
    sched, loop, thread = _scheduler_with_loop()
    try:
        fut = sched._submit_to_main_loop(_boom(), "Error in feed polling cycle", log_traceback=False)
        try:
            fut.result(timeout=5)
        except RuntimeError:
            pass
        for _ in range(50):
            if sched.logger.error.called:
                break
            threading.Event().wait(0.01)
        assert sched.logger.error.call_args[0][0] == "Error in feed polling cycle: %s"
        sched.logger.reset_mock()
        assert sched._submit_to_main_loop(_ok(), "x").result(timeout=5) == 5
        threading.Event().wait(0.05)
        sched.logger.error.assert_not_called()
        sched.logger.exception.assert_not_called()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
