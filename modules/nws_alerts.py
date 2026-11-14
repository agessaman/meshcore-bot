"""Parse entries of the NWS active-alerts Atom feed (api.weather.gov/alerts/active.atom).

Shared by the wx command and the weather service. The two classify Special
Weather Statements differently, so each passes its own ``SpecialStatementRules``
(``WX_SPECIAL_RULES`` or ``SERVICE_SPECIAL_RULES``); everything else is common.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

SPECIAL_EVENTS = ("special", "special weather")

_HEADLINE_SKIP_WORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'will', 'lead',
    'increased', 'threat', 'remains', 'effect', 'until', 'during', 'last', 'week', 'including', 'today',
}

# (keywords in the lowercased child tag, field); first match wins, as an elif chain
_CAP_CHILD_FIELDS = (
    (("event",), "event"),
    (("severity",), "severity"),
    (("urgency",), "urgency"),
    (("certainty",), "certainty"),
    (("effective",), "effective"),
    (("expires",), "expires"),
    (("areadesc", "area"), "area_desc"),
)

# localName -> (field, value meaning "not found yet")
_CAP_LOCAL_NAMES = {
    'event': ('event', ''),
    'severity': ('severity', 'Unknown'),
    'urgency': ('urgency', 'Unknown'),
    'certainty': ('certainty', 'Unknown'),
    'effective': ('effective', ''),
    'expires': ('expires', ''),
    'areadesc': ('area_desc', ''),
    'area': ('area_desc', ''),
}


def node_value(node: Any) -> str:
    """Joined text of a DOM node's direct text children."""
    if not node or not node.childNodes:
        return ""
    text_parts = []
    for child in node.childNodes:
        if child.nodeType == child.TEXT_NODE or hasattr(child, 'nodeValue') and child.nodeValue:
            text_parts.append(child.nodeValue)
    return " ".join(text_parts).strip()


def _first_child_value(entry: Any, tag: str) -> str:
    elems = entry.getElementsByTagName(tag)
    if elems and elems[0].childNodes:
        return elems[0].childNodes[0].nodeValue if elems[0].childNodes[0].nodeValue else ""
    return ""


def entry_title(entry: Any) -> Optional[str]:
    """The entry's <title> text ("" when absent; None when its first child is not text)."""
    title_elem = entry.getElementsByTagName("title")
    return title_elem[0].childNodes[0].nodeValue if title_elem and title_elem[0].childNodes else ""


def entry_summary(entry: Any) -> str:
    """The entry's <summary> text, falling back to <content>."""
    return _first_child_value(entry, "summary") or _first_child_value(entry, "content")


def entry_nws_headline(entry: Any) -> str:
    """The NWSheadline CAP parameter (cap:parameter, or parameter without the prefix)."""
    params = entry.getElementsByTagName("cap:parameter")
    if not params:
        params = entry.getElementsByTagName("parameter")
    for param in params:
        value_name_elem = param.getElementsByTagName("valueName")
        value_elem = param.getElementsByTagName("value")
        if value_name_elem and value_elem and value_name_elem[0].childNodes and value_elem[0].childNodes:
            value_name = value_name_elem[0].childNodes[0].nodeValue if value_name_elem[0].childNodes[0].nodeValue else ""
            if value_name == "NWSheadline":
                return value_elem[0].childNodes[0].nodeValue if value_elem[0].childNodes[0].nodeValue else ""
    return ""


def _first_meaningful_headline_word(headline_lower: str) -> Optional[str]:
    meaningful_words = [w for w in headline_lower.split() if w not in _HEADLINE_SKIP_WORDS and len(w) > 3]
    return meaningful_words[0].capitalize() if meaningful_words else None


def _rain_unless_other_threat(headline_lower: str) -> Optional[str]:
    # Rain mentioned alongside another threat keeps the generic event
    if not any(word in headline_lower for word in ['landslide', 'flood', 'wind', 'snow']):
        return "Rainfall"
    return None


