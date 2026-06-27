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


_SENTENCE_END = re.compile(r".*?[.!?…。！？\n]+", re.S)


class SentenceAggregator:
    """Incrementally buffer streamed LLM text and emit complete sentences.

    Lets conversation synthesize each sentence the moment the LLM finishes it,
    instead of waiting for the whole reply (much lower time-to-first-audio).
    """

    def __init__(self, max_chars: int = 200) -> None:
        self.max_chars = max_chars
        self._buf = ""

    def push(self, text: str) -> list[str]:
        self._buf += text
        out: list[str] = []
        while True:
            match = _SENTENCE_END.match(self._buf)
            if not match:
                break
            sentence = match.group().strip()
            self._buf = self._buf[match.end() :]
            if sentence:
                out.append(sentence)
        # Force-flush an overly long run with no punctuation.
        if len(self._buf) >= self.max_chars:
            out.append(self._buf.strip())
            self._buf = ""
        return out

    def flush(self) -> list[str]:
        rest = self._buf.strip()
        self._buf = ""
        return [rest] if rest else []


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
