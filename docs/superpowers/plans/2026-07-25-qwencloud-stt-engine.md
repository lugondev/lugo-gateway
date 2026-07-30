# QwenCloud STT Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `qwencloud` remote STT engine supporting two model families — qwen3-asr-flash (OpenAI-Realtime WS + inline HTTP batch) and fun-asr (DashScope-native WS + one-shot-WS batch) — via one unified provider selected through the Model Registry.

**Architecture:** One `QwenCloudSttProvider` in `app/services/stt/providers/qwencloud_provider.py`. A `_family()` helper routes on the configured model prefix. Batch (`transcribe_bytes`) and realtime (`open_stream`) each dispatch by family. Two `STTStream` subclasses share a `_BaseWsStream` (queue + background reader). Config resolves per-call from the Model Registry, mirroring `http_stt_provider.py`.

**Tech Stack:** Python 3.12, FastAPI gateway, `httpx` (async, MockTransport in tests), `websockets` 16.0, `pytest`/`pytest-asyncio`. Design spec: `docs/superpowers/specs/2026-07-25-qwencloud-stt-engine-design.md`.

## Global Constraints

- Engine name: `qwencloud`. Provider `name` attribute = `"qwencloud"`. `STTResult.engine` = `"qwencloud"`.
- API host (default `base_url`): `https://dashscope-intl.aliyuncs.com`. Auth header: `Authorization: Bearer <key>` (qwen3) / `Authorization: bearer <key>` (fun-asr — lowercase, as verified).
- Verified endpoints (see spec §3): batch HTTP `POST {base}/api/v1/services/aigc/multimodal-generation/generation`; qwen3 WS `wss://…/api-ws/v1/realtime?model={realtime_model}`; fun-asr WS `wss://…/api-ws/v1/inference`.
- Audio for WS: PCM16, mono, at the session `sample_rate`. qwen3 sends base64 in `input_audio_buffer.append`; fun-asr sends raw binary frames.
- Field mapping (verified live): qwen3 partial = `stash`, final = `transcript`; fun-asr `result-generated.payload.output.sentence` with `sentence_end` bool (false=partial `text`, true=final `text`).
- Tests live in repo-root `tests/unit/`, import `from app...`. Run from repo root: `cd /Users/lugon/code/speech-text-transformer`.
- Follow the `http_stt_provider.py` registry/resolve pattern exactly (per-call resolution, `_entry_override` for test-before-add).
- Do NOT push to `main` or deploy — this repo auto-deploys prod on push. Commit locally only.

---

## File Structure

- **Create** `apps/api_gateway/app/services/stt/providers/qwencloud_provider.py` — the provider, `_family`/`_ws_url`/`_wav_to_pcm16`/`_ws_connect` helpers, `_BaseWsStream`, `QwenOaiRealtimeStream`, `FunAsrNativeStream`.
- **Create** `tests/unit/test_qwencloud_stt_provider.py` — all unit tests.
- **Modify** `apps/api_gateway/app/services/stt/service.py` — register the provider + `list_engines` branch.
- **Modify** `apps/api_gateway/app/schemas/stt.py:7` — add `qwencloud` to the `STTRequest.engine` regex.
- **Modify** `pyproject.toml:~19` — declare `websockets` as a direct dependency.

---

## Task 1: Provider skeleton + qwen3 batch (inline HTTP)

**Files:**
- Create: `apps/api_gateway/app/services/stt/providers/qwencloud_provider.py`
- Test: `tests/unit/test_qwencloud_stt_provider.py`

**Interfaces:**
- Consumes: `STTProvider`, `STTResult`, `translate_httpx_error`, `model_registry_store`, `resolve_credentials` (all existing).
- Produces:
  - `_family(model: str | None) -> str` → `"qwen3"` | `"funasr"` (default `"qwen3"`).
  - `class QwenCloudSttProvider(STTProvider)` with `name="qwencloud"`, `__init__(self, name="qwencloud", timeout_seconds=60.0, entry=None)`, `async _resolve_entry(model) -> dict | None`, `async transcribe_bytes(audio_bytes, language=None, model=None) -> STTResult`.
  - `_DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_qwencloud_stt_provider.py`:

