"""Shared path-inference engine.

Decodes a hex path string (e.g. from an observed advert path) into a list of repeater nodes,
using recency-weighted scoring, geographic proximity, and the mesh graph for collision resolution.

This is the single implementation shared by the web viewer (POST /api/decode-path and
/api/mesh/resolve-path) and the bot `path` command (modules/commands/path_command.py). The
per-node *selection* logic (recency filtering, graph-based disambiguation, geographic proximity,
and the combination of the two) lives here once. Each caller keeps its own candidate gathering
and output shaping, because those genuinely differ (the web returns a list of minimal node dicts;
the bot returns a richer ``repeater_info`` mapping and has a device-contacts fallback).

Behavioral parity is preserved via :class:`PathInferenceConfig.bot_command`:

* ``bot_command=False`` (the web/default path) reproduces the web viewer's previous decode logic
  byte-for-byte. The web's ``decode_path_nodes`` output must not change — with one deliberate
  exception: ``_graph_path_validation_bonus_web`` used to compare uppercase ``path_context``
  tokens against lowercased stored hex, so its segment bonus could never fire. Fixing that case
  mismatch means the path-validation bonus (and therefore some confidence scores) can now be
  higher than before. No node resolves to a *different* repeater purely from case.
* ``bot_command=True`` (built by ``PathCommand``) enables the bot command's extra behaviors:
  SNR bonuses, zero-hop/SNR gating on graph evidence, ``proximity_method='path'`` selection with
  sender/previous/next-node proximity, simple-proximity tie-breakers, and the final-hop
  no-location penalty.
"""

import configparser
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from modules.utils import calculate_distance

# Minimum recency score for a repeater to survive collision filtering (~55h with the default
# 12h half-life). Shared by both callers.
MIN_RECENCY_THRESHOLD = 0.01


@dataclass
class PathInferenceConfig:
    """Snapshot of the selection knobs read from ``[Bot]``/``[Path_Command]``.

    ``decode_path_nodes`` builds one via :meth:`from_config` (``bot_command=False``); the bot
    ``PathCommand`` builds one from its own ``self.*`` attributes (``bot_command=True``) so that
    tests which mutate those attributes after construction are honored.
    """

    # Geographic
    geographic_guessing_enabled: bool = False
    bot_latitude: Optional[float] = None
    bot_longitude: Optional[float] = None
    geographic_scoring_enabled: bool = True
    proximity_method: str = 'simple'
    path_proximity_fallback: bool = True
    max_proximity_range: float = 200.0

    # Recency / weighting
    max_repeater_age_days: int = 14
    recency_weight: float = 0.4
    proximity_weight: float = 0.6
    recency_decay_half_life_hours: float = 12.0

    # Graph validation
    graph_based_validation: bool = True
    min_edge_observations: int = 3
    graph_use_bidirectional: bool = True
    graph_use_hop_position: bool = True
    graph_multi_hop_enabled: bool = True
    graph_multi_hop_max_hops: int = 2
    graph_geographic_combined: bool = False
    graph_geographic_weight: float = 0.7
    graph_confidence_override_threshold: float = 0.7
    graph_distance_penalty_enabled: bool = True
    graph_max_reasonable_hop_distance_km: float = 30.0
    graph_distance_penalty_strength: float = 0.3
    graph_zero_hop_bonus: float = 0.4
    graph_prefer_stored_keys: bool = True
    graph_final_hop_proximity_enabled: bool = True
    graph_final_hop_proximity_weight: float = 0.25
    graph_final_hop_max_distance: float = 0.0
    graph_final_hop_proximity_normalization_km: float = 200.0
    graph_final_hop_very_close_threshold_km: float = 10.0
    graph_final_hop_close_threshold_km: float = 30.0
    graph_final_hop_max_proximity_weight: float = 0.6
    graph_path_validation_max_bonus: float = 0.3
    graph_path_validation_obs_divisor: float = 50.0
    star_bias_multiplier: float = 2.5

    # Joint (whole-path) inference -- see `infer_path_nodes`. 'greedy' keeps the
    # per-hop selection above; 'joint' scores the path as a whole.
    path_inference_mode: str = 'greedy'
    joint_cost_km_per_unit: float = 50.0
    joint_long_hop_knee_km: float = 100.0
    joint_long_hop_multiplier: float = 4.0
    joint_graph_edge_discount: float = 0.5
    joint_unlisted_hop_cost: float = 1.6
    joint_confident_threshold: float = 0.7
    joint_confidence_temperature: float = 1.0
    joint_max_candidates_per_hop: int = 12

    # Master switch enabling the bot `path` command's extra behaviors (see module docstring).
    bot_command: bool = False

    @classmethod
    def joint_fields_from_config(cls, config) -> dict[str, Any]:
        """The ``joint_*`` fields and ``path_inference_mode``, read from ``[Path_Command]``.

        Shared so the two config paths cannot drift: :meth:`from_config` (web) and
        ``PathCommand._inference_config``, which builds the snapshot field by field.
        """
        mode = config.get('Path_Command', 'path_inference_mode', fallback='greedy')
        mode = mode.strip().lower() if mode else 'greedy'
        if mode not in ('greedy', 'joint'):
            mode = 'greedy'
        return {
            'path_inference_mode': mode,
            'joint_cost_km_per_unit': max(1.0, config.getfloat('Path_Command', 'joint_cost_km_per_unit', fallback=50.0)),
            'joint_long_hop_knee_km': max(1.0, config.getfloat('Path_Command', 'joint_long_hop_knee_km', fallback=100.0)),
            'joint_long_hop_multiplier': max(1.0, config.getfloat('Path_Command', 'joint_long_hop_multiplier', fallback=4.0)),
            'joint_graph_edge_discount': max(0.0, min(1.0, config.getfloat('Path_Command', 'joint_graph_edge_discount', fallback=0.5))),
            'joint_unlisted_hop_cost': max(0.0, config.getfloat('Path_Command', 'joint_unlisted_hop_cost', fallback=1.6)),
            'joint_confident_threshold': max(0.0, min(1.0, config.getfloat('Path_Command', 'joint_confident_threshold', fallback=0.7))),
            'joint_confidence_temperature': max(0.01, config.getfloat('Path_Command', 'joint_confidence_temperature', fallback=1.0)),
            'joint_max_candidates_per_hop': max(2, config.getint('Path_Command', 'joint_max_candidates_per_hop', fallback=12)),
        }

    @classmethod
    def from_config(cls, config) -> "PathInferenceConfig":
        """Read the selection knobs from a ConfigParser the way the web viewer always has.

        ``bot_command`` is False here; the web path never enables the bot-only behaviors.
        """
        geographic_guessing_enabled = False
        bot_latitude = None
        bot_longitude = None
        try:
            if config.has_section('Bot'):
                lat = config.getfloat('Bot', 'bot_latitude', fallback=None)
                lon = config.getfloat('Bot', 'bot_longitude', fallback=None)
                if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                    bot_latitude = lat
                    bot_longitude = lon
                    geographic_guessing_enabled = True
        except (ValueError, configparser.Error):  # malformed float or missing section
            pass

        preset = config.get('Path_Command', 'path_selection_preset', fallback='balanced').lower()
        if preset == 'geographic':
            preset_graph_confidence_threshold = 0.5
            preset_distance_threshold = 30.0
            preset_distance_penalty = 0.5
            preset_final_hop_weight = 0.4
        elif preset == 'graph':
            preset_graph_confidence_threshold = 0.9
            preset_distance_threshold = 50.0
            preset_distance_penalty = 0.2
            preset_final_hop_weight = 0.15
        else:  # 'balanced' (default)
            preset_graph_confidence_threshold = 0.7
            preset_distance_threshold = 30.0
            preset_distance_penalty = 0.3
            preset_final_hop_weight = 0.25

        recency_weight = max(0.0, min(1.0, config.getfloat('Path_Command', 'recency_weight', fallback=0.4)))
        graph_geographic_weight = max(0.0, min(1.0, config.getfloat('Path_Command', 'graph_geographic_weight', fallback=0.7)))
        graph_confidence_override_threshold = max(0.0, min(1.0, config.getfloat('Path_Command', 'graph_confidence_override_threshold', fallback=preset_graph_confidence_threshold)))
        graph_distance_penalty_strength = max(0.0, min(1.0, config.getfloat('Path_Command', 'graph_distance_penalty_strength', fallback=preset_distance_penalty)))
        graph_zero_hop_bonus = max(0.0, min(1.0, config.getfloat('Path_Command', 'graph_zero_hop_bonus', fallback=0.4)))
        graph_final_hop_proximity_weight = max(0.0, min(1.0, config.getfloat('Path_Command', 'graph_final_hop_proximity_weight', fallback=preset_final_hop_weight)))
        graph_final_hop_max_proximity_weight = max(0.0, min(1.0, config.getfloat('Path_Command', 'graph_final_hop_max_proximity_weight', fallback=0.6)))
        graph_path_validation_max_bonus = max(0.0, min(1.0, config.getfloat('Path_Command', 'graph_path_validation_max_bonus', fallback=0.3)))
        star_bias_multiplier = max(1.0, config.getfloat('Path_Command', 'star_bias_multiplier', fallback=2.5))

        return cls(
            geographic_guessing_enabled=geographic_guessing_enabled,
            bot_latitude=bot_latitude,
            bot_longitude=bot_longitude,
            geographic_scoring_enabled=config.getboolean('Path_Command', 'geographic_scoring_enabled', fallback=True),
            proximity_method=config.get('Path_Command', 'proximity_method', fallback='simple'),
            path_proximity_fallback=config.getboolean('Path_Command', 'path_proximity_fallback', fallback=True),
            max_proximity_range=config.getfloat('Path_Command', 'max_proximity_range', fallback=200.0),
            max_repeater_age_days=config.getint('Path_Command', 'max_repeater_age_days', fallback=14),
            recency_weight=recency_weight,
            proximity_weight=1.0 - recency_weight,
            recency_decay_half_life_hours=config.getfloat('Path_Command', 'recency_decay_half_life_hours', fallback=12.0),
            graph_based_validation=config.getboolean('Path_Command', 'graph_based_validation', fallback=True),
            min_edge_observations=config.getint('Path_Command', 'min_edge_observations', fallback=3),
            graph_use_bidirectional=config.getboolean('Path_Command', 'graph_use_bidirectional', fallback=True),
            graph_use_hop_position=config.getboolean('Path_Command', 'graph_use_hop_position', fallback=True),
            graph_multi_hop_enabled=config.getboolean('Path_Command', 'graph_multi_hop_enabled', fallback=True),
            graph_multi_hop_max_hops=config.getint('Path_Command', 'graph_multi_hop_max_hops', fallback=2),
            graph_geographic_combined=config.getboolean('Path_Command', 'graph_geographic_combined', fallback=False),
            graph_geographic_weight=graph_geographic_weight,
            graph_confidence_override_threshold=graph_confidence_override_threshold,
            graph_distance_penalty_enabled=config.getboolean('Path_Command', 'graph_distance_penalty_enabled', fallback=True),
            graph_max_reasonable_hop_distance_km=config.getfloat('Path_Command', 'graph_max_reasonable_hop_distance_km', fallback=preset_distance_threshold),
            graph_distance_penalty_strength=graph_distance_penalty_strength,
            graph_zero_hop_bonus=graph_zero_hop_bonus,
            graph_prefer_stored_keys=config.getboolean('Path_Command', 'graph_prefer_stored_keys', fallback=True),
            graph_final_hop_proximity_enabled=config.getboolean('Path_Command', 'graph_final_hop_proximity_enabled', fallback=True),
            graph_final_hop_proximity_weight=graph_final_hop_proximity_weight,
            graph_final_hop_max_distance=config.getfloat('Path_Command', 'graph_final_hop_max_distance', fallback=0.0),
            graph_final_hop_proximity_normalization_km=config.getfloat('Path_Command', 'graph_final_hop_proximity_normalization_km', fallback=200.0),
            graph_final_hop_very_close_threshold_km=config.getfloat('Path_Command', 'graph_final_hop_very_close_threshold_km', fallback=10.0),
            graph_final_hop_close_threshold_km=config.getfloat('Path_Command', 'graph_final_hop_close_threshold_km', fallback=30.0),
            graph_final_hop_max_proximity_weight=graph_final_hop_max_proximity_weight,
            graph_path_validation_max_bonus=graph_path_validation_max_bonus,
            graph_path_validation_obs_divisor=config.getfloat('Path_Command', 'graph_path_validation_obs_divisor', fallback=50.0),
            star_bias_multiplier=star_bias_multiplier,
            **cls.joint_fields_from_config(config),
            bot_command=False,
        )


