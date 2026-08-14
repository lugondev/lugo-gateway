"""The three-way split of the old single visibility rule.

`visible` governs reads (list/get/clone-source/health): a shared template is
readable by everyone. `usable` governs RUNNING on a profile (conversation, WS,
stt warm, session resume, device bind): a shared template is usable by NOBODY,
including the admin who owns it -- that is the whole point of "clone-only".
"""

import pytest

from app.services.profile_visibility import (
    is_shared_template,
    profile_usable,
    profile_visible,
    usable_profile_or_none,
    visible_profile_or_none,
)
from app.services.profiles.models import Profile


def _p(**kw) -> Profile:
    return Profile(name="p", **kw)


OWNED = _p(owner_id="alice")
OTHERS = _p(owner_id="bob")
SHARED_OWNED = _p(owner_id="alice", shared=True)
SHARED_OWNERLESS = _p(owner_id=None, shared=True)
DEV_MODE = _p(owner_id=None)  # auth disabled: caller_id is None too


@pytest.mark.parametrize(
    "profile,caller,visible,usable",
    [
        (OWNED, "alice", True, True),
        (OWNED, "bob", False, False),
        (OTHERS, "alice", False, False),
        # Shared is readable by everyone and runnable by no one -- including
        # its own owner, who must clone it like anybody else.
        (SHARED_OWNED, "alice", True, False),
        (SHARED_OWNED, "bob", True, False),
        (SHARED_OWNERLESS, "alice", True, False),
        (SHARED_OWNERLESS, None, True, False),
        # Dev mode (settings.auth_enabled False): owner_id and caller_id are
        # both None, so the owned path matches and nothing changes.
        (DEV_MODE, None, True, True),
    ],
)
def test_predicate_table(profile, caller, visible, usable):
    assert profile_visible(profile, caller) is visible
    assert profile_usable(profile, caller) is usable


def test_default_is_not_shared():
    assert Profile(name="p").shared is False


def test_usable_or_none_collapses_shared_to_none():
    assert usable_profile_or_none(SHARED_OWNED, "alice") is None
    assert visible_profile_or_none(SHARED_OWNED, "alice") is SHARED_OWNED


def test_bypass_does_not_resurrect_a_shared_template():
    """`bypass` exists for dev mode's "no way to prove ownership of anything".
    Shared is not an ownership question -- nobody may run on it, so bypass must
    not become a back door into one."""
    assert usable_profile_or_none(SHARED_OWNED, None, bypass=True) is None
    assert usable_profile_or_none(OTHERS, None, bypass=True) is OTHERS


def test_is_shared_template_tolerates_none():
    assert is_shared_template(None) is False
    assert is_shared_template(OWNED) is False
    assert is_shared_template(SHARED_OWNED) is True
