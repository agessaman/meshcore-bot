#!/usr/bin/env python3
"""Piped placeholders for command response templates (feed-style ``{field|filter:args}``).

Used by :class:`~modules.commands.test_command.TestCommand` and extensible for other
commands. Same brace limitation as feed formatting: no nested ``{}`` inside a placeholder.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from .utils import message_path_bytes_per_hop

FilterFn = Callable[[str, dict[str, Any], str], str]


def _filter_pathbytes_min(value: str, ctx: dict[str, Any], args: str) -> str:
    """Clear *value* unless message path uses at least *N* bytes per hop (N in 1..3)."""
    message = ctx.get('message')
    if message is None:
        return ''
    try:
        n = int(args.strip())
    except ValueError:
        return value
    if n < 1 or n > 3:
        return value
    prefix_hex = int(ctx.get('prefix_hex_chars') or 2)
    bph = message_path_bytes_per_hop(message, prefix_hex_chars=prefix_hex)
    if bph < n:
        return ''
    return value


def _filter_prefix_if_nonempty(value: str, ctx: dict[str, Any], args: str) -> str:
    """Prepend *args* literal to *value* only when *value* is non-empty after prior filters."""
    if not value:
        return ''
    return args + value


RESPONSE_TEMPLATE_FILTERS: dict[str, FilterFn] = {
    'pathbytes_min': _filter_pathbytes_min,
    'pathbytes': _filter_pathbytes_min,
    'prefix_if_nonempty': _filter_prefix_if_nonempty,
}


def _field_and_filter_specs(inner: str) -> tuple[str, list[tuple[str, str]]]:
    """Split ``inner`` into field name and ``(filter_name, args)`` pairs.

    Pipe ``|`` separates filters. ``prefix_if_nonempty`` is special: its argument may
    contain ``|`` (e.g. `` | Path Dist: ``), so once that filter is reached we merge
    all remaining segments and treat the rest as its args. ``prefix_if_nonempty`` must
    be last in the chain if the literal includes a pipe.
    """
    raw_parts = inner.split('|')
    field_name = raw_parts[0].strip()
    if len(raw_parts) < 2:
        return field_name, []
    specs: list[tuple[str, str]] = []
    i = 1
    while i < len(raw_parts):
        if raw_parts[i].lstrip().startswith('prefix_if_nonempty'):
            merged = '|'.join(raw_parts[i:])
            if re.match(r'^\s*prefix_if_nonempty\s*$', merged):
                specs.append(('prefix_if_nonempty', ''))
                break
            m = re.match(r'^\s*prefix_if_nonempty\s*:(.*)$', merged, flags=re.DOTALL)
            if m:
                specs.append(('prefix_if_nonempty', m.group(1)))
            else:
                specs.append(('prefix_if_nonempty', ''))
            break
        segment = raw_parts[i].strip()
        name, sep, arg = segment.partition(':')
        name = name.strip()
        arg = arg if sep else ''
        specs.append((name, arg))
        i += 1
    return field_name, specs


def format_piped_template(
    template: str,
    fields: dict[str, str],
    *,
    message: Any = None,
    logger: Any = None,
    prefix_hex_chars: int = 2,
) -> str:
    """Replace ``{field}`` and ``{field|filter:arg|...}`` using *fields* and optional *message*.

    Args:
        template: Raw template string from config.
        fields: Mapping of placeholder names to string values (e.g. ``sender``, ``path_distance``).
        message: Triggering mesh message; required for ``pathbytes`` / ``pathbytes_min`` filters.
        logger: Optional logger for unknown filter warnings.
        prefix_hex_chars: Bot prefix width for inferring bytes per hop from legacy path text.

    Returns:
        Fully expanded string.
    """
    ctx: dict[str, Any] = {
        'message': message,
        'logger': logger,
        'prefix_hex_chars': prefix_hex_chars,
    }

    def replace_placeholder(match: re.Match[str]) -> str:
        inner_raw = match.group(1)
        if '|' not in inner_raw:
            return str(fields.get(inner_raw.strip(), ''))
        field_name, filter_specs = _field_and_filter_specs(inner_raw)
        value = str(fields.get(field_name, ''))
        for name, arg in filter_specs:
            fn = RESPONSE_TEMPLATE_FILTERS.get(name)
            if fn is None:
                if logger is not None:
                    logger.warning(f"Unknown response template filter {name!r} in {{{inner_raw}}}")
                continue
            value = fn(value, ctx, arg)
        return value

    return re.sub(r"\{([^}]+)\}", replace_placeholder, template)
