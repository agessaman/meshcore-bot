"""
Shared feed item formatting and sorting.

Used by FeedManager (live posts) and the web viewer preview so output stays consistent.
Filter evaluation lives in feed_filter_eval; this module owns placeholders, shortening,
timestamps, and sort.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from modules.feed_filter_eval import get_nested_value, parse_microsoft_date
from modules.security_utils import sanitize_input
from modules.url_shortener import _coerce_url_string, shorten_url_sync


def format_relative_timestamp(published: datetime | None) -> str:
    """Format a timestamp as a relative time string (e.g. "5m ago")."""
    if not published:
        return ""

    try:
        now = datetime.now(timezone.utc) if published.tzinfo else datetime.now()

        diff = now - published
        minutes = int(diff.total_seconds() / 60)

        if minutes < 1:
            return "now"
        if minutes < 60:
            return f"{minutes}m ago"
        if minutes < 1440:
            hours = minutes // 60
            mins = minutes % 60
            return f"{hours}h {mins}m ago"
        days = minutes // 1440
        return f"{days}d ago"
    except Exception:
        return ""


def feed_format_auto_slots(format_str: str) -> list[tuple[int, int, str]]:
    """Return (start, end, field_name) for each {field|auto} placeholder (left-to-right)."""
    slots: list[tuple[int, int, str]] = []
    for m in re.finditer(r"\{([^}]+)\}", format_str):
        content = m.group(1)
        if "|" not in content:
            continue
        field_name, function = content.split("|", 1)
        if function.strip() == "auto":
            slots.append((m.start(), m.end(), field_name.strip()))
    return slots


def truncate_to_budget(text: str, budget: int) -> str:
    """Fit text to at most budget characters; ellipsis when budget > 3 (same idea as truncate:N)."""
    if budget <= 0:
        return ""
    if not text:
        return ""
    if len(text) <= budget:
        return text
    if budget > 3:
        return text[: budget - 3] + "..."
    return text[:budget]


def feed_format_auto_base_value(
    field_name: str,
    raw_data: Any,
    replacements: dict[str, str],
    link_original: str,
) -> str:
    """Full string for one field before |auto (long link, no shorten)."""
    if field_name.startswith("raw."):
        value = get_nested_value(raw_data, field_name[4:], "")
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value)
            except Exception:
                return str(value)
        return str(value)
    if field_name == "link":
        return link_original or ""
    return str(replacements.get(field_name, "") or "")


def clean_feed_html_body(body: str) -> str:
    """Strip HTML tags and normalize whitespace in a feed body/description."""
    if not body:
        return ""
    body = html.unescape(body)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</p>", "\n\n", body, flags=re.IGNORECASE)
    body = re.sub(r"<p[^>]*>", "", body, flags=re.IGNORECASE)
    body = re.sub(r"<[^>]+>", "", body)
    body = re.sub(r"\n\s*\n\s*\n+", "\n\n", body)
    lines = body.split("\n")
    body = "\n".join(" ".join(line.split()) for line in lines)
    return body.strip()


def _canonical_shorten_name(function: str) -> str:
    """Accept the response_template spelling ``shorten_url`` for ``shorten``.

    Feed formats and response templates are one operator-facing DSL as far as anyone
    configuring the bot is concerned; the same operation answering to a different
    name in each is a standing source of config mistakes.
    """
    if function == "shorten_url":
        return "shorten"
    if function.startswith("shorten_url|"):
        return "shorten|" + function.split("|", 1)[1]
    return function


def apply_feed_field_function(
    text: str,
    function: str,
    *,
    config: Any | None = None,
    logger: Any | None = None,
) -> str:
    """Apply a shortening, parsing, or conditional function to text.

    Supported functions:
    - shorten (alias shorten_url) - URL-shorten via [External_Data] short_url_website
    - shorten|truncate:N (etc.) - shorten first, then apply the rest
    - truncate:N / truncate_hard:N / substr:N[,M] / word_wrap:N / first_words:N
    - regex:… / if_regex:… / switch:… / regex_cond:…
    """
    if not function or not str(function).strip():
        return text or ""
    function = str(function).strip()
    function = _canonical_shorten_name(function)

    def _debug(msg: str) -> None:
        if logger is not None:
            logger.debug(msg)

    if function == "shorten":
        if not text:
            return ""
        out = shorten_url_sync(text, config=config, logger=logger)
        return out if out else text

    if function.startswith("shorten|"):
        if not text:
            return ""
        out = shorten_url_sync(text, config=config, logger=logger)
        base = out if out else text
        rest = function.split("|", 1)[1].strip()
        return apply_feed_field_function(base, rest, config=config, logger=logger)

    if not text:
        return ""

    if function.startswith("truncate:"):
        try:
            max_len = int(function.split(":", 1)[1])
            if len(text) <= max_len:
                return text
            return text[:max_len] + "..."
        except (ValueError, IndexError):
            return text

    if function.startswith("truncate_hard:"):
        try:
            max_len = int(function.split(":", 1)[1])
            if len(text) <= max_len:
                return text
            return text[:max_len]
        except (ValueError, IndexError):
            return text

    if function.startswith("substr:"):
        try:
            args = function.split(":", 1)[1].split(",")
            start = int(args[0])
            if len(args) > 1 and args[1].strip() != "":
                length = int(args[1])
                return text[start : start + length]
            return text[start:]
        except (ValueError, IndexError):
            return text

    if function.startswith("word_wrap:"):
        try:
            max_len = int(function.split(":", 1)[1])
            if len(text) <= max_len:
                return text
            truncated = text[:max_len]
            last_space = truncated.rfind(" ")
            if last_space > max_len * 0.7:
                return truncated[:last_space] + "..."
            return truncated + "..."
        except (ValueError, IndexError):
            return text

    if function.startswith("first_words:"):
        try:
            num_words = int(function.split(":", 1)[1])
            words = text.split()
            if len(words) <= num_words:
                return text
            return " ".join(words[:num_words]) + "..."
        except (ValueError, IndexError):
            return text

    if function.startswith("regex:"):
        try:
            remaining = function[6:]
            last_colon_idx = remaining.rfind(":")
            pattern = remaining
            group_num = None

            if last_colon_idx > 0:
                potential_group = remaining[last_colon_idx + 1 :]
                if potential_group.isdigit():
                    pattern = remaining[:last_colon_idx]
                    group_num = int(potential_group)

            if not pattern:
                return text

            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                if group_num is not None:
                    if 0 <= group_num <= len(match.groups()):
                        return match.group(group_num) if group_num > 0 else match.group(0)
                else:
                    if match.groups():
                        return match.group(1)
                    return match.group(0)
            return ""
        except (ValueError, IndexError, re.error) as e:
            _debug(f"Error applying regex function: {e}")
            return text

    if function.startswith("if_regex:"):
        try:
            parts = function[9:].split(":", 2)
            if len(parts) < 3:
                return text

            pattern = parts[0]
            then_value = parts[1]
            else_value = parts[2]

            if not pattern:
                return text

            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                return then_value
            return else_value
        except (ValueError, IndexError, re.error) as e:
            _debug(f"Error applying if_regex function: {e}")
            return text

    if function.startswith("switch:"):
        try:
            parts = function[7:].split(":")
            if len(parts) < 2:
                return text

            text_lower = text.lower().strip()
            for i in range(0, len(parts) - 1, 2):
                if i + 1 < len(parts):
                    value = parts[i].lower()
                    result = parts[i + 1]
                    if text_lower == value:
                        return result

            return parts[-1] if parts else text
        except (ValueError, IndexError) as e:
            _debug(f"Error applying switch function: {e}")
            return text

    if function.startswith("regex_cond:"):
        try:
            parts = function[11:].split(":", 3)
            if len(parts) < 4:
                return text

            extract_pattern = parts[0]
            check_pattern = parts[1]
            then_value = parts[2]
            else_group = int(parts[3]) if parts[3].isdigit() else 1

            if not extract_pattern:
                return text

            match = re.search(extract_pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                if match.groups():
                    extracted = (
                        match.group(else_group)
                        if else_group <= len(match.groups())
                        else match.group(1)
                    )
                    extracted = extracted.strip()
                else:
                    extracted = match.group(0).strip()

                if check_pattern:
                    if extracted.lower() == check_pattern.lower() or re.search(
                        check_pattern, extracted, re.IGNORECASE
                    ):
                        return then_value

                return extracted
            return ""
        except (ValueError, IndexError, re.error) as e:
            _debug(f"Error applying regex_cond function: {e}")
            return text

    return text


def sort_feed_items(
    items: list[dict[str, Any]],
    sort_config: dict[str, Any] | None,
    *,
    log_warning: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Sort items based on sort configuration.

    Sort config format:
    {
        "field": "raw.LastUpdatedTime",
        "order": "desc"  # "asc" or "desc"
    }
    """
    if not sort_config or not items:
        return items

    field_path = sort_config.get("field")
    order = (sort_config.get("order") or "desc").lower()

    if not field_path:
        return items

    def get_sort_value(item: dict[str, Any]) -> Any:
        raw_data = item.get("raw", {})
        value = get_nested_value(raw_data, field_path, "")

        if not value and field_path.startswith("raw."):
            value = get_nested_value(raw_data, field_path[4:], "")

        if not value:
            value = get_nested_value(item, field_path, "")

        if isinstance(value, str) and value.startswith("/Date("):
            dt = parse_microsoft_date(value)
            if dt:
                return dt.timestamp()

        if isinstance(value, datetime):
            return value.timestamp()

        if isinstance(value, (int, float)):
            return float(value)

        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return dt.timestamp()
            except ValueError:
                pass

            for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(value, fmt)
                    return dt.timestamp()
                except ValueError:
                    continue

        return str(value)

    try:
        return sorted(items, key=get_sort_value, reverse=(order == "desc"))
    except Exception as e:
        if log_warning is not None:
            log_warning(f"Error sorting items: {e}")
        return items


