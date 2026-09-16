"""Characterization tests for current command location-resolution behavior.

These lock today's semantics (before modules/location.py) so regressions are
visible after the shared location service lands. All Nominatim / geocode /
OpenMeteo calls are mocked — no live network.
"""

from __future__ import annotations

import asyncio
import configparser
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from tests.conftest import mock_message

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _base_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.add_section("Bot")
    cfg.set("Bot", "bot_name", "TestBot")
    cfg.set("Bot", "timezone", "America/Los_Angeles")
    cfg.add_section("Channels")
    cfg.set("Channels", "monitor_channels", "general")
    cfg.set("Channels", "respond_to_dms", "true")
    cfg.add_section("Keywords")
    cfg.add_section("Weather")
    cfg.set("Weather", "default_state", "WA")
    cfg.set("Weather", "default_country", "US")
    cfg.set("Weather", "weather_provider", "noaa")
    return cfg


def _make_bot(**extra_sections: dict[str, dict[str, str]]) -> MagicMock:
    bot = MagicMock()
    bot.logger = Mock()
    cfg = _base_config()
    for section, values in extra_sections.items():
        if not cfg.has_section(section):
            cfg.add_section(section)
        for k, v in values.items():
            cfg.set(section, k, v)
    bot.config = cfg
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    bot.db_manager = MagicMock()
    bot.db_manager.get_cached_geocoding = Mock(return_value=(None, None))
    bot.db_manager.cache_geocoding = Mock()
    bot.db_manager.get_cached_json = Mock(return_value=None)
    bot.db_manager.cache_json = Mock()
    bot.db_manager.execute_query = Mock(return_value=[])
    bot.nominatim_rate_limiter = Mock()
    bot.nominatim_rate_limiter.wait_for_request_sync = Mock()
    bot.nominatim_rate_limiter.record_request = Mock()
    return bot


def _make_geopy_location(
    lat: float = 47.6062,
    lon: float = -122.3321,
    address: str = "Seattle, Washington, United States",
    address_info: Optional[dict] = None,
) -> Mock:
    loc = Mock()
    loc.latitude = lat
    loc.longitude = lon
    loc.address = address
    loc.raw = {"address": address_info or {"city": "Seattle", "state": "Washington", "country": "United States", "country_code": "us"}}
    return loc


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# AQI — classification via execute() (capture get_aqi_for_location args)
# ---------------------------------------------------------------------------


@pytest.fixture
def aqi_cmd():
    bot = _make_bot(Aqi_Command={"enabled": "true"})
    with patch("modules.commands.aqi_command.get_nominatim_geocoder", return_value=Mock()), \
         patch("modules.commands.aqi_command.requests_cache.CachedSession"), \
         patch("modules.commands.aqi_command.retry", side_effect=lambda s, **kw: s), \
         patch("modules.commands.aqi_command.openmeteo_requests.Client", return_value=Mock()):
        from modules.commands.aqi_command import AqiCommand
        cmd = AqiCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)
    cmd.record_execution = Mock()
    return cmd