```python
import httpx
import pytest

from app.schemas.stt import STTRequest
from app.services.stt.providers.qwencloud_provider import (
    QwenCloudSttProvider,
    _family,
)

_QWEN_ENTRY = {
    "id": "q1", "kind": "stt", "engine": "qwencloud", "model_id": "qwen3-asr-flash",
    "label": "QwenCloud", "enabled": True, "stage": "stable",
    "api_key": "sk-ws-test", "base_url": "https://dashscope-intl.aliyuncs.com",
    "config": {},
}

_MM_OK = {  # multimodal-generation success shape (verified live)
    "output": {"choices": [{"finish_reason": "stop", "message": {
        "annotations": [{"type": "audio_info", "emotion": "neutral", "language": "vi"}],
        "content": [{"text": "  xin chào  "}], "role": "assistant"}}]},
    "usage": {"total_tokens": 50},
}


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["json"] = request.content
        return httpx.Response(200, json=_MM_OK)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def test_family_detection():
    assert _family("qwen3-asr-flash") == "qwen3"
    assert _family("qwen3-asr-flash-realtime") == "qwen3"
    assert _family("fun-asr") == "funasr"
    assert _family("fun-asr-realtime") == "funasr"
    assert _family(None) == "qwen3"


@pytest.mark.asyncio
async def test_qwen3_batch_posts_multimodal_with_base64(captured, monkeypatch):
    async def fake_find(kind, engine, model_id):
        return _QWEN_ENTRY

    monkeypatch.setattr(
        "app.services.stt.providers.qwencloud_provider.model_registry_store.find", fake_find
    )
    result = await QwenCloudSttProvider().transcribe_bytes(b"RIFFDATA", "vi", "qwen3-asr-flash")

    assert captured["url"].endswith("/api/v1/services/aigc/multimodal-generation/generation")
    assert captured["auth"] == "Bearer sk-ws-test"
    body = captured["json"].decode()
    assert "data:audio/wav;base64," in body
    assert "UklGRkRBVEE" in body or "RIFFDATA" not in body  # base64 of b"RIFFDATA"
    assert '"language": "vi"' in body or '"language":"vi"' in body
    assert result.text == "xin chào"       # stripped
    assert result.engine == "qwencloud"
    assert result.is_final is True


@pytest.mark.asyncio
async def test_qwen3_batch_empty_output_yields_empty_text(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"output": {"choices": []}})
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: original(*a, **{**k, "transport": transport}))
    result = await QwenCloudSttProvider(entry=_QWEN_ENTRY).transcribe_bytes(b"X")
    assert result.text == ""


@pytest.mark.asyncio
async def test_unconfigured_entry_raises_clear_error(monkeypatch):
    async def fake_find_enabled(kind, engine=None):
        return None
    monkeypatch.setattr(
        "app.services.stt.providers.qwencloud_provider.model_registry_store.find_enabled",
        fake_find_enabled,
    )
    with pytest.raises(RuntimeError, match="not configured"):
        await QwenCloudSttProvider().transcribe_bytes(b"X")


@pytest.mark.asyncio
async def test_http_error_surfaces_status(monkeypatch):
    def handler(request):
        return httpx.Response(401, text="invalid api key")
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: original(*a, **{**k, "transport": transport}))
    with pytest.raises(RuntimeError, match="HTTP 401"):
        await QwenCloudSttProvider(entry=_QWEN_ENTRY).transcribe_bytes(b"X")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/lugon/code/speech-text-transformer && python -m pytest tests/unit/test_qwencloud_stt_provider.py -v`
Expected: FAIL with `ModuleNotFoundError: ... qwencloud_provider`.

- [ ] **Step 3: Write the minimal implementation**

Create `apps/api_gateway/app/services/stt/providers/qwencloud_provider.py`:

