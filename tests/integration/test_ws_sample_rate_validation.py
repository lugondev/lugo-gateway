"""`sample_rate` / `output_sample_rate` are client-controlled query params that
were cast with a bare `int()` and used unchecked.

routes/stt.py already refuses a non-positive rate with a written rationale
("a non-positive value has no honest duration to bill"); conversation.py did
the same cast with no guard, so `?sample_rate=0` reached VadEndpointer and
divided by it, and `?sample_rate=abc` raised ValueError -- both AFTER
websocket.accept(), i.e. as an unhandled crash in the handler rather than an
error the client can read. The livehost plugin's own upstream connection to
this same route inherits the guard for free -- it is not a call site of its
own anymore.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

BAD_RATES = ["0", "-1", "abc", ""]


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.parametrize("value", BAD_RATES)
def test_conversation_stream_refuses_a_bad_sample_rate(client, value):
    with client.websocket_connect(
        f"/v1/conversation/stream?sample_rate={value}&output=text"
    ) as ws:
        message = ws.receive_json()
    assert message["event"] == "error"
    assert "sample_rate" in message["message"]


@pytest.mark.parametrize("value", BAD_RATES)
def test_conversation_stream_refuses_a_bad_output_sample_rate(client, value):
    with client.websocket_connect(
        f"/v1/conversation/stream?output_sample_rate={value}&output=text"
    ) as ws:
        message = ws.receive_json()
    assert message["event"] == "error"
    assert "output_sample_rate" in message["message"]


def test_an_absurd_sample_rate_is_refused_rather_than_allocated(client):
    """Unbounded above as well as below: the rate sizes Opus frame buffers and
    the silence padding, so a huge value is an allocation lever."""
    with client.websocket_connect(
        "/v1/conversation/stream?sample_rate=999999999&output=text"
    ) as ws:
        message = ws.receive_json()
    assert message["event"] == "error"
    assert "sample_rate" in message["message"]


def test_a_normal_sample_rate_still_connects(client):
    with client.websocket_connect(
        "/v1/conversation/stream?sample_rate=16000&output=text"
    ) as ws:
        message = ws.receive_json()
    assert message["event"] == "session_started"
