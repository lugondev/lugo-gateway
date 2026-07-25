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
import contextlib
import json
import uuid
from urllib.parse import urlsplit

import httpx
import websockets

from app.schemas.stt import STTResult
from app.services.http_errors import translate_httpx_error
from app.services.model_registry.store import model_registry_store
from app.services.providers.resolve import resolve_credentials
from app.services.stt.base import BufferingStream, STTProvider, STTStream

_DEFAULT_BASE_URL = "https://dashscope-intl.aliyuncs.com"
_DEFAULT_TIMEOUT = 60.0
_MM_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
_FUNASR_ASYNC_PATH = "/api/v1/services/audio/asr/transcription"
_UPLOAD_PATH = "/api/v1/uploads"
_TASKS_PATH = "/api/v1/tasks"
_ASYNC_MAX_WAIT = 180.0  # seconds; overridable via entry config.timeout_seconds


def _family(model: str | None) -> str:
    """Map a model id to its family. Default qwen3 (the primary family)."""
    return "funasr" if (model or "").strip().lower().startswith("fun-asr") else "qwen3"


def _batch_model(model: str) -> str:
    """The batch model for a given model id: strip a trailing '-realtime'.

    A '-realtime' model is a conversation/streaming model; batch always uses the
    non-realtime counterpart (qwen3-asr-flash-realtime -> qwen3-asr-flash,
    fun-asr-realtime -> fun-asr, fun-asr-mtl -> fun-asr-mtl)."""
    m = (model or "").strip()
    return m[: -len("-realtime")] if m.lower().endswith("-realtime") else m


def _transcription_url(out: dict) -> str | None:
    """Pull the signed transcript URL out of a SUCCEEDED task's output.results
    (the entry may be nested one more level in older shapes)."""
    for res in out.get("results", []) or []:
        if res.get("transcription_url"):
            return res["transcription_url"]
        for sub in res.get("results", []) or []:
            if sub.get("transcription_url"):
                return sub["transcription_url"]
    return None


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
        self._ready = asyncio.Event()
        self._ready_timeout = 10.0
        self._error: str | None = None

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

    def _is_ready(self, msg: dict) -> bool:
        return False

    def _failure(self, msg: dict) -> str | None:
        """Error message if this terminal frame is a failure, else None."""
        return None

    # --- machinery ---
    async def _ensure(self) -> None:
        if self._ws is not None:
            return
        try:
            self._ws = await _ws_connect(self._url, self._headers)
            hello = self._hello()
            if hello is not None:
                await self._ws.send(hello)
        except Exception as exc:  # handshake/connect failures -> RuntimeError for the route
            raise RuntimeError(f"qwencloud stream connect failed: {exc}") from exc
        self._reader = asyncio.create_task(self._read_loop())
        # FIX 3: wait for the server to signal readiness before any audio is sent
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self._ready_timeout)
        except asyncio.TimeoutError:
            pass  # proceed anyway; not all servers emit a distinct ready event

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not self._ready.is_set() and self._is_ready(msg):
                    self._ready.set()
                for result in self._parse(msg):
                    await self._q.put(result)
                if self._is_done(msg):
                    self._error = self._failure(msg)
                    break
        except Exception:  # noqa: BLE001 - a dropped socket ends the stream, not the app
            pass
        finally:
            self._ready.set()
            self._done.set()

    def _drain(self) -> list[STTResult]:
        out = []
        while not self._q.empty():
            out.append(self._q.get_nowait())
        return out

    async def accept(self, pcm: bytes) -> list[STTResult]:
        await self._ensure()
        if self._done.is_set():
            # upstream ended / socket closed -- don't send into a dead socket
            return self._drain()
        try:
            await self._ws.send(self._encode_audio(pcm))
        except Exception as exc:  # ConnectionClosed etc. -> RuntimeError so the route emits an error event
            raise RuntimeError(f"qwencloud stream send failed: {exc}") from exc
        await asyncio.sleep(0)  # let the reader parse a frame that has already arrived
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
        err = self._error
        await self._close()
        if err:
            raise RuntimeError(f"qwencloud stream task failed: {err}")
        finals = [r for r in results if r.is_final]
        text = finals[-1].text if finals else (results[-1].text if results else "")
        return STTResult(engine="qwencloud", text=text, is_final=True, confidence=None) if text else None

    async def _close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    async def aclose(self) -> None:
        await self._close()


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

    def _is_ready(self, msg):
        return msg.get("type") in ("session.created", "session.updated")


