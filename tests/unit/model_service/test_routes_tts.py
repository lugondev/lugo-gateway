import base64

from fastapi.testclient import TestClient

from app.core.errors import ProviderError
from app.schemas.tts import TTSRequest
from model_service.app.config import ServiceConfig
from model_service.app.main import create_app

_CFG = ServiceConfig(kind="tts", engine="vieneu", api_token="t0ken")
_AUTH = {"Authorization": "Bearer t0ken"}


class _FakeTTS:
    name = "vieneu"

    def __init__(self, exc: Exception | None = None, voices=None, clone: bool = False):
        self.calls: list[TTSRequest] = []
        self._exc = exc
        self._voices = voices or []
        self._clone = clone

    async def render_wav(self, payload: TTSRequest) -> bytes:
        self.calls.append(payload)
        if self._exc:
            raise self._exc
        return b"RIFFWAVEDATA"

    async def list_voices(self) -> list[dict]:
        return self._voices

    async def supports_voice_clone(self) -> bool:
        return self._clone


def _client(provider):
    return TestClient(create_app(config=_CFG, provider=provider))


def test_synthesizes_and_returns_wav_bytes():
    r = _client(_FakeTTS()).post("/v1/audio/speech", headers=_AUTH, json={"input": "xin chào"})
    assert r.status_code == 200
    assert r.content == b"RIFFWAVEDATA"
    assert r.headers["content-type"] == "audio/wav"


def test_maps_openai_fields_onto_the_tts_request():
    provider = _FakeTTS()
    _client(provider).post(
        "/v1/audio/speech",
        headers=_AUTH,
        json={"input": "xin chào", "voice": "vi-female-1", "speed": 1.25},
    )
    payload = provider.calls[0]
    assert (payload.text, payload.voice, payload.speed, payload.engine) == (
        "xin chào", "vi-female-1", 1.25, "vieneu",
    )


def test_requires_auth():
    assert _client(_FakeTTS()).post("/v1/audio/speech", json={"input": "hi"}).status_code == 401


def test_empty_input_is_rejected():
    r = _client(_FakeTTS()).post("/v1/audio/speech", headers=_AUTH, json={"input": ""})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_provider_error_becomes_502():
    r = _client(_FakeTTS(exc=ProviderError("vieneu synthesis failed: oom"))).post(
        "/v1/audio/speech", headers=_AUTH, json={"input": "xin chào"}
    )
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "provider_error"


def test_tts_container_does_not_expose_transcriptions():
    r = _client(_FakeTTS()).post(
        "/v1/audio/transcriptions", headers=_AUTH, files={"file": ("a.wav", b"D", "audio/wav")}
    )
    assert r.status_code == 404


def test_models_lists_the_running_engine():
    r = _client(_FakeTTS()).get("/v1/models", headers=_AUTH)
    assert [m["id"] for m in r.json()["data"]] == ["vieneu"]


def test_voices_route_returns_the_providers_schema():
    """This is the "schema" a deployed model_service instance returns so the
    gateway's HttpTtsProvider knows what the remote engine supports,
    without hardcoding per-engine special cases."""
    provider = _FakeTTS(voices=[{"label": "Host", "voice": "host"}], clone=True)
    r = _client(provider).get("/v1/voices", headers=_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == [{"label": "Host", "voice": "host"}]
    assert body["supports_clone"] is True


def test_voices_route_requires_auth():
    r = _client(_FakeTTS()).get("/v1/voices")
    assert r.status_code == 401


def test_create_speech_decodes_ref_audio_base64_to_a_temp_path():
    provider = _FakeTTS()
    ref_bytes = b"RIFF-fake-reference-wav-bytes"
    _client(provider).post(
        "/v1/audio/speech",
        headers=_AUTH,
        json={
            "input": "xin chào",
            "ref_audio_base64": base64.b64encode(ref_bytes).decode("ascii"),
            "ref_text": "reference words",
        },
    )
    payload = provider.calls[0]
    assert payload.ref_text == "reference words"
    assert payload.ref_audio_path is not None
    from pathlib import Path

    # The temp file existed long enough to be read by the (fake) provider's
    # render_wav call, which already ran by the time the response returned --
    # but it must be cleaned up afterward, not leaked.
    assert not Path(payload.ref_audio_path).exists()


def test_create_speech_writes_ref_audio_temp_file_with_restrictive_permissions():
    """round-2 Minor: the decode target sits in an HTTP-served artifacts dir
    (see routes_tts.py's comment on why it has to live there at all -- the
    ref_audio_path containment check from task 5). A plain open(path, "wb")
    creates it at the umask default (usually 0644, world-readable); the
    tempfile.NamedTemporaryFile this replaced always created 0600. Must not
    regress to a wider mode just because it's no longer a NamedTemporaryFile."""
    import os
    import stat

    captured: dict = {}

    class _PermCheckingTTS:
        name = "vieneu"

        async def render_wav(self, payload: TTSRequest) -> bytes:
            st = os.stat(payload.ref_audio_path)
            captured["mode"] = stat.S_IMODE(st.st_mode)
            return b"RIFFWAVEDATA"

    _client(_PermCheckingTTS()).post(
        "/v1/audio/speech",
        headers=_AUTH,
        json={"input": "xin chào", "ref_audio_base64": base64.b64encode(b"wav-bytes").decode("ascii")},
    )
    assert captured.get("mode") == 0o600, oct(captured.get("mode", -1))


def test_create_speech_without_ref_audio_leaves_ref_audio_path_unset():
    provider = _FakeTTS()
    _client(provider).post("/v1/audio/speech", headers=_AUTH, json={"input": "hi"})
    assert provider.calls[0].ref_audio_path is None


def test_create_speech_rejects_oversized_ref_audio_base64():
    """LOW: ref_audio_base64 was decoded fully into memory before any size
    check -- gated behind the service bearer token, so LOW severity, but an
    unbounded decode is still worth bounding. The check must reject before
    base64.b64decode ever runs (not after), so build a string well past the
    cap without needing it to be valid base64."""
    from model_service.app.routes_tts import _REF_AUDIO_BASE64_MAX_CHARS

    provider = _FakeTTS()
    oversized = "A" * (_REF_AUDIO_BASE64_MAX_CHARS + 1)
    r = _client(provider).post(
        "/v1/audio/speech",
        headers=_AUTH,
        json={"input": "xin chào", "ref_audio_base64": oversized},
    )
    assert r.status_code == 413
    assert provider.calls == []  # rejected before render_wav ever ran


def test_create_speech_accepts_ref_audio_base64_at_the_cap():
    from model_service.app.routes_tts import _REF_AUDIO_BASE64_MAX_CHARS

    provider = _FakeTTS()
    # Real base64 (not a bare repeated char) sized just under the cap.
    ref_bytes = b"x" * (_REF_AUDIO_BASE64_MAX_CHARS // 2)
    encoded = base64.b64encode(ref_bytes).decode("ascii")
    assert len(encoded) <= _REF_AUDIO_BASE64_MAX_CHARS
    r = _client(provider).post(
        "/v1/audio/speech",
        headers=_AUTH,
        json={"input": "xin chào", "ref_audio_base64": encoded},
    )
    assert r.status_code == 200
    assert len(provider.calls) == 1
