"""Tests for the --link-css and --embed-css options in generate_website.py"""

import sys

import pytest

import generate_website
from generate_website import STYLES, generate_builtin_css, generate_html


def _render(**kwargs) -> str:
    return generate_html(
        bot_name="TestBot",
        title="Test Site",
        introduction="Test intro",
        commands=[],
        monitor_channels=[],
        channels_data={},
        **kwargs,
    )


def test_generate_builtin_css_includes_css_variables():
    css = generate_builtin_css('default')

    assert ':root' in css
    assert '--bg-primary' in css
    assert '--accent-blue' in css
    assert 'body {' in css


def test_generate_builtin_css_includes_style_overrides():
    css = generate_builtin_css('minimalist')

    assert STYLES['minimalist']['css_overrides'].strip() in css


def test_default_output_has_no_custom_css():
    html = _render(style='default')

    assert 'Custom CSS overrides' not in html
    stylesheets = [line for line in html.splitlines() if 'rel="stylesheet"' in line]
    assert len(stylesheets) == 1
    assert 'fonts.googleapis.com/css2' in stylesheets[0]


def test_backwards_compatible_call_without_css_params():
    html = generate_html("TestBot", "Test Site", "Test intro", [], [], {}, 'default')

    assert '<style>' in html
    assert '--bg-primary' in html


def test_link_css_follows_builtin_style():
    """The linked stylesheet must load after the <style> block to win the cascade."""
    html = _render(style='minimalist', link_css='https://cdn.example.com/overrides.css')

    link = '<link rel="stylesheet" href="https://cdn.example.com/overrides.css">'
    assert link in html
    assert html.index('</style>') < html.index(link) < html.index('</head>')
    assert 'fonts.googleapis.com' in html
    assert '--bg-primary' in html


def test_link_css_is_escaped():
    html = _render(style='default', link_css='style.css?v=1&theme=dark"><script>alert(1)</script>')

    assert 'href="style.css?v=1&amp;theme=dark&quot;&gt;&lt;script&gt;' in html
    assert '<script>alert(1)' not in html


def test_embed_css_follows_builtin_css():
    html = _render(style='default', custom_css=':root {\n    --bg-primary: #123456;\n}\n')

    style_block = html[html.index('<style>'):html.index('</style>')]
    builtin_pos = style_block.index('--bg-primary: #0a0e14')
    custom_pos = style_block.index('--bg-primary: #123456')
    assert builtin_pos < custom_pos
    assert 'Custom CSS overrides' in style_block


def test_embed_and_link_css_together():
    html = _render(
        style='default',
        link_css='overrides.css',
        custom_css='.command-card { border-radius: 0; }',
    )

    embedded_pos = html.index('.command-card { border-radius: 0; }')
    link_pos = html.index('<link rel="stylesheet" href="overrides.css">')
    assert embedded_pos < html.index('</style>') < link_pos


@pytest.mark.parametrize('style', list(STYLES))
def test_every_style_accepts_custom_css(style):
    html = _render(style=style, link_css='overrides.css', custom_css='.x { color: red; }')

    assert STYLES[style]['fonts_url'] in html
    assert '.x { color: red; }' in html
    assert 'href="overrides.css"' in html
    assert 'mobile-menu-toggle' in html


def test_main_exits_when_embed_css_is_unreadable(monkeypatch, tmp_path):
    def fail_read_config(*args, **kwargs):
        raise AssertionError("config should not be read when --embed-css is unreadable")

    monkeypatch.setattr(generate_website, 'read_config', fail_read_config)
    monkeypatch.setattr(sys, 'argv', ['generate_website.py', '--embed-css', str(tmp_path / 'missing.css')])

    with pytest.raises(SystemExit) as exc:
        generate_website.main()
    assert exc.value.code == 1


def test_main_passes_custom_css_to_samples(monkeypatch, tmp_path):
    css_file = tmp_path / 'tweaks.css'
    css_file.write_text(':root { --accent-blue: #2563eb; }', encoding='utf-8')
    captured = {}

    def fake_generate_samples(config_file, link_css=None, custom_css=None):
        captured.update(config_file=config_file, link_css=link_css, custom_css=custom_css)

    monkeypatch.setattr(generate_website, 'generate_samples', fake_generate_samples)
    monkeypatch.setattr(sys, 'argv', [
        'generate_website.py', 'my.ini', '--sample',
        '--link-css', 'overrides.css', '--embed-css', str(css_file),
    ])

    with pytest.raises(SystemExit) as exc:
        generate_website.main()
    assert exc.value.code == 0
    assert captured == {
        'config_file': 'my.ini',
        'link_css': 'overrides.css',
        'custom_css': ':root { --accent-blue: #2563eb; }',
    }
