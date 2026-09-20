"""Regional flood scope ("region code") names, shared by the bot and the viewer.

A MeshCore client with a region set sends channel messages as TC_FLOOD, whose
transport code confines them to repeaters holding that region's key. The key is
derived from the scope *name*, so the name is the whole setting: ``west`` and
``#west`` are the same region, and everything hashes the leading-``#`` form.

``[Channels] flood_scopes`` and ``[Channels] outgoing_flood_scope_override``
are edited from the web viewer, which runs in its own process and must not
import the bot's command machinery to check a name the operator typed. So the
normalization the bot has always used, and the rules for what survives a trip
through ``config.ini``, live here and both sides call in.
"""

from __future__ import annotations

from hashlib import sha256

# Scope values that mean "no region" — an ordinary FLOOD every repeater
# rebroadcasts. Empty is how config.ini spells "unset".
GLOBAL_MARKERS = ("", "*", "0", "None")

# A scope name is hashed whole, so there is no protocol limit; this is a sanity
# bound on what the UI will write into a comma-separated INI value.
MAX_SCOPE_NAME_LENGTH = 64

# The companion firmware stores its own default scope in a fixed 31-byte field
# and rejects a name that does not fit with its terminator
# (NodePrefs.default_scope_name, and the n > 0 && n < 31 check in
# CMD_SET_DEFAULT_FLOOD_SCOPE). That is 30 bytes of name, '#' included.
MAX_DEVICE_SCOPE_NAME_LENGTH = 30

# Characters that do not survive the round trip. ',' separates flood_scopes
# entries; '%' is read as an interpolation token by every later configparser
# read of the section; a newline ends the key's line and re-parses the rest as
# INI. '#' is the scope marker itself and only belongs at the front.
_FORBIDDEN_IN_NAME = {",", "%", "#"}


def normalize_scope_name(scope: str) -> str:
    """Return scope with '#' prepended if it is a non-global named region without one."""
    if scope in GLOBAL_MARKERS or scope.lower() == "none":
        if scope.lower() == "none":
            return "None"
        return scope
    if not scope.startswith("#"):
        return "#" + scope
    return scope


def is_global_marker(scope: str) -> bool:
    """True when ``scope`` means global flood rather than a named region."""
    return scope in GLOBAL_MARKERS or scope.lower() == "none"


def validate_scope_name(scope: str) -> str:
    """Return the canonical form of one scope, or raise ``ValueError``.

    Global markers come back canonicalised (``none`` → ``None``); a named
    region comes back with its leading ``#``.
    """
    raw = str(scope or "").strip()
    if is_global_marker(raw):
        return normalize_scope_name(raw)

    normalized = normalize_scope_name(raw)
    body = normalized[1:]
    if not body.strip():
        raise ValueError("A region scope needs a name after the '#'")
    if len(normalized) > MAX_SCOPE_NAME_LENGTH:
        raise ValueError(
            f"Region scope {normalized!r} is longer than {MAX_SCOPE_NAME_LENGTH} characters"
        )
    for char in body:
        if ord(char) < 32 or ord(char) == 127:
            raise ValueError(f"Region scope {raw!r} contains a control character")
        if char in _FORBIDDEN_IN_NAME:
            raise ValueError(
                f"Region scope {raw!r} cannot contain {char!r} — "
                "',' separates scopes, '%' breaks config.ini, and '#' only leads"
            )
    return normalized


def parse_scope_list(raw: str) -> list[str]:
    """Split a comma-separated ``flood_scopes`` value into its raw entries."""
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def split_allowlist(raw: str) -> tuple[list[str], bool]:
    """Return ``(named_scopes, allow_global)`` for a ``flood_scopes`` value.

    Mirrors ``CommandManager._load_flood_scope_keys``: global markers do not
    become keys, they just opt unscoped FLOOD through the allowlist. Duplicates
    collapse so the UI shows the list the bot actually matches against.
    """
    named: list[str] = []
    allow_global = False
    for entry in parse_scope_list(raw):
        normalized = normalize_scope_name(entry)
        if is_global_marker(normalized):
            allow_global = True
        elif normalized not in named:
            named.append(normalized)
    return named, allow_global


def format_scope_list(scopes: list[str] | tuple[str, ...]) -> str:
    """Join scopes into the comma-separated form ``flood_scopes`` expects."""
    return ", ".join(scopes)


def scope_key_hex(scope: str) -> str:
    """The 16-byte transport key a named scope hashes to, as hex.

    SHA-256 over the '#'-prefixed name, truncated to 16 bytes — the same
    derivation as the firmware's TransportKeyStore::getAutoKeyFor and the
    meshcore library, so a key read back from a radio can be compared with the
    name stored beside it.
    """
    normalized = normalize_scope_name(str(scope or "").strip())
    if is_global_marker(normalized):
        return ""
    return sha256(normalized.encode("utf-8")).hexdigest()[:32]


def validate_device_scope_name(scope: str) -> str:
    """Return the canonical form of a scope the radio can store, or raise.

    Stricter than :func:`validate_scope_name` on two counts, both of them the
    firmware's and the library's, not ours: the name has to fit the radio's
    31-byte field, and it has to be ASCII. The library pads the name frame by
    character count while encoding it as UTF-8, so a multi-byte character
    pushes the transport key past the field it belongs in and the radio stores
    a key that is not the one the name hashes to.
    """
    canonical = validate_scope_name(scope)
    if is_global_marker(canonical):
        return canonical
    encoded = canonical.encode("utf-8")
    if not canonical.isascii():
        raise ValueError(
            f"Region scope {canonical!r} must be ASCII to be stored on the radio"
        )
    if len(encoded) > MAX_DEVICE_SCOPE_NAME_LENGTH:
        raise ValueError(
            f"Region scope {canonical!r} is longer than "
            f"{MAX_DEVICE_SCOPE_NAME_LENGTH} characters, which is all the radio stores"
        )
    return canonical