def wx_special_headline_event(headline_lower: str) -> Optional[str]:
    """wx: event name for a Special Weather Statement from its NWSheadline, or None to keep it generic."""
    if any(phrase in headline_lower for phrase in ['debris flow', 'mudslide']):
        return "Debris Flow"
    if 'landslide' in headline_lower:
        return "Landslide (Burn)" if ('burn' in headline_lower or 'burned area' in headline_lower) else "Landslide"
    if any(phrase in headline_lower for phrase in ['flash flood', 'river flood']) or 'flood' in headline_lower or 'flooding' in headline_lower:
        return "Flood"
    if any(phrase in headline_lower for phrase in ['high wind', 'strong wind', 'damaging wind']) or 'wind' in headline_lower or 'gust' in headline_lower:
        return "Wind"
    if any(phrase in headline_lower for phrase in ['heavy rain', 'excessive rain']):
        return "Heavy Rain"
    if 'rain' in headline_lower or 'rainfall' in headline_lower or 'precipitation' in headline_lower:
        return _rain_unless_other_threat(headline_lower)
    if any(phrase in headline_lower for phrase in ['heavy snow', 'blizzard', 'winter storm']) or 'snow' in headline_lower or 'winter' in headline_lower:
        return "Snow"
    if any(phrase in headline_lower for phrase in ['dense fog', 'low visibility']):
        return "Fog"
    if 'fog' in headline_lower or 'visibility' in headline_lower:
        return "Visibility"
    if any(phrase in headline_lower for phrase in ['extreme heat', 'excessive heat']):
        return "Heat"
    if 'heat' in headline_lower or 'temperature' in headline_lower:
        return "Temperature"
    if any(phrase in headline_lower for phrase in ['storm surge', 'coastal flood']) or 'marine' in headline_lower or 'coastal' in headline_lower:
        return "Marine"
    return _first_meaningful_headline_word(headline_lower)


def service_special_headline_event(headline_lower: str) -> Optional[str]:
    """Weather service: event name for a Special Weather Statement from its NWSheadline, or None."""
    if any(phrase in headline_lower for phrase in ['debris flow', 'mudslide']):
        return "Debris Flow"
    if 'landslide' in headline_lower:
        return "Landslide (Burn)" if ('burn' in headline_lower or 'burned area' in headline_lower) else "Landslide"
    if any(phrase in headline_lower for phrase in ['flash flood', 'river flood', 'flood', 'flooding']):
        return "Flood"
    if any(phrase in headline_lower for phrase in ['high wind', 'strong wind', 'damaging wind', 'wind', 'gust']):
        return "Wind"
    if any(phrase in headline_lower for phrase in ['heavy rain', 'excessive rain', 'rain', 'rainfall', 'precipitation']):
        return _rain_unless_other_threat(headline_lower)
    if any(phrase in headline_lower for phrase in ['heavy snow', 'blizzard', 'winter storm', 'snow', 'winter']):
        return "Snow"
    if any(phrase in headline_lower for phrase in ['dense fog', 'low visibility', 'fog', 'visibility']):
        return "Fog" if 'fog' in headline_lower else "Visibility"
    if any(phrase in headline_lower for phrase in ['extreme heat', 'excessive heat', 'heat', 'temperature']):
        return "Heat" if 'heat' in headline_lower else "Temperature"
    if any(phrase in headline_lower for phrase in ['storm surge', 'coastal flood', 'marine', 'coastal']):
        return "Marine"
    return _first_meaningful_headline_word(headline_lower)


_COMMON_SUMMARY_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (('landslide', 'debris flow', 'mudslide'), "Landslide"),
    (('hydrologic', 'river', 'flood', 'stream'), "Hydrologic"),
    (('marine', 'coastal', 'beach', 'surf'), "Marine"),
)


@dataclass(frozen=True)
class SpecialStatementRules:
    """How a caller names a generic Special Weather Statement."""

    headline_event: Callable[[str], Optional[str]]
    summary_keywords: tuple[tuple[tuple[str, ...], str], ...]
    # wx also reads CAP fields by DOM localName after the tag-name scan
    scan_local_names: bool


WX_SPECIAL_RULES = SpecialStatementRules(
    headline_event=wx_special_headline_event,
    summary_keywords=_COMMON_SUMMARY_KEYWORDS + (
        (('avalanche', 'snow', 'mountain'), "Avalanche"),
        (('air quality', 'smoke', 'pollution'), "Air Quality"),
        (('wind', 'gust'), "Wind"),
        (('rain', 'precipitation', 'shower', 'rainfall'), "Rainfall"),
        (('temperature', 'heat', 'cold', 'freeze'), "Temperature"),
        (('visibility', 'fog', 'haze'), "Visibility"),
    ),
    scan_local_names=True,
)

SERVICE_SPECIAL_RULES = SpecialStatementRules(
    headline_event=service_special_headline_event,
    summary_keywords=_COMMON_SUMMARY_KEYWORDS + (
        (('wind', 'gust'), "Wind"),
        (('rain', 'precipitation', 'shower', 'rainfall'), "Rainfall"),
    ),
    scan_local_names=False,
)


def _special_statement_event(event: str, title_lower: str, nws_headline: str, summary: str,
                             rules: SpecialStatementRules) -> str:
    if event.lower() in SPECIAL_EVENTS and nws_headline:
        event = rules.headline_event(nws_headline.lower()) or event
    if event.lower() in SPECIAL_EVENTS and summary:
        summary_lower = summary.lower()
        for words, name in rules.summary_keywords:
            if any(word in summary_lower for word in words):
                event = name
                break
    if event.lower() in SPECIAL_EVENTS:
        event = "Weather" if "weather" in title_lower else "Special"
    return event


