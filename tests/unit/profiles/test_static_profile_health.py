"""The Conversation profile bar shows `GET /v1/profiles/{name}/health` as a badge.

That route's own docstring says it exists "so the admin UI can show a profile as
broken before a user tries to talk to it" -- it sat unwired for a long time, so
these tests keep the wiring from quietly rotting back out.

Two things here are easy to get wrong in a way no one notices until it misleads
someone:

1. **The stale-response race.** The probe hits real engines, so it is slow enough
   that switching profiles quickly lets an older probe land last. Without a
   request ticket the badge then describes the PREVIOUS profile -- and the bad
   direction is showing "ready" over a profile that cannot start.

2. **`not_ready` is not a failure.** Only `unavailable` blocks a session
   (`EngineHealth.blocks_session`). A model that is still loading just makes the
   first turn slow; painting it red trains people to ignore red.
"""

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[3] / "apps" / "api_gateway" / "app" / "static"
PROFILES_JS = STATIC / "js" / "profiles.js"


@pytest.fixture(scope="module")
def source() -> str:
    return PROFILES_JS.read_text(encoding="utf-8")


def test_badge_element_exists(source: str) -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="profile-health"' in index, "no badge element in the profile bar"
    assert 'el("profile-health")' in source, "profiles.js never looks the badge up"


def test_health_probe_is_wired_to_the_profile_selector(source: str) -> None:
    """Unwired, the whole thing is dead code -- which is the state this replaced."""
    assert "refreshProfileHealth()" in source, "refreshProfileHealth is never called"
    change_handler = source[source.index('el("profile-select").addEventListener'):]
    assert "refreshProfileHealth()" in change_handler[:400], (
        "the badge never refreshes when the selected profile changes"
    )


def test_stale_probe_cannot_paint_the_badge(source: str) -> None:
    body = source[source.index("export async function refreshProfileHealth"):]
    body = body[: body.index("\nexport ")] if "\nexport " in body else body

    assert "healthRequestSeq" in body, (
        "no request ticket -- a slow probe for the previously selected profile can "
        "land last and label the current one"
    )

    # Derived from the awaits present, not a fixed count: every suspension point
    # reopens the race, so adding an await must force adding a guard. A hardcoded
    # threshold silently passes the moment someone adds a third await -- or
    # deletes one guard while leaving enough behind to satisfy the number.
    lines = body.splitlines()
    unguarded = []
    for i, line in enumerate(lines):
        if "await" not in line:
            continue
        following = "\n".join(lines[i + 1 : i + 3])
        if "ticket !== healthRequestSeq" not in following:
            unguarded.append(f"line {i + 1}: {line.strip()}")

    assert not unguarded, (
        "await(s) in refreshProfileHealth with no ticket re-check on the next "
        "line(s) -- a probe for the previously selected profile can resume here "
        "and paint the badge for the wrong profile:\n  " + "\n  ".join(unguarded)
    )


def test_not_ready_does_not_render_as_an_error(source: str) -> None:
    view = source[source.index("const HEALTH_VIEW"): source.index("function worstStatus")]
    not_ready = [ln for ln in view.splitlines() if "not_ready" in ln]
    assert not_ready, "HEALTH_VIEW has no not_ready state"
    assert "danger" not in not_ready[0], (
        "not_ready is painted as an error, but only `unavailable` blocks a session"
    )


def test_worst_status_wins(source: str) -> None:
    """A profile is only as good as its weaker half: healthy STT plus a dead TTS
    is a profile that cannot hold a voice conversation."""
    fn = source[source.index("function worstStatus"):]
    fn = fn[: fn.index("\n}") + 2]
    assert fn.index("unavailable") < fn.index("not_ready"), (
        "worstStatus must check unavailable before not_ready, or a dead engine "
        "gets reported as merely loading"
    )
