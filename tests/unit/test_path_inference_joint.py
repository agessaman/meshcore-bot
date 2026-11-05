#!/usr/bin/env python3
"""
Unit tests for the joint (whole-path) inference in modules/path_inference.py.

The per-hop selection resolves each hop on its own, so on a 1-byte prefix mesh a
hop is disambiguated against a guess at its neighbour and a prefix appearing
twice resolves to the same node both times. ``infer_path_nodes`` scores the path
as a whole instead: distance-based link costs, a mesh-graph discount, anchors at
the observer and sender, and one node per path.
"""

import logging

import pytest

from modules.path_inference import (
    PathInferenceConfig,
    _distance_cost,
    infer_path_nodes,
)

LOGGER = logging.getLogger("test_path_inference_joint")

# Puget Sound, so hops are a plausible LoRa distance apart.
BOT = (47.60, -122.33)
SENDER = (47.10, -122.40)
DENVER = (39.70, -104.99)


def rep(name, key_prefix, lat, lon):
    """A candidate repeater row of the shape gather_hop_candidates returns."""
    return {
        'name': name,
        'public_key': key_prefix.lower() + '0' * (64 - len(key_prefix)),
        'device_type': 'repeater',
        'last_seen': '2026-10-26T12:00:00',
        'last_heard': '2026-10-26T12:00:00',
        'last_advert_timestamp': '2026-10-26T12:00:00',
        'is_active': True,
        'latitude': lat,
        'longitude': lon,
        'city': None,
        'state': None,
        'country': None,
        'hop_count': None,
        'is_starred': False,
    }


@pytest.fixture
def cfg():
    return PathInferenceConfig(path_inference_mode='joint')


def infer(node_ids, candidates, cfg, **kwargs):
    kwargs.setdefault('observer_location', BOT)
    return infer_path_nodes(node_ids, candidates, cfg, logger=LOGGER, **kwargs)


def names(hops):
    return [h.repeater['name'] if h.repeater else None for h in hops]


@pytest.mark.unit
class TestDistanceCost:
    """Cost rises with distance and steepens past the knee."""

    def test_monotonic_below_knee(self, cfg):
        assert _distance_cost(10.0, cfg) < _distance_cost(40.0, cfg)

    def test_one_unit_is_the_configured_distance(self, cfg):
        assert _distance_cost(cfg.joint_cost_km_per_unit, cfg) == pytest.approx(1.0)

    def test_steepens_past_the_knee(self, cfg):
        knee = cfg.joint_long_hop_knee_km
        below = _distance_cost(knee, cfg) - _distance_cost(knee - 10, cfg)
        above = _distance_cost(knee + 10, cfg) - _distance_cost(knee, cfg)
        assert above == pytest.approx(below * cfg.joint_long_hop_multiplier)

    def test_unlisted_cost_is_about_one_long_hop(self, cfg):
        # The documented intent: the flat penalty is worth roughly an 80km hop.
        assert _distance_cost(80.0, cfg) == pytest.approx(cfg.joint_unlisted_hop_cost)


@pytest.mark.unit
class TestGeographicCoherence:
    """A path is scored as a whole, so an implausible hop loses."""

    def test_prefers_the_locally_coherent_candidate(self, cfg):
        near = rep('Near', 'aa11', 47.30, -122.35)
        far = rep('Far', 'aa22', *DENVER)
        hops = infer(['AA'], [[far, near]], cfg)
        assert names(hops) == ['Near']

    def test_lone_implausible_candidate_goes_unlisted(self, cfg):
        hops = infer(['AA'], [[rep('Far', 'aa22', *DENVER)]], cfg)
        assert hops[0].unlisted
        assert hops[0].repeater is None
        # The candidate was considered, it just lost to the flat penalty.
        assert hops[0].matches == 1

    def test_hop_with_no_candidates_is_unlisted(self, cfg):
        hops = infer(['AA'], [[]], cfg)
        assert hops[0].unlisted
        assert hops[0].matches == 0

    def test_sender_anchor_picks_the_candidate_near_the_sender(self, cfg):
        by_sender = rep('BySender', 'aa11', 47.12, -122.41)
        by_bot = rep('ByBot', 'aa22', 47.58, -122.32)
        hops = infer(['AA'], [[by_bot, by_sender]], cfg, sender_location=SENDER,
                     observer_location=None)
        assert names(hops) == ['BySender']

    def test_observer_anchor_picks_the_candidate_near_the_bot(self, cfg):
        by_sender = rep('BySender', 'aa11', 47.12, -122.41)
        by_bot = rep('ByBot', 'aa22', 47.58, -122.32)
        hops = infer(['AA'], [[by_sender, by_bot]], cfg, sender_location=None)
        assert names(hops) == ['ByBot']

    def test_empty_path_returns_nothing(self, cfg):
        assert infer([], [], cfg) == []


