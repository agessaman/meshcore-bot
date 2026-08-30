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

import asyncio
from typing import Any, Callable
from urllib.parse import quote

from .url_shortener import shorten_url
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
    """Swap *value* for its shortened form, resolved ahead of time.

    Rendering is synchronous and runs on the event loop, so this filter never
    performs the HTTP request itself: a 5 s shortener timeout here would stall the
    radio transport along with everything else. :func:`resolve_template_async`
    does the network work off-thread first and leaves the answers in ``ctx``.

    On a miss the clause is dropped rather than falling back to the long URL. A
    v.gd link is ~19 bytes against a 158-160 byte message budget where a real
    analyzer URL is ~59, and ``_send_path_response`` subtracts the prefix from the
    first segment's budget — so falling back would quietly turn one transmission
    into two every time the shortener was unreachable.
    """
    _warn_ignored_filter_arg_once(
        ctx.get('logger'), str(ctx.get('template') or ''), 'shorten_url', args
    )
    if not value:
        return ''
    resolved = ctx.get('shortened')

    # Collection pass: record what needs shortening, change nothing.
    if isinstance(resolved, set):
        resolved.add(value)
        return value

    if isinstance(resolved, dict):
        short = resolved.get(value)
        if short:
            return short
        # A resolved mapping that lacks this URL means the pre-pass ran and the
        # shortener could not answer. Debug, not warning: that is a transient
        # network condition on the message path, not a misconfiguration.
        logger = ctx.get('logger')
        if logger is not None:
            logger.debug("No shortened form for %r; dropping the clause", value)
        return ''

    _warn_unresolved_once(
        ctx.get('logger'),
        str(ctx.get('template') or ''),
        'the caller did not run resolve_template_async()',
    )
    return ''


def _filter_if_nonempty(value: str, ctx: dict[str, Any], args: str) -> str:
    """Return *args* literal only when *value* is non-empty after prior filters."""
    if not value:
        return ''
    return args


def _filter_urlencode(value: str, ctx: dict[str, Any], args: str) -> str:
    """Percent-encode *value* for safe interpolation into a URL.

    A quoted literal substitutes nested field values verbatim, which is right for
    prose but wrong the moment the literal is a URL: ``sender`` is whatever name a
    remote node advertises, so an unencoded ``&``, ``#``, ``?`` or space silently
    rewrites the link's query, truncates it at a fragment, or malforms it outright.
    Encodes ``/`` too, since an interpolated field is a single path segment.
    """
    if not value:
        return ''
    return quote(value, safe='')


RESPONSE_TEMPLATE_FILTERS: dict[str, FilterFn] = {
    'pathbytes_min': _filter_pathbytes_min,
    'pathbytes': _filter_pathbytes_min,
    'hops_min': _filter_hops_min,
    'prefix_if_nonempty': _filter_prefix_if_nonempty,
    'if_nonempty': _filter_if_nonempty,
    'urlencode': _filter_urlencode,
    'shorten_url': _filter_shorten_url,
}

# prefix_if_nonempty's literal argument may itself contain '|', so once the parser
# sees this filter name it stops splitting on '|' and takes everything up to the
# placeholder's closing '}' as one literal argument. It must therefore be last in
# a chain whenever its literal needs a pipe.
_GREEDY_ARG_FILTERS = frozenset({'prefix_if_nonempty'})

# Templates already warned about, so a misconfiguration is reported once rather than
# once per inbound message. Bounded by the number of templates in config.
_UNRESOLVED_WARNED: set[str] = set()
_IGNORED_FILTER_ARGS_WARNED: set[tuple[str, str]] = set()


def _warn_unresolved_once(logger: Any, template: str, reason: str) -> None:
    """Warn that ``shorten_url`` cannot resolve here, at most once per template.

    This filter runs on the inbound message path, so an unconditional warning is one
    log line per message forever on a device writing to a rotating 5 MB file.
    """
    if logger is None or template in _UNRESOLVED_WARNED:
        return
    _UNRESOLVED_WARNED.add(template)
    logger.warning(
        "shorten_url in template %r cannot resolve (%s); dropping the clause rather "
        "than blocking the event loop", template, reason,
    )


