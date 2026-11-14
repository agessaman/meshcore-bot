"""The ``{path_names}`` placeholder: a message's route as short repeater names.

``a1b2,c3d4,e5f6 (3 hops)`` renders as ``BlackHls▸KalamaSum▸KelsoG3``. A hop
gets a name only when it is a confident match on a multi-byte path:

- the path uses at least 2 bytes per hop (a 1-byte prefix is shared by too
  many repeaters for a name to mean anything), and
- the prefix has exactly one recent candidate, or the shared path-inference
  engine resolves a collision with at least ``path_names_min_confidence``.

Any other hop keeps its hex prefix in place, so the route is never shortened
or reordered and an unsure hop never shows a guessed name. A direct message
renders ``Direct``, the same as ``{path}``.

Settings live in ``[Path_Command]`` next to the resolution settings they
depend on, and are read on each render so a config reload applies them.
"""

from __future__ import annotations

from typing import Any, Optional

from .mesh_paths import (
    bytes_per_hop_from_routing_and_nodes,
    extract_path_node_ids_from_message,
    message_hop_count,
)
from .name_shortener import pack_names

PLACEHOLDER = "path_names"
SECTION = "Path_Command"

DEFAULT_MAX_NAME_LENGTH = 10
DEFAULT_MAX_BYTES = 60
DEFAULT_SEPARATOR = "▸"
DEFAULT_MIN_CONFIDENCE = 0.9


def template_uses_path_names(template: Optional[str]) -> bool:
    """True when *template* references the placeholder, so the lookup is worth doing."""
    return bool(template) and ("{" + PLACEHOLDER) in (template or "")


def _settings(config: Any) -> dict[str, Any]:
    def get_int(key: str, default: int) -> int:
        try:
            return config.getint(SECTION, key, fallback=default)
        except (ValueError, TypeError):
            return default

    try:
        min_confidence = config.getfloat(SECTION, "path_names_min_confidence", fallback=DEFAULT_MIN_CONFIDENCE)
    except (ValueError, TypeError):
        min_confidence = DEFAULT_MIN_CONFIDENCE
    separator = config.get(SECTION, "path_names_separator", fallback=DEFAULT_SEPARATOR)
    # Quotes let the separator carry spaces, which configparser would strip.
    if len(separator) >= 2 and separator[0] == separator[-1] == '"':
        separator = separator[1:-1]
    return {
        "max_name_length": max(1, get_int("path_names_max_name_length", DEFAULT_MAX_NAME_LENGTH)),
        "max_bytes": max(0, get_int("path_names_max_bytes", DEFAULT_MAX_BYTES)),
        "separator": separator or DEFAULT_SEPARATOR,
        "min_confidence": max(0.0, min(1.0, min_confidence)),
    }


def confident_hop_names(
    node_ids: list[str],
    *,
    config: Any,
    db_manager: Any,
    logger: Any,
    mesh_graph: Any = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    lookup_func: Any = None,
) -> list[Optional[str]]:
    """Resolve each hop to a repeater name, or ``None`` where the match is not confident.

    Uses the same candidate query and selection as the web viewer's path decode
    (:func:`modules.path_inference.decode_path_nodes`), but keeps the
    confidence that decode discards, since a collision "resolved" at 0.5 is
    still a guess.
    """
    # Deferred: path_inference imports modules.utils, which imports this module's callers.
    from .path_inference import PathInferenceConfig, candidate_repeaters, select_node_repeater

    cfg = PathInferenceConfig.from_config(config)
    names: list[Optional[str]] = []
    for index, node_id in enumerate(node_ids):
        candidates = candidate_repeaters(node_id, cfg, db_manager=db_manager, lookup_func=lookup_func)
        selection = select_node_repeater(
            node_id, candidates, node_ids, cfg,
            mesh_graph=mesh_graph, db_manager=db_manager, logger=logger,
            graph_n=len(node_id), node_index=index,
        )
        confident = selection.status == "single" or (
            selection.status == "resolved" and selection.confidence >= min_confidence
        )
        name = (selection.repeater or {}).get("name") if confident else None
        if logger is not None:
            logger.debug(
                f"path_names: {node_id} -> {selection.status} "
                f"({selection.confidence:.2f}, {selection.matches} candidates): {name or 'hex'}"
            )
        names.append(name or None)
    return names


def format_path_names(bot: Any, message: Any, *, lookup_func: Any = None) -> str:
    """Render ``{path_names}`` for *message*.

    Returns ``Direct`` for a zero-hop message, ``""`` when the route is
    unknown, and otherwise the hops joined by the configured separator. Any
    failure in the lookup falls back to the hex prefixes rather than dropping
    the route.
    """
    if message is None:
        return ""
    node_ids = extract_path_node_ids_from_message(message)
    if not node_ids:
        return "Direct" if message_hop_count(message) == 0 else ""

    config = bot.config
    settings = _settings(config)
    logger = getattr(bot, "logger", None)
    fallbacks = [node_id.lower() for node_id in node_ids]
    names: list[Optional[str]] = [None] * len(node_ids)

    routing_info = getattr(message, "routing_info", None)
    if bytes_per_hop_from_routing_and_nodes(routing_info, node_ids) >= 2:
        try:
            names = confident_hop_names(
                node_ids,
                config=config,
                db_manager=bot.db_manager,
                logger=logger,
                mesh_graph=getattr(bot, "mesh_graph", None),
                min_confidence=settings["min_confidence"],
                lookup_func=lookup_func,
            )
        except Exception as e:
            if logger is not None:
                logger.debug(f"path_names: lookup failed, showing hex prefixes: {e}")

    return pack_names(
        list(zip(names, fallbacks, strict=True)),
        separator=settings["separator"],
        max_name_length=settings["max_name_length"],
        max_bytes=settings["max_bytes"],
    )
