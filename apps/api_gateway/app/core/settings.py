import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Sinh một lần lúc import -> ổn định trong process, reset khi restart.
# Giữ đúng hành vi cũ của main.py (secrets.token_hex(32) ở module scope).
_GENERATED_SESSION_SECRET = secrets.token_hex(32)


def _default_app_root() -> Path:
    """Repo root derived from this file's own location, NOT the process CWD.

    settings.py lives at apps/api_gateway/app/core/settings.py, so
    parents[4] is core -> app -> api_gateway -> apps -> ROOT. Verified by
    checking the resolved path contains pyproject.toml (see task-2-report.md).
    """
    return Path(__file__).resolve().parents[4]


# Stable anchor for the CWD-dependent path defaults below (artifacts_dir,
# stt_model_dir, the sqlite path inside database_url). Closes B1: a process
# started with CWD=apps/api_gateway/ used to silently read/write a second,
# stale data/app.db + artifacts/ there instead of the repo-root ones. Prefer
# an explicit APP_ROOT env var (e.g. for containers with an unusual layout);
# otherwise derive it from this file's location so it's correct regardless
# of where the process is launched from.
APP_ROOT: Path = Path(os.environ["APP_ROOT"]).resolve() if os.environ.get("APP_ROOT") else _default_app_root()

_SQLITE_AIOSQLITE_PREFIX = "sqlite+aiosqlite:///"


def resolve_under_root(value: str, root: Path = APP_ROOT) -> str:
    """Resolve a plain relative path against `root`. Absolute paths (env
    overrides like ARTIFACTS_DIR=/tmp/artifacts) pass through unchanged."""
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str(root / path)


def resolve_sqlite_url(url: str, root: Path = APP_ROOT) -> str:
    """Anchor a relative sqlite+aiosqlite URL's path against `root`.

    `sqlite+aiosqlite:///data/app.db` (3 slashes = scheme + RELATIVE path
    `data/app.db`) becomes the 4-slash absolute form
    `sqlite+aiosqlite:////<root>/data/app.db`. Left unchanged: any non-sqlite
    URL (e.g. `postgresql://...`), an already-absolute sqlite URL (4 slashes,
    e.g. the model_service image's `sqlite+aiosqlite:////tmp/model_service.db`),
    and the in-memory `sqlite+aiosqlite:///:memory:` form.
    """
    if not url.startswith(_SQLITE_AIOSQLITE_PREFIX):
        return url
    path_part = url[len(_SQLITE_AIOSQLITE_PREFIX):]
    if path_part == ":memory:" or path_part.startswith("/"):
        return url
    return f"{_SQLITE_AIOSQLITE_PREFIX}{root / path_part}"


