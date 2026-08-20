# Knowledge Search Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the assistant a `search_knowledge` tool the LLM can call, backed by the finished `kbase` service, so the knowledge base is reachable from a conversation.

**Architecture:** A thin httpx client in `services/knowledge/` calls `kbase`'s `/v1/search`. A `KnowledgeToolSource` turns it into one `Tool` whose description the operator writes. Collection, `top_k` and `min_score` come from a `knowledge` block on `Profile`; the service URL and credential come from a new `SystemConfig` block. The tool fails open and meters its embedding spend.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, httpx (with `MockTransport` in tests), pytest (`asyncio_mode=auto`), ruff.

**Spec:** `docs/superpowers/specs/2026-08-20-knowledge-search-tool-design.md`

## Global Constraints

- All work is in `apps/api_gateway/` and `tests/unit/`. **`servers/knowledge-api` is not modified.**
- Create and stay on branch `feat/knowledge-search-tool`. Do not commit to `main`. Do not push.
- Run tests from the repo root with `.venv/bin/pytest`.
- **Run ONLY the test you are working on** while implementing (`pytest tests/unit/x/test_y.py::test_z -v`). Run the full suite once, at the end of a task. `tests/unit/test_concurrency_guard.py` deadlocks when two pytest runs overlap — never start a second run while one is going.
- **Every new test must be observed failing before its implementation is written.** Paste the actual failure output into your report. A test that has never been red is not evidence; this repo has a documented history of tests that cannot fail.
- Do not edit existing test assertions.
- ruff line-length 100. Run `.venv/bin/ruff check apps/api_gateway tests` before each commit.
- `from __future__ import annotations` at the top of every new module, matching neighbours.
- Commit as `lugondev <lugondev@gmail.com>` (`git -c user.name=lugondev -c user.email=lugondev@gmail.com commit`).
- **Never `SELECT *` on config tables** — `config_profiles` carries an LLM `api_key` inline, and a dump leaks it into logs and transcripts.

## Correction to the spec

The spec says the new `SystemConfig` fields follow "the `whisper_service_*` precedent already in that file". They do not: `RemoteSttConfig` is reached through `model_registry/resolve.py`'s legacy reconstruction (`get_raw_group("remote_stt")`) and is **not** nested in `SystemConfig`. It is the wrong template for new config.

Use `ConversationTuningConfig` instead — a live, nested block whose fields carry `Field(default=..., title=..., description=..., json_schema_extra={"subgroup": ...})`, which is what renders the admin UI. Everything else in the spec stands.

---

### Task 1: Configuration — a service block and a profile block

**Files:**
- Modify: `apps/api_gateway/app/services/system_config.py`
- Modify: `apps/api_gateway/app/services/profiles/models.py`
- Test: `tests/unit/profiles/test_profile_knowledge_config.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `KnowledgeServiceConfig` nested on `SystemConfig` as `.knowledge`, with `base_url: str`, `api_key: str`, `timeout_seconds: float`; and `KnowledgeConfig` on `Profile` as `.knowledge`, with `enabled: bool`, `collection: str`, `description: str`, `top_k: int`, `min_score: float`, `embed_model: str`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/profiles/test_profile_knowledge_config.py`:

