"""Regression coverage for the admin viewer's API-to-DOM trust boundary."""

import configparser
import logging
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from modules.web_viewer.app import BotDataViewer

_WEB_VIEWER = Path(__file__).resolve().parents[1] / "modules" / "web_viewer"
TEMPLATES = _WEB_VIEWER / "templates"
STATIC = _WEB_VIEWER / "static"


def _source(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _static_source(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _fake_setup_logging(viewer: BotDataViewer) -> None:
    viewer.logger = logging.getLogger("test_web_viewer_xss_hardening")
    viewer.logger.addHandler(logging.NullHandler())


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("viewer_xss")
    db_path = tmp / "viewer.db"
    config_path = tmp / "config.ini"
    config = configparser.ConfigParser()
    config["Connection"] = {
        "connection_type": "serial",
        "serial_port": "/dev/ttyUSB0",
    }
    config["Bot"] = {
        "bot_name": "TestBot",
        "db_path": str(db_path),
        "prefix_bytes": "1",
    }
    config["Channels"] = {"monitor_channels": "general"}
    config["Web_Viewer"] = {"multibyte_monitor_enabled": "true"}
    with config_path.open("w", encoding="utf-8") as handle:
        config.write(handle)

    with (
        patch.object(BotDataViewer, "_setup_logging", _fake_setup_logging),
        patch.object(BotDataViewer, "_start_database_polling", lambda self: None),
        patch.object(BotDataViewer, "_start_log_tailing", lambda self: None),
        patch.object(BotDataViewer, "_start_cleanup_scheduler", lambda self: None),
        patch.object(BotDataViewer, "_start_dashboard_refresher", lambda self: None),
    ):
        viewer = BotDataViewer(db_path=str(db_path), config_path=str(config_path))
    viewer.app.config["TESTING"] = True
    with viewer.app.test_client() as test_client:
        yield test_client


def test_dashboard_has_no_html_parser_sink_for_api_or_mesh_values() -> None:
    # The dashboard's script now lives in a static file (no CSP nonce needed),
    # so the trust boundary to police moved with it.
    template = _source("index.html")
    script = _static_source("js/dashboard.js")
    assert ".innerHTML" not in template
    assert ".innerHTML" not in script
    # Representative attacker-controlled sender, channel/title, path, client,
    # and role values all terminate in textContent-backed DOM construction.
    for safe_sink in (
        "node.textContent = String(value ?? '');",
        "makeTextElement('div', item.name, 'neighbor-row__name')",
        "makeTextElement('span', name, 'fw-semibold top-list-row__name')",
        "label.title = String(name ?? '');",
        "makeTextElement('code', item.path_string, 'small text-break')",
        "idCell.appendChild(makeTextElement('code', client.client_id))",
        "makeTextElement('div', name, 'mix-row__name')",
    ):
        assert safe_sink in script, safe_sink


def test_feed_metadata_preview_details_and_alerts_use_text_nodes() -> None:
    source = _source("feeds.html")
    # Payload classes include markup, event attributes, attribute breakouts,
    # script URLs, and malformed-looking close tags. They must never be placed
    # in a template literal that an HTML parser consumes.
    unsafe_fragments = (
        "tbody.innerHTML = this.feeds.map",
        "${feed.feed_name",
        "${feed.channel_name}",
        "${item.formatted}",
        "${feed.output_format.replace",
        "alert.innerHTML = `${message}",
        "onclick=\"feedManager.",
    )
    assert all(fragment not in source for fragment in unsafe_fragments)
    for safe_sink in (
        "addTextCell(feed.feed_name || String(feed.feed_url || '').substring(0, 30))",
        "addTextCell(feed.channel_name)",
        "formatted.textContent = String(item.formatted ?? '')",
        "format.textContent = String(feed.output_format)",
        "messageNode.textContent = String(message ?? '')",
    ):
        assert safe_sink in source


def test_restore_backup_rows_do_not_interpolate_names_or_paths() -> None:
    source = _source("config.html")
    restore_source = source.split("class RestoreManager", 1)[1].split(
        "class PurgeManager", 1
    )[0]
    assert "tr.innerHTML" not in restore_source
    assert "onclick=" not in restore_source
    assert "small.textContent = String(value ?? '')" in restore_source
    assert "this.pathEl.value = String(b.path ?? '')" in restore_source
    assert "active database remains unchanged" in source
    assert "Restart the complete MeshCore Bot service" in source


def test_config_api_data_and_errors_do_not_reach_inner_html() -> None:
    source = _source("config.html")
    unsafe_fragments = (
        "this.tbody.innerHTML = jobs.map",
        "this.tbody.innerHTML = `<tr><td colspan=\"3\"",
        "this.resultsBody.innerHTML = Object.entries(deleted)",
        "infoElement.innerHTML = this.dbData.tables.map",
        "tablesInfo.innerHTML = `<div class=\"alert alert-danger\">${message}",
    )
    assert all(fragment not in source for fragment in unsafe_fragments)
    for safe_sink in (
        "outcome.title = String(j.outcome)",
        "cell.textContent = `Failed to load: ${err.message}`",
        "code.textContent = String(tableName)",
        "heading.append(icon, document.createTextNode(String(table.name ?? '')))",
        "alert.textContent = String(message ?? '')",
    ):
        assert safe_sink in source


def test_inherited_notification_message_uses_text_content() -> None:
    source = _source("base.html")
    assert "${message}" not in source
    assert "notificationText.textContent = String(message ?? '')" in source


def test_legacy_admin_pages_encode_or_text_render_untrusted_values() -> None:
    contacts = _source("contacts.html")
    assert "return `${contact.city}, ${contact.state}`" not in contacts
    assert "this.escapeHtml(contact.city)" in contacts
    assert "this.escapeHtml(displayText)" in contacts
    assert "customTooltip.textContent = pathContent" in contacts
    assert "text.textContent = `Error: ${String(message ?? '')}`" in contacts
    assert "previewText.textContent = `${data.count} contact(s)" in contacts
    assert "onclick=\"contactsManager" not in contacts

    greeter = _source("greeter.html")
    assert "tableBody.innerHTML = users.map" not in greeter
    assert "button.addEventListener('click', () => this.ungreetUser" in greeter
    assert "sample_greeting" in greeter and "makeTextElement(" in greeter

    multibyte = _source("multibyte_rollout.html")
    assert "tbody.innerHTML = list.map" not in multibyte
    assert "tbody.innerHTML = rows.map" not in multibyte
    assert "info.title = tipText" in multibyte

    mesh = _source("mesh.html")
    assert "nodeDetailsBody').innerHTML" not in mesh
    assert "nodeDetailsBody').replaceChildren(table)" in mesh

    plugins = _source("plugins.html")
    assert "header.innerHTML = `" not in plugins
    assert "description.textContent = String(plugin.description ?? '')" in plugins


def test_nonce_hardened_templates_have_no_inline_event_attributes() -> None:
    event_attribute = re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE)
    for template_name in (
        "base.html",
        "index.html",
        "feeds.html",
        "config.html",
        "radio.html",
        "realtime.html",
        "contacts.html",
        "plugins.html",
        "greeter.html",
        "logs.html",
        "multibyte_rollout.html",
        "mesh.html",
        "api_explorer.html",
    ):
        assert not event_attribute.search(_source(template_name)), template_name


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/feeds",
        "/config",
        "/radio",
        "/realtime",
        "/contacts",
        "/plugins",
        "/greeter",
        "/logs",
        "/multibyte-rollout",
        "/mesh",
        "/api-explorer",
    ],
)
def test_csp_nonce_matches_every_rendered_inline_script(client, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    csp = response.headers["Content-Security-Policy"]
    script_directive = next(
        directive.strip()
        for directive in csp.split(";")
        if directive.strip().startswith("script-src ")
    )
    assert "'unsafe-inline'" not in script_directive
    nonce_match = re.search(r"'nonce-([^']+)'", script_directive)
    assert nonce_match, csp
    expected_nonce = nonce_match.group(1)

    html = response.get_data(as_text=True)
    # A block with a non-JavaScript type is a data island, not code: the browser
    # never executes it, so CSP does not gate it and it needs no nonce.
    data_block = re.compile(r'\btype\s*=\s*"(?!(?:text/javascript|module)\b)[^"]+"', re.IGNORECASE)
    inline_scripts = [
        attributes
        for attributes in re.findall(r"<script\b([^>]*)>", html, re.IGNORECASE)
        if not re.search(r"\bsrc\s*=", attributes, re.IGNORECASE)
        and not data_block.search(attributes)
    ]
    assert inline_scripts
    for attributes in inline_scripts:
        rendered_nonce = re.search(r'\bnonce="([^"]+)"', attributes)
        assert rendered_nonce, attributes
        assert rendered_nonce.group(1) == expected_nonce


def test_nonce_is_unique_per_response(client) -> None:
    nonces = []
    for _ in range(2):
        csp = client.get("/").headers["Content-Security-Policy"]
        nonces.append(re.search(r"'nonce-([^']+)'", csp).group(1))
    assert nonces[0] != nonces[1]


@pytest.mark.parametrize(
    ("payload_class", "payload"),
    [
        ("channel", '"><img src=x onerror="window.channelOwned=1">'),
        ("message", "</div><script>window.messageOwned=1</script>"),
        ("path", "' onmouseover='window.pathOwned=1"),
        ("error", "<svg/onload=window.errorOwned=1>"),
    ],
)
@pytest.mark.parametrize("template_name", ["radio.html", "realtime.html"])
def test_radio_and_realtime_payload_classes_never_reach_an_html_parser(
    template_name: str,
    payload_class: str,
    payload: str,
) -> None:
    """The representative payload is documentation for each trust class.

    These screens receive the values asynchronously, so the regression guard
    is static: there must be no API-to-parser sink capable of interpreting any
    payload as markup, regardless of its exact spelling.
    """
    assert payload_class
    assert payload
    source = _source(template_name)
    for parser_sink in (
        ".innerHTML",
        ".outerHTML",
        "insertAdjacentHTML",
        "document.write",
        ".onclick",
    ):
        assert parser_sink not in source, (template_name, payload_class, parser_sink)


def test_radio_dynamic_channel_feed_key_and_error_values_use_dom_text_nodes() -> None:
    source = _source("radio.html")
    for safe_sink in (
        "cell.textContent = String(value ?? '')",
        "link.textContent = String(feed.feed_name || feed.feed_url || 'Unnamed feed')",
        "code.textContent = String(channelKey)",
        "messageNode.textContent = String(message ?? '')",
        "viewButton.addEventListener('click'",
        "deleteButton.addEventListener('click'",
    ):
        assert safe_sink in source


def test_all_direct_radio_pollers_treat_interrupted_as_terminal() -> None:
    source = _source("radio.html")

    assert source.count("result.status === 'interrupted'") == 2
    assert "data.status === 'interrupted'" in source


def test_realtime_mesh_user_message_path_and_error_values_use_dom_text_nodes() -> None:
    source = _source("realtime.html")
    for safe_sink in (
        "element.textContent = String(text)",
        "chip.appendChild(document.createTextNode(String(channel)))",
        "makeElement('span', 'rt-who', msg.sender || '?')",
        "makeElement('span', 'rt-text', stripLeadingChannelBracketTag(msg.content || '', isDm))",
        "makeElement('span', 'rt-who', cmd.user || 'Unknown')",
        "makeElement('p', 'rt-reply-text', cmd.response || 'No response')",
        "makeElement('span', 'rt-path', path.join(' › '))",
        "document.getElementById('decoder-error-message').textContent = decodeError",
    ):
        assert safe_sink in source
