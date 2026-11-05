"""geocode_city and geocode_city_sync share one body but keep their drifted rule.

The async variant always reverse-geocodes an unqualified city's first hit to
decide whether it is an obscure place in the default country; the sync variant
only does so when address info was requested. Unifying them is a behavior
change and belongs in its own PR.
"""

import asyncio
import configparser
from unittest.mock import MagicMock, patch

import modules.utils as utils


def _bot():
    bot = MagicMock()
    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.set("Weather", "default_state", "WA")
    bot.config = config
    bot.db_manager.get_cached_geocoding.return_value = (None, None)
    bot.db_manager.get_cached_json.return_value = None
    return bot


def _location(lat, lon):
    loc = MagicMock()
    loc.latitude, loc.longitude = lat, lon
    return loc


def _fakes(calls):
    # "Snoqualmie" alone resolves to an obscure US township; with the default
    # state it resolves to the real city.
    def geocode(bot, query, timeout=10):
        calls.append(("geocode", query))
        return _location(1.0, 1.0) if query == "Snoqualmie" else _location(2.0, 2.0)

    def reverse(bot, coords, timeout=10):
        calls.append(("reverse", coords))
        result = MagicMock()
        result.raw = {"address": {"country": "United States", "country_code": "us",
                                  "town": "Snoqualmie Township", "type": "township"}}
        return result

    async def ageocode(bot, query, timeout=10):
        return geocode(bot, query, timeout)

    async def areverse(bot, coords, timeout=10):
        return reverse(bot, coords, timeout)

    return geocode, reverse, ageocode, areverse


def _patched(calls):
    geocode, reverse, ageocode, areverse = _fakes(calls)
    return (
        patch.object(utils, "rate_limited_nominatim_geocode_sync", geocode),
        patch.object(utils, "rate_limited_nominatim_reverse_sync", reverse),
        patch.object(utils, "rate_limited_nominatim_geocode", ageocode),
        patch.object(utils, "rate_limited_nominatim_reverse", areverse),
    )


def test_async_skips_an_obscure_first_hit_without_address_info():
    calls = []
    p1, p2, p3, p4 = _patched(calls)
    with p1, p2, p3, p4:
        lat, lon, info = asyncio.run(utils.geocode_city(_bot(), "Snoqualmie"))
    assert (lat, lon, info) == (2.0, 2.0, None)
    assert calls[:2] == [("geocode", "Snoqualmie"), ("reverse", "1.0, 1.0")]


def test_sync_takes_the_first_hit_without_address_info():
    calls = []
    p1, p2, p3, p4 = _patched(calls)
    with p1, p2, p3, p4:
        lat, lon, info = utils.geocode_city_sync(_bot(), "Snoqualmie")
    assert (lat, lon, info) == (1.0, 1.0, None)
    assert calls == [("geocode", "Snoqualmie")]


def test_sync_checks_the_first_hit_when_address_info_is_requested():
    calls = []
    p1, p2, p3, p4 = _patched(calls)
    with p1, p2, p3, p4:
        lat, lon, _ = utils.geocode_city_sync(_bot(), "Snoqualmie", include_address_info=True)
    assert (lat, lon) == (2.0, 2.0)
    assert calls[1] == ("reverse", "1.0, 1.0")


def test_geocoder_errors_reach_the_shared_body_as_they_would_a_direct_call():
    bot = _bot()

    def failing(bot, query, timeout=10):
        raise RuntimeError("nominatim down")

    with patch.object(utils, "rate_limited_nominatim_geocode_sync", failing):
        assert utils.geocode_zipcode_sync(bot, "98101") == (None, None)
    bot.logger.error.assert_called_once()
    assert "nominatim down" in bot.logger.error.call_args.args[0]
