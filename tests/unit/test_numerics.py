"""Numeric extraction — NFKC / kanji / unit-adjacency / 万・億 magnitude."""

from src.services import numerics


def test_full_width_and_unit_adjacent():
    assert "60" in numerics.extract_numbers("６０分")
    assert "999999" in numerics.extract_numbers("上限999999円")


def test_kanji_basic():
    assert "60" in numerics.extract_numbers("六十分")
    assert "1500" in numerics.extract_numbers("千五百ポイント")


def test_man_oku_magnitude():
    # the key regression: `1万` must be 10000, not 1 (else an unbacked magnitude
    # claim slips through the bidirectional provenance guard).
    assert "10000" in numerics.extract_numbers("上限は1万個")
    assert "10000" in numerics.extract_numbers("一万ポイント")
    assert "15000" in numerics.extract_numbers("1.5万")
    assert "20000000" in numerics.extract_numbers("二千万")
    assert "120000000" in numerics.extract_numbers("一億二千万")


def test_tokens_of_value():
    assert numerics.numeric_tokens_of(100) == {"100"}
    assert numerics.numeric_tokens_of("1万") == {"10000"}
    assert numerics.numeric_tokens_of(True) == set()  # bool is not numeric


def test_prose_extractor_arabic_and_magnitude_only():
    # prose bidirectional check: arabic + 万/億 magnitude, exact.
    assert numerics.extract_prose_numbers("10万個") == {"100000"}   # no spurious 10000
    assert numerics.extract_prose_numbers("1万") == {"10000"}
    assert numerics.extract_prose_numbers("在庫上限は100個") == {"100"}


def test_prose_extractor_ignores_kanji_idioms():
    # `万一` ("just in case") must NOT be read as a number — else it falsely rejects
    # a legitimate finding whose prose uses the idiom.
    assert numerics.extract_prose_numbers("万一プレイヤーが影響を受ける場合") == set()
    assert numerics.extract_prose_numbers("一部のプレイヤー") == set()
