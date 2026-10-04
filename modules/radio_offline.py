"""Radio-offline circuit breaker, mixed into MeshCoreBot.

After ``radio_offline_threshold`` consecutive send timeouts the bot stops
transmitting. An answered health probe arms a single trial send; its real
outcome clears the outage or ends the trial until the next probe. The state
lives on the bot instance, and the web viewer sees it through bot_metadata.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from typing import Any

# The id of the radio-offline trial the current task was admitted as
# (MeshCoreBot._as_offline_trial), so is_radio_offline lets only that send out.
_OFFLINE_TRIAL_SEND: contextvars.ContextVar[int] = contextvars.ContextVar(
    "offline_trial_send", default=0
)


class RadioOfflineBreaker:
    """Mixin for MeshCoreBot; expects ``config``, ``logger`` and ``db_manager``."""

    config: Any
    logger: Any
    db_manager: Any
    _radio_offline_trial: str | None  # None, 'armed' or 'in_flight'

    @property
    def is_radio_offline(self) -> bool:
        """True while repeated outbound send timeouts are suppressing sends.

        Distinct from zombie state — the radio may still be forwarding received
        packets but is not completing outbound sends. The state works like a
        circuit breaker: after a health probe gets an answer, the scheduler's
        next measured send (a scheduled message or interval advert) goes out on
        trial. Only that send sees False here; everything else stays suppressed
        until it succeeds, which clears the state. The web viewer's "Clear
        Offline Flag" also clears it.
        """
        if not getattr(self, '_radio_offline', False):
            return False
        return not self._holds_offline_trial(_OFFLINE_TRIAL_SEND.get())

    def _holds_offline_trial(self, trial: int) -> bool:
        """True when *trial* is the id of the trial send currently in flight."""
        return bool(trial) and (
            getattr(self, '_radio_offline_trial', None) == 'in_flight'
            and getattr(self, '_radio_offline_trial_id', 0) == trial
        )

    def _offline_lock(self) -> threading.Lock:
        """Guards the offline state, which the scheduler thread and the event loop both change."""
        lock = getattr(self, '_radio_offline_lock', None)
        if lock is None:
            lock = self._radio_offline_lock = threading.Lock()
        return lock

    def _offline_publish_lock(self) -> threading.RLock:
        """Serializes bot_metadata writes and reads of the offline state."""
        lock = getattr(self, '_radio_offline_publish_lock', None)
        if lock is None:
            lock = self._radio_offline_publish_lock = threading.RLock()
        return lock

    def _admit_measured_send(self) -> tuple[bool, int]:
        """Decide whether a measured scheduler send may go out: ``(allowed, trial_id)``.

        ``trial_id`` is 0 for an ordinary send. While offline, a probe-armed
        trial is claimed here, once, so concurrent callers cannot all pass. The
        caller runs the send under ``_as_offline_trial(coro, trial_id)`` and
        settles it with ``_record_send_success``, ``_record_send_failure`` or
        ``_record_send_inconclusive``; settlements naming an older trial are
        ignored.
        """
        with self._offline_lock():
            if not getattr(self, '_radio_offline', False):
                return True, 0
            if getattr(self, '_radio_offline_trial', None) == 'armed':
                self._radio_offline_trial = 'in_flight'
                self._radio_offline_trial_id = getattr(self, '_radio_offline_trial_id', 0) + 1
                return True, self._radio_offline_trial_id
            return False, 0

    @staticmethod
    async def _as_offline_trial(coro: Any, trial: int) -> Any:
        """Run *coro* as offline trial *trial*, so the send-path guards let it through."""
        _OFFLINE_TRIAL_SEND.set(trial)
        return await coro

    async def _send_as_offline_trial(self, trial: int, send: Any) -> bool:
        """Run an interactive send (``send()`` returns its coroutine) as offline trial *trial*.

        Lets a bot with no scheduled messages or interval adverts recover too.
        The send's own result settles the trial: True clears the outage; False
        or an exception ends the trial until the next answered health probe,
        so a dead radio still gets at most one attempt per probe.
        """
        token = _OFFLINE_TRIAL_SEND.set(trial)
        try:
            ok = bool(await send())
        except BaseException:
            self._end_offline_trial(trial)
            raise
        finally:
            _OFFLINE_TRIAL_SEND.reset(token)
        if ok:
            # Clearing the outage writes bot_metadata, so it runs in a worker
            # thread; shielded so a cancelled caller cannot strand the trial.
            await asyncio.shield(asyncio.to_thread(self._record_send_success, trial))
        else:
            self._end_offline_trial(trial)
        return ok

    def _end_offline_trial(self, trial: int) -> None:
        """Trial *trial* went out but did not succeed: suppress again until the next answered probe."""
        with self._offline_lock():
            if not self._holds_offline_trial(trial):
                return
            self._radio_offline_trial = None
        self.logger.warning("Trial send after a health probe failed; outbound sends stay suppressed")

    def _record_send_failure(self, scheduler: Any | None = None, trial: int = 0) -> None:
        """Increment the consecutive-send-failure counter.

        Called by the scheduler when an outbound send times out at the
        ``future.result()`` level (i.e. the outer 60-second wall-clock
        timeout fired).  After ``radio_offline_threshold`` consecutive
        failures the bot transitions to radio-offline state, persists it
        to the DB for the web viewer banner, and optionally sends an alert
        email (once per outage). A failed trial send leaves the state in
        place without a new alert; the next answered health probe arms
        another trial.
        """
        import datetime as _dt
        import threading as _threading

        threshold = self.config.getint(
            'Connection',
            'radio_offline_threshold',
            fallback=self.config.getint('Bot', 'radio_offline_threshold', fallback=3),
        )
        send_alert = False
        with self._offline_lock():
            self._send_consecutive_failures: int = (
                getattr(self, '_send_consecutive_failures', 0) + 1
            )
            failures = self._send_consecutive_failures
            if getattr(self, '_radio_offline', False):
                if self._holds_offline_trial(trial):
                    self._radio_offline_trial = None
                    self.logger.warning(
                        "Trial send after a health probe failed; outbound sends stay suppressed"
                    )
                return
            if failures < threshold:
                return
            self._radio_offline = True
            self._radio_offline_trial = None
            self._radio_offline_generation = getattr(self, '_radio_offline_generation', 0) + 1
            self._radio_offline_since = _dt.datetime.now(_dt.timezone.utc).isoformat()
            send_alert = not getattr(self, '_radio_offline_alerted', False)
            self._radio_offline_alerted = True
        self.logger.critical(
            "RADIO OFFLINE: %d consecutive send timeouts (threshold %d). "
            "Bot will suppress further outbound sends until one succeeds. "
            "Check radio power and connection.",
            failures,
            threshold,
        )
        self._publish_offline_state()
        if scheduler is not None and send_alert:
            _threading.Thread(
                target=scheduler.send_radio_offline_alert_email,
                args=(failures, threshold),
                daemon=True,
            ).start()

    def _record_send_success(self, trial: int = 0) -> None:
        """Clear the consecutive-send-failure counter after a successful send.

        While offline, only the success of trial *trial*, if it is still the
        trial in flight, clears the outage; a send admitted before the outage
        (or an older trial finishing late) cannot clear a newer one.
        """
        with self._offline_lock():
            failures = getattr(self, '_send_consecutive_failures', 0)
            was_offline = bool(getattr(self, '_radio_offline', False))
            clears = was_offline and self._holds_offline_trial(trial)
            if was_offline and not clears:
                return
            if clears:
                self._reset_offline_state_locked()
            self._send_consecutive_failures = 0
        if failures > 0 or clears:
            self.logger.info(
                "Outbound send succeeded — clearing radio-offline state "
                "(was_offline=%s, failure_count=%d)",
                was_offline,
                failures,
            )
        if clears:
            self._publish_offline_state()

    def _record_send_inconclusive(self, trial: int = 0) -> None:
        """A measured send finished without showing whether the radio transmits.

        Nothing was sent, or the send reported failure without timing out, so
        neither counter moves; trial *trial* goes back to waiting for the next send.
        """
        with self._offline_lock():
            if self._holds_offline_trial(trial):
                self._radio_offline_trial = 'armed'

    def _allow_offline_trial(self, reason: str) -> None:
        """Arm one trial send while offline; the scheduler's next measured send takes it."""
        with self._offline_lock():
            if not getattr(self, '_radio_offline', False) or getattr(self, '_radio_offline_trial', None) is not None:
                return
            self._radio_offline_trial = 'armed'
        self.logger.info("Radio offline, but %s; the next scheduled send goes out on trial", reason)

    def _clear_radio_offline_state(self, expected_generation: int | None = None) -> bool:
        """Leave radio-offline state entirely (counter, trial, alert latch, banner).

        With *expected_generation*, only if that outage is still the current
        one; returns whether anything was cleared.
        """
        with self._offline_lock():
            if expected_generation is not None and not (
                getattr(self, '_radio_offline', False)
                and getattr(self, '_radio_offline_generation', 0) == expected_generation
            ):
                return False
            self._reset_offline_state_locked()
        self._publish_offline_state()
        return True

    def _reset_offline_state_locked(self) -> None:
        """Leave radio-offline state in memory; the caller holds ``_offline_lock`` and publishes."""
        self._radio_offline = False
        self._radio_offline_trial = None
        self._radio_offline_alerted = False
        self._radio_offline_since = ''
        self._send_consecutive_failures = 0
        self._radio_offline_generation = getattr(self, '_radio_offline_generation', 0) + 1

    def _publish_offline_state(self) -> None:
        """Write the current offline state to bot_metadata for the web viewer.

        Serialized, and always writes the state as it is now rather than as a
        caller saw it, so a slow writer cannot overwrite a newer transition.
        While offline, the stored 'true' is read back (set_metadata swallows its
        own errors) and confirmed for this outage's generation; the viewer-clear
        check only trusts a stored 'false' after that.
        """
        with self._offline_publish_lock():
            with self._offline_lock():
                offline = bool(getattr(self, '_radio_offline', False))
                since = getattr(self, '_radio_offline_since', '') if offline else ''
                generation = getattr(self, '_radio_offline_generation', 0)
            try:
                self.db_manager.set_metadata('bot.radio_offline', 'true' if offline else 'false')
                self.db_manager.set_metadata('bot.radio_offline_since', since)
                confirmed = offline and self.db_manager.get_metadata('bot.radio_offline') == 'true'
            except Exception:
                confirmed = False
            if confirmed:
                with self._offline_lock():
                    if getattr(self, '_radio_offline_generation', 0) == generation:
                        self._radio_offline_persisted_generation = generation

    def _radio_offline_sync_due(self) -> bool:
        """True (and starts the 30 s throttle) when the viewer-clear check should run."""
        if not getattr(self, '_radio_offline', False):
            return False
        now = time.time()
        if now - getattr(self, '_last_offline_metadata_check', 0.0) < 30:
            return False
        self._last_offline_metadata_check = now
        return True

    def _sync_radio_offline_from_metadata(self, *, check_due: bool = True) -> None:
        """Honor the web viewer's "Clear Offline Flag", and retry an unconfirmed write.

        The viewer runs in its own process and can only write bot_metadata, so
        the health loop checks here (at most every 30 s) whether it stored
        'false'. Only once this process has confirmed its own 'true' for the
        current outage, so a read that races the bot's own trip cannot cancel
        it; until then, it retries that write. This blocks on the database, so
        the health loop runs it in a worker thread.
        """
        if check_due and not self._radio_offline_sync_due():
            return
        with self._offline_publish_lock():
            with self._offline_lock():
                generation = getattr(self, '_radio_offline_generation', 0)
                confirmed = getattr(self, '_radio_offline_persisted_generation', None) == generation
            if not confirmed:
                self._publish_offline_state()
                return
            try:
                cleared = self.db_manager.get_metadata('bot.radio_offline') == 'false'
            except Exception as e:
                self.logger.debug(f"Could not read radio-offline metadata: {e}")
                return
            if cleared and self._clear_radio_offline_state(expected_generation=generation):
                self.logger.info("Cleared radio-offline state: cleared from the web viewer")
