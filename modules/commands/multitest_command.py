#!/usr/bin/env python3
"""
Multitest command for the MeshCore Bot
Listens for a period of time and collects all unique paths from incoming messages
"""

import asyncio
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, Optional

CondensePathsMode = Literal["off", "flat", "nested"]

from ..clients.corescope_client import (
    DEFAULT_CACHE_SECONDS,
    DEFAULT_TIMEOUT,
    CoreScopeClient,
    ScopePacket,
    normalize_base_url,
)
from ..models import MeshMessage
from ..response_template import format_piped_template_async
from ..template_reference import MESSAGE_PLACEHOLDERS, template_spec
from ..utils import calculate_packet_hash, decode_escape_sequences, parse_path_string
from .base_command import BaseCommand

# Seconds to wait for observers to report to CoreScope before asking it. Long
# enough that the count is not a snapshot of whoever reported first.
DEFAULT_SETTLE_SECONDS = 30.0

_BRANCH_INTER = "\u251c"  # ├ (intermediate branch)
_BRANCH_LAST = "\u2514"  # └ (last branch)
_INDENT_NEST = "\u3000"  # 　 ideographic space before nested ├/└
_BRANCH_CORNER = "\u2510"  # ┐ (marks end of common path before branches)
# Nested layout “continuation” column: ASCII space only (1 byte) vs U+2502 │ (3 bytes) to fit mesh limits.
_NEST_PREFIX = "  "


def _line_has_branch_prefix(line: str) -> bool:
    """True if ``line`` starts with a tee (├/└), not a continuation indent."""
    return bool(line) and line[0] in (_BRANCH_INTER, _BRANCH_LAST)


def _nested_suffix_lines_disjoint(suffixes: list[list[str]]) -> list[str]:
    """Format suffixes when non-empty paths share no LCP (split by first hop, recurse like nested cluster)."""
    nonempty = [s for s in suffixes if s]
    if not nonempty:
        return []
    by_first: dict[str, list[list[str]]] = defaultdict(list)
    for t in nonempty:
        by_first[t[0]].append(t[1:])
    keys = sorted(by_first.keys())
    groups_out: list[list[str]] = []
    for ft in keys:
        rests = by_first[ft]
        sub = [[ft, *r] for r in rests]
        groups_out.append(_format_path_cluster_nested(sub, use_brackets=False))
    out: list[str] = []
    ng = len(groups_out)
    for gi, sub_lines in enumerate(groups_out):
        last_group = gi == ng - 1
        n_sub = len(sub_lines)
        for j, sl in enumerate(sub_lines):
            if j == 0:
                if _line_has_branch_prefix(sl):
                    out.append(sl)
                else:
                    br = _BRANCH_LAST if last_group and n_sub == 1 else _BRANCH_INTER
                    out.append(f"{br} {sl}")
            else:
                if sl.startswith(_NEST_PREFIX) or sl.startswith(_INDENT_NEST):
                    out.append(sl)
                else:
                    out.append(f"{_NEST_PREFIX}{sl}")
    return out


def _tree_branch_lines_flat(suffixes: list[str]) -> list[str]:
    """Format branch rows: ├ for all but the last, └ for the last; space after the tee only."""
    if not suffixes:
        return []
    n = len(suffixes)
    out: list[str] = []
    for i, text in enumerate(suffixes):
        prefix = _BRANCH_LAST if i == n - 1 else _BRANCH_INTER
        out.append(f"{prefix} {text}")
    return out


def _apply_tee_prefixes_flat(specs: list[tuple[str, str]]) -> list[str]:
    """Prefix tree rows with ├/└; nested rows use └ when the next row starts a new top branch (head/end)."""
    n = len(specs)
    out: list[str] = []
    for i, (kind, text) in enumerate(specs):
        last = i == n - 1
        if kind in ("head", "end"):
            p = _BRANCH_LAST if last else _BRANCH_INTER
            out.append(f"{p} {text}")
        else:
            next_kind = specs[i + 1][0] if i + 1 < n else None
            nest_continues = next_kind == "nest"
            p = _BRANCH_INTER if nest_continues else _BRANCH_LAST
            out.append(f"{_INDENT_NEST}{p} {text}")
    return out


def _flat_suffix_specs(acc: list[str], tails: list[list[str]]) -> list[tuple[str, str]]:
    """Build (kind, text) rows for flat condensed layout: full ``cd,7e,01``-style lines where possible."""
    ends_at_acc = [t for t in tails if len(t) == 0]
    continuing = [t for t in tails if len(t) > 0]

    # Exactly one path and it ends here (tail was [[]] from split).
    if len(tails) == 1 and len(tails[0]) == 0:
        return [("head", ",".join(acc))]

    # One route ends at acc, one continues (e.g. [7a] vs [7a,09] → head + nest 09).
    if len(ends_at_acc) >= 1 and len(continuing) == 1:
        rest = continuing[0]
        out: list[tuple[str, str]] = [("head", ",".join(acc))]
        if len(rest) == 1:
            out.append(("nest", rest[0]))
        else:
            out.extend(_flat_suffix_specs(acc, [rest]))
        return out

    specs: list[tuple[str, str]] = []

    if continuing:
        if len(continuing) == 1 and not ends_at_acc:
            return [("head", ",".join(acc + continuing[0]))]

        raw_lcp = _longest_common_prefix(continuing)
        lcp = _shrink_display_lcp(continuing, raw_lcp)
        if len(lcp) == 0:
            by_first: dict[str, list[list[str]]] = defaultdict(list)
            for t in continuing:
                by_first[t[0]].append(t[1:])
            for ft in sorted(by_first.keys()):
                specs.extend(_flat_suffix_specs(acc + [ft], by_first[ft]))
            if ends_at_acc:
                specs.append(("end", ",".join(acc)))
            return specs

        rems = [t[len(lcp) :] for t in continuing]
        has_exact = any(len(r) == 0 for r in rems)
        active = [r for r in rems if r]
        pre = acc + lcp

        if has_exact and len(active) == 1:
            specs.append(("head", ",".join(pre)))
            specs.extend(_flat_suffix_specs(pre, active))
            if ends_at_acc:
                specs.append(("end", ",".join(acc)))
            return specs

        if active:
            specs.extend(_flat_suffix_specs(pre, active))
        if has_exact:
            specs.append(("end", ",".join(pre)))
    if ends_at_acc:
        specs.append(("end", ",".join(acc)))
    return specs


def _format_suffix_branch_lines_flat(suffix_tokens: list[list[str]]) -> list[str]:
    """Flat multitest layout (``condense_paths = true``): one full suffix per branch row when possible."""
    non_empty = [list(t) for t in suffix_tokens if t]
    if not non_empty:
        return []
    if len(non_empty) == 1:
        return _tree_branch_lines_flat([",".join(non_empty[0])])
    specs = _flat_suffix_specs([], non_empty)
    return _apply_tee_prefixes_flat(specs)