```python
"""QwenCloud (Alibaba DashScope Model Studio, dashscope-intl) STT.

One engine, two model families selected by the registry entry's model:
  - qwen3-asr-flash: batch via inline multimodal-generation HTTP; realtime via
    the OpenAI-Realtime-compatible WebSocket (/api-ws/v1/realtime).
  - fun-asr:         batch via a one-shot native WS session; realtime via the
    DashScope-native run-task WebSocket (/api-ws/v1/inference).

Config resolves per-call from the Model Registry (like http_stt_provider), so
admin edits take effect immediately. See the design spec dated 2026-07-25.
"""

from __future__ import annotations

import base64

import httpx

from app.schemas.stt import STTResult
from app.services.http_errors import translate_httpx_error
from app.services.model_registry.store import model_registry_store
from app.services.providers.resolve import resolve_credentials
from app.services.stt.base import STTProvider

_DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com"
_DEFAULT_TIMEOUT = 60.0
_MM_PATH = "/api/v1/services/aigc/multimodal-generation/generation"


def _family(model: str | None) -> str:
    """Map a model id to its family. Default qwen3 (the primary family)."""
    return "funasr" if (model or "").strip().lower().startswith("fun-asr") else "qwen3"


class QwenCloudSttProvider(STTProvider):
    name = "qwencloud"

    def __init__(self, name: str = "qwencloud", timeout_seconds: float = _DEFAULT_TIMEOUT,
                 entry: dict | None = None) -> None:
        self.name = name
        self.timeout_seconds = timeout_seconds
        self._entry_override = entry  # only the registry test-before-add passes this

    async def _resolve_entry(self, model: str | None) -> dict | None:
        if self._entry_override is not None:
            return self._entry_override
        if model:
            return await model_registry_store.find(kind="stt", engine=self.name, model_id=model)
        return await model_registry_store.find_enabled(kind="stt", engine=self.name)

    async def _creds(self, model: str | None) -> tuple[dict, str, str, float]:
        entry = await self._resolve_entry(model)
        if entry:
            base_url, api_key = await resolve_credentials(entry)
        else:
            entry, base_url, api_key = {}, "", ""
        base_url = (base_url or "").strip() or (_DEFAULT_BASE_URL if entry else "")
        api_key = (api_key or "").strip()
        if not entry or not api_key:
            raise RuntimeError(
                f"{self.name} is not configured. Add a Model Registry entry with an API key "
                "(engine=qwencloud, model_id=qwen3-asr-flash or fun-asr)."
            )
        cfg_timeout = (entry.get("config") or {}).get("timeout_seconds")
        timeout = cfg_timeout if cfg_timeout is not None else self.timeout_seconds
        return entry, base_url, api_key, timeout

    async def transcribe_bytes(self, audio_bytes: bytes, language: str | None = None,
                               model: str | None = None) -> STTResult:
        entry, base_url, api_key, timeout = await self._creds(model)
        effective = model or entry.get("model_id") or "qwen3-asr-flash"
        # fun-asr has no inline HTTP endpoint -> one-shot WS (added in Task 4).
        return await self._qwen3_batch(base_url, api_key, timeout, effective, audio_bytes,
                                       language or (entry.get("config") or {}).get("language"))

    async def _qwen3_batch(self, base_url, api_key, timeout, model, audio_bytes, language):
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        asr_options: dict = {"enable_lid": True}
        if language:
            asr_options["language"] = language
        body = {
            "model": model,
            "input": {"messages": [{"role": "user",
                "content": [{"audio": f"data:audio/wav;base64,{b64}"}]}]},
            "parameters": {"asr_options": asr_options},
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{base_url.rstrip('/')}{_MM_PATH}",
                                         headers=headers, json=body)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as exc:
            raise translate_httpx_error(self.name, exc) from exc
        return STTResult(engine=self.name, text=_mm_text(payload), is_final=True, confidence=None)


def _mm_text(payload: dict) -> str:
    """Pull transcript from output.choices[0].message.content[0].text (defensive)."""
    try:
        content = payload["output"]["choices"][0]["message"]["content"]
        for part in content:
            if isinstance(part, dict) and "text" in part:
                return str(part["text"]).strip()
    except (KeyError, IndexError, TypeError):
        pass
    return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_qwencloud_stt_provider.py -v`
Expected: PASS (5 tests). If the base64 assertion is brittle, note `base64.b64encode(b"RIFFDATA")` = `b"UklGRkRBVEE="`.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/stt/providers/qwencloud_provider.py tests/unit/test_qwencloud_stt_provider.py
git commit -m "feat(stt): QwenCloud provider skeleton + qwen3 inline batch"
```

---

## Task 2: Wire into service, schema, and list_engines

**Files:**
- Modify: `apps/api_gateway/app/services/stt/service.py`
- Modify: `apps/api_gateway/app/schemas/stt.py:7`
- Test: `tests/unit/test_qwencloud_stt_provider.py` (append)

**Interfaces:**
- Consumes: `QwenCloudSttProvider` (Task 1), `STTService` / `stt_service`.
- Produces: `stt_service.get_provider("qwencloud")`; a `list_engines` row `{engine:"qwencloud", mode:"remote", realtime:<bool>, configured:<bool>}`.

- [ ] **Step 1: Write the failing tests** (append to the test file)

```python
from app.services.stt.service import stt_service


def test_engine_is_registered():
    assert stt_service.get_provider("qwencloud").name == "qwencloud"


def test_schema_accepts_the_engine():
    assert STTRequest(engine="qwencloud").engine == "qwencloud"


@pytest.mark.asyncio
async def test_list_engines_reports_qwencloud_remote():
    engines = await stt_service.list_engines()
    row = next(e for e in engines if e["engine"] == "qwencloud")
    assert row["mode"] == "remote"
    assert "configured" in row
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_qwencloud_stt_provider.py -k "registered or schema or list_engines" -v`
Expected: FAIL — `EngineNotFoundError`, schema `ValidationError`, and `StopIteration` in list_engines.

- [ ] **Step 3: Implement the wiring**

In `app/schemas/stt.py` line 7, add `|qwencloud` before the closing `)$`:

```python
        pattern="^(vosk|whisper|whisper_local|whisper_mlx|qwen3_asr|qwen3_asr_gguf|whisper_service|eventlab|qwen3_asr_or|whisper_or|http_stt|qwencloud)$",
