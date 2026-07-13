from starlette.requests import Request

from app.core.actor import current_role, current_user_id


class _FakeRequest:
    def __init__(self, session: dict):
        self.session = session


def test_current_role_defaults_to_admin_when_session_empty():
    assert current_role(_FakeRequest({})) == "admin"


def test_current_role_returns_actual_role_when_present():
    assert current_role(_FakeRequest({"role": "user"})) == "user"


def test_current_user_id_returns_none_when_absent():
    assert current_user_id(_FakeRequest({})) is None


def test_current_user_id_returns_value_when_present():
    assert current_user_id(_FakeRequest({"user_id": "u1"})) == "u1"
