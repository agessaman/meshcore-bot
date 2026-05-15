#!/usr/bin/env python3
"""
Watch Duty API polling helpers for meshcore-bot.
Fetches geo_events and reports using the same API/headers as the Watch Duty app.
Used by the scheduler: feed channel (summary when acreage/containment change) and report channel.
"""

import html
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests


def _ts_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _headers() -> Dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en",
        "Origin": "https://app.watchduty.org",
        "Referer": "https://app.watchduty.org/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.2 Safari/605.1.15",
        "X-App-Is-Native": "false",
        "X-App-Version": "2026.2.5",
        "X-Git-Tag": "2026.2.5",
    }


def strip_html(text: str) -> str:
    """Remove HTML tags and decode entities for plain-text mesh."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return html.unescape(text)


def first_sentence(text: str) -> str:
    """
    Return only the first sentence from a plain-text string.

    Splits on '.', '!' or '?' followed by whitespace or end-of-string.
    Falls back to the full text when no sentence boundary is found.
    """
    if not text:
        return ""
    # Find first sentence terminator followed by space or end
    m = re.search(r"([.!?])(\s|$)", text)
    if not m:
        return text
    end_idx = m.end(1)
    return text[:end_idx].strip()


def indicates_forward_progress_stopped(text: str) -> bool:
    """
    Return True when report text indicates fire forward progress has stopped.
    """
    if not text:
        return False
    normalized = " ".join(text.lower().split())
    patterns = (
        "forward progress has stopped",
        "forward progress has been stopped",
        "forward progress stopped",
        "forward progression has stopped",
        "forward progression has been stopped",
        "forward progression stopped",
        "forward spread has stopped",
        "spread has stopped",
        "no forward progress",
        "no significant forward progress",
        "no forward progression",
        "no significant forward progression",
    )
    return any(p in normalized for p in patterns)

def _inside_bbox(lat: Optional[float], lng: Optional[float], bbox: Optional[Tuple[float, float, float, float]]) -> bool:
    """Return True if (lat, lng) is inside (lat_min, lng_min, lat_max, lng_max)."""
    if bbox is None or lat is None or lng is None:
        return True
    lat_min, lng_min, lat_max, lng_max = bbox
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def get_event_acres(event: Dict[str, Any]) -> Optional[float]:
    """
    Extract acreage from a geo_event if present.
    Checks event.data.acres, event.data.size_acres, event.acres.
    Returns None if not found or not a valid number.
    """
    data = event.get("data") or {}
    for key in ("acres", "size_acres", "acreage"):
        val = data.get(key) if isinstance(data, dict) else None
        if val is None:
            val = event.get(key)
        if val is not None:
            try:
                n = float(val)
                if n >= 0:
                    return n
            except (TypeError, ValueError):
                pass
    return None


def event_meets_min_acres(event: Dict[str, Any], min_acres: float = 1.0) -> bool:
    """
    Return True only when acreage is present and >= min_acres.
    Events with missing or invalid acreage are excluded.
    """
    acres = get_event_acres(event)
    if acres is None:
        return False
    return acres >= min_acres


def geo_event_is_active(event: Dict[str, Any]) -> bool:
    """True when the Watch Duty API marks the geo_event as active (is_active == True)."""
    return event.get("is_active") is True


def containment_key(event: Dict[str, Any]) -> str:
    """
    Stable string for comparing containment across polls (empty if unknown/none).
    """
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return ""
    c = data.get("containment")
    if c is None or c == "":
        return ""
    if isinstance(c, bool):
        return "true" if c else "false"
    if isinstance(c, (int, float)):
        return f"{float(c):g}"
    return str(c).strip()


def format_containment_display(event: Dict[str, Any]) -> str:
    """Short containment text for mesh (e.g. percent or em dash if unknown)."""
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return "—"
    c = data.get("containment")
    if c is None or c == "":
        return "—"
    if isinstance(c, (int, float)):
        return f"{float(c):g}%"
    return str(c).strip()


def feed_state_changed(
    last_row: Optional[Tuple[Any, Any]],
    acres: float,
    containment_sig: str,
) -> bool:
    """True if there is no prior state or acreage/containment differ from last feed send."""
    if last_row is None:
        return True
    last_acres, last_ct = last_row[0], last_row[1]
    if last_acres is None or abs(float(last_acres) - float(acres)) > 1e-6:
        return True
    prev_ct = "" if last_ct is None else str(last_ct)
    return prev_ct != containment_sig


def format_feed_summary_message(
    name: str,
    acres: float,
    containment_display: str,
    location: str,
    event_id: int,
    max_len: int = 136,
) -> str:
    """
    One line: name, acreage, containment, location, then Watch Duty link.
    Trims the leading segment to keep the URL intact within max_len (mesh limit).
    """
    link = incident_url(event_id)
    suffix = f" | {link}"
    if len(suffix) > max_len:
        return (link[: max_len - 3].rstrip() + "...") if len(link) > max_len else link
    available = max_len - len(suffix)
    if available <= 0:
        return link
    core = f"{name} | {acres:g} ac | {containment_display} | {location}"
    if len(core) > available:
        core = core[: available - 3].rstrip() + "..."
    return f"{core}{suffix}"


def fetch_geo_events(
    bbox: Optional[Tuple[float, float, float, float]] = None,
    timeout: int = 30,
) -> List[Dict[str, Any]]:
    """
    Fetch geo_events (wildfire, location). Optionally filter by bounding box.
    Returns list of event dicts (id, name, lat, lng, address, ...).
    """
    url = f"https://api.watchduty.org/api/v1/geo_events/?geo_event_types=wildfire,location&ts={_ts_ms()}"
    resp = requests.get(url, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    events = resp.json()
    if not isinstance(events, list):
        return []
    if bbox is None:
        return events
    return [e for e in events if _inside_bbox(e.get("lat"), e.get("lng"), bbox)]


def fetch_event_detail(event_id: int, timeout: int = 30) -> Optional[Dict[str, Any]]:
    """Fetch a single geo_event by id. Returns None on 404 or error."""
    url = f"https://api.watchduty.org/api/v1/geo_events/{event_id}?ts={_ts_ms()}"
    try:
        resp = requests.get(url, headers=_headers(), timeout=timeout)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def fetch_reports(
    event_id: int,
    limit: int = 50,
    timeout: int = 30,
) -> List[Dict[str, Any]]:
    """
    Fetch approved reports for an event. API returns newest first.
    Returns list of report dicts (id, message, date_created, ...).
    """
    url = (
        f"https://api.watchduty.org/api/v1/reports/"
        f"?geo_event_id={event_id}&status=approved&limit={limit}&offset=0&ts={_ts_ms()}"
    )
    resp = requests.get(url, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results") or []
    # Sort by date_created ascending so oldest (first report) is first
    results.sort(key=lambda r: r.get("date_created") or "")
    return results


# Base URL for Watch Duty app incident pages (geo_event id)
WATCHDUTY_INCIDENT_BASE = "https://app.watchduty.org/i/"


def incident_url(event_id: int) -> str:
    """Return the Watch Duty app URL for this incident (geo_event)."""
    return f"{WATCHDUTY_INCIDENT_BASE}{event_id}"


def format_location(event: Dict[str, Any]) -> str:
    """Format event location for mesh: address or lat,lng."""
    addr = (event.get("address") or "").strip()
    if addr:
        return addr
    lat = event.get("lat")
    lng = event.get("lng")
    if lat is not None and lng is not None:
        return f"{lat:.4f}, {lng:.4f}"
    return "Location unknown"


def format_new_fire_message(
    name: str, location: str, event_id: Optional[int] = None, max_len: int = 136
) -> str:
    """
    One-line new fire alert for mesh.

    If event_id is set, always keeps the incident URL intact at the end of the line,
    trimming the name/location portion as needed to fit within max_len.
    """
    base = f"{name} | {location}"
    if event_id is None:
        msg = base
        if len(msg) > max_len:
            msg = msg[: max_len - 3].rstrip() + "..."
        return msg

    link = incident_url(event_id)
    suffix = f" | {link}"
    # If even the suffix alone is too long, fall back to raw link truncated
    if len(suffix) > max_len:
        return (link[: max_len - 3].rstrip() + "...") if len(link) > max_len else link

    # Reserve space for suffix; trim base if necessary
    available = max_len - len(suffix)
    if available <= 0:
        # No room for base text; send just the link
        return link

    if len(base) > available:
        base = base[: available - 3].rstrip() + "..."

    return f"{base}{suffix}"


def watchduty_bbox_from_config(config: Any) -> Optional[Tuple[float, float, float, float]]:
    """Parse [WatchDuty] bbox (lat_min,lng_min,lat_max,lng_max) or return None."""
    if not getattr(config, "has_section", lambda _: False)("WatchDuty"):
        return None
    bbox_str = config.get("WatchDuty", "bbox", fallback="").strip()
    if not bbox_str:
        return None
    try:
        parts = [float(x.strip()) for x in bbox_str.split(",")]
        if len(parts) != 4:
            return None
        return tuple(parts)  # type: ignore[return-value]
    except (ValueError, AttributeError):
        return None


def watchduty_min_acres_from_config(config: Any) -> float:
    """Minimum acres from [WatchDuty] min_acres (default 1.0)."""
    if not getattr(config, "has_section", lambda _: False)("WatchDuty"):
        return 1.0
    try:
        return float(config.getfloat("WatchDuty", "min_acres", fallback=1.0))
    except Exception:
        return 1.0


def fetch_active_geo_events_for_user_query(
    config: Any,
    *,
    include_prescribed: bool = False,
    timeout: int = 30,
) -> List[Dict[str, Any]]:
    """
    Active geo_events suitable for interactive fire commands.
    Uses the same bbox and min_acres as [WatchDuty] when that section exists.
    """
    bbox = watchduty_bbox_from_config(config)
    min_acres = watchduty_min_acres_from_config(config)
    events = fetch_geo_events(bbox=bbox, timeout=timeout)
    if not isinstance(events, list):
        return []
    if not include_prescribed:
        events = [e for e in events if not (e.get("data") or {}).get("is_prescribed")]
    events = [e for e in events if geo_event_is_active(e)]
    events = [e for e in events if event_meets_min_acres(e, min_acres=min_acres)]
    events.sort(key=lambda e: (str(e.get("name") or "").lower(), e.get("id") or 0))
    return events


def format_location_short(event: Dict[str, Any], max_len: int = 56) -> str:
    """
    Short location for compact lists: last comma-separated segment of ``address`` (city name),
    else ``format_location`` truncated.
    """
    addr = (event.get("address") or "").strip()
    if addr and "," in addr:
        parts = [p.strip() for p in addr.split(",") if p.strip()]
        if parts:
            tail = parts[-1]
            if tail:
                if len(tail) <= max_len:
                    return tail
                return tail[: max_len - 3].rstrip() + "..."
    loc = format_location(event)
    if len(loc) > max_len:
        return loc[: max_len - 3].rstrip() + "..."
    return loc


def event_data(event: Dict[str, Any]) -> Dict[str, Any]:
    d = event.get("data")
    return d if isinstance(d, dict) else {}


def _evac_field_nonempty(data: Dict[str, Any], raw_key: str, has_custom_key: str) -> bool:
    """True when Watch Duty sets the custom flag or embeds a non-empty orders/warnings/etc. collection."""
    if data.get(has_custom_key) is True:
        return True
    raw = data.get(raw_key)
    if raw is None or raw == "":
        return False
    if isinstance(raw, list):
        return len(raw) > 0
    if isinstance(raw, dict):
        return len(raw) > 0
    return True


def incident_has_evac_info(geo_event: Dict[str, Any]) -> bool:
    """True when the incident has structured evacuation orders or warnings on Watch Duty."""
    data = event_data(geo_event)
    pairs = (
        ("evacuation_orders", "has_custom_evacuation_orders"),
        ("evacuation_warnings", "has_custom_evacuation_warnings"),
    )
    for raw_key, has_key in pairs:
        if _evac_field_nonempty(data, raw_key, has_key):
            return True
    return False


def has_evacuation_orders_flag(data: Dict[str, Any]) -> bool:
    """Backward-compatible: prefer :func:`incident_has_evac_info` with the full geo_event dict."""
    return incident_has_evac_info({"data": data})


def _flatten_evac_item(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, str):
        return strip_html(item).strip()
    if isinstance(item, dict):
        for key in ("text", "message", "description", "title", "name", "body", "summary"):
            val = item.get(key)
            if val is not None and str(val).strip():
                return strip_html(str(val)).strip()
        try:
            return strip_html(json.dumps(item, ensure_ascii=False)).strip()
        except Exception:
            return str(item)
    return strip_html(str(item)).strip()


def _evacuation_lines_for_data_key(data: Dict[str, Any], raw_key: str) -> List[str]:
    """Plain-text lines for one ``data`` collection (orders, warnings, etc.)."""
    raw = data.get(raw_key)
    if raw is None:
        return []
    if isinstance(raw, str):
        t = strip_html(raw).strip()
        return [t] if t else []
    if isinstance(raw, list):
        lines: List[str] = []
        for item in raw:
            line = _flatten_evac_item(item)
            if line:
                lines.append(line)
        return lines
    if isinstance(raw, dict):
        line = _flatten_evac_item(raw)
        return [line] if line else []
    line = _flatten_evac_item(raw)
    return [line] if line else []


def evacuation_order_lines(data: Dict[str, Any]) -> List[str]:
    """Lines from ``data.evacuation_orders`` only (see :func:`evacuation_display_lines` for orders and warnings)."""
    return _evacuation_lines_for_data_key(data, "evacuation_orders")


def evacuation_display_lines(geo_event: Dict[str, Any]) -> List[str]:
    """Evacuation orders and warnings only (mesh display), in that order."""
    data = event_data(geo_event)
    out: List[str] = []
    labeled = (
        ("[Order] ", "evacuation_orders"),
        ("[Warn] ", "evacuation_warnings"),
    )
    for prefix, key in labeled:
        for line in _evacuation_lines_for_data_key(data, key):
            out.append(prefix + line)
    return out


def evacuation_display_count(geo_event: Dict[str, Any]) -> int:
    """Number of evacuation order and warning lines for mesh display."""
    return len(evacuation_display_lines(geo_event))


def resolve_active_event_by_query(
    events: List[Dict[str, Any]],
    query: str,
    *,
    config: Optional[Any] = None,
    include_prescribed: bool = False,
    numeric_index_list: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Match a fire from the ``fires`` list or by Watch Duty geo_event id / name.

    Numeric ``query`` resolution order:

    1. **Id in list** — any loaded event in ``events`` with ``id == n`` (same id as ``https://app.watchduty.org/i/<n>``).
    2. **List index** — if ``numeric_index_list`` is set, ``n`` from 1 … ``len(numeric_index_list)``
       (e.g. evac's "fires with evacuations" list); otherwise ``n`` from 1 … ``len(events)`` (fires order).
    3. **Fetch by id** — when ``config`` is passed, ``GET /geo_events/<n>``; must be active;
       (bbox is not applied so a direct id still resolves outside the optional Watch Duty bbox filter).
    """
    t = (query or "").strip()
    if not t:
        return None, "usage"
    if t.isdigit():
        n = int(t)
        for e in events:
            eid = e.get("id")
            if eid is not None:
                try:
                    if int(eid) == n:
                        return e, None
                except (TypeError, ValueError):
                    continue
        if numeric_index_list is not None:
            if 1 <= n <= len(numeric_index_list):
                return numeric_index_list[n - 1], None
        elif 1 <= n <= len(events):
            return events[n - 1], None
        if config is not None:
            detail = fetch_event_detail(n)
            if not detail:
                if numeric_index_list is not None:
                    return None, (
                        f"No active fire id {n}. "
                        f"Use evac for #1–{len(numeric_index_list)} or an id from app.watchduty.org/i/<id>"
                    )
                return None, (
                    f"No active fire id {n}. "
                    f"Use fires for #1–{len(events)} or an id from app.watchduty.org/i/<id>"
                )
            if not geo_event_is_active(detail):
                return None, f"Fire id {n} is not active."
            if not include_prescribed and (detail.get("data") or {}).get("is_prescribed"):
                return None, (
                    f"Fire id {n} is a prescribed burn."
                )
            return detail, None
        if numeric_index_list is not None:
            return None, (
                f"No evacuation list #{n} ({len(numeric_index_list)} with evacuations). "
                "Run evac (no args) for the list."
            )
        return None, f"No fire #{n} ({len(events)} active). Try fires."
    t_lower = t.lower()
    for e in events:
        name = (e.get("name") or "").strip()
        if name.lower() == t_lower:
            return e, None
    matches = [e for e in events if t_lower in (e.get("name") or "").strip().lower()]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, f"No active fire matching '{t}'."
    labels = [((m.get("name") or "?")[:48]) for m in matches[:5]]
    extra = f" (+{len(matches) - 5} more)" if len(matches) > 5 else ""
    return None, "Multiple matches: " + "; ".join(labels) + extra


