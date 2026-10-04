"""Placeholder and filter reference for template settings, and their preview.

A plugin's ``settings_schema`` field marks itself as a template with
``"template": template_spec(...)``. The web viewer's Plugins page reads that to
show which placeholders the field takes (and, for piped templates, the filters),
and the preview endpoint renders a piped template against sample messages with
:func:`render_preview`.

Three template syntaxes exist, and a field must say which one it uses, because a
reference that offered filters to a ``str.format`` field would be wrong:

- ``piped``: :mod:`modules.response_template` (``{field|filter:arg}``, quoted
  literals, conditionals). Used by the test reply and the path reply prefix.
- ``format``: Python ``str.format``. Placeholders only, no filters. What an
  unknown name does depends on the plugin (the greeter sends nothing, multitest
  falls back to its default), so each such field names a renderer in
  :data:`FORMAT_PREVIEWS` that reproduces its plugin's handling.

The catalog here is data for the UI. The fields themselves come from the
commands that render them; ``tests/test_template_reference.py`` keeps the
filter list in step with :data:`~modules.response_template.RESPONSE_TEMPLATE_FILTERS`.
"""

from __future__ import annotations

import configparser
import dataclasses
import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from .models import MeshMessage
from .response_template import format_piped_template
from .utils import decode_escape_sequences

# Sample messages the preview renders against. Each differs in what the piped
# filters test (hop count and bytes per hop), so a conditional shows its effect.
SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "multibyte",
        "label": "3 hops, 2-byte path",
        "nodes": ["a1b2", "c3d4", "e5f6"],
        "bytes_per_hop": 2,
    },
    {
        "id": "onebyte",
        "label": "3 hops, 1-byte path",
        "nodes": ["a1", "c3", "e5"],
        "bytes_per_hop": 1,
    },
    {
        "id": "direct",
        "label": "Direct (0 hops)",
        "nodes": [],
        "bytes_per_hop": None,
    },
]

# name -> (description, sample value or {scenario_id: value}).
_MESSAGE_PLACEHOLDERS: dict[str, tuple[str, Any]] = {
    "sender": ("Sender's name", "Alice"),
    "connection_info": ("Path, SNR and RSSI together", {
        "multibyte": "a1b2,c3d4,e5f6 (3 hops) | SNR: 9.5 dB | RSSI: -98 dBm",
        "onebyte": "a1,c3,e5 (3 hops) | SNR: 9.5 dB | RSSI: -98 dBm",
        "direct": "Direct | SNR: 9.5 dB | RSSI: -98 dBm",
    }),
    "path": ("Route the message took", {
        "multibyte": "a1b2,c3d4,e5f6 (3 hops)",
        "onebyte": "a1,c3,e5 (3 hops)",
        "direct": "Direct",
    }),
    "hops": ("Hop count; ? when unknown", {"multibyte": "3", "onebyte": "3", "direct": "0"}),
    "hops_label": ("Hop count with the word: 1 hop, 3 hops", {
        "multibyte": "3 hops", "onebyte": "3 hops", "direct": "0 hops",
    }),
    "timestamp": ("Bot's time when the message arrived (HH:MM:SS)", "14:32:07"),
    "snr": ("Signal-to-noise ratio in dB", "9.5"),
    "rssi": ("Signal strength in dBm", "-98"),
    "packet_hash": ("16-character packet hash; empty without RF data", "3F2A9C1B7E4D0A65"),
}

_EXTRA_PLACEHOLDERS: dict[str, tuple[str, Any]] = {
    "phrase": ("Text after the trigger word (test radio check → radio check)", "radio check"),
    "phrase_part": ("': <phrase>', or empty when there is none", ": radio check"),
    "elapsed": ("Time the message took to arrive, e.g. 842ms", "842ms"),
    "path_distance": (
        "Distance travelled, sender → hops → bot; empty unless every node has a location",
        {"multibyte": "12.4km", "onebyte": "12.4km", "direct": ""},
    ),
    "firstlast_distance": (
        "Distance between the first and last repeater; empty if either has no location",
        {"multibyte": "8.1km", "onebyte": "8.1km", "direct": ""},
    ),
}

