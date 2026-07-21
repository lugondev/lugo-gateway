import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.audio import pcm16_to_wav_bytes
from app.core.settings import settings
from app.main import app
from app.schemas.tts import TTSResult
from app.services.stt.base import STTProvider
from app.services.stt.service import stt_service
from app.services.tts.base import TTSProvider
from app.services.tts.service import tts_service


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def _with_password(monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "s3cret")
    yield
    monkeypatch.setattr(settings, "admin_password", "")


def _signup_login(client, username: str, role: str = "user") -> None:
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        import asyncio

        from app.services.auth.users import user_store

        user = asyncio.run(user_store.get_by_username(username))
        asyncio.run(user_store.set_fields(user.id, role="admin"))
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


async def _signup_login_async(client, username: str, role: str = "user") -> None:
    """Same as `_signup_login`, but await-based rather than `asyncio.run()` --
    needed by `@pytest.mark.asyncio` tests, where `asyncio.run()` blows up
    with "cannot be called from a running event loop" since pytest-asyncio
    already has one running for the test itself."""
    client.post("/api/auth/signup", json={"username": username, "password": "pw"})
    if role == "admin":
        from app.services.auth.users import user_store

        user = await user_store.get_by_username(username)
        await user_store.set_fields(user.id, role="admin")
    client.post("/api/auth/login", json={"username": username, "password": "pw"})


class _OkStub(STTProvider):
    name = "stub-registry-ok"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None):
        from app.schemas.stt import STTResult
        return STTResult(engine=self.name, text="ok", is_final=True)


class _FailStub(STTProvider):
    name = "stub-registry-fail"

    async def transcribe_bytes(self, audio_bytes, language=None, model=None):
        raise RuntimeError("engine unavailable")


class _TtsOkStub(TTSProvider):
    name = "stub-tts-registry-ok"

    async def synthesize(self, payload):
        return TTSResult(engine=self.name, sample_rate=24000, audio_url="/artifacts/x.wav", text=payload.text)


@pytest.fixture(autouse=True)
def _register_stubs():
    stt_service.providers["stub-registry-ok"] = _OkStub()
    stt_service.providers["stub-registry-fail"] = _FailStub()
    tts_service.providers["stub-tts-registry-ok"] = _TtsOkStub()
    yield
    stt_service.providers.pop("stub-registry-ok", None)
    stt_service.providers.pop("stub-registry-fail", None)
    tts_service.providers.pop("stub-tts-registry-ok", None)


def test_regular_user_cannot_reach_model_registry(client, _with_password):
    _signup_login(client, "toan", role="user")
    resp = client.get("/v1/model_registry")
    assert resp.status_code == 403


def test_create_stt_entry_runs_real_test_call_and_succeeds(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1", "label": "Stub OK",
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is True


def test_create_stt_entry_test_call_fails_rejects_and_does_not_persist(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-fail", "model_id": "v1", "label": "Stub Fail",
    })
    assert resp.status_code == 400
    listed = client.get("/v1/model_registry").json()["data"]
    assert not any(e["engine"] == "stub-registry-fail" for e in listed)


def test_create_tts_entry_runs_real_test_call(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "tts", "engine": "stub-tts-registry-ok", "model_id": "stub-tts-registry-ok",
        "label": "Stub TTS OK", "sample_text": "xin chào",
    })
    assert resp.status_code == 200


def test_patch_toggles_enabled_and_stage_without_retest(client, _with_password):
    _signup_login(client, "root", role="admin")
    created = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1", "label": "Stub OK",
    }).json()["data"]
    resp = client.patch(f"/v1/model_registry/{created['id']}", json={"enabled": False, "stage": "testing"})
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False
    assert resp.json()["data"]["stage"] == "testing"


def test_create_stt_entry_persists_and_masks_api_key(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1", "label": "Stub OK",
        "api_key": "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345",
    })
    assert resp.status_code == 200
    masked = resp.json()["data"]["api_key"]
    assert masked == "sk-or-v1-abc...345"
    assert masked != "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345"

    listed = client.get("/v1/model_registry").json()["data"]
    entry = next(e for e in listed if e["engine"] == "stub-registry-ok")
    assert entry["api_key"] == "sk-or-v1-abc...345"