```python
"""The knowledge block is off by default and optional on stored rows."""

from __future__ import annotations

from app.services.profiles.models import Profile
from app.services.system_config import SystemConfig


def test_a_profile_has_knowledge_disabled_by_default():
    p = Profile(name="p")
    assert p.knowledge.enabled is False
    assert p.knowledge.collection == ""
    assert p.knowledge.top_k == 5
    assert p.knowledge.min_score == 0.35


def test_a_stored_profile_without_a_knowledge_block_still_loads():
    # Every profile already persisted predates this field. If the model
    # required it, the first read after deploy would fail for all of them.
    p = Profile.model_validate({"name": "legacy"})
    assert p.knowledge.enabled is False


def test_a_knowledge_block_round_trips_through_serialization():
    p = Profile.model_validate(
        {
            "name": "shop",
            "knowledge": {
                "enabled": True,
                "collection": "faq",
                "description": "Tra cứu sổ tay bảo hành",
                "top_k": 3,
                "min_score": 0.5,
                "embed_model": "text-embedding-3-small",
            },
        }
    )
    again = Profile.model_validate(p.model_dump())
    assert again.knowledge.collection == "faq"
    assert again.knowledge.description == "Tra cứu sổ tay bảo hành"
    assert again.knowledge.top_k == 3
    assert again.knowledge.embed_model == "text-embedding-3-small"


def test_the_service_block_defaults_to_unconfigured():
    cfg = SystemConfig()
    assert cfg.knowledge.base_url == ""
    assert cfg.knowledge.api_key == ""
    assert cfg.knowledge.timeout_seconds == 10.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/profiles/test_profile_knowledge_config.py -v`
Expected: FAIL — `'Profile' object has no attribute 'knowledge'`.

- [ ] **Step 3: Add the profile block**

In `apps/api_gateway/app/services/profiles/models.py`, beside `MemoryConfig`:

```python
class KnowledgeConfig(BaseModel):
    enabled: bool = False       # off unless asked for: every existing profile is unchanged
    collection: str = ""        # which kbase collection this persona reads
    # What is in that collection, in the operator's own words. This becomes the
    # tool's description, and it is the only thing the model uses to decide
    # whether to call it. A generic description is the difference between a tool
    # that fires on "cảm ơn" and one that never fires when it matters.
    description: str = ""
    top_k: int = 5
    min_score: float = 0.35     # kbase's own default
    # Declared, not observed: the embedding happens inside kbase under its own
    # KB_EMBED_MODEL, and /v1/search does not name the model. record_usage needs
    # it to find the Model Registry row that carries the price -- a blank one
    # silently costs $0 forever. Must match kbase's KB_EMBED_MODEL.
    embed_model: str = ""
```

and on `Profile`, next to `memory: MemoryConfig = MemoryConfig()`:

```python
    knowledge: KnowledgeConfig = KnowledgeConfig()
```

- [ ] **Step 4: Add the service block**

In `apps/api_gateway/app/services/system_config.py`, beside `ConversationTuningConfig`:

```python
class KnowledgeServiceConfig(BaseModel):
    base_url: str = Field(
        default="",
        title="Knowledge base URL",
        description="Root URL of the kbase service. Empty disables the search_knowledge tool everywhere, whatever a profile asks for.",
        json_schema_extra={"subgroup": "Knowledge base"},
    )
    api_key: str = Field(
        default="",
        title="Knowledge base API key",
        description="Bearer credential for kbase. kbase maps it to a tenant, so this decides which collections are reachable at all.",
        json_schema_extra={"subgroup": "Knowledge base"},
    )
    timeout_seconds: float = Field(
        default=10.0,
        title="Knowledge search timeout (s)",
        description="A search runs inside a conversational turn, so this is latency the user hears. On timeout the tool fails open and the turn continues.",
        json_schema_extra={"subgroup": "Knowledge base", "unit": "s"},
    )
```

and add it to `SystemConfig`:

```python
    knowledge: KnowledgeServiceConfig = KnowledgeServiceConfig()
```

- [ ] **Step 5: Run the test, then the affected suites**

Run: `.venv/bin/pytest tests/unit/profiles/test_profile_knowledge_config.py -v`
Expected: PASS (4 tests)

Run: `.venv/bin/pytest tests/unit/profiles tests/unit/core -q`
Expected: all pass — a new field on a persisted model must not disturb existing profile round-trips.

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/system_config.py \
        apps/api_gateway/app/services/profiles/models.py \
        tests/unit/profiles/test_profile_knowledge_config.py
