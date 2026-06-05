"""Comment-preserving INI writer with backup support.

``configparser.ConfigParser.write()`` rewrites a config file from scratch and
discards every comment, blank line and original formatting.  Because
``config.ini`` is the heavily-commented source of truth for the bot, we cannot
use it to persist UI-driven setting changes.

This module performs a *surgical*, line-based update: it walks the existing
file, rewrites only the value portion of keys that changed, appends new keys to
the end of their section, and appends brand-new sections at EOF.  Comments,
blank lines and section ordering are preserved.  A timestamped backup is taken
before every write and the final replace is atomic.

The module deliberately has no Flask or bot-core dependencies so it can be
imported by both the bot process and the (separate) web-viewer process.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import datetime

# A section header line: ``[Section]`` (optionally indented / trailing space).
_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")

# A key/value line: ``key = value`` or ``key : value``.  Leading ``;`` or ``#``
# means it's a comment, not a key.  Captures indentation, key, separator and
# value so we can rewrite only the value while preserving spacing.  Horizontal
# whitespace only ([ \t]) in indent/sep so an empty value never lets the trailing
# newline get absorbed into the separator (which would split the rebuilt line).
_KEY_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>[^;#=:\s][^=:]*?)(?P<sep>[ \t]*[=:][ \t]*)(?P<value>.*)$"
)

# How many timestamped backups to keep per config file.
_MAX_BACKUPS = 20


def _normalize_key(key: str) -> str:
    """Match configparser's key handling (case-insensitive, stripped)."""
    return key.strip().lower()


def backup_config(config_path: str, backup_dir: str | None = None) -> str:
    """Copy ``config_path`` to a timestamped ``.bak`` file and prune old ones.

    Returns the path of the backup that was written.  Raises ``OSError`` on
    failure (callers should surface this to the user).
    """
    config_path = os.fspath(config_path)
    directory = backup_dir or os.path.dirname(os.path.abspath(config_path))
    base = os.path.basename(config_path)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(directory, f"{base}.bak.{timestamp}")

    # copy2 preserves mtime/permissions.
    shutil.copy2(config_path, backup_path)
    _prune_backups(directory, base)
    return backup_path


def _prune_backups(directory: str, base: str) -> None:
    """Keep only the most recent ``_MAX_BACKUPS`` backups for ``base``."""
    prefix = f"{base}.bak."
    try:
        backups = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.startswith(prefix)
        ]
    except OSError:
        return
    # Lexicographic sort works because the timestamp is zero-padded.
    backups.sort()
    for stale in backups[:-_MAX_BACKUPS]:
        try:
            os.remove(stale)
        except OSError:
            pass  # best effort — never block a save on cleanup


