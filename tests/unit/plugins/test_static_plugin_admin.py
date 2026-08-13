"""The admin console is a real write-client of /v1/plugins, and one specific
round-trip there silently destroys a credential.

`GET /v1/plugins` masks `secret` to "***" for a non-admin reader (routes/plugins.py
`_view`), and `PUT /v1/plugins/{name}` is a FULL replace with no partial update. So
a client that reads a row and writes it back unchanged overwrites the real secret
with the literal "***" -- after which every `POST /api/auth/introspect` that plugin
makes fails its `hmac.compare_digest` check, and every browser ticket for it stops
resolving. Nothing server-side can catch this: "***" is a perfectly valid secret
string as far as the API is concerned.

These tests pin the guards in plugins-admin.js that make the round-trip refuse
instead. They are deliberately about the *shape* of the client code rather than its
behavior -- there is no JS test harness for the static console -- so they check that
the guard exists on both write paths, not what it renders.
"""

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[3] / "apps" / "api_gateway" / "app" / "static"
JS = STATIC / "js"
ADMIN_JS = JS / "plugins-admin.js"


@pytest.fixture(scope="module")
def source() -> str:
    return ADMIN_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code(source: str) -> str:
    """`source` minus whole-line `//` comments.

    The comments explaining this bug quote the mask verbatim, and counting those
    as occurrences would make the assertion below fire on good documentation.
    """
    return "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("//")
    )


def test_masked_secret_placeholder_is_named_once(code: str) -> None:
    """The mask must be a named constant, not "***" sprinkled through the file --
    the guards below are only trustworthy if every check compares the same thing."""
    assert 'const MASKED_SECRET = "***"' in code, (
        "plugins-admin.js must define MASKED_SECRET so both write paths compare "
        "against one definition of the mask"
    )
    # The literal legitimately appears in the constant's own definition. A second
    # occurrence in real code means a comparison drifted off the constant.
    assert code.count('"***"') == 1, (
        'a bare "***" literal outside the MASKED_SECRET definition -- compare '
        "against the constant instead"
    )


def test_save_path_refuses_to_write_the_mask_back(source: str) -> None:
    save = _function_body(source, "export async function savePlugin()")
    assert "MASKED_SECRET" in save, (
        "savePlugin() writes `secret` straight through. An admin editing any other "
        "field on a row whose secret came back masked would replace the real secret "
        "with '***'. Compare against MASKED_SECRET and refuse."
    )


def test_toggle_path_refuses_to_write_the_mask_back(source: str) -> None:
    """The enabled-toggle is the sneakier of the two: it never shows the secret to
    anyone, it just resends the cached row because PUT is a full replace."""
    toggle = _function_body(source, "async function _setEnabledRaw(name, enabled)")
    assert "MASKED_SECRET" in toggle, (
        "_setEnabledRaw() resends the cached row to satisfy PUT's full-replace "
        "contract. If that cached secret is the mask, flipping the Enabled "
        "checkbox destroys the credential. Guard it like savePlugin() does."
    )


def test_admin_console_registers_the_plugins_section(source: str) -> None:
    """A module nothing routes to is dead code; these three wirings are what make
    the tab reachable."""
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    sidebar = (JS / "sidebar-nav.js").read_text(encoding="utf-8")

    assert 'data-section="plugins"' in index, "no nav item for the plugins section"
    assert 'id="section-plugins"' in index, "no panel for the plugins section"
    assert 'if (section === "plugins") loadPluginsAdmin();' in sidebar, (
        "sidebar-nav.js never loads the plugins section on activation"
    )


def test_plugins_nav_item_is_admin_only(source: str) -> None:
    """Write access to /v1/plugins is admin-only server-side (routes/plugins.py
    `_require_admin`), so showing the tab to a user would render a page whose every
    button 403s."""
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    marker = 'data-section="plugins"'
    before = index[: index.index(marker)]
    enclosing_li = before.rindex("<li")
    assert 'class="admin-only"' in index[enclosing_li : index.index(marker)], (
        "the plugins nav item must sit in an <li class=\"admin-only\">"
    )


def _function_body(source: str, signature: str) -> str:
    """Text from `signature` to the start of the next top-level declaration.

    Crude on purpose: it only has to be tight enough that a guard placed in a
    *different* function doesn't satisfy the assertion for this one.
    """
    assert signature in source, f"{signature} not found in plugins-admin.js"
    start = source.index(signature)
    rest = source[start + len(signature) :]
    ends = [rest.index(m) for m in ("\nexport ", "\nasync function ", "\nfunction ") if m in rest]
    return rest[: min(ends)] if ends else rest