ALL_PLACEHOLDERS = {**_MESSAGE_PLACEHOLDERS, **_EXTRA_PLACEHOLDERS}
MESSAGE_PLACEHOLDERS = tuple(_MESSAGE_PLACEHOLDERS)

# Filters for piped templates, in the order the reference lists them. Aliases
# resolve to the same filter and are listed with it rather than on their own.
PIPED_FILTERS: list[dict[str, Any]] = [
    {
        "name": "hops_min", "arg": "N", "aliases": [],
        "help": "Clear the value unless the message travelled at least N hops. "
                "hops_min:1 drops it on a direct message.",
        "example": "{firstlast_distance|hops_min:1}",
    },
    {
        "name": "pathbytes_min", "arg": "N", "aliases": ["pathbytes"],
        "help": "Clear the value unless the path uses at least N bytes per hop (1-3).",
        "example": "{path_distance|pathbytes_min:2}",
    },
    {
        "name": "prefix_if_nonempty", "arg": "text", "aliases": [],
        "help": "Put text in front of the value, only when the value is not empty. "
                "Unquoted, the text runs to the closing } (it may contain |), so it must "
                "come last; quote it to add filters after it.",
        "example": "{path_distance|prefix_if_nonempty: | Dist: }",
    },
    {
        "name": "if_nonempty", "arg": "text", "aliases": ["if_notempty"],
        "help": "Replace the value with text when the value is not empty.",
        "example": "{packet_hash|if_nonempty:#}",
    },
    {
        "name": "shorten", "arg": None, "aliases": ["shorten_url"],
        "help": "Shorten a URL with the [External_Data] shortener; the long URL is kept "
                "if that fails. Not applied in the preview.",
        "example": "{\"https://example.com/a/long/link\"|shorten}",
    },
]

PIPED_SYNTAX_NOTES = [
    "A filter argument in double quotes ends at the closing quote, so more filters can "
    "follow: {path_distance|prefix_if_nonempty:\" Dist \"|hops_min:1}",
    "A quoted literal can hold placeholders: {\"via {path}\"|hops_min:1}",
    "An unknown placeholder renders as nothing.",
]


def template_spec(
    syntax: str,
    placeholders: tuple[str, ...] | list[str] | dict[str, str],
    *,
    notes: Optional[list[str]] = None,
    escapes: bool = True,
    samples: Optional[dict[str, Any]] = None,
    blank_default: Optional[str] = None,
    blank_config: Optional[tuple[str, str]] = None,
    preview: Optional[str] = None,
) -> dict[str, Any]:
    """Build the ``"template"`` attribute of a settings_schema field.

    ``placeholders`` is a sequence of names from :data:`ALL_PLACEHOLDERS`, or a
    ``{name: description}`` mapping for fields with their own set (greeter mesh
    info, MQTT weather). ``escapes`` says whether ``\\n`` in the value becomes a
    newline. ``samples`` overrides preview values for this field, as
    ``{name: value_or_{scenario_id: value}}``. ``blank_default`` is the
    template the plugin uses when the field is blank, which the preview renders
    in that case; ``blank_config`` is a ``(section, key)`` the plugin tries
    first. ``preview`` names the :data:`FORMAT_PREVIEWS` renderer for a
    ``format`` field; piped fields always preview.
    """
    if preview is not None and preview not in FORMAT_PREVIEWS:
        raise ValueError(f"unknown format preview {preview!r}")
    if syntax not in ("piped", "format"):
        raise ValueError(f"unknown template syntax {syntax!r}")
    if isinstance(placeholders, dict):
        entries = [{"name": n, "help": h} for n, h in placeholders.items()]
    else:
        entries = [{"name": n, "help": ALL_PLACEHOLDERS[n][0]} for n in placeholders]
    spec: dict[str, Any] = {
        "syntax": syntax,
        "placeholders": entries,
        "notes": list(notes or []),
        "escapes": escapes,
    }
    if samples:
        spec["samples"] = samples
    if blank_default is not None:
        spec["blank_default"] = blank_default
    if blank_config is not None:
        spec["blank_config"] = list(blank_config)
    if syntax == "piped":
        spec["filters"] = PIPED_FILTERS
        spec["syntax_notes"] = PIPED_SYNTAX_NOTES
        spec["previewable"] = True
        spec["sample_note"] = "Sample values from a message by Alice."
    elif preview:
        spec["preview"] = preview
        spec["previewable"] = True
        spec["sample_note"] = FORMAT_PREVIEWS[preview][1]
    return spec