def _warn_ignored_filter_arg_once(
    logger: Any, template: str, filter_name: str, args: str
) -> None:
    """Warn once when an argumentless filter is given an ignored argument."""
    if logger is None or not args.strip():
        return
    key = (template, filter_name)
    if key in _IGNORED_FILTER_ARGS_WARNED:
        return
    _IGNORED_FILTER_ARGS_WARNED.add(key)
    logger.warning(
        "Response template filter %r does not accept an argument; ignoring %r in %r",
        filter_name,
        args,
        template,
    )


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
    shortened: dict[str, str] | None = None,
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
        config: Bot config, for filters that read ``[External_Data]``.
        shortened: Long-URL to short-URL mapping from :func:`resolve_template_async`.
            Required by the ``shorten_url`` filter, which will not make a network
            call from this synchronous path.
        prefix_hex_chars: Bot prefix width for inferring bytes per hop from legacy path text.

    Returns:
        Fully expanded string.
    """
    ctx: dict[str, Any] = {
        'message': message,
        'logger': logger,
        'prefix_hex_chars': prefix_hex_chars,
        'config': config,
        'shortened': shortened,
        'template': template,
    }
    if logger is not None:
        logger.debug("Rendering response template %r with fields %r", template, fields)
    return _TemplateParser(template, fields, ctx, logger).render()


def template_needs_resolution(template: str) -> bool:
    """True if *template* uses a filter that needs :func:`resolve_template_async`.

    A cheap substring test so the common template pays nothing for a feature it
    does not use; the collection pass below is what actually decides.
    """
    return 'shorten_url' in template


async def resolve_template_async(
    template: str,
    fields: dict[str, Any],
    *,
    message: Any = None,
    logger: Any = None,
    config: Any = None,
    prefix_hex_chars: int = 2,
) -> dict[str, str]:
    """Resolve *template*'s network-backed filters off the event loop.

    Renders the template once with ``shorten_url`` in collection mode, which walks
    the real filter chain — so gating filters such as ``hops_min`` have already had
    their say and a suppressed clause costs no request — then shortens whatever
    survived, concurrently and in a worker thread. Pass the result to
    :func:`format_piped_template` as ``shortened``.

    Returns an empty mapping when there is nothing to do, which renders exactly as
    an unresolved template would.
    """
    if not template_needs_resolution(template):
        return {}
    if config is None:
        _warn_unresolved_once(logger, template, 'no config was supplied to resolve it')
        return {}

    pending: set[str] = set()
    ctx: dict[str, Any] = {
        'message': message,
        'logger': logger,
        'prefix_hex_chars': prefix_hex_chars,
        'config': config,
        'shortened': pending,
        'template': template,
    }
    # The collection pass exists only to discover network-backed values; its output
    # is discarded and the real render below is responsible for syntax diagnostics.
    # Keeping the parser logger silent prevents every unknown-filter warning from
    # appearing twice for one response.
    _TemplateParser(template, fields, ctx, None).render()
    if not pending:
        return {}

    urls = sorted(pending)
    results = await asyncio.gather(
        *(shorten_url(u, config=config, logger=logger) for u in urls),
        return_exceptions=True,
    )
    resolved: dict[str, str] = {}
    for url, short in zip(urls, results, strict=True):
        # Cancellation is not a shortening failure. `return_exceptions=True` captures
        # it like any other, so swallowing it here would let a cancelled render carry
        # on and transmit during shutdown.
        if isinstance(short, asyncio.CancelledError):
            raise short
        if isinstance(short, BaseException):
            if logger is not None:
                logger.debug("Shortening %r failed: %s", url, short)
            continue
        if short:
            resolved[url] = short
    return resolved


async def format_piped_template_async(
    template: str,
    fields: dict[str, Any],
    *,
    message: Any = None,
    logger: Any = None,
    config: Any = None,
    prefix_hex_chars: int = 2,
) -> str:
    """Resolve network-backed filters off-thread, then render *template*.

    This is the safe entry point for async command paths. It keeps the template,
    fields, message and prefix width identical across collection and rendering so a
    caller cannot accidentally omit the resolved mapping or resolve different data.
    """
    shortened = await resolve_template_async(
        template,
        fields,
        message=message,
        logger=logger,
        config=config,
        prefix_hex_chars=prefix_hex_chars,
    )
    return format_piped_template(
        template,
        fields,
        message=message,
        logger=logger,
        config=config,
        shortened=shortened,
        prefix_hex_chars=prefix_hex_chars,
    )
