"""The Nix package must install every runtime dependency in requirements.txt.

nix/packages.nix sets dontCheckRuntimeDeps, so a dependency missing from its
propagatedBuildInputs builds fine and only fails when the bot imports it.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# requirements.txt name -> attribute name in nix/packages.nix, where they differ.
NIX_NAMES = {
    "asyncio-mqtt": "aiomqtt",  # renamed upstream; nixpkgs only has the new name
    "meshcore-cli": "meshcore-cli-fixed",  # local derivation with relaxed pins
}

# Declared but not needed under Nix: the standard library already provides it.
# tzfpy is a Rust crate that nixpkgs does not carry; the weather time zone helper
# uses timezonefinder instead, which nix/packages.nix adds on its own, and falls
# back to the longitude estimate if neither is importable.
NOT_PACKAGED = {"configparser", "tzfpy"}


def _requirement_names() -> set[str]:
    names = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        assert match is not None, f"unparsable requirement: {line!r}"
        names.add(match.group(0).lower().replace("_", "-"))
    return names


def _nix_bot_inputs() -> set[str]:
    text = (ROOT / "nix" / "packages.nix").read_text(encoding="utf-8")
    start = text.index("packages.default")
    block = text[start:]
    block = block[block.index("propagatedBuildInputs"):]
    block = block[: block.index("];\n\n")]
    names = set()
    for line in block.splitlines():
        line = line.split("#", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z0-9._-]+", line):
            names.add(line.lower())
    return names


def test_nix_package_carries_the_time_zone_lookup() -> None:
    """tzfpy is not in nixpkgs, so the Nix build needs its stand-in."""
    assert "timezonefinder" in _nix_bot_inputs()


def test_nix_package_installs_every_runtime_requirement() -> None:
    inputs = _nix_bot_inputs()
    assert "meshcore" in inputs  # guard against the parser silently finding nothing
    missing = sorted(
        name
        for name in _requirement_names() - NOT_PACKAGED
        if NIX_NAMES.get(name, name) not in inputs
    )
    assert not missing, f"requirements.txt packages missing from nix/packages.nix: {missing}"