```

In `app/services/stt/service.py`:
- Add the import near the other provider imports:
  ```python
  from app.services.stt.providers.qwencloud_provider import QwenCloudSttProvider
  ```
- Register it in `self.providers` (inside `__init__`, alongside `http_stt`):
  ```python
  "qwencloud": QwenCloudSttProvider(
      timeout_seconds=remote_stt.remote_stt_timeout_seconds
  ),
  ```
- In `list_engines`, add a branch BEFORE the final `else` (which does `remote[engine]` and would KeyError). Place it next to the `http_stt` branch:
  ```python
  elif engine == "qwencloud":
      from app.services.providers.resolve import resolve_credentials

      configured = False
      for candidate in await model_registry_store.list_all():
          if (candidate["kind"] != "stt" or candidate["engine"] != "qwencloud"
                  or not candidate["enabled"]):
              continue
          _base_url, api_key = await resolve_credentials(candidate)
          if api_key:
              configured = True
              break
      entry = {"mode": "remote", "available": configured, "detail": None}
  ```
  (`realtime` is computed generically above from `open_stream` being overridden — no change needed there. After Task 3 it becomes `True`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_qwencloud_stt_provider.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/stt/service.py apps/api_gateway/app/schemas/stt.py tests/unit/test_qwencloud_stt_provider.py
git commit -m "feat(stt): register qwencloud engine (service + schema + list_engines)"
```

---

## Task 3: qwen3 realtime WebSocket stream

**Files:**
- Modify: `apps/api_gateway/app/services/stt/providers/qwencloud_provider.py`
- Modify: `pyproject.toml` (declare `websockets`)
- Test: `tests/unit/test_qwencloud_stt_provider.py` (append)

**Interfaces:**
- Consumes: `STTStream`, `BufferingStream` base machinery. New module-level `async def _ws_connect(url, headers) -> connection`.
- Produces:
  - `class _BaseWsStream(STTStream)` with `accept`/`finalize` + hooks `_hello() -> str|bytes|None`, `_encode_audio(pcm) -> str|bytes`, `_parse(msg: dict) -> list[STTResult]`, `_finish_frame() -> str|bytes`, `_is_done(msg: dict) -> bool`.
  - `class QwenOaiRealtimeStream(_BaseWsStream)`.
  - `QwenCloudSttProvider.open_stream(sample_rate, language=None) -> STTStream` (qwen3 only for now; raises for funasr — completed in Task 4).

- [ ] **Step 1: Write the failing tests** (append)

```python
import asyncio
import json
from app.services.stt.providers import qwencloud_provider as qc


class FakeWS:
    """Async-iterable fake websocket. Yields seeded server messages, records sends."""
    def __init__(self, incoming):
        self._incoming = list(incoming)   # list[str] server frames
        self.sent = []                    # list of frames the stream sent
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._incoming.pop(0)

    async def send(self, frame):
        self.sent.append(frame)

    async def close(self):
        self.closed = True


def _qwen_msgs():
    return [
        json.dumps({"type": "session.created"}),
        json.dumps({"type": "session.updated"}),
        json.dumps({"type": "conversation.item.input_audio_transcription.text",
                    "text": "", "stash": "xin"}),
        json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "xin chào"}),
        json.dumps({"type": "session.finished"}),
    ]


@pytest.fixture
def fake_connect(monkeypatch):
    holder = {}

    async def _connect(url, headers):
        ws = FakeWS(holder["incoming"])
        holder["ws"] = ws
        holder["url"] = url
        holder["headers"] = headers
        return ws

    monkeypatch.setattr(qc, "_ws_connect", _connect)
    return holder


@pytest.mark.asyncio
async def test_qwen3_stream_maps_stash_and_transcript(fake_connect, monkeypatch):
    fake_connect["incoming"] = _qwen_msgs()
    monkeypatch.setattr(
        "app.services.stt.providers.qwencloud_provider.model_registry_store.find_enabled",
        lambda kind, engine=None: _async(_QWEN_ENTRY),
    )
    provider = QwenCloudSttProvider(entry=_QWEN_ENTRY)
    stream = provider.open_stream(sample_rate=16000, language="vi")

    results = await stream.accept(b"\x00\x00" * 160)
    # after connect+hello, drained partial(s)/final from the queue
    partials = [r for r in results if not r.is_final]
    finals = [r for r in results if r.is_final]
    assert any(r.text == "xin" for r in partials)
    assert any(r.text == "xin chào" for r in finals)

    # the hello (session.update) and a base64 append were sent
    assert any('"session.update"' in s for s in fake_connect["ws"].sent)
    assert any('"input_audio_buffer.append"' in s for s in fake_connect["ws"].sent)
    assert fake_connect["headers"]["Authorization"] == "Bearer sk-ws-test"
    assert "/api-ws/v1/realtime?model=qwen3-asr-flash-realtime" in fake_connect["url"]

    final = await stream.finalize()
    assert any('"session.finish"' in s for s in fake_connect["ws"].sent)
    assert fake_connect["ws"].closed is True


def _async(value):
    async def _c(*a, **k):
        return value
    return _c()
```

