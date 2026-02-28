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
from typing import Any, Dict, List, Optional

from meshcore import EventType


@dataclass
class RunTraceResult:
    """Result of running a trace."""

    success: bool
    tag: int
    path_nodes: List[Dict[str, Any]] = field(default_factory=list)
    path_len: int = 0
    flags: int = 0
    error_message: Optional[str] = None


def _get_timeout_seconds(bot: Any, path: Optional[List[str]]) -> float:
    """Compute total timeout from path length and config."""
    per_hop = bot.config.getfloat("Trace_Command", "timeout_per_hop_seconds", fallback=0.5)
    base = bot.config.getfloat("Trace_Command", "timeout_base_seconds", fallback=1.0)
    hops = len(path) if path else 0
    # Typical: ~1s for 6 hops, ~2s for 10 hops; base + per-hop gives margin
    total = base + max(1, hops) * per_hop
    return total


async def _run_trace_attempt(
    bot: Any,
    path: Optional[List[str]],
    path_string: Optional[str],
    flags: int,
    timeout_seconds: float,
    tag: int,
) -> RunTraceResult:
    """Execute one trace attempt: send_trace then wait for TRACE_DATA. Caller handles retries."""
    try:
        if hasattr(bot, "transmission_tracker") and bot.transmission_tracker:
            bot.transmission_tracker.record_transmission(
                content="trace",
                target="",
                message_type="trace",
                command_id=str(tag),
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
        return RunTraceResult(success=False, tag=tag, error_message=str(e))

    if result.type == EventType.ERROR:
        reason = result.payload.get("reason", "unknown error")
        return RunTraceResult(success=False, tag=tag, error_message=reason)

    path_str = ",".join(path) if path else "(flood)"
    try:
        event = await bot.meshcore.wait_for_event(
            EventType.TRACE_DATA,
            attribute_filters={"tag": tag},
            timeout=timeout_seconds,
        )
    except Exception as e:
        return RunTraceResult(
            success=False,
            tag=tag,
            error_message=f"Timeout or error waiting for trace (path: {path_str}): {e}",
        )

    if not event:
        return RunTraceResult(
            success=False,
            tag=tag,
            error_message=f"No trace response within timeout (path: {path_str})",
        )

    payload = event.payload or {}
    path_nodes = payload.get("path") or []
    path_len = payload.get("path_len", 0)
    flags_val = payload.get("flags", 0)
    return RunTraceResult(
        success=True,
        tag=tag,
        path_nodes=path_nodes,
        path_len=path_len,
        flags=flags_val,
    )


async def run_trace(
    bot: Any,
    path: Optional[List[str]] = None,
    flags: int = 0,
    timeout_seconds: Optional[float] = None,
) -> RunTraceResult:
    """
    Send a trace and wait for TRACE_DATA. Retries on failure per config (default 2 attempts, 1s delay).

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
    last_result: Optional[RunTraceResult] = None

    for attempt in range(max_attempts):
        tag = random.randint(1, 0xFFFFFFFF)
        if attempt > 0:
            bot.logger.debug("Trace retry %s/%s after %.1fs delay", attempt + 1, max_attempts, retry_delay)
            await asyncio.sleep(retry_delay)
        bot.logger.debug(
            "Trace: path=%s hops=%s timeout=%.1fs tag=%s attempt=%s/%s",
            path_str_debug,
            len(path) if path else 0,
            timeout_seconds,
            tag,
            attempt + 1,
            max_attempts,
        )
        last_result = await _run_trace_attempt(
            bot, path, path_string, flags, timeout_seconds, tag
        )
        if last_result.success:
            return last_result

    return last_result or RunTraceResult(
        success=False,
        tag=0,
        error_message="Trace failed (no attempts run)",
    )
