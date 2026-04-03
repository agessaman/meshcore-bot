#!/usr/bin/env python3
"""
config_tui.py — Interactive ncurses-based configuration editor for meshcore-bot.

Features:
  • Read an existing config.ini or create one from config.ini.example
  • Resize-aware layout — redraws correctly when terminal is resized
  • Browse sections and keys with arrow-key navigation
  • Edit values in-place with a full-featured line editor
  • Help overlay (?) shows description + example lines for the current key
  • Validate config against known required/optional keys (v)
  • Migrate config to newer schema — add missing keys from example (m)
  • Save (s), quit with unsaved-changes guard (q / Esc)

Usage:
    python3 scripts/config_tui.py [config.ini]
    python3 scripts/config_tui.py        # auto-detects config.ini in project root

Navigation:
    ↑ / ↓          move between keys in current section
    ← / →  or Tab  previous / next section
    Enter          edit selected value
    ?              help for current key (description + example from config.ini.example)
    s              save
    v              validate (show errors / warnings)
    m              migrate (add missing keys/sections from example)
    q / Esc        quit (asks to save if unsaved changes)
"""

import configparser
import curses
import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_project_root() -> Path:
    try:
        here = Path(__file__).resolve().parent
        for candidate in [here, here.parent, here.parent.parent]:
            if (candidate / "pyproject.toml").exists() or (candidate / "meshcore_bot.py").exists():
                return candidate
        return here.parent
    except Exception:
        return Path.cwd()


def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(allow_no_value=True, inline_comment_prefixes=None)
    cfg.optionxform = str  # preserve case
    if path.exists():
        try:
            cfg.read(str(path), encoding="utf-8")
        except (configparser.Error, IOError, OSError, UnicodeDecodeError) as e:
            # Return empty config rather than crash; caller may show a warning
            cfg._load_error = str(e)  # type: ignore[attr-defined]
    return cfg