_FUNASR_BATCH_SAMPLE_RATE = 16000


def _wav_to_pcm16(wav_bytes: bytes) -> tuple[bytes, int]:
    """Decode any supported upload to raw PCM16 mono @ 16 kHz for the fun-asr
    one-shot WS. Uses the project's canonical soundfile-fallback decoder
    (wav_bytes_to_pcm16), so a non-WAV file (mp3/ogg/flac) or a stereo/other-rate
    WAV just works -- the old raw-``wave`` path raised 'file does not start with
    RIFF id' on any non-RIFF container and rejected non-mono/16-bit WAVs."""
    from app.core.audio import wav_bytes_to_pcm16

    return wav_bytes_to_pcm16(wav_bytes, _FUNASR_BATCH_SAMPLE_RATE), _FUNASR_BATCH_SAMPLE_RATE


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

    def _is_ready(self, msg):
        return msg.get("header", {}).get("event") == "task-started"

    def _failure(self, msg):
        h = msg.get("header", {})
        if h.get("event") == "task-failed":
            return h.get("error_message") or h.get("error_code") or "task failed"
        return None


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
        cfg = entry.get("config") or {}
        lang = language or cfg.get("language")
        # Batch always uses the non-realtime model (a '-realtime' id is a
        # conversation model; see _batch_model). qwen3 -> inline; fun-asr -> async
        # multilingual file-transcription (fun-asr-realtime is Chinese-only and is
        # for streaming, never batch).
        bm = _batch_model(effective)
        if _family(bm) == "funasr":
            max_wait = cfg.get("timeout_seconds") if cfg.get("timeout_seconds") is not None else _ASYNC_MAX_WAIT
            return await self._funasr_async_batch(base_url, api_key, bm, audio_bytes, float(max_wait))
        return await self._qwen3_batch(base_url, api_key, timeout, bm, audio_bytes, lang)

    async def _funasr_async_batch(self, base_url, api_key, model, audio_bytes, max_wait):
        """Multilingual fun-asr batch via the async file-transcription API.

        DashScope has no external hosting requirement: getPolicy yields a
        temporary OSS upload, and the async ASR resolves the resulting oss://
        URL when X-DashScope-OssResourceResolve is enabled. No language_hints are
        sent -- fun-asr-mtl auto-detects (this is what makes Vietnamese work)."""
        host = _host_base(base_url)
        auth = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                # 1. temporary upload policy
                resp = await client.get(f"{host}{_UPLOAD_PATH}",
                                        params={"action": "getPolicy", "model": model}, headers=auth)
                resp.raise_for_status()
                pol = resp.json()["data"]
                key = f"{pol['upload_dir']}/audio.wav"
                # 2. OSS PostObject upload (temporary, private, ~5 min TTL)
                form = {
                    "OSSAccessKeyId": pol["oss_access_key_id"],
                    "Signature": pol["signature"],
                    "policy": pol["policy"],
                    "key": key,
                    "x-oss-object-acl": pol.get("x_oss_object_acl", "private"),
                    "x-oss-forbid-overwrite": pol.get("x_oss_forbid_overwrite", "true"),
                    "success_action_status": "200",
                }
                up = await client.post(pol["upload_host"], data=form,
                                       files={"file": ("audio.wav", audio_bytes, "audio/wav")})
                if up.status_code not in (200, 201, 203, 204):
                    raise RuntimeError(
                        f"{self.name} audio upload failed (HTTP {up.status_code}): {up.text[:200]}")
                oss_url = f"oss://{key}"
                # 3. submit async task (OssResourceResolve lets it read the oss:// url)
                resp = await client.post(
                    f"{host}{_FUNASR_ASYNC_PATH}",
                    headers={**auth, "X-DashScope-Async": "enable",
                             "X-DashScope-OssResourceResolve": "enable",
                             "Content-Type": "application/json"},
                    json={"model": model, "input": {"file_urls": [oss_url]}})
                resp.raise_for_status()
                task_id = resp.json()["output"]["task_id"]
                # 4. adaptive poll (near-realtime, rate-limit-safe)
                out = await self._poll_task(client, host, auth, task_id, max_wait)
                # 5. fetch + parse transcript
                turl = _transcription_url(out)
                if not turl:
                    raise RuntimeError(f"{self.name} async job returned no transcription_url")
                doc = await client.get(turl)
                doc.raise_for_status()
                text = "".join(t.get("text", "") for t in doc.json().get("transcripts", [])).strip()
        except httpx.HTTPError as exc:
            raise translate_httpx_error(self.name, exc) from exc
        except (KeyError, ValueError, TypeError) as exc:
            # DashScope can return HTTP 200 with an error envelope (throttle/quota)
            # or a shape we don't expect -- surface it as a clean STT error rather
            # than a raw KeyError/JSON error bubbling to a 500.
            raise RuntimeError(f"{self.name} async job: unexpected response ({exc})") from exc
        return STTResult(engine=self.name, text=text, is_final=True, confidence=None)

    async def _poll_task(self, client, host, auth, task_id, max_wait):
        elapsed = 0.0
        await asyncio.sleep(0.8)
        elapsed += 0.8
        while elapsed < max_wait:
            resp = await client.post(f"{host}{_TASKS_PATH}/{task_id}", headers=auth)
            resp.raise_for_status()
            out = resp.json().get("output", {})
            status = out.get("task_status")
            if status == "SUCCEEDED":
                return out
            if status == "FAILED":
                raise RuntimeError(
                    f"{self.name} async job failed: {out.get('message') or out.get('code') or 'unknown'}")
            interval = 1.0 if elapsed < 20 else 2.0
            await asyncio.sleep(interval)
            elapsed += interval
        raise RuntimeError(f"{self.name} async job timed out after {max_wait:.0f}s")

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

    def open_stream(self, sample_rate: int, language: str | None = None, model: str | None = None):
        from app.services.providers.resolve import resolve_credentials_sync

        entry = (
            self._entry_override
            or (model and model_registry_store.find_sync("stt", self.name, model))
            or model_registry_store.find_enabled_sync("stt", self.name)
        )
        if not entry:
            raise RuntimeError(f"{self.name} is not configured for streaming.")
        base_url, api_key = resolve_credentials_sync(entry)
        base_url = (base_url or "").strip() or _DEFAULT_BASE_URL
        cfg = entry.get("config") or {}
        fam = _family(model or entry.get("model_id") or cfg.get("realtime_model"))
        lang = language or cfg.get("language")
        # The conversation model: explicit config.realtime_model, else a family
        # default. qwen3 has a true realtime WS (qwen3-asr-flash-realtime);
        # fun-asr's only multilingual model is fun-asr-mtl, which is async (no
        # realtime WS), so it defaults to that. fun-asr-realtime (Chinese WS) is
        # opt-in via config.realtime_model.
        stream_model = cfg.get("realtime_model") or (
            "fun-asr-mtl" if fam == "funasr" else "qwen3-asr-flash-realtime"
        )
        # A non-realtime model configured for conversation (e.g. multilingual
        # fun-asr-mtl) can't stream incrementally -- buffer the turn and
        # transcribe on finalize, exactly like whisper / OpenRouter / http_stt
        # (every non-vosk engine streams this way). transcribe_bytes routes the
        # buffered audio to the async/inline batch path for that model.
        if not stream_model.endswith("-realtime"):
            return BufferingStream(self, sample_rate, lang, stream_model)
        if _family(stream_model) == "funasr":
            return FunAsrNativeStream(base_url, api_key, stream_model, sample_rate, lang,
                                      cfg.get("semantic_punctuation"))
        return QwenOaiRealtimeStream(base_url, api_key, stream_model, sample_rate, lang,
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