def format_feed_message(
    item: dict[str, Any],
    format_str: str,
    *,
    feed_name: str = "",
    feed_id: Any = None,
    max_message_length: int = 130,
    shorten_feed_urls: bool = False,
    config: Any | None = None,
    logger: Any | None = None,
) -> str:
    """Format a feed item using placeholders and field functions.

    Supported placeholders and functions match FeedManager.format_message.
    """
    title = sanitize_input(item.get("title") or "Untitled", max_length=None)
    title = html.unescape(title)
    body = sanitize_input(item.get("description", "") or item.get("body", ""), max_length=None)
    if body:
        body = clean_feed_html_body(body)

    link_original = _coerce_url_string(item.get("link", ""))
    published = item.get("published")
    date_str = format_relative_timestamp(published)

    emoji = item.get("emoji")
    if not emoji:
        emoji = "📢"
        feed_name_lower = (feed_name or "").lower()
        if "emergency" in feed_name_lower or "alert" in feed_name_lower:
            emoji = "🚨"
        elif "warning" in feed_name_lower:
            emoji = "⚠️"
        elif "info" in feed_name_lower or "news" in feed_name_lower:
            emoji = "ℹ️"

    replacements = {
        "title": title,
        "body": body,
        "date": date_str,
        "link": link_original,
        "emoji": emoji,
    }

    raw_data = item.get("raw", {})

    def replace_placeholder(match: re.Match[str]) -> str:
        content = match.group(1)

        if "|" in content:
            field_name, function = content.split("|", 1)
            field_name = field_name.strip()
            function = function.strip()
            if function == "auto":
                return ""

            if field_name.startswith("raw."):
                value = str(get_nested_value(raw_data, field_name[4:], ""))
            else:
                value = replacements.get(field_name, "")

            if field_name == "link":
                value = link_original
                fn = _canonical_shorten_name(function)
                if shorten_feed_urls and fn != "shorten" and not fn.startswith("shorten|"):
                    s = shorten_url_sync(
                        link_original,
                        config=config,
                        logger=logger,
                    )
                    if s:
                        value = s

            return apply_feed_field_function(
                value, function, config=config, logger=logger
            )

        field_name = content.strip()

        if field_name.startswith("raw."):
            value = get_nested_value(raw_data, field_name[4:], "")
            if value is None:
                return ""
            if isinstance(value, (dict, list)):
                try:
                    return json.dumps(value)
                except Exception:
                    return str(value)
            return str(value)
        if field_name == "link" and shorten_feed_urls:
            s = shorten_url_sync(
                link_original,
                config=config,
                logger=logger,
            )
            return s if s else link_original
        return replacements.get(field_name, "")

    auto_slots = feed_format_auto_slots(format_str)
    if len(auto_slots) > 1 and logger is not None:
        logger.warning(
            "Multiple {field|auto} placeholders in feed output format; "
            "only the first expands. Others render empty. (feed id %s)",
            feed_id,
        )

    if len(auto_slots) >= 1:
        start, end, auto_field = auto_slots[0]
        prefix = format_str[:start]
        suffix = format_str[end:]
        prefix_r = re.sub(r"\{([^}]+)\}", replace_placeholder, prefix)
        suffix_r = re.sub(r"\{([^}]+)\}", replace_placeholder, suffix)
        budget = max_message_length - len(prefix_r) - len(suffix_r)
        raw_auto = feed_format_auto_base_value(
            auto_field, raw_data, replacements, link_original
        )
        auto_text = truncate_to_budget(raw_auto, budget)
        message = prefix_r + auto_text + suffix_r
    else:
        message = re.sub(r"\{([^}]+)\}", replace_placeholder, format_str)

    if len(message) > max_message_length:
        lines = message.split("\n")
        if len(lines) > 1:
            total_length = sum(len(line) + 1 for line in lines[:-1])
            remaining = max_message_length - total_length - 3
            if remaining > 20:
                lines[-1] = lines[-1][:remaining] + "..."
                message = "\n".join(lines)
            else:
                message = message[: max_message_length - 3] + "..."
        else:
            message = message[: max_message_length - 3] + "..."

    return message