def _scenario_message(scenario: dict[str, Any]) -> MeshMessage:
    nodes = scenario["nodes"]
    routing_info: dict[str, Any] = {"path_length": len(nodes), "path_nodes": list(nodes)}
    if scenario["bytes_per_hop"]:
        routing_info["bytes_per_hop"] = scenario["bytes_per_hop"]
    return MeshMessage(
        content="test radio check",
        sender_id="Alice",
        hops=len(nodes),
        snr=9.5,
        rssi=-98,
        routing_info=routing_info,
    )


def _sample(value: Any, scenario_id: str) -> str:
    if isinstance(value, dict):
        return str(value.get(scenario_id, ""))
    return str(value)


def _row(row_id: str, label: str, output: str, error: Optional[str] = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": row_id,
        "label": label,
        "output": output,
        "bytes": len(output.encode("utf-8")),
    }
    if error:
        row["error"] = error
    return row


def _strip_quotes(text: str) -> str:
    """BaseCommand._strip_quotes_from_config: drop one pair of surrounding double quotes."""
    if text and text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    return text


# A preview renders str.format specs for real, and `{sender:999999999}` would build
# a gigabyte string; the endpoint is admin-only but still shouldn't be a memory lever.
_MAX_FORMAT_WIDTH = 500
_FORMAT_SPEC_RE = re.compile(r"\{[^{}:]*:([^{}]*)\}")


def _oversized_format_spec(text: str) -> bool:
    return any(
        int(number) > _MAX_FORMAT_WIDTH
        for spec in _FORMAT_SPEC_RE.findall(text)
        for number in re.findall(r"\d+", spec)
    )


Preview = dict[str, Any]
Renderer = Callable[[dict[str, Any], str, Optional[configparser.ConfigParser]], Preview]


def _preview(rows: list[dict[str, Any]], note: Optional[str] = None) -> Preview:
    return {"scenarios": rows, "note": note}


def render_preview(
    spec: dict[str, Any],
    template: str,
    config: Optional[configparser.ConfigParser] = None,
) -> Preview:
    """Render a template field as its plugin would, against sample data.

    Returns ``{"scenarios": rows, "note": text_or_None}``. Each row is
    ``{id, label, output, bytes}``, plus ``error`` when the plugin would hit a
    problem; ``output`` is then what the plugin sends instead. ``note`` says when
    the preview shows a fallback (a blank field) rather than the template.
    ``config`` is the bot's current configuration, for the settings beyond the
    template that change the output (a [Keywords] fallback, the multitest path
    layout, the MQTT length limit); without it the defaults apply.
    """
    if spec.get("syntax") == "piped":
        return _preview_piped(spec, template, config)
    renderer = FORMAT_PREVIEWS.get(spec.get("preview") or "")
    if renderer is None:
        raise ValueError("this template field has no preview")
    if _oversized_format_spec(template):
        return _preview([_row("template", "Template", "",
                              f"A format width or precision over {_MAX_FORMAT_WIDTH} isn't previewed.")])
    return renderer[0](spec, template, config)


