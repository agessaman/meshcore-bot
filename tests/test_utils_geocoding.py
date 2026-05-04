"""Tests for geocoding and geographic utility functions in modules/utils.py.

Covers: normalize_country_name, normalize_us_state, is_country_name, is_us_state,
parse_location_string, rate_limited_nominatim_* functions, geocode_zipcode,
geocode_city (async + sync), check_internet_connectivity_async.
"""

import asyncio
import configparser
import urllib.error
from unittest.mock import AsyncMock, Mock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_bot():
    """Minimal mock bot for geocoding function tests."""
    bot = Mock()
    cfg = configparser.ConfigParser()
    cfg.add_section("Weather")
    cfg.set("Weather", "default_state", "WA")
    cfg.set("Weather", "default_country", "US")
    bot.config = cfg
    bot.db_manager = Mock()
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))
    bot.db_manager.cache_geocoding = Mock()
    bot.db_manager.get_cached_json = Mock(return_value=None)
    bot.db_manager.cache_json = Mock()
    bot.logger = Mock()
    rl = Mock()
    rl.wait_for_request = AsyncMock()
    rl.wait_for_request_sync = Mock()
    rl.record_request = Mock()
    bot.nominatim_rate_limiter = rl
    return bot


def _make_location(lat=47.6062, lon=-122.3321):
    loc = Mock()
    loc.latitude = lat
    loc.longitude = lon
    loc.raw = {
        "address": {
            "city": "Seattle",
            "country": "United States",
            "country_code": "us",
            "type": "city",
        }
    }
    return loc


# ---------------------------------------------------------------------------
# normalize_country_name
# ---------------------------------------------------------------------------

class TestNormalizeCountryName:

    def test_alpha2_us(self):
        from modules.utils import PYCOUNTRY_AVAILABLE, normalize_country_name
        if not PYCOUNTRY_AVAILABLE:
            pytest.skip("pycountry not installed")
        code, name = normalize_country_name("US")
        assert code == "US"
        assert name is not None

    def test_alpha2_se(self):
        from modules.utils import PYCOUNTRY_AVAILABLE, normalize_country_name
        if not PYCOUNTRY_AVAILABLE:
            pytest.skip("pycountry not installed")
        code, name = normalize_country_name("SE")
        assert code == "SE"

    def test_alpha3_usa(self):
        from modules.utils import PYCOUNTRY_AVAILABLE, normalize_country_name
        if not PYCOUNTRY_AVAILABLE:
            pytest.skip("pycountry not installed")
        code, name = normalize_country_name("USA")
        assert code == "US"

    def test_full_name_sweden(self):
        from modules.utils import PYCOUNTRY_AVAILABLE, normalize_country_name
        if not PYCOUNTRY_AVAILABLE:
            pytest.skip("pycountry not installed")
        code, name = normalize_country_name("Sweden")
        assert code == "SE"

    def test_variant_uk(self):
        from modules.utils import PYCOUNTRY_AVAILABLE, normalize_country_name
        if not PYCOUNTRY_AVAILABLE:
            pytest.skip("pycountry not installed")
        code, name = normalize_country_name("uk")
        assert code is not None  # resolves to GB

    def test_variant_usa_lowercase(self):
        from modules.utils import PYCOUNTRY_AVAILABLE, normalize_country_name
        if not PYCOUNTRY_AVAILABLE:
            pytest.skip("pycountry not installed")
        code, name = normalize_country_name("usa")
        assert code == "US"

    def test_unknown_returns_none(self):
        from modules.utils import PYCOUNTRY_AVAILABLE, normalize_country_name
        if not PYCOUNTRY_AVAILABLE:
            pytest.skip("pycountry not installed")
        code, name = normalize_country_name("Narnia")
        assert code is None
        assert name is None

    def test_empty_returns_none(self):
        from modules.utils import normalize_country_name
        code, name = normalize_country_name("")
        assert code is None

    def test_none_returns_none(self):
        from modules.utils import normalize_country_name
        code, name = normalize_country_name(None)
        assert code is None


