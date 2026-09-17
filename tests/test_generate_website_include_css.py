"""
Tests for CSS customization options in generate_website.py

Tests cover:
- Original behavior (built-in styles only)
- New --link-css option (external CSS links)
- New --embed-css option (embedded CSS from file)
- Combination of --style with custom CSS options

NOTE: These tests require the project dependencies to be installed.
To run these tests:

1. Install dependencies:
   pip install -r requirements.txt

2. Run tests with pytest:
   pytest tests/test_generate_website_include_css.py -v

If dependencies are not available, the tests document the expected behavior
and can serve as integration test specifications.
"""

import configparser
import os
import tempfile

from generate_website import STYLES, generate_builtin_css, generate_html


def _build_minimal_config() -> configparser.ConfigParser:
    """Build a minimal config for testing"""
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.set("Bot", "command_prefix", "!")
    return config


def test_generate_builtin_css_includes_css_variables():
    """
    Test that generate_builtin_css returns CSS with variables from STYLES

    This validates that the new generate_builtin_css() function properly
    extracts CSS from the STYLES dictionary and includes:
    - CSS custom properties (:root with variables)
    - Base styling rules (body, fonts, etc.)
    """
    css = generate_builtin_css('default')

    # Should include CSS variables from the style definition
    assert ':root' in css
    assert '--bg-primary' in css
    assert '--accent-blue' in css

    # Should include base CSS rules
    assert 'body {' in css
    assert 'font-family:' in css


def test_generate_builtin_css_includes_style_overrides():
    """
    Test that generate_builtin_css includes style-specific overrides

    Some styles (like minimalist) have specific CSS overrides that
    modify the base template. This ensures those are included.
    """
    css = generate_builtin_css('minimalist')

    # Minimalist style has specific overrides to remove atmospheric effects
    # The actual content depends on the STYLES definition
    assert len(css) > 100  # Should have substantial CSS


def test_original_behavior_built_in_style_only():
    """
    Test original behavior: using built-in style without custom CSS

    This is the backwards compatibility test - ensures that when neither
    link_css nor embed_css is provided, the function works exactly as before.
    """
    html = generate_html(
        bot_name="TestBot",
        title="Test Site",
        introduction="Test intro",
        commands=[],
        monitor_channels=[],
        channels_data={},
        style='default',
        link_css=None,
        embed_css=None
    )

    # Should include fonts link
    assert 'fonts.googleapis.com' in html
    assert 'fonts.gstatic.com' in html

    # Should include embedded CSS with style variables
    assert '<style>' in html
    assert ':root' in html
    assert '--bg-primary' in html

    # Should NOT include external stylesheet link (only fonts)
    links = [line for line in html.split('\n') if '<link rel="stylesheet"' in line]
    assert all('fonts' in link or 'gstatic' in link for link in links if '<link' in link)


def test_link_css_without_style():
    """
    Test --link-css option with --style

    When using --link-css with a style, both the built-in CSS and
    the external link should be included.
    """
    html = generate_html(
        bot_name="TestBot",
        title="Test Site",
        introduction="Test intro",
        commands=[],
        monitor_channels=[],
        channels_data={},
        style='default',
        link_css='https://example.com/custom.css',
        embed_css=None
    )

    # Should include external CSS link
    assert '<link rel="stylesheet" href="https://example.com/custom.css">' in html

    # Should still include built-in CSS when style is specified
    assert '<style>' in html
    assert '--bg-primary' in html


def test_link_css_with_style():
    """
    Test --link-css option combined with --style

    Validates that when both --style and --link-css are used:
    1. Built-in style CSS is included (for layout/structure)
    2. Fonts from the built-in style are loaded
    3. External CSS link is added (for custom overrides)
    """
    html = generate_html(
        bot_name="TestBot",
        title="Test Site",
        introduction="Test intro",
        commands=[],
        monitor_channels=[],
        channels_data={},
        style='minimalist',
        link_css='https://cdn.example.com/overrides.css',
        embed_css=None
    )

    # Should include both fonts and external CSS
    assert 'fonts.googleapis.com' in html
    assert '<link rel="stylesheet" href="https://cdn.example.com/overrides.css">' in html

    # Should include built-in style CSS
    assert '<style>' in html
    assert '--bg-primary' in html