def _path_to_tokens(path: str) -> list[str]:
    """Split a comma-separated path into non-empty hex token strings."""
    return [p.strip() for p in path.split(",") if p.strip()]


def _is_strict_prefix(a: list[str], b: list[str]) -> bool:
    return len(a) < len(b) and b[: len(a)] == a


def _longest_common_prefix(token_lists: list[list[str]]) -> list[str]:
    if not token_lists:
        return []
    if len(token_lists) == 1:
        return list(token_lists[0])
    first = token_lists[0]
    for i, tok in enumerate(first):
        for tl in token_lists[1:]:
            if i >= len(tl) or tl[i] != tok:
                return first[:i]
    return list(first[: min(len(tl) for tl in token_lists)])


def _shrink_display_lcp(maximal: list[list[str]], lcp: list[str]) -> list[str]:
    """Shorten displayed LCP when one path ends exactly at LCP and another continues past it.

    Avoids showing the shorter path as the 'trunk' with only the tail as a branch (e.g. …0101
    on the common line and └ 0970), which reads like a single endpoint plus an offshoot.
    """
    lcp = list(lcp)
    while len(lcp) > 1:
        has_exact = any(t == lcp for t in maximal)
        has_extend = any(_is_strict_prefix(lcp, t) for t in maximal)
        if has_exact and has_extend:
            lcp.pop()
        else:
            break
    return lcp


def _cluster_head(common: str, suffix_tokens: list[list[str]]) -> str:
    """Trunk line above a cluster's branch rows.

    A path that stops exactly at the display LCP has an empty suffix, and the
    branch renderers drop empty suffixes — so the trunk itself has to carry that
    route. ``├ common`` (no ``┐``) is this file's marker for "a route ends here
    and others continue"; ``common ┐`` means "everything continues past here".
    Emitting ``┐`` in the first case silently lost the shortest path while the
    header still counted it.

    ``_shrink_display_lcp`` cannot cover this: it refuses to shrink below one
    token, so a single-token LCP (the common case for ``96`` / ``96,e0``) always
    lands here.
    """
    if any(len(t) == 0 for t in suffix_tokens):
        return f"{_BRANCH_INTER} {common}"
    return f"{common} {_BRANCH_CORNER}"


def _format_path_cluster_flat(token_lists: list[list[str]], use_brackets: bool) -> list[str]:
    """Format paths sharing a first hop as one cluster, suffix rows in the flat layout (full paths per branch when possible)."""
    token_lists = [t for t in token_lists if t]
    if not token_lists:
        return []
    if len(token_lists) == 1:
        s = ",".join(token_lists[0])
        return [f"[{s}]"] if use_brackets else [s]

    raw_lcp = _longest_common_prefix(token_lists)
    lcp = _shrink_display_lcp(token_lists, raw_lcp)

    if len(lcp) > 0:
        suffix_tokens = [t[len(lcp) :] for t in token_lists]
        common = ",".join(lcp)
        branch_lines = _format_suffix_branch_lines_flat(suffix_tokens)
        if branch_lines:
            lines = [_cluster_head(common, suffix_tokens)]
            lines.extend(branch_lines)
        else:
            lines = [common]
        return lines

    groups: dict[str, list[list[str]]] = {}
    for t in token_lists:
        groups.setdefault(t[0], []).append(t)

    lines: list[str] = []
    multi = len(groups) > 1
    for ft in sorted(groups.keys()):
        sub_lines = _format_path_cluster_flat(groups[ft], use_brackets=multi)
        lines.extend(sub_lines)
    return lines


def _nested_child_lines(paths: list[list[str]], col: str) -> list[str]:
    """Render paths under a ``├ …`` / ``├ … ┐`` row using a leading continuation prefix (ASCII spaces)."""
    paths = [p for p in paths if p]
    if not paths:
        return []
    if len(paths) == 1:
        return [f"{col}{_BRANCH_LAST} {','.join(paths[0])}"]

    raw_lcp = _longest_common_prefix(paths)
    lcp = _shrink_display_lcp(paths, raw_lcp)
    if len(lcp) == 0:
        lines: list[str] = []
        by_first: dict[str, list[list[str]]] = defaultdict(list)
        for t in paths:
            by_first[t[0]].append(t[1:])
        keys = sorted(by_first.keys())
        for i, ft in enumerate(keys):
            rests = by_first[ft]
            sub = [[ft, *r] for r in rests]
            is_last_ft = i == len(keys) - 1
            if len(sub) == 1 and len(sub[0]) == 1:
                br = _BRANCH_LAST if is_last_ft else _BRANCH_INTER
                lines.append(f"{col}{br} {ft}")
                continue
            lines.extend(_nested_child_lines(sub, col))
        return lines

    inner = ",".join(lcp)
    rest = [t[len(lcp) :] for t in paths]
    has_exact = any(len(r) == 0 for r in rest)
    cont = [r for r in rest if r]

    if not cont:
        return [f"{col}{_BRANCH_INTER} {inner}"]

    # No ``┐`` on the trunk when one path ends here and others continue (direct route + deeper paths).
    head_open = (
        f"{col}{_BRANCH_INTER} {inner}"
        if has_exact
        else f"{col}{_BRANCH_INTER} {inner} {_BRANCH_CORNER}"
    )
    sub_col = f"{col}{_INDENT_NEST}"

    if has_exact and len(cont) == 1:
        only = cont[0]
        if len(only) == 1:
            return [
                f"{col}{_BRANCH_INTER} {inner}",
                f"{sub_col}{_BRANCH_LAST} {only[0]}",
            ]
        sub = _nested_child_lines(cont, sub_col)
        return [head_open, *sub]

    if len(cont) == 1 and not has_exact:
        return [head_open, f"{sub_col}{_BRANCH_LAST} {','.join(cont[0])}"]

    sub = _nested_child_lines(cont, sub_col)
    return [head_open, *sub]


