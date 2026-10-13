"""Text normalisation tests (caption, hashtags, OCR text; English, Hindi, emoji). No database."""

import random
import unicodedata

import pytest

from app.worker.text_normalize import (
    NormalizeConfig,
    TextInput,
    normalize_fragment,
    normalize_text,
    split_hashtag,
)


def text_of(**kwargs: object) -> str:
    return normalize_text(TextInput(**kwargs)).text  # type: ignore[arg-type]


# ------------------------------------------------------------------ missing input


@pytest.mark.parametrize(
    "inp",
    [
        TextInput(),
        TextInput(caption=None, hashtags=None, ocr_text=None),
        TextInput(caption="", hashtags=(), ocr_text=""),
        TextInput(caption="   \n\t ", hashtags=("", "#")),
        TextInput(caption="!!! ... ???", hashtags=("123", "___")),
        TextInput(caption="12345 67890", ocr_text="%%%"),
        TextInput(caption="https://example.test/a @someone"),
    ],
)
def test_no_usable_text_gives_an_empty_result_not_a_default(inp: TextInput) -> None:
    result = normalize_text(inp)
    assert result.is_empty
    assert result.text == ""
    assert result.sources == ()
    assert result.hashtag_count == 0


def test_missing_caption_still_uses_hashtags_and_ocr() -> None:
    result = normalize_text(TextInput(caption=None, hashtags=("cricket", "IPL2026")))
    assert result.text == "cricket ipl 2026"
    assert result.sources == ("hashtags",)
    ocr_only = normalize_text(TextInput(ocr_text="Big Sale 50% off"))
    assert ocr_only.text == "Big Sale 50% off"
    assert ocr_only.sources == ("ocr",)


def test_unavailable_is_not_the_same_as_an_empty_list() -> None:
    assert normalize_text(TextInput(caption="hello world", hashtags=None)).text == "hello world"
    assert normalize_text(TextInput(caption="hello world", hashtags=())).text == "hello world"


# ------------------------------------------------------------------ order and sources


def test_text_is_caption_then_hashtags_then_ocr() -> None:
    result = normalize_text(
        TextInput(caption="Nice trip", hashtags=("Travel", "Goa"), ocr_text="Beach Resort")
    )
    assert result.text == "Nice trip travel goa Beach Resort"
    assert result.sources == ("caption", "hashtags", "ocr")
    assert result.hashtag_count == 2


# ------------------------------------------------------------------ hashtags


@pytest.mark.parametrize(
    ("tag", "words"),
    [
        ("MachineLearning", ["machine", "learning"]),
        ("machineLearning", ["machine", "learning"]),
        ("AIArt", ["ai", "art"]),
        ("AI_Art", ["ai", "art"]),
        ("ipl2024", ["ipl", "2024"]),
        ("2024ipl", ["2024", "ipl"]),
        ("COVID19", ["covid", "19"]),
        ("100DaysOfCode", ["100", "days", "of", "code"]),
        ("क्रिकेटIPL", ["क्रिकेट", "ipl"]),
        ("iplक्रिकेट", ["ipl", "क्रिकेट"]),
        ("मज़ेदार", ["मज़ेदार"]),
        ("कॉमेडी", ["कॉमेडी"]),
        ("machinelearning", ["machinelearning"]),  # no boundary to split on: known limit
        ("#Tag", ["tag"]),
        ("＃Tag", ["tag"]),
        ("", []),
        ("___", []),
        ("a_b_c", ["a", "b", "c"]),
    ],
)
def test_hashtag_splitting(tag: str, words: list[str]) -> None:
    assert split_hashtag(tag) == words


def test_inline_and_event_hashtags_are_merged_and_not_repeated() -> None:
    result = normalize_text(
        TextInput(
            caption="Love this #MachineLearning demo #AI",
            hashtags=("ai", "machinelearning", "MachineLearning"),
        )
    )
    # Event tags first, in order; the caption's own tags add only what is new; one copy of each.
    assert result.text == "Love this demo ai machinelearning machine learning"
    assert result.hashtag_count == 3


