from app.services.tts.segmenter import SentenceAggregator, segment_text, strip_emoji


def test_empty_text_returns_no_chunks():
    assert segment_text("") == []
    assert segment_text("   ") == []


def test_strip_emoji_removes_symbols_and_tidies_spacing():
    assert strip_emoji("Xin chào 👋 bạn nhé! 😊") == "Xin chào bạn nhé!"
    assert strip_emoji("Trời ☀️ đẹp 🎉🎉🎉") == "Trời đẹp"
    # a digit inside a keycap emoji survives; only the glue is dropped
    assert strip_emoji("Chọn 1️⃣ hoặc 2️⃣") == "Chọn 1 hoặc 2"


def test_segment_text_strips_emoji():
    assert segment_text("Chào bạn 😊. Tốt 👍!") == ["Chào bạn.", "Tốt!"]


def test_aggregator_strips_emoji_from_streamed_chunks():
    agg = SentenceAggregator()
    out: list[str] = []
    for tok in ["Chào ", "bạn 😊, ", "hôm nay ", "thế nào? ", "Tốt 👍."]:
        out += agg.push(tok)
    out += agg.flush()
    assert out == ["Chào bạn, hôm nay thế nào?", "Tốt."]


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


def test_short_first_sentence_cuts_at_its_own_hard_end_below_min_chars():
    # Bug: a short but COMPLETE opener ("Chào bạn!", 9 chars) was silently
    # held back because it's under first_chunk_min_chars=12, and since the
    # rest of the reply has no comma before its own final period, the whole
    # thing collapsed into one long "first chunk" -- adding seconds of
    # avoidable time-to-first-audio for exactly the common short-greeting
    # case. Real sentence-final punctuation (.!?) must always be eligible as
    # a first-chunk cut, unlike a soft clause comma (see
    # test_first_chunk_not_cut_below_min_chars, which must still hold).
    out = _stream(
        SentenceAggregator(first_chunk_min_chars=12),
        "Chào bạn! Rất vui được nói chuyện với bạn.",
    )
    assert out[0] == "Chào bạn!"
    assert out[1] == "Rất vui được nói chuyện với bạn."


# --- Regression: over-splitting seen on ESP32 storytelling output ------------
# Symptoms from a live log: dramatic mid-sentence "…" split the sentence, a lone
# closing quote " was emitted as its own chunk, and every dialogue newline forced
# a cut. Each tiny chunk becomes a separate TTS utterance -> choppy playback.


def test_closing_quote_stays_with_its_sentence():
    # Streaming: the '?' arrives before the closing quote, but the quote must NOT
    # be emitted as a lone chunk — it belongs to the question it closes.
    out = _stream(SentenceAggregator(first_chunk_min_chars=3), 'Nghe nè, Khách nói: "Ủa gì vậy?" xong rồi.')
    assert out == ["Nghe nè,", 'Khách nói: "Ủa gì vậy?"', "xong rồi."]


def test_ellipsis_midsentence_is_not_a_boundary():
    # "…" followed by a lowercase continuation is a dramatic pause, not a full stop.
    out = _stream(SentenceAggregator(first_chunk_min_chars=3), "Nghe nè, thấy cô dâu… đội mũ lạ hoắc.")
    assert out == ["Nghe nè,", "thấy cô dâu… đội mũ lạ hoắc."]


def test_ellipsis_before_capital_is_a_boundary():
    # "…" followed by whitespace + a capital letter still ends the sentence.
    out = _stream(SentenceAggregator(first_chunk_min_chars=3), "Nghe nè, thôi vậy… Chào bạn.")
    assert out == ["Nghe nè,", "thôi vậy…", "Chào bạn."]


def test_single_newline_is_not_a_boundary():
    # Dialogue formatted across lines must not be chopped at every newline.
    out = _stream(SentenceAggregator(first_chunk_min_chars=3), "Nghe nè, cô dâu nói:\nEm hứa rồi.")
    assert out == ["Nghe nè,", "cô dâu nói: Em hứa rồi."]


def test_blank_line_is_a_boundary():
    # A paragraph break (blank line) IS a boundary even without terminal punctuation.
    out = _stream(SentenceAggregator(first_chunk_min_chars=3), "Nghe nè, câu một\n\ncâu hai")
    assert out == ["Nghe nè,", "câu một", "câu hai"]


def test_decimal_number_is_not_split():
    # A period between digits is not a sentence boundary.
    out = _stream(SentenceAggregator(first_chunk_min_chars=3), "Giá là, 3.14 đô thôi.")
    assert out == ["Giá là,", "3.14 đô thôi."]