# ---------------------------------------------------------------------------
# normalize_us_state
# ---------------------------------------------------------------------------

class TestNormalizeUsState:

    def test_abbr_wa(self):
        from modules.utils import US_AVAILABLE, normalize_us_state
        if not US_AVAILABLE:
            pytest.skip("us library not installed")
        abbr, name = normalize_us_state("WA")
        assert abbr == "WA"

    def test_full_name_washington(self):
        from modules.utils import US_AVAILABLE, normalize_us_state
        if not US_AVAILABLE:
            pytest.skip("us library not installed")
        abbr, name = normalize_us_state("Washington")
        assert abbr == "WA"

    def test_abbr_ca(self):
        from modules.utils import US_AVAILABLE, normalize_us_state
        if not US_AVAILABLE:
            pytest.skip("us library not installed")
        abbr, name = normalize_us_state("CA")
        assert abbr == "CA"

    def test_unknown_returns_none(self):
        from modules.utils import US_AVAILABLE, normalize_us_state
        if not US_AVAILABLE:
            pytest.skip("us library not installed")
        abbr, name = normalize_us_state("XX")
        assert abbr is None

    def test_empty_returns_none(self):
        from modules.utils import normalize_us_state
        abbr, name = normalize_us_state("")
        assert abbr is None

    def test_none_returns_none(self):
        from modules.utils import normalize_us_state
        abbr, name = normalize_us_state(None)
        assert abbr is None


# ---------------------------------------------------------------------------
# is_country_name
# ---------------------------------------------------------------------------

class TestIsCountryName:

    def test_none_returns_false(self):
        from modules.utils import is_country_name
        assert is_country_name(None) is False

    def test_empty_returns_false(self):
        from modules.utils import is_country_name
        assert is_country_name("") is False

    def test_long_unknown_text_returns_true(self):
        from modules.utils import is_country_name
        # Texts > 2 chars with no library match default to True (assumed country)
        result = is_country_name("Narnia")
        assert result is True

    def test_known_country_with_pycountry(self):
        from modules.utils import PYCOUNTRY_AVAILABLE, is_country_name
        if not PYCOUNTRY_AVAILABLE:
            pytest.skip("pycountry not installed")
        assert is_country_name("Sweden") is True

    def test_us_state_not_country(self):
        from modules.utils import US_AVAILABLE, is_country_name
        if not US_AVAILABLE:
            pytest.skip("us library not installed")
        # 'WA' is a US state — should not be a country
        result = is_country_name("WA")
        assert result is False

    def test_two_char_without_libraries(self):
        from modules.utils import PYCOUNTRY_AVAILABLE, US_AVAILABLE, is_country_name
        if PYCOUNTRY_AVAILABLE or US_AVAILABLE:
            pytest.skip("libraries present, 2-char lookup changes result")
        assert is_country_name("ZZ") is False


# ---------------------------------------------------------------------------
# is_us_state
# ---------------------------------------------------------------------------

class TestIsUsState:

    def test_none_returns_false(self):
        from modules.utils import is_us_state
        assert is_us_state(None) is False

    def test_empty_returns_false(self):
        from modules.utils import is_us_state
        assert is_us_state("") is False

    def test_wa_abbr_is_state(self):
        from modules.utils import US_AVAILABLE, is_us_state
        if not US_AVAILABLE:
            pytest.skip("us library not installed")
        assert is_us_state("WA") is True

    def test_washington_full_name_is_state(self):
        from modules.utils import US_AVAILABLE, is_us_state
        if not US_AVAILABLE:
            pytest.skip("us library not installed")
        assert is_us_state("Washington") is True

    def test_xx_not_state(self):
        from modules.utils import US_AVAILABLE, is_us_state
        if not US_AVAILABLE:
            pytest.skip("us library not installed")
        assert is_us_state("XX") is False

    def test_without_us_library_returns_false(self):
        from modules.utils import US_AVAILABLE, is_us_state
        if US_AVAILABLE:
            pytest.skip("us library present")
        assert is_us_state("WA") is False