@pytest.mark.asyncio
async def test_listing_entries_does_not_mask_the_cached_key_read_by_hot_paths(client, _with_password):
    """Regression: GET /v1/model_registry used to mask api_key in place on the
    store's cached dicts, so after an admin merely OPENED the registry UI the
    LLM/STT hot paths (responder.py, openrouter_provider.py) resolved the
    masked string as the real key and failed provider auth until restart."""
    await _signup_login_async(client, "root", role="admin")
    real_key = "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345"
    client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1", "label": "Stub OK",
        "api_key": real_key,
    })

    listed = client.get("/v1/model_registry").json()["data"]
    assert listed[0]["api_key"] == "sk-or-v1-abc...345"  # response IS masked

    from app.services.model_registry.store import model_registry_store

    entry = await model_registry_store.find("stt", "stub-registry-ok", "v1")
    assert entry["api_key"] == real_key  # ...but the cache must keep the real key


def test_create_openrouter_stt_entry_uses_submitted_key_for_the_test_call(client, _with_password, monkeypatch):
    """qwen3_asr_or/whisper_or aren't backed by the stub providers registered
    for other tests -- the route must build a temporary OpenRouterSttProvider
    with the submitted api_key (not the fixed singleton, which would look up a
    registry entry that doesn't exist yet) to run the live test call."""
    _signup_login(client, "root", role="admin")

    async def fake_transcribe(self, audio_bytes, language=None, model=None):
        from app.schemas.stt import STTResult
        assert self.model == "qwen/qwen3-asr-flash-2026-02-10"
        return STTResult(engine=self.name, text="ok", is_final=True)

    from app.services.stt.providers.openrouter_provider import OpenRouterSttProvider
    monkeypatch.setattr(OpenRouterSttProvider, "transcribe_bytes", fake_transcribe)

    resp = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "qwen3_asr_or", "model_id": "qwen/qwen3-asr-flash-2026-02-10",
        "label": "Qwen3 ASR Flash", "api_key": "sk-or-test",
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["api_key"] == "***"  # short key -> full mask, not partial


def test_create_openrouter_stt_entry_without_key_fails_test_call(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "qwen3_asr_or", "model_id": "qwen/qwen3-asr-flash-2026-02-10",
        "label": "Qwen3 ASR Flash",
    })
    assert resp.status_code == 400
    assert "not configured" in resp.json()["detail"]


