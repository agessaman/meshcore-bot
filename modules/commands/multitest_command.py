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
from typing import Literal, Optional

CondensePathsMode = Literal["off", "flat", "nested"]

from ..models import MeshMessage
from ..utils import calculate_packet_hash, parse_path_string
from .base_command import BaseCommand

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


def _grouped_suffix_line_specs(non_empty: list[list[str]]) -> list[tuple[str, str]]:
    """Build (line_kind, text) rows: group by first hop, then longest in-group prefix on the head line.

    So paths 96,e0 / 96,e0,01 / … share head '96,e0' and nest 01, … instead of head '96' with
    misleading 'e0' as if it were the only endpoint under that branch.

    kind "end": path that ends exactly at inner_lcp (shorter alternate route) — top-level branch
    with the same text as head, listed so every collected suffix appears as an endpoint.
    """
    by_first: dict[str, list[list[str]]] = defaultdict(list)
    for suf in non_empty:
        by_first[suf[0]].append(suf[1:])

    specs: list[tuple[str, str]] = []
    for ft in sorted(by_first.keys()):
        rests = by_first[ft]
        full_sufs = [[ft, *r] for r in rests]
        if len(full_sufs) == 1:
            specs.append(("head", ",".join(full_sufs[0])))
            continue
        inner_lcp = _longest_common_prefix(full_sufs)
        head_text = ",".join(inner_lcp)
        remainders = [s[len(inner_lcp) :] for s in full_sufs]
        has_exact = any(len(r) == 0 for r in remainders)
        nested = sorted((r for r in remainders if r), key=lambda x: ",".join(x))
        if not nested:
            specs.append(("head", head_text))
            continue
        specs.append(("head", head_text))
        for rem in nested:
            specs.append(("nest", ",".join(rem)))
        if has_exact:
            specs.append(("end", head_text))
    return specs


def _apply_tee_prefixes(specs: list[tuple[str, str]]) -> list[str]:
    """Assign ├/└ from flattened order; nested rows use 　 before ├/└. Only the final row uses └."""
    n = len(specs)
    out: list[str] = []
    for i, (kind, text) in enumerate(specs):
        last = i == n - 1
        if kind in ("head", "end"):
            p = _BRANCH_LAST if last else _BRANCH_INTER
            out.append(f"{p} {text}")
        else:
            p = _BRANCH_LAST if last else _BRANCH_INTER
            out.append(f"{_INDENT_NEST}{p} {text}")
    return out


def _apply_tee_prefixes_flat(specs: list[tuple[str, str]]) -> list[str]:
    """Like `_apply_tee_prefixes` but nested rows use └ when the next row is a new top branch (head/end)."""
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


def _format_suffix_branch_lines(suffix_tokens: list[list[str]]) -> list[str]:
    """Format suffixes after display LCP: group by first hop, in-group LCP on head, nest tails indented with U+3000."""
    non_empty = [s for s in suffix_tokens if s]
    if not non_empty:
        return []

    if len(non_empty) == 1:
        return _tree_branch_lines_flat([",".join(non_empty[0])])

    specs = _grouped_suffix_line_specs(non_empty)
    return _apply_tee_prefixes(specs)


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


def _format_path_cluster(token_lists: list[list[str]], use_brackets: bool) -> list[str]:
    """Format a cluster into condensed lines (common prefix + ┐ + ├/└, nested tails indented with 　).

    The shared path line ends with ┐ (U+2510) when branch lines follow. Suffixes are grouped by
    their first hop after the display LCP; within a group, the longest common prefix of all
    suffixes in that group is one ├ line, then 　├/　└ for each distinct tail. The last line of the
    block uses └ at top level or 　└ when nested.

    If one path stops exactly where another continues, the displayed LCP is shortened so the shared
    segment is not mistaken for a single endpoint (e.g. only └ tail after a full shorter path).

    Every path in token_lists is represented (no prefix paths dropped as ``...``).
    """
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
        branch_lines = _format_suffix_branch_lines(suffix_tokens)
        if branch_lines:
            lines = [f"{common} {_BRANCH_CORNER}"]
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
        sub_lines = _format_path_cluster(groups[ft], use_brackets=multi)
        lines.extend(sub_lines)
    return lines