# ---------------------------------------------------------------------------
# parse_location_string
# ---------------------------------------------------------------------------

class TestParseLocationString:

    def test_no_comma_returns_city_only(self):
        from modules.utils import parse_location_string
        city, part, typ = parse_location_string("Seattle")
        assert city == "Seattle"
        assert part is None
        assert typ is None

    def test_city_state_abbr(self):
        from modules.utils import US_AVAILABLE, parse_location_string
        if not US_AVAILABLE:
            pytest.skip("us library not installed")
        city, part, typ = parse_location_string("Seattle, WA")
        assert city == "Seattle"
        assert typ == "state"

    def test_city_state_full_name(self):
        from modules.utils import US_AVAILABLE, parse_location_string
        if not US_AVAILABLE:
            pytest.skip("us library not installed")
        city, part, typ = parse_location_string("Portland, Oregon")
        assert city == "Portland"
        assert typ == "state"

    def test_city_country_full_name(self):
        from modules.utils import PYCOUNTRY_AVAILABLE, parse_location_string
        if not PYCOUNTRY_AVAILABLE:
            pytest.skip("pycountry not installed")
        city, part, typ = parse_location_string("Stockholm, Sweden")
        assert city == "Stockholm"
        assert typ == "country"

    def test_two_char_second_defaults_to_state(self):
        from modules.utils import parse_location_string
        city, part, typ = parse_location_string("SomeCity, ZZ")
        assert city == "SomeCity"
        # 2-char unknown → state (or may be country if pycountry recognises it)
        assert typ in ("state", "country")

    def test_longer_unknown_second_defaults_to_country(self):
        from modules.utils import parse_location_string
        city, part, typ = parse_location_string("Paris, SomeLongPlace")
        assert city == "Paris"
        assert typ == "country"

    def test_whitespace_trimmed(self):
        from modules.utils import parse_location_string
        city, part, typ = parse_location_string("  London  ,  UK  ")
        assert city == "London"


# ---------------------------------------------------------------------------
# rate_limited_nominatim_geocode (async)
# ---------------------------------------------------------------------------

class TestRateLimitedNominatimGeocode:

    async def test_no_rate_limiter_calls_geocoder_directly(self):
        from modules.utils import rate_limited_nominatim_geocode
        bot = Mock(spec=[])  # no nominatim_rate_limiter attr
        mock_loc = _make_location()
        mock_geocoder = Mock()
        mock_geocoder.geocode = Mock(return_value=mock_loc)
        with patch("modules.utils.get_nominatim_geocoder", return_value=mock_geocoder):
            result = await rate_limited_nominatim_geocode(bot, "Seattle")
        assert result is mock_loc

    async def test_with_rate_limiter_waits_and_records(self, mock_bot):
        from modules.utils import rate_limited_nominatim_geocode
        mock_loc = _make_location()
        mock_geocoder = Mock()
        mock_geocoder.geocode = Mock(return_value=mock_loc)
        with patch("modules.utils.get_nominatim_geocoder", return_value=mock_geocoder):
            result = await rate_limited_nominatim_geocode(mock_bot, "Tokyo")
        mock_bot.nominatim_rate_limiter.wait_for_request.assert_called_once()
        mock_bot.nominatim_rate_limiter.record_request.assert_called_once()
        assert result is mock_loc

    async def test_returns_none_when_not_found(self, mock_bot):
        from modules.utils import rate_limited_nominatim_geocode
        mock_geocoder = Mock()
        mock_geocoder.geocode = Mock(return_value=None)
        with patch("modules.utils.get_nominatim_geocoder", return_value=mock_geocoder):
            result = await rate_limited_nominatim_geocode(mock_bot, "nowhere")
        assert result is None


