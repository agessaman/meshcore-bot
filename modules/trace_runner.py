#!/usr/bin/env python3
"""
Trace runner for MeshCore Bot.
Runs send_trace via MeshCore_py, waits for TRACE_DATA, and returns a structured result.
Supports configurable retries with delay between attempts. Shared by the trace command
and future automated mesh tracing service.
"""

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any, Optional

from meshcore import EventType


@dataclass
class RunTraceResult:
    """Result of running a trace."""

    success: bool
    tag: int
    path_nodes: list[dict[str, Any]] = field(default_factory=list)
    path_len: int = 0
    flags: int = 0
    error_message: Optional[str] = None


def _get_timeout_seconds(bot: Any, path: Optional[list[str]]) -> float:
    """Compute total timeout from path length and config."""
    per_hop = bot.config.getfloat("Trace_Command", "timeout_per_hop_seconds", fallback=1.5)
    base = bot.config.getfloat("Trace_Command", "timeout_base_seconds", fallback=2.0)
    hops = len(path) if path else 0
    # Each repeater waits a random TX delay before forwarding, often a second or more per hop
    total = base + max(1, hops) * per_hop
    return total


async def _send_trace_attempt(
    bot: Any,
    path_string: Optional[str],
    flags: int,
    tag: int,
) -> Optional[str]:
    """Record and send one trace. Returns an error message, or None once the trace is sent."""
    try:
        if hasattr(bot, "transmission_tracker") and bot.transmission_tracker:
            bot.transmission_tracker.record_transmission(
                content="trace",
                target="",
                message_type="trace",
                command_id=str(tag),
                trace_tag=tag,
            )
    except Exception as e:
        bot.logger.debug(f"Trace runner: failed to record transmission: {e}")

    try:
        result = await bot.meshcore.commands.send_trace(
            auth_code=0,
            tag=tag,
            flags=flags,
            path=path_string,
        )
    except Exception as e:
        return str(e)

    if result.type == EventType.ERROR:
        return str(result.payload.get("reason", "unknown error"))
    return None


async def _wait_for(arrived: asyncio.Event, timeout: float) -> bool:
    """Wait up to timeout seconds for arrived; True if it was set."""
    try:
        await asyncio.wait_for(arrived.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    return arrived.is_set()


async def run_trace(
    bot: Any,
    path: Optional[list[str]] = None,
    flags: int = 0,
    timeout_seconds: Optional[float] = None,
) -> RunTraceResult:
    """
    Send a trace and wait for TRACE_DATA. Retries on failure per config (default 2 attempts, 1s delay).

    A reply to any attempt counts until the last attempt's timeout runs out, so a slow mesh that
    answers the first trace while the retry is out still succeeds.

    Args:
        bot: MeshCoreBot instance (must have meshcore, config, transmission_tracker).
        path: Optional list of 2-char hex node IDs (e.g. ["01", "7a", "55"]). None = flood.
        flags: 8-bit flags for send_trace (0 = one_byte default).
        timeout_seconds: Override; if None, uses base + (path hops * per_hop) from config.

    Returns:
        RunTraceResult with success, tag, path_nodes (hash/snr), path_len, flags, error_message.
    """
    if not bot.meshcore or not getattr(bot.meshcore, "commands", None):
        return RunTraceResult(
            success=False,
            tag=0,
            error_message="Not connected or send_trace not available",
        )

    path_string = None
    if path:
        path_string = ",".join(p.strip().lower() for p in path if p and len(p.strip()) >= 2)

    if timeout_seconds is None:
        timeout_seconds = _get_timeout_seconds(bot, path)

    max_attempts = max(1, bot.config.getint("Trace_Command", "trace_retry_count", fallback=2))
    retry_delay = max(0.0, bot.config.getfloat("Trace_Command", "trace_retry_delay_seconds", fallback=1.0))

    path_str_debug = path_string if path_string else "(flood)"
    path_str = ",".join(path) if path else "(flood)"

    sent_tags: list[int] = []
    replies: list[tuple[int, Any]] = []
    arrived = asyncio.Event()

    def _on_trace_data(event: Any) -> None:
        attributes = getattr(event, "attributes", None) or {}
        payload = getattr(event, "payload", None) or {}
        tag = attributes.get("tag", payload.get("tag"))
        if tag in sent_tags and not replies:
            replies.append((tag, event))
            arrived.set()

    # Subscribe before the first send so neither a fast reply nor a late one is missed
    subscription = bot.meshcore.subscribe(EventType.TRACE_DATA, _on_trace_data)
    last_error: Optional[str] = None
    last_tag = 0
    try:
        for attempt in range(max_attempts):
            tag = random.randint(1, 0xFFFFFFFF)
            if attempt > 0:
                bot.logger.debug("Trace retry %s/%s after %.1fs delay", attempt + 1, max_attempts, retry_delay)
                if await _wait_for(arrived, retry_delay):
                    break
            bot.logger.debug(
                "Trace: path=%s hops=%s timeout=%.1fs tag=%s attempt=%s/%s",
                path_str_debug,
                len(path) if path else 0,
                timeout_seconds,
                tag,
                attempt + 1,
                max_attempts,
            )
            sent_tags.append(tag)
            last_tag = tag
            error = await _send_trace_attempt(bot, path_string, flags, tag)
            if error is not None:
                last_error = error
                # An earlier attempt's reply can still arrive
                if arrived.is_set():
                    break
                continue
            if await _wait_for(arrived, timeout_seconds):
                break
            last_error = f"No trace response within timeout (path: {path_str})"
    finally:
        try:
            subscription.unsubscribe()
        except Exception as e:
            bot.logger.debug(f"Trace runner: failed to unsubscribe: {e}")

    if replies:
        reply_tag, event = replies[0]
        payload = event.payload or {}
        if reply_tag != last_tag:
            bot.logger.debug(
                "Trace: reply for attempt %s/%s (tag=%s)", sent_tags.index(reply_tag) + 1, max_attempts, reply_tag
            )
        return RunTraceResult(
            success=True,
            tag=reply_tag,
            path_nodes=payload.get("path") or [],
            path_len=payload.get("path_len", 0),
            flags=payload.get("flags", 0),
        )

    return RunTraceResult(
        success=False,
        tag=last_tag,
        error_message=last_error or "Trace failed (no attempts run)",
    )