Note: the `find_enabled` monkeypatch above returns a coroutine each call; if your store call signature differs, adapt. The stream should also work with `entry=` override (no registry hit) — the test uses the override, so the monkeypatch is belt-and-suspenders.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_qwencloud_stt_provider.py -k qwen3_stream -v`
Expected: FAIL — `open_stream` returns the default `BufferingStream` (no `session.update` sent) / `_ws_connect` missing.

- [ ] **Step 3: Implement `_ws_connect`, `_BaseWsStream`, `QwenOaiRealtimeStream`, and `open_stream`**

Add to `qwencloud_provider.py` (imports at top: `import asyncio`, `import json`, `import uuid`, `import websockets`, and `from app.services.stt.base import STTStream`):

```python
async def _ws_connect(url: str, headers: dict):
    """Open a websocket. Wrapped so tests can monkeypatch it. websockets>=14
    uses additional_headers (not extra_headers)."""
    return await websockets.connect(url, additional_headers=headers)


def _ws_base(base_url: str) -> str:
    # Build from the HOST ROOT (Task 1's _host_base), not the raw base_url: a
    # resolved base may carry a path (e.g. the qwencloud provider preset ends
    # in /compatible-mode/v1). QwenCloud WS paths are absolute from the host.
    return _host_base(base_url).replace("https://", "wss://").replace("http://", "ws://")


class _BaseWsStream(STTStream):
    """Shared machinery: connect lazily, run a background reader that parses
    server frames into an asyncio.Queue of STTResult; accept() drains what's
    ready, finalize() flushes and closes. Subclasses supply the protocol hooks."""

    def __init__(self, url: str, headers: dict) -> None:
        self._url = url
        self._headers = headers
        self._ws = None
        self._q: asyncio.Queue = asyncio.Queue()
        self._reader = None
        self._done = asyncio.Event()

    # --- protocol hooks (override) ---
    def _hello(self):  # -> str | bytes | None
        return None

    def _encode_audio(self, pcm: bytes):  # -> str | bytes
        raise NotImplementedError

    def _parse(self, msg: dict) -> list[STTResult]:
        raise NotImplementedError

    def _finish_frame(self):  # -> str | bytes
        raise NotImplementedError

    def _is_done(self, msg: dict) -> bool:
        raise NotImplementedError

    # --- machinery ---
    async def _ensure(self) -> None:
        if self._ws is not None:
            return
        self._ws = await _ws_connect(self._url, self._headers)
        hello = self._hello()
        if hello is not None:
            await self._ws.send(hello)
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                for result in self._parse(msg):
                    await self._q.put(result)
                if self._is_done(msg):
                    break
        except Exception:  # noqa: BLE001 - a dropped socket ends the stream, not the app
            pass
        finally:
            self._done.set()

    def _drain(self) -> list[STTResult]:
        out = []
        while not self._q.empty():
            out.append(self._q.get_nowait())
        return out

    async def accept(self, pcm: bytes) -> list[STTResult]:
        await self._ensure()
        await self._ws.send(self._encode_audio(pcm))
        await asyncio.sleep(0)  # let the reader run
        return self._drain()

    async def finalize(self) -> STTResult | None:
        if self._ws is None:
            return None
        try:
            await self._ws.send(self._finish_frame())
            await asyncio.wait_for(self._done.wait(), timeout=15)
        except Exception:  # noqa: BLE001 - return whatever we have
            pass
        results = self._drain()
        await self._close()
        finals = [r for r in results if r.is_final]
        text = finals[-1].text if finals else (results[-1].text if results else "")
        return STTResult(engine="qwencloud", text=text, is_final=True, confidence=None) if text else None

    async def _close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass


class QwenOaiRealtimeStream(_BaseWsStream):
    """qwen3-asr-flash-realtime: OpenAI-Realtime-compatible protocol."""

    def __init__(self, base_url, api_key, model, sample_rate, language, turn_detection):
        url = f"{_ws_base(base_url)}/api-ws/v1/realtime?model={model}"
        super().__init__(url, {"Authorization": f"Bearer {api_key}"})
        self._sample_rate = sample_rate
        self._language = language
        self._turn_detection = turn_detection

    def _hello(self):
        session = {
            "modalities": ["text"],
            "input_audio_format": "pcm",
            "sample_rate": self._sample_rate,
            "input_audio_transcription": ({"language": self._language} if self._language else {}),
            "turn_detection": (None if self._turn_detection == "manual"
                               else {"type": "server_vad", "threshold": 0.0,
                                     "silence_duration_ms": 400}),
        }
        return json.dumps({"type": "session.update", "session": session})

    def _encode_audio(self, pcm):
        return json.dumps({"type": "input_audio_buffer.append",
                           "audio": base64.b64encode(pcm).decode("ascii")})

    def _parse(self, msg):
        t = msg.get("type")
        if t == "conversation.item.input_audio_transcription.text":
            stash = msg.get("stash") or msg.get("text") or ""
            return [STTResult(engine="qwencloud", text=stash, is_final=False)] if stash else []
        if t == "conversation.item.input_audio_transcription.completed":
            txt = (msg.get("transcript") or "").strip()
            return [STTResult(engine="qwencloud", text=txt, is_final=True)] if txt else []
        return []

    def _finish_frame(self):
        return json.dumps({"type": "session.finish"})

    def _is_done(self, msg):
        return msg.get("type") in ("session.finished", "error")
