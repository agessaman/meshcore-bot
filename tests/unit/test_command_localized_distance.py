#!/usr/bin/env python3
"""
Unit tests for TestCommand distance units.

[Test_Command] distance_unit decides the unit for the {path_distance} and
{firstlast_distance} placeholders. Under the default 'auto' it follows the reply
language: miles for US English, kilometres everywhere else — en-GB shares the
"en" catalog but not the units, so it stays metric.
"""

import pytest

from modules.commands.test_command import TestCommand as MeshTestCommand
from tests.conftest import mock_message


@pytest.fixture
def build_command(mock_bot):
    """Build a TestCommand whose repeater lookups resolve to fixed coordinates.

    distance_unit is read once at construction, so it is a build-time argument.
    """
    for section in ('Localization', 'Test_Command'):
        if not mock_bot.config.has_section(section):
            mock_bot.config.add_section(section)

    def _build(distance_unit=None):
        if distance_unit is None:
            mock_bot.config.remove_option('Test_Command', 'distance_unit')
        else:
            mock_bot.config.set('Test_Command', 'distance_unit', distance_unit)
        cmd = MeshTestCommand(mock_bot)
        # Two hops one degree of longitude apart at 47N (~75.8 km/deg), so the
        # distance is large enough that km and mi cannot be confused.
        coords = {'AA': (47.0, -122.0), 'BB': (47.0, -121.0)}
        cmd._lookup_repeater_location = lambda node_id, path_context=None: coords.get(node_id)
        return cmd

    return _build


@pytest.fixture
def test_command(build_command):
    """TestCommand with the default distance_unit ('auto')."""
    return build_command()


def _two_hop_message():
    return mock_message(
        content="test",
        routing_info={'path_length': 2, 'path_nodes': ['AA', 'BB']},
    )


def _set_language(cmd, language):
    cmd.bot.config.set('Localization', 'language', language)


@pytest.mark.unit
class TestDistanceUnitConfig:
    """distance_unit overrides the language-derived unit."""

    def test_defaults_to_auto(self, test_command):
        assert test_command.distance_unit == 'auto'

    def test_km_forces_metric_for_english(self, build_command):
        cmd = build_command('km')
        _set_language(cmd, 'en')
        assert cmd._format_distance(100.0) == "100.0km"

    def test_mi_forces_miles_for_german(self, build_command):
        cmd = build_command('mi')
        _set_language(cmd, 'de')
        assert cmd._format_distance(100.0) == "62.1mi"

    def test_value_is_normalized(self, build_command):
        cmd = build_command('  MI  ')
        _set_language(cmd, 'de')
        assert cmd.distance_unit == 'mi'
        assert cmd._format_distance(100.0) == "62.1mi"

    def test_invalid_value_falls_back_to_auto(self, build_command):
        cmd = build_command('furlongs')
        assert cmd.distance_unit == 'auto'
        _set_language(cmd, 'en')
        assert cmd._format_distance(100.0) == "62.1mi"
        _set_language(cmd, 'de')
        assert cmd._format_distance(100.0) == "100.0km"

    def test_invalid_value_warns_once_at_startup(self, build_command):
        cmd = build_command('furlongs')
        warnings = [str(call) for call in cmd.logger.warning.call_args_list]
        assert any('distance_unit' in w and 'furlongs' in w for w in warnings)
        # Rendering must not re-warn on every reply.
        cmd.logger.warning.reset_mock()
        cmd._format_distance(100.0)
        cmd.logger.warning.assert_not_called()

    def test_explicit_unit_reaches_the_placeholders(self, build_command):
        cmd = build_command('km')
        _set_language(cmd, 'en')
        assert cmd._calculate_firstlast_distance(_two_hop_message()) == "75.8km"


@pytest.mark.unit
class TestAutoUnitFollowsLanguage:
    """Under 'auto', _format_distance picks its unit from the reply language."""

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