@pytest.mark.unit
class TestForwardOnce:
    """The firmware forwards a packet once, so no node takes two hops."""

    def test_repeated_prefix_resolves_to_two_nodes(self, cfg):
        first = rep('AA-south', 'aa11', 47.30, -122.35)
        second = rep('AA-north', 'aa33', 47.45, -122.30)
        middle = rep('BB', 'bb11', 47.20, -122.38)
        hops = infer(['AA', 'BB', 'AA'], [[first, second], [middle], [first, second]], cfg)
        keys = [h.repeater['public_key'] for h in hops if h.repeater]
        assert len(keys) == len(set(keys)), f"node reused across hops: {names(hops)}"

    def test_repeated_prefix_with_one_candidate_leaves_a_hop_unlisted(self, cfg):
        only = rep('AA-only', 'aa11', 47.30, -122.35)
        middle = rep('BB', 'bb11', 47.20, -122.38)
        hops = infer(['AA', 'BB', 'AA'], [[only], [middle], [only]], cfg)
        # One occurrence keeps the node; the other cannot reuse it.
        resolved = [h for h in hops if h.repeater and h.node_id == 'AA']
        assert len(resolved) == 1
        assert sum(1 for h in hops if h.node_id == 'AA' and h.unlisted) == 1

    def test_the_cheaper_position_is_the_one_given_up(self, cfg):
        # Only one AA candidate, far from the bot but next to the sender: the
        # first hop is where it belongs, so the last hop is the one released.
        shared = rep('AA-shared', 'aa11', 47.11, -122.40)
        middle = rep('BB', 'bb11', 47.30, -122.38)
        hops = infer(['AA', 'BB', 'AA'], [[shared], [middle], [shared]], cfg,
                     sender_location=SENDER)
        assert hops[0].repeater is not None
        assert hops[2].unlisted


@pytest.mark.unit
class TestGraphEvidence:
    """An observed link is cheaper, and only for the node that was observed.

    These use a real MeshGraph: the lookup leans on its prefix matching and on
    the public keys edges carry, so a stub would prove nothing.
    """

    @staticmethod
    def _observe(graph, frm, to, times=3, **keys):
        for _ in range(times):
            graph.add_edge(frm, to, **keys)

    def test_specific_edge_breaks_a_geographic_tie(self, cfg, mesh_graph):
        # Equidistant from BB, and listed so that the unobserved one wins on order.
        linked = rep('Linked', 'aa11', 47.40, -122.30)
        unlinked = rep('Unlinked', 'aa22', 47.20, -122.30)
        middle = rep('BB', 'bb11', 47.30, -122.30)
        self._observe(
            mesh_graph, 'aa11', 'bb11',
            from_public_key=linked['public_key'], to_public_key=middle['public_key'],
        )
        hops = infer(['AA', 'BB'], [[unlinked, linked], [middle]], cfg,
                     mesh_graph=mesh_graph, observer_location=None)
        assert names(hops)[0] == 'Linked'

    def test_edge_recorded_for_another_node_is_not_claimed(self, cfg, mesh_graph):
        # A 1-byte edge matches both candidates by prefix, but it was recorded
        # against one known public key, so only that node may claim it.
        first = rep('First', 'aa11', 47.40, -122.30)
        second = rep('Second', 'aa22', 47.20, -122.30)
        middle = rep('BB', 'bb11', 47.30, -122.30)
        self._observe(
            mesh_graph, 'aa', 'bb',
            from_public_key=second['public_key'], to_public_key=middle['public_key'],
        )
        hops = infer(['AA', 'BB'], [[first, second], [middle]], cfg,
                     mesh_graph=mesh_graph, observer_location=None)
        assert names(hops)[0] == 'Second'

    def test_edge_below_the_observation_floor_is_ignored(self, cfg, mesh_graph):
        linked = rep('Linked', 'aa11', 47.40, -122.30)
        unlinked = rep('Unlinked', 'aa22', 47.20, -122.30)
        middle = rep('BB', 'bb11', 47.30, -122.30)
        self._observe(
            mesh_graph, 'aa11', 'bb11', times=cfg.min_edge_observations - 1,
            from_public_key=linked['public_key'], to_public_key=middle['public_key'],
        )
        hops = infer(['AA', 'BB'], [[unlinked, linked], [middle]], cfg,
                     mesh_graph=mesh_graph, observer_location=None)
        # Without the discount the two tie, so the hop stays a guess.
        assert names(hops)[0] == 'Unlinked'
        assert not hops[0].confident

    def test_discount_can_be_disabled(self, cfg, mesh_graph):
        linked = rep('Linked', 'aa11', 47.40, -122.30)
        unlinked = rep('Unlinked', 'aa22', 47.20, -122.30)
        middle = rep('BB', 'bb11', 47.30, -122.30)
        self._observe(
            mesh_graph, 'aa11', 'bb11',
            from_public_key=linked['public_key'], to_public_key=middle['public_key'],
        )
        cfg.joint_graph_edge_discount = 1.0
        hops = infer(['AA', 'BB'], [[unlinked, linked], [middle]], cfg,
                     mesh_graph=mesh_graph, observer_location=None)
        assert names(hops)[0] == 'Unlinked'

    def test_an_observed_link_outweighs_a_longer_unobserved_one(self, cfg, mesh_graph):
        # Half the cost of a 40km observed hop beats a 25km unobserved one.
        observed = rep('Observed', 'aa11', 47.66, -122.30)
        closer = rep('Closer', 'aa22', 47.52, -122.30)
        middle = rep('BB', 'bb11', 47.30, -122.30)
        self._observe(
            mesh_graph, 'aa11', 'bb11',
            from_public_key=observed['public_key'], to_public_key=middle['public_key'],
        )
        hops = infer(['AA', 'BB'], [[closer, observed], [middle]], cfg,
                     mesh_graph=mesh_graph, observer_location=None)
        assert names(hops)[0] == 'Observed'

    def test_a_raising_graph_is_survived(self, cfg):
        class Exploding:
            def get_edge(self, *_args):
                raise RuntimeError("graph unavailable")

        near = rep('Near', 'aa11', 47.30, -122.35)
        hops = infer(['AA'], [[near]], cfg, mesh_graph=Exploding())
        assert names(hops) == ['Near']