@dataclass
class NodeSelection:
    """Result of resolving a single path node to a repeater.

    ``status`` is one of:
      * ``'single'`` — exactly one (recent) candidate; ``repeater`` is it.
      * ``'resolved'`` — a colliding prefix disambiguated with confidence >= 0.5; ``repeater``,
        ``confidence`` and ``method`` describe the winner.
      * ``'collision'`` — a colliding prefix that could not be confidently resolved; ``repeater``
        is None and ``recent_repeaters`` holds the candidates for display/fallback.
      * ``'not_found'`` — no candidate survived recency filtering.
    """

    status: str
    repeater: Optional[dict[str, Any]] = None
    confidence: float = 0.0
    method: Optional[str] = None
    matches: int = 0
    recent_repeaters: list[dict[str, Any]] = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Recency scoring
# ---------------------------------------------------------------------------

def calculate_recency_weighted_scores(repeaters, cfg: PathInferenceConfig):
    """Score repeaters 0.0-1.0 by recency (higher = more recently heard), sorted descending."""
    scored_repeaters = []
    now = datetime.now()

    for repeater in repeaters:
        most_recent_time = None
        for field in ('last_heard', 'last_advert_timestamp', 'last_seen'):
            value = repeater.get(field)
            if value:
                try:
                    if isinstance(value, str):
                        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    else:
                        dt = value
                    if most_recent_time is None or dt > most_recent_time:
                        most_recent_time = dt
                except Exception:
                    pass

        if most_recent_time is None:
            recency_score = 0.1
        else:
            hours_ago = (now - most_recent_time).total_seconds() / 3600.0
            recency_score = math.exp(-hours_ago / cfg.recency_decay_half_life_hours)
            recency_score = max(0.0, min(1.0, recency_score))

        scored_repeaters.append((repeater, recency_score))

    scored_repeaters.sort(key=lambda x: x[1], reverse=True)
    return scored_repeaters


def apply_tie_breakers(distances: list[tuple[float, dict[str, Any]]]) -> dict[str, Any]:
    """Break ties between equidistant repeaters deterministically (bot `path` command only)."""
    import contextlib

    min_distance = distances[0][0]
    tied_repeaters = [repeater for distance, repeater in distances if distance == min_distance]

    # Tie-breaker 1: prefer active repeaters
    active_repeaters = [r for r in tied_repeaters if r.get('is_active', True)]
    if len(active_repeaters) == 1:
        return active_repeaters[0]
    elif len(active_repeaters) > 1:
        tied_repeaters = active_repeaters

    # Tie-breaker 2: prefer most recent activity
    def get_recent_timestamp(repeater):
        timestamps = []
        for field in ('last_heard', 'last_advert_timestamp', 'last_seen'):
            value = repeater.get(field)
            if value:
                try:
                    if isinstance(value, str):
                        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    else:
                        dt = value
                    timestamps.append(dt)
                except Exception:
                    pass
        return max(timestamps) if timestamps else datetime.min

    try:
        tied_repeaters.sort(key=get_recent_timestamp, reverse=True)
    except Exception:
        pass

    # Tie-breaker 3: higher advertisement count
    with contextlib.suppress(BaseException):
        tied_repeaters.sort(key=lambda r: r.get('advert_count', 0), reverse=True)

    # Tie-breaker 4: alphabetical (deterministic)
    tied_repeaters.sort(key=lambda r: r.get('name', ''))

    return tied_repeaters[0]


# ---------------------------------------------------------------------------
# Geographic proximity
# ---------------------------------------------------------------------------

def get_node_location(node_id: str, cfg: PathInferenceConfig, *, db_manager, logger) -> Optional[tuple[float, float]]:
    """Look up a node's coordinates from complete_contact_tracking (bot `path` proximity only)."""
    try:
        if cfg.max_repeater_age_days > 0:
            query = f'''
                SELECT latitude, longitude, is_starred FROM complete_contact_tracking
                WHERE public_key LIKE ? AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0 AND role IN ('repeater', 'roomserver')
                AND (
                    (last_advert_timestamp IS NOT NULL AND last_advert_timestamp >= datetime('now', '-{cfg.max_repeater_age_days} days'))
                    OR (last_advert_timestamp IS NULL AND last_heard >= datetime('now', '-{cfg.max_repeater_age_days} days'))
                )
                ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 1
            '''
        else:
            query = '''
                SELECT latitude, longitude, is_starred FROM complete_contact_tracking
                WHERE public_key LIKE ? AND latitude IS NOT NULL AND longitude IS NOT NULL
                AND latitude != 0 AND longitude != 0 AND role IN ('repeater', 'roomserver')
                ORDER BY is_starred DESC, COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 1
            '''
        results = db_manager.execute_query(query, (f"{node_id}%",))
        if results:
            row = results[0]
            return (row['latitude'], row['longitude'])
        return None
    except Exception as e:
        logger.warning(f"Error getting location for node {node_id}: {e}")
        return None


