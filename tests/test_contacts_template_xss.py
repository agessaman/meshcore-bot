#!/usr/bin/env python3
"""Regression tests for stored-XSS hardening in the web-viewer templates.

A malicious MeshCore node can broadcast an advertisement whose ``adv_name``
field contains arbitrary bytes (the protocol does no character validation).
That name is decoded and stored verbatim, then served to the browser by the
web viewer as ``contact.username`` / ``node.name`` / sample ``name`` values.
The pages render those values into the DOM, so every such value MUST pass
through the template's ``escapeHtml()`` helper before it reaches the DOM.
Otherwise a name like ``<img src=x onerror=alert(document.cookie)>`` executes
in the operator's browser (see
https://mxsasha.eu/posts/meshcore-xss-home-assistant/).

Two sink families are covered:

* ``contacts.html`` -- names rendered via ``innerHTML`` / ``insertAdjacentHTML``.
* ``mesh.html`` -- names placed in vis-network node ``title`` fields.  vis-network
  (9.x) renders a string ``title`` tooltip via ``innerHTML``, so an unescaped
  name fires on hover.  (Node ``label`` values are canvas-rendered text and are
  intentionally NOT escaped.)

There is no JavaScript test runner in this project (package.json only wires up
linting), so these tests assert the security invariant against the template
source: untrusted advert-name fields are only ever emitted into a DOM-rendering
sink through ``escapeHtml(...)``, and the advert name is never interpolated into
an ``onclick`` attribute.
"""

import re
from pathlib import Path

import pytest

TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent / "modules" / "web_viewer" / "templates"
)
TEMPLATE_PATH = TEMPLATES_DIR / "contacts.html"
MESH_TEMPLATE_PATH = TEMPLATES_DIR / "mesh.html"

# Template-literal interpolation: ${ ... } with no nested braces (sufficient for
# the expressions used in this template).
INTERPOLATION_RE = re.compile(r"\$\{([^{}]*)\}")

# Tokens that carry attacker-controlled advert names into the DOM.
UNTRUSTED_TOKENS = (
    "contact.username",
    "raw_advert_data",
    "s.name",  # purge-preview sample names
)


@pytest.fixture(scope="module")
def template_source() -> str:
    assert TEMPLATE_PATH.is_file(), f"missing template: {TEMPLATE_PATH}"
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def test_escape_helper_exists(template_source: str) -> None:
    """The template must define the escapeHtml helper the fixes rely on."""
    assert "escapeHtml(text)" in template_source


def test_untrusted_advert_fields_are_escaped_in_dom_interpolations(template_source):
    """Every DOM-rendered interpolation of an advert-name field is escaped.

    Iterates the actual ``${...}`` interpolations so the invariant survives
    line moves / reformatting, rather than matching brittle exact lines.
    """
    offenders = []
    for match in INTERPOLATION_RE.finditer(template_source):
        expr = match.group(1)
        if not any(token in expr for token in UNTRUSTED_TOKENS):
            continue
        if "escapeHtml(" not in expr:
            line_no = template_source.count("\n", 0, match.start()) + 1
            offenders.append(f"line {line_no}: ${{{expr.strip()}}}")

    assert not offenders, (
        "Unescaped advert-name field rendered into the DOM (stored XSS). "
        "Wrap each in this.escapeHtml(...):\n  " + "\n  ".join(offenders)
    )


def test_specific_known_sinks_are_escaped(template_source: str) -> None:
    """Guard the exact sinks identified in the audit against regressions."""
    assert "${this.escapeHtml(contact.username || 'Unknown')}" in template_source
    assert "${this.escapeHtml(s.name)}" in template_source
    # raw advert JSON blob is HTML-escaped before landing in the <pre>
    assert "this.escapeHtml(JSON.stringify(contact.raw_advert_data_parsed" in template_source
    # the raw (unescaped) forms must be gone
    assert "${contact.username || 'Unknown'}" not in template_source
    assert "`<em>${s.name}</em>`" not in template_source


