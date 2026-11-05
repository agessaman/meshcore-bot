"""Nominatim names places in the bot's [Localization] language, and reverse lookups are cached per language."""

import configparser
from unittest.mock import Mock, patch

import pytest

from modules import utils
from modules.utils import (
    _nominatim_language,
    _reverse_cache_key,
    rate_limited_nominatim_geocode,
    rate_limited_nominatim_geocode_sync,
    rate_limited_nominatim_reverse,
    rate_limited_nominatim_reverse_sync,
)


def _bot(language=None, limiter=True, place_names=None):
    config = configparser.ConfigParser()
    localization = {}
    if language is not None:
        localization["language"] = language
    if place_names is not None:
        localization["place_name_language"] = place_names
    if localization:
        config.read_dict({"Localization": localization})
    bot = Mock(spec=["config", "nominatim_rate_limiter"] if limiter else ["config"])
    bot.config = config
    if limiter:
        bot.nominatim_rate_limiter = Mock()
        bot.nominatim_rate_limiter.wait_and_request = Mock(side_effect=lambda: _done())
    return bot


async def _done():
    return None


@pytest.mark.parametrize(("language", "expected"), [
    (None, "en"),          # no [Localization] section
    ("de", "de,en"),
    ("en-GB", "en-GB"),
    (" pt-BR ", "pt-BR,en"),
    ("zh_Hant", "zh_Hant,en"),
    ("", "en"),
    ("en;q=0.5,de", "en"),  # not a language tag: never forwarded to Nominatim
])
def test_language_comes_from_localization(language, expected):
    assert _nominatim_language(_bot(language)) == expected


@pytest.mark.parametrize(("language", "place_names", "expected"), [
    ("de", "", "de,en"),        # empty follows [Localization] language
    ("de", "local", False),     # each place's own name, as before
    ("de", " LOCAL ", False),
    ("de", "en", "en"),
    ("en", "fr", "fr,en"),
    ("de", "not a tag!", "de,en"),
])
def test_place_name_language_overrides(language, place_names, expected):
    assert _nominatim_language(_bot(language, place_names=place_names)) == expected


def test_local_names_reuse_the_cache_key_from_before():
    assert _reverse_cache_key(_bot("de", place_names="local"), 35.68, 139.76) == "reverse_35.68_139.76"


def test_language_without_a_usable_config():
    assert _nominatim_language(object()) == "en"
    bot = Mock()  # config.get returns a Mock, not a string
    assert _nominatim_language(bot) == "en"


def test_reverse_cache_key_includes_the_language():
    assert _reverse_cache_key(_bot("de"), 35.68, 139.76) == "reverse_de,en_35.68_139.76"
    assert _reverse_cache_key(_bot(), 35.68, 139.76) == "reverse_en_35.68_139.76"


@pytest.mark.parametrize("limiter", [True, False])
def test_sync_lookups_pass_the_language(limiter):
    geolocator = Mock()
    bot = _bot("fr", limiter=limiter)
    with patch.object(utils, "get_nominatim_geocoder", return_value=geolocator):
        rate_limited_nominatim_geocode_sync(bot, "Tokyo", timeout=5)
        rate_limited_nominatim_reverse_sync(bot, "35.68, 139.76", timeout=5)
    geolocator.geocode.assert_called_once_with("Tokyo", timeout=5, language="fr,en")
    geolocator.reverse.assert_called_once_with("35.68, 139.76", timeout=5, language="fr,en")


@pytest.mark.asyncio
@pytest.mark.parametrize("limiter", [True, False])
async def test_async_lookups_pass_the_language(limiter):
    geolocator = Mock()
    bot = _bot("fr", limiter=limiter)
    with patch.object(utils, "get_nominatim_geocoder", return_value=geolocator):
        await rate_limited_nominatim_geocode(bot, "Tokyo", timeout=5)
        await rate_limited_nominatim_reverse(bot, "35.68, 139.76", timeout=5)
    geolocator.geocode.assert_called_once_with("Tokyo", timeout=5, language="fr,en")
    geolocator.reverse.assert_called_once_with("35.68, 139.76", timeout=5, language="fr,en")


def test_a_cached_address_from_before_is_not_reused():
    """Addresses cached under the old language-less key were in each place's local language."""
    from modules.location import _address_from_result

    bot = _bot("en")
    cache = {"reverse_35.68_139.76": {"city": "千代田区", "country_code": "jp"}}
    bot.db_manager = Mock()
    bot.db_manager.get_cached_json = Mock(side_effect=lambda key, _kind: cache.get(key))
    reverse = Mock()
    reverse.raw = {"address": {"city": "Chiyoda", "country_code": "jp"}}
    with patch("modules.location.rate_limited_nominatim_reverse_sync", return_value=reverse) as lookup:
        address = _address_from_result(bot, 35.68, 139.76, timeout=5)
    lookup.assert_called_once()
    assert address["city"] == "Chiyoda"
    bot.db_manager.cache_json.assert_called_once()
    assert bot.db_manager.cache_json.call_args.args[0] == "reverse_en_35.68_139.76"