git commit -m "feat(knowledge): a service block and a per-profile knowledge block"
```

---

### Task 2: The client

**Files:**
- Create: `apps/api_gateway/app/services/knowledge/__init__.py`
- Create: `apps/api_gateway/app/services/knowledge/client.py`
- Test: `tests/unit/knowledge/test_knowledge_client.py` (create the directory; do **not** add an `__init__.py` — no sibling test directory has one, `tests/unit/memory/` and `tests/unit/conversation/` included)

**Interfaces:**
- Consumes: `SystemConfig.knowledge` from Task 1.
- Produces: `search_with_usage(collection, query, *, limit, min_score) -> tuple[list[dict], int]` on a module-level `knowledge_client`. Each hit is a dict with `text`, `title`, `filename`, `heading`, `score`. Raises `KnowledgeUnavailable` (defined in this module) for every failure mode — never an httpx exception.

The name mirrors `embed_texts_with_usage`: in this codebase a `*_with_usage` method means "this spends money and hands back the token count". Task 4 registers it with the paid-call-site harness, which scans for exactly these names.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/knowledge/test_knowledge_client.py`:

```python
"""The client against a stubbed kbase."""

from __future__ import annotations

import httpx
import pytest

from app.services.knowledge.client import KnowledgeClient, KnowledgeUnavailable

BODY = {
    "chunks": [
        {
            "text": "Bảo hành mười hai tháng.",
            "document_id": "d1",
            "title": "Sổ tay",
            "filename": "sotay.md",
            "heading": "Bảo hành",
            "score": 0.91,
        }
    ],
    "usage": {"prompt_tokens": 7},
}


def _client(handler, **kw):
    transport = httpx.MockTransport(handler)
    return KnowledgeClient(
        base_url="http://kb.invalid", api_key="secret-key", timeout=1.0, transport=transport, **kw
    )


async def test_it_parses_hits_and_usage():
    async def handler(request):
        return httpx.Response(200, json=BODY)

    hits, tokens = await _client(handler).search_with_usage("faq", "bảo hành", limit=5, min_score=0.3)

    assert tokens == 7
    assert hits[0]["text"] == "Bảo hành mười hai tháng."
    assert hits[0]["heading"] == "Bảo hành"


async def test_it_sends_the_bearer_credential_and_the_query():
    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read().decode()
        return httpx.Response(200, json=BODY)

    await _client(handler).search_with_usage("faq", "bảo hành", limit=3, min_score=0.4)

    assert seen["auth"] == "Bearer secret-key"
    assert '"collection": "faq"' in seen["body"] or '"collection":"faq"' in seen["body"]


async def test_a_non_200_raises_knowledge_unavailable():
    async def handler(request):
        return httpx.Response(503, text="upstream down")

    with pytest.raises(KnowledgeUnavailable):
        await _client(handler).search_with_usage("faq", "q", limit=5, min_score=0.3)


async def test_a_transport_error_raises_knowledge_unavailable():
    async def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(KnowledgeUnavailable):
        await _client(handler).search_with_usage("faq", "q", limit=5, min_score=0.3)


async def test_a_malformed_body_raises_knowledge_unavailable():
    async def handler(request):
        return httpx.Response(200, text="not json")

    with pytest.raises(KnowledgeUnavailable):
        await _client(handler).search_with_usage("faq", "q", limit=5, min_score=0.3)


async def test_missing_usage_counts_as_zero_tokens():
    async def handler(request):
        return httpx.Response(200, json={"chunks": []})

    hits, tokens = await _client(handler).search_with_usage("faq", "q", limit=5, min_score=0.3)
    assert hits == []
    assert tokens == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/knowledge/test_knowledge_client.py -v`
Expected: FAIL — `No module named 'app.services.knowledge'`.

- [ ] **Step 3: Write the client**

Create `apps/api_gateway/app/services/knowledge/__init__.py` (empty), then `apps/api_gateway/app/services/knowledge/client.py`:

```python
"""The one call the gateway makes into kbase: search.

One `AsyncClient` for the process, not one per call. This runs inside a
conversational turn, and rebuilding the connection pool each time pays a fresh
TCP and TLS handshake on the one path where latency is audible.
"""

from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = 10.0


class KnowledgeUnavailable(Exception):
    """The lookup could not be performed.

    Carries a message for the log, never for the model: an httpx error text
    holds the base URL, and a tool result may be spoken aloud.
    """


class KnowledgeClient:
    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    def configure(self, *, base_url: str, api_key: str, timeout: float) -> None:
        """Re-point at the configured service. The admin can change these at
        runtime, so they are read per call rather than frozen at import."""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client.timeout = httpx.Timeout(timeout)

    async def search_with_usage(
        self, collection: str, query: str, *, limit: int, min_score: float
    ) -> tuple[list[dict], int]:
        """Hits and the tokens kbase spent embedding the query.

        `*_with_usage` is this codebase's marker for "spends money, reports the
        count" -- see memory's `embed_texts_with_usage`. The paid-call-site
        inventory scans for that name.
        """
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            resp = await self._client.post(
                f"{self._base_url}/v1/search",
                headers=headers,
                json={
                    "collection": collection,
                    "query": query,
                    "limit": limit,
                    "min_score": min_score,
                },
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise KnowledgeUnavailable(f"knowledge search failed: {exc}") from exc
        if not isinstance(body, dict):
            raise KnowledgeUnavailable("knowledge search returned a non-object body")
        hits = body.get("chunks") or []
        tokens = int((body.get("usage") or {}).get("prompt_tokens") or 0)
        return list(hits), tokens

    async def aclose(self) -> None:
        await self._client.aclose()


knowledge_client = KnowledgeClient()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/knowledge/test_knowledge_client.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff check apps/api_gateway tests
git add apps/api_gateway/app/services/knowledge tests/unit/knowledge
git commit -m "feat(knowledge): an httpx client for kbase search"
```

---

### Task 3: The tool

**Files:**
- Create: `apps/api_gateway/app/services/conversation/tools/knowledge.py`
- Test: `tests/unit/conversation/test_knowledge_tool.py` (create)

**Interfaces:**
- Consumes: `KnowledgeClient.search_with_usage` (Task 2), `KnowledgeConfig` (Task 1), `Tool`/`ToolSource` from `app.services.conversation.tools.base`.
- Produces: `KnowledgeToolSource(profile, client, user_id)` implementing `list_tools() -> list[Tool]`, whose single `Tool` is named `search_knowledge` and takes one argument `query`.

**Why the tool catches its own errors:** `ToolRegistry.run` already wraps a raising tool — but it returns `f"Error running {name}: {exc}"`, and an httpx exception's text contains the base URL. Letting an error reach that handler leaks infrastructure into a string the model may read aloud. The tool must catch and return its own clean sentence.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/conversation/test_knowledge_tool.py`:

```python
"""The search_knowledge tool: rendering, budget, fail-open, metering."""

from __future__ import annotations

from app.services.conversation.tools.base import ToolContext
from app.services.conversation.tools.knowledge import MAX_CHARS, KnowledgeToolSource
from app.services.knowledge.client import KnowledgeUnavailable
from app.services.profiles.models import KnowledgeConfig, Profile


class FakeClient:
    def __init__(self, hits=None, tokens=0, error=None):
        self._hits, self._tokens, self._error = hits or [], tokens, error
        self.calls = []

    async def search_with_usage(self, collection, query, *, limit, min_score):
        self.calls.append((collection, query, limit, min_score))
        if self._error:
            raise self._error
        return self._hits, self._tokens


def _profile(**kw):
    cfg = {"enabled": True, "collection": "faq", "embed_model": "m", **kw}
    return Profile(name="shop", knowledge=KnowledgeConfig(**cfg))


def _hit(text="Mười hai tháng.", heading="Bảo hành", title="Sổ tay"):
    return {"text": text, "title": title, "filename": "s.md", "heading": heading, "score": 0.9}


def _ctx():
    return ToolContext()


async def test_the_operator_description_reaches_the_schema_verbatim():
    desc = "Tra cứu sổ tay bảo hành và chính sách đổi trả"
    src = KnowledgeToolSource(_profile(description=desc), FakeClient(), user_id="u")
    tool = src.list_tools()[0]
    assert tool.name == "search_knowledge"
    assert tool.description == desc


async def test_a_blank_description_falls_back_without_being_empty():
    src = KnowledgeToolSource(_profile(description=""), FakeClient(), user_id="u")
    tool = src.list_tools()[0]
    assert tool.description.strip()
    assert "faq" in tool.description


async def test_the_model_may_only_pass_a_query():
    src = KnowledgeToolSource(_profile(), FakeClient(), user_id="u")
    params = src.list_tools()[0].parameters
    assert set(params["properties"]) == {"query"}
    assert params["required"] == ["query"]


async def test_a_hit_is_rendered_with_its_heading_path():
    client = FakeClient(hits=[_hit()], tokens=4)
    src = KnowledgeToolSource(_profile(), client, user_id="u")
    out = await src.list_tools()[0].run({"query": "bảo hành"}, _ctx())
    assert "Sổ tay > Bảo hành" in out
    assert "Mười hai tháng." in out


async def test_profile_settings_drive_the_search_not_the_model():
    client = FakeClient(hits=[])
    src = KnowledgeToolSource(_profile(top_k=3, min_score=0.7), client, user_id="u")
    await src.list_tools()[0].run({"query": "q", "limit": 99, "collection": "other"}, _ctx())
    assert client.calls == [("faq", "q", 3, 0.7)]


async def test_no_hits_says_so_rather_than_returning_nothing():
    src = KnowledgeToolSource(_profile(), FakeClient(hits=[]), user_id="u")
    out = await src.list_tools()[0].run({"query": "q"}, _ctx())
    assert out.strip()


async def test_the_rendered_block_respects_the_budget():
    hits = [_hit(text="x" * 500, heading=f"H{i}") for i in range(20)]
    src = KnowledgeToolSource(_profile(), FakeClient(hits=hits), user_id="u")
    out = await src.list_tools()[0].run({"query": "q"}, _ctx())
    assert len(out) <= MAX_CHARS


async def test_a_failure_never_leaks_the_url_or_the_driver_error():
    err = KnowledgeUnavailable("knowledge search failed: connect to http://kb.internal:8090 refused")
    src = KnowledgeToolSource(_profile(), FakeClient(error=err), user_id="u")
    out = await src.list_tools()[0].run({"query": "q"}, _ctx())
    assert "kb.internal" not in out
    assert "8090" not in out
    assert out.strip()


async def test_an_unexpected_error_also_fails_open():
    src = KnowledgeToolSource(_profile(), FakeClient(error=RuntimeError("boom")), user_id="u")
    out = await src.list_tools()[0].run({"query": "q"}, _ctx())
    assert "boom" not in out
    assert out.strip()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/conversation/test_knowledge_tool.py -v`
Expected: FAIL — `No module named 'app.services.conversation.tools.knowledge'`.

- [ ] **Step 3: Write the tool**

Create `apps/api_gateway/app/services/conversation/tools/knowledge.py`:

```python
"""The `search_knowledge` tool.

The description is the feature. With always-inject the model sees the content
whether it wants it or not; with a tool it must choose to call, and it chooses
from the description alone. That text is the operator's, not ours.
"""

from __future__ import annotations

import logging

from app.services.conversation.tools.base import Tool, ToolContext, ToolSource

logger = logging.getLogger(__name__)

MAX_CHARS = 2000
UNAVAILABLE = "The knowledge base could not be reached just now."
NO_HITS = "No matching documents in the knowledge base."

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What to look up, in the user's own words.",
        }
    },
    "required": ["query"],
}


def _render(hits: list[dict], limit: int = MAX_CHARS) -> str:
    """Heading path then text, so the model can attribute what it answers."""
    parts: list[str] = []
    total = 0
    for hit in hits:
        title = (hit.get("title") or hit.get("filename") or "").strip()
        heading = (hit.get("heading") or "").strip()
        path = " > ".join(p for p in (title, heading) if p)
        text = (hit.get("text") or "").strip()
        if not text:
            continue
        block = f"### {path}\n{text}" if path else text
        extra = len(block) + (2 if parts else 0)
        if total + extra > limit:
            break
        parts.append(block)
        total += extra
    return "\n\n".join(parts)


class KnowledgeToolSource(ToolSource):
    def __init__(self, profile, client, *, user_id: str | None = None) -> None:
        self._profile = profile
        self._client = client
        self._user_id = user_id or ""

    def _description(self) -> str:
        written = (self._profile.knowledge.description or "").strip()
        if written:
            return written
        # Never empty: a tool with no description is one the model cannot judge.
        return (
            f"Search the '{self._profile.knowledge.collection}' knowledge base for "
            "reference material. Prefer it over guessing when the user asks about "
            "documented facts."
        )

    def list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="search_knowledge",
                description=self._description(),
                parameters=_PARAMETERS,
                run=self._run,
            )
        ]

    async def _run(self, args: dict, ctx: ToolContext) -> str:
        cfg = self._profile.knowledge
        query = (args or {}).get("query") or ""
        if not query.strip():
            return NO_HITS
        try:
            # limit and min_score come from the profile, never from `args`: a
            # model that picks the collection turns a prompt injection into a
            # cross-persona read, and one that picks top_k asks for fifty.
            hits, tokens = await self._client.search_with_usage(
                cfg.collection, query, limit=cfg.top_k, min_score=cfg.min_score
            )
        except Exception as exc:  # noqa: BLE001 - fail open; a raise kills the turn
            # Caught here rather than in ToolRegistry.run, which formats the
            # exception into its reply -- and an httpx error carries the base URL.
            logger.warning("knowledge search failed: %s", exc)
            return UNAVAILABLE
        await self._record(tokens)
        return _render(hits) or NO_HITS

    async def _record(self, tokens: int) -> None:
        if tokens <= 0:
            return
        from app.services.usage.recorder import record_usage

        await record_usage(
            user_id=self._user_id,
            profile_id=self._profile.name,
            kind="embed",
            engine=self._profile.llm.engine or "",
            model_id=self._profile.knowledge.embed_model,
            unit="tokens",
            native_amount=tokens,
            prompt_tokens=tokens,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/conversation/test_knowledge_tool.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
.venv/bin/ruff check apps/api_gateway tests
git add apps/api_gateway/app/services/conversation/tools/knowledge.py \
        tests/unit/conversation/test_knowledge_tool.py
git commit -m "feat(knowledge): the search_knowledge tool"
```

---

### Task 4: Wiring, and telling the metering harness about it

**Files:**
- Modify: `apps/api_gateway/app/services/conversation/session.py` (`_build_tool_registry`, line ~74–141)
- Modify: `tests/unit/test_paid_call_site_inventory.py`
- Test: `tests/unit/conversation/test_knowledge_tool_wiring.py` (create)
- Test: `tests/unit/usage/test_knowledge_usage_metering.py` (create)

**Interfaces:**
- Consumes: everything above.
- Produces: `search_knowledge` present in the session's `ToolRegistry` exactly when `SystemConfig.knowledge.base_url`, `profile.knowledge.enabled` and `profile.knowledge.collection` are all set.

**The harness matters here.** `tests/unit/test_paid_call_site_inventory.py` fails on any new provider-invoking call until it is classified, and its own docstring admits "a caller reaching a provider through an indirection these patterns do not match would slip past". `search_with_usage` is named to be caught rather than to slip: add it to `_PROVIDER_METHODS` and add the classifying row. Do not skip this because the suite is green without it — green here means invisible, which is the failure mode the harness exists to prevent.