def select_by_simple_proximity(repeaters_with_location, cfg: PathInferenceConfig, *, logger):
    """Select the closest repeater to the bot, with a strong recency bias."""
    if cfg.bot_latitude is None or cfg.bot_longitude is None:
        return None, 0.0

    scored_repeaters = calculate_recency_weighted_scores(repeaters_with_location, cfg)
    scored_repeaters = [(r, score) for r, score in scored_repeaters if score >= MIN_RECENCY_THRESHOLD]

    if not scored_repeaters:
        return None, 0.0

    if len(scored_repeaters) == 1:
        repeater, recency_score = scored_repeaters[0]
        distance = calculate_distance(cfg.bot_latitude, cfg.bot_longitude, repeater['latitude'], repeater['longitude'])
        if cfg.max_proximity_range > 0 and distance > cfg.max_proximity_range:
            return None, 0.0
        base_confidence = 0.4 + (recency_score * 0.5)
        return repeater, base_confidence

    combined_scores = []
    for repeater, recency_score in scored_repeaters:
        distance = calculate_distance(cfg.bot_latitude, cfg.bot_longitude, repeater['latitude'], repeater['longitude'])
        if cfg.max_proximity_range > 0 and distance > cfg.max_proximity_range:
            continue

        normalized_distance = min(distance / 1000.0, 1.0)
        proximity_score = 1.0 - normalized_distance
        combined_score = (recency_score * cfg.recency_weight) + (proximity_score * cfg.proximity_weight)

        if repeater.get('is_starred', False):
            combined_score *= cfg.star_bias_multiplier

        # SNR presence means a confirmed zero-hop neighbor; only the bot command carries SNR.
        if cfg.bot_command and repeater.get('snr') is not None:
            combined_score += combined_score * 0.2

        combined_scores.append((combined_score, distance, repeater))

    if not combined_scores:
        return None, 0.0

    combined_scores.sort(key=lambda x: x[0], reverse=True)
    best_score, best_distance, best_repeater = combined_scores[0]

    if len(combined_scores) == 1:
        confidence = 0.4 + (best_score * 0.5)
    else:
        second_best_score = combined_scores[1][0]
        score_ratio = best_score / second_best_score if second_best_score > 0 else 1.0
        if score_ratio > 1.5:
            confidence = 0.9
        elif score_ratio > 1.2:
            confidence = 0.8
        elif score_ratio > 1.1:
            confidence = 0.7
        elif cfg.bot_command:
            # Scores too similar — fall back to a deterministic tie-breaker.
            distances_for_tiebreaker = [(d, r) for _, d, r in combined_scores]
            return apply_tie_breakers(distances_for_tiebreaker), 0.5
        else:
            confidence = 0.5

    return best_repeater, confidence


def select_by_dual_proximity(repeaters, prev_location, next_location, cfg: PathInferenceConfig, *, logger):
    """Select a repeater near both its previous and next path nodes (bot `path` proximity)."""
    scored_repeaters = calculate_recency_weighted_scores(repeaters, cfg)
    scored_repeaters = [(r, score) for r, score in scored_repeaters if score >= MIN_RECENCY_THRESHOLD]
    if not scored_repeaters:
        return None, 0.0

    best_repeater = None
    best_combined_score = 0.0

    for repeater, recency_score in scored_repeaters:
        prev_distance = calculate_distance(prev_location[0], prev_location[1], repeater['latitude'], repeater['longitude'])
        next_distance = calculate_distance(next_location[0], next_location[1], repeater['latitude'], repeater['longitude'])

        avg_distance = (prev_distance + next_distance) / 2
        normalized_distance = min(avg_distance / 1000.0, 1.0)
        proximity_score = 1.0 - normalized_distance
        combined_score = (recency_score * cfg.recency_weight) + (proximity_score * cfg.proximity_weight)

        if repeater.get('is_starred', False):
            combined_score *= cfg.star_bias_multiplier

        if repeater.get('snr') is not None:
            combined_score += combined_score * 0.2

        if combined_score > best_combined_score:
            best_combined_score = combined_score
            best_repeater = repeater

    if best_repeater:
        if cfg.max_proximity_range > 0:
            prev_dist = calculate_distance(prev_location[0], prev_location[1], best_repeater['latitude'], best_repeater['longitude'])
            next_dist = calculate_distance(next_location[0], next_location[1], best_repeater['latitude'], best_repeater['longitude'])
            if prev_dist > cfg.max_proximity_range or next_dist > cfg.max_proximity_range:
                return None, 0.0
        confidence = 0.4 + (best_combined_score * 0.5)
        return best_repeater, confidence

    return None, 0.0


def select_by_single_proximity(repeaters, reference_location, direction, cfg: PathInferenceConfig, *, logger):
    """Select a repeater near a single reference point (sender/bot/prev/next)."""
    scored_repeaters = calculate_recency_weighted_scores(repeaters, cfg)
    scored_repeaters = [(r, score) for r, score in scored_repeaters if score >= MIN_RECENCY_THRESHOLD]
    if not scored_repeaters:
        return None, 0.0

    # The first hop (from sender) and last hop (to bot) prioritize distance above all else.
    if direction in ("bot", "sender"):
        proximity_weight = 1.0
        recency_weight = 0.0
    else:
        proximity_weight = cfg.proximity_weight
        recency_weight = cfg.recency_weight

    best_repeater = None
    best_combined_score = 0.0

    for repeater, recency_score in scored_repeaters:
        distance = calculate_distance(reference_location[0], reference_location[1], repeater['latitude'], repeater['longitude'])
        if cfg.max_proximity_range > 0 and distance > cfg.max_proximity_range:
            continue

        normalized_distance = min(distance / 1000.0, 1.0)
        proximity_score = 1.0 - normalized_distance
        combined_score = (recency_score * recency_weight) + (proximity_score * proximity_weight)

        if repeater.get('is_starred', False):
            combined_score *= cfg.star_bias_multiplier

        if repeater.get('snr') is not None:
            combined_score += combined_score * 0.2

        if combined_score > best_combined_score:
            best_combined_score = combined_score
            best_repeater = repeater

    if best_repeater:
        confidence = 0.4 + (best_combined_score * 0.5)
        return best_repeater, confidence

    return None, 0.0


def select_by_path_proximity(repeaters_with_location, node_id, path_context, sender_location, cfg: PathInferenceConfig, *, db_manager, logger, node_index=None):
    """Select a repeater based on proximity to its previous/next nodes in the path."""
    try:
        scored_repeaters = calculate_recency_weighted_scores(repeaters_with_location, cfg)
        recent_repeaters = [r for r, score in scored_repeaters if score >= MIN_RECENCY_THRESHOLD]
        if not recent_repeaters:
            return None, 0.0

        current_index = _resolve_node_index(node_id, path_context, node_index)
        if current_index == -1:
            return None, 0.0

        prev_location = None
        next_location = None
        if current_index > 0:
            prev_location = get_node_location(path_context[current_index - 1], cfg, db_manager=db_manager, logger=logger)
        if current_index < len(path_context) - 1:
            next_location = get_node_location(path_context[current_index + 1], cfg, db_manager=db_manager, logger=logger)

        # First repeater: prioritize the sender as the source.
        is_first_repeater = (current_index == 0)
        if is_first_repeater and sender_location:
            return select_by_single_proximity(recent_repeaters, sender_location, "sender", cfg, logger=logger)

        # Last repeater: prioritize the bot as the destination.
        is_last_repeater = (current_index == len(path_context) - 1)
        if is_last_repeater and cfg.geographic_guessing_enabled:
            if cfg.bot_latitude is not None and cfg.bot_longitude is not None:
                bot_location = (cfg.bot_latitude, cfg.bot_longitude)
                return select_by_single_proximity(recent_repeaters, bot_location, "bot", cfg, logger=logger)

        if prev_location and next_location:
            return select_by_dual_proximity(recent_repeaters, prev_location, next_location, cfg, logger=logger)
        elif prev_location:
            return select_by_single_proximity(recent_repeaters, prev_location, "previous", cfg, logger=logger)
        elif next_location:
            return select_by_single_proximity(recent_repeaters, next_location, "next", cfg, logger=logger)
        else:
            return None, 0.0
    except Exception as e:
        logger.warning(f"Error in path proximity calculation: {e}")
        return None, 0.0


def _resolve_node_index(node_id, path_context, node_index=None) -> int:
    """Position of ``node_id`` within ``path_context``, or -1.

    Prefers the caller's loop index. With 1-byte prefixes the same hop value can
    legitimately appear more than once in a path, and ``list.index()`` would
    always resolve to the first occurrence — giving every repeat the neighbours
    and hop position of hop #1.
    """
    if node_index is not None and 0 <= node_index < len(path_context):
        return node_index
    try:
        return path_context.index(node_id)
    except (ValueError, AttributeError):
        return -1


def select_geographic(repeaters, node_id, path_context, cfg: PathInferenceConfig, *, db_manager, logger, sender_location=None, node_index=None):
    """Pick the best repeater geographically, dispatching on ``proximity_method``.

    For the web/default path (``bot_command=False``) this is always simple proximity, matching the
    web viewer's previous behavior (which ignored ``proximity_method``).
    """
    if not cfg.geographic_guessing_enabled:
        return None, 0.0

    if cfg.bot_command:
        repeaters_with_location = []
        for repeater in repeaters:
            lat = repeater.get('latitude')
            lon = repeater.get('longitude')
            if lat is not None and lon is not None and not (lat == 0.0 and lon == 0.0):
                repeaters_with_location.append(repeater)
        if not repeaters_with_location:
            return None, 0.0

        if cfg.proximity_method == 'path' and path_context and node_id:
            result = select_by_path_proximity(repeaters_with_location, node_id, path_context, sender_location, cfg, db_manager=db_manager, logger=logger, node_index=node_index)
            if result[0] is not None:
                return result
            elif cfg.path_proximity_fallback:
                return select_by_simple_proximity(repeaters_with_location, cfg, logger=logger)
            else:
                return None, 0.0
        return select_by_simple_proximity(repeaters_with_location, cfg, logger=logger)

    # Web/default: truthiness filter (excludes None and 0.0) + simple proximity.
    repeaters_with_location = [r for r in repeaters if r.get('latitude') and r.get('longitude')]
    if not repeaters_with_location:
        return None, 0.0
    return select_by_simple_proximity(repeaters_with_location, cfg, logger=logger)


# ---------------------------------------------------------------------------
# Graph-based selection
# ---------------------------------------------------------------------------