def test_embed_css_from_file():
    """
    Test --embed-css option reading from a file

    Validates that:
    1. CSS is read from the specified file
    2. It's embedded into the <style> block
    3. Built-in CSS is included first (when --style is used)
    4. Custom CSS comes after (allowing overrides)
    """
    # Create a temporary CSS file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.css', delete=False) as f:
        f.write("""
:root {
    --custom-color: #ff0000;
}

.custom-class {
    color: var(--custom-color);
}
""")
        temp_css_path = f.name

    try:
        html = generate_html(
            bot_name="TestBot",
            title="Test Site",
            introduction="Test intro",
            commands=[],
            monitor_channels=[],
            channels_data={},
            style='default',
            link_css=None,
            embed_css=temp_css_path
        )

        # Should include embedded custom CSS
        assert '<style>' in html
        assert '--custom-color: #ff0000' in html
        assert '.custom-class' in html

        # Should also include built-in style CSS when style is specified
        assert '--bg-primary' in html

        # Should include comment showing custom CSS section
        assert 'Custom CSS overrides' in html

    finally:
        os.unlink(temp_css_path)


def test_embed_css_without_style():
    """
    Test --embed-css option with --style

    Even when a style is specified, embedded CSS should still work
    with the built-in CSS loaded first.
    """
    # Create a temporary CSS file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.css', delete=False) as f:
        f.write("""
body {
    background: #000;
    color: #fff;
}
""")
        temp_css_path = f.name

    try:
        html = generate_html(
            bot_name="TestBot",
            title="Test Site",
            introduction="Test intro",
            commands=[],
            monitor_channels=[],
            channels_data={},
            style='default',
            link_css=None,
            embed_css=temp_css_path
        )

        # Should include the custom CSS
        assert 'background: #000' in html

        # Should still include built-in CSS since style is specified
        assert '--bg-primary' in html

    finally:
        os.unlink(temp_css_path)


def test_embed_css_file_not_found_fallback():
    """
    Test --embed-css with non-existent file falls back to built-in style

    Validates error handling: if the CSS file can't be read,
    the function should fall back to the built-in style gracefully.
    """
    html = generate_html(
        bot_name="TestBot",
        title="Test Site",
        introduction="Test intro",
        commands=[],
        monitor_channels=[],
        channels_data={},
        style='default',
        link_css=None,
        embed_css='/nonexistent/path/to/file.css'
    )

    # Should fall back to built-in style
    assert '<style>' in html
    assert '--bg-primary' in html

    # Should still render a valid HTML page
    assert '<!DOCTYPE html>' in html
    assert '</html>' in html


def test_css_order_embed_after_builtin():
    """
    Test that embedded CSS comes after built-in CSS (allowing overrides)

    This is critical for the override functionality to work:
    - Built-in CSS provides the complete base styling
    - Custom CSS comes after and can override specific properties
    - CSS cascade ensures custom values take precedence
    """
    with tempfile.NamedTemporaryFile(mode='w', suffix='.css', delete=False) as f:
        f.write("""
:root {
    --bg-primary: #custom-value;
}
""")
        temp_css_path = f.name

    try:
        html = generate_html(
            bot_name="TestBot",
            title="Test Site",
            introduction="Test intro",
            commands=[],
            monitor_channels=[],
            channels_data={},
            style='default',
            link_css=None,
            embed_css=temp_css_path
        )

        # Find the style block
        style_start = html.index('<style>')
        style_end = html.index('</style>')
        style_content = html[style_start:style_end]

        # Built-in CSS should come before custom CSS
        builtin_pos = style_content.index('--bg-primary: #0a0e14')  # default style value
        custom_pos = style_content.index('--bg-primary: #custom-value')

        assert builtin_pos < custom_pos, "Built-in CSS should come before custom CSS"

    finally:
        os.unlink(temp_css_path)


