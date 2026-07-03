# Chat History + Per-Profile Memory — Design

Date: 2026-07-03
Status: Approved

## Goal

Persist chat history per session and long-term memory per profile (mem0-inspired),
with a profile editor UI modeled on the xiaozhi configuration screen (nickname,
persona prompt, memory list, model config).

## Decisions (from brainstorming)

- **History**: per-session, keyed by `session_id`. UI can list and reload old sessions.
- **Memory**: per-profile. Hybrid: auto-extracted by LLM after each session AND
  manually editable (add/edit/delete items) in the profile panel.
- **Retrieval**: default inject ALL memories into the system prompt; per-profile
  opt-in `semantic` mode uses embeddings + cosine top-k.
- **Storage**: SQLite via SQLAlchemy async (`aiosqlite`) so a later move to
  PostgreSQL is a connection-string change. New deps: `sqlalchemy[asyncio]`, `aiosqlite`.

## Data Schema

```
sessions
  id          TEXT PK           -- session_id UUID
  profile_id  TEXT              -- profile name ('' = no profile)
  created_at  DATETIME
  ended_at    DATETIME NULL
  meta        JSON              -- stt_engine, tts_engine, llm_model...

messages
  id          INTEGER PK AUTOINCREMENT
  session_id  TEXT FK sessions.id (CASCADE delete)
  turn        INTEGER
  role        TEXT              -- user | assistant | tool
  content     TEXT
  created_at  DATETIME

memories
  id                 TEXT PK    -- UUID
  profile_id         TEXT
  content            TEXT       -- fact, e.g. "User prefers Vietnamese"
  source_session_id  TEXT NULL
  embedding          JSON NULL  -- list[float]; populated when semantic mode on
  created_at         DATETIME
  updated_at         DATETIME
```

No separate embeddings table — vectors live inline in `memories.embedding`.

## Backend Services

```
services/
  db/
    engine.py        # async engine, init (create_all), session factory
    models.py        # ORM: Session, Message, Memory
  history/
    store.py         # SessionStore: create/append/get/list/mark_ended/delete
  memory/
    store.py         # MemoryStore: list/add/update/delete/delete_all per profile
    extractor.py     # MemoryExtractor: LLM call -> parse JSON facts -> upsert
    retriever.py     # MemoryRetriever: build "## User Memories" block for system prompt
    embedder.py      # optional embed text -> vector (lazy import / LLM endpoint)
```

### Extraction flow (post-session)

```
WS disconnect (or /chat completes with profile)
  -> SessionStore.mark_ended(session_id)
  -> asyncio.create_task(extractor.extract_and_upsert(session_id, profile_id))
     - skipped if profile.memory.enabled is False or history too short
     - LLM prompt: "Extract key durable facts about the user from this
       conversation. Return JSON array of strings."
     - dedupe against existing memories (exact/near match) before insert
     - failures logged, never crash the session teardown
```

### Injection flow (per turn)

```
build_responder_ex(..., profile)
  -> MemoryRetriever.get_context(profile_id, query=last_user_msg)
     - mode "all": all memories, newest first, capped (e.g. 50 items / ~2k chars)
     - mode "semantic": embed query, cosine vs memory.embedding, top_k
  -> prepend to system_prompt:
     "## User Memories\n- fact1\n- fact2\n\n<original system prompt>"
```

## API Routes

```
/v1/sessions
  GET    ?profile=name&limit=20&offset=0   -> list sessions (id, created_at, ended_at, first message preview, count)
  GET    /{session_id}                     -> session detail + messages
  DELETE /{session_id}                     -> delete session + messages

/v1/profiles/{name}/memories
  GET                       -> list memories
  POST   {content}          -> add manual memory
  PUT    /{memory_id}       -> edit memory content
  DELETE /{memory_id}       -> delete one
  DELETE                    -> delete all for profile
```

Changed routes:

- `POST /v1/conversation/chat?profile=&session_id=` — optional `session_id`:
  load existing messages as context, append new turns; without it, create a
  session and return its id in the response.
- `WS /v1/conversation/stream?profile=&session_id=` — same; `session_started`
  event includes the (new or resumed) `session_id`. On resume, prior messages
  seed the in-memory `history`.

## Profile Model Changes

```python
class MemoryConfig(BaseModel):
    enabled: bool = True        # auto-extract after session
    mode: str = "all"           # "all" | "semantic"
    top_k: int = 5              # semantic only
    extractor_model: str = ""   # "" = profile's own LLM

class Profile(BaseModel):
    name: str
    nickname: str = ""          # display name (xiaozhi "My Pet")
    llm: LlmConfig = LlmConfig()
    system_prompt: str = ""     # persona / "Introduction"
    tts: TtsConfig = TtsConfig()
    mcp_servers: list[McpServer] = []
    memory: MemoryConfig = MemoryConfig()   # NEW
```

Existing `profiles.json` entries stay valid (new fields default).

## UI

Profile panel (modeled on xiaozhi screenshot):
- **Nickname** field at top.
- **Memory** section under System Prompt:
  - toggle "Auto-extract memory", mode dropdown (All / Semantic).
  - list of memory items, each with edit and delete buttons; "+ Add memory".
- Right column unchanged (LLM URL/model/key, TTS engine/voice).

Chat section:
- **Sessions** control next to the profile bar: lists past sessions of the
  selected profile (timestamp + first-message preview), click to reload the
  history into the dialogue, "New session" button.
- Switching profile resets to a fresh session.

## Error Handling

- DB init failures surface at startup (fail fast).
- Memory extraction errors are logged and swallowed (never break teardown).
- Semantic mode with no embedder available falls back to "all" with a warning.
- Deleting a profile does not auto-delete its sessions/memories (kept; can be
  purged via the DELETE endpoints).

## Testing

- Unit: stores (CRUD, cascade delete), extractor JSON parsing + dedupe,
  retriever formatting + semantic top-k, profile model back-compat.
- Route tests: sessions + memories CRUD, chat with `session_id` resume.
- WS test: `session_started` carries session_id; messages persisted per turn.