def _split_top_level_args(arg_text: str) -> list:
    """Split a call's argument text on top-level commas (paren/bracket aware)."""
    args, depth, current = [], 0, []
    for ch in arg_text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(current))
            current = []
        else:
            current.append(ch)
    if "".join(current).strip():
        args.append("".join(current))
    return args


def _extract_calls(source: str, func: str) -> list:
    """Return the balanced-paren argument text of every ``func(...)`` call."""
    calls, marker = [], func + "("
    start = source.find(marker)
    while start != -1:
        i = start + len(marker)
        depth, buf = 1, []
        while i < len(source) and depth > 0:
            ch = source[i]
            if ch == "(":
                depth += 1
                buf.append(ch)
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
                buf.append(ch)
            else:
                buf.append(ch)
            i += 1
        calls.append("".join(buf))
        start = source.find(marker, i)
    return calls


def test_extract_calls_omits_balancing_close_paren() -> None:
    """Argument text must not include the call's closing parenthesis."""
    assert _extract_calls("foo(1, 2)", "foo") == ["1, 2"]
    assert _extract_calls("showDeleteConfirmation(userId)", "showDeleteConfirmation") == [
        "userId"
    ]
    assert _extract_calls("f(a(b))", "f") == ["a(b)"]
    assert _extract_calls("empty()", "empty") == [""]
    # A single-argument call must not capture the terminator as part of the arg.
    assert _extract_calls("only(x)", "only") != ["x)"]
    assert _extract_calls("only(x)", "only") == ["x"]


def test_advert_name_not_passed_through_onclick_attribute(template_source):
    """showDeleteConfirmation must not receive an advert name via an HTML attribute.

    escapeHtml() (textContent->innerHTML) does NOT encode quotes, so a name
    interpolated into onclick="...('${name}')" could break out of the attribute.
    The fix derives the name from the looked-up contact instead, so every call
    site passes only the userId.
    """
    calls = _extract_calls(template_source, "showDeleteConfirmation")
    assert calls, "expected showDeleteConfirmation references in template"
    for arg_text in calls:
        args = _split_top_level_args(arg_text)
        # Only the userId argument is allowed at any call site.
        assert len(args) <= 1, (
            "showDeleteConfirmation must take only userId; an advert name is "
            f"being interpolated into the call/attribute: {arg_text!r}"
        )
        assert "username" not in arg_text and ".name" not in arg_text, (
            f"advert name leaked into showDeleteConfirmation call: {arg_text!r}"
        )
    # The removed unsafe helper must not reappear.
    assert "nameJs" not in template_source


# ---------------------------------------------------------------------------
# mesh.html -- vis-network node tooltips
# ---------------------------------------------------------------------------

# Matches vis-network title assignments in either object form (``title: <expr>``)
# or local-variable form (``const title = <expr>;``), capturing the expression up
# to the line end / trailing comma.
TITLE_ASSIGNMENT_RE = re.compile(
    r"""(?:title\s*:|(?:const|let|var)\s+title\s*=)\s*(?P<expr>.+)""",
)

# Tokens that carry attacker-controlled advert names into a vis-network title.
MESH_NAME_TOKENS = ("node.name", "n.name")


@pytest.fixture(scope="module")
def mesh_source() -> str:
    assert MESH_TEMPLATE_PATH.is_file(), f"missing template: {MESH_TEMPLATE_PATH}"
    return MESH_TEMPLATE_PATH.read_text(encoding="utf-8")


def test_mesh_escape_helper_exists(mesh_source: str) -> None:
    """mesh.html must define the escapeHtml helper the tooltip fixes rely on."""
    assert "function escapeHtml(text)" in mesh_source