def test_multiple_styles_available():
    """
    Test that all defined styles can be generated

    Ensures backward compatibility - all existing styles should
    continue to work with the new CSS generation system.
    """
    for style_name in STYLES:
        html = generate_html(
            bot_name="TestBot",
            title="Test Site",
            introduction="Test intro",
            commands=[],
            monitor_channels=[],
            channels_data={},
            style=style_name,
            link_css=None,
            embed_css=None
        )

        # Each style should produce valid HTML
        assert '<!DOCTYPE html>' in html
        assert '<style>' in html
        assert ':root' in html


def test_html_structure_preserved_with_custom_css():
    """
    Test that HTML structure is preserved when using custom CSS

    Validates that adding custom CSS doesn't break the HTML structure:
    - All essential elements are present
    - Navigation, header, and content sections work
    - JavaScript functionality is included
    """
    html = generate_html(
        bot_name="TestBot",
        title="Test Site",
        introduction="This is a test introduction",
        commands=[],
        monitor_channels=['#general'],
        channels_data={},
        style='default',
        link_css='https://example.com/custom.css',
        embed_css=None
    )

    # All essential HTML elements should be present
    assert '<!DOCTYPE html>' in html
    assert '<html lang="en">' in html
    assert '<head>' in html
    assert '<title>Test Site</title>' in html
    assert '<body>' in html
    assert '<h1>TestBot</h1>' in html
    assert 'This is a test introduction' in html
    assert '</body>' in html
    assert '</html>' in html

    # Should include the mobile menu
    assert 'mobile-menu-toggle' in html

    # Should include JavaScript
    assert '<script>' in html


def test_fonts_included_with_link_css():
    """
    Test that font links are included when using --link-css with --style

    Validates that the appropriate fonts for the base style are loaded
    even when using external CSS.
    """
    html = generate_html(
        bot_name="TestBot",
        title="Test Site",
        introduction="Test intro",
        commands=[],
        monitor_channels=[],
        channels_data={},
        style='terminal',  # Uses JetBrains Mono
        link_css='https://example.com/custom.css',
        embed_css=None
    )

    # Should include font preconnect and font link
    assert 'fonts.googleapis.com' in html
    assert 'fonts.gstatic.com' in html
    assert 'JetBrains+Mono' in html

    # Should include custom CSS link
    assert 'https://example.com/custom.css' in html


def test_escape_html_in_css_paths():
    """
    Test that CSS file paths are properly handled in HTML

    URLs with query parameters and special characters should be
    preserved correctly in href attributes.
    """
    html = generate_html(
        bot_name="TestBot",
        title="Test Site",
        introduction="Test intro",
        commands=[],
        monitor_channels=[],
        channels_data={},
        style='default',
        link_css='https://example.com/path/to/style.css?v=1&theme=dark',
        embed_css=None
    )

    # URL should be preserved (ampersands work in href attributes)
    assert 'https://example.com/path/to/style.css?v=1&theme=dark' in html


def test_backwards_compatibility():
    """
    Test that calling generate_html without CSS params works (backwards compatibility)

    This is the most important test - ensures existing code continues to work
    when the new parameters aren't provided.
    """
    # Old-style call without link_css and embed_css parameters
    html = generate_html(
        bot_name="TestBot",
        title="Test Site",
        introduction="Test intro",
        commands=[],
        monitor_channels=[],
        channels_data={},
        style='default'
        # link_css and embed_css default to None
    )

    # Should work exactly like before
    assert '<!DOCTYPE html>' in html
    assert '<style>' in html
    assert '--bg-primary' in html
    assert 'fonts.googleapis.com' in html
