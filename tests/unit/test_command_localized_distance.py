#!/usr/bin/env python3
"""
Unit tests for TestCommand distance units.

The {path_distance} and {firstlast_distance} placeholders are kilometres for
every locale except US English, which gets miles. en-GB shares the "en" catalog
but not the units, so it stays metric.
"""

import pytest

from modules.commands.test_command import TestCommand as MeshTestCommand
from tests.conftest import mock_message


@pytest.fixture
def test_command(mock_bot):
    """TestCommand whose repeater lookups resolve to fixed, known coordinates."""
    if not mock_bot.config.has_section('Localization'):
        mock_bot.config.add_section('Localization')
    cmd = MeshTestCommand(mock_bot)
    # Two hops one degree of longitude apart at 47N (~75.8 km/deg), so the
    # distance is large enough that km and mi cannot be confused.
    coords = {'AA': (47.0, -122.0), 'BB': (47.0, -121.0)}
    cmd._lookup_repeater_location = lambda node_id, path_context=None: coords.get(node_id)
    return cmd


def _two_hop_message():
    return mock_message(
        content="test",
        routing_info={'path_length': 2, 'path_nodes': ['AA', 'BB']},
    )


def _set_language(cmd, language):
    cmd.bot.config.set('Localization', 'language', language)


@pytest.mark.unit
class TestFormatDistance:
    """_format_distance picks its unit from the Localization language."""

    def test_english_converts_to_miles(self, test_command):
        _set_language(test_command, 'en')
        assert test_command._format_distance(100.0) == "62.1mi"

    def test_non_english_stays_metric(self, test_command):
        _set_language(test_command, 'de')
        assert test_command._format_distance(100.0) == "100.0km"

    def test_en_us_converts_to_miles(self, test_command):
        _set_language(test_command, 'en-US')
        assert test_command._format_distance(100.0) == "62.1mi"

    def test_en_gb_stays_metric(self, test_command):
        # en-GB reads the English catalog but not US units — the same split the
        # !gwx unit fix settled on.
        _set_language(test_command, 'en-GB')
        assert test_command._format_distance(100.0) == "100.0km"

    def test_underscore_locale_is_normalized(self, test_command):
        _set_language(test_command, 'en_US')
        assert test_command._format_distance(100.0) == "62.1mi"

    def test_missing_language_defaults_to_miles(self, test_command):
        # [Localization] language defaults to 'en' throughout the bot.
        assert test_command._format_distance(100.0) == "62.1mi"

    def test_response_translator_language_wins(self, test_command):
        # An auto-detected sender language must carry the units with it.
        _set_language(test_command, 'en')
        test_command.bot.translator.language = 'fr'
        assert test_command._format_distance(100.0) == "100.0km"


@pytest.mark.unit
class TestPathDistancePlaceholders:
    """The rendered {path_distance} / {firstlast_distance} strings."""

    def test_path_distance_in_miles_for_english(self, test_command):
        _set_language(test_command, 'en')
        rendered = test_command._calculate_path_distance(_two_hop_message())
        assert rendered.endswith(" (1 segs)")
        assert "mi " in rendered
        assert "km" not in rendered

    def test_path_distance_in_km_for_spanish(self, test_command):
        _set_language(test_command, 'es')
        rendered = test_command._calculate_path_distance(_two_hop_message())
        assert rendered.endswith(" (1 segs)")
        assert "km " in rendered
        assert "mi" not in rendered

    def test_firstlast_distance_in_miles_for_english(self, test_command):
        _set_language(test_command, 'en')
        assert test_command._calculate_firstlast_distance(_two_hop_message()) == "47.1mi"

    def test_firstlast_distance_in_km_for_spanish(self, test_command):
        _set_language(test_command, 'es')
        assert test_command._calculate_firstlast_distance(_two_hop_message()) == "75.8km"

    def test_miles_value_is_the_converted_km_value(self, test_command):
        _set_language(test_command, 'es')
        km = float(test_command._calculate_firstlast_distance(_two_hop_message())[:-2])
        _set_language(test_command, 'en')
        mi = float(test_command._calculate_firstlast_distance(_two_hop_message())[:-2])
        assert mi == pytest.approx(km * 0.621371, abs=0.05)

    def test_direct_connection_is_unit_agnostic(self, test_command):
        _set_language(test_command, 'en')
        direct = mock_message(content="test", routing_info={'path_length': 0})
        assert test_command._calculate_path_distance(direct) == "N/A"
        assert test_command._calculate_firstlast_distance(direct) == "N/A"
