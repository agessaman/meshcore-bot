"""Tests for compact node names (modules/name_shortener.py)."""

import pytest

from modules.name_shortener import name_words, pack_names, shorten_name


@pytest.mark.unit
class TestNameWords:
    def test_emoji_punctuation_and_filler_are_dropped(self):
        assert name_words("Kelso-G3 Repeater 📡") == ["Kelso", "G", "3"]

    def test_accents_fold_to_ascii(self):
        assert name_words("Café Montréal") == ["Cafe", "Montreal"]

    def test_all_caps_names_are_title_cased(self):
        assert name_words("BLACK HILLS RPTR") == ["Black", "Hills"]

    def test_short_caps_in_a_mixed_name_are_kept(self):
        assert name_words("PDX Downtown") == ["PDX", "Downtown"]

    def test_camel_case_input_splits_into_words(self):
        assert name_words("KalamaSummit") == ["Kalama", "Summit"]

    def test_filler_is_kept_when_it_is_the_whole_name(self):
        assert name_words("The Repeater") == ["The", "Repeater"]

    def test_emoji_only_name_has_no_words(self):
        assert name_words("🛰️🛰️") == []


@pytest.mark.unit
class TestShortenName:
    @pytest.mark.parametrize("name, expected", [
        ("Black Hills", "BlackHls"),
        ("Kalama Summit", "KalamaSum"),
        ("Kelso G3", "KelsoG3"),
        ("Tiger Mountain", "TigerMtn"),
    ])
    def test_place_words_always_abbreviate(self, name, expected):
        assert shorten_name(name, 10) == expected

    def test_compass_words_shorten_only_when_over_budget(self):
        assert shorten_name("North Bend", 10) == "NorthBend"
        assert shorten_name("North Bend Ridge", 10) == "NBendRdg"

    def test_vowels_go_before_letters(self):
        assert shorten_name("Kalama Summit", 6) == "KlmSum"

    def test_never_exceeds_the_limit(self):
        for limit in range(1, 15):
            assert len(shorten_name("Mount St. Helens Lookout Tower 7", limit)) <= limit

    def test_numbers_survive_trimming(self):
        assert shorten_name("Tiger Mountain Summit 2", 4).endswith("2")

    def test_emoji_only_name_is_empty(self):
        assert shorten_name("📡", 10) == ""

    def test_callsign_is_dropped_when_another_word_names_the_node(self):
        assert shorten_name("N7JMV-EVERETT-RP", 10) == "Everett"
        assert shorten_name("N7JMV Grass", 20) == "Grass"

    def test_callsign_is_kept_when_it_is_the_name(self):
        assert shorten_name("K9JEB Repeater", 10) == "K9JEB"

    def test_mesh_goes_before_vowels(self):
        assert shorten_name("Puget Mesh Cougar", 11) == "PugetCougar"

    def test_callsign_stays_when_it_is_the_only_identifier(self):
        assert shorten_name("WW7STR KC7BYF", 20) == "WW7STRKC7BYF"

    def test_all_caps_words_with_digits_are_title_cased(self):
        assert shorten_name("N7JMV-PAINE2-RP", 10) == "Paine2"


@pytest.mark.unit
class TestPackNames:
    HOPS = [("Black Hills", "a1b2"), ("Kalama Summit", "c3d4"), (None, "e5f6"), ("Kelso G3", "0102")]

    def test_unnamed_hops_keep_their_fallback(self):
        assert pack_names(self.HOPS) == "BlackHls▸KalamaSum▸e5f6▸KelsoG3"

    def test_emoji_only_name_falls_back(self):
        assert pack_names([("📡", "a1b2"), ("Kelso", "c3d4")]) == "a1b2▸Kelso"

    def test_names_shrink_to_fit_the_byte_budget(self):
        packed = pack_names(self.HOPS, max_bytes=30)
        assert len(packed.encode()) <= 30
        assert packed.count("▸") == 3

    def test_hops_after_the_first_are_elided_last(self):
        packed = pack_names(self.HOPS, max_bytes=20)
        assert len(packed.encode()) <= 20
        assert packed.startswith("Blc") and "…" in packed and packed.endswith("G3")

    def test_custom_separator(self):
        assert pack_names([("A", "aa"), ("B", "bb")], separator=" > ") == "A > B"

    def test_zero_budget_means_unlimited(self):
        hops = [("Kalama Summit", f"{i:04x}") for i in range(12)]
        assert pack_names(hops, max_bytes=0).count("KalamaSum") == 12

    def test_empty_route(self):
        assert pack_names([]) == ""