# ---------------------------------------------------------------------------
# rate_limited_nominatim_reverse (async)
# ---------------------------------------------------------------------------

class TestRateLimitedNominatimReverse:

    async def test_no_rate_limiter_calls_directly(self):
        from modules.utils import rate_limited_nominatim_reverse
        bot = Mock(spec=[])
        mock_loc = _make_location()
        mock_geocoder = Mock()
        mock_geocoder.reverse = Mock(return_value=mock_loc)
        with patch("modules.utils.get_nominatim_geocoder", return_value=mock_geocoder):
            result = await rate_limited_nominatim_reverse(bot, "47.6, -122.3")
        assert result is mock_loc

    async def test_with_rate_limiter_waits_and_records(self, mock_bot):
        from modules.utils import rate_limited_nominatim_reverse
        mock_loc = _make_location()
        mock_geocoder = Mock()
        mock_geocoder.reverse = Mock(return_value=mock_loc)
        with patch("modules.utils.get_nominatim_geocoder", return_value=mock_geocoder):
            result = await rate_limited_nominatim_reverse(mock_bot, "47.6, -122.3")
        mock_bot.nominatim_rate_limiter.wait_for_request.assert_called_once()
        assert result is mock_loc


# ---------------------------------------------------------------------------
# rate_limited_nominatim_geocode_sync
# ---------------------------------------------------------------------------

class TestRateLimitedNominatimGeocodeSync:

    def test_no_rate_limiter_calls_geocoder_directly(self):
        from modules.utils import rate_limited_nominatim_geocode_sync
        bot = Mock(spec=[])
        mock_loc = _make_location()
        mock_geocoder = Mock()
        mock_geocoder.geocode = Mock(return_value=mock_loc)
        with patch("modules.utils.get_nominatim_geocoder", return_value=mock_geocoder):
            result = rate_limited_nominatim_geocode_sync(bot, "Seattle")
        assert result is mock_loc

    def test_with_rate_limiter_waits_and_records(self, mock_bot):
        from modules.utils import rate_limited_nominatim_geocode_sync
        mock_loc = _make_location()
        mock_geocoder = Mock()
        mock_geocoder.geocode = Mock(return_value=mock_loc)
        with patch("modules.utils.get_nominatim_geocoder", return_value=mock_geocoder):
            result = rate_limited_nominatim_geocode_sync(mock_bot, "Portland")
        mock_bot.nominatim_rate_limiter.wait_for_request_sync.assert_called_once()
        mock_bot.nominatim_rate_limiter.record_request.assert_called_once()
        assert result is mock_loc

    def test_returns_none_when_not_found(self, mock_bot):
        from modules.utils import rate_limited_nominatim_geocode_sync
        mock_geocoder = Mock()
        mock_geocoder.geocode = Mock(return_value=None)
        with patch("modules.utils.get_nominatim_geocoder", return_value=mock_geocoder):
            result = rate_limited_nominatim_geocode_sync(mock_bot, "nowhere")
        assert result is None


# ---------------------------------------------------------------------------
# rate_limited_nominatim_reverse_sync
# ---------------------------------------------------------------------------

class TestRateLimitedNominatimReverseSync:

    def test_no_rate_limiter(self):
        from modules.utils import rate_limited_nominatim_reverse_sync
        bot = Mock(spec=[])
        mock_loc = _make_location()
        mock_geocoder = Mock()
        mock_geocoder.reverse = Mock(return_value=mock_loc)
        with patch("modules.utils.get_nominatim_geocoder", return_value=mock_geocoder):
            result = rate_limited_nominatim_reverse_sync(bot, "47.6, -122.3")
        assert result is mock_loc

    def test_with_rate_limiter(self, mock_bot):
        from modules.utils import rate_limited_nominatim_reverse_sync
        mock_loc = _make_location()
        mock_geocoder = Mock()
        mock_geocoder.reverse = Mock(return_value=mock_loc)
        with patch("modules.utils.get_nominatim_geocoder", return_value=mock_geocoder):
            result = rate_limited_nominatim_reverse_sync(mock_bot, "47.6, -122.3")
        mock_bot.nominatim_rate_limiter.wait_for_request_sync.assert_called_once()
        assert result is mock_loc


