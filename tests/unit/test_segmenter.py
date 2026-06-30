from app.services.tts.segmenter import SentenceAggregator, segment_text


def test_empty_text_returns_no_chunks():
    assert segment_text("") == []
    assert segment_text("   ") == []


def test_splits_on_sentence_boundaries():
    chunks = segment_text("Hello world. How are you? I am fine!")
    assert chunks == ["Hello world.", "How are you?", "I am fine!"]


def test_splits_vietnamese_sentences():
    chunks = segment_text("Xin chào. Bạn khỏe không?")
    assert chunks == ["Xin chào.", "Bạn khỏe không?"]


def test_long_sentence_is_split_by_length():
    sentence = " ".join(["word"] * 100)  # no punctuation, ~500 chars
    chunks = segment_text(sentence, max_chars=50)
    assert len(chunks) > 1
    assert all(len(c) <= 50 for c in chunks)


def test_newlines_are_boundaries():
    assert segment_text("line one\nline two") == ["line one", "line two"]


def _stream(agg: SentenceAggregator, text: str) -> list[str]:
    """Feed text char-by-char (worst-case token streaming) and collect emitted chunks."""
    out: list[str] = []
    for ch in text:
        out += agg.push(ch)
    out += agg.flush()
    return out


def test_first_chunk_cuts_early_at_clause_boundary():
    # The FIRST chunk should cut at the first clause boundary (comma) past the min
    # length, so TTS can start on the opening clause instead of waiting for the full
    # stop — this is the time-to-first-audio optimization borrowed from xiaozhi.
    out = _stream(SentenceAggregator(first_chunk_min_chars=10), "Chào bạn nhé, rất vui được gặp lại bạn hôm nay.")
    assert out[0] == "Chào bạn nhé,"
    assert out[1] == "rất vui được gặp lại bạn hôm nay."


def test_only_first_chunk_uses_clause_boundary():
    # After the first chunk, later sentences split only on sentence-final punctuation
    # (commas inside them do NOT split) — keeps natural prosody for the rest.
    out = _stream(SentenceAggregator(first_chunk_min_chars=6), "Ừ được, oke. Phần sau, vẫn liền mạch nhé.")
    assert out[0] == "Ừ được,"
    assert out[1] == "oke."
    assert out[2] == "Phần sau, vẫn liền mạch nhé."


def test_first_chunk_not_cut_below_min_chars():
    # A tiny leading clause ("Ừ,") must not be cut off on its own — wait for min chars.
    out = _stream(SentenceAggregator(first_chunk_min_chars=10), "Ừ, được rồi nhé.")
    assert out[0] == "Ừ, được rồi nhé."


def test_first_chunk_falls_back_to_sentence_end_when_no_clause_boundary():
    out = _stream(SentenceAggregator(first_chunk_min_chars=10), "Xin chào thế giới này. Tạm biệt.")
    assert out[0] == "Xin chào thế giới này."
    assert out[1] == "Tạm biệt."
