"""Compact node names for packing a route into one mesh message.

``Black Hills Repeater 🗻`` becomes ``BlackHls``: accents are folded to ASCII,
emoji and punctuation dropped, filler words ("repeater", "the") removed, an
amateur callsign dropped when the name has another word (one operator often
runs many repeaters: N7JMV-EVERETT-RP → Everett), and the words run together in CamelCase so the word breaks survive without spaces.
Conventional place-name abbreviations (Hills → Hls, Summit → Sum, Mountain → Mtn)
always apply; anything still over the length budget is squeezed in stages that
keep the name readable as long as possible:

1. compass words and other secondary abbreviations (North → N, Upper → Up)
2. "Mesh", which usually names the network rather than the node, while
   another word is left
3. interior lowercase vowels, longest word first (Kalama → Klm)
4. one character at a time off the longest word, digits last, since a number
   (G3, 2) is usually what tells two nearby repeaters apart

:func:`pack_names` then fits a whole route into a byte budget by lowering the
per-name length until the joined string fits.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Always applied: standard place-name abbreviations (mostly USPS street-suffix
# style), which read naturally in a name that is about a place.
PRIMARY_ABBREVIATIONS: dict[str, str] = {
    "avenue": "Ave",
    "beach": "Bch",
    "bridge": "Brg",
    "butte": "Bt",
    "canyon": "Cyn",
    "center": "Ctr",
    "centre": "Ctr",
    "county": "Co",
    "creek": "Crk",
    "downtown": "Dtwn",
    "estates": "Ests",
    "falls": "Fls",
    "forest": "Frst",
    "fort": "Ft",
    "gardens": "Gdns",
    "harbor": "Hbr",
    "harbour": "Hbr",
    "heights": "Hts",
    "highway": "Hwy",
    "hill": "Hl",
    "hills": "Hls",
    "hollow": "Holw",
    "island": "Is",
    "junction": "Jct",
    "lake": "Lk",
    "lakes": "Lks",
    "lookout": "Lkt",
    "meadow": "Mdw",
    "meadows": "Mdws",
    "mount": "Mt",
    "mountain": "Mtn",
    "mountains": "Mtns",
    "observatory": "Obs",
    "park": "Pk",
    "peak": "Pk",
    "point": "Pt",
    "prairie": "Pr",
    "ranch": "Rnch",
    "ridge": "Rdg",
    "river": "Riv",
    "road": "Rd",
    "saint": "St",
    "springs": "Spgs",
    "station": "Sta",
    "street": "St",
    "summit": "Sum",
    "tower": "Twr",
    "university": "Univ",
    "valley": "Vly",
    "village": "Vlg",
}

# Applied only when a name is still over budget: shorter, but "NBend" reads
# worse than "NorthBend" when there is room for the latter.
SECONDARY_ABBREVIATIONS: dict[str, str] = {
    "north": "N",
    "south": "S",
    "east": "E",
    "west": "W",
    "northeast": "NE",
    "northwest": "NW",
    "southeast": "SE",
    "southwest": "SW",
    "upper": "Up",
    "lower": "Lwr",
}

# Words that say nothing about which node this is. Dropped unless nothing
# else is left.
FILLER_WORDS = frozenset({
    "the", "of", "and", "at", "on",
    "repeater", "repeaters", "rptr", "rpt", "rep", "rp",
    "meshcore", "node",
})

# Dropped only when a name is over budget and another word remains.
NETWORK_WORDS = frozenset({"mesh"})

_VOWELS = frozenset("aeiou")
# An amateur callsign: one or two letters, a digit, one to four letters.
_CALLSIGN = re.compile(r"^[A-Za-z]{1,2}[0-9][A-Za-z]{1,4}$")
# Splits CamelCase and letter/digit runs: "KalamaSummit2" -> Kalama, Summit, 2;
# "WA7ABC" -> WA, 7, ABC.
_WORD_PARTS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")


def _ascii_fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def name_words(name: str) -> list[str]:
    """Split *name* into ASCII words with filler removed and casing normalized.

    Emoji, punctuation and anything without an ASCII fold are dropped. A name
    written in all caps (``BLACK HILLS``) is title-cased so CamelCase can show
    the word breaks; short all-caps words in a mixed-case name (``PDX``,
    ``WA``) are kept as written.
    """
    if not name:
        return []
    folded = _ascii_fold(name)
    raw = re.sub(r"[^0-9A-Za-z]+", " ", folded).split()
    all_caps = all(not w.isalpha() or w.isupper() for w in raw) and any(w.isalpha() for w in raw)
    words: list[str] = []
    for token in raw:
        if _CALLSIGN.match(token):
            words.append(token.upper())
            continue
        for part in _WORD_PARTS.findall(token) or [token]:
            if all_caps and part.isalpha() and len(part) > 3:
                part = part.capitalize()
            words.append(part[0].upper() + part[1:])
    kept = [w for w in words if w.lower() not in FILLER_WORDS]
    return kept or words


def _abbreviate(words: list[str], table: dict[str, str]) -> list[str]:
    return [table.get(w.lower(), w) for w in words]


def _total(words: list[str]) -> int:
    return sum(len(w) for w in words)


def _drop_vowels(word: str) -> str:
    """Remove lowercase vowels after the first character (Kalama -> Klm)."""
    return word[0] + "".join(c for c in word[1:] if c not in _VOWELS)


def shorten_name(name: str, max_length: int) -> str:
    """Return *name* compacted to at most *max_length* characters.

    Returns ``""`` when the name has nothing printable in ASCII (an emoji-only
    name), so the caller can fall back to something else.
    """
    words = _abbreviate(name_words(name), PRIMARY_ABBREVIATIONS)
    if not words or max_length <= 0:
        return ""
    if any(w.isalpha() for w in words if not _CALLSIGN.match(w)):
        words = [w for w in words if not _CALLSIGN.match(w)]
    if _total(words) <= max_length:
        return "".join(words)

    words = _abbreviate(words, SECONDARY_ABBREVIATIONS)
    if _total(words) <= max_length:
        return "".join(words)

    for index in reversed(range(len(words))):
        if words[index].lower() not in NETWORK_WORDS:
            continue
        rest = words[:index] + words[index + 1:]
        if any(w.isalpha() and w.lower() not in NETWORK_WORDS for w in rest):
            words = rest
            if _total(words) <= max_length:
                return "".join(words)

    # Longest word first, so the distinctive short tokens are the last touched.
    for index in sorted(range(len(words)), key=lambda i: -len(words[i])):
        word = words[index]
        if len(word) > 3 and not word.isupper():
            words[index] = _drop_vowels(word)
            if _total(words) <= max_length:
                return "".join(words)

    while _total(words) > max_length:
        trimmable = [i for i, w in enumerate(words) if len(w) > 1 and not w.isdigit()]
        if not trimmable:
            trimmable = [i for i, w in enumerate(words) if len(w) > 1]
        if not trimmable:
            break
        # Ties go to the later word: the first is usually the place name.
        index = max(trimmable, key=lambda i: (len(words[i]), i))
        words[index] = words[index][:-1]
    return "".join(words)[:max_length]


def pack_names(
    hops: list[tuple[Optional[str], str]],
    *,
    separator: str = "▸",
    max_name_length: int = 10,
    max_bytes: int = 0,
    min_name_length: int = 4,
    ellipsis: str = "…",
) -> str:
    """Join a route's hops into one compact string within *max_bytes*.

    Each hop is ``(name, fallback)``: *name* is shortened, and *fallback* (the
    hop's hex prefix) is used verbatim when there is no name or the name has
    nothing printable. Names start at *max_name_length* characters and shrink
    together, one character at a time down to *min_name_length*, until the
    joined string fits. If even that is too long, hops after the first are
    replaced by a single *ellipsis*, keeping the first hop and as many of the
    last ones (nearest the bot) as fit, each at the longest length that fits.
    ``max_bytes`` of 0 means no limit.
    """
    if not hops:
        return ""
    top = max(max_name_length, 1)
    floor = max(1, min(min_name_length, top))

    def render(subset: list[tuple[Optional[str], str]], length: int) -> list[str]:
        return [(shorten_name(name, length) if name else "") or fallback for name, fallback in subset]

    def fit(subset: list[tuple[Optional[str], str]], elide: bool) -> Optional[str]:
        for length in range(top, floor - 1, -1):
            parts = render(subset, length)
            if elide:
                parts = parts[:1] + [ellipsis] + parts[1:]
            text = separator.join(parts)
            if max_bytes <= 0 or len(text.encode("utf-8")) <= max_bytes:
                return text
        return None

    packed = fit(hops, elide=False)
    if packed is not None:
        return packed
    for keep in range(len(hops) - 2, 0, -1):
        packed = fit(hops[:1] + hops[-keep:], elide=True)
        if packed is not None:
            return packed
    # Nothing fits the budget: send the shortest form rather than nothing.
    if len(hops) == 1:
        return render(hops, floor)[0]
    return separator.join(render(hops[:1], floor) + [ellipsis] + render(hops[-1:], floor))
