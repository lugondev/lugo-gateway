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

import asyncio
import base64
import json
import uuid
from urllib.parse import urlsplit

import httpx
import websockets

from app.schemas.stt import STTResult
from app.services.http_errors import translate_httpx_error
from app.services.model_registry.store import model_registry_store
from app.services.providers.resolve import resolve_credentials
from app.services.stt.base import STTProvider, STTStream

_DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com"
_DEFAULT_TIMEOUT = 60.0
_MM_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
_ACCEPT_PUMP_TICKS = 32  # scheduler hops accept() yields to drain already-buffered frames


def _family(model: str | None) -> str:
    """Map a model id to its family. Default qwen3 (the primary family)."""
    return "funasr" if (model or "").strip().lower().startswith("fun-asr") else "qwen3"


def _host_base(base_url: str) -> str:
    """Scheme://host of a resolved base_url, dropping any path (e.g. a
    /compatible-mode/v1 suffix). QwenCloud STT endpoints are absolute from the
    host root. Falls back to the default host when empty/unparseable."""
    parts = urlsplit((base_url or "").strip() or _DEFAULT_BASE_URL)
    if not parts.scheme or not parts.netloc:
        return _DEFAULT_BASE_URL
    return f"{parts.scheme}://{parts.netloc}"


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
        # Let the reader run: each frame the background task consumes from the
        # transport (and each queue.put) is its own scheduler hop, so a single
        # sleep(0) only advances the reader one step. Yield a bounded number of
        # times so any already-buffered server frames get parsed and queued
        # before we drain -- without blocking on frames that haven't arrived.
        for _ in range(_ACCEPT_PUMP_TICKS):
            if self._done.is_set():
                break
            await asyncio.sleep(0)
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


class QwenCloudSttProvider(STTProvider):
    name = "qwencloud"

    def __init__(self, name: str = "qwencloud", timeout_seconds: float = _DEFAULT_TIMEOUT,
                 entry: dict | None = None) -> None:
        self.name = name
        self.timeout_seconds = timeout_seconds
        self._entry_override = entry  # only the registry test-before-add call passes this

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
                resp = await client.post(f"{_host_base(base_url)}{_MM_PATH}",
                                         headers=headers, json=body)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as exc:
            raise translate_httpx_error(self.name, exc) from exc
        return STTResult(engine=self.name, text=_mm_text(payload), is_final=True, confidence=None)

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