def _config_get(config: Optional[configparser.ConfigParser], section: str, key: str) -> Optional[str]:
    if config is None or not config.has_section(section) or not config.has_option(section, key):
        return None
    return config.get(section, key, raw=True)


def _preview_piped(spec: dict[str, Any], template: str, config: Optional[configparser.ConfigParser]) -> Preview:
    """Render a piped template against every sample scenario.

    Mirrors what the commands do before rendering: surrounding double quotes are
    stripped, a blank result falls back (to ``blank_config`` in the bot's config,
    then ``blank_default``) and, when the field takes them, escape sequences are
    decoded. The ``shorten`` filter gets no config, so it passes the URL through
    instead of calling the shortener.
    """
    text = _strip_quotes(template.strip()).strip()
    note = None
    if not text:
        fallback_key = spec.get("blank_config")
        configured = _config_get(config, *fallback_key) if fallback_key else None
        if configured:
            text = _strip_quotes(configured)
            note = f"The field is blank, so the bot uses [{fallback_key[0]}] {fallback_key[1]}."
        elif spec.get("blank_default"):
            text = spec["blank_default"]
            note = "The field is blank, so this is the built-in default."
    if spec.get("escapes", True):
        text = decode_escape_sequences(text)

    overrides = spec.get("samples") or {}
    names = [p["name"] for p in spec.get("placeholders", [])]
    results = []
    for scenario in SCENARIOS:
        sid = scenario["id"]
        fields = {}
        for name in names:
            override = overrides.get(name)
            if override is not None and (not isinstance(override, dict) or sid in override):
                fields[name] = _sample(override, sid)
            elif name in ALL_PLACEHOLDERS:
                fields[name] = _sample(ALL_PLACEHOLDERS[name][1], sid)
        output = format_piped_template(text, fields, message=_scenario_message(scenario))
        results.append(_row(sid, scenario["label"], output))
    return _preview(results, note)


# --- str.format fields ------------------------------------------------------

_SAVES_DEFAULT = "The field is blank; saving it stores the default, shown here."


def _blank_or(spec: dict[str, Any], template: str) -> tuple[str, Optional[str]]:
    """The template a blank field saves as (the schema default), with a note saying so."""
    text = template.strip()
    if text or not spec.get("blank_default"):
        return text, None
    return spec["blank_default"], _SAVES_DEFAULT


def _describe_format_error(exc: Exception) -> str:
    """Say what is wrong with a str.format template, in the operator's terms."""
    if isinstance(exc, KeyError):
        return f"Unknown placeholder {{{exc.args[0]}}}"
    if isinstance(exc, IndexError):
        return "Numbered placeholders such as {0} aren't supported"
    if isinstance(exc, ValueError):
        return f"Unbalanced braces ({exc}); write {{{{ or }}}} for a literal brace"
    return f"Template error: {exc}"


def _greeting_rows(text: str, row_id: str, label: str) -> list[dict[str, Any]]:
    """Rows for one greeting template, as GreeterCommand formats and sends it."""
    from .commands.greeter_command import split_greeting_parts

    parts = split_greeting_parts(text)
    try:
        formatted = [part.format(sender="Alice") for part in parts]
    except Exception as exc:  # noqa: BLE001 - mirrors the greeter's catch-all
        return [_row(row_id, label, "", f"{_describe_format_error(exc)}. The greeter sends no greeting.")]
    if len(formatted) == 1:
        return [_row(row_id, label, formatted[0])]
    return [
        _row(f"{row_id}-{i}", f"{label}, message {i} of {len(formatted)}", part)
        for i, part in enumerate(formatted, 1)
    ]


def _preview_greeting(spec: dict[str, Any], template: str, config: Optional[configparser.ConfigParser]) -> Preview:
    text, note = _blank_or(spec, template)
    return _preview(_greeting_rows(decode_escape_sequences(text), "greeting", "Greeting"), note)


