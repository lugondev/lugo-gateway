"""Domain-vocabulary biasing for Whisper-family STT.

Whisper's ``initial_prompt`` seeds the decoder with expected orthography, which is
the practical equivalent of FunASR's hotword list: it nudges recognition toward
domain terms (product names, wake-words, commands, proper nouns with the right
Vietnamese diacritics). Keep the prompt SHORT — an overly long initial prompt
makes Whisper hallucinate / repeat, so ``build_initial_prompt`` caps its length.
"""

import os


def load_glossary_terms(path: str) -> list[str]:
    """Read a glossary file (one term per line, ``#`` comments) into a term list.

    Blank lines and comments are skipped, surrounding whitespace trimmed, and
    duplicates removed while preserving first-seen order. A missing/empty path
    returns an empty list (biasing is optional).
    """
    if not path or not os.path.isfile(path):
        return []
    seen: dict[str, None] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            term = line.strip()
            if not term or term.startswith("#"):
                continue
            seen.setdefault(term, None)
    return list(seen)


def build_initial_prompt(base: str, terms: list[str], max_chars: int = 240) -> str | None:
    """Combine a base prompt sentence with glossary terms into an initial prompt.

    Returns ``None`` when there is nothing to bias with (so callers pass ``None``
    to the engine, i.e. no initial prompt). The result is truncated to
    ``max_chars`` — as many leading terms as fit are kept — to avoid the
    long-prompt hallucination failure mode.
    """
    base = (base or "").strip()
    parts = [base] if base else []
    parts.extend(t for t in terms if t)
    if not parts:
        return None
    prompt = " ".join(parts)
    if len(prompt) <= max_chars:
        return prompt
    # Trim to whole space-separated tokens that fit within the cap.
    truncated = prompt[:max_chars]
    cut = truncated.rfind(" ")
    if cut > 0:
        truncated = truncated[:cut]
    return truncated.strip()


# Cache keyed by (base, glossary_path) — the glossary file is read once per config.
_prompt_cache: dict[tuple[str, str], str | None] = {}


def resolve_initial_prompt(base: str, glossary_path: str) -> str | None:
    """Build (and cache) the Whisper initial prompt from a base + glossary file.

    This is the entry point providers call: it merges ``base`` (e.g.
    ``system_config_store.get().stt_local.whisper_initial_prompt``) with the terms
    in ``glossary_path`` and caches the result so the file isn't re-read on every
    transcription.
    """
    key = (base or "", glossary_path or "")
    if key not in _prompt_cache:
        _prompt_cache[key] = build_initial_prompt(base, load_glossary_terms(glossary_path))
    return _prompt_cache[key]