def _nested_format_suffix_lines(suffixes: list[list[str]]) -> list[str]:
    """Highly nested layout (``condense_paths = nested``): two ASCII spaces + ``├``/``└``; extra ``┐`` rows."""
    if not suffixes:
        return []
    nonempty = [s for s in suffixes if s]
    if not nonempty:
        return []
    if len(suffixes) == 1:
        return _tree_branch_lines_flat([",".join(suffixes[0])])

    # LCP among non-empty suffixes only: ``[]`` means “ends at parent” and must not collapse LCP
    # (e.g. ``[[1ed6],[cc5d],[]]`` → still share ``e0ee`` in the caller, not flat ``e0ee,1ed6`` rows).
    raw_lcp = _longest_common_prefix(nonempty)
    lcp = _shrink_display_lcp(suffixes, raw_lcp)
    # When shrink removes the whole LCP, nested ``├ {main} ┐`` would have empty ``main`` → ``├  ┐``.
    if len(lcp) == 0:
        return _nested_suffix_lines_disjoint(suffixes)

    rems = [t[len(lcp) :] for t in suffixes]
    has_exact = any(len(r) == 0 for r in rems)
    cont = [r for r in rems if r]

    if not cont:
        return _tree_branch_lines_flat([",".join(lcp)])

    # Two suffixes only, one ends at ``lcp`` (empty remainder) and one continues: ``cont`` is a
    # single list — the inner2 merge would only see [[e0]] and can emit a bogus ``├ 7a,e0 ┐``.
    # Example: ``7a,e0`` vs ``7a`` → use flat sibling rows (``├ 7a`` / ``└ 7a,e0``).
    if has_exact and len(rems) == 2 and len(cont) == 1:
        return _format_suffix_branch_lines_flat(suffixes)

    # Pull one more shared segment into the open line when a shorter route ends at ``lcp`` only
    # (e.g. ``cd`` vs ``cd,7e,…`` → ``├ cd,7e ┐`` then ``  ├ 01``, not ``├ cd ┐`` then ``  ├ 7e ┐``).
    if has_exact and cont:
        inner2 = _longest_common_prefix(cont)
        inner2 = _shrink_display_lcp(cont, inner2)
        if len(inner2) > 0:
            main = ",".join(lcp + inner2)
            cont = [t[len(inner2) :] for t in cont]
            # Same trunk rule as the simple branch: if some remainders are empty, a route ends at
            # ``main`` while others continue — use ``├ main`` without ``┐`` (not ``├ main ┐``).
            inner_has_exact = any(len(t) == 0 for t in cont)
            head_open = (
                f"{_BRANCH_INTER} {main}"
                if inner_has_exact
                else f"{_BRANCH_INTER} {main} {_BRANCH_CORNER}"
            )
            col = _NEST_PREFIX
            child = _nested_child_lines(cont, col)
            lines = [head_open, *child]
            lines.append(f"{_BRANCH_LAST} {','.join(lcp)}")
            return lines

    main = ",".join(lcp)
    head_open = (
        f"{_BRANCH_INTER} {main}"
        if has_exact
        else f"{_BRANCH_INTER} {main} {_BRANCH_CORNER}"
    )
    col = _NEST_PREFIX
    child = _nested_child_lines(cont, col)
    lines = [head_open, *child]
    return lines


def _format_path_cluster_nested(token_lists: list[list[str]], use_brackets: bool) -> list[str]:
    """Highly nested tree: extra ┐ levels; continuation column uses ASCII spaces (not U+2502) to save bytes."""
    token_lists = [t for t in token_lists if t]
    if not token_lists:
        return []
    if len(token_lists) == 1:
        s = ",".join(token_lists[0])
        return [f"[{s}]"] if use_brackets else [s]

    raw_lcp = _longest_common_prefix(token_lists)
    lcp = _shrink_display_lcp(token_lists, raw_lcp)

    if len(lcp) > 0:
        suffix_tokens = [t[len(lcp) :] for t in token_lists]
        common = ",".join(lcp)
        # Hand the branch renderer only the suffixes that actually continue.
        # It strips its own LCP off every suffix, which maps an *empty* suffix
        # (a route ending at ``common``) onto the same [] as a route ending at
        # that inner LCP — making it emit a ``├ inner`` row for a route that
        # does not exist. _cluster_head already represents the ends-at-common
        # route on the trunk, so the empty suffix has no business here.
        continuing = [s for s in suffix_tokens if s]
        branch_lines = _nested_format_suffix_lines(continuing)
        if branch_lines:
            return [_cluster_head(common, suffix_tokens), *branch_lines]
        return [common]

    groups: dict[str, list[list[str]]] = defaultdict(list)
    for t in token_lists:
        groups[t[0]].append(t[1:])

    lines: list[str] = []
    multi = len(groups) > 1
    for ft in sorted(groups.keys()):
        sub = [[ft, *r] for r in groups[ft]]
        sub_lines = _format_path_cluster_nested(sub, use_brackets=multi)
        lines.extend(sub_lines)
    return lines


def _condense_path_lines(paths: list[str], mode: Literal["flat", "nested"] = "flat") -> str:
    """Condense sorted unique path strings by shared prefix and branch suffixes."""
    if len(paths) <= 1:
        return "\n".join(paths)
    token_lists = [_path_to_tokens(p) for p in paths]
    if mode == "nested":
        lines = _format_path_cluster_nested(token_lists, use_brackets=False)
    else:
        lines = _format_path_cluster_flat(token_lists, use_brackets=False)
    return "\n".join(lines)


def _parse_condense_paths_mode(raw: object) -> CondensePathsMode:
    """Parse ``Multitest_Command.condense_paths``: ``false`` / ``true`` (flat) / ``nested``."""
    s = str(raw).strip().lower()
    if s in ("false", "0", "off", "no"):
        return "off"
    if s == "nested":
        return "nested"
    if s in ("true", "1", "yes", "flat"):
        return "flat"
    return "flat"


@dataclass
class MultitestSession:
    """Represents an active multitest listening session"""
    user_id: str
    target_packet_hash: str
    triggering_timestamp: float
    listening_start_time: float
    listening_duration: float
    collected_paths: set[str]
    initial_path: Optional[str] = None
    required_path_bytes_mode: int = 0


