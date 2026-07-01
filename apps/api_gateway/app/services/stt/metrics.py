"""ASR accuracy metrics (CER / WER) with Vietnamese-friendly normalization.

CER (character error rate) is the primary metric for Vietnamese — it captures
diacritic/tone mistakes that WER (word error rate) would score as whole-word
errors. Both use Levenshtein edit distance over normalized text.
"""

import re
import unicodedata

# Keep letters (incl. Vietnamese diacritics), digits and spaces; drop the rest.
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """NFC-normalize, lowercase, strip punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFC", text or "")
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _levenshtein(ref: list, hyp: list) -> int:
    """Edit distance between two sequences (characters or word tokens)."""
    if not ref:
        return len(hyp)
    if not hyp:
        return len(ref)
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        cur = [i]
        for j, h in enumerate(hyp, start=1):
            cost = 0 if r == h else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate over normalized text (spaces included)."""
    ref = normalize_text(reference)
    hyp = normalize_text(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return _levenshtein(list(ref), list(hyp)) / len(ref)


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate over normalized, space-split tokens."""
    ref = normalize_text(reference).split()
    hyp = normalize_text(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    return _levenshtein(ref, hyp) / len(ref)


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in 0..100); 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac
