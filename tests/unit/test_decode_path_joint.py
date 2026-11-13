#!/usr/bin/env python3
"""
Unit tests for decode_path_nodes under [Path_Command] path_inference_mode = joint.

The web decode returns one entry per hop already, so joint mode maps onto the
same shape. The default stays 'greedy', whose output the web viewer and its
callers depend on.
"""

import logging

import pytest

from modules.path_inference import decode_path_nodes

LOGGER = logging.getLogger("test_decode_path_joint")

BOT = (47.60, -122.33)
AA_SOUTH = ('AA-south', 'aa11', 47.30, -122.35)
AA_NORTH = ('AA-north', 'aa33', 47.45, -122.30)
BB_MID = ('BB-mid', 'bb11', 47.20, -122.38)
CC_FAR = ('CC-far', 'cc11', 39.70, -104.99)
CC_FAR_TOO = ('CC-far2', 'cc22', 39.75, -105.02)


def repeater(name, key_prefix, lat, lon):
    return {
        'name': name,
        'public_key': key_prefix.lower() + '0' * (64 - len(key_prefix)),
        'device_type': 'repeater',
        'role': 'repeater',
        'last_heard': '2026-10-26T12:00:00',
        'last_seen': '2026-10-26T12:00:00',
        'last_advert_timestamp': '2026-10-26T12:00:00',
        'latitude': lat,
        'longitude': lon,
        'city': None,
        'state': None,
        'country': None,
        'advert_count': 5,
        'signal_strength': None,
        'hop_count': None,
        'is_starred': 0,
    }


def lookup_from(*nodes):
    rows = [repeater(*node) for node in nodes]

    def _lookup(node_id):
        return [row for row in rows if row['public_key'].startswith(node_id.lower())]

    return _lookup


@pytest.fixture
def config(mock_bot):
    cfg = mock_bot.config
    for section in ('Bot', 'Path_Command'):
        if not cfg.has_section(section):
            cfg.add_section(section)
    cfg.set('Bot', 'bot_latitude', str(BOT[0]))
    cfg.set('Bot', 'bot_longitude', str(BOT[1]))
    return cfg


def decode(config, path_hex='AABBAA', *, mode=None, lookup=None, **kwargs):
    if mode is None:
        config.remove_option('Path_Command', 'path_inference_mode')
    else:
        config.set('Path_Command', 'path_inference_mode', mode)
    return decode_path_nodes(
        path_hex, 1,
        config=config, db_manager=None, logger=LOGGER, mesh_graph=None,
        lookup_func=lookup if lookup is not None else lookup_from(AA_SOUTH, AA_NORTH, BB_MID),
        **kwargs,
    )


@pytest.mark.unit
class TestGreedyRemainsTheDefault:
    """The web's existing output must not change without the flag."""

    def test_default_mode_is_greedy(self, config):
        nodes = decode(config)
        assert [node['node_id'] for node in nodes] == ['AA', 'BB', 'AA']
        # The per-hop path has no confidence field; joint adds one.
        assert all('confidence' not in node for node in nodes)

    def test_default_reuses_one_node_for_the_repeated_prefix(self, config):
        nodes = decode(config)
        assert nodes[0]['public_key'] == nodes[2]['public_key']


@pytest.mark.unit
class TestJointDecode:
    """Joint mode resolves the hops together."""

    def test_one_entry_per_hop(self, config):
        nodes = decode(config, mode='joint')
        assert [node['node_id'] for node in nodes] == ['AA', 'BB', 'AA']

    def test_repeated_prefix_resolves_to_two_nodes(self, config):
        nodes = decode(config, mode='joint')
        assert nodes[0]['public_key'] != nodes[2]['public_key']
        assert {node['name'] for node in nodes} == {'AA-south', 'AA-north', 'BB-mid'}

    def test_entries_keep_the_web_shape(self, config):
        node = decode(config, mode='joint')[1]
        assert node['found'] is True
        assert node['role'] == 'repeater'
        assert node['device_type'] == 'repeater'
        assert set(node) >= {
            'node_id', 'name', 'public_key', 'device_type', 'role',
            'found', 'geographic_guess', 'collision', 'matches',
        }

    def test_a_colliding_hop_is_marked(self, config):
        nodes = decode(config, mode='joint')
        assert nodes[0]['collision'] is True
        assert nodes[0]['matches'] == 2

    def test_a_lone_candidate_is_not_a_collision(self, config):
        nodes = decode(config, mode='joint')
        assert nodes[1]['collision'] is False
        assert nodes[1]['matches'] == 1

    def test_geographic_guess_follows_confidence(self, config):
        nodes = decode(config, mode='joint')
        for node in nodes:
            assert node['geographic_guess'] is (node['confidence'] < 0.7)

    def test_confidence_threshold_is_honored(self, config):
        config.set('Path_Command', 'joint_confident_threshold', '0.0')
        nodes = decode(config, mode='joint')
        assert all(node['geographic_guess'] is False for node in nodes)
        config.remove_option('Path_Command', 'joint_confident_threshold')

    def test_include_location_adds_coordinates(self, config):
        node = decode(config, mode='joint', include_location=True)[1]
        assert (node['latitude'], node['longitude']) == (BB_MID[2], BB_MID[3])
        assert node['last_seen'] == '2026-10-26T12:00:00'

    def test_location_is_left_out_by_default(self, config):
        node = decode(config, mode='joint')[1]
        assert 'latitude' not in node


@pytest.mark.unit
class TestJointRejections:
    """Hops no node fits are reported as not found, never bridged."""

    def test_an_unplaceable_hop_is_not_found(self, config):
        nodes = decode(
            config, 'BBCC', mode='joint',
            lookup=lookup_from(BB_MID, CC_FAR, CC_FAR_TOO),
        )
        assert nodes[0]['found'] is True
        assert nodes[1] == {'node_id': 'CC', 'name': None, 'found': False}

    def test_a_prefix_with_no_candidate_is_not_found(self, config):
        nodes = decode(config, 'BBFF', mode='joint', lookup=lookup_from(BB_MID))
        assert nodes[1]['found'] is False

    def test_a_failure_returns_nothing(self, config):
        def boom(_node_id):
            raise RuntimeError("boom")

        assert decode(config, mode='joint', lookup=boom) == []

    def test_no_hex_returns_nothing(self, config):
        assert decode(config, 'zz', mode='joint') == []