def _preview_channel_greetings(
    spec: dict[str, Any], template: str, config: Optional[configparser.ConfigParser]
) -> Preview:
    from .commands.greeter_command import parse_channel_greetings

    greetings, ignored = parse_channel_greetings(template.strip())
    rows = [
        _row(f"ignored-{i}", "Ignored", fragment.strip(),
             "Has no channel: in front and no entry before it to join, so the greeter skips it.")
        for i, fragment in enumerate(ignored)
    ]
    for key, entry in greetings.items():
        rows.extend(_greeting_rows(entry["greeting"], f"ch-{key}", entry["channel"]))
    note = None if rows else "No channel greetings: every channel gets the default greeting."
    return _preview(rows, note)


_MESH_INFO_SAMPLE = {"total_contacts": 412, "repeaters": 57, "companions": 331, "recent_activity_24h": 38}


def _preview_mesh_info(spec: dict[str, Any], template: str, config: Optional[configparser.ConfigParser]) -> Preview:
    text, note = _blank_or(spec, template)
    text = decode_escape_sequences(text)
    try:
        return _preview([_row("mesh_info", "Appended", text.format(**_MESH_INFO_SAMPLE))], note)
    except Exception as exc:  # noqa: BLE001 - the greeter catches everything here
        return _preview([_row("mesh_info", "Appended", "",
                              f"{_describe_format_error(exc)}. The greeting goes out without mesh info.")], note)


_MULTITEST_PATHS = ["a1,c3,e5", "a1,b7,e5", "a1,c3,f2"]


def _preview_multitest(spec: dict[str, Any], template: str, config: Optional[configparser.ConfigParser]) -> Preview:
    """As MultitestCommand._load_config and its reply: layout, decoding, default on error."""
    from .commands.multitest_command import _condense_path_lines, _parse_condense_paths_mode

    mode = _parse_condense_paths_mode(_config_get(config, "Multitest_Command", "condense_paths") or "true")
    paths = sorted(_MULTITEST_PATHS)
    paths_text = _condense_path_lines(paths, mode) if mode in ("flat", "nested") else "\n".join(paths)
    sample = {"sender": "Alice", "path_count": len(paths), "paths": paths_text, "listening_duration": 6}
    default = f"Paths({len(paths)}):\n{paths_text}"

    text = _strip_quotes(template.strip()).strip()
    if not text:
        return _preview([_row("reply", "Reply", default)], "The field is blank, so multitest uses its default format.")
    try:
        text = text.encode("latin-1").decode("unicode_escape")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    try:
        return _preview([_row("reply", "Reply", text.format(**sample))])
    except (KeyError, ValueError) as exc:
        return _preview([_row("reply", "Reply", default,
                              f"{_describe_format_error(exc)}. Multitest sends its default format instead.")])
    except Exception as exc:  # noqa: BLE001 - shows what reaches the command's caller
        return _preview([_row("reply", "Reply", "", f"{_describe_format_error(exc)}. The multitest reply fails.")])


_MQTT_SAMPLES = [
    ("full", "Full station reading", {
        "time": "2026-10-04 14:30:00", "device": "WS-2902", "temperature_F": 61.3, "humidity": 72,
        "dewpoint_f": 52.1, "pressure_hpa": 1016.4, "wind_current_kmh": 9.7, "wind_peak_kmh": 22.5,
        "wind_direction_deg": 225, "rain_today_in": 0.12, "battery_voltage_v": 3.1,
    }),
    ("partial", "Reading without humidity", {
        "time": "2026-10-04 14:30:00", "device": "WS-2902", "temperature_F": 61.3,
    }),
]


def _mqtt_reply_for_error(err: str) -> str:
    """The reply wx sends for a payload error (WeatherCommon._mqtt_weather_error_key, in English)."""
    try:
        path = Path(__file__).resolve().parent.parent / "translations" / "en.json"
        wx = json.loads(path.read_text(encoding="utf-8"))["commands"]["wx"]
    except (OSError, ValueError, KeyError):
        wx = {}
    if err in ("no_data", "empty_payload", "empty_after_sanitize"):
        return wx.get("mqtt_weather_no_data", err)
    return wx.get("mqtt_weather_payload_error", "{detail}").format(detail=err.replace("_", " "))


