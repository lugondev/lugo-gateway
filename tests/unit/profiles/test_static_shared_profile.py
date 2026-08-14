"""The console's shared-profile wiring.

`#profile-select` does double duty -- it is both "what does my conversation run
on" and the only route to Edit/Clone (profiles.js's profile-edit-btn handler).
Filtering shared rows out of it therefore has to come WITH a separate templates
picker, or shared profiles become unreachable and the one thing users are
supposed to do with them (clone) becomes impossible.
"""

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[3] / "apps" / "api_gateway" / "app" / "static"


@pytest.fixture(scope="module")
def profiles_js() -> str:
    return (STATIC / "js" / "profiles.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def devices_js() -> str:
    return (STATIC / "js" / "devices.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def test_templates_picker_exists(index_html: str) -> None:
    assert 'id="profile-template-select"' in index_html
    assert 'id="profile-template-clone-btn"' in index_html


def test_templates_picker_is_wired(profiles_js: str) -> None:
    assert "renderProfileTemplateSelect" in profiles_js
    assert 'el("profile-template-clone-btn")' in profiles_js

    # Defining the renderer is not enough to prove it runs: `renderProfileTemplateSelect`
    # appears in the assertions above merely because it's the function's own
    # definition. If loadProfiles() never calls it, #profile-template-select stays
    # permanently empty -- so pin the call to inside loadProfiles's own body.
    body = profiles_js[profiles_js.index("export async function loadProfiles"):]
    body = body[: body.index("export function renderProfileSelect")]
    assert "renderProfileTemplateSelect()" in body, (
        "renderProfileTemplateSelect is defined but loadProfiles() never calls it -- "
        "the templates dropdown would never populate"
    )


def test_run_selector_excludes_shared(profiles_js: str) -> None:
    body = profiles_js[profiles_js.index("export function renderProfileSelect"):]
    body = body[: body.index("export function renderLivehostProfileSelect")]
    # Pin the literal negated predicate, not just ".shared" appearing somewhere --
    # a flipped filter (showing ONLY shared, unrunnable profiles) would still
    # satisfy a bare ".shared" substring check.
    assert ".filter((n) => !profileData[n]?.shared)" in body, (
        "renderProfileSelect must filter shared profiles OUT via a negated predicate"
    )


def test_livehost_selector_excludes_shared(profiles_js: str) -> None:
    body = profiles_js[profiles_js.index("export function renderLivehostProfileSelect"):]
    body = body[: body.index("export function renderProfileTtsSelect")]
    assert ".filter((n) => !profileData[n]?.shared)" in body, (
        "renderLivehostProfileSelect must filter shared profiles OUT via a negated predicate"
    )


def test_templates_picker_includes_shared(profiles_js: str) -> None:
    """The templates picker is the mirror image of the run/bind selectors: it
    must show ONLY shared profiles. A stray `!` here would invert it to hide
    the one thing users are supposed to clone."""
    body = profiles_js[profiles_js.index("export function renderProfileTemplateSelect"):]
    body = body[: body.index("export function renderProfileTtsSelect")]
    assert ".filter((n) => profileData[n]?.shared)" in body
    assert ".filter((n) => !profileData[n]?.shared)" not in body, (
        "renderProfileTemplateSelect must NOT use the negated predicate -- that "
        "would hide shared templates instead of showing only them"
    )


def test_edit_panel_keys_readonly_off_shared_not_owner_id(profiles_js: str) -> None:
    body = profiles_js[profiles_js.index("export async function openProfilePanel"):]
    body = body[: body.index("export function closeProfilePanel")]
    assert "p.shared" in body
    assert "p.owner_id === null" not in body, "still keying read-only off the old rule"


def test_shared_checkbox_is_admin_only(profiles_js: str, index_html: str) -> None:
    assert 'id="pf-shared"' in index_html
    body = profiles_js[profiles_js.index("export async function openProfilePanel"):]
    body = body[: body.index("export function closeProfilePanel")]
    # Pin the literal toggle expression, not just "isAdmin" and "pf-shared"
    # appearing somewhere in the function -- both survive elsewhere in the body
    # (e.g. the pf-shared VALUE assignment, or the isAdmin computation itself),
    # so a bare substring check on each independently cannot fail even if the
    # admin-only toggle for pf-shared-label is deleted outright.
    assert 'el("pf-shared-label").classList.toggle("hidden", !isAdmin);' in body, (
        "pf-shared-label must be hidden for non-admins via the exact "
        "classList.toggle(\"hidden\", !isAdmin) expression"
    )


def test_device_binding_pickers_exclude_shared(devices_js: str) -> None:
    pair = devices_js[devices_js.index("export function renderDevicePairProfileSelect"):]
    pair = pair[: pair.index("export function renderAllDeviceFilterProfileOptions")]
    assert ".filter((n) => !profileData[n]?.shared)" in pair, (
        "renderDevicePairProfileSelect must filter shared profiles OUT via a negated predicate"
    )

    # Bound to the next function by name, not a magic byte count -- a fixed
    # slice silently stops checking anything once an earlier edit pushes the
    # filter line past it.
    per_device = devices_js[devices_js.index("function myDeviceProfileColumn"):]
    per_device = per_device[: per_device.index("function renderMyDeviceList")]
    assert ".filter((name) => !profileData[name]?.shared)" in per_device, (
        "myDeviceProfileColumn must filter shared profiles OUT via a negated predicate"
    )


def test_all_devices_filter_still_lists_every_name(devices_js: str) -> None:
    """It filters a read-only table rather than writing a binding, so hiding
    shared names there would only make a legacy binding invisible."""
    body = devices_js[devices_js.index("export function renderAllDeviceFilterProfileOptions"):]
    body = body[: body.index("export async function loadMyDevices")]
    assert ".shared" not in body