def _graph_path_validation_bonus_web(candidate_public_key, path_context, current_index, cfg, *, db_manager, logger):
    """Web variant of the path-validation bonus (prefix-match aware)."""
    path_validation_bonus = 0.0
    if not (candidate_public_key and len(path_context) > 1):
        return 0.0
    try:
        query = '''
            SELECT path_hex, observation_count, last_seen, from_prefix, to_prefix, bytes_per_hop
            FROM observed_paths
            WHERE public_key = ? AND packet_type = 'advert'
            ORDER BY observation_count DESC, last_seen DESC
            LIMIT 10
        '''
        stored_paths = db_manager.execute_query(query, (candidate_public_key,))
        if stored_paths:
            decoded_path_hex = ''.join([node.lower() for node in path_context])
            path_prefix_up_to_current = ''.join([node.lower() for node in path_context[:current_index]])

            for stored_path in stored_paths:
                stored_hex = stored_path.get('path_hex', '').lower()
                obs_count = stored_path.get('observation_count', 1)
                if stored_hex:
                    n = (stored_path.get('bytes_per_hop') or 1) * 2
                    if n <= 0:
                        n = 2
                    stored_nodes = [stored_hex[i:i+n] for i in range(0, len(stored_hex), n)]
                    if (len(stored_hex) % n) != 0:
                        stored_nodes = [stored_hex[i:i+2] for i in range(0, len(stored_hex), 2)]
                    # path_context tokens are uppercase (see node_ids above) while
                    # stored_hex is lowercased, so fold before comparing segments.
                    decoded_nodes = ([node.lower() for node in path_context] if path_context
                                     else [decoded_path_hex[i:i+n] for i in range(0, len(decoded_path_hex), n)])

                    common_segments = 0
                    min_len = min(len(stored_nodes), len(decoded_nodes))
                    for i in range(min_len):
                        if stored_nodes[i] == decoded_nodes[i]:
                            common_segments += 1
                        else:
                            break

                    prefix_match = False
                    if path_prefix_up_to_current and len(stored_hex) >= len(path_prefix_up_to_current):
                        if stored_hex.startswith(path_prefix_up_to_current):
                            prefix_match = True

                    if common_segments >= 2 or prefix_match:
                        if prefix_match and common_segments >= current_index:
                            segment_bonus = min(cfg.graph_path_validation_max_bonus, 0.1 * (current_index + 1))
                        else:
                            segment_bonus = min(0.2, 0.05 * common_segments)
                        obs_bonus = min(0.15, obs_count / cfg.graph_path_validation_obs_divisor)
                        path_validation_bonus = max(path_validation_bonus, segment_bonus + obs_bonus)
                        path_validation_bonus = min(cfg.graph_path_validation_max_bonus, path_validation_bonus)
                        if path_validation_bonus >= cfg.graph_path_validation_max_bonus * 0.9:
                            break
    except (sqlite3.Error, OSError, KeyError, ValueError) as _score_err:
        logger.debug("Path-scoring graph query failed: %s", _score_err)
    return path_validation_bonus


def _graph_path_validation_bonus_bot(candidate_public_key, candidate_prefix, path_context, cfg, *, db_manager, logger):
    """Bot `path` command variant of the path-validation bonus (preserved verbatim)."""
    path_validation_bonus = 0.0
    if not (candidate_public_key and len(path_context) > 1):
        return 0.0
    try:
        query = '''
            SELECT path_hex, observation_count, last_seen, from_prefix, to_prefix, bytes_per_hop
            FROM observed_paths
            WHERE public_key = ? AND packet_type = 'advert'
            ORDER BY observation_count DESC, last_seen DESC
            LIMIT 10
        '''
        stored_paths = db_manager.execute_query(query, (candidate_public_key,))
        if stored_paths:
            decoded_path_hex = ''.join([node.lower() for node in path_context])
            path_n = len(path_context[0]) if path_context else 0

            for stored_path in stored_paths:
                stored_hex = stored_path.get('path_hex', '').lower()
                obs_count = stored_path.get('observation_count', 1)
                if not stored_hex:
                    continue
                stored_n = (stored_path.get('bytes_per_hop') or 1) * 2
                if stored_n <= 0:
                    stored_n = 2
                if path_n != stored_n:
                    continue
                stored_nodes = [stored_hex[i:i+stored_n] for i in range(0, len(stored_hex), stored_n)]
                if (len(stored_hex) % stored_n) != 0:
                    stored_nodes = [stored_hex[i:i+2] for i in range(0, len(stored_hex), 2)]
                decoded_nodes = [decoded_path_hex[i:i+path_n] for i in range(0, len(decoded_path_hex), path_n)]
                if (len(decoded_path_hex) % path_n) != 0:
                    decoded_nodes = [decoded_path_hex[i:i+2] for i in range(0, len(decoded_path_hex), 2)]

                # Scoring runs for every stored path, not just ragged ones: for a
                # uniform-width path the remainder above is always 0, which used to
                # leave this whole block unreachable.
                common_segments = 0
                min_len = min(len(stored_nodes), len(decoded_nodes))
                for i in range(min_len):
                    if stored_nodes[i] == decoded_nodes[i]:
                        common_segments += 1
                    else:
                        break

                if common_segments >= 2:
                    segment_bonus = min(0.2, 0.05 * common_segments)
                    obs_bonus = min(0.15, obs_count / cfg.graph_path_validation_obs_divisor)
                    path_validation_bonus = max(path_validation_bonus, segment_bonus + obs_bonus)
                    path_validation_bonus = min(cfg.graph_path_validation_max_bonus, path_validation_bonus)
                    if path_validation_bonus >= cfg.graph_path_validation_max_bonus * 0.9:
                        break
    except Exception as e:
        logger.debug(f"Error checking path validation for {candidate_prefix}: {e}")
    return path_validation_bonus


