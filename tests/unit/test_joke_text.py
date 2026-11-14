"""modules.joke_text: fetch-until-short, split, and two-part send."""

from unittest.mock import AsyncMock, Mock, patch

from modules.joke_text import JOKE_CHAR_LIMIT, fetch_fitting_joke, send_joke, split_joke_text

LONG = "x" * (JOKE_CHAR_LIMIT + 5)


async def test_fetch_retries_until_short_unless_long_allowed():
    fetch = AsyncMock(side_effect=[{"t": LONG}, {"t": "short"}])
    fmt = lambda d: d["t"]  # noqa: E731
    assert await fetch_fitting_joke(fetch, fmt, allow_long=False, logger=Mock(), label="joke") == {"t": "short"}
    fetch = AsyncMock(return_value={"t": LONG})
    assert await fetch_fitting_joke(fetch, fmt, allow_long=True, logger=Mock(), label="joke") == {"t": LONG}
    assert fetch.await_count == 1


async def test_fetch_gives_up_with_the_last_long_joke_or_none():
    logger = Mock()
    fetch = AsyncMock(return_value={"t": LONG})
    got = await fetch_fitting_joke(fetch, lambda d: d["t"], allow_long=False, logger=logger, label="dad joke")
    assert got == {"t": LONG} and fetch.await_count == 5
    logger.warning.assert_called_once_with("Could not get short dad joke after 5 attempts")
    assert await fetch_fitting_joke(AsyncMock(return_value=None), str, allow_long=False, logger=logger, label="j") is None


def test_split_prefers_listed_points_then_middle_space():
    assert split_joke_text("🎭 Why? Because.", "🎭", ("? ",)) == ["🎭 Why? ", "🎭 Because."]
    assert split_joke_text("🥸 aaaa bbbb cccc", "🥸", ()) == ["🥸 aaaa bbbb", "🥸 cccc"]


async def test_send_splits_long_jokes_and_pauses_only_when_asked():
    send = AsyncMock()
    long_joke = "🎭 " + "a" * 100 + ". " + "b" * 100
    with patch("modules.joke_text.asyncio.sleep", AsyncMock()) as sleep:
        await send_joke(send, "msg", long_joke, lambda t: split_joke_text(t, "🎭", (". ",)), pause_seconds=2.0)
        sleep.assert_awaited_once_with(2.0)
    assert send.await_count == 2
    assert send.await_args_list[1].kwargs == {"skip_user_rate_limit": True}
    send = AsyncMock()
    with patch("modules.joke_text.asyncio.sleep", AsyncMock()) as sleep:
        await send_joke(send, "msg", "🥸 short", lambda t: [t])
        sleep.assert_not_awaited()
    send.assert_awaited_once_with("msg", "🥸 short")
