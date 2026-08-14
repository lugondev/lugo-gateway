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


def test_run_selector_excludes_shared(profiles_js: str) -> None:
    body = profiles_js[profiles_js.index("export function renderProfileSelect"):]
    body = body[: body.index("export function renderLivehostProfileSelect")]
    assert ".shared" in body, "renderProfileSelect never looks at the shared flag"


def test_livehost_selector_excludes_shared(profiles_js: str) -> None:
    body = profiles_js[profiles_js.index("export function renderLivehostProfileSelect"):]
    body = body[: body.index("export function renderProfileTtsSelect")]
    assert ".shared" in body


def test_edit_panel_keys_readonly_off_shared_not_owner_id(profiles_js: str) -> None:
    body = profiles_js[profiles_js.index("export async function openProfilePanel"):]
    body = body[: body.index("export function closeProfilePanel")]
    assert "p.shared" in body
    assert "p.owner_id === null" not in body, "still keying read-only off the old rule"


def test_shared_checkbox_is_admin_only(profiles_js: str, index_html: str) -> None:
    assert 'id="pf-shared"' in index_html
    body = profiles_js[profiles_js.index("export async function openProfilePanel"):]
    body = body[: body.index("export function closeProfilePanel")]
    assert "isAdmin" in body and "pf-shared" in body


def test_device_binding_pickers_exclude_shared(devices_js: str) -> None:
    pair = devices_js[devices_js.index("export function renderDevicePairProfileSelect"):]
    pair = pair[: pair.index("export function renderAllDeviceFilterProfileOptions")]
    assert ".shared" in pair

    per_device = devices_js[devices_js.index("function myDeviceProfileColumn"):]
    per_device = per_device[:600]
    assert ".shared" in per_device


def test_all_devices_filter_still_lists_every_name(devices_js: str) -> None:
    """It filters a read-only table rather than writing a binding, so hiding
    shared names there would only make a legacy binding invisible."""
    body = devices_js[devices_js.index("export function renderAllDeviceFilterProfileOptions"):]
    body = body[: body.index("export async function loadMyDevices")]
    assert ".shared" not in body