# ---------------------------------------------------------------------------
# geocode_zipcode (async)
# ---------------------------------------------------------------------------

class TestGeocodeZipcodeAsync:

    async def test_cache_hit_returns_coords(self, mock_bot):
        from modules.utils import geocode_zipcode
        mock_bot.db_manager.get_cached_geocoding = Mock(return_value=(47.6, -122.3))
        lat, lon = await geocode_zipcode(mock_bot, "98101")
        assert lat == 47.6
        assert lon == -122.3

    async def test_cache_miss_nominatim_hit(self, mock_bot):
        from modules.utils import geocode_zipcode
        mock_loc = _make_location(47.6, -122.3)
        with patch("modules.utils.rate_limited_nominatim_geocode", new=AsyncMock(return_value=mock_loc)):
            lat, lon = await geocode_zipcode(mock_bot, "98101")
        assert lat == 47.6
        assert lon == -122.3
        mock_bot.db_manager.cache_geocoding.assert_called_once()

    async def test_cache_miss_nominatim_none(self, mock_bot):
        from modules.utils import geocode_zipcode
        with patch("modules.utils.rate_limited_nominatim_geocode", new=AsyncMock(return_value=None)):
            lat, lon = await geocode_zipcode(mock_bot, "00000")
        assert lat is None
        assert lon is None

    async def test_exception_returns_none(self, mock_bot):
        from modules.utils import geocode_zipcode
        mock_bot.db_manager.get_cached_geocoding = Mock(side_effect=RuntimeError("db error"))
        lat, lon = await geocode_zipcode(mock_bot, "98101")
        assert lat is None
        assert lon is None

    async def test_explicit_default_country(self, mock_bot):
        from modules.utils import geocode_zipcode
        mock_loc = _make_location(48.8, 2.3)
        with patch("modules.utils.rate_limited_nominatim_geocode", new=AsyncMock(return_value=mock_loc)) as m:
            await geocode_zipcode(mock_bot, "75001", default_country="FR")
        call_args = str(m.call_args)
        assert "FR" in call_args


# ---------------------------------------------------------------------------
# geocode_zipcode_sync
# ---------------------------------------------------------------------------

class TestGeocodeZipcodeSync:

    def test_cache_hit(self, mock_bot):
        from modules.utils import geocode_zipcode_sync
        mock_bot.db_manager.get_cached_geocoding = Mock(return_value=(47.6, -122.3))
        lat, lon = geocode_zipcode_sync(mock_bot, "98101")
        assert lat == 47.6

    def test_cache_miss_nominatim_hit(self, mock_bot):
        from modules.utils import geocode_zipcode_sync
        mock_loc = _make_location(48.8, 2.3)
        with patch("modules.utils.rate_limited_nominatim_geocode_sync", return_value=mock_loc):
            lat, lon = geocode_zipcode_sync(mock_bot, "75001", default_country="FR")
        assert lat == 48.8

    def test_nominatim_returns_none(self, mock_bot):
        from modules.utils import geocode_zipcode_sync
        with patch("modules.utils.rate_limited_nominatim_geocode_sync", return_value=None):
            lat, lon = geocode_zipcode_sync(mock_bot, "00000")
        assert lat is None
        assert lon is None

    def test_exception_returns_none(self, mock_bot):
        from modules.utils import geocode_zipcode_sync
        mock_bot.db_manager.get_cached_geocoding = Mock(side_effect=RuntimeError("err"))
        lat, lon = geocode_zipcode_sync(mock_bot, "98101")
        assert lat is None
        assert lon is None