def test_create_tts_entry_persists_and_masks_api_key(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "tts", "engine": "stub-tts-registry-ok", "model_id": "stub-tts-registry-ok",
        "label": "Stub TTS OK", "api_key": "elevenlabs-key-0123456789abcdef",
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["api_key"] == "elevenlabs-k...def"


def test_create_llm_entry_persists_api_key_and_base_url(client, _with_password, monkeypatch):
    _signup_login(client, "root", role="admin")

    async def fake_reply(self, messages):
        return "ok"

    from app.services.conversation.responder import OpenAICompatResponder
    monkeypatch.setattr(OpenAICompatResponder, "reply", fake_reply)

    resp = client.post("/v1/model_registry", json={
        "kind": "llm", "engine": "openrouter", "model_id": "openrouter/some-model", "label": "Some Model",
        "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-v1-llmkeyabcdefghijklmno012",
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["base_url"] == "https://openrouter.ai/api/v1"
    assert data["api_key"] == "sk-or-v1-llm...012"


def test_patch_enabling_an_llm_entry_does_not_disable_the_others_end_to_end(client, _with_password, monkeypatch):
    """`enabled` is no longer exclusive for kind="llm" -- multiple llm entries
    may be enabled (selectable per-profile) at once. Confirm that holds
    through the actual HTTP route, not just the store directly."""
    _signup_login(client, "root", role="admin")

    async def fake_reply(self, messages):
        return "ok"

    from app.services.conversation.responder import OpenAICompatResponder
    monkeypatch.setattr(OpenAICompatResponder, "reply", fake_reply)

    first = client.post("/v1/model_registry", json={
        "kind": "llm", "engine": "openrouter", "model_id": "model-a", "label": "Model A",
        "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-v1-firstkeyabc0123456789",
    }).json()["data"]
    second = client.post("/v1/model_registry", json={
        "kind": "llm", "engine": "ollama", "model_id": "model-b", "label": "Model B",
        "base_url": "http://localhost:11434/v1", "api_key": "",
    }).json()["data"]
    assert first["enabled"] is True  # first row's default enabled=True still holds alone

    resp = client.patch(f"/v1/model_registry/{second['id']}", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is True

    entries = {e["id"]: e for e in client.get("/v1/model_registry").json()["data"]}
    assert entries[second["id"]]["enabled"] is True
    assert entries[first["id"]]["enabled"] is True


def test_patch_setting_is_default_disables_the_previous_default_end_to_end(client, _with_password, monkeypatch):
    """The admin System Config 'Default LLM' select PATCHes {is_default: true,
    enabled: true} on the chosen row -- confirm the store's
    single-is_default-llm-row enforcement holds through the actual HTTP
    route, not just the store directly, and that `enabled` stays true for
    both rows (is_default exclusivity must not clear the sibling's enabled)."""
    _signup_login(client, "root", role="admin")

    async def fake_reply(self, messages):
        return "ok"

    from app.services.conversation.responder import OpenAICompatResponder
    monkeypatch.setattr(OpenAICompatResponder, "reply", fake_reply)

    first = client.post("/v1/model_registry", json={
        "kind": "llm", "engine": "openrouter", "model_id": "model-a", "label": "Model A",
        "base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-v1-firstkeyabc0123456789",
        "is_default": True,
    }).json()["data"]
    second = client.post("/v1/model_registry", json={
        "kind": "llm", "engine": "ollama", "model_id": "model-b", "label": "Model B",
        "base_url": "http://localhost:11434/v1", "api_key": "",
    }).json()["data"]
    assert first["is_default"] is True
    assert second["is_default"] is False

    resp = client.patch(f"/v1/model_registry/{second['id']}", json={"is_default": True, "enabled": True})
    assert resp.status_code == 200
    assert resp.json()["data"]["is_default"] is True

    entries = {e["id"]: e for e in client.get("/v1/model_registry").json()["data"]}
    assert entries[second["id"]]["is_default"] is True
    assert entries[second["id"]]["enabled"] is True
    assert entries[first["id"]]["is_default"] is False
    assert entries[first["id"]]["enabled"] is True


def test_patch_blank_api_key_preserves_existing(client, _with_password):
    _signup_login(client, "root", role="admin")
    created = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1", "label": "Stub OK",
        "api_key": "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345",
    }).json()["data"]

    resp = client.patch(f"/v1/model_registry/{created['id']}", json={"api_key": ""})
    assert resp.status_code == 200
    assert resp.json()["data"]["api_key"] == "sk-or-v1-abc...345"  # unchanged, still the original key


def test_patch_non_blank_api_key_updates_it(client, _with_password):
    _signup_login(client, "root", role="admin")
    created = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1", "label": "Stub OK",
        "api_key": "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345",
    }).json()["data"]

    resp = client.patch(
        f"/v1/model_registry/{created['id']}", json={"api_key": "sk-or-v1-newkeyabcdefghijklmno999"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["api_key"] == "sk-or-v1-new...999"


def test_create_stt_entry_accepts_base_url_and_config(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1",
        "label": "Stub OK", "base_url": "https://api.example.com",
        "config": {"timeout_seconds": 45.0},
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["base_url"] == "https://api.example.com"
    assert resp.json()["data"]["config"] == {"timeout_seconds": 45.0}


@pytest.mark.asyncio
async def test_patch_entry_can_update_config(client, _with_password):
    await _signup_login_async(client, "root", role="admin")
    from app.services.model_registry.store import model_registry_store

    entry = await model_registry_store.create("tts", "omnivoice", "k2-fsa/OmniVoice", "OmniVoice")
    resp = client.patch(f"/v1/model_registry/{entry['id']}", json={"config": {"omnivoice_device": "mps"}})
    assert resp.status_code == 200
    assert resp.json()["data"]["config"] == {"omnivoice_device": "mps"}


@pytest.mark.asyncio
async def test_patch_qwen3_asr_config_clears_the_model_cache(client, _with_password):
    """Ported from test_system_config_routes.py's
    test_changing_qwen3_asr_device_clears_the_model_cache -- the reinit trigger
    moved from PUT /v1/system/config (which read SystemConfig.stt_local, now
    removed) to PATCH /v1/model_registry/{id} (Task 7)."""
    await _signup_login_async(client, "root", role="admin")
    from app.services.model_registry.store import model_registry_store
    from app.services.stt.providers import qwen3_asr_provider as mod

    entry = await model_registry_store.create("stt", "qwen3_asr", "", "Qwen3-ASR (device config)")
    mod._MODEL_CACHE["cuda:some-model"] = object()

    resp = client.patch(f"/v1/model_registry/{entry['id']}", json={"config": {"device": "cuda:1"}})
    assert resp.status_code == 200
    assert mod._MODEL_CACHE == {}


@pytest.mark.asyncio
async def test_patch_unrelated_qwen3_asr_field_does_not_clear_the_model_cache(client, _with_password):
    await _signup_login_async(client, "root", role="admin")
    from app.services.model_registry.store import model_registry_store
    from app.services.stt.providers import qwen3_asr_provider as mod

    entry = await model_registry_store.create("stt", "qwen3_asr", "", "Qwen3-ASR (device config)")
    sentinel = object()
    mod._MODEL_CACHE["cuda:some-model"] = sentinel

    resp = client.patch(f"/v1/model_registry/{entry['id']}", json={"stage": "testing"})
    assert resp.status_code == 200
    assert mod._MODEL_CACHE.get("cuda:some-model") is sentinel


@pytest.mark.asyncio
async def test_patch_whisper_service_entry_rebuilds_the_provider(client, _with_password):
    """Ported from test_system_config_routes.py's
    test_changing_remote_stt_base_url_rebuilds_the_provider -- moved to
    PATCH /v1/model_registry/{id} (Task 7 removed SystemConfig.remote_stt)."""
    await _signup_login_async(client, "root", role="admin")
    from app.services.model_registry.store import model_registry_store
    from app.services.stt.service import stt_service

    entry = await model_registry_store.create("stt", "whisper_service", "whisper-1", "Whisper Service")
    original = stt_service.providers["whisper_service"]

    resp = client.patch(f"/v1/model_registry/{entry['id']}", json={"base_url": "https://changed.example/v1"})
    assert resp.status_code == 200
    assert stt_service.providers["whisper_service"] is not original
    assert stt_service.providers["whisper_service"].base_url == "https://changed.example/v1"


@pytest.mark.asyncio
async def test_patch_omnivoice_entry_respawns_the_sidecar(client, _with_password, monkeypatch):
    """Ported from test_system_config_routes.py's
    test_changing_omnivoice_model_id_clears_voice_ref_and_respawns -- moved to
    PATCH /v1/model_registry/{id} (Task 7 removed SystemConfig.omnivoice).
    omnivoice_use_server defaults to True on a freshly-created entry (no
    `config` override), so -- unlike the old test -- no extra setup is needed
    to land in the respawn branch."""
    await _signup_login_async(client, "root", role="admin")
    from app.services.model_registry.store import model_registry_store
    from app.services.tts.providers import omnivoice_provider as ov_mod

    entry = await model_registry_store.create("tts", "omnivoice", "k2-fsa/OmniVoice", "OmniVoice")
    ov_mod._voice_ref.update({"path": "/tmp/old.wav", "text": "old"})
    spawn_calls = []
    monkeypatch.setattr(ov_mod.OmniVoiceProvider, "_spawn_sidecar", lambda self: spawn_calls.append(1))

    resp = client.patch(f"/v1/model_registry/{entry['id']}", json={"config": {"omnivoice_device": "mps"}})
    assert resp.status_code == 200
    assert ov_mod._voice_ref == {}
    assert len(spawn_calls) == 1


_SERVICE_BASE = "http://tts-service:8100/v1"


def test_tts_entry_keeps_its_base_url(client, monkeypatch):
    """Regression: create_entry whitelisted base_url to (llm, stt), so a TTS
    service entry lost its URL on save and openai_tts could never resolve it."""
    from app.services.tts.providers import openai_tts_provider

    async def fake_render(self, payload):
        # Must be a real WAV container, not just the "RIFFWAVEDATA" placeholder:
        # RenderingTTSProvider.synthesize() runs wav_duration_seconds() on this
        # return value, which raises wave.Error on anything that isn't a real
        # RIFF/WAVE file -- masking the base_url regression behind an unrelated
        # 400 ("not a WAVE file") instead of exercising the code path this test
        # is meant to pin.
        return pcm16_to_wav_bytes(b"\x00\x00" * 100, sample_rate=24000)

    monkeypatch.setattr(openai_tts_provider.OpenAICompatTTSProvider, "_render_wav", fake_render)
    _signup_login(client, "admin_base_url", role="admin")

    r = client.post(
        "/v1/model_registry",
        json={
            "kind": "tts", "engine": "openai_tts", "model_id": "vieneu",
            "label": "local box", "base_url": _SERVICE_BASE, "api_key": "t0ken",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["base_url"] == _SERVICE_BASE


def test_bad_service_url_is_rejected_at_add_time(client, monkeypatch):
    """The admin should learn the URL/token is wrong when they click Add, not on
    the first real transcription.

    Asserting only `"openai_stt" in detail` would also pass if the add-time
    test call never reached the network at all: the *singleton* openai_stt
    provider (no entry override) resolves its entry by looking up a registry
    row, finds none yet (this row hasn't been created), and raises "openai_stt
    is not configured" -- which also contains the substring "openai_stt", so a
    bare substring check can't tell a short-circuited lookup apart from an
    actual failed HTTP attempt against the submitted base_url. Pin the latter
    specifically: the provider must reach OpenAICompatSttProvider.transcribe_bytes
    and fail there with a network/request error, using the base_url from *this*
    payload, not report a missing config.

    The base_url is deliberately bogus (".invalid" TLD, per RFC 2606), but the
    test must stay hermetic -- no real DNS/socket call. httpx.AsyncClient is
    swapped for a MockTransport whose handler raises httpx.ConnectError, same
    pattern as test_openai_stt_provider.py's `captured` fixture, so the
    provider's `except httpx.HTTPError` branch fires exactly like it would
    against an unreachable host, without ever touching the network."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known", request=request)

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    _signup_login(client, "admin_bad_url", role="admin")
    r = client.post(
        "/v1/model_registry",
        json={
            "kind": "stt", "engine": "openai_stt", "model_id": "whisper-medium",
            "label": "typo", "base_url": "http://nonexistent.invalid:9/v1", "api_key": "t0ken",
        },
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "openai_stt" in detail
    # Proves an HTTP attempt was actually made against the submitted URL, not a
    # short-circuited "not configured" from a provider that never saw the payload.
    assert "not configured" not in detail
    assert "request failed" in detail


def test_location_classification():
    """Three-state locality: 'service' for engines that call a configurable
    HTTP endpoint and need a base_url (openai_stt/openai_tts, whisper_service/
    eventlab, and every kind='llm' entry); 'remote' for the OpenRouter-backed
    STT engines that hit a fixed API with api_key only (qwen3_asr_or/whisper_or)
    -- remote, so NOT 'local', but no base_url either; 'local' for engines that
    run in-process (whisper, vosk, qwen3_asr, omnivoice, vieneu, edge_tts,
    qwen3_tts_*, voxcpm2, ...)."""
    from app.api.routes.model_registry import _location

    assert _location("llm", "anything") == "service"
    assert _location("stt", "openai_stt") == "service"
    assert _location("tts", "openai_tts") == "service"
    assert _location("stt", "whisper_service") == "service"
    assert _location("stt", "eventlab") == "service"
    assert _location("stt", "qwen3_asr_or") == "remote"
    assert _location("stt", "whisper_or") == "remote"
    assert _location("stt", "whisper") == "local"
    assert _location("stt", "vosk") == "local"
    assert _location("tts", "vieneu") == "local"
    assert _location("tts", "omnivoice") == "local"


def test_requires_base_url_classification():
    """base_url is required exactly for the 'service' location -- the engines
    that talk to a configurable HTTP endpoint. OpenRouter ('remote') and
    in-process ('local') engines both return False (neither reads base_url)."""
    from app.api.routes.model_registry import _requires_base_url

    assert _requires_base_url("llm", "anything") is True
    assert _requires_base_url("stt", "openai_stt") is True
    assert _requires_base_url("tts", "openai_tts") is True
    assert _requires_base_url("stt", "whisper_service") is True
    assert _requires_base_url("stt", "eventlab") is True
    assert _requires_base_url("stt", "whisper") is False
    assert _requires_base_url("stt", "qwen3_asr_or") is False
    assert _requires_base_url("stt", "whisper_or") is False
    assert _requires_base_url("tts", "vieneu") is False
    assert _requires_base_url("tts", "omnivoice") is False


def test_list_entries_surfaces_requires_base_url(client, _with_password):
    _signup_login(client, "root", role="admin")
    client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1", "label": "Stub OK",
    })

    listed = client.get("/v1/model_registry").json()["data"]
    entry = next(e for e in listed if e["engine"] == "stub-registry-ok")
    assert entry["requires_base_url"] is False
    assert entry["location"] == "local"


# ---------- Feature: block enabling an entry whose artifact isn't installed ----------


@pytest.mark.asyncio
async def test_patch_enabling_uncached_whisper_entry_is_rejected(client, _with_password, monkeypatch):
    await _signup_login_async(client, "root", role="admin")
    from app.services.model_registry.store import model_registry_store
    from app.services.whisper_models import whisper_manager

    monkeypatch.setattr(
        whisper_manager, "snapshot",
        lambda: {"models": [{"size": "medium", "label": "Medium", "cached": False}]},
    )
    entry = await model_registry_store.create("stt", "whisper", "medium", "Whisper Medium", enabled=False)

    resp = client.patch(f"/v1/model_registry/{entry['id']}", json={"enabled": True})
    assert resp.status_code == 400
    assert "not installed" in resp.json()["detail"]

    # rejected -- must not have flipped enabled in the store
    fresh = await model_registry_store.get(entry["id"])
    assert fresh["enabled"] is False


@pytest.mark.asyncio
async def test_patch_enabling_cached_whisper_entry_succeeds(client, _with_password, monkeypatch):
    await _signup_login_async(client, "root", role="admin")
    from app.services.model_registry.store import model_registry_store
    from app.services.whisper_models import whisper_manager

    monkeypatch.setattr(
        whisper_manager, "snapshot",
        lambda: {"models": [{"size": "medium", "label": "Medium", "cached": True}]},
    )
    entry = await model_registry_store.create("stt", "whisper", "medium", "Whisper Medium", enabled=False)

    resp = client.patch(f"/v1/model_registry/{entry['id']}", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is True


@pytest.mark.asyncio
async def test_patch_enabling_missing_entry_still_404s(client, _with_password):
    """The artifact-installed guard must not swallow the pre-existing 404 case
    (fetching `existing` for the guard finds nothing -- fall through to the
    normal set_fields()-returns-None 404, don't raise a second time)."""
    await _signup_login_async(client, "root", role="admin")
    resp = client.patch("/v1/model_registry/does-not-exist", json={"enabled": True})
    assert resp.status_code == 404


def test_create_vosk_entry_for_uninstalled_model_is_rejected_without_provider_call(
    client, _with_password, monkeypatch
):
    from app.services.models import model_manager
    from app.services.stt.providers.vosk_provider import VoskProvider

    monkeypatch.setattr(model_manager, "snapshot", lambda: {"installed": []})

    calls = []

    async def spy_transcribe(self, audio_bytes, language=None, model=None):
        calls.append(1)
        from app.schemas.stt import STTResult
        return STTResult(engine=self.name, text="ok", is_final=True)

    monkeypatch.setattr(VoskProvider, "transcribe_bytes", spy_transcribe)

    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "vosk", "model_id": "vosk-model-vn-0.4", "label": "Vosk VN",
    })
    assert resp.status_code == 400
    assert "not installed" in resp.json()["detail"]
    assert calls == []  # rejected before the provider test-call ever ran

    listed = client.get("/v1/model_registry").json()["data"]
    assert not any(e["engine"] == "vosk" for e in listed)


def test_create_vosk_entry_for_installed_model_succeeds(client, _with_password, monkeypatch):
    from app.services.models import model_manager
    from app.services.stt.providers.vosk_provider import VoskProvider

    monkeypatch.setattr(
        model_manager, "snapshot",
        lambda: {"installed": [{"name": "vosk-model-vn-0.4", "active": True}]},
    )

    async def fake_transcribe(self, audio_bytes, language=None, model=None):
        from app.schemas.stt import STTResult
        return STTResult(engine=self.name, text="ok", is_final=True)

    monkeypatch.setattr(VoskProvider, "transcribe_bytes", fake_transcribe)

    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "vosk", "model_id": "vosk-model-vn-0.4", "label": "Vosk VN",
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is True


def test_create_and_patch_for_non_special_cased_engine_is_unaffected_by_the_guard(
    client, _with_password
):
    """edge_tts has no per-model artifact concept -- is_artifact_installed()
    returns None for it, so the enable-guard must be a no-op."""
    _signup_login(client, "root", role="admin")
    resp = client.post("/v1/model_registry", json={
        "kind": "tts", "engine": "edge_tts", "model_id": "vi-VN-NamMinhNeural", "label": "Edge TTS",
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is True

    entry_id = resp.json()["data"]["id"]
    resp2 = client.patch(f"/v1/model_registry/{entry_id}", json={"enabled": True})
    assert resp2.status_code == 200
    assert resp2.json()["data"]["enabled"] is True


@pytest.mark.asyncio
async def test_list_entries_surfaces_artifact_installed(client, _with_password, monkeypatch):
    await _signup_login_async(client, "root", role="admin")
    from app.services.model_registry.store import model_registry_store
    from app.services.whisper_models import whisper_manager

    monkeypatch.setattr(
        whisper_manager, "snapshot",
        lambda: {"models": [{"size": "medium", "label": "Medium", "cached": True}]},
    )
    cached = await model_registry_store.create("stt", "whisper", "medium", "Whisper Medium (cached)")
    uncached = await model_registry_store.create("stt", "whisper", "large-v3", "Whisper Large (uncached)")
    not_applicable = await model_registry_store.create("stt", "stub-registry-ok", "v1", "Stub OK")

    listed = client.get("/v1/model_registry").json()["data"]
    by_id = {e["id"]: e for e in listed}
    assert by_id[cached["id"]]["artifact_installed"] is True
    assert by_id[uncached["id"]]["artifact_installed"] is False
    assert by_id[not_applicable["id"]]["artifact_installed"] is None


# ---------- Feature: hard DELETE ----------


def test_admin_can_delete_a_disabled_entry(client, _with_password):
    _signup_login(client, "root", role="admin")
    created = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1", "label": "Stub OK",
    }).json()["data"]
    client.patch(f"/v1/model_registry/{created['id']}", json={"enabled": False})

    resp = client.delete(f"/v1/model_registry/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True

    listed = client.get("/v1/model_registry").json()["data"]
    assert not any(e["id"] == created["id"] for e in listed)


def test_delete_enabled_entry_is_rejected(client, _with_password):
    """Deleting is a destructive, unrecoverable action -- requiring
    disable-first is a safety rail against hard-deleting something still
    actively in use, and matches how the admin UI's Delete button gates on
    the entry already being disabled."""
    _signup_login(client, "root", role="admin")
    created = client.post("/v1/model_registry", json={
        "kind": "stt", "engine": "stub-registry-ok", "model_id": "v1", "label": "Stub OK",
    }).json()["data"]
    assert created["enabled"] is True

    resp = client.delete(f"/v1/model_registry/{created['id']}")
    assert resp.status_code == 400
    assert "disable" in resp.json()["detail"].lower()

    listed = client.get("/v1/model_registry").json()["data"]
    assert any(e["id"] == created["id"] for e in listed)


def test_delete_nonexistent_entry_is_404(client, _with_password):
    _signup_login(client, "root", role="admin")
    resp = client.delete("/v1/model_registry/does-not-exist")
    assert resp.status_code == 404


def test_regular_user_cannot_delete_model_registry_entry(client, _with_password):
    """Mirrors test_regular_user_cannot_reach_model_registry -- DELETE should be
    covered by the same admin-only prefix gate as every other /v1/model_registry
    route, proven rather than assumed."""
    _signup_login(client, "toan", role="user")
    resp = client.delete("/v1/model_registry/some-id")
    assert resp.status_code == 403
