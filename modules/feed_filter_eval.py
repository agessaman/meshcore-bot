"""
Shared feed filter_config evaluation (RSS/API items).

Used by FeedManager and the web viewer preview so filter behavior stays consistent.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Union


def get_nested_value(data: Any, path: str, default: Any = "") -> Any:
    """Nested dict/list access using dot notation (same rules as FeedManager._get_nested_value)."""
    if not path or not data:
        return default

    parts = path.split(".")
    value = data

    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list):
            try:
                idx = int(part)
                if 0 <= idx < len(value):
                    value = value[idx]
                else:
                    return default
            except (ValueError, TypeError):
                return default
        else:
            return default

        if value is None:
            return default

    return value if value is not None else default


def parse_microsoft_date(date_str: str) -> Optional[datetime]:
    """Parse Microsoft JSON date format: /Date(timestamp-offset)/"""
    if not date_str or not isinstance(date_str, str):
        return None

    match = re.match(r"/Date\((\d+)([+-]\d+)?\)/", date_str)
    if match:
        timestamp_ms = int(match.group(1))
        offset_str = match.group(2) if match.group(2) else "+0000"
        timestamp = timestamp_ms / 1000.0

        try:
            offset_hours = int(offset_str[:3])
            offset_mins = int(offset_str[3:5])
            offset_seconds = (offset_hours * 3600) + (offset_mins * 60)
            if offset_str[0] == "-":
                offset_seconds = -offset_seconds

            tz = timezone.utc
            if offset_seconds != 0:
                from datetime import timedelta as td

                tz = timezone(td(seconds=offset_seconds))

            return datetime.fromtimestamp(timestamp, tz=tz)
        except (ValueError, IndexError):
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)

    return None


def _normalize_to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def parse_item_field_as_datetime(item: Dict[str, Any], field_path: str) -> Optional[datetime]:
    """
    Resolve field_path on item and parse to datetime (aligned with FeedManager._sort_items get_sort_value).
    """
    raw_data = item.get("raw", {})
    value: Any = get_nested_value(raw_data, field_path, "")
    if not value and field_path.startswith("raw."):
        value = get_nested_value(raw_data, field_path[4:], "")
    if not value:
        value = get_nested_value(item, field_path, "")

    if value is None or value == "":
        return None

    if isinstance(value, str) and value.startswith("/Date("):
        dt = parse_microsoft_date(value)
        if dt:
            return dt

    if isinstance(value, datetime):
        return _normalize_to_utc(value)

    if isinstance(value, (int, float)):
        num = float(value)
        # Heuristic: milliseconds since epoch (e.g. WSDOT-style APIs)
        if num > 1e12:
            num = num / 1000.0
        try:
            return datetime.fromtimestamp(num, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return _normalize_to_utc(dt)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

    return None


def _get_field_value_for_string_ops(item: Dict[str, Any], field_path: str) -> Any:
    raw_data = item.get("raw", {})
    field_value = get_nested_value(raw_data, field_path, "")
    if not field_value and field_path.startswith("raw."):
        field_value = get_nested_value(raw_data, field_path[4:], "")
    if not field_value:
        field_value = get_nested_value(item, field_path, "")
    return field_value


def evaluate_filter_condition(
    item: Dict[str, Any],
    condition: Dict[str, Any],
    *,
    log_warning: Optional[Callable[[str], None]] = None,
) -> Optional[bool]:
    """
    Evaluate a single filter condition. Returns None if the condition should be skipped
    (invalid / missing field path for operators that require it).
    """
    field_path = condition.get("field")
    operator = condition.get("operator", "equals")

    if operator in ("within_days", "within_weeks"):
        if not field_path:
            return None
        include_if_missing = bool(condition.get("include_if_missing", False))
        dt = parse_item_field_as_datetime(item, field_path)
        if dt is None:
            return True if include_if_missing else False

        if operator == "within_days":
            if "days" not in condition:
                return False
            try:
                days = float(condition["days"])
            except (TypeError, ValueError):
                return False
        else:
            if "weeks" not in condition:
                return False
            try:
                weeks = float(condition["weeks"])
            except (TypeError, ValueError):
                return False
            days = weeks * 7.0

        if days < 0:
            days = 0.0

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        item_dt = _normalize_to_utc(dt)
        return item_dt >= cutoff

    if not field_path:
        return None

    field_value = _get_field_value_for_string_ops(item, field_path)
    field_value_str = str(field_value).lower() if field_value is not None else ""

    if operator == "equals":
        compare_value = str(condition.get("value", "")).lower()
        return field_value_str == compare_value
    if operator == "not_equals":
        compare_value = str(condition.get("value", "")).lower()
        return field_value_str != compare_value
    if operator == "in":
        values = [str(v).lower() for v in condition.get("values", [])]
        return field_value_str in values
    if operator == "not_in":
        values = [str(v).lower() for v in condition.get("values", [])]
        return field_value_str not in values
    if operator == "matches":
        pattern = condition.get("pattern", "")
        if pattern:
            try:
                return bool(re.search(pattern, str(field_value), re.IGNORECASE))
            except re.error:
                return False
        return False
    if operator == "not_matches":
        pattern = condition.get("pattern", "")
        if pattern:
            try:
                return not bool(re.search(pattern, str(field_value), re.IGNORECASE))
            except re.error:
                return True
        return True
    if operator == "contains":
        compare_value = str(condition.get("value", "")).lower()
        return compare_value in field_value_str
    if operator == "not_contains":
        compare_value = str(condition.get("value", "")).lower()
        return compare_value not in field_value_str

    if log_warning:
        log_warning(f"Unknown filter operator: {operator}")
    return True


def item_passes_filter_config(
    item: Dict[str, Any],
    filter_config: Union[str, Dict[str, Any], None],
    *,
    log_warning: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    Return True if the item passes all filter conditions (AND) or any (OR), matching FeedManager behavior.

    Invalid JSON or empty conditions => pass through (True).
    """
    if not filter_config:
        return True

    try:
        cfg = json.loads(filter_config) if isinstance(filter_config, str) else filter_config
    except (json.JSONDecodeError, TypeError):
        if log_warning:
            log_warning("Invalid filter_config JSON, sending all items")
        return True

    if not isinstance(cfg, dict):
        return True

    conditions = cfg.get("conditions", [])
    if not conditions:
        return True

    logic = str(cfg.get("logic", "AND")).upper()

    results: list[bool] = []
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        out = evaluate_filter_condition(item, condition, log_warning=log_warning)
        if out is None:
            continue
        results.append(out)

    if not results:
        return True

    if logic == "OR":
        return any(results)
    return all(results)