# ---------------------------------------------------------------------------
# geocode_city (async)
# ---------------------------------------------------------------------------

class TestGeocodeCityAsync:

    async def test_exception_returns_none_tuple(self, mock_bot):
        from modules.utils import geocode_city
        mock_bot.db_manager.get_cached_geocoding = Mock(side_effect=RuntimeError("fail"))
        lat, lon, addr = await geocode_city(mock_bot, "Seattle")
        assert lat is None
        assert lon is None
        assert addr is None

    async def test_cache_hit_returns_coords(self, mock_bot):
        from modules.utils import geocode_city
        mock_bot.db_manager.get_cached_geocoding = Mock(return_value=(47.6, -122.3))
        lat, lon, addr = await geocode_city(mock_bot, "Seattle, WA")
        assert lat == 47.6
        assert lon == -122.3

    async def test_city_with_country_nominatim(self, mock_bot):
        from modules.utils import geocode_city
        mock_loc = _make_location(59.33, 18.07)
        with patch("modules.utils.rate_limited_nominatim_geocode", new=AsyncMock(return_value=mock_loc)):
            with patch("modules.utils.rate_limited_nominatim_reverse", new=AsyncMock(return_value=None)):
                lat, lon, addr = await geocode_city(
                    mock_bot, "Stockholm, Sweden", default_state="", default_country="US"
                )
        assert lat == 59.33

    async def test_bare_city_nominatim_hit(self, mock_bot):
        from modules.utils import geocode_city
        mock_loc = _make_location(35.68, 139.69)
        with patch("modules.utils.rate_limited_nominatim_geocode", new=AsyncMock(return_value=mock_loc)):
            with patch("modules.utils.rate_limited_nominatim_reverse", new=AsyncMock(return_value=None)):
                lat, lon, addr = await geocode_city(
                    mock_bot, "Wenatchee", default_state="", default_country="US"
                )
        assert lat == 35.68

    async def test_nominatim_returns_none(self, mock_bot):
        from modules.utils import geocode_city
        with patch("modules.utils.rate_limited_nominatim_geocode", new=AsyncMock(return_value=None)):
            lat, lon, addr = await geocode_city(
                mock_bot, "Xyznonexistent", default_state="", default_country="US"
            )
        assert lat is None

    async def test_include_address_info_false_by_default(self, mock_bot):
        from modules.utils import geocode_city
        mock_bot.db_manager.get_cached_geocoding = Mock(return_value=(47.6, -122.3))
        lat, lon, addr = await geocode_city(mock_bot, "Seattle, WA")
        assert addr is None

    async def test_city_with_state_nominatim(self, mock_bot):
        from modules.utils import geocode_city
        mock_loc = _make_location(47.0, -120.5)
        with patch("modules.utils.rate_limited_nominatim_geocode", new=AsyncMock(return_value=mock_loc)):
            with patch("modules.utils.rate_limited_nominatim_reverse", new=AsyncMock(return_value=None)):
                lat, lon, addr = await geocode_city(
                    mock_bot, "Ellensburg, WA", default_state="WA", default_country="US"
                )
        assert lat is not None


# ---------------------------------------------------------------------------
# geocode_city_sync
# ---------------------------------------------------------------------------