def select_repeater_by_graph(repeaters, node_id, path_context, cfg: PathInferenceConfig, *,
                             mesh_graph, db_manager, logger, graph_n, path_prefix_hex_chars=None,
                             node_index=None):
    """Select the best repeater for a colliding prefix using mesh-graph evidence.

    ``graph_n`` is the configured prefix width in hex chars (edge keys use it). When the path was
    decoded with wider (multi-byte) node IDs, ``path_prefix_hex_chars`` is the node-ID width used
    for candidate public-key matching; graph lookups are normalized to ``graph_n``.
    """
    if not cfg.graph_based_validation or not mesh_graph:
        return None, 0.0, None

    if graph_n <= 0:
        graph_n = 2
    if path_prefix_hex_chars is None:
        path_prefix_hex_chars = graph_n
    prefix_n = path_prefix_hex_chars if path_prefix_hex_chars >= 2 else graph_n

    current_index = _resolve_node_index(node_id, path_context, node_index)
    if current_index == -1:
        return None, 0.0, None

    prev_node_id = path_context[current_index - 1] if current_index > 0 else None
    next_node_id = path_context[current_index + 1] if current_index < len(path_context) - 1 else None
    prev_norm = (prev_node_id[:graph_n].lower() if prev_node_id and len(prev_node_id) > graph_n else (prev_node_id.lower() if prev_node_id else None))
    next_norm = (next_node_id[:graph_n].lower() if next_node_id and len(next_node_id) > graph_n else (next_node_id.lower() if next_node_id else None))

    best_repeater = None
    best_score = 0.0
    best_method = None

    # find_intermediate_nodes() depends only on (prev_norm, next_norm), so compute it at most once.
    multi_hop_map: Optional[dict[str, float]] = None

    for repeater in repeaters:
        pk = repeater.get('public_key') or ''
        candidate_prefix = pk[:prefix_n].lower() if pk else None
        candidate_public_key = pk.lower() if pk else None
        if not candidate_prefix:
            continue
        candidate_norm = candidate_prefix[:graph_n].lower() if len(candidate_prefix) > graph_n else candidate_prefix

        graph_score = mesh_graph.get_candidate_score(
            candidate_norm, prev_norm, next_norm, cfg.min_edge_observations,
            hop_position=current_index if cfg.graph_use_hop_position else None,
            use_bidirectional=cfg.graph_use_bidirectional,
            use_hop_position=cfg.graph_use_hop_position,
        )

        stored_key_bonus = 0.0
        if cfg.graph_prefer_stored_keys and candidate_public_key:
            if prev_norm:
                prev_to_candidate_edge = mesh_graph.get_edge(prev_norm, candidate_norm)
                if prev_to_candidate_edge:
                    stored_to_key = prev_to_candidate_edge.get('to_public_key', '').lower() if prev_to_candidate_edge.get('to_public_key') else None
                    if stored_to_key and stored_to_key == candidate_public_key:
                        stored_key_bonus = max(stored_key_bonus, 0.4)
            if next_norm:
                candidate_to_next_edge = mesh_graph.get_edge(candidate_norm, next_norm)
                if candidate_to_next_edge:
                    stored_from_key = candidate_to_next_edge.get('from_public_key', '').lower() if candidate_to_next_edge.get('from_public_key') else None
                    if stored_from_key and stored_from_key == candidate_public_key:
                        stored_key_bonus = max(stored_key_bonus, 0.4)

        # Zero-hop / SNR bonus. The bot command requires graph evidence (graph_score > 0) before
        # trusting a direct-heard signal, and adds an SNR-confirmed bonus; the web path applies the
        # zero-hop bonus unconditionally and has no SNR data.
        zero_hop_bonus = 0.0
        snr_bonus = 0.0
        hop_count = repeater.get('hop_count')
        snr = repeater.get('snr')
        if cfg.bot_command:
            if hop_count is not None and hop_count == 0 and graph_score > 0:
                zero_hop_bonus = cfg.graph_zero_hop_bonus
            if snr is not None and graph_score > 0:
                snr_bonus = cfg.graph_zero_hop_bonus * 1.2
        else:
            if hop_count is not None and hop_count == 0:
                zero_hop_bonus = cfg.graph_zero_hop_bonus

        graph_score_with_bonus = min(1.0, graph_score + stored_key_bonus + zero_hop_bonus + snr_bonus)

        # The bot adds path-validation before multi-hop; the web adds it at the end (see below).
        if cfg.bot_command:
            path_validation_bonus = _graph_path_validation_bonus_bot(
                candidate_public_key, candidate_prefix, path_context, cfg, db_manager=db_manager, logger=logger
            )
            graph_score_with_bonus = min(1.0, graph_score_with_bonus + path_validation_bonus)

        multi_hop_score = 0.0
        if cfg.graph_multi_hop_enabled and graph_score_with_bonus < 0.6 and prev_norm and next_norm:
            if multi_hop_map is None:
                multi_hop_map = dict(mesh_graph.find_intermediate_nodes(
                    prev_norm, next_norm, cfg.min_edge_observations, max_hops=cfg.graph_multi_hop_max_hops
                ))
            multi_hop_score = multi_hop_map.get(candidate_norm, 0.0)

        candidate_score = max(graph_score_with_bonus, multi_hop_score)
        method = 'graph_multihop' if multi_hop_score > graph_score_with_bonus else 'graph'

        # Distance penalty for intermediate hops (identical for both callers).
        if cfg.graph_distance_penalty_enabled and next_norm is not None:
            repeater_lat = repeater.get('latitude')
            repeater_lon = repeater.get('longitude')
            if repeater_lat is not None and repeater_lon is not None:
                max_distance = 0.0
                if prev_norm:
                    prev_to_candidate_edge = mesh_graph.get_edge(prev_norm, candidate_norm)
                    if prev_to_candidate_edge and prev_to_candidate_edge.get('geographic_distance'):
                        max_distance = max(max_distance, prev_to_candidate_edge.get('geographic_distance'))
                if next_norm:
                    candidate_to_next_edge = mesh_graph.get_edge(candidate_norm, next_norm)
                    if candidate_to_next_edge and candidate_to_next_edge.get('geographic_distance'):
                        max_distance = max(max_distance, candidate_to_next_edge.get('geographic_distance'))

                if max_distance > cfg.graph_max_reasonable_hop_distance_km:
                    excess_distance = max_distance - cfg.graph_max_reasonable_hop_distance_km
                    normalized_excess = min(excess_distance / cfg.graph_max_reasonable_hop_distance_km, 1.0)
                    penalty = normalized_excess * cfg.graph_distance_penalty_strength
                    candidate_score = candidate_score * (1.0 - penalty)
                elif max_distance > 0:
                    if max_distance > cfg.graph_max_reasonable_hop_distance_km * 0.8:
                        small_penalty = (max_distance - cfg.graph_max_reasonable_hop_distance_km * 0.8) / (cfg.graph_max_reasonable_hop_distance_km * 0.2) * cfg.graph_distance_penalty_strength * 0.5
                        candidate_score = candidate_score * (1.0 - small_penalty)

        # Final-hop bot-proximity bonus.
        if next_norm is None and cfg.graph_final_hop_proximity_enabled:
            if cfg.bot_latitude is not None and cfg.bot_longitude is not None:
                repeater_lat = repeater.get('latitude')
                repeater_lon = repeater.get('longitude')
                if cfg.bot_command:
                    has_valid_location = (repeater_lat is not None and repeater_lon is not None and not (repeater_lat == 0.0 and repeater_lon == 0.0))
                    if has_valid_location:
                        distance = calculate_distance(cfg.bot_latitude, cfg.bot_longitude, repeater_lat, repeater_lon)
                        if cfg.graph_final_hop_max_distance > 0 and distance > cfg.graph_final_hop_max_distance:
                            pass  # beyond max distance — skip proximity bonus
                        else:
                            candidate_score = _final_hop_blend(candidate_score, distance, cfg)
                    else:
                        # No usable location — penalize so located neighbors win the final hop.
                        candidate_score = candidate_score * 0.5
                else:
                    if repeater_lat is not None and repeater_lon is not None:
                        distance = calculate_distance(cfg.bot_latitude, cfg.bot_longitude, repeater_lat, repeater_lon)
                        if cfg.graph_final_hop_max_distance > 0 and distance > cfg.graph_final_hop_max_distance:
                            candidate_score *= 0.3
                        else:
                            candidate_score = _final_hop_blend(candidate_score, distance, cfg)

        if not cfg.bot_command:
            path_validation_bonus = _graph_path_validation_bonus_web(
                candidate_public_key, path_context, current_index, cfg, db_manager=db_manager, logger=logger
            )
            candidate_score = min(1.0, candidate_score + path_validation_bonus)

        if repeater.get('is_starred', False):
            candidate_score *= cfg.star_bias_multiplier

        if candidate_score > best_score:
            best_score = candidate_score
            best_repeater = repeater
            best_method = method

    if best_repeater and best_score > 0.0:
        confidence = min(1.0, best_score) if best_score <= 1.0 else 0.95 + (min(0.05, (best_score - 1.0) / cfg.star_bias_multiplier))
        return best_repeater, confidence, best_method or 'graph'

    return None, 0.0, None


def _final_hop_blend(candidate_score, distance, cfg: PathInferenceConfig):
    """Blend the graph score with bot-proximity for the final hop (shared formula)."""
    normalized_distance = min(distance / cfg.graph_final_hop_proximity_normalization_km, 1.0)
    proximity_score = 1.0 - normalized_distance

    effective_weight = cfg.graph_final_hop_proximity_weight
    if distance < cfg.graph_final_hop_very_close_threshold_km:
        effective_weight = min(cfg.graph_final_hop_max_proximity_weight, cfg.graph_final_hop_proximity_weight * 2.0)
    elif distance < cfg.graph_final_hop_close_threshold_km:
        effective_weight = min(0.5, cfg.graph_final_hop_proximity_weight * 1.5)

    return candidate_score * (1.0 - effective_weight) + proximity_score * effective_weight


# ---------------------------------------------------------------------------
# Per-node orchestration (shared by both callers)
# ---------------------------------------------------------------------------

def _has_valid_location(repeater) -> bool:
    lat = repeater.get('latitude')
    lon = repeater.get('longitude')
    return lat is not None and lon is not None and not (lat == 0.0 and lon == 0.0)


def select_node_repeater(node_id, candidates, node_ids, cfg: PathInferenceConfig, *,
                         mesh_graph, db_manager, logger, graph_n, sender_location=None,
                         node_index=None) -> NodeSelection:
    """Resolve one path node from its candidate repeaters (recency + graph + geographic).

    Returns a :class:`NodeSelection`; each caller maps it to its own output shape.
    """
    if not candidates:
        return NodeSelection('not_found', matches=0, recent_repeaters=[])

    if len(candidates) == 1:
        recent_repeaters = candidates
    else:
        scored_repeaters = calculate_recency_weighted_scores(candidates, cfg)
        recent_repeaters = [r for r, score in scored_repeaters if score >= MIN_RECENCY_THRESHOLD]

    if len(recent_repeaters) > 1:
        graph_repeater = None
        graph_confidence = 0.0
        method = None
        geo_repeater = None
        geo_confidence = 0.0

        if cfg.graph_based_validation and mesh_graph:
            graph_repeater, graph_confidence, method = select_repeater_by_graph(
                recent_repeaters, node_id, node_ids, cfg,
                mesh_graph=mesh_graph, db_manager=db_manager, logger=logger,
                graph_n=graph_n, path_prefix_hex_chars=len(node_id) if node_id else graph_n,
                node_index=node_index,
            )

        if cfg.geographic_guessing_enabled:
            geo_repeater, geo_confidence = select_geographic(
                recent_repeaters, node_id, node_ids, cfg,
                db_manager=db_manager, logger=logger, sender_location=sender_location,
                node_index=node_index,
            )

        selected_repeater, confidence, method = _combine_selection(
            node_id, node_ids, cfg, graph_repeater, graph_confidence, geo_repeater, geo_confidence, method,
            node_index=node_index,
        )

        if selected_repeater and confidence >= 0.5:
            return NodeSelection('resolved', selected_repeater, confidence, method, len(recent_repeaters), recent_repeaters)
        return NodeSelection('collision', None, confidence, method, len(recent_repeaters), recent_repeaters)

    elif len(recent_repeaters) == 1:
        return NodeSelection('single', recent_repeaters[0], 1.0, None, 1, recent_repeaters)

    return NodeSelection('not_found', matches=0, recent_repeaters=[])


