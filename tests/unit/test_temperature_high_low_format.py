#!/usr/bin/env python3
"""Unit tests for format_temperature_high_low ([Weather] templates)."""

import configparser
from unittest.mock import Mock

import pytest

from modules.utils import format_temperature_high_low


@pytest.fixture
def cfg():
    c = configparser.ConfigParser()
    c.add_section("Weather")
    return c


def test_default_pair_format(cfg):
    assert format_temperature_high_low(cfg, 47, 33, "°F", None) == "H:47°F L:33°F"


def test_custom_pair_format(cfg):
    cfg.set("Weather", "temperature_high_low_format", "{high}{units}/{low}{units}")
    assert format_temperature_high_low(cfg, 47, 33, "°F", None) == "47°F/33°F"


def test_arrow_style_example(cfg):
    cfg.set("Weather", "temperature_high_low_format", "↓{low}°↑{high}{units}")
    assert format_temperature_high_low(cfg, 47, 33, "°F", None) == "↓33°↑47°F"


def test_high_only_default(cfg):
    assert format_temperature_high_low(cfg, 46, None, "°F", None) == "H:46°F"


def test_low_only_default(cfg):
    assert format_temperature_high_low(cfg, None, 38, "°F", None) == "L:38°F"


def test_custom_high_only(cfg):
    cfg.set("Weather", "temperature_high_only_format", "{high}{units}")
    assert format_temperature_high_low(cfg, 46, None, "°F", None) == "46°F"


def test_both_none_returns_empty(cfg):
    assert format_temperature_high_low(cfg, None, None, "°F", None) == ""


def test_bad_template_falls_back(mock_logger):
    c = configparser.ConfigParser()
    c.add_section("Weather")
    c.set("Weather", "temperature_high_low_format", "{bad}")
    out = format_temperature_high_low(c, 10, 5, "°C", mock_logger)
    assert out == "H:10°C L:5°C"
    mock_logger.warning.assert_called()