def test_hashtags_are_removed_from_the_caption_body() -> None:
    result = normalize_text(TextInput(caption="#one two #three"))
    assert "#" not in result.text
    assert result.text == "two one three"


def test_a_hash_without_letters_is_not_a_hashtag() -> None:
    assert text_of(caption="the #1 song of 2026") == "the #1 song of 2026"


def test_hashtag_count_is_capped() -> None:
    cfg = NormalizeConfig(max_hashtags=3)
    tags = tuple(f"tag{chr(97 + i)}" for i in range(10))
    result = normalize_text(TextInput(caption="x y", hashtags=tags), cfg)
    assert result.hashtag_count == 3


def test_devanagari_hashtags_survive_with_their_vowel_signs() -> None:
    assert text_of(caption="देखिए #क्रिकेट #मज़ेदार_वीडियो") == "देखिए क्रिकेट मज़ेदार वीडियो"


# ------------------------------------------------------------------ urls, mentions


def test_urls_and_mentions_are_removed() -> None:
    result = text_of(
        caption="Check https://example.test/path?q=1#frag and www.example.test now @some.handle_1 thanks"
    )
    assert result == "Check and now thanks"


# ------------------------------------------------------------------ emoji


def test_emoji_become_their_names() -> None:
    assert text_of(caption="Match day 🏏") == "Match day cricket bat and ball"
    assert text_of(caption="so funny 😂") == "so funny face with tears of joy"


def test_emoji_only_caption_still_has_text() -> None:
    result = normalize_text(TextInput(caption="🏏🏏🏏"))
    assert result.text == "cricket bat and ball"
    assert result.sources == ("caption",)


def test_repeated_emoji_count_once_and_the_number_is_capped() -> None:
    assert text_of(caption="😂😂😂😂 ok") == "face with tears of joy ok"
    many = "".join(chr(0x1F600 + i) for i in range(30))
    words = text_of(caption=many).split(" face")
    assert len(words) - 1 <= 10 + 1  # at most max_emoji distinct names (names share "face")
    cfg = NormalizeConfig(max_emoji=2)
    capped = normalize_text(TextInput(caption=many), cfg).text
    assert capped.count("face") <= 2 * 3


def test_skin_tones_variation_selectors_joiners_flags_and_keycaps_are_dropped() -> None:
    assert text_of(caption="👍🏽 great") == "thumbs up sign great"
    assert text_of(caption="❤️ love") == "heavy black heart love"
    assert text_of(caption="🇮🇳 India") == "India"
    assert text_of(caption="1️⃣ first") == "1 first"
    family = text_of(caption="👨‍👩‍👧 family")
    assert "‍" not in family
    assert family.endswith("family")


def test_no_emoji_character_survives() -> None:
    out = text_of(caption="a 🏏 b 😂 c 🔥 d ✨ e ❤️ f 🇮🇳 g 👩🏽‍🍳")
    assert all(ord(ch) < 0x1F000 and not 0x2600 <= ord(ch) < 0x27C0 for ch in out)


# ------------------------------------------------------------------ hindi and unicode


def test_hindi_text_is_kept_intact() -> None:
    caption = "जब टीचर बोले कल टेस्ट है, क्रिकेट मैच देखें"
    assert text_of(caption=caption) == unicodedata.normalize("NFKC", caption)


def test_hindi_with_emoji_and_hashtags() -> None:
    result = normalize_text(TextInput(caption="जब टीचर बोले कल टेस्ट है 😂😂 #मज़ेदार #कॉमेडी"))
    assert result.text == "जब टीचर बोले कल टेस्ट है face with tears of joy मज़ेदार कॉमेडी"


def test_zero_width_joiners_inside_hindi_words_are_kept() -> None:
    word = "क्‍ष"  # conjunct written with an explicit joiner
    assert word in text_of(caption=f"शब्द {word}")