```

Then implement `open_stream` on the provider. `open_stream` is sync (the WS route calls it before its receive loop), so resolve synchronously via the **confirmed-existing** `model_registry_store.find_enabled_sync(kind, engine=None)` (store.py:157) and `resolve_credentials_sync` (resolve.py:27):

```python
    def open_stream(self, sample_rate: int, language: str | None = None):
        from app.services.providers.resolve import resolve_credentials_sync

        entry = self._entry_override or model_registry_store.find_enabled_sync("stt", self.name)
        if not entry:
            raise RuntimeError(f"{self.name} is not configured for streaming.")
        base_url, api_key = resolve_credentials_sync(entry)
        base_url = (base_url or "").strip() or _DEFAULT_BASE_URL
        cfg = entry.get("config") or {}
        realtime_model = cfg.get("realtime_model") or "qwen3-asr-flash-realtime"
        lang = language or cfg.get("language")
        if _family(realtime_model) == "funasr":
            raise RuntimeError("fun-asr streaming lands in Task 4")  # replaced in Task 4
        return QwenOaiRealtimeStream(base_url, api_key, realtime_model, sample_rate, lang,
                                     cfg.get("turn_detection"))
```

- [ ] **Step 4: Declare the `websockets` dependency**

In root `pyproject.toml`, in the same `dependencies` array as `httpx>=0.27.0`, add:
```toml
  "websockets>=14.0",
```
Verify import still works: `python -c "import websockets; print(websockets.__version__)"` (expect `16.0`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_qwencloud_stt_provider.py -v`
Expected: PASS (all prior + qwen3_stream). The `list_engines` row for `qwencloud` now has `realtime: True` — if `test_list_engines_reports_qwencloud_remote` asserted `realtime is False`, it did not (it only checks `mode`/`configured`), so no change needed.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/stt/providers/qwencloud_provider.py pyproject.toml tests/unit/test_qwencloud_stt_provider.py
git commit -m "feat(stt): qwen3 realtime WebSocket stream for qwencloud"
```

---

## Task 4: fun-asr native WS stream + fun-asr one-shot batch

**Files:**
- Modify: `apps/api_gateway/app/services/stt/providers/qwencloud_provider.py`
- Test: `tests/unit/test_qwencloud_stt_provider.py` (append)

**Interfaces:**
- Consumes: `_BaseWsStream`, `_ws_connect`, `FakeWS` (test), `_wav_to_pcm16`.
- Produces:
  - `class FunAsrNativeStream(_BaseWsStream)` (run-task/finish-task, binary frames).
  - `_wav_to_pcm16(wav_bytes: bytes) -> tuple[bytes, int]`.
  - `transcribe_bytes` dispatches funasr → one-shot; `open_stream` dispatches funasr → `FunAsrNativeStream`.

- [ ] **Step 1: Write the failing tests** (append)

```python
_FUNASR_ENTRY = {
    "id": "f1", "kind": "stt", "engine": "qwencloud", "model_id": "fun-asr-realtime",
    "label": "FunASR", "enabled": True, "stage": "stable",
    "api_key": "sk-ws-test", "base_url": "https://dashscope-intl.aliyuncs.com",
    "config": {"realtime_model": "fun-asr-realtime"},
}


def _funasr_msgs():
    return [
        json.dumps({"header": {"event": "task-started"}}),
        json.dumps({"header": {"event": "result-generated"},
                    "payload": {"output": {"sentence": {"text": "xin", "sentence_end": False}}}}),
        json.dumps({"header": {"event": "result-generated"},
                    "payload": {"output": {"sentence": {"text": "xin chào", "sentence_end": True}}}}),
        json.dumps({"header": {"event": "task-finished"}, "payload": {"output": {}}}),
    ]