class MultitestCommand(BaseCommand):
    """Handles the multitest command - listens for multiple path variations"""

    # Plugin metadata
    name = "multitest"
    honors_skip_channel_check = False
    enabled_attr = "multitest_enabled"
    keywords = ['multitest', 'mt']
    description = "Listens for 6 seconds and collects all unique paths from incoming messages"
    category = "meshcore_info"

    # Documentation
    short_description = "Listens for 6 seconds and collects all unique paths your incoming messages took to reach the bot"
    usage = "multitest"
    examples = ["multitest", "mt"]

    # Reply used when response_format is blank. Rendered through the same piped
    # engine as a configured template, so the two cannot drift apart.
    DEFAULT_FORMAT = "Paths({path_count}):\n{paths}"

    # Sent as soon as a CoreScope request is accepted, because the settle delay
    # leaves the user waiting half a minute with nothing. Plain ASCII and short:
    # it is a second transmission on a shared mesh and has to fit the budget on
    # its own.
    DEFAULT_ACK_MESSAGE = "Checking CoreScope for all paths, reply in ~{wait_seconds}s"

    # Longest settle an operator can configure. A typo of 3000 would otherwise
    # hold the command path for 50 minutes.
    MAX_SETTLE_SECONDS = 300.0

    # "long"/"xlong" lengthen the local listening window; in CoreScope mode there
    # is no window, so they lengthen the wait for observers to report instead.
    SETTLE_MULTIPLIERS = {"": 1, "long": 2, "xlong": 3}

    # Web-viewer settings schema (see modules/settings_schema.py)
    settings_schema = [
        {"key": "response_format", "label": "Response format", "type": "str",
         "default": "",
         "help": "Result template. Empty = default.",
         "template": template_spec(
             "piped",
             MESSAGE_PLACEHOLDERS + ("path_count", "paths", "listening_duration"),
             notes=[
                 "The message placeholders describe the triggering message, the one "
                 "whose packet the bot listened for.",
                 "\\n starts a new line.",
             ],
             blank_default=DEFAULT_FORMAT,
             preview="multitest",
         )},
        {"key": "condense_paths", "label": "Path layout", "type": "enum",
         "options": [
             {"value": "true", "label": "Condensed tree (default)"},
             {"value": "false", "label": "One full path per line"},
             {"value": "nested", "label": "Nested/indented tree"},
         ],
         "default": "true",
         "help": "How collected paths are displayed."},
        {"key": "require_path_bytes_greater_or_equal_to", "label": "Require path bytes ≥", "type": "int",
         "min": 0, "max": 3, "default": 0,
         "help": "Only respond when the path's total size is at least 2 bytes (2) or exactly 3 bytes (3), counting every hop. 0/1 = allow all."},
        {"key": "require_path_bytes_failure_response", "label": "Path-byte reject reply", "type": "str",
         "default": "",
         "help": "Reply when rejected by the path-byte requirement. Empty = silent reject."},
    ]

    def __init__(self, bot):
        super().__init__(bot)
        self.multitest_enabled = self.get_config_value('Multitest_Command', 'enabled', fallback=True, value_type='bool')
        # Track active sessions per user to prevent race conditions
        # Key: user_id, Value: MultitestSession
        self._active_sessions: dict[str, MultitestSession] = {}
        # Lock to prevent concurrent execution from interfering (lazily initialized)
        self._execution_lock: Optional[asyncio.Lock] = None
        self._load_config()

    def _get_execution_lock(self) -> asyncio.Lock:
        """Get or create the execution lock (lazy initialization)"""
        if self._execution_lock is None:
            self._execution_lock = asyncio.Lock()
        return self._execution_lock

    def _load_config(self):
        """Load configuration for multitest command"""
        response_format = self.get_config_value('Multitest_Command', 'response_format', fallback='')
        if response_format and response_format.strip():
            # Strip quotes if present (config parser may add them)
            response_format = self._strip_quotes_from_config(response_format).strip()
            # Decode escape sequences (e.g. \n -> newline) the way every other
            # response template does. The old encode('latin-1') route also read
            # \uXXXX, but it raised on any non-latin-1 character, so one emoji in
            # the template left every \n in it literal.
            self.response_format = decode_escape_sequences(response_format)
        else:
            self.response_format = None  # Use default format

        raw_cp = self.get_config_value(
            'Multitest_Command', 'condense_paths', fallback='true'
        )
        self.condense_paths_mode: CondensePathsMode = _parse_condense_paths_mode(raw_cp)

        self._corescope = self._build_corescope_client()
        self.corescope_settle_seconds = self._load_settle_seconds()

        ack = self.get_config_value(
            'Multitest_Command', 'corescope_ack_message', fallback=None
        )
        if ack is None:
            self.corescope_ack_message: str = self.DEFAULT_ACK_MESSAGE
        else:
            # An explicitly blank value disables the acknowledgement; anything else
            # is a template, decoded like every other configured response.
            stripped = self._strip_quotes_from_config(ack).strip()
            self.corescope_ack_message = decode_escape_sequences(stripped) if stripped else ""

    def _load_settle_seconds(self) -> float:
        """Seconds to wait before asking CoreScope, clamped to a sane range.

        Observers publish to CoreScope over MQTT as they hear a packet, so asking
        the moment the trigger is decoded returns a floor rather than a total.
        Waiting is the whole point of the delay; an out-of-range value is rejected
        rather than honoured, since the command path is occupied throughout.
        """
        # Read as text and parse here: get_config_value's 'float' coercion falls
        # back silently, so a typo would quietly become the default with nothing
        # in the log to explain why the bot is not waiting as long as configured.
        raw = self.get_config_value(
            'Multitest_Command', 'corescope_settle_seconds', fallback=None
        )
        if raw is None or not str(raw).strip():
            return DEFAULT_SETTLE_SECONDS
        try:
            value = float(str(raw).strip())
        except ValueError:
            self.logger.warning(
                f"Multitest: corescope_settle_seconds {raw!r} is not a number; "
                f"using {DEFAULT_SETTLE_SECONDS:g}s"
            )
            return DEFAULT_SETTLE_SECONDS

        clamped = max(0.0, min(value, self.MAX_SETTLE_SECONDS))
        if clamped != value:
            self.logger.warning(
                f"Multitest: corescope_settle_seconds {value:g} is out of range "
                f"(0-{self.MAX_SETTLE_SECONDS:g}); using {clamped:g}s"
            )
        return clamped

    def _settle_for_variant(self, variant: str) -> float:
        """The settle this run waits: the configured value, scaled by the variant."""
        multiplier = self.SETTLE_MULTIPLIERS.get(variant, 1)
        return min(self.corescope_settle_seconds * multiplier, self.MAX_SETTLE_SECONDS)

    def _build_corescope_client(self) -> Optional[CoreScopeClient]:
        """The CoreScope client, or ``None`` to keep listening locally.

        Rebuilt on every config load so a changed URL or toggle takes effect
        without a restart. Enabled with no usable URL degrades to the local
        listening window rather than breaking the command: there is no default
        instance to guess at, since this sends a packet hash the bot heard to a
        third-party service the operator has to name.
        """
        if not self.get_config_value(
            'Multitest_Command', 'corescope_enabled', fallback=False, value_type='bool'
        ):
            return None

        base_url = normalize_base_url(
            self.get_config_value('External_Data', 'corescope_url', fallback='') or ''
        )
        if not base_url:
            self.logger.warning(
                "Multitest: corescope_enabled is on but [External_Data] corescope_url is "
                "missing or unusable; listening locally instead"
            )
            return None

        return CoreScopeClient(
            base_url,
            timeout=self.get_config_value(
                'Multitest_Command', 'corescope_timeout',
                fallback=DEFAULT_TIMEOUT, value_type='float',
            ),
            cache_seconds=self.get_config_value(
                'Multitest_Command', 'corescope_cache_seconds',
                fallback=DEFAULT_CACHE_SECONDS, value_type='int',
            ),
            logger=self.logger,
        )

    @property
    def uses_corescope(self) -> bool:
        """True when paths come from CoreScope rather than from listening."""
        return self._corescope is not None

    @property
    def condense_paths(self) -> bool:
        """True when any condensed layout is enabled (flat or nested)."""
        return self.condense_paths_mode != "off"

    def get_help_text(self, message: MeshMessage | None = None) -> str:
        return self.translate('commands.multitest.help', fallback="Listens for 6 seconds and collects all unique paths from incoming messages")

    def extract_path_from_rf_data(self, rf_data: dict) -> Optional[str]:
        """Extract path in prefix string format from RF data routing_info.
        Supports 1-, 2-, and 3-byte-per-hop (2, 4, or 6 hex chars per node).
        """
        try:
            routing_info = rf_data.get('routing_info')
            if not routing_info:
                return None

            path_nodes = routing_info.get('path_nodes', [])
            if not path_nodes:
                # Fallback: build from path_hex using bytes_per_hop from packet
                path_hex = routing_info.get('path_hex', '')
                if path_hex:
                    bytes_per_hop = routing_info.get('bytes_per_hop')
                    n = (bytes_per_hop * 2) if bytes_per_hop and bytes_per_hop >= 1 else getattr(self.bot, 'prefix_hex_chars', 2)
                    if n <= 0:
                        n = 2
                    path_nodes = [path_hex[i:i + n] for i in range(0, len(path_hex), n)]
                    if (len(path_hex) % n) != 0:
                        path_nodes = [path_hex[i:i + 2] for i in range(0, len(path_hex), 2)]

            if path_nodes:
                # Validate: each node 2, 4, or 6 hex chars
                valid_parts = []
                for node in path_nodes:
                    node_str = str(node).lower().strip()
                    if len(node_str) in (2, 4, 6) and all(c in '0123456789abcdef' for c in node_str):
                        valid_parts.append(node_str)
                if valid_parts:
                    return ','.join(valid_parts)
            return None
        except Exception as e:
            self.logger.debug(f"Error extracting path from RF data: {e}")
            return None

    def extract_path_from_message(self, message: MeshMessage) -> Optional[str]:
        """Extract path in prefix string format from a message.
        Prefers message.routing_info.path_nodes when present (multi-byte).
        When routing_info has bytes_per_hop, uses it instead of inferring hop size.
        Otherwise parses message.path: comma-separated tokens infer (2/4/6 hex);
        continuous hex uses bot.prefix_hex_chars via parse_path_string().
        """
        routing_info = getattr(message, 'routing_info', None)
        bytes_per_hop = routing_info.get('bytes_per_hop') if routing_info else None
        hex_chars_per_node = (bytes_per_hop * 2) if (bytes_per_hop and bytes_per_hop >= 1) else None

        # Prefer routing_info when present (same as path command; no re-parse)
        if routing_info is not None:
            if routing_info.get('path_length', 0) == 0:
                return None
            path_nodes = routing_info.get('path_nodes', [])
            if not path_nodes and hex_chars_per_node:
                # Build from path_hex when path_nodes missing but bytes_per_hop known
                path_hex = routing_info.get('path_hex', '')
                if path_hex:
                    n = hex_chars_per_node
                    path_nodes = [path_hex[i:i + n] for i in range(0, len(path_hex), n)]
                    if (len(path_hex) % n) != 0:
                        path_nodes = [path_hex[i:i + 2] for i in range(0, len(path_hex), 2)]
            if path_nodes:
                valid = [str(n).lower().strip() for n in path_nodes
                         if len(str(n).strip()) in (2, 4, 6) and all(c in '0123456789abcdef' for c in str(n).lower().strip())]
                if valid:
                    return ','.join(valid)
        if not message.path:
            return None
        if "Direct" in message.path or "0 hops" in message.path:
            return None

        path_string = message.path
        if " via ROUTE_TYPE_" in path_string:
            path_string = path_string.split(" via ROUTE_TYPE_")[0]
        path_string = re.sub(r'\s*\([^)]*hops?[^)]*\)', '', path_string, flags=re.IGNORECASE).strip()

        # When bytes_per_hop is known, use it; otherwise infer from commas or use bot.prefix_hex_chars
        expected_n = hex_chars_per_node or getattr(self.bot, 'prefix_hex_chars', 2)
        if expected_n <= 0:
            expected_n = 2

        if ',' in path_string:
            tokens = [t.strip() for t in path_string.split(',') if t.strip()]
            if tokens:
                if hex_chars_per_node:
                    # Use known hop size: all tokens must be that length
                    valid_hex = all(
                        len(t) == expected_n and all(c in '0123456789aAbBcCdDeEfF' for c in t)
                        for t in tokens
                    )
                    if valid_hex:
                        return ','.join(t.lower() for t in tokens)
                else:
                    # Infer from token length (2, 4, or 6, all same)
                    lengths = {len(t) for t in tokens}
                    valid_hex = all(
                        len(t) in (2, 4, 6) and all(c in '0123456789aAbBcCdDeEfF' for c in t)
                        for t in tokens
                    )
                    if valid_hex and len(lengths) == 1 and next(iter(lengths)) in (2, 4, 6):
                        return ','.join(t.lower() for t in tokens)
        # Continuous hex: use bytes_per_hop when known, else bot.prefix_hex_chars
        node_ids = parse_path_string(path_string, prefix_hex_chars=expected_n)
        if node_ids:
            return ','.join(n.lower() for n in node_ids)
        return None

    def _get_routing_info_path_byte_length(self, routing_info: dict) -> int:
        """Best-effort extraction of path byte length from routing_info."""
        if not routing_info:
            return 0

        raw_path_byte_length = routing_info.get('path_byte_length')
        if isinstance(raw_path_byte_length, int) and raw_path_byte_length >= 0:
            return raw_path_byte_length

        bytes_per_hop = routing_info.get('bytes_per_hop')
        path_length = routing_info.get('path_length')
        if (
            isinstance(bytes_per_hop, int)
            and bytes_per_hop >= 0
            and isinstance(path_length, int)
            and path_length >= 0
        ):
            return bytes_per_hop * path_length

        path_nodes = routing_info.get('path_nodes') or []
        total = 0
        for node in path_nodes:
            node_str = str(node).strip()
            if not node_str:
                continue
            total += len(node_str) // 2
        return total

    def get_rf_data_for_message(self, message: MeshMessage) -> Optional[dict]:
        """Get RF data for a message by looking it up in recent RF data"""
        try:
            # Try multiple correlation strategies
            # Strategy 1: Use sender_pubkey to find recent RF data
            if message.sender_pubkey:
                # Try full pubkey first
                recent_rf_data = self.bot.message_handler.find_recent_rf_data(message.sender_pubkey)
                if recent_rf_data:
                    return recent_rf_data

                # Try pubkey prefix (first 16 chars)
                if len(message.sender_pubkey) >= 16:
                    pubkey_prefix = message.sender_pubkey[:16]
                    recent_rf_data = self.bot.message_handler.find_recent_rf_data(pubkey_prefix)
                    if recent_rf_data:
                        return recent_rf_data

            # Strategy 2: Look through recent RF data for matching pubkey
            if message.sender_pubkey and self.bot.message_handler.recent_rf_data:
                # Search recent RF data for matching pubkey
                for rf_data in reversed(self.bot.message_handler.recent_rf_data):
                    rf_pubkey = rf_data.get('pubkey_prefix', '')
                    if rf_pubkey and message.sender_pubkey.startswith(rf_pubkey):
                        return rf_data

            # Strategy 3: Use most recent RF data as fallback
            # This is less reliable but might work if timing is very close
            if self.bot.message_handler.recent_rf_data:
                # Get the most recent RF data entry within a short time window
                current_time = time.time()
                recent_entries = [
                    rf for rf in self.bot.message_handler.recent_rf_data
                    if current_time - rf.get('timestamp', 0) < 5.0  # Within last 5 seconds
                ]
                if recent_entries:
                    most_recent = max(recent_entries, key=lambda x: x.get('timestamp', 0))
                    return most_recent

            return None
        except Exception as e:
            self.logger.debug(f"Error getting RF data for message: {e}")
            return None

    def on_message_received(self, message: MeshMessage):
        """Callback method called by message handler when a message is received during listening.

        Checks all active sessions to see if this message matches any of them.
        """
        if not self._active_sessions:
            return

        # Get RF data for this message (contains pre-calculated packet hash)
        rf_data = self.get_rf_data_for_message(message)
        if not rf_data:
            # Can't get RF data, skip this message
            self.logger.debug(f"Skipping message - no RF data found (sender: {message.sender_id})")
            return

        # Use pre-calculated packet hash if available, otherwise calculate it
        message_hash = rf_data.get('packet_hash')
        if not message_hash and rf_data.get('raw_hex'):
            # Fallback: calculate hash if not stored (for older RF data)
            try:
                payload_type = None
                routing_info = rf_data.get('routing_info', {})
                if routing_info:
                    # Try to get payload type from routing_info if available
                    payload_type = routing_info.get('payload_type')
                message_hash = calculate_packet_hash(rf_data['raw_hex'], payload_type)
            except Exception as e:
                self.logger.debug(f"Error calculating packet hash: {e}")
                message_hash = None

        if not message_hash:
            # Can't determine hash, skip this message
            self.logger.debug(f"Skipping message - could not determine packet hash (sender: {message.sender_id})")
            return

        # Check all active sessions to see if this message matches any of them
        current_time = time.time()
        for user_id, session in list(self._active_sessions.items()):
            # Check if we're still in the listening window for this session
            elapsed = current_time - session.listening_start_time
            if elapsed >= session.listening_duration:
                continue  # Session expired, skip it

            # CRITICAL: Only collect paths if this message has the same hash as the target
            # This ensures we only track variations of the same original message
            if message_hash == session.target_packet_hash:
                routing_info = rf_data.get('routing_info', {})
                path_byte_length = self._get_routing_info_path_byte_length(routing_info)
                if not self._path_bytes_match_requirement(path_byte_length, session.required_path_bytes_mode):
                    continue

                # Try to extract path from RF data first (more reliable)
                path = self.extract_path_from_rf_data(rf_data)

                # Fallback to message path if RF data extraction failed
                if not path:
                    path = self.extract_path_from_message(message)

                if path:
                    session.collected_paths.add(path)
                    self.logger.info(f"✓ Collected path for user {user_id}: {path} (hash: {message_hash[:8]}...)")
                else:
                    # Log when we have a matching hash but can't extract path
                    path_length = routing_info.get('path_length', 0)
                    if path_length == 0:
                        self.logger.debug(f"Matched hash {message_hash[:8]}... but path is direct (0 hops) for user {user_id}")
                    else:
                        self.logger.debug(f"Matched hash {message_hash[:8]}... but couldn't extract path from routing_info: {routing_info} for user {user_id}")
            else:
                # Log hash mismatches for debugging (but limit to avoid spam)
                self.logger.debug(f"✗ Hash mismatch for user {user_id} - target: {session.target_packet_hash[:8]}..., received: {message_hash[:8]}... (sender: {message.sender_id})")

    def _scan_recent_rf_data(self, session: MultitestSession):
        """Scan recent RF data for packets with matching hash (for messages that haven't been processed yet)

        Args:
            session: The multitest session to scan for
        """
        if not session.target_packet_hash:
            return

        try:
            current_time = time.time()
            matching_count = 0
            mismatching_count = 0

            # Look at RF data from the last few seconds (before listening started, in case packets arrived just before)
            for rf_data in self.bot.message_handler.recent_rf_data:
                # Check if this RF data is recent enough
                rf_timestamp = rf_data.get('timestamp', 0)
                time_diff = current_time - rf_timestamp

                # Only include RF data from the triggering message timestamp onwards
                # This prevents collecting packets from earlier messages that happen to have the same hash
                if rf_timestamp >= session.triggering_timestamp and time_diff <= session.listening_duration:
                    packet_hash = rf_data.get('packet_hash')

                    # CRITICAL: Only process if hash matches exactly and is not None/empty
                    if packet_hash and packet_hash == session.target_packet_hash:
                        routing_info = rf_data.get('routing_info', {})
                        path_byte_length = self._get_routing_info_path_byte_length(routing_info)
                        if not self._path_bytes_match_requirement(path_byte_length, session.required_path_bytes_mode):
                            continue

                        matching_count += 1
                        # Extract path from this RF data
                        path = self.extract_path_from_rf_data(rf_data)
                        if path:
                            session.collected_paths.add(path)
                            self.logger.info(f"✓ Collected path from RF scan for user {session.user_id}: {path} (hash: {packet_hash[:8]}..., time: {time_diff:.2f}s)")
                        else:
                            self.logger.debug(f"Matched hash {packet_hash[:8]}... in RF scan but couldn't extract path for user {session.user_id}")
                    elif packet_hash:
                        mismatching_count += 1
                        # Only log first few mismatches to avoid spam
                        if mismatching_count <= 3:
                            self.logger.debug(f"✗ RF scan hash mismatch for user {session.user_id} - target: {session.target_packet_hash[:8]}..., found: {packet_hash[:8]}... (time: {time_diff:.2f}s)")

            if matching_count > 0 or mismatching_count > 0:
                self.logger.debug(f"RF scan complete for user {session.user_id}: {matching_count} matching, {mismatching_count} mismatching packets")
        except Exception as e:
            self.logger.debug(f"Error scanning recent RF data for user {session.user_id}: {e}")

    async def _render_reply(
        self,
        message: MeshMessage,
        template: str,
        *,
        paths_text: str,
        path_count: int,
        listening_duration: int,
    ) -> str:
        """Render the result template, off the event loop.

        Takes the same placeholders and ``|filter`` chains as the test reply and
        the path reply prefix: every standard message field (``{packet_hash}``,
        ``{path}``, ``{hops}``, ``{snr}``, ...) describing the *triggering*
        message, plus this command's ``{path_count}``, ``{paths}`` and
        ``{listening_duration}``. An unknown placeholder renders as nothing, the
        way it does in the other piped templates.

        Async because a ``shorten`` filter makes a blocking HTTP call, which
        rendered inline would stall radio RX and every other handler for its
        whole timeout.
        """
        default = f"Paths({path_count}):\n{paths_text}"
        try:
            fields: dict[str, Any] = self.get_standard_placeholder_fields(message)
            fields.update({
                'sender': message.sender_id or "Unknown",
                'path_count': path_count,
                'paths': paths_text,
                'listening_duration': listening_duration,
            })
            return await format_piped_template_async(
                template,
                {k: str(v) for k, v in fields.items()},
                message=message,
                logger=self.logger,
                config=self.bot.config,
                prefix_hex_chars=getattr(self.bot, 'prefix_hex_chars', 2),
            )
        except (KeyError, ValueError) as e:
            # Fall back to the plain path list rather than dropping the reply.
            self.logger.debug(f"Error formatting multitest response: {e}, using default format")
            return default

    async def _collect_locally(self, session: MultitestSession) -> None:
        """Listen for the window, collecting paths the bot's own radio hears.

        Scanning before and after covers packets the message handler had not
        processed yet when the window opened, and those that arrive while it is
        closing.
        """
        self._scan_recent_rf_data(session)
        await asyncio.sleep(session.listening_duration)
        self._scan_recent_rf_data(session)

    async def _send_corescope_ack(
        self, message: MeshMessage, session: MultitestSession
    ) -> None:
        """Tell the user the request landed, before waiting out the settle.

        Without this the bot is silent for the whole settle and reads as having
        ignored the request. Sent with ``skip_user_rate_limit`` because the user's
        budget should be charged for the result, not for the bot's progress note,
        and truncated rather than chunked if an operator configures something
        long: a progress note is not worth two transmissions.
        """
        template = self.corescope_ack_message
        if not template:
            return

        wait_seconds = int(round(session.listening_duration))
        text = await self._render_ack(message, template, wait_seconds=wait_seconds)
        if not text.strip():
            return

        budget = self.get_max_message_length(message)
        if len(text.encode("utf-8")) > budget:
            chunks = self.bot.command_manager.split_text_into_utf8_chunks(text, budget)
            self.logger.debug(
                f"Multitest: acknowledgement is longer than {budget} bytes, truncating"
            )
            text = chunks[0] if chunks else text
        await self.send_response(message, text, skip_user_rate_limit=True)

    async def _render_ack(
        self, message: MeshMessage, template: str, *, wait_seconds: int
    ) -> str:
        """Render the acknowledgement, off the event loop like the reply.

        Takes ``{wait_seconds}`` -- the wait this run will actually do, variant
        scaling included -- plus the standard message placeholders, so the text
        cannot promise a delay the command is not about to take.
        """
        try:
            fields: dict[str, Any] = self.get_standard_placeholder_fields(message)
            fields.update({
                'sender': message.sender_id or "Unknown",
                'wait_seconds': wait_seconds,
            })
            return await format_piped_template_async(
                template,
                {k: str(v) for k, v in fields.items()},
                message=message,
                logger=self.logger,
                config=self.bot.config,
                prefix_hex_chars=getattr(self.bot, 'prefix_hex_chars', 2),
            )
        except (KeyError, ValueError) as e:
            # Silence is the thing this message exists to prevent, so fall back to
            # the built-in text rather than dropping it.
            self.logger.debug(f"Error formatting multitest acknowledgement: {e}, using default")
            return self.DEFAULT_ACK_MESSAGE.replace("{wait_seconds}", str(wait_seconds))

    async def _collect_from_corescope(
        self, message: MeshMessage, session: MultitestSession, *, started: float
    ) -> Optional[ScopePacket]:
        """Ask CoreScope for the packet's paths instead of listening for them.

        No listening window, no RF scan, no listener registration: the observer
        network already heard what this command would have waited for, and from
        more radios than this one has. ``None`` when the lookup found nothing,
        which the caller turns into :meth:`_corescope_miss_response`.

        Acknowledges first, then waits out the settle so observers have reported
        before the lookup. The wait runs to a deadline measured from *started*,
        the moment this command picked the trigger up, rather than sleeping a flat
        interval after the acknowledgement: that send takes real time (the rate
        limiter, and a DM waits for an ACK), which would otherwise push the total
        past the configured wait.

        The client blocks on HTTP, so it runs in the executor. Inline it would
        stall radio RX and every other handler for the whole timeout.
        """
        client = self._corescope
        if client is None:
            return None

        await self._send_corescope_ack(message, session)

        remaining = session.listening_duration - (time.monotonic() - started)
        if remaining > 0:
            await asyncio.sleep(remaining)

        self.logger.info(
            f"Multitest: asking CoreScope for packet {session.target_packet_hash} "
            f"after {session.listening_duration:g}s"
        )
        return await asyncio.get_running_loop().run_in_executor(
            None, partial(client.fetch, session.target_packet_hash)
        )

    async def _end_session(self, user_id: str) -> None:
        """Drop the session and release the listener when nothing else needs it."""
        async with self._get_execution_lock():
            self._active_sessions.pop(user_id, None)
            if not self._active_sessions and self.bot.message_handler.multitest_listener == self:
                self.bot.message_handler.multitest_listener = None

    def _lay_out_paths(self, paths: list[str]) -> str:
        """Render *paths* per the configured layout. One path is always one line."""
        if len(paths) > 1 and self.condense_paths_mode in ("flat", "nested"):
            return _condense_path_lines(paths, self.condense_paths_mode)
        return "\n".join(paths)

    def _corescope_miss_response(
        self, tracking_hash: Optional[str], session: MultitestSession
    ) -> str:
        """Reply for a CoreScope lookup that came back with nothing.

        CoreScope mode does not listen, so there is no local path list to fall
        back on -- except the triggering message's own path, which was extracted
        at trigger time and costs nothing to report. Saying which packet missed
        matters: the usual cause is that no observer covers the bot's area, and
        the hash is what the operator needs to check that on the map.
        """
        miss = f"No CoreScope data for packet {tracking_hash}."
        if session.initial_path:
            return f"{miss} Heard locally: {session.initial_path}"
        return f"{miss} No observer has reported it."

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the multitest command"""
        user_id = message.sender_id or "unknown"
        # Marks when this command picked the trigger up, so a CoreScope settle can
        # run to a deadline rather than a flat sleep after the acknowledgement.
        started = time.monotonic()
        required_path_bytes_mode = self._get_required_path_bytes_setting('Multitest_Command')
        # Before anything is sent: a request the path-byte requirement rejects gets
        # the configured reject reply or silence, never an acknowledgement.
        if not await self.enforce_path_byte_requirement(message, 'Multitest_Command'):
            return True

        # Use lock to prevent concurrent execution from interfering
        async with self._get_execution_lock():
            # Check if user already has an active session
            if user_id in self._active_sessions:
                existing_session = self._active_sessions[user_id]
                elapsed = time.time() - existing_session.listening_start_time
                if elapsed < existing_session.listening_duration:
                    # User already has an active session - silently ignore second Mt
                    # so the first session can complete and send its response
                    return True

            # Record execution time BEFORE starting async work to prevent race conditions
            self.record_execution(user_id)

            # Determine how long this run occupies: the listening window locally,
            # the settle in CoreScope mode. Either way it is what the duplicate-session
            # guard above measures against and what {listening_duration} reports.
            _trigger, args = self.split_trigger_and_args(message.content)
            # Variants: "<trigger> long" / "<trigger> xlong" (trigger = multitest, mt, or alias)
            variant = args.split()[0].lower() if args else ""
            if self.uses_corescope:
                listening_duration = self._settle_for_variant(variant)
                self.logger.info(
                    f"Multitest command ({variant or 'default'}) executed by {user_id} - "
                    f"waiting {listening_duration:g}s for observers to report to CoreScope"
                )
            elif variant == 'long':
                listening_duration = 10.0
                self.logger.info(f"Multitest command (long) executed by {user_id} - starting 10 second listening window")
            elif variant == 'xlong':
                listening_duration = 14.0
                self.logger.info(f"Multitest command (xlong) executed by {user_id} - starting 14 second listening window")
            else:
                listening_duration = 6.0
                self.logger.info(f"Multitest command executed by {user_id} - starting 6 second listening window")

            # Get RF data for the triggering message (contains pre-calculated packet hash)
            rf_data = self.get_rf_data_for_message(message)
            if not rf_data:
                response = "Error: Could not find packet data for this message. Please try again."
                await self.send_response(message, response)
                return True

            # Use pre-calculated packet hash if available, otherwise calculate it
            packet_hash = rf_data.get('packet_hash')
            if not packet_hash and rf_data.get('raw_hex'):
                # Fallback: calculate hash if not stored (for older RF data)
                # IMPORTANT: Must use same payload_type that was used during ingestion
                payload_type = None
                routing_info = rf_data.get('routing_info', {})
                if routing_info:
                    payload_type = routing_info.get('payload_type')
                packet_hash = calculate_packet_hash(rf_data['raw_hex'], payload_type)

            if not packet_hash:
                response = "Error: Could not calculate packet hash for this message. Please try again."
                await self.send_response(message, response)
                return True

            # Store the timestamp of the triggering message to avoid collecting older packets
            triggering_rf_timestamp = rf_data.get('timestamp', time.time())

            # Also extract path from the triggering message itself
            initial_path = self.extract_path_from_message(message)
            # Also try to extract from RF data (more reliable)
            if not initial_path and rf_data:
                initial_path = self.extract_path_from_rf_data(rf_data)

            if initial_path:
                self.logger.debug(f"Initial path from triggering message for user {user_id}: {initial_path}")

            # Create a new session for this user
            session = MultitestSession(
                user_id=user_id,
                target_packet_hash=packet_hash,
                triggering_timestamp=triggering_rf_timestamp,
                listening_start_time=time.time(),
                listening_duration=listening_duration,
                collected_paths=set(),
                initial_path=initial_path,
                required_path_bytes_mode=required_path_bytes_mode,
            )

            # Add initial path if available
            if initial_path:
                session.collected_paths.add(initial_path)

            # Register this session
            self._active_sessions[user_id] = session

            # Only the local listening window needs the message handler to feed it
            # matching packets; CoreScope mode never collects any itself.
            if not self.uses_corescope and self.bot.message_handler.multitest_listener is None:
                self.bot.message_handler.multitest_listener = self

            self.logger.info(f"Tracking packet hash for user {user_id}: {packet_hash[:16]}... (full: {packet_hash})")
            self.logger.debug(f"Triggering message timestamp for user {user_id}: {triggering_rf_timestamp}")

        # Lock released, so other users can start their own sessions while this one
        # waits.
        scope: Optional[ScopePacket] = None
        try:
            if self.uses_corescope:
                scope = await self._collect_from_corescope(
                    message, session, started=started
                )
            else:
                await self._collect_locally(session)
        finally:
            await self._end_session(user_id)

        # Store hash for error message before clearing it
        tracking_hash = session.target_packet_hash

        # One source or the other, never both: CoreScope mode did not listen, so
        # session.collected_paths holds nothing but the triggering message's own path.
        if self.uses_corescope:
            paths = list(scope.paths) if scope is not None else []
        else:
            paths = sorted(session.collected_paths)

        # Format the collected paths
        if paths:
            paths_text = self._lay_out_paths(paths)

            # Configured template or the built-in default; both take the same
            # placeholders and filters, and both render the same way.
            response = await self._render_reply(
                message,
                self.response_format or self.DEFAULT_FORMAT,
                paths_text=paths_text,
                path_count=len(paths),
                listening_duration=int(session.listening_duration),
            )
        elif self.uses_corescope:
            response = self._corescope_miss_response(tracking_hash, session)
        else:
            # Provide more helpful error message with diagnostic info
            matching_packets = 0
            if self.bot.message_handler.recent_rf_data and tracking_hash:
                for rf_data in self.bot.message_handler.recent_rf_data:
                    if rf_data.get('packet_hash') == tracking_hash:
                        matching_packets += 1

            if tracking_hash is None:
                response = ("Error: Could not determine packet hash for tracking. "
                           "The triggering message may not have valid packet data.")
            elif matching_packets > 0:
                response = (f"No paths extracted from {matching_packets} matching packet(s) "
                           f"(hash: {tracking_hash}). "
                           f"Packets may be direct (0 hops) or path extraction failed.")
            else:
                response = (f"No matching packets found during {session.listening_duration}s window. "
                           f"Tracking hash: {tracking_hash}. ")

        # Also wait for user rate limiter if needed
        if not self.bot.rate_limiter.can_send():
            wait_time = self.bot.rate_limiter.time_until_next()
            if wait_time > 0:
                self.logger.info(f"Waiting {wait_time:.1f} seconds for rate limiter")
                await asyncio.sleep(wait_time + 0.1)  # Small buffer

        # Send without truncating path tokens; chunk if over DM/channel budget
        max_len = self.get_max_message_length(message)
        if len(response.encode("utf-8")) <= max_len:
            await self.send_response(message, response)
        else:
            chunks = self.bot.command_manager.split_text_into_utf8_chunks(response, max_len)
            await self.send_response_chunked(message, chunks)

        return True

