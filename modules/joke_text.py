"""Length handling shared by the joke-style commands (joke, dadjoke).

Both fetch a joke from an API, retry for a short one unless long jokes are
allowed, and split a long one into two messages at a natural break. Lengths are
counted in characters against a fixed limit, as these commands always have.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Optional

JOKE_CHAR_LIMIT = 130
MAX_FETCH_ATTEMPTS = 5


async def fetch_fitting_joke(
    fetch: Callable[[], Awaitable[Optional[dict[str, Any]]]],
    format_joke: Callable[[dict[str, Any]], str],
    *,
    allow_long: bool,
    logger: Any,
    label: str,
) -> Optional[dict[str, Any]]:
    """Fetch jokes until one fits ``JOKE_CHAR_LIMIT`` (or any, when long ones are allowed).

    Returns None when a fetch fails; after ``MAX_FETCH_ATTEMPTS`` long jokes,
    returns the last one anyway.
    """
    joke_data = None
    for _attempt in range(MAX_FETCH_ATTEMPTS):
        joke_data = await fetch()
        if joke_data is None:
            return None
        joke_text = format_joke(joke_data)
        if len(joke_text) <= JOKE_CHAR_LIMIT or allow_long:
            return joke_data
        logger.debug(f"{label.capitalize()} too long ({len(joke_text)} chars), fetching another...")
    logger.warning(f"Could not get short {label} after {MAX_FETCH_ATTEMPTS} attempts")
    return joke_data


def split_joke_text(joke_text: str, emoji: str, split_points: Sequence[str]) -> list[str]:
    """Split ``joke_text`` (which starts with ``"<emoji> "``) into two parts.

    Splits after the first of ``split_points`` found, else at the first space
    from the middle; both parts get the emoji prefix back.
    """
    prefix = f"{emoji} "
    clean_joke = joke_text[len(emoji) + 1:] if joke_text.startswith(prefix) else joke_text
    for split_point in split_points:
        if split_point in clean_joke:
            parts = clean_joke.split(split_point, 1)
            if len(parts) == 2:
                return [f"{prefix}{parts[0]}{split_point}", f"{prefix}{parts[1]}"]
    mid_point = len(clean_joke) // 2
    for i in range(mid_point, len(clean_joke)):
        if clean_joke[i] == ' ':
            mid_point = i
            break
    return [f"{prefix}{clean_joke[:mid_point]}", f"{prefix}{clean_joke[mid_point + 1:]}"]


async def send_joke(
    send: Callable[..., Awaitable[Any]],
    message: Any,
    joke_text: str,
    split: Callable[[str], list[str]],
    *,
    pause_seconds: float = 0.0,
) -> None:
    """Send a joke, as two messages when it is long and splits cleanly.

    The per-user rate limit applies to the first message only. A joke that
    does not split into two parts within the limit goes out whole.
    """
    if len(joke_text) <= JOKE_CHAR_LIMIT:
        await send(message, joke_text)
        return
    parts = split(joke_text)
    if len(parts) == 2 and len(parts[0]) <= JOKE_CHAR_LIMIT and len(parts[1]) <= JOKE_CHAR_LIMIT:
        await send(message, parts[0])
        if pause_seconds:
            await asyncio.sleep(pause_seconds)
        await send(message, parts[1], skip_user_rate_limit=True)
    else:
        await send(message, joke_text)
