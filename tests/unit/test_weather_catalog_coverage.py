"""Every bundled language has the weather replies' own words, instead of falling back to English."""

import json
from pathlib import Path

import pytest

CATALOGS = sorted((Path(__file__).resolve().parents[2] / "translations").glob("*.json"))
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
REQUIRED = (
    [f"commands.gwx.day_abbrev.{day}" for day in DAYS]
    + [f"commands.{ns}.{key}" for ns in ("wx", "gwx") for key in ("hourly_not_available", "source_option_not_available")]
)


def _get(catalog, key):
    node = catalog
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


@pytest.mark.parametrize("path", CATALOGS, ids=lambda p: p.stem)
def test_weather_keys_are_translated(path):
    catalog = json.loads(path.read_text(encoding="utf-8"))
    assert [key for key in REQUIRED if _get(catalog, key) is None] == []


@pytest.mark.parametrize("path", CATALOGS, ids=lambda p: p.stem)
def test_wind_directions_are_complete_where_present(path):
    directions = _get(json.loads(path.read_text(encoding="utf-8")), "common.wind_directions")
    if directions is not None:  # Polish keeps the international letters from the English catalog
        assert len(directions) == 16 and len(set(directions.values())) == 16
