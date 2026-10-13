"""Text normalisation: caption, hashtags and OCR text become one string for the text encoder.

Deterministic and language-agnostic (English, Hindi, Hinglish). Steps, in order, per text:

1. Unicode NFKC (full-width forms, ligatures, styled "fancy font" letters become plain), then
   removal of zero-width space, word joiner, BOM and soft hyphen.
2. URLs and @mentions are removed. A handle names a person, not a topic.
3. Hashtags are cut out of the text and collected; they are split into words (below) and added
   once, after the caption. Event hashtags and hashtags written inside the caption are merged and
   de-duplicated, so a tag that appears in both is not counted twice.
4. Emoji are replaced by their Unicode names (`🏏` becomes "cricket bat and ball"): an emoji often
   carries the topic and the encoder reads words, not pictographs. Skin-tone modifiers, variation
   selectors, joiners, flags and keycaps are dropped; repeats are dropped; at most `max_emoji`
   distinct emoji per text are kept. The names come from Python's `unicodedata`, so the Unicode
   version is part of the pipeline version (config.py).
5. Other control and format characters go, whitespace collapses to single spaces, and a character
   repeated more than `max_repeat` times ("sooooo", "!!!!!!") is cut to `max_repeat`. The Hindi
   joiners ZWNJ and ZWJ are kept: they can change how Devanagari conjuncts are written.
6. Case is kept (the encoder is cased), except hashtag words, which are lower-cased because
   hashtags are case-insensitive identifiers.

Hashtag splitting: underscores, camelCase and PascalCase (`MachineLearning`), acronym runs
(`AIArt` -> "ai art"), letter/digit boundaries (`ipl2024` -> "ipl 2024") and Devanagari/Latin
boundaries. An all-lowercase run (`machinelearning`) has no boundary to split on and stays one
token; the encoder's subword vocabulary handles it. This is a known limit, not a bug.

Missing input is normal: no caption, no hashtags and no OCR text give an empty result, which the
pipeline reports as "no text", never as a made-up default.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

NORMALIZER_VERSION = "text-norm-1"

_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MENTION = re.compile(r"@[A-Za-z0-9._]+")
_INVISIBLE = {ord(c): None for c in "​⁠﻿­"}
_DROPPED = frozenset({"Cc", "Co", "Cs", "Cn"})
_KEEP_FORMAT = frozenset({"‌", "‍"})  # ZWNJ, ZWJ: part of Devanagari conjuncts
_WHITESPACE = re.compile(r"\s+")

# Hashtag word boundaries. Python's re has no \p{L}, so Devanagari is addressed by block.
_SPLIT_AT = re.compile(
    r"(?<=[a-z])(?=[A-Z])"  # camelCase
    r"|(?<=[A-Z])(?=[A-Z][a-z])"  # acronym followed by a word: AIArt
    r"|(?<=[A-Za-z])(?=[0-9])"
    r"|(?<=[0-9])(?=[A-Za-z])"
    r"|(?<=[ऀ-ॿ])(?=[A-Za-z0-9])"
    r"|(?<=[A-Za-z0-9])(?=[ऀ-ॿ])"
)

_REGIONAL = range(0x1F1E6, 0x1F200)
_SKIN_TONES = range(0x1F3FB, 0x1F400)
_EMOJI_RANGES = (
    range(0x1F000, 0x1FB00),
    range(0x2600, 0x27C0),
    range(0x23E9, 0x23F4),
    range(0x23F8, 0x23FB),
    range(0x2B1B, 0x2B1D),
    range(0x2B50, 0x2B51),
    range(0x2B55, 0x2B56),
)
_TAG_EMOJI_LIMIT = 3
_EMOJI_GLUE = frozenset({0xFE0E, 0xFE0F, 0x20E3})  # variation selectors, combining keycap
_TAG_CHARS = range(0xE0000, 0xE0080)


@dataclass(frozen=True)
class NormalizeConfig:
    """Parameters that change the output. All of them are part of the pipeline version."""

    max_chars: int = 2000
    max_hashtags: int = 30
    max_emoji: int = 10
    max_repeat: int = 3
    emoji: Literal["names"] = "names"


@dataclass(frozen=True)
class TextInput:
    """What the pipeline may read for one Reel. `None` means unavailable, never an empty default."""

    caption: str | None = None
    #: Hashtags without the leading `#`, as captured by the extension. `None` if unavailable.
    hashtags: tuple[str, ...] | None = None
    ocr_text: str | None = None


@dataclass(frozen=True)
class NormalizedText:
    text: str
    #: Which inputs contributed: any of "caption", "hashtags", "ocr".
    sources: tuple[str, ...]
    hashtag_count: int
    truncated: bool

    @property
    def is_empty(self) -> bool:
        return self.text == ""


def _is_emoji_base(cp: int) -> bool:
    if cp in _REGIONAL or cp in _SKIN_TONES:
        return False
    return any(cp in r for r in _EMOJI_RANGES)


def _is_tag_char(ch: str) -> bool:
    return ch == "_" or ch.isalnum() or unicodedata.category(ch) in ("Mn", "Mc")


def _extract_hashtags(text: str) -> tuple[str, list[str]]:
    """Cut `#tag` tokens out of `text`. A `#` followed by no letters is left alone."""
    out: list[str] = []
    tags: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "#":
            j = i + 1
            while j < n and _is_tag_char(text[j]):
                j += 1
            body = text[i + 1 : j]
            if any(c.isalpha() for c in body):
                tags.append(body)
                out.append(" ")
                i = j
                continue
        out.append(text[i])
        i += 1
    return "".join(out), tags


def _replace_emoji(text: str, limit: int) -> str:
    out: list[str] = []
    seen: set[str] = set()
    after_emoji = False
    for ch in text:
        cp = ord(ch)
        if _is_emoji_base(cp):
            name = unicodedata.name(ch, "").lower()
            if name and name not in seen and len(seen) < limit:
                seen.add(name)
                out.append(f" {name} ")
            after_emoji = True
        elif cp in _SKIN_TONES or cp in _REGIONAL or cp in _EMOJI_GLUE or cp in _TAG_CHARS:
            after_emoji = cp not in _TAG_CHARS and after_emoji
        elif ch == "‍" and after_emoji:
            pass  # joiner inside an emoji sequence
        else:
            after_emoji = False
            out.append(ch)
    return "".join(out)


def _drop_noise(text: str) -> str:
    out: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if ch in "\t\n\r\v\f" or category in ("Zs", "Zl", "Zp"):
            out.append(" ")
        elif category in _DROPPED or (category in ("Cf", "Me") and ch not in _KEEP_FORMAT):
            continue
        else:
            out.append(ch)
    return "".join(out)


def normalize_fragment(raw: str) -> str:
    """The light form used for taxonomy descriptions: NFKC, no invisibles, single spaces."""
    text = unicodedata.normalize("NFKC", raw).translate(_INVISIBLE)
    return _WHITESPACE.sub(" ", _drop_noise(text)).strip()


def split_hashtag(tag: str) -> list[str]:
    """`MachineLearning` -> ["machine", "learning"]; `क्रिकेटIPL` -> ["क्रिकेट", "ipl"]."""
    body = unicodedata.normalize("NFKC", tag).lstrip("#").translate(_INVISIBLE)
    # Event hashtags come from the page, not the caption scanner above: clean them the same way.
    body = _drop_noise(_replace_emoji(body, _TAG_EMOJI_LIMIT))
    words: list[str] = []
    for part in body.split("_"):
        spaced = _SPLIT_AT.sub(" ", part)
        words.extend(w.lower() for w in spaced.split() if w)
    return words


def _clean(raw: str, config: NormalizeConfig) -> tuple[str, list[str]]:
    text = unicodedata.normalize("NFKC", raw).translate(_INVISIBLE)
    text = _URL.sub(" ", text)
    text, tags = _extract_hashtags(text)
    text = _MENTION.sub(" ", text)
    text = _replace_emoji(text, config.max_emoji)
    text = _drop_noise(text)
    if config.max_repeat >= 1:
        n = config.max_repeat
        text = re.sub(rf"([^\s\d])\1{{{n},}}", lambda m: m.group(1) * n, text)
    return _WHITESPACE.sub(" ", text).strip(), tags


def _has_letters(text: str) -> bool:
    return any(c.isalpha() for c in text)


def _cut(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    if " " in head and text[limit] != " ":
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(), True


def normalize_text(inp: TextInput, config: NormalizeConfig | None = None) -> NormalizedText:
    """Caption, then hashtags, then OCR text, joined by single spaces."""
    cfg = config or NormalizeConfig()
    tags: list[str] = list(inp.hashtags or ())
    cleaned: dict[str, str] = {}
    for label, raw in (("caption", inp.caption), ("ocr", inp.ocr_text)):
        if not raw:
            continue
        text, found = _clean(raw, cfg)
        tags.extend(found)  # hashtags written inside the caption or OCR text join the event's
        if _has_letters(text):
            cleaned[label] = text

    unique: dict[str, list[str]] = {}
    for tag in tags:
        words = split_hashtag(tag)
        key = " ".join(words)
        if words and _has_letters(key) and key not in unique and len(unique) < cfg.max_hashtags:
            unique[key] = words
    if unique:
        cleaned["hashtags"] = " ".join(key for key in unique)

    order = [label for label in ("caption", "hashtags", "ocr") if label in cleaned]
    text, truncated = _cut(" ".join(cleaned[label] for label in order), cfg.max_chars)
    if not _has_letters(text):
        return NormalizedText("", (), 0, False)
    return NormalizedText(text, tuple(order), len(unique), truncated)
