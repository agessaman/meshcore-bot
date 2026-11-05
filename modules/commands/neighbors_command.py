#!/usr/bin/env python3
"""
Neighbors command for the MeshCore Bot
Triggers one zero-hop neighbor discovery cycle on demand
"""

import asyncio
import math
import time
from typing import Any, Optional

from ..models import MeshMessage
from .base_command import BaseCommand

# How long a refused sender must wait before retrying. Matches the default
# discover window: long enough that a busy cycle is usually done, short enough
# that "busy" / "disabled" is not a fifteen-minute lockout.
_REFUSAL_RETRY_SECONDS = 60.0


class NeighborsCommand(BaseCommand):
    """Runs one neighbor discovery cycle immediately.

    The scheduled interval has a 12 hour floor (the firmware's band), which makes
    waiting for the scheduler impractical when testing or after moving the node.
    This is the bot's equivalent of the firmware's ``discover.neighbors``.

    A cycle takes at least ``neighbors_discover_window`` seconds — 60 by default —
    so this acknowledges immediately and reports the result in a second message
    rather than holding the reply open.

    Two cooldowns apply. ``cooldown_seconds`` is this sender's own, from the base
    class. The node-wide one belongs to the service (``MIN_CYCLE_GAP_SECONDS``),
    because airtime is spent by whichever trigger asks — the scheduler included —
    and it is only *reported* here, so the reply can say how long is left instead
    of failing opaquely.
    """

    # Plugin metadata
    name = "neighbors"
    enabled_attr = "command_enabled"
    keywords = ['neighbors', 'neighbours']
    description = "Runs a zero-hop neighbor discovery cycle (DM only)"
    requires_dm = True
    cooldown_seconds = 900  # 15 minutes, per sender *and* per node; see above
    category = "special"

    def __init__(self, bot: Any):
        """Initialize the neighbors command.

        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.command_enabled = self.get_config_value(
            'Neighbors_Command', 'enabled', fallback=True, value_type='bool'
        )
        # Tracked so a second invocation cannot start an overlapping *report*
        # task. Overlapping cycles are refused by the service itself, which is
        # where the scheduler's own trigger is also visible.
        self._cycle_task: Optional[asyncio.Task] = None

    def get_help_text(self, message: MeshMessage | None = None) -> str:
        """Get help text for the neighbors command.

        Returns:
            str: The help text for this command.
        """
        return self.translate('commands.neighbors.description')

    def _get_capture_service(self) -> Any:
        """The packet capture service instance, or None when unavailable."""
        service = getattr(self.bot, 'packet_capture_service', None)
        if service is not None:
            return service
        # The alias is set up at init; fall back to the service registry in case
        # the service was loaded but not aliased.
        services = getattr(self.bot, 'services', None) or {}
        try:
            return services.get('packetcapture')
        except AttributeError:
            return None

    def _shared_cooldown_remaining(self, service: Any) -> float:
        """Seconds left before *any* sender may trigger another cycle.

        The service owns this rule — it applies to the scheduler and to every
        other trigger too, and it would refuse the cycle regardless. Asking it
        here only buys a clearer reply than a generic failure summary, so a
        service that cannot answer is treated as "no wait known" and left to
        refuse for itself.
        """
        remaining = getattr(service, 'neighbors_cooldown_remaining', None)
        if not callable(remaining):
            return 0.0
        try:
            return float(remaining())
        except Exception as e:
            self.logger.debug(f"Neighbors: could not read the shared cooldown: {e}")
            return 0.0

    def _busy_retry_seconds(self, service: Any) -> float:
        """How long a 'busy' refusal should hold this sender's personal cooldown.

        Aligns with the discover window when known: by then a normal cycle has
        finished listening, so a retry is meaningful.
        """
        cfg = getattr(service, 'neighbors_config', None)
        window = getattr(cfg, 'discover_window', None)
        if isinstance(window, (int, float)) and window > 0:
            return float(window)
        return _REFUSAL_RETRY_SECONDS

    def _yield_user_cooldown(self, user_id: Optional[str], remaining: float) -> None:
        """Rewind this sender's own cooldown to expire after *remaining* seconds.

        The command manager records the execution *before* calling execute(), so a
        request refused in here has already spent the sender's 15 minutes. Without
        this, "wait 1 more minute" (or "busy, try again shortly") would be a lie:
        retrying when ready would be refused for another fourteen minutes by their
        personal cooldown.

        Rewound rather than cleared, so the refusal itself cannot be spammed — a
        DM reply is airtime too.
        """
        if not user_id or self.cooldown_seconds <= 0:
            return
        spent = max(0.0, self.cooldown_seconds - remaining)
        self._user_cooldowns[user_id] = time.time() - spent

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the neighbors command.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if handled (including the error, busy and cooldown notices).
        """
        service = self._get_capture_service()
        if service is None or not getattr(service, 'neighbors_enabled', False):
            # Same yield as other refusals: the manager already recorded a full
            # personal cooldown, and "disabled" must not become a 15-minute lockout
            # after the operator turns the feature on.
            self._yield_user_cooldown(message.sender_id, _REFUSAL_RETRY_SECONDS)
            await self.send_response(message, self.translate('commands.neighbors.disabled'))
            return True

        if self._cycle_task is not None and not self._cycle_task.done():
            self._yield_user_cooldown(message.sender_id, self._busy_retry_seconds(service))
            await self.send_response(message, self.translate('commands.neighbors.busy'))
            return True

        # The service refuses an overlapping cycle on its own; this only decides
        # what the requester is told, and rations airtime across senders.
        if getattr(service, 'neighbors_cycle_active', False):
            self._yield_user_cooldown(message.sender_id, self._busy_retry_seconds(service))
            await self.send_response(message, self.translate('commands.neighbors.busy'))
            return True

        remaining = self._shared_cooldown_remaining(service)
        if remaining > 0:
            self.logger.debug(
                f"Neighbors: refusing {message.sender_id}'s request, "
                f"{remaining:.0f}s left on the shared cooldown"
            )
            self._yield_user_cooldown(message.sender_id, remaining)
            await self.send_response(
                message,
                self.translate('commands.neighbors.cooldown_active',
                               minutes=max(1, math.ceil(remaining / 60))),
            )
            return True

        cfg = service.neighbors_config
        self.logger.info(f"User {message.sender_id} requested a neighbors discovery cycle")

        # Claim before acknowledging. send_response awaits, and without the lock
        # the scheduler can start a cycle in that gap — the user would then get
        # "started" followed by "failed: already running".
        claimed = False
        claim = getattr(service, 'claim_neighbors_cycle', None)
        if callable(claim):
            refusal = claim()
            if refusal is not None:
                if "already" in refusal:
                    self._yield_user_cooldown(
                        message.sender_id, self._busy_retry_seconds(service)
                    )
                    await self.send_response(
                        message, self.translate('commands.neighbors.busy')
                    )
                else:
                    left = self._shared_cooldown_remaining(service) or _REFUSAL_RETRY_SECONDS
                    self._yield_user_cooldown(message.sender_id, left)
                    await self.send_response(
                        message,
                        self.translate(
                            'commands.neighbors.cooldown_active',
                            minutes=max(1, math.ceil(left / 60)),
                        ),
                    )
                return True
            claimed = True

        try:
            await self.send_response(
                message,
                self.translate('commands.neighbors.started', seconds=int(cfg.discover_window)),
            )
        except Exception:
            release = getattr(service, 'release_neighbors_cycle', None)
            if claimed and callable(release):
                release()
            raise

        # Run detached so the discover window does not hold the command open, and
        # bound it so a stalled radio link cannot leave the task alive forever.
        self._cycle_task = asyncio.create_task(
            self._run_and_report(
                message, service, cfg.cycle_budget, already_claimed=claimed
            )
        )
        return True

    async def _run_and_report(
        self,
        message: MeshMessage,
        service: Any,
        budget: float,
        *,
        already_claimed: bool = False,
    ) -> None:
        """Run one cycle and DM the outcome."""
        try:
            summary = await asyncio.wait_for(
                service.run_neighbors_cycle(already_claimed=already_claimed),
                timeout=budget,
            )
        except asyncio.TimeoutError:
            self.logger.error(f"Neighbors: manual cycle exceeded {budget:.0f}s and was abandoned")
            await self.send_response(
                message,
                self.translate('commands.neighbors.error',
                               error=f"timed out after {budget:.0f}s"),
                skip_user_rate_limit=True,
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.error(f"Neighbors: manual cycle failed: {e}", exc_info=True)
            await self.send_response(
                message,
                self.translate('commands.neighbors.error', error=str(e)),
                skip_user_rate_limit=True,
            )
            return

        await self.send_response(
            message, self._format_summary(summary), skip_user_rate_limit=True
        )

    def _format_summary(self, summary: dict[str, Any]) -> str:
        """Render a cycle summary short enough for a mesh DM."""
        if not summary.get('ok'):
            reason = summary.get('reason') or 'unknown error'
            return self.translate('commands.neighbors.failed', reason=reason)

        found = summary.get('queried', 0)
        if not found:
            return self.translate('commands.neighbors.none')

        best = summary.get('best_snr')
        best_text = f"{best:.1f}dB" if isinstance(best, (int, float)) else "n/a"
        text = self.translate(
            'commands.neighbors.success',
            count=found,
            best_snr=best_text,
            recorded=summary.get('recorded', 0),
        )
        # Only mention brokers when at least one was actually tried, so an
        # operator with no MQTT does not see a confusing "0/0".
        if summary.get('attempted'):
            text += " " + self.translate(
                'commands.neighbors.published',
                succeeded=summary.get('succeeded', 0),
                attempted=summary.get('attempted', 0),
            )
        return text