def _format_path_cluster_flat(token_lists: list[list[str]], use_brackets: bool) -> list[str]:
    """Like `_format_path_cluster` but suffix rows use the flat layout (full paths per branch when possible)."""
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
            lines = [f"{common} {_BRANCH_CORNER}"]
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
        branch_lines = _nested_format_suffix_lines(suffix_tokens)
        if branch_lines:
            nonempty_sfx = [s for s in suffix_tokens if s]
            ne_lcp = _longest_common_prefix(nonempty_sfx) if nonempty_sfx else []
            if any(len(t) == 0 for t in suffix_tokens) and len(ne_lcp) == 0:
                return [f"{_BRANCH_INTER} {common}", *branch_lines]
            return [f"{common} {_BRANCH_CORNER}", *branch_lines]
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


class MultitestCommand(BaseCommand):
    """Handles the multitest command - listens for multiple path variations"""

    # Plugin metadata
    name = "multitest"
    keywords = ['multitest', 'mt']
    description = "Listens for 6 seconds and collects all unique paths from incoming messages"
    category = "meshcore_info"

    # Documentation
    short_description = "Listens for 6 seconds and collects all unique paths your incoming messages took to reach the bot"
    usage = "multitest"
    examples = ["multitest", "mt"]

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

    def can_execute(self, message: MeshMessage, skip_channel_check: bool = False) -> bool:
        """Check if this command can be executed with the given message.

        Args:
            message: The message triggering the command.

        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.multitest_enabled:
            return False
        return super().can_execute(message)

    def _load_config(self):
        """Load configuration for multitest command"""
        response_format = self.get_config_value('Multitest_Command', 'response_format', fallback='')
        if response_format and response_format.strip():
            # Strip quotes if present (config parser may add them)
            response_format = self._strip_quotes_from_config(response_format).strip()
            # Decode escape sequences (e.g., \n -> newline)
            try:
                # Use encode/decode to convert escape sequences to actual characters
                self.response_format = response_format.encode('latin-1').decode('unicode_escape')
            except (UnicodeDecodeError, UnicodeEncodeError):
                # If decoding fails, use as-is (fallback)
                self.response_format = response_format
        else:
            self.response_format = None  # Use default format

        raw_cp = self.get_config_value(
            'Multitest_Command', 'condense_paths', fallback='true'
        )
        self.condense_paths_mode: CondensePathsMode = _parse_condense_paths_mode(raw_cp)

    @property
    def condense_paths(self) -> bool:
        """True when any condensed layout is enabled (flat or nested)."""
        return self.condense_paths_mode != "off"

    def get_help_text(self) -> str:
        return self.translate('commands.multitest.help', fallback="Listens for 6 seconds and collects all unique paths from incoming messages")

    def matches_keyword(self, message: MeshMessage) -> bool:
        """Check if message matches multitest keyword"""
        content_lower = self.cleanup_message_for_matching(message)

        # Check for exact match or keyword followed by space
        for keyword in self.keywords:
            if content_lower == keyword or content_lower.startswith(keyword + ' '):
                return True

        # Check for variants: "mt long", "mt xlong", "multitest long", "multitest xlong"
        if content_lower.startswith('mt ') or content_lower.startswith('multitest '):
            parts = content_lower.split()
            if len(parts) >= 2 and parts[0] in ['mt', 'multitest']:
                variant = parts[1]
                if variant in ['long', 'xlong']:
                    return True

        return False

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
                    routing_info = rf_data.get('routing_info', {})
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

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the multitest command"""
        user_id = message.sender_id or "unknown"

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

            # Determine listening duration based on command variant
            content = message.content.strip()
            if content.startswith('!'):
                content = content[1:].strip()

            content_lower = content.lower()
            listening_duration = 6.0  # Default
            # Check for variants: "mt long", "mt xlong", "multitest long", "multitest xlong"
            if content_lower.startswith('mt ') or content_lower.startswith('multitest '):
                parts = content_lower.split()
                if len(parts) >= 2 and parts[0] in ['mt', 'multitest']:
                    variant = parts[1]
                    if variant == 'long':
                        listening_duration = 10.0
                        self.logger.info(f"Multitest command (long) executed by {user_id} - starting 10 second listening window")
                    elif variant == 'xlong':
                        listening_duration = 14.0
                        self.logger.info(f"Multitest command (xlong) executed by {user_id} - starting 14 second listening window")
                    else:
                        self.logger.info(f"Multitest command executed by {user_id} - starting 6 second listening window")
                else:
                    self.logger.info(f"Multitest command executed by {user_id} - starting 6 second listening window")
            else:
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
                initial_path=initial_path
            )

            # Add initial path if available
            if initial_path:
                session.collected_paths.add(initial_path)

            # Register this session
            self._active_sessions[user_id] = session

            # Register this command instance as the active listener (if not already registered)
            # Store reference in message handler so it can call on_message_received
            if self.bot.message_handler.multitest_listener is None:
                self.bot.message_handler.multitest_listener = self

            self.logger.info(f"Tracking packet hash for user {user_id}: {packet_hash[:16]}... (full: {packet_hash})")
            self.logger.debug(f"Triggering message timestamp for user {user_id}: {triggering_rf_timestamp}")

        # Release lock before async sleep to allow other users to start their sessions
        # Also scan recent RF data for matching hashes (in case messages haven't been processed yet)
        # But only include packets that arrived at or after the triggering message
        self._scan_recent_rf_data(session)

        try:
            # Wait for the listening duration
            await asyncio.sleep(session.listening_duration)
        finally:
            # Re-acquire lock to clean up session
            async with self._get_execution_lock():
                # Remove this session
                if user_id in self._active_sessions:
                    del self._active_sessions[user_id]

                # Unregister listener if no more active sessions
                if not self._active_sessions and self.bot.message_handler.multitest_listener == self:
                    self.bot.message_handler.multitest_listener = None

        # Do a final scan of RF data in case any matching packets arrived
        self._scan_recent_rf_data(session)

        # Store hash for error message before clearing it
        tracking_hash = session.target_packet_hash

        # Format the collected paths
        if session.collected_paths:
            # Sort paths for consistent output
            sorted_paths = sorted(session.collected_paths)
            if len(sorted_paths) > 1 and self.condense_paths_mode == "flat":
                paths_text = _condense_path_lines(sorted_paths, "flat")
            elif len(sorted_paths) > 1 and self.condense_paths_mode == "nested":
                paths_text = _condense_path_lines(sorted_paths, "nested")
            else:
                paths_text = "\n".join(sorted_paths)
            path_count = len(sorted_paths)

            # Use configured format if available, otherwise use default
            if self.response_format:
                try:
                    response = self.response_format.format(
                        sender=message.sender_id or "Unknown",
                        path_count=path_count,
                        paths=paths_text,
                        listening_duration=int(session.listening_duration)
                    )
                except (KeyError, ValueError) as e:
                    # If formatting fails, fall back to default
                    self.logger.debug(f"Error formatting multitest response: {e}, using default format")
                    response = f"Found {path_count} unique path(s):\n{paths_text}"
            else:
                # Default format
                response = f"Found {path_count} unique path(s):\n{paths_text}"
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

        # Wait for bot TX rate limiter cooldown to expire before sending
        # This ensures we respond even if another command put the bot on cooldown
        await self.bot.bot_tx_rate_limiter.wait_for_tx()

        # Also wait for user rate limiter if needed
        if not self.bot.rate_limiter.can_send():
            wait_time = self.bot.rate_limiter.time_until_next()
            if wait_time > 0:
                self.logger.info(f"Waiting {wait_time:.1f} seconds for rate limiter")
                await asyncio.sleep(wait_time + 0.1)  # Small buffer

        # Send the response
        await self.send_response(message, response)

        return True

