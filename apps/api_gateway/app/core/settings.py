from pydantic_settings import BaseSettings, SettingsConfigDict


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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def auth_enabled(self) -> bool:
        return bool(self.admin_password or self.admin_bootstrap_password)

    @property
    def cors_origins_list(self) -> list[str]:
        value = self.cors_allow_origins.strip()
        if not value or value == "*":
            return ["*"]
        return [origin.strip() for origin in value.split(",") if origin.strip()]

settings = Settings()