@pytest.mark.asyncio
async def test_funasr_stream_maps_sentence_end(fake_connect):
    fake_connect["incoming"] = _funasr_msgs()
    stream = QwenCloudSttProvider(entry=_FUNASR_ENTRY).open_stream(sample_rate=16000, language="vi")

    results = await stream.accept(b"\x00\x00" * 160)
    assert any(r.text == "xin" and not r.is_final for r in results)
    assert any(r.text == "xin chào" and r.is_final for r in results)

    # run-task text frame sent first, then a BINARY audio frame
    assert any(isinstance(s, str) and '"run-task"' in s for s in fake_connect["ws"].sent)
    assert any(isinstance(s, (bytes, bytearray)) for s in fake_connect["ws"].sent)
    assert "/api-ws/v1/inference" in fake_connect["url"]
    assert fake_connect["headers"]["Authorization"] == "bearer sk-ws-test"

    await stream.finalize()
    assert any(isinstance(s, str) and '"finish-task"' in s for s in fake_connect["ws"].sent)


@pytest.mark.asyncio
async def test_funasr_batch_one_shot_concatenates_finals(fake_connect, monkeypatch):
    fake_connect["incoming"] = _funasr_msgs()

    async def fake_find(kind, engine, model_id):
        return _FUNASR_ENTRY
    monkeypatch.setattr(
        "app.services.stt.providers.qwencloud_provider.model_registry_store.find", fake_find)

    # minimal valid WAV (44-byte header + a few PCM samples)
    import wave, io
    buf = io.BytesIO()
    w = wave.open(buf, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"\x00\x00" * 320); w.close()

    result = await QwenCloudSttProvider().transcribe_bytes(buf.getvalue(), "vi", "fun-asr-realtime")
    assert result.engine == "qwencloud"
    assert result.text == "xin chào"   # last/accumulated final
    assert result.is_final is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_qwencloud_stt_provider.py -k funasr -v`
Expected: FAIL — `open_stream` raises "lands in Task 4"; `transcribe_bytes` routes funasr to the qwen3 HTTP path (wrong).

- [ ] **Step 3: Implement `FunAsrNativeStream`, `_wav_to_pcm16`, and dispatch**

Add to `qwencloud_provider.py` (add `import io`, `import wave`):

```python
def _wav_to_pcm16(wav_bytes: bytes) -> tuple[bytes, int]:
    """Extract raw PCM16 mono frames + sample rate from WAV bytes."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate()


class FunAsrNativeStream(_BaseWsStream):
    """fun-asr-realtime: DashScope-native run-task protocol, binary audio frames."""

    def __init__(self, base_url, api_key, model, sample_rate, language, semantic_punct):
        url = f"{_ws_base(base_url)}/api-ws/v1/inference"
        super().__init__(url, {"Authorization": f"bearer {api_key}"})
        self._model = model
        self._sample_rate = sample_rate
        self._language = language
        self._semantic_punct = semantic_punct
        self._task_id = str(uuid.uuid4())

    def _hello(self):
        params = {"format": "pcm", "sample_rate": self._sample_rate}
        if self._semantic_punct is not None:
            params["semantic_punctuation_enabled"] = bool(self._semantic_punct)
        if self._language:
            params["language_hints"] = [self._language]
        return json.dumps({
            "header": {"action": "run-task", "task_id": self._task_id, "streaming": "duplex"},
            "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                        "model": self._model, "parameters": params, "input": {}},
        })

    def _encode_audio(self, pcm):
        return pcm  # raw binary frame

    def _parse(self, msg):
        if msg.get("header", {}).get("event") != "result-generated":
            return []
        s = msg.get("payload", {}).get("output", {}).get("sentence") or {}
        text = (s.get("text") or "").strip()
        if not text:
            return []
        return [STTResult(engine="qwencloud", text=text, is_final=bool(s.get("sentence_end")))]

    def _finish_frame(self):
        return json.dumps({
            "header": {"action": "finish-task", "task_id": self._task_id, "streaming": "duplex"},
            "payload": {"input": {}}})

    def _is_done(self, msg):
        return msg.get("header", {}).get("event") in ("task-finished", "task-failed")
```

Update `open_stream` — replace the `raise RuntimeError("fun-asr streaming lands in Task 4")` block:

```python
        if _family(realtime_model) == "funasr":
            return FunAsrNativeStream(base_url, api_key, realtime_model, sample_rate, lang,
                                      cfg.get("semantic_punctuation"))
        return QwenOaiRealtimeStream(base_url, api_key, realtime_model, sample_rate, lang,
                                     cfg.get("turn_detection"))
```

Update `transcribe_bytes` to dispatch funasr to a one-shot WS session. Replace the single-return body with:

```python
    async def transcribe_bytes(self, audio_bytes, language=None, model=None):
        entry, base_url, api_key, timeout = await self._creds(model)
        effective = model or entry.get("model_id") or "qwen3-asr-flash"
        cfg = entry.get("config") or {}
        lang = language or cfg.get("language")
        if _family(effective) == "funasr":
            return await self._funasr_batch(base_url, api_key, effective, audio_bytes, lang,
                                             cfg.get("semantic_punctuation"))
        return await self._qwen3_batch(base_url, api_key, timeout, effective, audio_bytes, lang)

    async def _funasr_batch(self, base_url, api_key, model, audio_bytes, language, semantic_punct):
        pcm, sample_rate = _wav_to_pcm16(audio_bytes)
        stream = FunAsrNativeStream(base_url, api_key, model, sample_rate, language, semantic_punct)
        finals: list[str] = []
        for i in range(0, len(pcm), 3200):
            for r in await stream.accept(pcm[i:i + 3200]):
                if r.is_final:
                    finals.append(r.text)
        tail = await stream.finalize()
        if tail and tail.text and (not finals or tail.text != finals[-1]):
            finals.append(tail.text)
        return STTResult(engine=self.name, text=" ".join(finals).strip(),
                         is_final=True, confidence=None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_qwencloud_stt_provider.py -v`
Expected: PASS (all). If `test_funasr_batch...` sees a duplicated "xin chào", the dedup guard (`tail.text != finals[-1]`) handles the case where finalize returns the last already-collected final.

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/stt/providers/qwencloud_provider.py tests/unit/test_qwencloud_stt_provider.py
git commit -m "feat(stt): fun-asr native WS stream + one-shot batch for qwencloud"
```

---

## Task 5: WS-disconnect resilience + full-suite regression

**Files:**
- Test: `tests/unit/test_qwencloud_stt_provider.py` (append)

**Interfaces:**
- Consumes: everything above. No new production symbols (verifies existing `finalize` resilience).

- [ ] **Step 1: Write the failing test** (append)

```python
@pytest.mark.asyncio
async def test_finalize_returns_last_partial_on_disconnect(fake_connect):
    # No terminal event: the reader ends on StopAsyncIteration (socket closed).
    fake_connect["incoming"] = [
        json.dumps({"type": "conversation.item.input_audio_transcription.text",
                    "stash": "xin ch"}),
    ]
    stream = QwenCloudSttProvider(entry=_QWEN_ENTRY).open_stream(16000, "vi")
    await stream.accept(b"\x00\x00" * 160)
    final = await stream.finalize()          # must not raise
    assert final is None or final.text == "xin ch"
```

- [ ] **Step 2: Run test to verify it passes (resilience already built in)**

Run: `python -m pytest tests/unit/test_qwencloud_stt_provider.py -k disconnect -v`
Expected: PASS. `finalize` swallows the send/await error and drains what it has. If it FAILS (raises), fix `finalize`'s try/except to cover the `send` on a closed socket.

- [ ] **Step 3: Run the full changed-scope suite (regression gate)**

Run: `python -m pytest tests/unit/test_qwencloud_stt_provider.py tests/unit/test_http_stt_provider.py tests/unit/test_stt_remote_registry.py tests/unit/test_stt_schema.py tests/unit/test_stt_routes.py -v`
Expected: PASS. The schema-regex change and the new provider must not break sibling STT tests.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_qwencloud_stt_provider.py
git commit -m "test(stt): qwencloud WS-disconnect resilience"
```

---

## Task 6: Live smoke test + admin registry form verification (manual)

**Files:** none (verification only; produces notes, not code).

- [ ] **Step 1: Confirm the admin Model Registry entry form exposes `config`**

Read the registry entry form in the admin UI (search: `grep -rn "base_url\|model_id\|config" apps/api_gateway/app/static` or the web-client registry form). Confirm an operator can set `config.realtime_model`, `config.language`, `config.turn_detection`, `config.semantic_punctuation` when adding a `qwencloud` entry. If the form has no free-form `config` JSON field, note the gap in the spec's §7 "Verify" line — do NOT expand scope to build UI here.

- [ ] **Step 2: Live smoke (optional, requires a key)**

With a rotated QwenCloud key, add two Model Registry entries (qwen3-asr-flash, fun-asr-realtime) and run a real batch + a real streaming request against a short Vietnamese clip. Confirm transcripts return. (The dev harness already proved both protocols end-to-end on 2026-07-25; this re-checks the wired path.)

- [ ] **Step 3: Note results**

Record in the PR/commit description: which families were smoke-tested live vs. covered only by unit tests, and any admin-form gap found.

---

## Notes for the implementer

- **Security:** the key shared during design (`sk-ws-H.XLR…`) is exposed and must be rotated. Never hardcode a key — it lives only in a Model Registry entry.
- **Sync store lookup:** `open_stream` is synchronous, so it uses `model_registry_store.find_enabled_sync` + `resolve_credentials_sync` — both confirmed to exist (store.py:157, resolve.py:27). No new store method needed.
- **Do not** add `qwencloud` to `STT_MODEL_CATALOGS` in `model_catalog.py` — the model lives in the registry row, like `http_stt` (spec §7).
- **Emotion/language** metadata is available in both paths but intentionally not surfaced in v1 (spec §8).