def test_mesh_node_titles_escape_advert_name(mesh_source: str) -> None:
    """Any node-name placed in a vis-network ``title`` (HTML tooltip) is escaped.

    vis-network renders a string ``title`` via innerHTML, so an unescaped
    ``node.name`` / ``n.name`` is stored XSS that fires on hover.
    """
    offenders = []
    for line_no, line in enumerate(mesh_source.splitlines(), start=1):
        m = TITLE_ASSIGNMENT_RE.search(line)
        if not m:
            continue
        expr = m.group("expr")
        if not any(token in expr for token in MESH_NAME_TOKENS):
            continue
        if "escapeHtml(" not in expr:
            offenders.append(f"line {line_no}: {line.strip()}")

    assert not offenders, (
        "Unescaped advert name rendered into a vis-network title tooltip "
        "(stored XSS on hover). Wrap the name in escapeHtml(...):\n  "
        + "\n  ".join(offenders)
    )


def test_mesh_known_title_sinks_are_escaped(mesh_source: str) -> None:
    """Guard the exact tooltip sinks identified in the audit against regressions."""
    assert "title: `${escapeHtml(node.name)} (${escapeHtml(node.prefix)})`" in mesh_source
    assert "const title = escapeHtml(n.name) + ' (' + escapeHtml(n.prefix) + ')';" in mesh_source
    assert "title: escapeHtml(node.name) + ' (' + escapeHtml(node.prefix) + ')'," in mesh_source
    # The raw (unescaped) forms must be gone.
    assert "title: `${node.name} (${node.prefix})`" not in mesh_source
    assert "const title = n.name + ' (' + n.prefix + ')';" not in mesh_source
    assert "title: node.name + ' (' + node.prefix + ')'," not in mesh_source


# ---------------------------------------------------------------------------
# realtime.html -- live message/packet stream
# ---------------------------------------------------------------------------

REALTIME_TEMPLATE_PATH = TEMPLATES_DIR / "realtime.html"

# Free-text mesh fields (advert name, message sender, message body) that must be
# escaped before reaching innerHTML.  Each ${...} interpolation referencing one
# of these must go through escapeHtml(...).
REALTIME_UNTRUSTED_TOKENS = (
    "data.advert_name",
    "data.sender",
    "bodyRaw",  # message body, assigned from data.content
)


@pytest.fixture(scope="module")
def realtime_source() -> str:
    assert REALTIME_TEMPLATE_PATH.is_file(), f"missing template: {REALTIME_TEMPLATE_PATH}"
    return REALTIME_TEMPLATE_PATH.read_text(encoding="utf-8")


def test_realtime_escape_helper_exists(realtime_source: str) -> None:
    """realtime.html must define an escapeHtml helper."""
    assert "function escapeHtml(" in realtime_source


def test_realtime_untrusted_fields_escaped_in_interpolations(realtime_source):
    """Every DOM interpolation of an advert name / message field is escaped."""
    offenders = []
    for match in INTERPOLATION_RE.finditer(realtime_source):
        expr = match.group(1)
        if not any(token in expr for token in REALTIME_UNTRUSTED_TOKENS):
            continue
        if "escapeHtml(" not in expr:
            line_no = realtime_source.count("\n", 0, match.start()) + 1
            offenders.append(f"line {line_no}: ${{{expr.strip()}}}")

    assert not offenders, (
        "Unescaped mesh field rendered into the live stream DOM (stored XSS):\n  "
        + "\n  ".join(offenders)
    )


def test_realtime_known_safe_sinks(realtime_source: str) -> None:
    """Lock in the audited escaping of advert name, sender, body, and channel."""
    # Advert name (the article's adv_name) -- escaped in both render paths.
    assert "escapeHtml(data.advert_name)" in realtime_source
    assert "<strong>Name:</strong> ${escapeHtml(name)}" in realtime_source
    # Message sender, body, and channel badge.
    assert "escapeHtml(data.sender || '?')" in realtime_source
    assert "escapeHtml(bodyRaw)" in realtime_source
    assert "escapeHtml(data.channel)" in realtime_source
    # Raw (unescaped) forms must be absent.
    assert "${data.advert_name}" not in realtime_source
    assert "${data.sender}" not in realtime_source
    assert "${data.sender || '?'}" not in realtime_source
    assert "${bodyRaw}" not in realtime_source
