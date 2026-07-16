from starlette.requests import Request

from app.core.actor import Actor, current_role, current_user_id


def _request(session: dict, actor: Actor | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "session": session,
        "state": {},
    }
    request = Request(scope)
    if actor is not None:
        request.state.actor = actor
    return request


def test_session_path_unchanged_for_admin():
    request = _request({"user_id": "u1", "role": "admin"})
    assert current_user_id(request) == "u1"
    assert current_role(request) == "admin"


def test_session_missing_role_still_falls_back_to_admin():
    """Hành vi dev-mode cũ giữ nguyên -- task này không sửa nhánh session."""
    request = _request({"user_id": "u1"})
    assert current_role(request) == "admin"


def test_state_actor_takes_precedence_over_session():
    request = _request({"user_id": "u1", "role": "admin"}, actor=Actor(user_id="u2", role="user"))
    assert current_user_id(request) == "u2"
    assert current_role(request) == "user"


def test_state_actor_with_empty_session():
    request = _request({}, actor=Actor(user_id="u2", role="user"))
    assert current_user_id(request) == "u2"
    assert current_role(request) == "user"