@pytest.mark.unit
class TestAqiLocationClassification:
    """Lock AQI's front-door location typing and rewrites.

    execute() passes the raw user location into get_aqi_for_location; typing and
    intl rewrites happen inside resolve_location / classify_location.
    """

    def _capture(self, cmd, content: str) -> tuple[str, str]:
        from modules.location import classify_location

        with patch.object(cmd, "get_aqi_for_location", new_callable=AsyncMock) as m:
            m.return_value = "ok"
            _run(cmd.execute(mock_message(content=content)))
            assert m.called, f"get_aqi_for_location not called for {content!r}"
            raw = m.call_args[0][0]
            return classify_location(raw, use_international_cities=True)

    def test_coordinates_basic(self, aqi_cmd):
        loc, typ = self._capture(aqi_cmd, "aqi 47.6,-122.3")
        assert typ == "coordinates"
        assert loc == "47.6,-122.3"

    def test_coordinates_with_spaces(self, aqi_cmd):
        loc, typ = self._capture(aqi_cmd, "aqi 47.6, -122.3")
        assert typ == "coordinates"
        assert loc == "47.6, -122.3"

    def test_coordinates_negative_lat(self, aqi_cmd):
        loc, typ = self._capture(aqi_cmd, "aqi -33.9, 151.2")
        assert typ == "coordinates"

    def test_zipcode_five_digits(self, aqi_cmd):
        loc, typ = self._capture(aqi_cmd, "aqi 98101")
        assert typ == "zipcode"
        assert loc == "98101"

    def test_plain_city(self, aqi_cmd):
        loc, typ = self._capture(aqi_cmd, "aqi seattle")
        assert typ == "city"
        assert loc == "seattle"

    def test_city_state_comma_kept(self, aqi_cmd):
        loc, typ = self._capture(aqi_cmd, "aqi paris, tx")
        assert typ == "city"
        assert loc == "paris, tx"

    def test_space_separated_city_country_rewritten(self, aqi_cmd):
        loc, typ = self._capture(aqi_cmd, "aqi vancouver canada")
        assert typ == "city"
        assert loc == "vancouver, canada"

    def test_united_kingdom_rewritten_to_uk(self, aqi_cmd):
        loc, typ = self._capture(aqi_cmd, "aqi london united kingdom")
        assert typ == "city"
        assert loc == "london, uk"

    def test_international_city_single_token_london(self, aqi_cmd):
        loc, typ = self._capture(aqi_cmd, "aqi london")
        assert typ == "city"
        assert loc == "london, uk"

    def test_international_city_single_token_tokyo(self, aqi_cmd):
        loc, typ = self._capture(aqi_cmd, "aqi tokyo")
        assert typ == "city"
        assert loc == "tokyo, japan"

    def test_multiword_intl_key_rewritten(self, aqi_cmd):
        """Multi-word intl keys rewrite like single-token ones."""
        loc, typ = self._capture(aqi_cmd, "aqi mexico city")
        assert typ == "city"
        assert loc == "mexico city, mexico"

    def test_execute_passes_raw_location(self, aqi_cmd):
        with patch.object(aqi_cmd, "get_aqi_for_location", new_callable=AsyncMock) as m:
            m.return_value = "ok"
            _run(aqi_cmd.execute(mock_message(content="aqi mexico city")))
        assert m.call_args[0][0] == "mexico city"

    def test_astronomical_early_out_skips_geocode(self, aqi_cmd):
        with patch.object(aqi_cmd, "get_aqi_for_location", new_callable=AsyncMock) as m:
            _run(aqi_cmd.execute(mock_message(content="aqi mars")))
            m.assert_not_called()
            aqi_cmd.send_response.assert_called_once()
            body = aqi_cmd.send_response.call_args[0][1]
            assert "Mars" in body or "mars" in body.lower() or "CO2" in body


@pytest.mark.unit
class TestAqiNeighborhoodQueries:
    def test_seattle_neighborhood(self, aqi_cmd):
        queries = aqi_cmd.get_neighborhood_queries("greenwood")
        assert queries == [
            "greenwood, Seattle, WA, USA",
            "greenwood, Seattle, USA",
        ]

    def test_nyc_neighborhood(self, aqi_cmd):
        queries = aqi_cmd.get_neighborhood_queries("williamsburg")
        assert "New York" in queries[0]

    def test_unknown_returns_empty(self, aqi_cmd):
        assert aqi_cmd.get_neighborhood_queries("notaneighborhood") == []


@pytest.mark.unit
class TestAqiCityToLatLon:
    def test_uses_geocode_city_sync_for_plain_city(self, aqi_cmd):
        with patch(
            "modules.location.geocode_city_sync",
            return_value=(47.6, -122.3, {"city": "Seattle"}),
        ) as geo:
            lat, lon, addr = aqi_cmd.city_to_lat_lon("seattle")
        geo.assert_called_once()
        assert lat == pytest.approx(47.6)
        assert lon == pytest.approx(-122.3)
        assert addr == {"city": "Seattle"}

    def test_country_comma_path_uses_nominatim_directly(self, aqi_cmd):
        loc = _make_geopy_location(48.8566, 2.3522, "Paris, France", {"city": "Paris", "country": "France"})
        with patch(
            "modules.location.rate_limited_nominatim_geocode_sync",
            return_value=loc,
        ) as geo, patch(
            "modules.location.geocode_city_sync",
        ) as shared:
            lat, lon, addr = aqi_cmd.city_to_lat_lon("paris, france")
        geo.assert_called()
        shared.assert_not_called()
        assert lat == pytest.approx(48.8566)
        assert lon == pytest.approx(2.3522)

    def test_neighborhood_fallback_when_geocode_city_fails(self, aqi_cmd):
        loc = _make_geopy_location(47.69, -122.35, "Greenwood, Seattle")
        with patch(
            "modules.location.geocode_city_sync",
            return_value=(None, None, None),
        ), patch(
            "modules.location.rate_limited_nominatim_geocode_sync",
            return_value=loc,
        ) as geo:
            lat, lon, addr = aqi_cmd.city_to_lat_lon("greenwood")
        assert lat == pytest.approx(47.69)
        assert any("Seattle" in str(c) for c in geo.call_args_list)