- [ ] **Step 1: Write the failing wiring test**

Create `tests/unit/conversation/test_knowledge_tool_wiring.py`:

```python
"""The tool appears only when the service and the profile both say so."""

from __future__ import annotations

import pytest

from app.services.conversation.session import _build_tool_registry
from app.services.profiles.models import KnowledgeConfig, Profile


@pytest.fixture
def configured(monkeypatch):
    from app.services import system_config as sc

    cfg = sc.system_config_store.get().model_copy(deep=True)
    cfg.knowledge.base_url = "http://kb.invalid"
    cfg.knowledge.api_key = "k"
    monkeypatch.setattr(sc.system_config_store, "get", lambda: cfg)
    return cfg


def _profile(**kw):
    return Profile(name="shop", knowledge=KnowledgeConfig(**kw))


async def _names(profile):
    reg = await _build_tool_registry(profile)
    return reg.names() if reg else []


async def test_the_tool_is_present_when_configured_and_enabled(configured):
    assert "search_knowledge" in await _names(_profile(enabled=True, collection="faq"))


async def test_absent_when_the_profile_has_not_enabled_it(configured):
    assert "search_knowledge" not in await _names(_profile(enabled=False, collection="faq"))


async def test_absent_when_no_collection_is_bound(configured):
    assert "search_knowledge" not in await _names(_profile(enabled=True, collection=""))


async def test_absent_when_the_service_is_not_configured(monkeypatch):
    from app.services import system_config as sc

    cfg = sc.system_config_store.get().model_copy(deep=True)
    cfg.knowledge.base_url = ""
    monkeypatch.setattr(sc.system_config_store, "get", lambda: cfg)
    assert "search_knowledge" not in await _names(_profile(enabled=True, collection="faq"))


async def test_a_profile_with_no_knowledge_block_is_unaffected(configured):
    assert "search_knowledge" not in await _names(Profile(name="plain"))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/unit/conversation/test_knowledge_tool_wiring.py -v`
Expected: FAIL — `search_knowledge` is in no registry.

- [ ] **Step 3: Wire the source in**

In `apps/api_gateway/app/services/conversation/session.py`, inside
`_build_tool_registry`, after the `LocalToolSource` block and before the MCP
discovery block:

```python
    # Three switches, all required: the service must exist, the persona must
    # want it, and it must name a collection. Any one missing means no tool
    # rather than a tool that fails on every call.
    kb_cfg = system_config_store.get().knowledge
    kn = getattr(profile, "knowledge", None) if profile else None
    if kb_cfg.base_url and kn and kn.enabled and kn.collection:
        knowledge_client.configure(
            base_url=kb_cfg.base_url,
            api_key=kb_cfg.api_key,
            timeout=kb_cfg.timeout_seconds,
        )
        tool_sources.append(
            KnowledgeToolSource(profile, knowledge_client, user_id=identity_user_id)
        )
```

Add the imports at the top of the file:

```python
from app.services.conversation.tools.knowledge import KnowledgeToolSource
from app.services.knowledge.client import knowledge_client
```

`_build_tool_registry(profile, can_hang_up=False)` has no `identity_user_id`
parameter today. Add one — `_build_tool_registry(profile, can_hang_up=False, identity_user_id="")` — and pass `self.cfg.identity_user_id` from the call site in
`session.py` (the same value `memory_retriever.get_context` is given at line ~575).
Find every caller with `grep -rn "_build_tool_registry" apps/ tests/` and update
each; a missing user id means usage rows attributed to nobody.

- [ ] **Step 4: Run the wiring test**

Run: `.venv/bin/pytest tests/unit/conversation/test_knowledge_tool_wiring.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Write the metering test**

Create `tests/unit/usage/test_knowledge_usage_metering.py`:

```python
"""A knowledge search spends embedding money, so it records a row."""

from __future__ import annotations