def update_ini_values(
    config_path: str,
    updates: dict[str, dict[str, str]],
    deletes: dict[str, list[str] | set[str]] | None = None,
) -> dict:
    """Surgically apply ``updates`` (and optional ``deletes``) to an INI file.

    ``updates`` maps ``{section: {key: value}}``.  ``deletes`` maps
    ``{section: [key, ...]}`` of keys to remove (used by dynamic-list editors
    where rows can be deleted).  Values must already be stringified.  A backup
    is taken before writing and the write is atomic.

    Returns a summary dict::

        {
            "backup_path": str,
            "changed": [(section, key), ...],   # existing keys whose value changed
            "added": [(section, key), ...],     # keys appended to an existing section
            "removed": [(section, key), ...],   # keys deleted
            "created_sections": [section, ...], # brand-new sections appended at EOF
        }

    Limitation: a trailing inline comment (``key = value  ; note``) on a *changed*
    line is dropped, since the whole value portion is replaced.  Full-line
    comments and untouched lines are always preserved.
    """
    config_path = os.fspath(config_path)

    if not updates and not deletes:
        return {"backup_path": "", "changed": [], "added": [], "removed": [], "created_sections": []}

    with open(config_path, encoding="utf-8") as fh:
        lines = fh.readlines()

    # Build a case-insensitive lookup of the pending updates so we can match
    # against configparser-normalized keys while preserving the caller's keys.
    pending: dict[str, dict[str, str]] = {}
    section_case: dict[str, str] = {}  # normalized section -> caller's casing
    for section, kv in (updates or {}).items():
        norm_section = section.strip().lower()
        section_case[norm_section] = section
        pending[norm_section] = {_normalize_key(k): str(v) for k, v in kv.items()}

    # Normalized set of keys to delete, per normalized section.
    delete_norm: dict[str, set[str]] = {}
    for section, keys in (deletes or {}).items():
        norm_section = section.strip().lower()
        delete_norm[norm_section] = {_normalize_key(k) for k in keys}

    changed: list[tuple[str, str]] = []
    added: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    created_sections: list[str] = []

    out: list[str] = []
    current_norm: str | None = None
    # Index in ``out`` marking the end of the current section's body, so we can
    # insert appended keys right before the next section header / EOF.
    section_body_end: dict[str, int] = {}
    # Keys still needing to be written, per normalized section.
    remaining: dict[str, dict[str, str]] = {
        s: dict(kv) for s, kv in pending.items()
    }

    def _record_section_boundary() -> None:
        if current_norm is not None and current_norm in remaining:
            section_body_end[current_norm] = len(out)

    for raw in lines:
        section_match = _SECTION_RE.match(raw)
        if section_match:
            # Leaving the previous section: remember where its body ended.
            _record_section_boundary()
            current_norm = section_match.group("name").strip().lower()
            out.append(raw)
            continue

        # Deletions: drop matching key lines entirely (preserve comments).
        if current_norm in delete_norm:
            key_match = _KEY_RE.match(raw)
            if key_match:
                norm_key = _normalize_key(key_match.group("key"))
                if norm_key in delete_norm[current_norm]:
                    removed.append((current_norm, norm_key))
                    continue

        if current_norm in remaining:
            key_match = _KEY_RE.match(raw)
            if key_match:
                norm_key = _normalize_key(key_match.group("key"))
                sect_updates = remaining[current_norm]
                if norm_key in sect_updates:
                    new_value = sect_updates.pop(norm_key)
                    old_value = key_match.group("value").rstrip("\r\n")
                    line_ending = raw[len(raw.rstrip("\r\n")):] or "\n"
                    if old_value != new_value:
                        changed.append((section_case[current_norm], norm_key))
                    rebuilt = (
                        f"{key_match.group('indent')}{key_match.group('key')}"
                        f"{key_match.group('sep')}{new_value}{line_ending}"
                    )
                    out.append(rebuilt)
                    continue
        out.append(raw)

    # End of file: close out the final section.
    _record_section_boundary()

    # Append keys that belonged to an existing section but weren't found inline.
    # Insert from the highest index first so earlier insertions don't shift the
    # indices we computed for later sections.
    insertions: list[tuple[int, list[str]]] = []
    for norm_section, leftover in remaining.items():
        if not leftover:
            continue
        if norm_section in section_body_end:
            new_lines = [f"{key} = {value}\n" for key, value in leftover.items()]
            for key in leftover:
                added.append((section_case[norm_section], key))
            insertions.append((section_body_end[norm_section], new_lines))

    for index, new_lines in sorted(insertions, key=lambda item: item[0], reverse=True):
        # Ensure the line before the insertion ends with a newline.
        if index > 0 and not out[index - 1].endswith("\n"):
            out[index - 1] = out[index - 1] + "\n"
        out[index:index] = new_lines

    # Append brand-new sections (those never seen in the file) at EOF.
    new_sections = [
        norm for norm, leftover in remaining.items()
        if leftover and norm not in section_body_end
    ]
    if new_sections:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        for norm_section in new_sections:
            if out and out[-1].strip() != "":
                out.append("\n")
            out.append(f"[{section_case[norm_section]}]\n")
            for key, value in remaining[norm_section].items():
                out.append(f"{key} = {value}\n")
                added.append((section_case[norm_section], key))
            created_sections.append(section_case[norm_section])

    # Take the backup *before* replacing the file.
    backup_path = backup_config(config_path)

    # Atomic write: temp file in the same directory, then os.replace.
    directory = os.path.dirname(os.path.abspath(config_path))
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".ini_tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.writelines(out)
        os.replace(tmp_path, config_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    return {
        "backup_path": backup_path,
        "changed": changed,
        "added": added,
        "removed": removed,
        "created_sections": created_sections,
    }