def _event_from_title(title: str, nws_headline: str, summary: str, rules: SpecialStatementRules) -> tuple[str, str]:
    """(event_type, event) from a title such as "High Wind Warning issued ... by NWS Seattle WA"."""
    title_lower = title.lower()
    for event_type, pattern in (
        ("Warning", r'^([^W]+?)\s+Warning'),
        ("Watch", r'^([^W]+?)\s+Watch'),
        ("Advisory", r'^([^A]+?)\s+Advisory'),
    ):
        if event_type.lower() in title_lower:
            event_match = re.search(pattern, title, re.IGNORECASE)
            return event_type, event_match.group(1).strip() if event_match else ""
    if "statement" in title_lower:
        event_match = re.search(r'^([^S]+?)\s+Statement', title, re.IGNORECASE)
        event = event_match.group(1).strip() if event_match else "Special"
        return "Statement", _special_statement_event(event, title_lower, nws_headline, summary, rules)
    return "Unknown", title.split()[0] if title else ""


def _times_and_office_from_title(title: str) -> tuple[str, str, str]:
    """(effective, expires, office) from "issued X until Y by OFFICE"."""
    effective = expires = office = ""
    issued_match = re.search(r'issued\s+([^u]+?)\s+until\s+(.+?)\s+by', title, re.IGNORECASE)
    if issued_match:
        effective = issued_match.group(1).strip()
        expires = issued_match.group(2).strip()
    else:
        until_match = re.search(r'until\s+(.+?)\s+by', title, re.IGNORECASE)
        if until_match:
            expires = until_match.group(1).strip()
    office_match = re.search(r'by\s+(.+?)$', title, re.IGNORECASE)
    if office_match:
        office = office_match.group(1).strip()
    return effective, expires, office


def _apply_cap_children(entry: Any, fields: dict[str, str]) -> None:
    """Override fields from the entry's direct CAP children (cap:event, cap:severity, ...)."""
    for child in entry.childNodes:
        if not hasattr(child, 'tagName'):
            continue
        tag_lower = child.tagName.lower()
        for keywords, field in _CAP_CHILD_FIELDS:
            if not any(k in tag_lower for k in keywords):
                continue
            if field == "event" and fields["event"]:
                continue
            value = node_value(child)
            if value:
                fields[field] = value
            break


def _apply_cap_local_names(entry: Any, fields: dict[str, str]) -> None:
    """Fill still-unset fields from any descendant by DOM localName."""
    try:
        for node in entry.getElementsByTagName("*"):
            if hasattr(node, 'localName'):
                local_name = node.localName.lower()
                node_val = node_value(node)
                if node_val and local_name in _CAP_LOCAL_NAMES:
                    field, unset = _CAP_LOCAL_NAMES[local_name]
                    if fields[field] == unset:
                        fields[field] = node_val
    except Exception:
        pass  # Namespace-aware attributes may not be available


def parse_alert_fields(entry: Any, title: str, summary: str, nws_headline: str,
                       rules: SpecialStatementRules) -> dict[str, str]:
    """Alert dict (title through office) for one feed entry; raises on malformed input."""
    event_type, event = _event_from_title(title, nws_headline, summary, rules)
    effective, expires, office = _times_and_office_from_title(title)
    fields = {
        'event': event,
        'severity': "Unknown",
        'urgency': "Unknown",
        'certainty': "Unknown",
        'effective': effective,
        'expires': expires,
        'area_desc': "",
    }
    _apply_cap_children(entry, fields)
    if rules.scan_local_names:
        _apply_cap_local_names(entry, fields)

    event = fields['event']
    severity = fields['severity']
    if severity == "Unknown":
        if any(word in event.lower() for word in ['extreme', 'tornado', 'hurricane', 'blizzard']):
            severity = "Extreme"
        elif any(word in event.lower() for word in ['severe', 'warning']):
            severity = "Severe"
        elif any(word in event.lower() for word in ['advisory', 'moderate']):
            severity = "Moderate"
        else:
            severity = "Minor"

    urgency = fields['urgency']
    if urgency == "Unknown":
        if event_type == "Warning":
            urgency = "Immediate"
        elif event_type == "Watch":
            urgency = "Expected"
        else:
            urgency = "Future"

    return {
        'title': title,
        'summary': summary,
        'nws_headline': nws_headline,
        'event': event,
        'event_type': event_type,
        'severity': severity,
        'urgency': urgency,
        'certainty': fields['certainty'],
        'effective': fields['effective'],
        'expires': fields['expires'],
        'area_desc': fields['area_desc'],
        'office': office,
    }