def load_example_comments(example_path: Path) -> Dict[str, Dict[str, str]]:
    """Parse config.ini.example — return {section: {key: comment_text}}."""
    comments: Dict[str, Dict[str, str]] = {}
    if not example_path.exists():
        return comments
    try:
        current_section = ""
        pending: List[str] = []
        with open(example_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    current_section = stripped[1:-1]
                    comments.setdefault(current_section, {})
                    pending = []
                elif stripped.startswith("#"):
                    pending.append(stripped[1:].strip())
                elif "=" in stripped and not stripped.startswith("#"):
                    key = stripped.split("=", 1)[0].strip()
                    if current_section and pending:
                        comments.setdefault(current_section, {})[key] = " ".join(pending)
                    pending = []
                else:
                    pending = []
    except (IOError, OSError, UnicodeDecodeError):
        pass
    return comments


def load_example_lines(example_path: Path) -> Dict[str, Dict[str, str]]:
    """Return {section: {key: 'full raw line from example'}}."""
    lines: Dict[str, Dict[str, str]] = {}
    if not example_path.exists():
        return lines
    try:
        current_section = ""
        with open(example_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    current_section = stripped[1:-1]
                    lines.setdefault(current_section, {})
                elif "=" in stripped and not stripped.startswith("#"):
                    key = stripped.split("=", 1)[0].strip()
                    if current_section:
                        lines.setdefault(current_section, {})[key] = stripped
    except (IOError, OSError, UnicodeDecodeError):
        pass
    return lines


def load_example_keys(example_path: Path) -> Dict[str, Dict[str, str]]:
    """Return {section: {key: default_value}} from config.ini.example."""
    keys: Dict[str, Dict[str, str]] = {}
    if not example_path.exists():
        return keys
    try:
        cfg = configparser.ConfigParser(allow_no_value=True)
        cfg.optionxform = str
        cfg.read(str(example_path), encoding="utf-8")
        for section in cfg.sections():
            try:
                keys[section] = dict(cfg.items(section))
            except configparser.Error:
                keys[section] = {}
    except (configparser.Error, IOError, OSError, UnicodeDecodeError):
        pass
    return keys


def validate_config(
    cfg: configparser.ConfigParser,
    example_keys: Dict[str, Dict[str, str]],
) -> List[Tuple[str, str, str]]:
    """Return list of (severity, section, message) issues."""
    issues: List[Tuple[str, str, str]] = []
    try:
        required = {
            "Connection": {"connection_type"},
            "Bot": {"bot_name"},
        }
        for section, req_keys in required.items():
            if not cfg.has_section(section):
                issues.append(("ERROR", section, f"Section [{section}] is missing"))
                continue
            for key in req_keys:
                if not cfg.has_option(section, key):
                    issues.append(("ERROR", section, f"Required key '{key}' is missing"))
        for section in cfg.sections():
            if section not in example_keys:
                continue
            try:
                section_opts = cfg.options(section)
            except configparser.Error:
                continue
            for key in section_opts:
                if key not in example_keys.get(section, {}):
                    issues.append(("WARNING", section, f"Unknown key '{key}' (not in example)"))
        for section in example_keys:
            if not cfg.has_section(section):
                issues.append(("INFO", section, f"Optional section [{section}] not configured"))
    except Exception as e:
        issues.append(("ERROR", "", f"Validation error: {e}"))
    if not issues:
        issues.append(("INFO", "", "Config looks good — no errors found"))
    return issues


def migrate_config(
    cfg: configparser.ConfigParser,
    example_keys: Dict[str, Dict[str, str]],
    example_comments: Dict[str, Dict[str, str]],
) -> List[str]:
    changes: List[str] = []
    for section, keys in example_keys.items():
        if not cfg.has_section(section):
            try:
                cfg.add_section(section)
                changes.append(f"Added section [{section}]")
            except configparser.DuplicateSectionError:
                pass
            except configparser.Error as e:
                changes.append(f"Error adding [{section}]: {e}")
                continue
        for key, default in keys.items():
            if not cfg.has_option(section, key):
                try:
                    cfg.set(section, key, default or "")
                    changes.append(f"Added [{section}] {key} = {default!r}")
                except configparser.Error as e:
                    changes.append(f"Error setting [{section}] {key}: {e}")
    return changes


def save_config(cfg: configparser.ConfigParser, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except (IOError, OSError, PermissionError) as e:
        raise IOError(f"Cannot create directory {path.parent}: {e}") from e
    try:
        with open(path, "w", encoding="utf-8") as fh:
            cfg.write(fh)
    except (IOError, OSError, PermissionError) as e:
        raise IOError(f"Cannot write {path}: {e}") from e


# ---------------------------------------------------------------------------
# TUI state
# ---------------------------------------------------------------------------

class TUIState:
    def __init__(
        self,
        cfg: configparser.ConfigParser,
        path: Path,
        example_keys: Dict[str, Dict[str, str]],
        example_comments: Dict[str, Dict[str, str]],
        example_lines: Dict[str, Dict[str, str]],
    ) -> None:
        self.cfg = cfg
        self.path = path
        self.example_keys = example_keys
        self.example_comments = example_comments
        self.example_lines = example_lines
        self.dirty = False
        self.sections: List[str] = []
        try:
            self.sections = cfg.sections()
        except Exception:
            self.sections = []
        self.section_idx = 0
        self.key_idx = 0
        self.focus: str = "sections"  # "sections" or "keys"
        self.status_msg = ""
        self.status_attr = 0

    def current_section(self) -> str:
        if not self.sections or self.section_idx >= len(self.sections):
            return ""
        return self.sections[self.section_idx]

    def current_keys(self) -> List[str]:
        s = self.current_section()
        if not s:
            return []
        try:
            if self.cfg.has_section(s):
                return list(self.cfg.options(s))
        except configparser.Error:
            pass
        return []

    def clamp(self) -> None:
        if self.sections:
            self.section_idx = max(0, min(self.section_idx, len(self.sections) - 1))
        else:
            self.section_idx = 0
        keys = self.current_keys()
        if keys:
            self.key_idx = max(0, min(self.key_idx, len(keys) - 1))
        else:
            self.key_idx = 0

    def set_status(self, msg: str, error: bool = False) -> None:
        self.status_msg = msg
        try:
            self.status_attr = curses.color_pair(3) if error else curses.color_pair(2)
        except Exception:
            self.status_attr = 0


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _safe_addstr(win, row: int, col: int, text: str, attr: int = 0) -> None:
    """addstr that silently ignores out-of-bounds writes."""
    try:
        h, w = win.getmaxyx()
        if row < 0 or row >= h or col < 0 or col >= w:
            return
        available = w - col - 1
        if available <= 0:
            return
        if attr:
            win.addstr(row, col, text[:available], attr)
        else:
            win.addstr(row, col, text[:available])
    except curses.error:
        pass
    except Exception:
        pass


def _color(pair: int, fallback: int = 0) -> int:
    """Return color_pair(pair), falling back to fallback on terminals without color."""
    try:
        return curses.color_pair(pair)
    except Exception:
        return fallback


def draw_header(win, path: Path, dirty: bool) -> None:
    try:
        h, w = win.getmaxyx()
        flag = " [modified]" if dirty else ""
        header = f"  meshcore-bot config — {path.name}{flag}"
        _safe_addstr(win, 0, 0, header.ljust(w - 1), _color(1) | curses.A_BOLD)
    except Exception:
        pass


def draw_footer(win, state: TUIState) -> None:
    try:
        h, w = win.getmaxyx()
        if state.status_msg and h > 2:
            _safe_addstr(win, h - 2, 0, f" {state.status_msg} ".ljust(w - 1), state.status_attr)
        if state.focus == "sections":
            help_line = "  ↑↓:section  Tab/→:keys pane  PgUp/Dn:cycle sections  Enter:select  s:save  q:quit"
        else:
            help_line = "  ↑↓:key  Tab/←:sections  Enter:edit  r:rename  a:add  d:delete  ?:help  s:save  v:validate  m:migrate  q:quit"
        if h > 1:
            _safe_addstr(win, h - 1, 0, help_line.ljust(w - 1), _color(1))
    except Exception:
        pass


def draw_sections_pane(win, state: TUIState, top: int, left: int, height: int, width: int, scroll: int) -> int:
    """Draw sections list. Returns updated scroll offset."""
    try:
        if state.section_idx < scroll:
            scroll = state.section_idx
        if state.section_idx >= scroll + height:
            scroll = state.section_idx - height + 1

        focused = (state.focus == "sections")
        for i, section in enumerate(state.sections):
            vis = i - scroll
            if vis < 0 or vis >= height:
                continue
            row = top + vis
            label = f" [{section}] "
            if i == state.section_idx:
                if focused:
                    _safe_addstr(win, row, left, label.ljust(width), _color(4) | curses.A_BOLD)
                else:
                    # Dimmer selection indicator when pane is not focused
                    _safe_addstr(win, row, left, label.ljust(width), curses.A_BOLD)
            else:
                _safe_addstr(win, row, left, label.ljust(width))
    except Exception:
        pass
    return scroll


def draw_keys_pane(
    win, state: TUIState, top: int, left: int, height: int, width: int, scroll: int
) -> int:
    """Draw keys pane. Returns updated scroll offset."""
    try:
        keys = state.current_keys()
        if not keys:
            _safe_addstr(win, top, left, " (no keys) ")
            return 0

        if state.key_idx < scroll:
            scroll = state.key_idx
        if state.key_idx >= scroll + height:
            scroll = state.key_idx - height + 1

        section = state.current_section()
        ex_sec = state.example_keys.get(section, {})
        # Dynamic sections (no fixed keys in example) — never flag keys as unknown
        dynamic_section = section in state.example_keys and not ex_sec

        for i, key in enumerate(keys):
            vis = i - scroll
            if vis < 0 or vis >= height:
                continue
            row = top + vis
            try:
                val = state.cfg.get(section, key, fallback="")
            except configparser.Error:
                val = "(error)"
            in_example = dynamic_section or key in ex_sec
            marker = " " if in_example else "?"
            line = f"{marker} {key} = {val}"

            if i == state.key_idx:
                _safe_addstr(win, row, left, line.ljust(width), _color(5) | curses.A_BOLD)
            elif not in_example:
                _safe_addstr(win, row, left, line, _color(3))
            else:
                _safe_addstr(win, row, left, line)
    except Exception:
        pass
    return scroll


def draw_status_bar(win, state: TUIState, top: int, left: int, width: int, attr: int = 0) -> None:
    """Show section count / key count in the keys pane title bar."""
    try:
        section = state.current_section()
        keys = state.current_keys()
        n_sec = len(state.sections)
        n_key = len(keys)
        focus_marker = ">" if state.focus == "keys" else " "
        title = f"{focus_marker}[{section}]  key {state.key_idx + 1}/{n_key}  (section {state.section_idx + 1}/{n_sec}) "
        _safe_addstr(win, top, left, title.ljust(width), attr or _color(1))
    except Exception:
        pass


def draw_hint_line(win, state: TUIState, top: int, left: int, width: int) -> None:
    """Show a one-line hint for the selected key below the keys pane."""
    try:
        section = state.current_section()
        keys = state.current_keys()
        if not keys or state.key_idx >= len(keys):
            return
        key = keys[state.key_idx]
        comment = state.example_comments.get(section, {}).get(key, "")
        max_w = max(4, width - 3)
        if comment and len(comment) > max_w:
            hint = textwrap.shorten(comment, max_w, placeholder="…")
        elif comment:
            hint = comment
        else:
            hint = "(no description — press ? for full help)"
        _safe_addstr(win, top, left, f"  {hint}", _color(2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Overlays
# ---------------------------------------------------------------------------

def show_overlay(stdscr, title: str, items: List[Tuple[str, str, str]]) -> None:
    """Generic scrollable list overlay. Items: (tag, section, message)."""
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scroll = 0
    while True:
        try:
            h, w = stdscr.getmaxyx()
        except curses.error:
            return

        if h < 6 or w < 20:
            try:
                stdscr.getch()
            except curses.error:
                pass
            return

        oh = max(4, min(h - 4, max(10, len(items) + 4)))
        ow = max(10, min(w - 4, 92))
        dy = max(0, (h - oh) // 2)
        dx = max(0, (w - ow) // 2)

        # Ensure window fits within screen
        if dy + oh > h:
            oh = h - dy
        if dx + ow > w:
            ow = w - dx
        if oh < 4 or ow < 10:
            try:
                stdscr.getch()
            except curses.error:
                pass
            return

        try:
            win = curses.newwin(oh, ow, dy, dx)
        except curses.error:
            try:
                stdscr.getch()
            except curses.error:
                pass
            return

        try:
            win.keypad(True)
            win.clear()
            try:
                win.border()
            except curses.error:
                pass
            _safe_addstr(win, 0, 2, f" {title} (↑↓:scroll  q/Esc/Enter:close) ", _color(1))
            visible = max(1, oh - 2)
            for i, (tag, sec, msg) in enumerate(items):
                vis = i - scroll
                if vis < 0 or vis >= visible:
                    continue
                row = 1 + vis
                if tag == "ERROR":
                    attr = _color(3)
                elif tag == "WARNING":
                    attr = _color(6)
                else:
                    attr = _color(2)
                label = f" [{tag}] {sec}: {msg}" if sec else f" [{tag}] {msg}"
                _safe_addstr(win, row, 1, label, attr)
            win.refresh()
        except curses.error:
            pass

        try:
            ch = win.getch()
        except curses.error:
            ch = -1

        try:
            del win
            stdscr.touchwin()
        except curses.error:
            pass

        if ch == curses.KEY_RESIZE:
            scroll = 0
            continue
        elif ch in (ord("q"), ord("Q"), 27, 10, 13, curses.KEY_ENTER):
            break
        elif ch == curses.KEY_DOWN:
            scroll = min(scroll + 1, max(0, len(items) - visible))
        elif ch == curses.KEY_UP:
            scroll = max(0, scroll - 1)
        elif ch == curses.KEY_NPAGE:
            scroll = min(scroll + visible, max(0, len(items) - visible))
        elif ch == curses.KEY_PPAGE:
            scroll = max(0, scroll - visible)

    try:
        stdscr.touchwin()
    except curses.error:
        pass


def show_key_help(stdscr, state: TUIState) -> None:
    """Show full help for the currently selected key."""
    try:
        section = state.current_section()
        keys = state.current_keys()
        if not keys or state.key_idx >= len(keys):
            return
        key = keys[state.key_idx]
        comment = state.example_comments.get(section, {}).get(key, "(no description available)")
        example = state.example_lines.get(section, {}).get(key, "")
        try:
            current_val = state.cfg.get(section, key, fallback="")
        except configparser.Error:
            current_val = "(error reading value)"
        default_val = state.example_keys.get(section, {}).get(key, "")

        lines: List[Tuple[str, str, str]] = [
            ("INFO", "Section", section),
            ("INFO", "Key", key),
            ("INFO", "Current value", repr(current_val)),
            ("INFO", "Default (example)", repr(default_val)),
            ("INFO", "", ""),
            ("INFO", "Description", ""),
        ]
        for para in textwrap.wrap(comment, 72) or ["(none)"]:
            lines.append(("INFO", "", f"  {para}"))
        if example:
            lines.append(("INFO", "", ""))
            lines.append(("INFO", "Example line", ""))
            lines.append(("INFO", "", f"  {example}"))

        show_overlay(stdscr, f"Help: [{section}] {key}", lines)
    except Exception as e:
        state.set_status(f"Help error: {e}", error=True)


# ---------------------------------------------------------------------------
# Inline editor
# ---------------------------------------------------------------------------

_EDIT_VALUE_DEPTH = 0
_EDIT_VALUE_MAX_DEPTH = 20


def edit_value(stdscr, prompt: str, current: str) -> Optional[str]:
    """One-line editor dialog. Returns new value or None if cancelled."""
    global _EDIT_VALUE_DEPTH
    _EDIT_VALUE_DEPTH += 1
    if _EDIT_VALUE_DEPTH > _EDIT_VALUE_MAX_DEPTH:
        _EDIT_VALUE_DEPTH -= 1
        return None

    try:
        return _edit_value_impl(stdscr, prompt, current)
    finally:
        _EDIT_VALUE_DEPTH -= 1


def _edit_value_impl(stdscr, prompt: str, current: str) -> Optional[str]:
    while True:
        try:
            h, w = stdscr.getmaxyx()
        except curses.error:
            return None

        dw = max(30, min(w - 4, 84))
        dh = 6
        dy = max(0, (h - dh) // 2)
        dx = max(0, (w - dw) // 2)

        if dy + dh > h or dx + dw > w or dh < 4 or dw < 10:
            return None

        try:
            win = curses.newwin(dh, dw, dy, dx)
        except curses.error:
            return None

        try:
            win.keypad(True)
        except curses.error:
            pass

        try:
            try:
                win.border()
            except curses.error:
                pass
            _safe_addstr(win, 0, 2, " Edit Value ", _color(1) | curses.A_BOLD)
            _safe_addstr(win, 1, 2, prompt[:dw - 4])
            _safe_addstr(win, 2, 2, "─" * max(0, dw - 4))
            _safe_addstr(win, 4, 2, "Enter=confirm   Esc=cancel   Ctrl-A=home   Ctrl-E=end   Ctrl-K=clear", _color(2))
            win.refresh()
        except curses.error:
            pass

        buf = list(current)
        pos = len(buf)
        inner_w = max(1, dw - 4)
        result: Optional[str] = None

        try:
            curses.curs_set(1)
        except curses.error:
            pass

        def redraw_input() -> None:
            try:
                win.move(3, 2)
                win.clrtoeol()
                start = max(0, pos - inner_w + 1)
                display = "".join(buf)[start:start + inner_w]
                cursor_col = pos - start
                try:
                    win.addstr(3, 2, display)
                    win.move(3, 2 + cursor_col)
                except curses.error:
                    pass
                win.refresh()
            except curses.error:
                pass

        try:
            redraw_input()
        except Exception:
            pass

        done = False
        resize_requested = False
        while not done:
            try:
                ch = win.getch()
            except curses.error:
                done = True
                break

            if ch == curses.KEY_RESIZE:
                resize_requested = True
                done = True
            elif ch in (curses.KEY_ENTER, 10, 13):
                result = "".join(buf)
                done = True
            elif ch == 27:  # Esc
                done = True
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if pos > 0:
                    buf.pop(pos - 1)
                    pos -= 1
            elif ch == curses.KEY_DC:
                if pos < len(buf):
                    buf.pop(pos)
            elif ch == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif ch == curses.KEY_RIGHT:
                pos = min(len(buf), pos + 1)
            elif ch == curses.KEY_HOME or ch == 1:  # Ctrl-A
                pos = 0
            elif ch == curses.KEY_END or ch == 5:  # Ctrl-E
                pos = len(buf)
            elif ch == 11:  # Ctrl-K — clear to end
                buf = buf[:pos]
            elif ch == 21:  # Ctrl-U — clear whole line
                buf = []
                pos = 0
            elif 32 <= ch < 256:
                try:
                    buf.insert(pos, chr(ch))
                    pos += 1
                except Exception:
                    pass
            if not done:
                try:
                    redraw_input()
                except Exception:
                    pass

        try:
            curses.curs_set(0)
        except curses.error:
            pass
        try:
            del win
            stdscr.touchwin()
            stdscr.refresh()
        except curses.error:
            pass

        if resize_requested:
            # Terminal resized during edit — restart dialog preserving buffer
            return edit_value(stdscr, prompt, "".join(buf))

        return result


# ---------------------------------------------------------------------------
# Key management helpers (add / rename / delete)
# ---------------------------------------------------------------------------

def action_add_key(stdscr, state: TUIState) -> None:
    """Prompt for a new key name and value, then add it to the current section."""
    section = state.current_section()
    if not section:
        return
    new_key = edit_value(stdscr, f"[{section}]  New key name", "")
    if not new_key:
        state.set_status("Add cancelled — no key name entered")
        return
    new_key = new_key.strip()
    if not new_key:
        state.set_status("Add cancelled — key name was blank")
        return
    if state.cfg.has_option(section, new_key):
        state.set_status(f"Key '{new_key}' already exists", error=True)
        return
    new_val = edit_value(stdscr, f"[{section}]  {new_key}  (value)", "")
    if new_val is None:
        state.set_status("Add cancelled")
        return
    try:
        state.cfg.set(section, new_key, new_val)
        state.dirty = True
        # Move selection to the newly added key
        keys = list(state.cfg.options(section))
        try:
            state.key_idx = keys.index(new_key)
        except ValueError:
            pass
        state.set_status(f"Added [{section}] {new_key}")
    except configparser.Error as e:
        state.set_status(f"Cannot add key: {e}", error=True)


def action_rename_key(stdscr, state: TUIState) -> None:
    """Rename the currently selected key (useful to change a scheduled-message time)."""
    section = state.current_section()
    keys = state.current_keys()
    if not keys or state.key_idx >= len(keys):
        return
    old_key = keys[state.key_idx]
    new_key = edit_value(stdscr, f"[{section}]  Rename key (was: {old_key})", old_key)
    if new_key is None:
        state.set_status("Rename cancelled")
        return
    new_key = new_key.strip()
    if not new_key or new_key == old_key:
        state.set_status("Rename cancelled — unchanged")
        return
    if state.cfg.has_option(section, new_key):
        state.set_status(f"Key '{new_key}' already exists", error=True)
        return
    try:
        old_val = state.cfg.get(section, old_key, fallback="")
    except configparser.Error:
        old_val = ""
    try:
        state.cfg.remove_option(section, old_key)
        state.cfg.set(section, new_key, old_val)
        state.dirty = True
        # Re-select the renamed key
        new_keys = list(state.cfg.options(section))
        try:
            state.key_idx = new_keys.index(new_key)
        except ValueError:
            state.key_idx = max(0, min(state.key_idx, len(new_keys) - 1))
        state.set_status(f"Renamed '{old_key}' → '{new_key}'")
    except configparser.Error as e:
        state.set_status(f"Cannot rename key: {e}", error=True)


def action_delete_key(stdscr, state: TUIState) -> None:
    """Delete the currently selected key after confirmation."""
    section = state.current_section()
    keys = state.current_keys()
    if not keys or state.key_idx >= len(keys):
        return
    key = keys[state.key_idx]
    if not confirm(stdscr, f"Delete [{section}] {key}?"):
        state.set_status("Delete cancelled")
        return
    try:
        state.cfg.remove_option(section, key)
        state.dirty = True
        new_keys = state.current_keys()
        state.key_idx = max(0, min(state.key_idx, len(new_keys) - 1))
        state.set_status(f"Deleted [{section}] {key}")
    except configparser.Error as e:
        state.set_status(f"Cannot delete key: {e}", error=True)


# ---------------------------------------------------------------------------
# Confirmation dialog
# ---------------------------------------------------------------------------

def confirm(stdscr, question: str) -> bool:
    """Y/N prompt. Returns True if user presses y/Y."""
    try:
        h, w = stdscr.getmaxyx()
        dw = max(20, min(w - 4, 60))
        dh = 4
        dy = max(0, (h - dh) // 2)
        dx = max(0, (w - dw) // 2)

        if dy + dh > h or dx + dw > w:
            return False

        try:
            win = curses.newwin(dh, dw, dy, dx)
        except curses.error:
            return False

        try:
            try:
                win.border()
            except curses.error:
                pass
            _safe_addstr(win, 1, 2, question[:dw - 4], _color(6) | curses.A_BOLD)
            _safe_addstr(win, 2, 2, "Press Y to confirm, any other key to cancel.", _color(2))
            win.refresh()
            ch = win.getch()
        except curses.error:
            ch = -1
        finally:
            try:
                del win
                stdscr.touchwin()
                stdscr.refresh()
            except curses.error:
                pass

        return ch in (ord("y"), ord("Y"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main TUI loop
# ---------------------------------------------------------------------------

def tui_main(stdscr, state: TUIState) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass

    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)    # header / footer / titles
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_GREEN)   # ok / hint / status
        curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_RED)     # error / unknown key
        curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_CYAN)    # selected section
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_YELLOW)  # selected key
        curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_YELLOW)  # warning / confirm
    except curses.error:
        pass  # monochrome terminal — continue without color

    try:
        stdscr.keypad(True)
    except curses.error:
        pass

    # Per-pane scroll offsets
    sec_scroll = 0
    key_scroll = 0

    while True:
        try:
            stdscr.clear()
            h, w = stdscr.getmaxyx()
        except curses.error:
            continue

        if h < 12 or w < 50:
            _safe_addstr(stdscr, 0, 0, "Terminal too small — resize and press any key")
            try:
                stdscr.refresh()
                stdscr.getch()
            except curses.error:
                pass
            continue

        # ── Layout ──────────────────────────────────────────────────────────
        #  Row 0            : header
        #  Row 1            : section pane title | keys pane title bar
        #  Rows 2..body_h+1 : section list | keys list  (body_h rows)
        #  body_h+2         : hint line
        #  h-2              : status message
        #  h-1              : key bindings footer
        # Total: 1+1+body_h+1+1+1 = body_h+5 = h  →  body_h = h-5

        section_w = max(16, min(26, w // 5))
        divider = section_w          # column of the vertical divider
        keys_left = divider + 1
        keys_w = max(1, w - keys_left - 1)

        body_h = max(1, h - 5)       # rows of pane content
        hint_row = body_h + 2        # one row below the last pane row

        # ── Draw phase — wrapped so a single bad render doesn't abort ───────
        try:
            draw_header(stdscr, state.path, state.dirty)
            sec_title_attr = (_color(1) | curses.A_BOLD) if state.focus == "sections" else _color(1)
            keys_title_attr = (_color(1) | curses.A_BOLD) if state.focus == "keys" else _color(1)
            focus_marker = ">" if state.focus == "sections" else " "
            _safe_addstr(stdscr, 1, 0, f"{focus_marker}Sections ({len(state.sections)}) ".ljust(section_w), sec_title_attr)
            draw_status_bar(stdscr, state, 1, keys_left, keys_w, keys_title_attr)

            for row in range(2, body_h + 2):
                _safe_addstr(stdscr, row, divider, "│")

            sec_scroll = draw_sections_pane(stdscr, state, 2, 0, body_h, section_w, sec_scroll)
            key_scroll = draw_keys_pane(stdscr, state, 2, keys_left, body_h, keys_w, key_scroll)
            draw_hint_line(stdscr, state, hint_row, 0, w)
            draw_footer(stdscr, state)
        except curses.error:
            pass
        except Exception:
            pass

        try:
            stdscr.refresh()
        except curses.error:
            pass

        # ── Input ─────────────────────────────────────────────────────────────
        try:
            ch = stdscr.getch()
        except curses.error:
            continue
        except KeyboardInterrupt:
            if state.dirty:
                try:
                    if confirm(stdscr, "Unsaved changes — quit without saving?"):
                        break
                except Exception:
                    break
            else:
                break
            continue

        if ch == curses.KEY_RESIZE:
            key_scroll = 0
            sec_scroll = 0
            state.status_msg = ""

        # ── Tab: toggle focus between panes ─────────────────────────────────
        elif ch in (ord("\t"), curses.KEY_BTAB):
            if ch == curses.KEY_BTAB or state.focus == "keys":
                state.focus = "sections"
            else:
                state.focus = "keys"
            state.status_msg = ""

        # ── Arrow up/down: navigate within the focused pane ─────────────────
        elif ch == curses.KEY_UP:
            if state.focus == "sections":
                state.section_idx = max(0, state.section_idx - 1)
                state.key_idx = 0
                key_scroll = 0
            else:
                state.key_idx = max(0, state.key_idx - 1)
            state.status_msg = ""

        elif ch == curses.KEY_DOWN:
            if state.focus == "sections":
                state.section_idx = min(max(0, len(state.sections) - 1), state.section_idx + 1)
                state.key_idx = 0
                key_scroll = 0
            else:
                keys = state.current_keys()
                if keys:
                    state.key_idx = min(len(keys) - 1, state.key_idx + 1)
            state.status_msg = ""

        # ── Left: from keys pane → focus sections pane ──────────────────────
        elif ch == curses.KEY_LEFT:
            if state.focus == "keys":
                state.focus = "sections"
            else:
                # Already in sections pane — cycle to previous section
                state.section_idx = (state.section_idx - 1) % max(1, len(state.sections))
                state.key_idx = 0
                key_scroll = 0
            state.status_msg = ""

        # ── Right: from sections pane → focus keys pane ─────────────────────
        elif ch == curses.KEY_RIGHT:
            if state.focus == "sections":
                state.focus = "keys"
            else:
                # Already in keys pane — cycle to next section
                state.section_idx = (state.section_idx + 1) % max(1, len(state.sections))
                state.key_idx = 0
                key_scroll = 0
            state.status_msg = ""

        # ── PgUp/PgDn: cycle sections from either pane ──────────────────────
        elif ch == curses.KEY_PPAGE:
            state.section_idx = (state.section_idx - 1) % max(1, len(state.sections))
            state.key_idx = 0
            key_scroll = 0
            state.status_msg = ""

        elif ch == curses.KEY_NPAGE:
            state.section_idx = (state.section_idx + 1) % max(1, len(state.sections))
            state.key_idx = 0
            key_scroll = 0
            state.status_msg = ""

        elif ch in (curses.KEY_HOME,):
            state.section_idx = 0
            state.key_idx = 0
            key_scroll = sec_scroll = 0

        # ── Enter: on sections pane → move focus to keys; on keys → edit ────
        elif ch in (curses.KEY_ENTER, 10, 13):
            if state.focus == "sections":
                state.focus = "keys"
                state.status_msg = ""
            else:
                try:
                    section = state.current_section()
                    keys = state.current_keys()
                    if keys and state.key_idx < len(keys):
                        key = keys[state.key_idx]
                        try:
                            current_val = state.cfg.get(section, key, fallback="")
                        except configparser.Error:
                            current_val = ""
                        new_val = edit_value(stdscr, f"[{section}]  {key}", current_val)
                        if new_val is not None and new_val != current_val:
                            try:
                                state.cfg.set(section, key, new_val)
                                state.dirty = True
                                state.set_status(f"Updated [{section}] {key}")
                            except configparser.Error as e:
                                state.set_status(f"Cannot set value: {e}", error=True)
                except Exception as e:
                    state.set_status(f"Edit error: {e}", error=True)

        elif ch in (ord("?"),):
            try:
                show_key_help(stdscr, state)
            except Exception as e:
                state.set_status(f"Help error: {e}", error=True)

        elif ch in (ord("a"), ord("A")):
            if state.focus == "keys":
                try:
                    action_add_key(stdscr, state)
                except Exception as e:
                    state.set_status(f"Add error: {e}", error=True)

        elif ch in (ord("r"), ord("R")):
            if state.focus == "keys":
                try:
                    action_rename_key(stdscr, state)
                except Exception as e:
                    state.set_status(f"Rename error: {e}", error=True)

        elif ch in (ord("d"), ord("D"), curses.KEY_DC):
            if state.focus == "keys":
                try:
                    action_delete_key(stdscr, state)
                except Exception as e:
                    state.set_status(f"Delete error: {e}", error=True)

        elif ch in (ord("s"), ord("S")):
            try:
                save_config(state.cfg, state.path)
                state.dirty = False
                state.set_status(f"Saved → {state.path}")
            except Exception as e:
                state.set_status(f"Save failed: {e}", error=True)

        elif ch in (ord("v"), ord("V")):
            try:
                issues = validate_config(state.cfg, state.example_keys)
                show_overlay(stdscr, "Validation Results", issues)
            except Exception as e:
                state.set_status(f"Validate error: {e}", error=True)

        elif ch in (ord("m"), ord("M")):
            try:
                changes = migrate_config(state.cfg, state.example_keys, state.example_comments)
                try:
                    state.sections = state.cfg.sections()
                except Exception:
                    pass
                if changes:
                    state.dirty = True
                    show_overlay(
                        stdscr,
                        f"Migration — {len(changes)} change(s)",
                        [("INFO", "", c) for c in changes],
                    )
                    state.set_status(f"Migrated {len(changes)} item(s) — press s to save")
                else:
                    state.set_status("Config is up-to-date — nothing to migrate")
            except Exception as e:
                state.set_status(f"Migrate error: {e}", error=True)

        elif ch in (ord("q"), ord("Q"), 27):
            try:
                if state.dirty:
                    if confirm(stdscr, "Unsaved changes — quit without saving?"):
                        break
                else:
                    break
            except Exception:
                break

        try:
            state.clamp()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        project_root = find_project_root()
    except Exception:
        project_root = Path.cwd()

    config_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else project_root / "config.ini"
    example_path = project_root / "config.ini.example"

    if not config_path.exists():
        if not example_path.exists():
            print(
                f"ERROR: Neither {config_path} nor {example_path} found.\n"
                "Run this from the meshcore-bot project root.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            import shutil
            print(f"config.ini not found — creating from {example_path.name}…")
            shutil.copy(str(example_path), str(config_path))
            print(f"Created {config_path}")
        except (IOError, OSError, shutil.Error) as e:
            print(f"ERROR: Could not create config from example: {e}", file=sys.stderr)
            sys.exit(1)

    try:
        cfg = load_config(config_path)
    except Exception as e:
        print(f"ERROR: Could not load config: {e}", file=sys.stderr)
        sys.exit(1)

    # Warn if the config had a parse error but continue with whatever was loaded
    load_error = getattr(cfg, "_load_error", None)
    if load_error:
        print(f"WARNING: Config parse error (partial load): {load_error}", file=sys.stderr)

    try:
        example_keys = load_example_keys(example_path)
        example_comments = load_example_comments(example_path)
        example_lines = load_example_lines(example_path)
    except Exception as e:
        print(f"WARNING: Could not load example file ({e}); help/migrate features disabled.", file=sys.stderr)
        example_keys = {}
        example_comments = {}
        example_lines = {}

    try:
        state = TUIState(cfg, config_path, example_keys, example_comments, example_lines)
    except Exception as e:
        print(f"ERROR: Could not initialise TUI state: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        curses.wrapper(tui_main, state)
    except KeyboardInterrupt:
        pass
    except curses.error as e:
        print(f"ERROR: Terminal error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)

    print("Exited config editor.")
    if state.dirty:
        try:
            answer = input("Unsaved changes. Save now? [y/N] ").strip().lower()
            if answer in ("y", "yes"):
                save_config(state.cfg, config_path)
                print(f"Saved to {config_path}")
        except (KeyboardInterrupt, EOFError):
            print()  # clean newline
        except Exception as e:
            print(f"Save failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