def mesh_pack_lines(lines: List[str], max_len: int) -> List[str]:
    """Pack non-empty lines into newline-separated chunks no longer than max_len."""
    out: List[str] = []
    buf = ""
    for line in lines:
        if not line:
            continue
        if not buf:
            candidate = line
        else:
            candidate = buf + "\n" + line
        if len(candidate) <= max_len:
            buf = candidate
        else:
            if buf:
                out.append(buf)
            if len(line) <= max_len:
                buf = line
            else:
                start = 0
                while start < len(line):
                    out.append(line[start : start + max_len])
                    start += max_len
                buf = ""
    if buf:
        out.append(buf)
    return out if out else [""]


def format_report_message(
    event_name: str,
    report_message_plain: str,
    event_id: Optional[int] = None,
    max_len: int = 136,
) -> str:
    """
    Prefix report with event name; truncate to max_len.

    If event_id is set, always keeps the incident URL intact at the end of the line,
    trimming the report text portion as needed to fit within max_len.
    """
    max_name = 40
    name = (event_name[: max_name - 3] + "...") if len(event_name) > max_name else event_name
    prefix = f"{name}: "
    if event_id is None:
        remainder = max_len - len(prefix)
        if remainder <= 0:
            return prefix[: max_len]
        text = report_message_plain
        if len(text) > remainder:
            text = text[: remainder - 3].rstrip() + "..."
        return prefix + text

    link = incident_url(event_id)
    suffix = f" | {link}"
    # If even the suffix alone is too long, fall back to raw link truncated
    if len(suffix) > max_len:
        return (link[: max_len - 3].rstrip() + "...") if len(link) > max_len else link

    available = max_len - len(suffix)
    if available <= 0:
        return link

    # Ensure prefix fits inside available space and trim text accordingly
    if len(prefix) >= available:
        # Prefix alone fills available space; trim prefix
        base = prefix[: available - 3].rstrip() + "..."
    else:
        remainder = available - len(prefix)
        text = report_message_plain
        if len(text) > remainder:
            text = text[: remainder - 3].rstrip() + "..."
        base = prefix + text

    return f"{base}{suffix}"
