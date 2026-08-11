from app.services.auth.tokens import (
    PLUGIN_SESSION_TTL_SECONDS,
    PLUGIN_TICKET_TTL_SECONDS,
    issue_access_token,
    issue_plugin_session_token,
    issue_plugin_token,
    verify_access_token,
    verify_plugin_session_token,
    verify_plugin_token,
)


def test_a_ticket_round_trips_for_its_own_plugin():
    token = issue_plugin_token("user-1", "livehost")
    assert verify_plugin_token(token, "livehost") == "user-1"


def test_a_ticket_minted_for_one_plugin_is_worthless_at_another():
    token = issue_plugin_token("user-1", "livehost")
    assert verify_plugin_token(token, "lugo") is None


def test_a_ticket_is_not_an_access_token():
    """Salt separation runs both ways: a ticket must not open the bearer path,
    and an access token must not open a plugin."""
    ticket = issue_plugin_token("user-1", "livehost")
    assert verify_access_token(ticket) is None
    access = issue_access_token("user-1")
    assert verify_plugin_token(access, "livehost") is None


def test_an_expired_ticket_is_refused(monkeypatch):
    """Proves the TTL is actually consulted, not merely that _verify catches
    SignatureExpired. verify_plugin_token reads the module-level constant at
    call time, so shrinking it below zero makes a ticket issued a moment ago
    genuinely expired -- through real itsdangerous, with no clock to wait on
    and no shared class left patched."""
    from app.services.auth import tokens

    token = issue_plugin_token("user-1", "livehost")
    assert verify_plugin_token(token, "livehost") == "user-1"

    monkeypatch.setattr(tokens, "PLUGIN_TICKET_TTL_SECONDS", -1)
    assert verify_plugin_token(token, "livehost") is None


def test_garbage_is_refused():
    assert verify_plugin_token("", "livehost") is None
    assert verify_plugin_token("not-a-token", "livehost") is None


def test_the_ttl_is_short_because_the_ticket_travels_in_a_query_string():
    assert PLUGIN_TICKET_TTL_SECONDS <= 300


# --- plugin session tokens: server-to-server only, never handed to a browser ---


def test_a_session_token_round_trips():
    token = issue_plugin_session_token("user-1")
    assert verify_plugin_session_token(token) == "user-1"


def test_a_session_token_is_not_an_access_token_and_not_a_plugin_ticket():
    """Different salt from both -- an access token minted separately must not
    verify as a session token, and vice versa, and neither substitutes for a
    plugin ticket."""
    session = issue_plugin_session_token("user-1")
    assert verify_access_token(session) is None
    access = issue_access_token("user-1")
    assert verify_plugin_session_token(access) is None
    ticket = issue_plugin_token("user-1", "livehost")
    assert verify_plugin_session_token(ticket) is None


def test_a_session_token_is_not_scoped_to_a_plugin():
    """Unlike the ticket, this token isn't audience-bound to which plugin
    requested it -- once minted it just IS that user at ordinary privilege,
    same as verify_access_token gives. Nothing to bind: no plugin argument
    exists on the issue/verify pair at all."""
    token = issue_plugin_session_token("user-1")
    assert verify_plugin_session_token(token) == "user-1"


def test_an_expired_session_token_is_refused(monkeypatch):
    from app.services.auth import tokens

    token = issue_plugin_session_token("user-1")
    assert verify_plugin_session_token(token) == "user-1"

    monkeypatch.setattr(tokens, "PLUGIN_SESSION_TTL_SECONDS", -1)
    assert verify_plugin_session_token(token) is None


def test_session_token_garbage_is_refused():
    assert verify_plugin_session_token("") is None
    assert verify_plugin_session_token("not-a-token") is None


def test_the_session_ttl_is_short_despite_never_touching_a_browser():
    """It never leaves the plugin's own process, so a long TTL couldn't leak
    the way the ticket's could -- but the plugin uses it exactly once,
    immediately, to open one socket, so there is no reason to hold it
    longer than the ticket it was traded for."""
    assert PLUGIN_SESSION_TTL_SECONDS <= 300
