#!/usr/bin/env python3
"""
Unit tests for the PathCommand {path_distance} placeholder.

Distance is summed sender -> each resolved hop -> bot, and is deliberately blank
whenever any node in that chain lacks usable coordinates, so a partial sum never
gets reported as the real distance travelled.
"""


import pytest

from modules.commands.path_command import PathCommand


@pytest.mark.unit
class TestPathCommandDistance:
    """_calculate_path_distance_km and its {path_distance} rendering."""

    @pytest.fixture
    def path_command(self, mock_bot):
        cmd = PathCommand(mock_bot)
        # Bot sits at the origin; sender and hops are placed east of it.
        cmd.bot_latitude = 47.0
        cmd.bot_longitude = -122.0
        cmd._get_sender_location = lambda message=None: (47.0, -122.5)
        return cmd

    @staticmethod
    def _info(lat, lon, **over):
        base = {'found': True, 'collision': False, 'latitude': lat, 'longitude': lon}
        base.update(over)
        return base

    def test_sums_sender_through_hops_to_bot(self, path_command):
        info = {'AA': self._info(47.0, -122.4), 'BB': self._info(47.0, -122.2)}
        km = path_command._calculate_path_distance_km(['AA', 'BB'], info)
        assert km is not None
        # Three legs spanning 0.5 deg of longitude at 47N (~75.9 km/deg) => ~38 km.
        assert 37.0 < km < 39.0

    def test_renders_with_km_suffix(self, path_command):
        info = {'AA': self._info(47.0, -122.4), 'BB': self._info(47.0, -122.2)}
        path_command._last_path_distance_km = path_command._calculate_path_distance_km(
            ['AA', 'BB'], info
        )
        rendered = path_command._format_path_distance()
        assert rendered.endswith("km")
        assert rendered[0].isdigit()

    def test_blank_when_a_hop_has_no_coordinates(self, path_command):
        info = {'AA': self._info(47.0, -122.4), 'BB': self._info(None, None)}
        assert path_command._calculate_path_distance_km(['AA', 'BB'], info) is None

    def test_blank_when_a_hop_is_a_prefix_collision(self, path_command):
        info = {'AA': self._info(47.0, -122.4), 'BB': self._info(47.0, -122.2, collision=True)}
        assert path_command._calculate_path_distance_km(['AA', 'BB'], info) is None

    def test_blank_when_a_hop_is_unresolved(self, path_command):
        info = {'AA': self._info(47.0, -122.4), 'BB': {'found': False}}
        assert path_command._calculate_path_distance_km(['AA', 'BB'], info) is None

    def test_blank_when_hop_coordinates_are_null_island(self, path_command):
        """0,0 in the DB means 'unset', not a real position in the Gulf of Guinea."""
        info = {'AA': self._info(0, 0)}
        assert path_command._calculate_path_distance_km(['AA'], info) is None

    def test_blank_when_sender_location_unknown(self, path_command):
        path_command._get_sender_location = lambda message=None: None
        info = {'AA': self._info(47.0, -122.4)}
        assert path_command._calculate_path_distance_km(['AA'], info) is None

    def test_blank_when_bot_has_no_configured_position(self, path_command):
        path_command.bot_latitude = None
        info = {'AA': self._info(47.0, -122.4)}
        assert path_command._calculate_path_distance_km(['AA'], info) is None

    def test_placeholder_is_empty_string_when_unmeasurable(self, path_command):
        path_command._last_path_distance_km = None
        assert path_command._format_path_distance() == ""


