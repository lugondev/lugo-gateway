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

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "apps" / "api_gateway" / "app"

# The methods that actually reach a provider (network or local inference).
_PROVIDER_METHODS = {
    "transcribe_bytes", "synthesize", "reply_stream", "reply", "open_stream",
    "embed_texts", "embed_texts_with_usage",
}

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
#   "exempt" -- real provider spend, deliberately left unmetered or ungated, with
#       the reason stating why -- NOT a bucket for "couldn't otherwise categorize"
#   "not-a-provider-call" -- the pattern matched a symbol that is not actually a
#       provider call (e.g. a same-named local helper); the reason names what it
#       really is
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
        "ChunkedStreamTranscriber is not constructed by any route -- only from "
        "its own module and a unit test with a local stub -- so it is dead code "
        "with no live metering exposure, not a route sharing base.py's coverage",
        "tests/unit/test_stt_streaming_chunked.py",
    ),
    ("api/routes/model_registry.py", "transcribe_bytes"): (
        1, "exempt",
        "add-time credential test; metered but never gated, or an admin over "
        "quota could not validate the provider needed to fix it",
        "tests/unit/test_model_registry_test_call_metering.py",
    ),
    ("api/routes/model_registry.py", "synthesize"): (
        1, "exempt",
        "add-time credential test; metered but never gated, same as "
        "transcribe_bytes above",
        "tests/unit/test_model_registry_test_call_metering.py",
    ),
    ("api/routes/model_registry.py", "reply"): (
        1, "exempt",
        "add-time credential test; metered but never gated, same as "
        "transcribe_bytes above",
        "tests/unit/test_model_registry_test_call_metering.py",
    ),
    ("api/routes/model_registry.py", "embed_texts"): (
        1, "exempt",
        "add-time credential test; metered but never gated, same as "
        "transcribe_bytes above",
        "tests/unit/test_model_registry_test_call_metering.py",
    ),
}

_VALID_STATUSES = {
    "metered+gated", "covered-by-caller", "exempt", "not-a-provider-call",
}


def _found_call_sites(root: Path = APP) -> dict[tuple[str, str], int]:
    """Parse each file with Python's own grammar instead of pattern-matching
    text -- a hand-written quote tracker keeps finding new ways to be fooled
    by what a string literal looks like (see git history of this function).
    ast.parse never confuses a docstring, a comment, or a string containing
    a stray quote character for code, because it isn't guessing from text at
    all: a docstring is a string node, not a Call node, full stop.
    """
    found: dict[tuple[str, str], int] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in rel or any(rel.startswith(p) for p in _IMPLEMENTATIONS):
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise AssertionError(
                f"{rel}: could not be parsed as Python -- the inventory scanner "
                f"cannot see calls in this file, which is a problem in its own "
                f"right, not something to silently skip past: {exc}"
            ) from exc
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                continue
            if name in _PROVIDER_METHODS:
                found[(rel, name)] = found.get((rel, name), 0) + 1
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


def test_scanner_finds_a_call_whose_argument_is_a_multiline_triple_quoted_string(
    tmp_path,
):
    """A real call site does not stop being real just because one of its
    arguments happens to be a triple-quoted string spanning several lines."""
    (tmp_path / "caller.py").write_text(
        'result = await provider.synthesize("""multi\n'
        "line prompt\n"
        'goes here""")\n'
    )
    found = _found_call_sites(tmp_path)
    assert ("caller.py", "synthesize") in found


def test_scanner_finds_code_after_a_closing_delimiter_on_the_same_line(tmp_path):
    """A docstring closing mid-line does not erase the real code that follows
    it on that same physical line."""
    (tmp_path / "caller.py").write_text(
        '"""\n'
        "docstring\n"
        '"""; return provider.synthesize(x)\n'
    )
    found = _found_call_sites(tmp_path)
    assert ("caller.py", "synthesize") in found


def test_scanner_does_not_mistake_docstring_prose_for_a_call(tmp_path):
    """Prose that merely reads like a call, inside a real docstring, must not
    be found -- this is the false-positive class the scanner exists to avoid."""
    (tmp_path / "caller.py").write_text(
        "def f():\n"
        '    """This reply (looks like a call) but is not.\n'
        "\n"
        "    Still just prose about synthesize(x) in here.\n"
        '    """\n'
        "    return 1\n"
    )
    found = _found_call_sites(tmp_path)
    assert not found


def test_scanner_finds_a_call_near_a_string_with_an_apostrophe(tmp_path):
    """A stray apostrophe in a nearby ordinary string must not be mistaken for
    the start of a triple-quoted block and swallow the real call after it."""
    (tmp_path / "caller.py").write_text(
        "text = \"it's fine\"\n" "await provider.synthesize(text)\n"
    )
    found = _found_call_sites(tmp_path)
    assert ("caller.py", "synthesize") in found


def test_scanner_skips_def_lines_and_comments(tmp_path):
    (tmp_path / "caller.py").write_text(
        "async def synthesize(self, payload):\n"
        "    # await provider.synthesize(payload) -- not a real call\n"
        "    return None\n"
    )
    found = _found_call_sites(tmp_path)
    assert not found


def test_scanner_finds_a_call_after_a_string_containing_a_literal_triple_quote(
    tmp_path,
):
    """A `\"\"\"` sequence inside an ordinary quoted string is just three
    characters, not a string-opening delimiter -- a hand-written quote tracker
    can be fooled into thinking it opens a triple-quoted block and silently
    blank every line after it for the rest of the file. ast.parse never makes
    this mistake because it parses the real grammar."""
    (tmp_path / "caller.py").write_text(
        "x = 'contains \"\"\" literally'\n"
        "await provider.synthesize(payload)\n"
    )
    found = _found_call_sites(tmp_path)
    assert ("caller.py", "synthesize") in found


def test_scanner_finds_a_call_written_across_multiple_lines(tmp_path):
    """A call is still a call when its argument list is wrapped across lines --
    ast.parse works from the parsed structure, not from what's on one physical
    line, so this can never be a blind spot the way a line-oriented pattern
    could be."""
    (tmp_path / "caller.py").write_text(
        "provider.synthesize(\n"
        "    payload,\n"
        ")\n"
    )
    found = _found_call_sites(tmp_path)
    assert ("caller.py", "synthesize") in found


def test_every_classification_names_a_test_that_exists():
    """A row claiming coverage from a test that does not exist is worse than no
    row at all: it reads as proof.

    Also guards the "exempt" vocabulary itself: exempt means real provider spend
    someone deliberately chose not to gate, not a dumping ground for anything hard
    to categorize. Requiring the word "gate" in the reason keeps that distinction
    from eroding -- a row that can't say why it isn't gated probably isn't exempt.
    """
    repo_root = Path(__file__).resolve().parents[2]
    missing = []
    for key, (_count, status, reason, covering_test) in sorted(_CLASSIFIED.items()):
        assert status in _VALID_STATUSES, f"{key}: unknown status {status!r}"
        assert reason.strip(), f"{key}: a classification needs a reason"
        if status == "exempt":
            assert "gate" in reason.lower(), (
                f"{key}: an exempt reason must explain why it isn't gated"
            )
        if not (repo_root / covering_test).is_file():
            missing.append(f"{key}: {covering_test}")
    assert not missing, "Covering test file(s) do not exist:\n  " + "\n  ".join(missing)
