"""Split text into speakable chunks for pseudo-streaming TTS.

Splits on sentence boundaries first, then caps overly long sentences by length
so each generated chunk stays small enough for fast first-byte time.
"""

import re

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…。！？])\s+|\n+")


def segment_text(text: str, max_chars: int = 200) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []

    sentences = [s.strip() for s in _SENTENCE_BOUNDARY.split(text) if s.strip()]
    if not sentences:
        sentences = [text]

    chunks: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            chunks.append(sentence)
            continue
        chunks.extend(_split_by_length(sentence, max_chars))
    return chunks


def _split_by_length(sentence: str, max_chars: int) -> list[str]:
    words = sentence.split()
    chunks: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