def _combine_selection(node_id, node_ids, cfg, graph_repeater, graph_confidence, geo_repeater, geo_confidence, method,
                       node_index=None):
    """Choose between graph and geographic selections (web vs bot semantics)."""
    selected_repeater = None
    confidence = 0.0
    # Compare by position, not value: a 1-byte prefix that repeats in the path
    # would otherwise mark every occurrence as the final hop.
    if not node_ids:
        is_final_hop = False
    elif node_index is not None and 0 <= node_index < len(node_ids):
        is_final_hop = (node_index == len(node_ids) - 1)
    else:
        is_final_hop = (node_id == node_ids[-1])

    if cfg.bot_command:
        if cfg.graph_geographic_combined and graph_repeater and geo_repeater:
            graph_pubkey = graph_repeater.get('public_key', '')
            geo_pubkey = geo_repeater.get('public_key', '')
            if graph_pubkey and geo_pubkey and graph_pubkey == geo_pubkey:
                confidence = graph_confidence * cfg.graph_geographic_weight + geo_confidence * (1.0 - cfg.graph_geographic_weight)
                selected_repeater = graph_repeater
                method = 'graph_geographic_combined'
            else:
                if is_final_hop and graph_repeater and not _has_valid_location(graph_repeater) and geo_repeater:
                    selected_repeater = geo_repeater
                    confidence = geo_confidence
                    method = 'geographic'
                elif graph_confidence > geo_confidence:
                    selected_repeater = graph_repeater
                    confidence = graph_confidence
                    method = method or 'graph'
                else:
                    selected_repeater = geo_repeater
                    confidence = geo_confidence
                    method = 'geographic'
        else:
            if graph_repeater and graph_confidence >= cfg.graph_confidence_override_threshold:
                if is_final_hop and not _has_valid_location(graph_repeater) and geo_repeater:
                    selected_repeater = geo_repeater
                    confidence = geo_confidence
                    method = 'geographic'
                else:
                    selected_repeater = graph_repeater
                    confidence = graph_confidence
                    method = method or 'graph'
            elif not graph_repeater or graph_confidence < cfg.graph_confidence_override_threshold:
                if geo_repeater and (not graph_repeater or geo_confidence > graph_confidence):
                    selected_repeater = geo_repeater
                    confidence = geo_confidence
                    method = 'geographic'
                elif graph_repeater:
                    if is_final_hop and not _has_valid_location(graph_repeater) and geo_repeater:
                        selected_repeater = geo_repeater
                        confidence = geo_confidence
                        method = 'geographic'
                    else:
                        selected_repeater = graph_repeater
                        confidence = graph_confidence
                        method = method or 'graph'
        return selected_repeater, confidence, method

    # Web/default semantics.
    if cfg.graph_geographic_combined and graph_repeater and geo_repeater:
        graph_pubkey = graph_repeater.get('public_key', '')
        geo_pubkey = geo_repeater.get('public_key', '')
        if graph_pubkey and geo_pubkey and graph_pubkey == geo_pubkey:
            confidence = graph_confidence * cfg.graph_geographic_weight + geo_confidence * (1.0 - cfg.graph_geographic_weight)
            selected_repeater = graph_repeater
        else:
            if graph_confidence > geo_confidence:
                selected_repeater = graph_repeater
                confidence = graph_confidence
            else:
                selected_repeater = geo_repeater
                confidence = geo_confidence
    else:
        if is_final_hop and geo_repeater and geo_confidence >= 0.6:
            if not graph_repeater or geo_confidence >= graph_confidence * 0.9:
                selected_repeater = geo_repeater
                confidence = geo_confidence
            elif graph_repeater:
                selected_repeater = graph_repeater
                confidence = graph_confidence
        elif graph_repeater and graph_confidence >= cfg.graph_confidence_override_threshold:
            selected_repeater = graph_repeater
            confidence = graph_confidence
        elif not graph_repeater or graph_confidence < cfg.graph_confidence_override_threshold:
            if geo_repeater and (not graph_repeater or geo_confidence > graph_confidence):
                selected_repeater = geo_repeater
                confidence = geo_confidence
            elif graph_repeater:
                selected_repeater = graph_repeater
                confidence = graph_confidence
    return selected_repeater, confidence, method


# ---------------------------------------------------------------------------
# Candidate gathering (shared)
# ---------------------------------------------------------------------------

def gather_hop_candidates(node_id, cfg: PathInferenceConfig, *, db_manager,
                          lookup_func: Optional[Callable[[str], list[dict[str, Any]]]] = None,
                          ) -> list[dict[str, Any]]:
    """Candidate repeaters/room servers whose public key starts with ``node_id``.

    Extracted verbatim from ``decode_path_nodes`` so the joint inference path and the
    web decode share one query and one row shape. ``lookup_func``, when given, replaces
    the database query (callers and tests that inject candidates).
    """
    if lookup_func is not None:
        results = lookup_func(node_id)
    elif cfg.max_repeater_age_days > 0:
        query = f'''
            SELECT name, public_key, device_type, last_heard, last_heard as last_seen,
                   last_advert_timestamp, latitude, longitude, city, state, country,
                   advert_count, signal_strength, hop_count, role, is_starred
            FROM complete_contact_tracking
            WHERE public_key LIKE ? AND role IN ('repeater', 'roomserver')
            AND (
                (last_advert_timestamp IS NOT NULL AND last_advert_timestamp >= datetime('now', 'localtime', '-{cfg.max_repeater_age_days} days'))
                OR (last_advert_timestamp IS NULL AND last_heard >= datetime('now', 'localtime', '-{cfg.max_repeater_age_days} days'))
            )
            ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
        '''
        results = db_manager.execute_query(query, (f"{node_id}%",))
    else:
        query = '''
            SELECT name, public_key, device_type, last_heard, last_heard as last_seen,
                   last_advert_timestamp, latitude, longitude, city, state, country,
                   advert_count, signal_strength, hop_count, role, is_starred
            FROM complete_contact_tracking
            WHERE public_key LIKE ? AND role IN ('repeater', 'roomserver')
            ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
        '''
        results = db_manager.execute_query(query, (f"{node_id}%",))

    if not results:
        return []

    return [
        {
            'name': row['name'],
            'public_key': row['public_key'],
            'device_type': row['device_type'],
            'last_seen': row['last_seen'],
            'last_heard': row.get('last_heard', row['last_seen']),
            'last_advert_timestamp': row.get('last_advert_timestamp'),
            'is_active': True,
            'latitude': row['latitude'],
            'longitude': row['longitude'],
            'city': row['city'],
            'state': row['state'],
            'country': row['country'],
            'hop_count': row.get('hop_count'),
            'is_starred': bool(row.get('is_starred', 0)),
        } for row in results
    ]


# ---------------------------------------------------------------------------
# Joint (whole-path) inference
# ---------------------------------------------------------------------------
#
# The per-hop selection above resolves each hop on its own, and the neighbour
# context it leans on is itself a guess: `get_node_location` takes the most
# recently heard node sharing the neighbour's prefix. With 1-byte prefixes many
# repeaters share each prefix, so hop N is disambiguated against a coin flip at
# hop N+-1, nothing checks that the chosen nodes form a route a packet could
# actually take, and a prefix appearing twice in one path resolves to the same
# node both times.
#
# `infer_path_nodes` scores the path as a whole instead: every hop's candidates
# become states in a chain, each link is charged a cost that grows with distance
# and is discounted (halved by default) when the mesh graph has seen that link,
# and a min-cost pass over the chain picks the cheapest sequence. The route is anchored at the observer
# (the bot heard the last hop) and, when known, at the sender. A node that wins
# two positions is banned from whichever one is cheaper to give up, because the
# firmware forwards a packet once.


@dataclass
class HopInference:
    """One hop of a jointly inferred path.

    ``repeater`` is None when the hop is *unlisted*: no known node fits there
    better than the flat ``joint_unlisted_hop_cost`` penalty. ``confidence`` is
    this hop's share of the probability mass over its own candidates, so a hop
    with one plausible node scores high even on a path that is poor overall.
    """

    node_id: str
    repeater: Optional[dict[str, Any]] = None
    confidence: float = 0.0
    confident: bool = False
    unlisted: bool = True
    matches: int = 0
    alternatives: list[dict[str, Any]] = None  # type: ignore[assignment]


def _hop_location(repeater: Optional[dict[str, Any]]) -> Optional[tuple[float, float]]:
    """A candidate's coordinates, or None when it has none worth using."""
    if not repeater:
        return None
    lat = repeater.get('latitude')
    lon = repeater.get('longitude')
    if lat is None or lon is None:
        return None
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    return (lat, lon)


def _candidate_key(repeater: Optional[dict[str, Any]]) -> Optional[str]:
    """Identity used for the forward-once constraint, or None when unknowable."""
    if not repeater:
        return None
    key = (repeater.get('public_key') or '').strip().lower()
    return key or None


def _distance_cost(km: float, cfg: PathInferenceConfig) -> float:
    """Cost of covering ``km`` in one hop, rising steeply past the knee.

    One unit is ``joint_cost_km_per_unit`` of ordinary propagation. Past
    ``joint_long_hop_knee_km`` each further kilometre costs
    ``joint_long_hop_multiplier`` times as much, so an implausible hop is
    expensive rather than merely worse.
    """
    knee = cfg.joint_long_hop_knee_km
    raw = km if km <= knee else knee + (km - knee) * cfg.joint_long_hop_multiplier
    return raw / cfg.joint_cost_km_per_unit