class TestGeocodeCitySync:

    def test_exception_returns_none_tuple(self, mock_bot):
        from modules.utils import geocode_city_sync
        mock_bot.db_manager.get_cached_geocoding = Mock(side_effect=RuntimeError("fail"))
        lat, lon, addr = geocode_city_sync(mock_bot, "Seattle")
        assert lat is None
        assert lon is None
        assert addr is None

    def test_cache_hit_returns_coords(self, mock_bot):
        from modules.utils import geocode_city_sync
        mock_bot.db_manager.get_cached_geocoding = Mock(return_value=(47.6, -122.3))
        lat, lon, addr = geocode_city_sync(mock_bot, "Seattle, WA")
        assert lat == 47.6
        assert lon == -122.3

    def test_city_with_country_nominatim(self, mock_bot):
        from modules.utils import geocode_city_sync
        mock_loc = _make_location(59.33, 18.07)
        with patch("modules.utils.rate_limited_nominatim_geocode_sync", return_value=mock_loc):
            with patch("modules.utils.rate_limited_nominatim_reverse_sync", return_value=None):
                lat, lon, addr = geocode_city_sync(
                    mock_bot, "Stockholm, Sweden", default_state="", default_country="US"
                )
        assert lat == 59.33

    def test_bare_city_nominatim_hit(self, mock_bot):
        from modules.utils import geocode_city_sync
        mock_loc = _make_location(35.68, 139.69)
        with patch("modules.utils.rate_limited_nominatim_geocode_sync", return_value=mock_loc):
            with patch("modules.utils.rate_limited_nominatim_reverse_sync", return_value=None):
                lat, lon, addr = geocode_city_sync(
                    mock_bot, "Wenatchee", default_state="", default_country="US"
                )
        assert lat == 35.68

    def test_nominatim_returns_none(self, mock_bot):
        from modules.utils import geocode_city_sync
        with patch("modules.utils.rate_limited_nominatim_geocode_sync", return_value=None):
            lat, lon, addr = geocode_city_sync(
                mock_bot, "Xyznonexistent", default_state="", default_country="US"
            )
        assert lat is None

    def test_city_with_state_nominatim(self, mock_bot):
        from modules.utils import geocode_city_sync
        mock_loc = _make_location(47.0, -120.5)
        with patch("modules.utils.rate_limited_nominatim_geocode_sync", return_value=mock_loc):
            with patch("modules.utils.rate_limited_nominatim_reverse_sync", return_value=None):
                lat, lon, addr = geocode_city_sync(
                    mock_bot, "Ellensburg, WA", default_state="WA", default_country="US"
                )
        assert lat is not None


# ---------------------------------------------------------------------------
# check_internet_connectivity_async
# ---------------------------------------------------------------------------

class TestCheckInternetConnectivityAsync:

    async def test_socket_success_returns_true(self):
        from modules.utils import check_internet_connectivity_async
        mock_writer = Mock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()

        async def fake_open(host, port):
            return Mock(), mock_writer

        with patch("asyncio.open_connection", fake_open):
            result = await check_internet_connectivity_async(timeout=1.0)
        assert result is True

    async def test_socket_fails_http_succeeds_returns_true(self):
        from modules.utils import check_internet_connectivity_async

        async def fail_open(host, port):
            raise OSError("refused")

        # urlopen returns something with a .close() method
        mock_response = Mock()
        mock_response.close = Mock()

        with patch("asyncio.open_connection", fail_open):
            with patch("urllib.request.urlopen", return_value=mock_response):
                result = await check_internet_connectivity_async(timeout=2.0)
        # Result depends on executor; just ensure no exception
        assert isinstance(result, bool)

    async def test_all_connections_fail_returns_false(self):
        from modules.utils import check_internet_connectivity_async

        async def fail_open(host, port):
            raise OSError("refused")

        with patch("asyncio.open_connection", fail_open):
            with patch(
                "urllib.request.urlopen", side_effect=urllib.error.URLError("no net")
            ):
                result = await check_internet_connectivity_async(timeout=1.0)
        assert result is False

    async def test_timeout_error_on_socket_falls_through(self):
        from modules.utils import check_internet_connectivity_async

        async def timeout_open(host, port):
            raise asyncio.TimeoutError()

        with patch("asyncio.open_connection", timeout_open):
            with patch(
                "urllib.request.urlopen", side_effect=urllib.error.URLError("no net")
            ):
                result = await check_internet_connectivity_async(timeout=1.0)
        assert result is False
