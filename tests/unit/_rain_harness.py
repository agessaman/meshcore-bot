#!/usr/bin/env python3
"""Shared scaffolding for the rain/snow command tests (not a test module).

The leading underscore keeps pytest from collecting this file. It builds a
RainCommand wired to a real ConfigParser + real i18n Translator, captures the
reply at bot.command_manager.send_response, and (optionally) stubs the two
network seams so renders are deterministic. Imported by test_rain_command_e2e
(mocked weather) and test_rain_live_smoke (real Open-Meteo).
"""

import asyncio
import configparser
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from modules.commands.rain_command import RainCommand
from modules.i18n import Translator
from modules.models import MeshMessage

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSLATIONS = str(REPO_ROOT / "translations")
NOW = "2026-06-03T14:00"
DEFAULT_LABEL = "Nashville, TN"
DEFAULT_COORDS = (36.1627, -86.7816)


def make_series(
    *, n=9, step=15, now=NOW,
    precip=None, codes=None, snow=None, prob=None, temp=None,
    current_precip=0.0, current_code=0,
):
    """Build the dict shape fetch_precip_series() returns. Lists shorter than n
    are zero/None-padded; temp defaults to a mild 18 C (no borderline tag)."""
    base = datetime.fromisoformat(now)
    times = [(base + timedelta(minutes=step * i)).isoformat(timespec="minutes") for i in range(n)]

    def pad(seq, fill):
        seq = list(seq if seq is not None else [])
        return (seq + [fill] * (n - len(seq)))[:n]

    return {
        "times": times,
        "precip": pad(precip, 0.0),
        "codes": pad(codes, 0),
        "snow": pad(snow, 0.0),
        "prob": pad(prob, 0),
        "temp": pad(temp, 18.0),
        "now": now,
        "current_precip": current_precip,
        "current_code": current_code,
        "step": step,
    }


def make_bot(*, bot_name="WeatherBot-V3", rain_overrides=None):
    """A minimal bot with a real config + real translator and a capturing
    command_manager.send_response. Returns (bot, captured_responses_list).

    A SimpleNamespace (not a Mock) is deliberate: get_max_message_length checks
    hasattr(bot, 'meshcore'), and a Mock would answer True to everything.
    """
    cfg = configparser.ConfigParser()
    cfg.add_section("Bot")
    cfg.set("Bot", "bot_name", bot_name)
    cfg.add_section("Channels")
    cfg.set("Channels", "monitor_channels", "general")
    cfg.set("Channels", "respond_to_dms", "true")
    cfg.add_section("Rain_Command")
    cfg.set("Rain_Command", "enabled", "true")
    for key, val in (rain_overrides or {}).items():
        cfg.set("Rain_Command", key, val)

    captured: list[str] = []

    async def _send_response(message, content, **kwargs):
        captured.append(content)
        return True

    async def _send_response_chunked(message, chunks, **kwargs):
        captured.extend(chunks)
        return True

    command_manager = SimpleNamespace(
        send_response=_send_response,
        send_response_chunked=_send_response_chunked,
        monitor_channels=["general"],
    )
    bot = SimpleNamespace(
        logger=Mock(),
        config=cfg,
        translator=Translator(language="en", translation_path=TRANSLATIONS),
        command_manager=command_manager,
    )
    return bot, captured


def build_cmd(
    series=None, *, coords=DEFAULT_COORDS, label=DEFAULT_LABEL,
    rain_overrides=None, bot_name="WeatherBot-V3",
):
    """RainCommand wired to a fixed resolved location. When `series` is given,
    the Open-Meteo fetch is stubbed to it; when None, the real fetch runs
    (used by the live smoke). Geocoding is always bypassed for determinism."""
    bot, captured = make_bot(bot_name=bot_name, rain_overrides=rain_overrides)
    cmd = RainCommand(bot)
    if series is not None:
        cmd._fetch_series = lambda lat, lon: series
    cmd._resolve_location = lambda message, location: (coords[0], coords[1], location or label, None)
    return cmd, captured


def render(cmd, captured, content, *, is_dm=False):
    """Run execute() once and return the single captured reply, asserting it
    fits the real channel/DM byte budget."""
    msg = MeshMessage(
        content=content,
        channel=None if is_dm else "general",
        is_dm=is_dm,
        sender_id="U1",
    )
    ok = asyncio.run(cmd.execute(msg))
    assert ok is True
    assert len(captured) == 1, f"expected one reply, got {captured!r}"
    resp = captured[-1]
    budget = cmd.get_max_message_length(msg)
    assert len(resp.encode("utf-8")) <= budget, f"{len(resp.encode('utf-8'))}B > {budget}B: {resp!r}"
    return resp


def assert_render(resp, pattern):
    assert re.fullmatch(pattern, resp), f"\nGOT:      {resp!r}\nEXPECTED: /{pattern}/"