def _graph_query_prefix(candidate_key: str) -> str:
    """The most specific prefix MeshGraph accepts for a candidate (6, else 4, else 2 chars).

    Edges are stored at 2, 4 or 6 hex chars and ``get_edge`` prefix-matches, longest
    first, so querying at full width still finds a coarse edge while letting a
    wider one win. Querying at the *path's* width instead would match every
    candidate sharing the hop's prefix, which is exactly what has to be told apart.
    """
    for width in (6, 4, 2):
        if len(candidate_key) >= width:
            return candidate_key[:width]
    return ''


def _edge_endpoint_fits(edge: dict[str, Any], endpoint: str, candidate_key: str) -> bool:
    """True unless the edge records a *different* node at ``endpoint``.

    An edge recorded against a known public key (adverts carry one) identifies its
    endpoint exactly, so a candidate that merely shares the prefix must not claim
    it. An edge with no recorded key is only as specific as its prefix, and the
    prefix match that found it stands.
    """
    stored = (edge.get(f'{endpoint}_public_key') or '').strip().lower()
    if not stored:
        return True
    return stored.startswith(candidate_key) or candidate_key.startswith(stored)


def _graph_link_discount(cost: float, a, b, cfg: PathInferenceConfig, mesh_graph) -> float:
    """Discount ``cost`` when the mesh graph has observed a link between ``a`` and ``b``."""
    if not mesh_graph:
        return cost
    key_a = _candidate_key(a)
    key_b = _candidate_key(b)
    if not key_a or not key_b:
        return cost
    prefix_a = _graph_query_prefix(key_a)
    prefix_b = _graph_query_prefix(key_b)
    if not prefix_a or not prefix_b:
        return cost
    try:
        for frm, to, frm_key, to_key in (
            (prefix_a, prefix_b, key_a, key_b),
            (prefix_b, prefix_a, key_b, key_a),
        ):
            edge = mesh_graph.get_edge(frm, to)
            if not edge or edge.get('observation_count', 0) < cfg.min_edge_observations:
                continue
            if _edge_endpoint_fits(edge, 'from', frm_key) and _edge_endpoint_fits(edge, 'to', to_key):
                return cost * cfg.joint_graph_edge_discount
    except Exception:
        return cost
    return cost


def _transition_cost(a, b, cfg: PathInferenceConfig, mesh_graph) -> float:
    """Cost of ``a`` forwarding to ``b``; either being unlisted costs the flat penalty."""
    if a is None or b is None:
        return cfg.joint_unlisted_hop_cost
    loc_a = _hop_location(a)
    loc_b = _hop_location(b)
    if loc_a is None or loc_b is None:
        # No geometry to judge, but a known link is still evidence.
        return _graph_link_discount(cfg.joint_unlisted_hop_cost, a, b, cfg, mesh_graph)
    cost = _distance_cost(calculate_distance(loc_a[0], loc_a[1], loc_b[0], loc_b[1]), cfg)
    return _graph_link_discount(cost, a, b, cfg, mesh_graph)


def _anchor_cost(repeater, anchor: Optional[tuple[float, float]], cfg: PathInferenceConfig) -> float:
    """Cost of tying an end of the path to a known position (sender or observer)."""
    if repeater is None:
        return cfg.joint_unlisted_hop_cost
    if anchor is None:
        return 0.0
    loc = _hop_location(repeater)
    if loc is None:
        return cfg.joint_unlisted_hop_cost
    return _distance_cost(calculate_distance(loc[0], loc[1], anchor[0], anchor[1]), cfg)


def _hop_states(candidates: list[dict[str, Any]], cfg: PathInferenceConfig) -> list[dict[str, Any]]:
    """Recency-filter one hop's candidates, capped, in the greedy path's order.

    A single candidate skips the recency filter, as ``select_node_repeater`` does.
    """
    if not candidates:
        return []
    if len(candidates) == 1:
        kept = list(candidates)
    else:
        scored = calculate_recency_weighted_scores(candidates, cfg)
        kept = [r for r, score in scored if score >= MIN_RECENCY_THRESHOLD]
    return kept[:cfg.joint_max_candidates_per_hop]


def _solve_chain(states, entry_costs, exit_costs, trans):
    """Min-cost path through the chain, with per-state min-marginals.

    ``entry_costs`` applies to hop 0 and ``exit_costs`` to the last hop: those are
    the two ends the anchors tie down.

    Returns ``(chosen, totals, best_total)`` where ``chosen[i]`` indexes the
    winning state at hop ``i`` and ``totals[i][c]`` is the cost of the cheapest
    whole path that uses state ``c`` at hop ``i`` (its min-marginal).
    """
    n = len(states)
    inf = float('inf')

    forward = [[inf] * len(states[i]) for i in range(n)]
    parent = [[-1] * len(states[i]) for i in range(n)]
    for c in range(len(states[0])):
        forward[0][c] = entry_costs[c]
    for i in range(1, n):
        for c in range(len(states[i])):
            best = inf
            best_p = -1
            for p in range(len(states[i - 1])):
                if forward[i - 1][p] == inf:
                    continue
                cost = forward[i - 1][p] + trans(i - 1, p, i, c)
                if cost < best:
                    best = cost
                    best_p = p
            forward[i][c] = best
            parent[i][c] = best_p

    backward = [[inf] * len(states[i]) for i in range(n)]
    for c in range(len(states[n - 1])):
        backward[n - 1][c] = exit_costs[c]
    for i in range(n - 2, -1, -1):
        for c in range(len(states[i])):
            best = inf
            for nxt in range(len(states[i + 1])):
                if backward[i + 1][nxt] == inf:
                    continue
                cost = trans(i, c, i + 1, nxt) + backward[i + 1][nxt]
                if cost < best:
                    best = cost
            backward[i][c] = best

    totals = [
        [
            forward[i][c] + backward[i][c]
            if forward[i][c] != inf and backward[i][c] != inf else inf
            for c in range(len(states[i]))
        ]
        for i in range(n)
    ]

    last = n - 1
    end = min(range(len(states[last])), key=lambda c: totals[last][c])
    best_total = totals[last][end]
    chosen = [0] * n
    chosen[last] = end
    for i in range(last, 0, -1):
        chosen[i - 1] = parent[i][chosen[i]]
    return chosen, totals, best_total


def _softmax_confidence(costs: list[float], chosen: int, cfg: PathInferenceConfig) -> list[float]:
    """Turn a hop's min-marginal costs into probabilities over its states."""
    inf = float('inf')
    finite = [c for c in costs if c != inf]
    if not finite:
        probs = [0.0] * len(costs)
        if 0 <= chosen < len(probs):
            probs[chosen] = 1.0
        return probs
    floor = min(finite)
    weights = [
        0.0 if cost == inf else math.exp(-(cost - floor) / cfg.joint_confidence_temperature)
        for cost in costs
    ]
    total = sum(weights)
    if total <= 0:
        probs = [0.0] * len(costs)
        if 0 <= chosen < len(probs):
            probs[chosen] = 1.0
        return probs
    return [w / total for w in weights]


def infer_path_nodes(node_ids, candidates_by_hop, cfg: PathInferenceConfig, *,
                     mesh_graph=None, logger,
                     sender_location: Optional[tuple[float, float]] = None,
                     observer_location: Optional[tuple[float, float]] = None,
                     ) -> list[HopInference]:
    """Infer the most likely node for every hop of one observed path, jointly.

    Args:
        node_ids: hop prefixes in path order (hop 0 heard the sender).
        candidates_by_hop: candidate repeater dicts per hop, same order and length
            as ``node_ids`` (see :func:`gather_hop_candidates`).
        cfg: tuning; the ``joint_*`` fields and ``min_edge_observations`` apply here.
        mesh_graph: MeshGraph for the link discount, or None to score on geometry alone.
            Looked up by each candidate's own public key, not by the hop's prefix.
        sender_location: the sender's position, anchoring hop 0 when known.
        observer_location: the receiving node's position, anchoring the last hop.

    Returns:
        One :class:`HopInference` per hop, in path order.
    """
    if not node_ids:
        return []

    hop_candidates = [_hop_states(candidates_by_hop[i] if i < len(candidates_by_hop) else [], cfg)
                      for i in range(len(node_ids))]

    # Every hop may also be unlisted (the trailing None state), so an implausible
    # candidate loses to the flat penalty instead of inventing a 950km hop.
    trans_cache: dict[tuple[int, int, int, int], float] = {}
    banned: set[tuple[int, str]] = set()

    # Assigned on the first pass below, which always runs; typed up front so the
    # reads after the loop are not reads of an Optional.
    states: list[list[Optional[dict[str, Any]]]] = []
    chosen: list[int] = []
    totals: list[list[float]] = []
    best_total = float('inf')
    for _ in range(max(4, len(node_ids) * 2)):
        states = [
            [rep for rep in hop_candidates[i] if (i, _candidate_key(rep)) not in banned] + [None]
            for i in range(len(node_ids))
        ]
        trans_cache.clear()

        def trans(i, c, j, d, _states=states):
            key = (i, c, j, d)
            cached = trans_cache.get(key)
            if cached is None:
                cached = _transition_cost(_states[i][c], _states[j][d], cfg, mesh_graph)
                trans_cache[key] = cached
            return cached

        entry_costs = [_anchor_cost(rep, sender_location, cfg) for rep in states[0]]
        exit_costs = [_anchor_cost(rep, observer_location, cfg) for rep in states[-1]]

        chosen, totals, best_total = _solve_chain(states, entry_costs, exit_costs, trans)

        # Forward-once: a node that won two positions keeps the one it would be
        # dearer to lose, and is banned from the other.
        positions: dict[str, list[int]] = {}
        for i, c in enumerate(chosen):
            key = _candidate_key(states[i][c])
            if key:
                positions.setdefault(key, []).append(i)
        duplicates = {k: v for k, v in positions.items() if len(v) > 1}
        if not duplicates:
            break

        for key, where in duplicates.items():
            regrets = []
            for i in where:
                rival_costs = [totals[i][c] for c in range(len(states[i])) if c != chosen[i]]
                finite = [cost for cost in rival_costs if cost != float('inf')]
                regrets.append((min(finite) - best_total if finite else float('inf'), i))
            regrets.sort()
            # Keep the costliest position to give up; ban the rest.
            for _regret, i in regrets[:-1]:
                banned.add((i, key))

    results: list[HopInference] = []
    for i, node_id in enumerate(node_ids):
        probs = _softmax_confidence(totals[i], chosen[i], cfg)
        repeater = states[i][chosen[i]]
        confidence = probs[chosen[i]] if chosen[i] < len(probs) else 0.0
        alternatives: list[dict[str, Any]] = [
            {'repeater': states[i][c], 'probability': probs[c]}
            for c in range(len(states[i])) if c != chosen[i] and probs[c] > 0.0
        ]
        alternatives.sort(key=lambda a: a['probability'], reverse=True)
        results.append(HopInference(
            node_id=node_id,
            repeater=repeater,
            confidence=confidence,
            confident=confidence >= cfg.joint_confident_threshold,
            unlisted=repeater is None,
            matches=len(hop_candidates[i]),
            alternatives=alternatives,
        ))

    logger.debug(
        "Joint path inference: %s hops, cost %.2f, %s unlisted, %s confident",
        len(results), best_total,
        sum(1 for r in results if r.unlisted),
        sum(1 for r in results if r.confident),
    )
    return results