@pytest.mark.unit
class TestConfidence:
    """Confidence is each hop's share of the probability over its own candidates."""

    def test_a_clear_winner_is_confident(self, cfg):
        near = rep('Near', 'aa11', 47.58, -122.32)
        far = rep('Far', 'aa22', *DENVER)
        hops = infer(['AA'], [[far, near]], cfg)
        assert hops[0].confident
        assert hops[0].confidence >= cfg.joint_confident_threshold

    def test_two_equally_plausible_candidates_are_a_guess(self, cfg):
        west = rep('West', 'aa11', 47.60, -122.45)
        east = rep('East', 'aa22', 47.60, -122.21)
        hops = infer(['AA'], [[west, east]], cfg)
        assert not hops[0].confident
        assert hops[0].confidence < cfg.joint_confident_threshold

    def test_alternatives_are_ranked_and_exclude_the_winner(self, cfg):
        near = rep('Near', 'aa11', 47.58, -122.32)
        mid = rep('Mid', 'aa22', 47.20, -122.40)
        far = rep('Far', 'aa33', *DENVER)
        hops = infer(['AA'], [[far, mid, near]], cfg)
        probabilities = [alt['probability'] for alt in hops[0].alternatives]
        assert probabilities == sorted(probabilities, reverse=True)
        chosen = hops[0].repeater['public_key']
        assert all(
            alt['repeater'] is None or alt['repeater']['public_key'] != chosen
            for alt in hops[0].alternatives
        )

    def test_confidence_threshold_is_configurable(self, cfg):
        west = rep('West', 'aa11', 47.60, -122.45)
        east = rep('East', 'aa22', 47.60, -122.21)
        cfg.joint_confident_threshold = 0.1
        hops = infer(['AA'], [[west, east]], cfg)
        assert hops[0].confident


@pytest.mark.unit
class TestCandidateFiltering:
    """Candidate lists are recency-filtered and capped, as the greedy path does."""

    def test_stale_candidates_are_dropped(self, cfg):
        fresh = rep('Fresh', 'aa11', 47.30, -122.35)
        stale = rep('Stale', 'aa22', 47.31, -122.36)
        for key in ('last_seen', 'last_heard', 'last_advert_timestamp'):
            stale[key] = '2020-01-01T00:00:00'
        hops = infer(['AA'], [[stale, fresh]], cfg)
        assert hops[0].matches == 1
        assert names(hops) == ['Fresh']

    def test_a_single_candidate_skips_the_recency_filter(self, cfg):
        # select_node_repeater does the same: one candidate is never aged out.
        stale = rep('Stale', 'aa22', 47.30, -122.35)
        for key in ('last_seen', 'last_heard', 'last_advert_timestamp'):
            stale[key] = '2020-01-01T00:00:00'
        hops = infer(['AA'], [[stale]], cfg)
        assert names(hops) == ['Stale']

    def test_candidates_are_capped(self, cfg):
        cfg.joint_max_candidates_per_hop = 3
        candidates = [rep(f'R{i}', f'aa{i:02d}', 47.30 + i / 100.0, -122.35) for i in range(10)]
        hops = infer(['AA'], [candidates], cfg)
        assert hops[0].matches == 3
