"""The admin console's device table must not offer actions the server refuses.

`DeviceStore.set_name()` returns False for a revoked device -- it is a tombstone,
not a device the owner still has -- and the route turns that into a 404 that is
deliberately indistinguishable from "no such device". So a Rename button shown on a
revoked row can only ever produce a confusing "device not found" for a device the
user is looking straight at. Same for Revoke, which is already spent.

Renaming is also owner-scoped only: `POST /v1/devices/mine/{id}/name` exists,
`/v1/devices/{id}/name` does not. Wiring the button to an admin-shaped path would
404 for everyone, including admins.
"""

from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[2] / "apps" / "api_gateway" / "app" / "static" / "js"
DEVICES_JS = JS / "devices.js"


@pytest.fixture(scope="module")
def source() -> str:
    return DEVICES_JS.read_text(encoding="utf-8")


def test_rename_uses_the_owner_scoped_route(source: str) -> None:
    assert "/v1/devices/mine/${encodeURIComponent(id)}/name" in source, (
        "rename must POST to /v1/devices/mine/{id}/name -- there is no "
        "/v1/devices/{id}/name route, owner-scoped or not"
    )


def test_rename_and_revoke_are_disabled_on_a_revoked_device(source: str) -> None:
    """Both buttons live in the same `render` template, so check each carries its
    own `d.revoked ? "disabled"` -- one guard covering only the neighbour is the
    bug this pins."""
    problems = []
    for marker in ("data-device-rename", "data-device-revoke-mine"):
        assert marker in source, f"{marker} button is missing from the device table"
        start = source.index(marker)
        # The disabled expression sits between this button's attribute and the end
        # of its own tag; anything past `>` belongs to the next button.
        tag = source[start : source.index(">", start)]
        if 'd.revoked ? "disabled"' not in tag:
            problems.append(marker)
    assert not problems, (
        "button(s) still clickable on a revoked device, which the server refuses: "
        + ", ".join(problems)
    )
