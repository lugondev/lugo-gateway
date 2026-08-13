from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.services.memory.subjects import ANON_SUBJECT


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ChatSession(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Indexed: every listing orders by it (History, and latest_for_client's
    # "the newest conversation of this client", which runs on every device
    # connect), so without it each of those sorts the whole table.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    # Which client this conversation belongs to. `source` is the kind of client
    # ("device", "web", ...) and `client_id` the instance within it -- a devices.id
    # for a speaker, the user id for the web (all a person's browsers share one
    # thread). Together they answer "the latest conversation for THIS client",
    # which is what a reconnecting device resumes; "the latest for this user" mixed
    # the speaker's thread with the browser's. Empty on rows written before this
    # existed, which is exactly why implicit resume skips them.
    source: Mapped[str] = mapped_column(String(16), default="", index=True)
    client_id: Mapped[str] = mapped_column(String(64), default="", index=True)


class ChatMessage(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id"), index=True
    )
    turn: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryItem(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(128), index=True)
    # Default is the ownerless subject, not "": a raw insert that bypasses
    # MemoryStore.add must still land somewhere a scoped query can find. See
    # services/memory/subjects.py.
    user_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, default=ANON_SUBJECT, index=True
    )
    content: Mapped[str] = mapped_column(Text)
    source_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MemoryProfileDoc(Base):
    __tablename__ = "memory_profile_docs"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=ANON_SUBJECT)
    profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    content: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="user")
    can_use_testing: Mapped[bool] = mapped_column(Boolean, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    serial: Mapped[str] = mapped_column(String(128), index=True)
    # The profile this device runs, by NAME -- profile_store is keyed by name,
    # same as sessions.profile_id above. "" means unassigned, which is a legal
    # state: a device keeps its pairing token when its profile is deleted or
    # deliberately unassigned, so it never has to be re-paired over a soft
    # setting. A name here may dangle only between a profile delete and the
    # binding sweep in profiles.py; every read path resolves it through
    # visible_profile_or_none and falls back to defaults.
    profile_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class ModelRegistryEntry(Base):
    __tablename__ = "model_registry_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), index=True)   # "stt" | "tts" | "llm"
    engine: Mapped[str] = mapped_column(String(64), index=True)
    model_id: Mapped[str] = mapped_column(String(128), index=True)
    label: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    stage: Mapped[str] = mapped_column(String(16), default="stable")  # "stable" | "testing"
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-model credential, looked up by (kind, engine, model_id) at call time
    # instead of a single system-wide key. Used today by OpenRouter-backed STT
    # engines (qwen3_asr_or/whisper_or) and by kind="llm" entries (see
    # conversation/responder.py's resolve_llm_override_from_registry). Stored
    # for kind="tts" too for UI/schema consistency even though no current TTS
    # engine reads it (all run local, no auth) -- ready for one that does.
    api_key: Mapped[str] = mapped_column(String(256), default="")
    # Endpoint override, meaningful for kind="llm" only (an OpenAI-compatible
    # base_url paired with this entry's model_id/api_key). Empty otherwise.
    base_url: Mapped[str] = mapped_column(String(256), default="")
    # Engine-specific settings (device, compute_type, timeouts, server host/port,
    # ...) validated against a per-(kind, engine) Pydantic model -- see
    # app/services/model_registry/config_schemas.py. Shape varies by engine, so
    # this stays a free-form JSON blob rather than dedicated columns.
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Provider family: "openai" | "openrouter" | "qwencloud" | custom string.
    name: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    # OpenAI-compatible base URL (ends with /v1). Shared by every registry entry
    # whose config.provider_id points here.
    base_url: Mapped[str] = mapped_column(String(256), default="")
    api_key: Mapped[str] = mapped_column(String(256), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Extra per-provider knobs (default timeout, org id, extra headers). Free-form.
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    # "" = shared-device / anonymous bucket (matches memory user-scoping convention).
    user_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    profile_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    # "" when the model isn't linked to a Provider (local engine / own creds).
    provider_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    kind: Mapped[str] = mapped_column(String(8), index=True)      # stt|tts|llm
    engine: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(16))                 # tokens|seconds|chars
    native_amount: Mapped[float] = mapped_column(Float, default=0.0)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|error|blocked


class Quota(Base):
    __tablename__ = "quotas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), index=True)   # user|provider|global
    # user_id | provider_id | "" (global)
    scope_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    limit_usd: Mapped[float] = mapped_column(Float, default=0.0)
    period: Mapped[str] = mapped_column(String(16), default="monthly")  # monthly|total
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
