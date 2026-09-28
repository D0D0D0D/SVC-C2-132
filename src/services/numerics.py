"""Numeric normalisation for provenance fact-matching (docs/02_design.md §(b)④).

Extraction is NFKC + kanji-numeral aware so `６０`, `60`, and `六十` all normalise
to the same token and a unit character adjacent to the number (`60分` / `100個` /
`999999円`) does not hide it. Without this a `\\w`-boundary extractor drops
`999999円` to the empty set and the numeric guard passes vacuously
(a unit token adjacent to the number defeats a naive numeric guard).
"""

from __future__ import annotations

import re
import unicodedata

# Arabic number optionally followed by a 万/億 magnitude word (`1万` == 10000): the
# multiplier MUST be captured or an unbacked magnitude claim slips through the
# bidirectional guard (unit adjacency must not bypass it).
_ARABIC_RUN = re.compile(r"(\d+(?:\.\d+)?)\s*([万億])?")
_MAG_WORDS = {"万": 10000, "億": 100000000}
_KANJI_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
                 "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "億": 100000000}
_KANJI_RUN = re.compile(r"[〇零一二三四五六七八九十百千万億]+")


def _canon(value: float) -> str:
    """Canonical numeric string: integral floats collapse to int form."""
    if value == int(value):
        return str(int(value))
    return repr(value)


def _kanji_to_int(run: str) -> int | None:
    """Convert a kanji numeral run to an int, incl. 万/億 magnitudes.

    Handles: 十=10, 二十=20, 百=100, 千五百=1500, 一万=10000, 二千万=20000000,
    一億二千万=120000000. Units below 万 accumulate in `section`; 万/億 flush
    (section+current) at their magnitude into `total`."""
    total = 0
    section = 0
    current = 0
    saw_value = False   # True once a kanji digit or a 十/百/千 unit is seen
    for ch in run:
        if ch in _KANJI_DIGITS:
            current = _KANJI_DIGITS[ch]
            saw_value = True
        elif ch in ("十", "百", "千"):
            section += (current if current else 1) * _KANJI_UNITS[ch]
            current = 0
            saw_value = True
        elif ch in ("万", "億"):
            mult = (section + current) if (section or current) else 1
            total += mult * _KANJI_UNITS[ch]
            section = 0
            current = 0
        else:
            return None
    # A run of ONLY magnitude chars (万/億) is either already consumed by the arabic
    # pass (e.g. "10万") or meaningless alone — ignore it (avoids a spurious token).
    if not saw_value:
        return None
    total += section + current
    return total


def extract_numbers(text: str) -> set[str]:
    """Return the set of canonical numeric tokens present in `text`.

    Arabic (incl. full-width via NFKC, and a trailing 万/億 magnitude) and kanji
    numerals are both covered; adjacent unit characters are ignored (the number is
    extracted from within the surrounding text)."""
    if not isinstance(text, str) or not text:
        return set()
    norm = unicodedata.normalize("NFKC", text)
    out: set[str] = set()
    for num, mag in _ARABIC_RUN.findall(norm):
        try:
            value = float(num) * (_MAG_WORDS[mag] if mag else 1)
        except ValueError:  # pragma: no cover - regex guarantees numeric
            continue
        out.add(_canon(value))
    for run in _KANJI_RUN.findall(text):
        val = _kanji_to_int(run)
        if val is not None:
            out.add(_canon(float(val)))
    return out


def extract_prose_numbers(text: str) -> set[str]:
    """Numbers for the free-NL prose bidirectional check — ARABIC (incl. full-width
    and a trailing 万/億 magnitude) ONLY, NOT bare kanji-numeral runs.

    Free Japanese prose is full of kanji that read as numbers but are idioms
    (`万一`="just in case", `一` as a counter), so running the full kanji parser over
    prose produces spurious tokens that FALSELY reject legitimate findings. When the
    LLM references a config number it writes it in arabic (matching the structured
    facts), so arabic+magnitude extraction is the right net here. Kanji equivalence
    (`六十`==`60`) still applies to structured fact matching via `extract_numbers`."""
    if not isinstance(text, str) or not text:
        return set()
    norm = unicodedata.normalize("NFKC", text)
    out: set[str] = set()
    for num, mag in _ARABIC_RUN.findall(norm):
        try:
            out.add(_canon(float(num) * (_MAG_WORDS[mag] if mag else 1)))
        except ValueError:  # pragma: no cover
            continue
    return out


def numeric_tokens_of(value) -> set[str]:
    """Canonical numeric tokens of a fact value (number, numeric string, or text)."""
    if isinstance(value, bool):
        return set()
    if isinstance(value, (int, float)):
        return {_canon(float(value))}
    if isinstance(value, str):
        return extract_numbers(value)
    return set()