# ---------------------------------------------------------------------------
# Public entrypoint (web viewer)
# ---------------------------------------------------------------------------

def _joint_hop_entry(hop: HopInference, *, include_location: bool) -> dict[str, Any]:
    """One :class:`HopInference` in ``decode_path_nodes``'s output shape.

    ``geographic_guess`` is the web's "this is a guess" flag, so it follows
    ``confident`` rather than the greedy path's hardcoded 0.8, and ``collision``
    says the prefix had more than one candidate whether or not one was chosen.
    ``confidence`` is additional: the greedy shape carries none.
    """
    if hop.repeater is None:
        return {'node_id': hop.node_id, 'name': None, 'found': False}

    repeater = hop.repeater
    entry = {
        'node_id': hop.node_id,
        'name': repeater['name'],
        'public_key': repeater['public_key'],
        'device_type': repeater['device_type'],
        'role': repeater.get('role', 'repeater'),
        'found': True,
        'geographic_guess': not hop.confident,
        'collision': hop.matches > 1,
        'matches': hop.matches,
        'confidence': hop.confidence,
    }
    if include_location:
        entry.update({
            'latitude': repeater.get('latitude'),
            'longitude': repeater.get('longitude'),
            'last_seen': repeater.get('last_seen'),
        })
    return entry


def _decode_path_nodes_joint(node_ids, cfg: PathInferenceConfig, *, db_manager, logger,
                             mesh_graph, include_location: bool,
                             lookup_func) -> list[dict[str, Any]]:
    """``decode_path_nodes`` with the whole path resolved at once."""
    try:
        candidates = [
            gather_hop_candidates(node_id, cfg, db_manager=db_manager, lookup_func=lookup_func)
            for node_id in node_ids
        ]
        observer = None
        if cfg.bot_latitude is not None and cfg.bot_longitude is not None:
            observer = (cfg.bot_latitude, cfg.bot_longitude)
        hops = infer_path_nodes(
            node_ids, candidates, cfg,
            mesh_graph=mesh_graph, logger=logger, observer_location=observer,
        )
    except Exception as e:
        logger.error(f"Error decoding path: {e}", exc_info=True)
        return []
    return [_joint_hop_entry(hop, include_location=include_location) for hop in hops]


def decode_path_nodes(
    path_hex: str,
    bytes_per_hop: int | None = None,
    *,
    config,
    db_manager,
    logger,
    mesh_graph=None,
    include_location: bool = False,
    lookup_func: Optional[Callable[[str], list[dict[str, Any]]]] = None,
) -> list[dict[str, Any]]:
    """Decode a hex path string to repeater nodes (see module docstring).

    Args:
        path_hex: hex path string (continuous or separated).
        bytes_per_hop: 1/2/3 when known from a packet/contact; else derived from config.
        config: ConfigParser-like accessor for [Bot]/[Path_Command] settings.
        db_manager: object exposing execute_query(sql, params) -> list[dict].
        logger: logging.Logger.
        mesh_graph: MeshGraph instance, or None to disable graph-based selection.
        include_location: when True, found nodes also carry latitude/longitude/last_seen
            (used by the mesh-map /api/mesh/resolve-path caller).
        lookup_func: optional candidate provider, Callable(node_id) -> list[repeater dict]. When
            given, candidates come from it instead of the database query (used by callers/tests
            that inject candidates).
    """
    import re

    cfg = PathInferenceConfig.from_config(config)

    # Parse the path input - use bytes_per_hop when provided (e.g. from packet/contact)
    if bytes_per_hop is not None and bytes_per_hop in (1, 2, 3):
        prefix_hex_chars = bytes_per_hop * 2
    else:
        prefix_hex_chars = config.getint('Bot', 'prefix_bytes', fallback=1) * 2
    if prefix_hex_chars <= 0:
        prefix_hex_chars = 2

    path_input_clean = path_hex.replace(' ', '').replace(',', '').replace(':', '')
    if re.match(r'^[0-9a-fA-F]{4,}$', path_input_clean):
        hex_matches = [path_input_clean[i:i+prefix_hex_chars] for i in range(0, len(path_input_clean), prefix_hex_chars)]
        if (len(path_input_clean) % prefix_hex_chars) != 0 and prefix_hex_chars > 2:
            hex_matches = [path_input_clean[i:i+2] for i in range(0, len(path_input_clean), 2)]
    else:
        path_input = path_hex.replace(',', ' ').replace(':', ' ')
        hex_pattern = rf'[0-9a-fA-F]{{{prefix_hex_chars}}}'
        hex_matches = re.findall(hex_pattern, path_input)
        if not hex_matches and prefix_hex_chars > 2:
            hex_pattern = r'[0-9a-fA-F]{2}'
            hex_matches = re.findall(hex_pattern, path_input)

    if not hex_matches:
        return []

    node_ids = [match.upper() for match in hex_matches]

    if cfg.path_inference_mode == 'joint':
        return _decode_path_nodes_joint(
            node_ids, cfg, db_manager=db_manager, logger=logger, mesh_graph=mesh_graph,
            include_location=include_location, lookup_func=lookup_func,
        )

    def _loc(rep):
        if not include_location:
            return {}
        return {
            'latitude': rep.get('latitude'),
            'longitude': rep.get('longitude'),
            'last_seen': rep.get('last_seen'),
        }

    decoded_path = []
    try:
        for node_index, node_id in enumerate(node_ids):
            repeaters_data = gather_hop_candidates(
                node_id, cfg, db_manager=db_manager, lookup_func=lookup_func,
            )

            if repeaters_data:
                selection = select_node_repeater(
                    node_id, repeaters_data, node_ids, cfg,
                    mesh_graph=mesh_graph, db_manager=db_manager, logger=logger,
                    graph_n=prefix_hex_chars,
                    node_index=node_index,
                )

                if selection.status == 'resolved':
                    selected_repeater = selection.repeater
                    if selected_repeater is not None:
                        decoded_path.append({
                            'node_id': node_id,
                            'name': selected_repeater['name'],
                            'public_key': selected_repeater['public_key'],
                            'device_type': selected_repeater['device_type'],
                            'role': selected_repeater.get('role', 'repeater'),
                            'found': True,
                            'geographic_guess': selection.confidence < 0.8,
                            'collision': True,
                            'matches': selection.matches,
                            **_loc(selected_repeater),
                        })
                elif selection.status == 'collision':
                    fallback = selection.recent_repeaters[0]
                    decoded_path.append({
                        'node_id': node_id,
                        'name': fallback['name'],
                        'public_key': fallback['public_key'],
                        'device_type': fallback['device_type'],
                        'role': fallback.get('role', 'repeater'),
                        'found': True,
                        'geographic_guess': True,
                        'collision': True,
                        'matches': selection.matches,
                        **_loc(fallback),
                    })
                elif selection.status == 'single':
                    repeater = selection.repeater
                    if repeater is not None:
                        decoded_path.append({
                            'node_id': node_id,
                            'name': repeater['name'],
                            'public_key': repeater['public_key'],
                            'device_type': repeater['device_type'],
                            'role': repeater.get('role', 'repeater'),
                            'found': True,
                            'geographic_guess': False,
                            'collision': False,
                            'matches': 1,
                            **_loc(repeater),
                        })
                else:
                    decoded_path.append({
                        'node_id': node_id,
                        'name': None,
                        'found': False,
                    })
            else:
                decoded_path.append({
                    'node_id': node_id,
                    'name': None,
                    'found': False,
                })
    except Exception as e:
        logger.error(f"Error decoding path: {e}", exc_info=True)
        return []

    return decoded_path
