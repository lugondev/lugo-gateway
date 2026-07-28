import os
from pathlib import Path

from app.core.deps import module_available
from app.core.errors import EngineNotFoundError
from app.schemas.health import EngineHealth
from app.services.model_registry.health_probe import check_remote_engine_health, probe_service_health
from app.services.model_registry.resolve import resolve_remote_stt_config
from app.services.stt.base import STTProvider
from app.services.stt.providers.http_stt_provider import HttpSttProvider
from app.services.stt.providers.openrouter_provider import OpenRouterSttProvider
from app.services.stt.providers.remote_whisper_provider import RemoteWhisperProvider
from app.services.stt.providers.vosk_provider import VoskProvider
from app.services.stt.providers.qwen3_asr_gguf_provider import Qwen3AsrGgufProvider
from app.services.stt.providers.qwen3_asr_provider import Qwen3AsrProvider
from app.services.stt.providers.qwencloud_provider import QwenCloudSttProvider
from app.services.stt.providers.whisper_mlx_provider import WhisperMlxProvider
from app.services.stt.providers.whisper_provider import WhisperProvider
from app.services.warmup import _needs_warming, is_ready


class STTService:
    def __init__(self) -> None:
        whisper_local = WhisperProvider()
        remote_stt = resolve_remote_stt_config()
        self.providers: dict[str, STTProvider] = {
            "vosk": VoskProvider(),
            "whisper": whisper_local,
            "whisper_local": whisper_local,
            "whisper_mlx": WhisperMlxProvider(),
            "qwen3_asr": Qwen3AsrProvider(),
            "qwen3_asr_gguf": Qwen3AsrGgufProvider(),
            "whisper_service": RemoteWhisperProvider(
                name="whisper_service",
                base_url=remote_stt.whisper_service_base_url,
                api_key=remote_stt.whisper_service_api_key,
                model=remote_stt.whisper_service_model,
                timeout_seconds=remote_stt.remote_stt_timeout_seconds,
            ),
            "eventlab": RemoteWhisperProvider(
                name="eventlab",
                base_url=remote_stt.eventlab_base_url,
                api_key=remote_stt.eventlab_api_key,
                model=remote_stt.eventlab_model,
                timeout_seconds=remote_stt.remote_stt_timeout_seconds,
            ),
            "qwen3_asr_or": OpenRouterSttProvider(
                name="qwen3_asr_or",
                model="qwen/qwen3-asr-flash-2026-02-10",
                timeout_seconds=remote_stt.remote_stt_timeout_seconds,
            ),
            "whisper_or": OpenRouterSttProvider(
                name="whisper_or",
                model="openai/whisper-large-v3-turbo",
                timeout_seconds=remote_stt.remote_stt_timeout_seconds,
            ),
            "http_stt": HttpSttProvider(
                timeout_seconds=remote_stt.remote_stt_timeout_seconds
            ),
            "qwencloud": QwenCloudSttProvider(
                timeout_seconds=remote_stt.remote_stt_timeout_seconds
            ),
        }

    def reinit_remote_providers(self) -> None:
        """Rebuild whisper_service/eventlab/qwen3_asr_or/whisper_or with fresh
        settings — these providers cache base_url/api_key/model/timeout as
        instance attributes at construction and never re-read afterward."""
        remote_stt = resolve_remote_stt_config()
        self.providers["whisper_service"] = RemoteWhisperProvider(
            name="whisper_service",
            base_url=remote_stt.whisper_service_base_url,
            api_key=remote_stt.whisper_service_api_key,
            model=remote_stt.whisper_service_model,
            timeout_seconds=remote_stt.remote_stt_timeout_seconds,
        )
        self.providers["eventlab"] = RemoteWhisperProvider(
            name="eventlab",
            base_url=remote_stt.eventlab_base_url,
            api_key=remote_stt.eventlab_api_key,
            model=remote_stt.eventlab_model,
            timeout_seconds=remote_stt.remote_stt_timeout_seconds,
        )
        self.providers["qwen3_asr_or"] = OpenRouterSttProvider(
            name="qwen3_asr_or",
            model="qwen/qwen3-asr-flash-2026-02-10",
            timeout_seconds=remote_stt.remote_stt_timeout_seconds,
        )
        self.providers["whisper_or"] = OpenRouterSttProvider(
            name="whisper_or",
            model="openai/whisper-large-v3-turbo",
            timeout_seconds=remote_stt.remote_stt_timeout_seconds,
        )

    def get_provider(self, engine: str) -> STTProvider:
        provider = self.providers.get(engine)
        if provider is None:
            raise EngineNotFoundError(f"Unsupported STT engine: {engine}")
        return provider

    async def check_engine(self, engine: str, model: str = "") -> EngineHealth:
        """Whether a session can start on this engine right now.

        Only http_stt gets a live network probe -- it is the one STT engine
        backed by a self-hosted process that can silently die. Cloud engines
        are config-checked only, to avoid paying for an API call per session.
        """
        provider = self.providers.get(engine)
        if provider is None:
            return EngineHealth(
                engine=engine, status="unavailable", detail=f"unknown STT engine: {engine}"
            )

        if engine == "http_stt":
            return await check_remote_engine_health(
                "stt", engine, model, probe=probe_service_health
            )

        # Everything else: reuse the availability semantics list_engines()
        # already publishes, so the gate and the dashboard never disagree.
        # Match by PROVIDER IDENTITY, not by the row's engine-key string:
        # self.providers registers "whisper" and "whisper_local" (and other
        # alias keys) as the SAME provider instance, and list_engines() dedups
        # by id(provider) so only the first-inserted key ("whisper") ever
        # appears in its output -- a plain string match on `engine` would
        # never find a row for the alias key and silently fall through to
        # "ok" without ever consulting its real availability.
        for listed in await self.list_engines():
            if self.providers.get(listed["engine"]) is not provider:
                continue
            if not listed["available"]:
                return EngineHealth(
                    engine=engine, status="unavailable",
                    detail=listed.get("detail") or "not configured",
                )
            break

        if _needs_warming(provider) and not is_ready(provider):
            return EngineHealth(engine=engine, status="not_ready", detail="model still loading")
        return EngineHealth(engine=engine, status="ok")

    async def list_engines(self) -> list[dict]:
        # Lazy import to avoid any module load-order coupling.
        from app.services.model_registry.store import model_registry_store
        from app.services.stt.providers.vosk_provider import get_active_vosk_path
        from app.services.whisper_models import whisper_manager

        active_vosk_path = get_active_vosk_path()
        vosk_present = module_available("vosk") and os.path.isdir(active_vosk_path)
        vosk_detail = Path(active_vosk_path).name if vosk_present else None

        fw_available = module_available("faster_whisper")
        active_whisper = whisper_manager.snapshot()["active"]
        whisper_cached = whisper_manager._cached(active_whisper)
        whisper_detail = active_whisper + (" · cached" if whisper_cached else " · downloads on first use")

        remote_stt = resolve_remote_stt_config()
        remote = {
            "whisper_service": (remote_stt.whisper_service_base_url, remote_stt.whisper_service_model),
            "eventlab": (remote_stt.eventlab_base_url, remote_stt.eventlab_model),
        }

        result: list[dict] = []
        seen_providers: set[int] = set()
        for engine, provider in self.providers.items():
            # Skip alias keys that point to an already-listed provider (e.g. whisper_local).
            if id(provider) in seen_providers:
                continue
            seen_providers.add(id(provider))

            # Realtime = the provider implements native incremental streaming
            # (overrides open_stream); buffering engines return a final only on stop.
            realtime = type(provider).open_stream is not STTProvider.open_stream

            if engine == "vosk":
                entry = {"mode": "local", "available": vosk_present, "detail": vosk_detail}
            elif engine in ("whisper", "whisper_local"):
                entry = {"mode": "local", "available": fw_available, "detail": whisper_detail}
            elif engine == "whisper_mlx":
                mlx_ok = provider.available()
                entry = {
                    "mode": "local",
                    "available": mlx_ok,
                    "detail": provider.detail() if mlx_ok else "needs mlx-whisper + converted model",
                }
            elif engine == "qwen3_asr":
                q_ok = provider.available()
                entry = {
                    "mode": "local",
                    "available": q_ok,
                    "detail": provider.detail() if q_ok else "needs mlx-qwen3-asr (Apple) or qwen-asr (NVIDIA GPU)",
                }
            elif engine == "qwen3_asr_gguf":
                g_ok = provider.available()
                entry = {
                    "mode": "local",
                    "available": g_ok,
                    "detail": provider.detail() if g_ok else "needs the qwen3-asr-cli binary + a .gguf model",
                }
            elif engine in ("qwen3_asr_or", "whisper_or"):
                # Per-model key (Model Registry entry), not a system-wide toggle --
                # "configured" here means at least one enabled entry for this engine
                # has a key set, so it's actually usable for some model.
                # has_key_for_engine only looks at the entry's own api_key column,
                # so a provider-linked entry (blank own key, creds on the linked
                # provider) would wrongly report unconfigured -- resolve each
                # enabled entry for this engine instead.
                from app.services.providers.resolve import resolve_credentials

                configured = False
                for candidate in await model_registry_store.list_all():
                    if candidate["kind"] != "stt" or candidate["engine"] != engine or not candidate["enabled"]:
                        continue
                    _base_url, api_key = await resolve_credentials(candidate)
                    if api_key:
                        configured = True
                        break
                entry = {"mode": "remote", "available": configured, "detail": provider.model if configured else None}
            elif engine == "http_stt":
                # Configured = some enabled entry carries a base_url pointing at a
                # service; there is no per-engine singleton config to read.
                row = await model_registry_store.find_enabled("stt", "http_stt")
                base_url = (row or {}).get("base_url", "")
                entry = {"mode": "remote", "available": bool(base_url), "detail": base_url or None}
            elif engine == "qwencloud":
                from app.services.providers.resolve import resolve_credentials

                configured = False
                models: list[str] = []
                for candidate in await model_registry_store.list_all():
                    if (candidate["kind"] != "stt" or candidate["engine"] != "qwencloud"
                            or not candidate["enabled"]):
                        continue
                    _base_url, api_key = await resolve_credentials(candidate)
                    if api_key:
                        configured = True
                        if candidate["model_id"]:
                            models.append(candidate["model_id"])
                entry = {"mode": "remote", "available": configured, "detail": ", ".join(models) or None}
            else:
                base_url, model = remote[engine]
                configured = bool(base_url)
                entry = {"mode": "remote", "available": configured, "detail": model if configured else None}

            result.append(
                {"engine": engine, "configured": entry["available"], "realtime": realtime, **entry}
            )
        return result


stt_service = STTService()