def test_nfkc_and_invisible_characters() -> None:
    assert text_of(caption="ｗｏｒｌｄ ｈｅｌｌｏ") == "world hello"
    assert text_of(caption="𝓗𝓮𝓵𝓵𝓸 ﬁnal") == "Hello final"
    assert text_of(caption="zero​width soft­hyphen﻿") == "zerowidth softhyphen"


def test_hinglish_is_left_as_written() -> None:
    assert text_of(caption="Ghar pe hi karo ye workout") == "Ghar pe hi karo ye workout"


# ------------------------------------------------------------------ whitespace, noise, length


def test_whitespace_and_control_characters_collapse() -> None:
    assert text_of(caption="a\n\n  b\t\tc\r\nd\x00e\x07f") == "a b c def"


def test_character_floods_are_cut() -> None:
    assert text_of(caption="sooooooo goooood!!!!!!!!") == "sooo goood!!!"
    assert text_of(caption="aaa ok") == "aaa ok"
    assert text_of(caption="5555555 votes") == "5555555 votes"  # digits are not flooding


def test_long_text_is_cut_at_a_word_boundary() -> None:
    cfg = NormalizeConfig(max_chars=20)
    result = normalize_text(TextInput(caption="alpha beta gamma delta epsilon"), cfg)
    assert result.text == "alpha beta gamma"
    assert result.truncated
    short = normalize_text(TextInput(caption="short text"), cfg)
    assert not short.truncated


def test_one_very_long_word_is_cut_hard() -> None:
    cfg = NormalizeConfig(max_chars=10)
    assert normalize_text(TextInput(caption="abcdefghijklmnopqrstuvwxyz"), cfg).text == "abcdefghij"


def test_default_limit_holds_for_a_huge_caption() -> None:
    result = normalize_text(TextInput(caption="word " * 50_000))
    assert len(result.text) <= NormalizeConfig().max_chars


# ------------------------------------------------------------------ determinism


def test_normalisation_is_deterministic_and_idempotent() -> None:
    inp = TextInput(
        caption="Best #MachineLearning tips 🏏🔥 sooooo good!!!!!! https://x.test @me",
        hashtags=("ai", "coding"),
        ocr_text="SALE 50%",
    )
    first = normalize_text(inp)
    assert normalize_text(inp) == first
    again = normalize_text(TextInput(caption=first.text))
    assert again.text == first.text


def test_fragment_normalisation_for_taxonomy_descriptions() -> None:
    assert normalize_fragment("  funny ​comedy\n skit  ") == "funny comedy skit"
    assert normalize_fragment("ｆｕｎ") == "fun"


def test_random_text_never_breaks_the_invariants() -> None:
    rng = random.Random(20261007)
    ranges = [
        (0x20, 0x7E),
        (0x0900, 0x097F),
        (0x1F300, 0x1F64F),
        (0x2600, 0x27BF),
        (0x200B, 0x200F),
        (0x0000, 0x001F),
        (0xE000, 0xE0FF),
        (0x1F1E6, 0x1F1FF),
        (0xFF00, 0xFFEF),
    ]
    cfg = NormalizeConfig()
    for _ in range(500):
        chars = [chr(rng.randint(*rng.choice(ranges))) for _ in range(rng.randint(0, 120))]
        chars += list(rng.choice(["#tag", " #", "@x", "http://u", "😂"]) for _ in range(3))
        raw = "".join(chars)
        tags = tuple("".join(chr(rng.randint(0x20, 0x17F)) for _ in range(5)) for _ in range(3))
        out = normalize_text(TextInput(caption=raw, hashtags=tags), cfg).text
        assert len(out) <= cfg.max_chars
        assert out == out.strip()
        assert "  " not in out
        assert not any(unicodedata.category(c) == "Cc" for c in out)
        assert out == "" or any(c.isalpha() for c in out)
