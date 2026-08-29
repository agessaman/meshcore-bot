#!/usr/bin/env python3
"""Piped placeholders for command response templates (feed-style ``{field|filter:args}``).

Used by :class:`~modules.commands.test_command.TestCommand` and extensible for other
commands. A placeholder holds either a bare field name (``{sender}``) or a
double-quoted string literal (``{"Hello {sender}!"}``); a quoted literal may embed
further ``{...}`` placeholders, which are expanded first and substituted into the
literal before any filters run. Either form may be followed by a ``|filter:arg``
chain, evaluated left to right.
"""

from __future__ import annotations

from typing import Any, Callable

from .url_shortener import shorten_url_sync
from .utils import message_hop_count, message_path_bytes_per_hop

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


def _filter_hops_min(value: str, ctx: dict[str, Any], args: str) -> str:
    """Clear *value* unless the message travelled at least *N* hops.

    Asks about the route rather than how it is encoded, which is what separates
    this from ``pathbytes_min``: a one-byte multi-hop path has a real, measurable
    distance, and ``pathbytes_min:2`` would throw it away along with the direct
    messages it was aimed at. ``hops_min:1`` is the way to drop a clause on a
    direct message and nothing else.

    An unknown hop count clears the value: a gate that cannot confirm the route
    should suppress rather than guess, matching ``pathbytes_min``.
    """
    message = ctx.get('message')
    if message is None:
        return ''
    try:
        n = int(args.strip())
    except ValueError:
        return value
    if n < 0:
        return value
    hops = message_hop_count(message)
    if hops is None or hops < n:
        return ''
    return value


def _filter_prefix_if_nonempty(value: str, ctx: dict[str, Any], args: str) -> str:
    """Prepend *args* literal to *value* only when *value* is non-empty after prior filters."""
    if not value:
        return ''
    return args + value

def _filter_shorten_url(value: str, ctx: dict[str, Any], args: str) -> str:
    """Shorten *value* URL using configured URL shortener (v.gd / is.gd compatible)."""
    logger = ctx.get('logger')
    config = ctx.get('config')
    if logger is not None:
        logger.debug("Shortening URL %r", value)
    if config is None or value == '':
        if logger is not None:
            logger.debug("Abandoning shorten url due to empty value or config")
        return value
    return shorten_url_sync(value, config=config, logger=logger) or value

def _filter_if_notempty(value: str, ctx: dict[str, Any], args: str) -> str:
    """Return *args* literal only when *value* is non-empty after prior filters."""
    if not value:
        return ''
    return args

RESPONSE_TEMPLATE_FILTERS: dict[str, FilterFn] = {
    'pathbytes_min': _filter_pathbytes_min,
    'pathbytes': _filter_pathbytes_min,
    'hops_min': _filter_hops_min,
    'prefix_if_nonempty': _filter_prefix_if_nonempty,
    'if_notempty': _filter_if_notempty,
    'shorten_url': _filter_shorten_url,
}

# prefix_if_nonempty's literal argument may itself contain '|', so once the parser
# sees this filter name it stops splitting on '|' and takes everything up to the
# placeholder's closing '}' as one literal argument. It must therefore be last in
# a chain whenever its literal needs a pipe.
_GREEDY_ARG_FILTERS = frozenset({'prefix_if_nonempty'})