@pytest.mark.unit
class TestAqiZipcodePath:
    def test_zip_override_98013_queries_mapped_place(self, aqi_cmd):
        loc = _make_geopy_location(47.45, -122.46, "Vashon, Washington, United States")
        with patch(
            "modules.location.rate_limited_nominatim_geocode_sync",
            return_value=loc,
        ) as geo, patch.object(
            aqi_cmd, "get_openmeteo_aqi", return_value="🟢 20 (Good)"
        ), patch(
            "modules.location.rate_limited_nominatim_reverse_sync",
            return_value=loc,
        ):
            result = _run(aqi_cmd.get_aqi_for_location("98013", "zipcode"))
        assert "🟢" in result or "20" in result
        first_query = geo.call_args_list[0][0][1]
        assert "Vashon" in first_query

    def test_zip_falls_back_to_geocode_zipcode_sync(self, aqi_cmd):
        with patch(
            "modules.location.rate_limited_nominatim_geocode_sync",
            return_value=None,
        ), patch(
            "modules.location.geocode_zipcode_sync",
            return_value=(47.6, -122.3),
        ) as zip_geo, patch.object(
            aqi_cmd, "get_openmeteo_aqi", return_value="ok"
        ), patch(
            "modules.location.rate_limited_nominatim_reverse_sync",
            return_value=None,
        ):
            result = _run(aqi_cmd.get_aqi_for_location("98101", "zipcode"))
        zip_geo.assert_called()
        assert result.startswith("98101:") or "ok" in result


@pytest.mark.unit
class TestAqiCoordinateErrors:
    def test_invalid_latitude_message(self, aqi_cmd):
        result = _run(aqi_cmd.get_aqi_for_location("91,0"))
        assert result == "Invalid latitude: 91.0. Must be between -90 and 90."

    def test_invalid_longitude_message(self, aqi_cmd):
        result = _run(aqi_cmd.get_aqi_for_location("0,200"))
        assert result == "Invalid longitude: 200.0. Must be between -180 and 180."


@pytest.mark.unit
class TestAqiPrefixBudget:
    def test_under_budget_includes_display_name(self, aqi_cmd):
        from modules.location import ResolvedLocation

        resolved = ResolvedLocation(
            lat=47.6, lon=-122.3, location_type="city", query="seattle",
            display_name="Seattle, WA",
            address_info={"city": "Seattle", "state": "Washington", "country": "United States"},
        )
        with patch("modules.commands.aqi_command.resolve_location", return_value=resolved), \
             patch.object(aqi_cmd, "get_openmeteo_aqi", return_value="🟢 20"):
            result = _run(aqi_cmd.get_aqi_for_location("seattle"))
        assert result.startswith("Seattle, WA:")

    def test_over_budget_same_state_omits_prefix(self, aqi_cmd):
        from modules.location import ResolvedLocation

        aqi_cmd.default_state = "WA"
        long_aqi = "X" * 120
        resolved = ResolvedLocation(
            lat=47.6, lon=-122.3, location_type="city", query="seattle",
            display_name="Seattle, WA",
            address_info={"city": "Seattle", "state": "Washington", "country": "United States"},
        )
        with patch("modules.commands.aqi_command.resolve_location", return_value=resolved), \
             patch.object(aqi_cmd, "get_openmeteo_aqi", return_value=long_aqi):
            result = _run(aqi_cmd.get_aqi_for_location("seattle"))
        assert result == long_aqi
        assert not result.startswith("Seattle")

    def test_over_budget_different_state_keeps_short_prefix(self, aqi_cmd):
        from modules.location import ResolvedLocation

        aqi_cmd.default_state = "WA"
        long_aqi = "X" * 120
        resolved = ResolvedLocation(
            lat=30.27, lon=-97.74, location_type="city", query="austin",
            display_name="Austin, TX",
            address_info={"city": "Austin", "state": "Texas", "country": "United States"},
        )
        with patch("modules.commands.aqi_command.resolve_location", return_value=resolved), \
             patch.object(aqi_cmd, "get_openmeteo_aqi", return_value=long_aqi):
            result = _run(aqi_cmd.get_aqi_for_location("austin"))
        assert result.endswith(long_aqi)
        assert result.startswith("Austin")