class Settings(BaseSettings):
    app_name: str = "speech-text-transformer"
    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    cors_allow_origins: str = "*"

    # Browser control-panel login (single shared password). Empty = auth disabled.
    admin_password: str = ""
    # Cookie-signing secret for the login session. Empty (with admin_password set)
    # -> a random secret is generated at process startup (sessions reset on restart).
    session_secret: str = ""
    # Shared secret for ESP32/RPi device WS clients, which can't do a browser
    # cookie login. Empty = device WS connections are rejected while
    # admin_password is set (browsers still work via cookie session).
    # NOTE: sent as a WS URL query param (not a header), so it will appear in
    # plaintext in standard reverse-proxy/uvicorn access logs -- account for
    # that in log handling/retention if you enable this.
    device_auth_token: str = ""

    # Bootstrap admin account, created once on startup if the `users` table is
    # empty. Falls back to admin_password (legacy single-secret login) with
    # username "admin" if these are unset, so upgrading an existing deployment
    # doesn't lock the operator out.
    admin_bootstrap_username: str = ""
    admin_bootstrap_password: str = ""

    # Allow the /v1/models/install endpoint to pip-install engine packages at runtime.
    # ON by default for this self-hosted deploy; set ALLOW_RUNTIME_INSTALL=false on
    # public deploys (it runs pip on the server). Installs are restricted to a fixed
    # allowlist.
    allow_runtime_install: bool = True

    artifacts_dir: str = "artifacts"
    # Generated audio artifacts older than this are pruned hourly (each TTS
    # sentence writes one file; without pruning, disk grows without bound).
    # 0 disables pruning. Old chat sessions' audio URLs 404 after their files
    # are pruned -- raise this if long-lived playback history matters.
    artifacts_ttl_hours: float = 24.0

    # LLM profiles + MCP tooling
    profiles_path: str = "profiles.json"
    tts_profiles_path: str = "tts_profiles.json"
    mcp_servers_path: str = "mcp_servers.json"
    system_config_path: str = "system_config.json"
    database_url: str = "sqlite+aiosqlite:///data/app.db"
    mcp_tool_cache_ttl_seconds: int = 300
    mcp_connection_timeout_seconds: float = 10.0
    mcp_tool_timeout_seconds: float = 30.0
    # Function-calling / tool use
    conversation_tools_enabled: bool = False
    conversation_tool_max_iters: int = 3

    # Device MCP: gateway acts as an MCP client to a device advertising
    # features.mcp in its wakeup, discovering + relaying tool calls over the
    # Lugo WS (see apps/api_gateway/app/services/conversation/tools/device_mcp.py).
    device_mcp_enabled: bool = True
    device_mcp_request_timeout_s: float = 10.0
    device_mcp_discovery_timeout_s: float = 10.0

    # Livehost: TikTok Live AI co-host (see docs/superpowers/specs/2026-07-05-livehost-tiktok-cohost-design.md).
    # Comma-separated keywords that boost a comment's reply priority (e.g. bot name).
    livehost_mention_keywords: str = ""
    # Backlog size at/under which the scheduler replies to events one at a time.
    livehost_individual_threshold: int = 3
    # Above the threshold, how many top-priority events to fold into one batch reply.
    livehost_batch_top_k: int = 3
    # Hard cap on pending events; lowest-priority non-gift/non-mention entries are
    # dropped first once exceeded.
    livehost_queue_max_size: int = 200
    # TikTok ingestor reconnect backoff (transient errors): starts here, doubles up
    # to the max, plus jitter.
    livehost_backoff_initial_seconds: float = 1.0
    livehost_backoff_max_seconds: float = 60.0
    # How often to re-check whether an offline room has gone live again.
    livehost_offline_poll_interval_seconds: float = 30.0
    # Force-reconnect if no event arrives for this long while state is "live" (a
    # connection that died without a clean disconnect signal).
    livehost_watchdog_idle_seconds: float = 300.0

    # STT/TTS deployment-time config: read once at process startup/init, never
    # meaningfully "tuned" live -- kept out of the admin-editable SystemConfig
    # on purpose (see docs/superpowers/specs/2026-07-23-system-settings-restructure-design.md).
    ollama_bin: str = ""
    warmup_on_startup: bool = True
    warmup_startup_timeout_s: int = 180
    stt_model_dir: str = "models/stt"
    vosk_model_base_url: str = "https://alphacephei.com/vosk/models"
    stt_stream_sample_rate: int = 16000
    stt_glossary_path: str = ""
    pyannote_vad_model: str = "pyannote/segmentation-3.0"
    pyannote_auth_token: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def auth_enabled(self) -> bool:
        return bool(self.admin_password or self.admin_bootstrap_password)

    @property
    def effective_session_secret(self) -> str:
        """Secret ký cho cả cookie session lẫn Lugo bearer token. Rỗng ->
        secret ngẫu nhiên mỗi process: session và token cùng reset khi
        restart, đúng bằng hành vi trước đây."""
        return self.session_secret or _GENERATED_SESSION_SECRET

    @property
    def cors_origins_list(self) -> list[str]:
        value = self.cors_allow_origins.strip()
        if not value or value == "*":
            return ["*"]
        return [origin.strip() for origin in value.split(",") if origin.strip()]

    # CWD-independent forms of the three path-shaped settings above (Task 2,
    # closes B1). The raw fields (`artifacts_dir`, `stt_model_dir`,
    # `database_url`) keep their literal string values -- consumers that need
    # an actual filesystem/DB location should use these instead.
    @property
    def artifacts_dir_resolved(self) -> str:
        return resolve_under_root(self.artifacts_dir)

    @property
    def stt_model_dir_resolved(self) -> str:
        return resolve_under_root(self.stt_model_dir)

    @property
    def database_url_resolved(self) -> str:
        return resolve_sqlite_url(self.database_url)

settings = Settings()