def _preview_mqtt_json(spec: dict[str, Any], template: str, config: Optional[configparser.ConfigParser]) -> Preview:
    """Through format_mqtt_weather_payload, with the configured length limit."""
    from .clients.mqtt_weather import format_mqtt_weather_payload, load_mqtt_weather_format_config

    text, note = _blank_or(spec, template)
    fmt = dataclasses.replace(
        load_mqtt_weather_format_config(config or configparser.ConfigParser()),
        output_mode="json_template",
        json_template=text,
        json_device_key="",
        json_device_value="",
    )
    rows = []
    for row_id, label, data in _MQTT_SAMPLES:
        out, err = format_mqtt_weather_payload(json.dumps(data).encode(), fmt)
        if err:
            rows.append(_row(row_id, label, _mqtt_reply_for_error(err), f"The template failed ({err}); wx sends this instead."))
        else:
            rows.append(_row(row_id, label, out or ""))
    return _preview(rows, note)


class _WarningCatcher:
    """Logger stand-in that keeps the warnings a renderer logs."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, msg: str, *args: Any) -> None:
        self.warnings.append(msg % args if args else msg)


def _temperature_preview(key: str, high: Optional[int], low: Optional[int], label: str) -> Renderer:
    """Preview one [Weather] temperature_*_format through utils.format_temperature_high_low."""

    def render(spec: dict[str, Any], template: str, config: Optional[configparser.ConfigParser]) -> Preview:
        from .utils import format_temperature_high_low

        text, note = _blank_or(spec, template)
        weather = configparser.ConfigParser(interpolation=None)
        weather.add_section("Weather")
        weather.set("Weather", key, text)
        catcher = _WarningCatcher()
        try:
            out = format_temperature_high_low(weather, high, low, "°F", logger=catcher)
        except Exception as exc:  # noqa: BLE001 - what escapes reaches the weather reply
            return _preview([_row(key, label, "", f"{_describe_format_error(exc)}. The weather reply fails.")], note)
        if catcher.warnings:
            # The template failed and the default went out; say why in the template's terms.
            try:
                text.format(high=high, low=low, units="°F", high_label="H", low_label="L")
                reason = "Template error"
            except Exception as exc:  # noqa: BLE001
                reason = _describe_format_error(exc)
            return _preview([_row(key, label, out, f"{reason}. The bot uses the default format instead.")], note)
        return _preview([_row(key, label, out)], note)

    return render


# name -> (renderer, description of the sample data shown under the preview)
FORMAT_PREVIEWS: dict[str, tuple[Renderer, str]] = {
    "greeting": (_preview_greeting, "Greeting a user named Alice. Mesh info, when on, is added to the last message."),
    "channel_greetings": (_preview_channel_greetings, "Greeting a user named Alice on each listed channel."),
    "mesh_info": (_preview_mesh_info, "Sample counts: 412 contacts, 57 repeaters, 331 companions, 38 active."),
    "multitest": (_preview_multitest, "Alice heard over 3 paths in 6 seconds, laid out per the saved Path layout."),
    "mqtt_json": (_preview_mqtt_json, "Two sample station payloads; the second has no humidity. "
                                      "Uses the saved passthrough length limit."),
    "temp_pair": (_temperature_preview("temperature_high_low_format", 68, 51, "High 68, low 51"),
                  "A day with a high of 68 and a low of 51 °F."),
    "temp_high": (_temperature_preview("temperature_high_only_format", 68, None, "High 68 only"),
                  "A day with only a high of 68 °F."),
    "temp_low": (_temperature_preview("temperature_low_only_format", None, 51, "Low 51 only"),
                 "A night with only a low of 51 °F."),
}