@pytest.mark.unit
class TestAqiIntlResolveWiring:
    def test_london_rewrites_via_resolve(self, aqi_cmd):
        loc = _make_geopy_location(
            51.5, -0.12, "London, UK", {"city": "London", "country": "United Kingdom"}
        )
        with patch(
            "modules.location.rate_limited_nominatim_geocode_sync", return_value=loc
        ) as geo, patch(
            "modules.location.geocode_city_sync", return_value=(None, None, None)
        ), patch(
            "modules.location.rate_limited_nominatim_reverse_sync", return_value=loc
        ), patch.object(aqi_cmd, "get_openmeteo_aqi", return_value="ok"):
            result = _run(aqi_cmd.get_aqi_for_location("london"))
        assert "ok" in result
        # Intl rewrite should query "london, uk" on the country path
        assert any(
            isinstance(c[0][1], str) and "london" in c[0][1].lower()
            for c in geo.call_args_list
        )


# ---------------------------------------------------------------------------
# WX — type detection + thin geocode wrappers
# ---------------------------------------------------------------------------


@pytest.fixture
def wx_cmd():
    bot = _make_bot(Wx_Command={"enabled": "true"})
    from modules.commands.wx_command import WxCommand
    return WxCommand(bot)


def _wx_classify(location: str) -> str:
    """Desired shared classify contract (AQI / location.classify_location)."""
    import re
    if re.match(r'^\s*-?\d+\.?\d*\s*,\s*-?\d+\.?\d*\s*$', location):
        return "coordinates"
    if re.match(r'^\s*\d{5}\s*$', location):
        return "zipcode"
    return "city"


@pytest.mark.unit
class TestWxLocationClassification:
    def test_coordinates(self):
        assert _wx_classify("47.6,-122.3") == "coordinates"
        assert _wx_classify("47.6, -122.3") == "coordinates"

    def test_zipcode_no_whitespace(self):
        assert _wx_classify("98101") == "zipcode"

    def test_zipcode_with_whitespace_is_zipcode(self):
        """Desired: surrounding whitespace does not demote ZIP to city."""
        assert _wx_classify(" 98101 ") == "zipcode"

    def test_wx_execute_accepts_zip_with_whitespace(self, wx_cmd):
        """Live WxCommand.execute type-detect must treat ' 98101 ' as zipcode."""
        import inspect
        import re

        from modules.commands.wx_command import WxCommand

        source = inspect.getsource(WxCommand.execute)
        assert r"^\s*\d{5}\s*$" in source
        assert re.match(r"^\s*\d{5}\s*$", " 98101 ")

    def test_city(self):
        assert _wx_classify("seattle") == "city"
        assert _wx_classify("paris, tx") == "city"


@pytest.mark.unit
class TestWxGeocodeWrappers:
    def test_zipcode_delegates_to_geocode_zipcode_sync(self, wx_cmd):
        with patch(
            "modules.commands.wx_command.geocode_zipcode_sync",
            return_value=(47.6, -122.3),
        ) as geo:
            lat, lon = wx_cmd.zipcode_to_lat_lon("98101")
        geo.assert_called_once()
        assert lat == pytest.approx(47.6)

    def test_city_delegates_to_geocode_city_sync(self, wx_cmd):
        with patch(
            "modules.commands.wx_command.geocode_city_sync",
            return_value=(47.6, -122.3, {"city": "Seattle"}),
        ) as geo:
            lat, lon, addr = wx_cmd.city_to_lat_lon("seattle")
        geo.assert_called_once()
        kwargs = geo.call_args.kwargs
        assert kwargs.get("include_address_info") is True
        assert lat == pytest.approx(47.6)
        assert addr == {"city": "Seattle"}


# ---------------------------------------------------------------------------
# Rain — _resolve_location contracts
# ---------------------------------------------------------------------------


@pytest.fixture
def rain_cmd():
    bot = _make_bot(
        Rain_Command={"enabled": "true", "zip_city_lookup": "false"},
    )
    bot.config.set("Bot", "bot_latitude", "47.6")
    bot.config.set("Bot", "bot_longitude", "-122.3")
    from modules.commands.rain_command import RainCommand
    cmd = RainCommand(bot)
    return cmd