from app.services.conversation.tools.base import ToolContext
from app.services.conversation.tools.knowledge import KnowledgeToolSource
from app.services.knowledge.client import KnowledgeUnavailable
from app.services.profiles.models import KnowledgeConfig, Profile


class FakeClient:
    def __init__(self, tokens=0, error=None):
        self._tokens, self._error = tokens, error

    async def search_with_usage(self, collection, query, *, limit, min_score):
        if self._error:
            raise self._error
        return [], self._tokens


def _profile():
    return Profile(
        name="shop",
        knowledge=KnowledgeConfig(
            enabled=True, collection="faq", embed_model="text-embedding-3-small"
        ),
    )


async def test_a_successful_search_records_one_embed_row(monkeypatch):
    rows = []

    async def fake_record(**kw):
        rows.append(kw)

    monkeypatch.setattr("app.services.usage.recorder.record_usage", fake_record)
    src = KnowledgeToolSource(_profile(), FakeClient(tokens=11), user_id="u1")
    await src.list_tools()[0].run({"query": "q"}, ToolContext())

    assert len(rows) == 1
    assert rows[0]["kind"] == "embed"
    assert rows[0]["prompt_tokens"] == 11
    assert rows[0]["user_id"] == "u1"
    assert rows[0]["profile_id"] == "shop"
    # Blank would silently price at $0 forever -- see recorder.record_usage.
    assert rows[0]["model_id"] == "text-embedding-3-small"


async def test_a_failed_search_records_nothing(monkeypatch):
    rows = []

    async def fake_record(**kw):
        rows.append(kw)

    monkeypatch.setattr("app.services.usage.recorder.record_usage", fake_record)
    src = KnowledgeToolSource(
        _profile(), FakeClient(error=KnowledgeUnavailable("down")), user_id="u1"
    )
    await src.list_tools()[0].run({"query": "q"}, ToolContext())

    assert rows == []
```

Run it: expected PASS if Task 3's `_record` is correct; if it fails, fix Task 3's
metering rather than this test.

- [ ] **Step 6: Register the call site with the harness**

In `tests/unit/test_paid_call_site_inventory.py`, add `"search_with_usage"` to
`_PROVIDER_METHODS`, and add the classifying row to `_CLASSIFIED`:

```python
    ("services/conversation/tools/knowledge.py", "search_with_usage"): (
        1, "metered+gated",
        "search_knowledge tool: kbase embeds the query on every call, so an "
        "LLM-invoked lookup is provider spend. Metered after the call returns.",
        "tests/unit/usage/test_knowledge_usage_metering.py",
    ),
```

Run: `.venv/bin/pytest tests/unit/test_paid_call_site_inventory.py -v`
Expected: PASS. If it reports a count mismatch, the count in the row must match
the number of `search_with_usage` calls the scanner finds — fix the number, do
not delete the row.

- [ ] **Step 7: Full suite and commit**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check apps/api_gateway tests
git add apps/api_gateway/app/services/conversation/session.py \
        tests/unit/conversation/test_knowledge_tool_wiring.py \
        tests/unit/usage/test_knowledge_usage_metering.py \
        tests/unit/test_paid_call_site_inventory.py
git commit -m "feat(knowledge): wire the tool into sessions and meter its spend"
```

The full suite must be green. A session-building change touches every
conversation test, and those are the regression net for "the registry still
contains what it used to".

---

## What the plan does not do

No route is added, so `core/auth_guard.py` needs no new prefix — the classification the superseded 2026-08-01 sketch called for disappears with the always-inject design it belonged to.

No admin UI field is built for the new config. The `Field(title=..., description=..., json_schema_extra={"subgroup": "Knowledge base"})` metadata is what the existing system-config UI renders from, so the fields appear without further work; a dedicated editing experience for `Profile.knowledge` is a separate piece.

`Profile.knowledge.embed_model` is declared by the operator and can drift from `kbase`'s `KB_EMBED_MODEL`. The spec records this as accepted; the fix, if the two ever disagree in practice, is to have `/v1/search` name the model it used.
