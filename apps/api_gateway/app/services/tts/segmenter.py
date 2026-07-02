"""Split text into speakable chunks for pseudo-streaming TTS.

Splits on sentence boundaries first, then caps overly long sentences by length
so each generated chunk stays small enough for fast first-byte time.
"""

import re

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…。！？])\s+|\n+")

# Emoji / pictographic symbols that must never reach TTS (they get mispronounced
# or read out as their Unicode name). Covers the common emoji blocks plus the
# ZWJ / variation-selector / keycap glue used to build composite emoji. A plain
# digit like "1" in a keycap "1️⃣" survives (only the FE0F + 20E3 glue is dropped).
_EMOJI_RE = re.compile(
    "["
    "\U0001f1e6-\U0001f1ff"  # regional indicators (flags)
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f680-\U0001f6ff"  # transport & map
    "\U0001f700-\U0001f77f"  # alchemical
    "\U0001f780-\U0001f7ff"  # geometric shapes extended
    "\U0001f800-\U0001f8ff"  # supplemental arrows-C
    "\U0001f900-\U0001f9ff"  # supplemental symbols & pictographs
    "\U0001fa00-\U0001faff"  # symbols & pictographs extended-A
    "\U0001f000-\U0001f0ff"  # mahjong / dominoes / playing cards
    "\U00002190-\U000021ff"  # arrows (→ ← ↑ ↓ ↗ …)
    "\U00002300-\U000023ff"  # misc technical (⌚⏰⏳ …)
    "\U00002400-\U000025ff"  # enclosed alphanumerics (①②③) + box drawing + geometric shapes (■▲▶◆ …)
    "\U00002600-\U000026ff"  # miscellaneous symbols (☀☎♻ …)
    "\U00002700-\U000027bf"  # dingbats (✂✅✨ …)
    "\U00002b00-\U00002bff"  # misc symbols & arrows (⭐⬅ …)
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U0000200d"             # zero-width joiner
    "\U000020e3"             # combining enclosing keycap
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text: str) -> str:
    """Remove emoji/pictographic symbols so TTS never tries to speak them.

    Also tidies the whitespace and stray punctuation gaps left behind, e.g.
    "Xin chào 👋 !" -> "Xin chào!".
    """
    if not text:
        return text
    cleaned = _EMOJI_RE.sub("", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)          # collapse doubled spaces
    cleaned = re.sub(r"\s+([,.;:!?…。！？])", r"\1", cleaned)  # no space before punctuation
    return cleaned.strip()


def segment_text(text: str, max_chars: int = 200) -> list[str]:
    text = strip_emoji((text or "").strip())
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
# Clause-level boundaries used ONLY for the first chunk of a reply, so TTS can start
# on the opening clause instead of waiting for the first full stop (lower time-to-
# first-audio). Borrowed from xiaozhi-server's expanded first-segment punctuation.
_CLAUSE_BOUNDARY = re.compile(r"[.!?…。！？\n,;:，；：、]")


class SentenceAggregator:
    """Incrementally buffer streamed LLM text and emit complete sentences.

    Lets conversation synthesize each sentence the moment the LLM finishes it,
    instead of waiting for the whole reply (much lower time-to-first-audio).

    The FIRST chunk is cut more aggressively — at the first clause boundary (comma,
    colon, …) past ``first_chunk_min_chars`` — so the opening clause reaches TTS as
    early as possible. Subsequent chunks split only on sentence-final punctuation.
    """

    def __init__(self, max_chars: int = 200, first_chunk_min_chars: int = 12) -> None:
        self.max_chars = max_chars
        self.first_chunk_min_chars = first_chunk_min_chars
        self._buf = ""
        self._first_done = False

    def _take_first_chunk(self) -> str | None:
        """Cut at the earliest clause boundary at/after the min length; else None."""
        for m in _CLAUSE_BOUNDARY.finditer(self._buf):
            if m.end() >= self.first_chunk_min_chars:
                chunk = strip_emoji(self._buf[: m.end()].strip())
                self._buf = self._buf[m.end() :]
                return chunk or None
        return None

    def push(self, text: str) -> list[str]:
        self._buf += text
        out: list[str] = []
        if not self._first_done:
            chunk = self._take_first_chunk()
            if chunk:
                out.append(chunk)
                self._first_done = True
        if self._first_done:
            while True:
                match = _SENTENCE_END.match(self._buf)
                if not match:
                    break
                sentence = strip_emoji(match.group().strip())
                self._buf = self._buf[match.end() :]
                if sentence:
                    out.append(sentence)
        # Force-flush an overly long run with no punctuation.
        if len(self._buf) >= self.max_chars:
            chunk = strip_emoji(self._buf.strip())
            self._buf = ""
            self._first_done = True
            if chunk:
                out.append(chunk)
        return out

    def flush(self) -> list[str]:
        rest = strip_emoji(self._buf.strip())
        self._buf = ""
        self._first_done = True
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