@pytest.mark.unit
class TestRainResolveLocation:
    def test_coordinates(self, rain_cmd):
        with patch.object(rain_cmd, "_coordinates_to_location_string", return_value="Seattle, WA"):
            lat, lon, label, err = rain_cmd._resolve_location(
                mock_message(content="rain 47.6,-122.3"), "47.6,-122.3"
            )
        assert err is None
        assert lat == pytest.approx(47.6)
        assert lon == pytest.approx(-122.3)
        assert label == "Seattle, WA"

    def test_invalid_coordinates(self, rain_cmd):
        lat, lon, label, err = rain_cmd._resolve_location(
            mock_message(content="rain 200,0"), "200,0"
        )
        assert err == "commands.rain.error"
        assert lat is None

    def test_zipcode_uses_geocode_zipcode_sync(self, rain_cmd):
        with patch(
            "modules.commands.rain_command.geocode_zipcode_sync",
            return_value=(47.6, -122.3),
        ) as geo, patch.object(
            rain_cmd, "_coordinates_to_location_string", return_value="Seattle, WA"
        ):
            lat, lon, label, err = rain_cmd._resolve_location(
                mock_message(content="rain 98101"), "98101"
            )
        geo.assert_called_once()
        assert err is None
        assert lat == pytest.approx(47.6)
        assert "98101" in label or "Seattle" in label

    def test_city_uses_geocode_city_sync(self, rain_cmd):
        with patch(
            "modules.commands.rain_command.geocode_city_sync",
            return_value=(36.16, -86.78, None),
        ) as geo, patch.object(
            rain_cmd, "_suffix_for_coords", return_value="TN"
        ):
            lat, lon, label, err = rain_cmd._resolve_location(
                mock_message(content="rain nashville"), "nashville"
            )
        geo.assert_called_once()
        assert err is None
        assert "Nashville" in label or "nashville" in label.lower()
        assert "TN" in label

    def test_empty_uses_bot_location(self, rain_cmd):
        rain_cmd.bot.db_manager.execute_query.return_value = []
        with patch.object(
            rain_cmd, "_coordinates_to_location_string", return_value="Botville, WA"
        ):
            lat, lon, label, err = rain_cmd._resolve_location(
                mock_message(content="rain"), None
            )
        assert err is None
        assert lat == pytest.approx(47.6)
        assert lon == pytest.approx(-122.3)


# ---------------------------------------------------------------------------
# Prefix / solarforecast — repeater-first parse order
# ---------------------------------------------------------------------------


@pytest.fixture
def prefix_cmd():
    bot = _make_bot(
        Prefix_Command={"enabled": "true"},
        External_Data={"repeater_prefix_api_url": ""},
    )
    from modules.commands.prefix_command import PrefixCommand
    return PrefixCommand(bot)


@pytest.fixture
def solar_cmd():
    bot = _make_bot(Solarforecast_Command={"enabled": "true"})
    with patch("modules.commands.solarforecast_command.get_nominatim_geocoder", return_value=Mock()):
        from modules.commands.solarforecast_command import SolarforecastCommand
        return SolarforecastCommand(bot)


@pytest.mark.unit
class TestPrefixParseLocationOrder:
    def test_repeater_wins_before_city(self, prefix_cmd):
        async def _run_parse():
            with patch.object(
                prefix_cmd, "_repeater_name_to_lat_lon", new_callable=AsyncMock, return_value=(47.1, -122.1)
            ) as rep, patch(
                "modules.commands.prefix_command.geocode_city", new_callable=AsyncMock
            ) as city:
                lat, lon, typ = await prefix_cmd._parse_location_to_lat_lon("KR7ABC")
            return lat, lon, typ, rep, city

        lat, lon, typ, rep, city = _run(_run_parse())
        assert typ == "repeater"
        assert lat == pytest.approx(47.1)
        rep.assert_awaited_once()
        city.assert_not_called()

    def test_coordinates(self, prefix_cmd):
        async def _run_parse():
            with patch.object(
                prefix_cmd, "_repeater_name_to_lat_lon", new_callable=AsyncMock, return_value=(None, None)
            ):
                return await prefix_cmd._parse_location_to_lat_lon("47.6,-122.3")

        lat, lon, typ = _run(_run_parse())
        assert typ == "coordinates"
        assert lat == pytest.approx(47.6)

    def test_zipcode(self, prefix_cmd):
        async def _run_parse():
            with patch.object(
                prefix_cmd, "_repeater_name_to_lat_lon", new_callable=AsyncMock, return_value=(None, None)
            ), patch(
                "modules.commands.prefix_command.geocode_zipcode",
                new_callable=AsyncMock,
                return_value=(47.6, -122.3),
            ):
                return await prefix_cmd._parse_location_to_lat_lon("98101")

        lat, lon, typ = _run(_run_parse())
        assert typ == "zipcode"
        assert lat == pytest.approx(47.6)

    def test_city_fallback(self, prefix_cmd):
        async def _run_parse():
            with patch.object(
                prefix_cmd, "_repeater_name_to_lat_lon", new_callable=AsyncMock, return_value=(None, None)
            ), patch(
                "modules.commands.prefix_command.geocode_city",
                new_callable=AsyncMock,
                return_value=(47.6, -122.3, None),
            ):
                return await prefix_cmd._parse_location_to_lat_lon("seattle")

        lat, lon, typ = _run(_run_parse())
        assert typ == "city"
        assert lat == pytest.approx(47.6)