class _TemplateParser:
    """Finite-state parser for ``{field|filter:arg|...}``-style placeholders.

    Walks the template left to right, character by character, alternating between
    plain text and placeholder spans. A placeholder's base value is either a bare
    field name or a ``"..."`` string literal; a literal may contain nested
    ``{...}`` placeholders (parsed recursively, same grammar) which are expanded
    before the literal is used as the base value for any following filters.
    """

    def __init__(self, template: str, fields: dict[str, Any], ctx: dict[str, Any], logger: Any):
        self.s = template
        self.n = len(template)
        self.fields = fields
        self.ctx = ctx
        self.logger = logger

    def render(self) -> str:
        out: list[str] = []
        i = 0
        while i < self.n:
            j = self.s.find('{', i)
            if j == -1:
                out.append(self.s[i:])
                break
            out.append(self.s[i:j])
            value, end = self._parse_placeholder(j)
            if value is None:
                out.append('{')
                i = j + 1
            else:
                out.append(value)
                i = end
        return ''.join(out)

    def _skip_ws(self, i: int) -> int:
        while i < self.n and self.s[i].isspace():
            i += 1
        return i

    def _parse_placeholder(self, start: int) -> tuple[str | None, int]:
        """Parse the placeholder beginning at ``self.s[start] == '{'``.

        Returns ``(expanded_value, index_after_closing_brace)``, or ``(None, start)``
        if there is no well-formed placeholder here (left as literal text).
        """
        i = start + 1
        if i < self.n and self.s[i] == '}':
            return None, start  # `{}` has no content
        i = self._skip_ws(i)
        if i >= self.n:
            return None, start
        if self.s[i] == '"':
            value, i = self._parse_quoted_string(i)
        else:
            value, i = self._parse_field_name(i)
        if value is None:
            return None, start

        i = self._skip_ws(i)
        filter_specs: list[tuple[str, str]] = []
        while i < self.n and self.s[i] == '|':
            i += 1
            name, args, i = self._parse_filter_spec(i)
            if name is None:
                return None, start
            filter_specs.append((name, args))
            i = self._skip_ws(i)

        if i >= self.n or self.s[i] != '}':
            return None, start
        raw_inner = self.s[start + 1:i]
        for name, args in filter_specs:
            value = self._apply_filter(name, args, value, raw_inner)
        return value, i + 1

    def _parse_field_name(self, i: int) -> tuple[str | None, int]:
        start = i
        while i < self.n and self.s[i] not in '|}':
            i += 1
        if i >= self.n:
            return None, i
        name = self.s[start:i].strip()
        return str(self.fields.get(name, '')), i

    def _parse_quoted_string(self, i: int) -> tuple[str | None, int]:
        """Parse a ``"..."`` literal starting at the opening quote.

        ``\\"`` and ``\\\\`` are recognized escapes; any ``{...}`` inside the
        literal is expanded recursively and substituted in place.
        """
        i += 1
        parts: list[str] = []
        while i < self.n:
            ch = self.s[i]
            if ch == '\\' and i + 1 < self.n and self.s[i + 1] in ('"', '\\'):
                parts.append(self.s[i + 1])
                i += 2
                continue
            if ch == '"':
                return ''.join(parts), i + 1
            if ch == '{':
                value, i = self._parse_placeholder(i)
                if value is None:
                    return None, i
                parts.append(value)
                continue
            parts.append(ch)
            i += 1
        return None, i  # unterminated string literal

    def _parse_filter_spec(self, i: int) -> tuple[str | None, str, int]:
        start = i
        while i < self.n and self.s[i] not in ':|}':
            i += 1
        if i >= self.n:
            return None, '', i
        name = self.s[start:i].strip()
        if i >= self.n or self.s[i] != ':':
            return name, '', i
        i += 1  # consume ':'
        if name in _GREEDY_ARG_FILTERS:
            close = self.s.find('}', i)
            if close == -1:
                return None, '', self.n
            return name, self.s[i:close], close
        quoted_at = self._skip_ws(i)
        if quoted_at < self.n and self.s[quoted_at] == '"':
            value, j = self._parse_quoted_string(quoted_at)
            if value is None:
                return None, '', j
            return name, value, j
        arg_start = i
        while i < self.n and self.s[i] not in '|}':
            i += 1
        return name, self.s[arg_start:i], i

    def _apply_filter(self, name: str, args: str, value: str, raw_inner: str) -> str:
        fn = RESPONSE_TEMPLATE_FILTERS.get(name)
        if fn is None:
            if self.logger is not None:
                self.logger.warning(f"Unknown response template filter {name!r} in {{{raw_inner}}}")
            return value
        return fn(value, self.ctx, args)


def format_piped_template(
    template: str,
    fields: dict[str, Any],
    *,
    message: Any = None,
    logger: Any = None,
    config: Any = None,
    prefix_hex_chars: int = 2,
) -> str:
    """Replace ``{field}``, ``{"literal {field}"}``, and their piped filter chains.

    Args:
        template: Raw template string from config.
        fields: Mapping of placeholder names to values (e.g. ``sender``, ``path_distance``).
            An unavailable field is an empty string, which renders as nothing and
            lets ``prefix_if_nonempty`` drop its literal label too.
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
        'config': config,
    }
    if (logger is not None) and (config is not None):
        logger.debug("Rendering response template %r with fields %r", template, fields)
    return _TemplateParser(template, fields, ctx, logger).render()
