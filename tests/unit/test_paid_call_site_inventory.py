"""Every call that can spend money is classified here, on purpose.

Three times in this subsystem's history a paid call site was added and nobody
metered it: the /chat LLM path, the whole livehost endpoint, and both streaming
endpoints. Each was found months later by an audit. This test replaces the audit:
a new provider-invoking call fails it until someone adds a row below, with a
status, a reason, and the name of a test that covers the behavior.

WHAT THIS CATCHES: a new file calling a provider; a new provider-invoking method
name; an extra call added to an already-listed file (the count is part of the
key); a row naming a covering test that does not exist.

WHAT IT DOES NOT: it cannot tell whether a site marked "metered" really records a
row -- that is test_every_paid_entry_point_records_usage. And a caller reaching a
provider through an indirection these patterns do not match would slip past. This
makes an omission loud; it does not make one impossible.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "apps" / "api_gateway" / "app"

# The methods that actually reach a provider (network or local inference).
_CALL_PATTERN = re.compile(
    r"(?<![\w.])(?:await\s+)?[\w.\[\]()]*\.?"
    r"(transcribe_bytes|synthesize|reply_stream|reply|open_stream"
    r"|embed_texts|embed_texts_with_usage)\s*\("
)

# Files that DEFINE these methods rather than call a provider through them.
_IMPLEMENTATIONS = (
    "services/stt/providers/",
    "services/tts/providers/",
    "services/conversation/responder.py",
    "services/memory/embedder.py",
)

# (relative path, method) -> (call count, status, reason, covering test)
#
# status is one of:
#   "metered+gated"  -- records usage and checks the quota itself
#   "covered-by-caller" -- a helper; its caller records one row for the whole unit
#   "exempt" -- deliberately unmetered or ungated, with the reason stated
_CLASSIFIED: dict[tuple[str, str], tuple[int, str, str, str]] = {
    ("api/routes/conversation.py", "reply_stream"): (
        1, "metered+gated", "POST /v1/conversation/chat, tool-enabled path",
        "tests/unit/test_routes_usage_metering.py",
    ),
    ("api/routes/conversation.py", "reply"): (
        1, "metered+gated", "POST /v1/conversation/chat, plain path",
        "tests/unit/test_routes_usage_metering.py",
    ),
    ("api/routes/stt.py", "transcribe_bytes"): (
        1, "metered+gated", "POST /v1/stt/transcribe",
        "tests/unit/test_routes_usage_metering.py",
    ),
    ("api/routes/stt.py", "open_stream"): (
        1, "metered+gated", "WS /v1/stt/stream: gated at connect and each flush",
        "tests/unit/test_stt_stream_metering.py",
    ),
    ("api/routes/tts.py", "synthesize"): (
        2, "metered+gated", "POST /v1/tts/synthesize and the /v1/tts/stream job",
        "tests/unit/test_tts_stream_metering.py",
    ),
    ("api/routes/livehost.py", "transcribe_bytes"): (
        1, "metered+gated", "livehost voice turn STT",
        "tests/unit/test_livehost_quota_gate.py",
    ),
    ("api/routes/livehost.py", "synthesize"): (
        1, "metered+gated", "livehost TTS per sentence",
        "tests/unit/test_livehost_quota_gate.py",
    ),
    ("api/routes/livehost.py", "reply_stream"): (
        2, "metered+gated", "livehost voice and social turns",
        "tests/unit/test_livehost_quota_gate.py",
    ),
    ("services/conversation/session.py", "transcribe_bytes"): (
        1, "metered+gated", "conversation core STT, incl. the fast-path engine switch",
        "tests/unit/test_session_usage_metering.py",
    ),
    ("services/conversation/session.py", "synthesize"): (
        2, "metered+gated", "conversation core TTS, prefetch and direct",
        "tests/unit/test_session_usage_metering.py",
    ),
    ("services/conversation/session.py", "reply_stream"): (
        2, "metered+gated", "conversation core LLM, tool and plain paths",
        "tests/unit/test_session_usage_metering.py",
    ),
    ("services/memory/extractor.py", "embed_texts_with_usage"): (
        1, "metered+gated", "memory fact embedding at session teardown",
        "tests/unit/test_memory_usage_metering.py",
    ),
    ("services/memory/retriever.py", "embed_texts_with_usage"): (
        1, "metered+gated", "per-turn query embedding; gated by the turn it runs in",
        "tests/unit/test_memory_usage_metering.py",
    ),
    ("services/stt/segmented.py", "transcribe_bytes"): (
        2, "covered-by-caller",
        "long-clip segments; the route records one row for the whole clip, so "
        "metering here would double-count",
        "tests/unit/test_routes_usage_metering.py",
    ),
    ("services/stt/base.py", "transcribe_bytes"): (
        1, "covered-by-caller",
        "streaming adapter reached only from WS /v1/stt/stream, which meters per flush",
        "tests/unit/test_stt_stream_metering.py",
    ),
    ("services/stt/streaming_chunked.py", "transcribe_bytes"): (
        1, "covered-by-caller",
        "chunked streaming adapter, same path as services/stt/base.py",
        "tests/unit/test_stt_stream_metering.py",
    ),
    ("api/routes/model_registry.py", "transcribe_bytes"): (
        1, "exempt",
        "add-time credential test; metered but never gated, or an admin over "
        "quota could not validate the provider needed to fix it",
        "tests/unit/test_model_registry_test_call_metering.py",
    ),
    ("api/routes/model_registry.py", "synthesize"): (
        1, "exempt", "add-time credential test; see transcribe_bytes above",
        "tests/unit/test_model_registry_test_call_metering.py",
    ),
    ("api/routes/model_registry.py", "reply"): (
        1, "exempt", "add-time credential test; see transcribe_bytes above",
        "tests/unit/test_model_registry_test_call_metering.py",
    ),
    ("api/routes/model_registry.py", "embed_texts"): (
        1, "exempt", "add-time credential test; see transcribe_bytes above",
        "tests/unit/test_model_registry_test_call_metering.py",
    ),
    ("services/memory/extractor.py", "reply"): (
        1, "exempt",
        "regex false match: docstring prose 'from an LLM reply (tolerant of "
        "prose/fences)' -- not a call at all",
        "tests/unit/test_paid_call_site_inventory.py",
    ),
    ("services/tts/base.py", "synthesize"): (
        1, "exempt",
        "regex false match: docstring prose referencing the synthesize() method "
        "defined just below it, which is itself a `def` line and skipped",
        "tests/unit/test_paid_call_site_inventory.py",
    ),
    ("services/tts/segmenter.py", "reply"): (
        1, "exempt",
        "regex false match: docstring prose 'waiting for the whole reply (much "
        "lower time-to-first-audio)' -- not a call at all",
        "tests/unit/test_paid_call_site_inventory.py",
    ),
    ("services/tts/streaming.py", "synthesize"): (
        1, "exempt",
        "regex false match: `asyncio.create_task(_synthesize())` calls a local "
        "private helper (leading underscore fools the pattern); it awaits the "
        "injected `synth` callable, not a provider method named synthesize -- the "
        "real provider call is metered at whichever caller binds `synth`",
        "tests/unit/test_paid_call_site_inventory.py",
    ),
}

_VALID_STATUSES = {"metered+gated", "covered-by-caller", "exempt"}


def _found_call_sites() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if "__pycache__" in rel or any(rel.startswith(p) for p in _IMPLEMENTATIONS):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            # Skip definitions and comments -- we want callers, not declarations.
            if stripped.startswith(("def ", "async def ", "#")):
                continue
            for match in _CALL_PATTERN.finditer(line):
                found[(rel, match.group(1))] = found.get((rel, match.group(1)), 0) + 1
    return found


def test_every_paid_call_site_is_classified():
    found = _found_call_sites()
    unclassified = sorted(set(found) - set(_CLASSIFIED))
    assert not unclassified, (
        "New paid call site(s) found. Add each to _CLASSIFIED with a status, a "
        "reason, and a covering test -- and make sure the site actually meters "
        f"and gates before you call it metered:\n  " + "\n  ".join(map(str, unclassified))
    )


def test_no_classified_call_site_has_disappeared():
    """A stale row hides the fact that nobody is checking that path any more."""
    found = _found_call_sites()
    gone = sorted(set(_CLASSIFIED) - set(found))
    assert not gone, (
        "These classified call sites no longer exist. Remove their rows so the "
        f"table keeps describing the real code:\n  " + "\n  ".join(map(str, gone))
    )


def test_call_counts_match_so_an_added_call_cannot_hide():
    """Keying on (file, method) alone would let a second, unmetered call slip
    into a file that already has a classified one."""
    found = _found_call_sites()
    drifted = [
        f"{key}: classified {_CLASSIFIED[key][0]}, found {count}"
        for key, count in sorted(found.items())
        if key in _CLASSIFIED and _CLASSIFIED[key][0] != count
    ]
    assert not drifted, (
        "Call count changed. If you added a call, meter and gate it, then update "
        f"the count:\n  " + "\n  ".join(drifted)
    )


def test_every_classification_names_a_test_that_exists():
    """A row claiming coverage from a test that does not exist is worse than no
    row at all: it reads as proof."""
    repo_root = Path(__file__).resolve().parents[2]
    missing = []
    for key, (_count, status, reason, covering_test) in sorted(_CLASSIFIED.items()):
        assert status in _VALID_STATUSES, f"{key}: unknown status {status!r}"
        assert reason.strip(), f"{key}: a classification needs a reason"
        if not (repo_root / covering_test).is_file():
            missing.append(f"{key}: {covering_test}")
    assert not missing, "Covering test file(s) do not exist:\n  " + "\n  ".join(missing)