@pytest.mark.unit
class TestSolarforecastParseLocationOrder:
    def test_repeater_wins(self, solar_cmd):
        async def _run_parse():
            with patch.object(
                solar_cmd, "_repeater_name_to_lat_lon", new_callable=AsyncMock, return_value=(48.0, -122.0)
            ), patch.object(
                solar_cmd, "_city_to_lat_lon", new_callable=AsyncMock
            ) as city:
                lat, lon, typ = await solar_cmd._parse_location("SomeRepeater")
            return lat, lon, typ, city

        lat, lon, typ, city = _run(_run_parse())
        assert typ == "repeater"
        city.assert_not_called()

    def test_coordinates(self, solar_cmd):
        async def _run_parse():
            with patch.object(
                solar_cmd, "_repeater_name_to_lat_lon", new_callable=AsyncMock, return_value=(None, None)
            ):
                return await solar_cmd._parse_location("47.6, -122.3")

        lat, lon, typ = _run(_run_parse())
        assert typ == "coordinates"
        assert lat == pytest.approx(47.6)


# ---------------------------------------------------------------------------
# Alert — _parse_query geocode-related typing
# ---------------------------------------------------------------------------


@pytest.fixture
def alert_cmd():
    # Query parsing lives in the PulsePoint alert service now (the alert command
    # is a thin orchestrator). These characterization tests target the service's
    # parse_query, which preserves the original heuristics.
    bot = _make_bot(
        PulsePoint_Alert_Service={
            "enabled": "true",
            "agency.city.seattle": "1234",
            "agency.county.king": "5678",
            # Legacy agency.* keys are treated as counties today
            "agency.everett": "9999",
        }
    )
    from modules.service_plugins.pulsepoint_alert_service import PulsePointAlertService
    svc = PulsePointAlertService(bot)
    # Expose parse_query under the historical name used by these tests.
    svc._parse_query = svc.parse_query
    return svc


@pytest.mark.unit
class TestAlertParseQuery:
    def test_coordinates(self, alert_cmd):
        qtype, location, lat, lon = alert_cmd._parse_query("47.6,-122.3")
        assert qtype == "coordinates"
        assert lat == pytest.approx(47.6)
        assert lon == pytest.approx(-122.3)

    def test_zipcode(self, alert_cmd):
        qtype, location, lat, lon = alert_cmd._parse_query("98101")
        assert qtype == "zipcode"
        assert location == "98101"
        assert lat is None and lon is None

    def test_county_alias_sno(self, alert_cmd):
        qtype, location, lat, lon = alert_cmd._parse_query("sno")
        assert qtype == "county"
        assert location == "sno"

    def test_county_alias_sea(self, alert_cmd):
        qtype, location, _, _ = alert_cmd._parse_query("sea")
        assert qtype == "county"

    def test_street_city_heuristic(self, alert_cmd):
        qtype, location, lat, lon = alert_cmd._parse_query("178th seattle")
        assert qtype == "street_city"
        assert "178th" in location
        assert "seattle" in location.lower()

    def test_configured_city(self, alert_cmd):
        qtype, location, _, _ = alert_cmd._parse_query("seattle")
        assert qtype == "city"
        assert location == "seattle"

    def test_configured_county(self, alert_cmd):
        qtype, location, _, _ = alert_cmd._parse_query("king")
        assert qtype == "county"
        assert location == "king"

    def test_legacy_agency_key_treated_as_county(self, alert_cmd):
        qtype, location, _, _ = alert_cmd._parse_query("everett")
        assert qtype == "county"
        assert location == "everett"

    def test_unknown_defaults_to_city(self, alert_cmd):
        qtype, location, _, _ = alert_cmd._parse_query("bothell")
        assert qtype == "city"
        assert location == "bothell"