@pytest.mark.unit
class TestDistanceThroughTheRealLookup:
    """Regression for a gap the unit tests above could not see.

    Those pass hand-built repeater_info dicts straight to the calculator. The
    resolution code that actually *builds* repeater_info was dropping latitude and
    longitude, so {path_distance} was always blank in production while the tests
    stayed green. These feed raw DB-shaped rows through _lookup_repeater_names via
    its lookup_func hook, so the real construction code runs.
    """

    @pytest.fixture
    def path_command(self, mock_bot):
        from modules.commands.path_command import PathCommand

        cmd = PathCommand(mock_bot)
        cmd.bot_latitude = 47.0
        cmd.bot_longitude = -122.0
        cmd._get_sender_location = lambda message=None: (47.0, -122.5)
        return cmd

    @staticmethod
    def _row(node_id, lat, lon):
        """A row shaped like the repeater query's output."""
        return {
            'name': f'Hop {node_id}',
            'public_key': node_id.lower() * 32,
            'device_type': 'Repeater',
            'last_seen': '2026-08-21 12:00:00',
            'last_heard': '2026-08-21 12:00:00',
            'last_advert_timestamp': None,
            'is_active': True,
            'latitude': lat,
            'longitude': lon,
            'city': 'Seattle',
            'state': 'WA',
            'country': 'US',
            'snr': 5.0,
            'is_starred': False,
        }

    @pytest.mark.asyncio
    async def test_coordinates_survive_the_real_repeater_info_builder(self, path_command):
        info = await path_command._lookup_repeater_names(
            ['AA'], lookup_func=lambda node_id: [self._row(node_id, 47.0, -122.3)]
        )
        assert info['AA']['found'] is True
        # The bug: these were dropped when repeater_info was constructed.
        assert info['AA']['latitude'] == 47.0
        assert info['AA']['longitude'] == -122.3

    @pytest.mark.asyncio
    async def test_distance_is_computed_end_to_end(self, path_command):
        info = await path_command._lookup_repeater_names(
            ['AA'], lookup_func=lambda node_id: [self._row(node_id, 47.0, -122.3)]
        )
        km = path_command._calculate_path_distance_km(['AA'], info)
        assert km is not None and km > 0

    @pytest.mark.asyncio
    async def test_row_without_coordinates_still_yields_no_distance(self, path_command):
        info = await path_command._lookup_repeater_names(
            ['AA'], lookup_func=lambda node_id: [self._row(node_id, None, None)]
        )
        assert path_command._calculate_path_distance_km(['AA'], info) is None


@pytest.mark.unit
class TestDistanceIsRequestScoped:
    """Two path commands can interleave: the decode awaits a database lookup and the
    dispatcher runs handlers as independent tasks. Neither may see the other's value."""

    @pytest.fixture
    def path_command(self, mock_bot):
        from modules.commands.path_command import PathCommand

        cmd = PathCommand(mock_bot)
        cmd.bot_latitude = 47.0
        cmd.bot_longitude = -122.0
        return cmd

    @staticmethod
    def _msg(sender):
        from modules.models import MeshMessage

        return MeshMessage(content="path", sender_id=sender, channel="#general")

    def test_a_request_without_a_distance_does_not_borrow_another(self, path_command):
        """The exact leak: B stores None, A stores a value, B must still render ''."""
        a, b = self._msg("a"), self._msg("b")
        path_command._store_path_distance(None, b)
        path_command._store_path_distance(42.0, a)

        assert path_command._format_path_distance(b) == ""
        assert path_command._format_path_distance(a) == "42.0km"

    def test_interleaved_requests_keep_their_own_values(self, path_command):
        a, b = self._msg("a"), self._msg("b")
        path_command._store_path_distance(10.0, a)
        path_command._store_path_distance(20.0, b)

        assert path_command._format_path_distance(a) == "10.0km"
        assert path_command._format_path_distance(b) == "20.0km"

    def test_sender_location_uses_the_request_not_shared_state(self, path_command):
        """_current_message is shared; a concurrent command can replace it."""
        a, b = self._msg("a"), self._msg("b")
        a.sender_pubkey = "aa" * 32
        b.sender_pubkey = "bb" * 32
        path_command._current_message = b  # as if another request overwrote it

        seen = {}
        path_command.bot.db_manager.execute_query = lambda q, params: seen.setdefault('key', params[0]) and []
        path_command._get_sender_location(a)
        assert seen['key'] == a.sender_pubkey
