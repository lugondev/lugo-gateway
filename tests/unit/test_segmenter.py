from app.services.tts.segmenter import segment_text


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
