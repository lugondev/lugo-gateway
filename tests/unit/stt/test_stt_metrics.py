import pytest

from app.services.stt.metrics import cer, normalize_text, percentile, wer


def test_normalize_lowercases_strips_punct_collapses_space():
    assert normalize_text("Xin  chào, Bạn!") == "xin chào bạn"


def test_normalize_preserves_vietnamese_diacritics():
    assert normalize_text("Trời nắng đẹp.") == "trời nắng đẹp"


def test_cer_identical_is_zero():
    assert cer("xin chào", "xin chào") == 0.0


def test_cer_single_substitution():
    # normalized "abc" vs "abx" -> 1 edit / 3 chars
    assert cer("abc", "abx") == pytest.approx(1 / 3)


def test_cer_normalizes_before_comparing():
    assert cer("Xin chào!", "xin chào") == 0.0


def test_cer_empty_reference():
    assert cer("", "") == 0.0
    assert cer("", "abc") == 1.0  # all hyp chars are insertions


def test_wer_single_word_substitution():
    assert wer("con mèo đen", "con chó đen") == pytest.approx(1 / 3)


def test_wer_deletion_and_insertion():
    assert wer("a b c", "a c") == pytest.approx(1 / 3)  # one deletion
    assert wer("a c", "a b c") == pytest.approx(1 / 2)  # one insertion over 2 ref words


def test_wer_empty_reference():
    assert wer("", "") == 0.0
    assert wer("", "hello world") == 1.0


def test_percentile():
    assert percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
    assert percentile([10], 95) == 10
    assert percentile([], 50) == 0.0
