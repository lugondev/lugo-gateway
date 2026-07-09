# Memory Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the gateway per-profile memory so it stops injecting unfiltered recent facts, dedupes semantically, and compacts a large raw-fact buffer into a structured user-identity profile document.

**Architecture:** Raw extracted facts become a transient buffer (`MemoryItem`). When the buffer reaches a threshold, one LLM call rebuilds a structured user-profile Markdown doc (`MemoryProfileDoc`) merging duplicates/contradictions, then the folded facts are pruned. Each turn injects the profile doc plus the small remaining buffer. Embeddings are computed at add-time (fixing dead semantic mode) but the whole system also works without an `embed_model`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async (aiosqlite), Pydantic, pytest + pytest-asyncio, httpx.

## Global Constraints

- Work on branch `feat/memory-compaction` (already created; spec committed there).
- All memory work is **best-effort**: extraction and compaction must never raise out of session teardown — catch, log at WARNING, swallow.
- **No data loss**: never delete raw facts unless the profile-doc write succeeded; on compaction delete only the fact ids captured at compaction start.
- Works with **no `embed_model`**: dedup falls back to exact `strip().lower()`; compaction uses the profile's chat LLM.
- Tests run from repo root with `.venv/bin/pytest tests/...` (pyproject sets `pythonpath=["apps/api_gateway"]`, `testpaths=["tests"]`, `asyncio_mode="auto"`; existing memory tests live in `tests/unit/`). Use the existing autouse fixtures `_hermetic` + `_tmp_db` (per-test tmp SQLite; no real network).
- New SQLAlchemy models auto-create via `Base.metadata.create_all` in `init_db()` — **no migration needed**, but the model must live in `app/services/db/models.py` so it is registered on `Base`.
- Follow existing file style: `from __future__ import annotations`, module-level singletons (e.g. `memory_store`), `# noqa: BLE001` on best-effort broad excepts.

---

### Task 1: Foundation — config fields, profile-doc model & store, `delete_many`

**Files:**
- Modify: `apps/api_gateway/app/services/profiles/models.py:29-34` (MemoryConfig)
- Modify: `apps/api_gateway/app/services/db/models.py` (add `MemoryProfileDoc`)
- Modify: `apps/api_gateway/app/services/memory/store.py` (add `ProfileDocStore` + `profile_doc_store`, `MemoryStore.delete_many`)
- Test: `tests/unit/test_memory_profile_doc_store.py` (new), `tests/unit/test_memory_store.py` (append)

Note on test path: existing memory tests are under the repo-root `tests/unit/`. Create the new test file there too: `tests/unit/test_memory_profile_doc_store.py`.

**Interfaces:**
- Produces:
  - `MemoryConfig.compaction_threshold: int = 20`, `MemoryConfig.max_facts: int = 200`, `MemoryConfig.dedup_threshold: float = 0.92`
  - `class MemoryProfileDoc(Base)` table `memory_profile_docs`, PK `profile_id: str`, `content: str`, `updated_at: datetime`
  - `profile_doc_store.get(profile_id) -> dict | None` (`{"profile_id","content","updated_at"}`)
  - `profile_doc_store.upsert(profile_id, content) -> dict`
  - `profile_doc_store.delete(profile_id) -> bool`
  - `MemoryStore.delete_many(ids: list[str]) -> int`

- [ ] **Step 1: Write the failing test for the profile-doc store**

Create `tests/unit/test_memory_profile_doc_store.py`:

```python
import pytest

from app.services.memory.store import profile_doc_store


@pytest.mark.asyncio
async def test_get_absent_returns_none():
    assert await profile_doc_store.get("ghost") is None


@pytest.mark.asyncio
async def test_upsert_creates_then_updates():
    created = await profile_doc_store.upsert("pet", "## User Profile\n- v1")
    assert created["content"] == "## User Profile\n- v1"
    got = await profile_doc_store.get("pet")
    assert got["content"] == "## User Profile\n- v1"

    updated = await profile_doc_store.upsert("pet", "## User Profile\n- v2")
    assert updated["content"] == "## User Profile\n- v2"
    assert (await profile_doc_store.get("pet"))["content"] == "## User Profile\n- v2"


@pytest.mark.asyncio
async def test_delete():
    await profile_doc_store.upsert("pet", "x")
    assert await profile_doc_store.delete("pet") is True
    assert await profile_doc_store.delete("pet") is False
    assert await profile_doc_store.get("pet") is None
```

Append to `tests/unit/test_memory_store.py`:

```python
@pytest.mark.asyncio
async def test_delete_many(store):
    a = await store.add("pet", "a")
    b = await store.add("pet", "b")
    c = await store.add("pet", "c")
    assert await store.delete_many([a["id"], b["id"]]) == 2
    remaining = {m["content"] for m in await store.list("pet")}
    assert remaining == {"c"}
    assert await store.delete_many([]) == 0
    _ = c
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_memory_profile_doc_store.py ../../tests/unit/test_memory_store.py::test_delete_many -v`
Expected: FAIL — `ImportError: cannot import name 'profile_doc_store'` / `AttributeError: 'MemoryStore' object has no attribute 'delete_many'`.

- [ ] **Step 3: Add the config fields**

In `apps/api_gateway/app/services/profiles/models.py`, replace the `MemoryConfig` class body:

```python
class MemoryConfig(BaseModel):
    enabled: bool = True        # auto-extract memories after a session ends
    mode: str = "all"           # "all" | "semantic"
    top_k: int = 5              # semantic mode: how many memories to inject
    extractor_model: str = ""   # "" = use the profile's own LLM model
    embed_model: str = ""       # semantic mode: OpenAI-compatible embedding model
    compaction_threshold: int = 20  # raw buffer facts that trigger compaction
    max_facts: int = 200            # hard cap; also forces compaction
    dedup_threshold: float = 0.92   # cosine >= this => treat new fact as duplicate
```

- [ ] **Step 4: Add the `MemoryProfileDoc` model**

In `apps/api_gateway/app/services/db/models.py`, append after `MemoryItem`:

```python
class MemoryProfileDoc(Base):
    __tablename__ = "memory_profile_docs"

    profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    content: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
```

- [ ] **Step 5: Add `delete_many`, `ProfileDocStore`, and the singleton**

In `apps/api_gateway/app/services/memory/store.py`, update the import line:

```python
from app.services.db.models import MemoryItem, MemoryProfileDoc, utcnow
```

Add inside `class MemoryStore` (after `delete_all`):

```python
    async def delete_many(self, ids: list[str]) -> int:
        if not ids:
            return 0
        async with db_session() as s:
            result = await s.execute(sa_delete(MemoryItem).where(MemoryItem.id.in_(ids)))
            await s.commit()
            return result.rowcount or 0
```

Append at end of file (after `memory_store = MemoryStore()`):

```python
def _doc_dict(d: MemoryProfileDoc) -> dict:
    return {
        "profile_id": d.profile_id,
        "content": d.content,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


class ProfileDocStore:
    async def get(self, profile_id: str) -> dict | None:
        async with db_session() as s:
            row = await s.get(MemoryProfileDoc, profile_id)
            return _doc_dict(row) if row else None

    async def upsert(self, profile_id: str, content: str) -> dict:
        async with db_session() as s:
            row = await s.get(MemoryProfileDoc, profile_id)
            if row is None:
                row = MemoryProfileDoc(profile_id=profile_id, content=content)
                s.add(row)
            else:
                row.content = content
                row.updated_at = utcnow()
            await s.commit()
            return _doc_dict(row)

    async def delete(self, profile_id: str) -> bool:
        async with db_session() as s:
            row = await s.get(MemoryProfileDoc, profile_id)
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
            return True


profile_doc_store = ProfileDocStore()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_memory_profile_doc_store.py ../../tests/unit/test_memory_store.py -v`
Expected: PASS (all, including the existing store tests).

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/profiles/models.py apps/api_gateway/app/services/db/models.py apps/api_gateway/app/services/memory/store.py tests/unit/test_memory_profile_doc_store.py tests/unit/test_memory_store.py
git commit -m "feat(memory): add profile-doc store, delete_many, and compaction config fields

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Embedding at add-time + cosine dedup in the extractor

**Files:**
- Modify: `apps/api_gateway/app/services/memory/extractor.py`
- Test: `tests/unit/test_memory_extractor.py` (append)

**Interfaces:**
- Consumes: `memory_store.add(profile_id, content, source_session_id=, embedding=)`, `embedder.embed_texts`, `embedder.cosine`, `MemoryConfig.dedup_threshold/embed_model`.
- Produces: `MemoryExtractor._maybe_embed(profile, texts) -> list[list[float] | None]`; unchanged public `extract_and_upsert(session_id, profile) -> int` now stores embeddings and cosine-dedupes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_memory_extractor.py`:

```python
@pytest.mark.asyncio
async def test_extract_and_upsert_stores_embedding(monkeypatch):
    await session_store.create("s3", profile_id="emb")
    await session_store.append_message("s3", 1, "user", "tôi thích trà")
    await session_store.append_message("s3", 1, "assistant", "ok")

    async def fake_extract(self, messages, base_url, api_key, model):
        return ["User likes tea"]

    async def fake_embed(texts, base_url, api_key, model):
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(MemoryExtractor, "extract", fake_extract)
    monkeypatch.setattr("app.services.memory.extractor.embed_texts", fake_embed)
    profile = Profile(
        name="emb",
        llm={"base_url": "http://llm.local/v1", "model": "m"},
        memory={"embed_model": "e"},
    )
    added = await MemoryExtractor().extract_and_upsert("s3", profile)
    assert added == 1
    rows = await memory_store.list("emb")
    assert rows[0]["embedding"] == [1.0, 0.0]


@pytest.mark.asyncio
async def test_extract_and_upsert_cosine_dedup(monkeypatch):
    await memory_store.add("emb2", "User enjoys tea", embedding=[1.0, 0.0])
    await session_store.create("s4", profile_id="emb2")
    await session_store.append_message("s4", 1, "user", "x")
    await session_store.append_message("s4", 1, "assistant", "y")

    async def fake_extract(self, messages, base_url, api_key, model):
        return ["User loves tea"]  # different string, near-identical meaning

    async def fake_embed(texts, base_url, api_key, model):
        return [[1.0, 0.02] for _ in texts]  # cosine ~1.0 vs stored

    monkeypatch.setattr(MemoryExtractor, "extract", fake_extract)
    monkeypatch.setattr("app.services.memory.extractor.embed_texts", fake_embed)
    profile = Profile(
        name="emb2",
        llm={"base_url": "http://llm.local/v1", "model": "m"},
        memory={"embed_model": "e", "dedup_threshold": 0.9},
    )
    added = await MemoryExtractor().extract_and_upsert("s4", profile)
    assert added == 0  # dropped as a semantic duplicate
    assert len(await memory_store.list("emb2")) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_memory_extractor.py -k "embedding or cosine" -v`
Expected: FAIL — embedding is `None` (add-time embedding not implemented) / `added == 1` (no cosine dedup).

- [ ] **Step 3: Implement embedding + cosine dedup**

In `apps/api_gateway/app/services/memory/extractor.py`, add to imports (after the existing `from app.services.memory.store import memory_store`):

```python
from app.services.memory.embedder import cosine, embed_texts
```

Add a helper method inside `class MemoryExtractor` (before `extract_and_upsert`):

```python
    async def _maybe_embed(
        self, profile: Profile, texts: list[str]
    ) -> list[list[float] | None]:
        """Embed texts when an embed_model is configured; else all None. Best-effort."""
        if not texts or not profile.memory.embed_model or not profile.llm.base_url:
            return [None] * len(texts)
        try:
            return await embed_texts(
                texts, profile.llm.base_url, profile.llm.api_key,
                profile.memory.embed_model,
            )
        except Exception as exc:  # noqa: BLE001 - embedding is best-effort
            logger.warning("memory embed failed: %s", exc)
            return [None] * len(texts)
```

Replace the dedup/add block in `extract_and_upsert` (the current lines building `existing` and the `for fact in facts:` loop) with:

```python
            existing_items = await memory_store.list(profile.name)
            existing_norm = {m["content"].strip().lower() for m in existing_items}
            existing_vecs = [
                m["embedding"] for m in existing_items if m.get("embedding")
            ]
            new_vecs = await self._maybe_embed(profile, facts)
            threshold = profile.memory.dedup_threshold
            added = 0
            for fact, vec in zip(facts, new_vecs):
                norm = fact.strip().lower()
                if norm in existing_norm:
                    continue
                if vec is not None and any(
                    cosine(vec, ev) >= threshold for ev in existing_vecs
                ):
                    continue
                await memory_store.add(
                    profile.name, fact, source_session_id=session_id, embedding=vec
                )
                existing_norm.add(norm)
                if vec is not None:
                    existing_vecs.append(vec)
                added += 1
            if added:
                logger.info("memory: added %d facts for profile %s", added, profile.name)
            return added
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_memory_extractor.py -v`
Expected: PASS (new tests plus the existing `test_extract_and_upsert_dedupes`, which uses no `embed_model` and still dedupes case-insensitively).

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/memory/extractor.py tests/unit/test_memory_extractor.py
git commit -m "feat(memory): compute embeddings at add-time and cosine-dedupe new facts

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Compactor — rebuild the structured profile & prune folded facts

**Files:**
- Create: `apps/api_gateway/app/services/memory/compactor.py`
- Modify: `apps/api_gateway/app/services/memory/extractor.py` (call `memory_compactor.maybe_compact` at end of `extract_and_upsert`)
- Test: `tests/unit/test_memory_compactor.py` (new)

**Interfaces:**
- Consumes: `memory_store.list`, `memory_store.delete_many`, `profile_doc_store.get/upsert`, `Profile`.
- Produces:
  - `memory_compactor.maybe_compact(profile) -> bool` (compacts iff buffer >= `compaction_threshold` or >= `max_facts`)
  - `memory_compactor.compact(profile, items=None) -> bool`
  - module attr `app.services.memory.compactor.memory_compactor`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_memory_compactor.py`:

```python
import pytest

from app.services.memory.compactor import MemoryCompactor
from app.services.memory.store import memory_store, profile_doc_store
from app.services.profiles.models import Profile


def _profile(**mem):
    return Profile(name="pet", llm={"base_url": "http://llm.local/v1", "model": "m"},
                   memory=mem)


@pytest.mark.asyncio
async def test_maybe_compact_below_threshold_noop(monkeypatch):
    await memory_store.add("pet", "a")
    called = {"n": 0}

    async def fake_call(self, profile, current_doc, facts):
        called["n"] += 1
        return "## User Profile\n- x"

    monkeypatch.setattr(MemoryCompactor, "_call_llm", fake_call)
    did = await MemoryCompactor().maybe_compact(_profile(compaction_threshold=5))
    assert did is False
    assert called["n"] == 0
    assert len(await memory_store.list("pet")) == 1


@pytest.mark.asyncio
async def test_compact_rebuilds_doc_and_prunes(monkeypatch):
    ids = [(await memory_store.add("pet", f"fact {i}"))["id"] for i in range(3)]

    async def fake_call(self, profile, current_doc, facts):
        assert "fact 0" in "\n".join(facts)
        return "## User Profile\n### Danh tính\n- merged"

    monkeypatch.setattr(MemoryCompactor, "_call_llm", fake_call)
    did = await MemoryCompactor().maybe_compact(_profile(compaction_threshold=3))
    assert did is True
    doc = await profile_doc_store.get("pet")
    assert doc["content"] == "## User Profile\n### Danh tính\n- merged"
    assert await memory_store.list("pet") == []  # folded facts pruned
    _ = ids


@pytest.mark.asyncio
async def test_compact_preserves_facts_added_after_snapshot(monkeypatch):
    for i in range(3):
        await memory_store.add("pet", f"old {i}")

    async def fake_call(self, profile, current_doc, facts):
        # simulate a concurrent add landing during the LLM call
        await memory_store.add("pet", "new-arrival")
        return "## User Profile\n- merged"

    monkeypatch.setattr(MemoryCompactor, "_call_llm", fake_call)
    await MemoryCompactor().maybe_compact(_profile(compaction_threshold=3))
    remaining = {m["content"] for m in await memory_store.list("pet")}
    assert remaining == {"new-arrival"}


@pytest.mark.asyncio
async def test_compact_llm_failure_keeps_facts(monkeypatch):
    await memory_store.add("pet", "a")
    await memory_store.add("pet", "b")
    await memory_store.add("pet", "c")

    async def boom(self, profile, current_doc, facts):
        raise RuntimeError("llm down")

    monkeypatch.setattr(MemoryCompactor, "_call_llm", boom)
    did = await MemoryCompactor().maybe_compact(_profile(compaction_threshold=3))
    assert did is False
    assert len(await memory_store.list("pet")) == 3  # no data loss
    assert await profile_doc_store.get("pet") is None


@pytest.mark.asyncio
async def test_compact_empty_doc_keeps_facts(monkeypatch):
    await memory_store.add("pet", "a")
    await memory_store.add("pet", "b")
    await memory_store.add("pet", "c")

    async def empty(self, profile, current_doc, facts):
        return "   "

    monkeypatch.setattr(MemoryCompactor, "_call_llm", empty)
    did = await MemoryCompactor().maybe_compact(_profile(compaction_threshold=3))
    assert did is False
    assert len(await memory_store.list("pet")) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_memory_compactor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.memory.compactor'`.

- [ ] **Step 3: Create the compactor**

Create `apps/api_gateway/app/services/memory/compactor.py`:

```python
"""Threshold-triggered compaction of raw memory facts into a structured
user-profile document.

One LLM call rebuilds the profile from (current doc + all buffered facts);
the folded facts are then pruned so the buffer stays small. Best-effort: any
failure is logged and swallowed, and facts are pruned only after the doc write
succeeds and only for the ids captured at compaction start (concurrently added
facts survive).
"""

from __future__ import annotations

import logging

import httpx

from app.core.settings import settings
from app.services.memory.store import memory_store, profile_doc_store
from app.services.profiles.models import Profile

logger = logging.getLogger(__name__)

COMPACTION_PROMPT = (
    "You maintain a compact profile of a user, written in the user's language. "
    "You are given the CURRENT PROFILE (may be empty) and a list of NEW FACTS. "
    "Return the updated profile as Markdown, starting with '## User Profile' and "
    "using these sub-sections, omitting any that would be empty:\n"
    "### Danh tính\n### Dự án\n### Sở thích\n### Ràng buộc\n### Quan hệ\n"
    "Merge duplicates, keep bullets short, and when two facts conflict prefer the "
    "more recent one (facts are listed oldest first). Return ONLY the Markdown."
)


class MemoryCompactor:
    def _model(self, profile: Profile) -> str:
        return profile.memory.extractor_model or profile.llm.model

    async def _call_llm(
        self, profile: Profile, current_doc: str, facts: list[str]
    ) -> str:
        prompt = (
            "CURRENT PROFILE:\n"
            + (current_doc or "(empty)")
            + "\n\nNEW FACTS (oldest first):\n"
            + "\n".join(f"- {f}" for f in facts)
        )
        headers = (
            {"Authorization": f"Bearer {profile.llm.api_key}"}
            if profile.llm.api_key
            else {}
        )
        async with httpx.AsyncClient(
            timeout=settings.conversation_llm_timeout_seconds
        ) as client:
            resp = await client.post(
                f"{profile.llm.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": self._model(profile),
                    "messages": [
                        {"role": "system", "content": COMPACTION_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        return str(content).strip()

    async def maybe_compact(self, profile: Profile) -> bool:
        """Compact iff the raw buffer has reached the profile's threshold."""
        try:
            if not profile.memory.enabled or not profile.llm.base_url:
                return False
            items = await memory_store.list(profile.name)
            threshold = max(1, profile.memory.compaction_threshold)
            if len(items) < threshold and len(items) < profile.memory.max_facts:
                return False
            return await self.compact(profile, items)
        except Exception as exc:  # noqa: BLE001 - compaction is best-effort
            logger.warning("maybe_compact failed for %s: %s", profile.name, exc)
            return False

    async def compact(self, profile: Profile, items: list[dict] | None = None) -> bool:
        if items is None:
            items = await memory_store.list(profile.name)
        if not items:
            return False
        # oldest first so the LLM can honor "prefer the more recent fact"
        items = sorted(items, key=lambda i: (i["created_at"] or "", i["id"]))
        fact_ids = [i["id"] for i in items]
        facts = [i["content"] for i in items]
        current = await profile_doc_store.get(profile.name)
        current_doc = current["content"] if current else ""
        new_doc = await self._call_llm(profile, current_doc, facts)
        if not new_doc:
            logger.warning(
                "compaction produced empty doc for %s; keeping facts", profile.name
            )
            return False
        await profile_doc_store.upsert(profile.name, new_doc)
        await memory_store.delete_many(fact_ids)
        logger.info(
            "memory: compacted %d facts into profile %s", len(fact_ids), profile.name
        )
        return True


memory_compactor = MemoryCompactor()
```

- [ ] **Step 4: Wire the trigger into the extractor**

In `apps/api_gateway/app/services/memory/extractor.py`, add to imports:

```python
from app.services.memory.compactor import memory_compactor
```

In `extract_and_upsert`, change the final `return added` (added in Task 2) to run compaction first:

```python
            if added:
                logger.info("memory: added %d facts for profile %s", added, profile.name)
            await memory_compactor.maybe_compact(profile)
            return added
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_memory_compactor.py ../../tests/unit/test_memory_extractor.py -v`
Expected: PASS. (Existing extractor tests still pass: their thresholds stay at the default 20, so `maybe_compact` is a no-op and makes no LLM call.)

- [ ] **Step 6: Commit**

```bash
git add apps/api_gateway/app/services/memory/compactor.py apps/api_gateway/app/services/memory/extractor.py tests/unit/test_memory_compactor.py
git commit -m "feat(memory): threshold-triggered compaction into a structured user profile

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Retriever — inject the profile doc + remaining buffer

**Files:**
- Modify: `apps/api_gateway/app/services/memory/retriever.py`
- Test: `tests/unit/test_memory_retriever.py` (update one existing test, add new ones)

**Interfaces:**
- Consumes: `profile_doc_store.get`, `memory_store.list`, `_semantic_filter` (unchanged).
- Produces: `MemoryRetriever.get_context(profile, query="") -> str` — returns the profile-doc block, then a `## Recent notes` block of remaining buffer facts, joined by a blank line, truncated to `MAX_CHARS`. Empty string when nothing to inject.

- [ ] **Step 1: Update/write the tests**

In `tests/unit/test_memory_retriever.py`, **replace** `test_get_context_all_mode` with:

```python
@pytest.mark.asyncio
async def test_get_context_buffer_only():
    await memory_store.add("pet", "likes tea")
    await memory_store.add("pet", "from Hanoi")
    profile = Profile(name="pet")
    block = await MemoryRetriever().get_context(profile)
    assert "## Recent notes" in block
    assert "- likes tea" in block and "- from Hanoi" in block


@pytest.mark.asyncio
async def test_get_context_includes_profile_doc():
    from app.services.memory.store import profile_doc_store

    await profile_doc_store.upsert("pet", "## User Profile\n### Danh tính\n- Toan")
    await memory_store.add("pet", "just mentioned guitar")
    profile = Profile(name="pet")
    block = await MemoryRetriever().get_context(profile)
    assert block.startswith("## User Profile")
    assert "- Toan" in block
    assert "## Recent notes" in block
    assert "- just mentioned guitar" in block


@pytest.mark.asyncio
async def test_get_context_doc_only_no_buffer():
    from app.services.memory.store import profile_doc_store

    await profile_doc_store.upsert("pet", "## User Profile\n- Toan")
    block = await MemoryRetriever().get_context(Profile(name="pet"))
    assert block == "## User Profile\n- Toan"


@pytest.mark.asyncio
async def test_get_context_truncates_to_max_chars():
    from app.services.memory import retriever as r

    for i in range(200):
        await memory_store.add("pet", f"fact number {i} " + "x" * 20)
    block = await MemoryRetriever().get_context(Profile(name="pet"))
    assert len(block) <= r.MAX_CHARS + len("## Recent notes\n")
```

Keep `test_get_context_empty_cases`, `test_semantic_mode_top_k`, and `test_semantic_falls_back_when_no_embeddings` as-is — they use no profile doc, so their buffer-only assertions (`"- likes tea" in block`, `"- no vector here" in block`, the fallback warning) still hold under the new format.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_memory_retriever.py -v`
Expected: FAIL — new tests fail (no profile-doc injection; old `## User Memories` header).

- [ ] **Step 3: Rewrite `get_context`**

In `apps/api_gateway/app/services/memory/retriever.py`, update the store import:

```python
from app.services.memory.store import memory_store, profile_doc_store
```

Replace the `get_context` method with:

```python
    async def get_context(self, profile: Profile | None, query: str = "") -> str:
        if profile is None or not profile.memory.enabled:
            return ""
        doc = await profile_doc_store.get(profile.name)
        doc_block = doc["content"].strip() if doc and doc["content"] else ""
        items = await memory_store.list(profile.name)
        if profile.memory.mode == "semantic" and query and items:
            items = await self._semantic_filter(items, query, profile)
        buffer_lines: list[str] = []
        total = len(doc_block)
        for item in items[:MAX_ITEMS]:
            content = item["content"]
            if total + len(content) > MAX_CHARS:
                break
            buffer_lines.append(f"- {content}")
            total += len(content)
        parts: list[str] = []
        if doc_block:
            parts.append(doc_block)
        if buffer_lines:
            parts.append("## Recent notes\n" + "\n".join(buffer_lines))
        return "\n\n".join(parts)
```

(`_semantic_filter`, `inject_memories`, `MAX_ITEMS`, `MAX_CHARS` are unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_memory_retriever.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/services/memory/retriever.py tests/unit/test_memory_retriever.py
git commit -m "feat(memory): inject structured profile doc plus remaining buffer

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: End-to-end integration + full suite

**Files:**
- Test: `tests/unit/test_memory_compaction_e2e.py` (new)

**Interfaces:**
- Consumes: everything above. No production code changes expected; if a test surfaces a defect, fix the relevant module and note it in the commit.

- [ ] **Step 1: Write the end-to-end test (no `embed_model` path)**

Create `tests/unit/test_memory_compaction_e2e.py`:

```python
import pytest

from app.services.memory.compactor import MemoryCompactor
from app.services.memory.extractor import MemoryExtractor
from app.services.memory.store import memory_store, profile_doc_store
from app.services.history.store import session_store
from app.services.profiles.models import Profile


@pytest.mark.asyncio
async def test_extract_then_compact_without_embed_model(monkeypatch):
    await session_store.create("e2e", profile_id="u")
    await session_store.append_message("e2e", 1, "user", "hello")
    await session_store.append_message("e2e", 1, "assistant", "hi")

    async def fake_extract(self, messages, base_url, api_key, model):
        return ["User is Toan", "User builds an ESP32 assistant", "User speaks Vietnamese"]

    async def fake_call(self, profile, current_doc, facts):
        assert "User is Toan" in "\n".join(facts)
        return "## User Profile\n### Danh tính\n- Toan, speaks Vietnamese\n### Dự án\n- ESP32 assistant"

    monkeypatch.setattr(MemoryExtractor, "extract", fake_extract)
    monkeypatch.setattr(MemoryCompactor, "_call_llm", fake_call)

    profile = Profile(
        name="u",
        llm={"base_url": "http://llm.local/v1", "model": "m"},
        memory={"compaction_threshold": 3},  # no embed_model
    )
    added = await MemoryExtractor().extract_and_upsert("e2e", profile)
    assert added == 3
    # buffer hit the threshold -> compacted and pruned
    assert await memory_store.list("u") == []
    doc = await profile_doc_store.get("u")
    assert doc["content"].startswith("## User Profile")
    assert "Toan" in doc["content"]
```

- [ ] **Step 2: Run the e2e test**

Run: `.venv/bin/pytest tests/unit/test_memory_compaction_e2e.py -v`
Expected: PASS.

- [ ] **Step 3: Run the full memory suite + broader unit suite**

Run: `.venv/bin/pytest tests/unit -k memory -v && python -m pytest ../../tests/unit -q`
Expected: All memory tests PASS; the wider suite shows no new failures (the only pre-existing known failure is `tests/unit/test_conversation_engine_ready.py::test_session_started_reports_ready_when_already_warm`, unrelated to this work — confirm it is the *only* red).

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_memory_compaction_e2e.py
git commit -m "test(memory): end-to-end extract->compact without embed_model

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- (A) embeddings at add-time → Task 2. ✓
- (B) consolidation/merge/contradictions → Task 3 (compaction LLM) + Task 2 (cosine dedup). ✓
- (C) cap / bounded store / no more unfiltered-recent-50 → Task 3 (prune) + Task 4 (doc+small buffer). ✓
- Compaction → structured user-identity doc → Task 1 (model/store) + Task 3. ✓
- Works without `embed_model` → Task 2 fallback + Task 5 e2e. ✓
- Teardown safety / no data loss → Task 3 tests (`llm_failure_keeps_facts`, `empty_doc_keeps_facts`, `preserves_facts_added_after_snapshot`). ✓
- New config fields → Task 1. ✓

**Placeholder scan:** none — every code/test step contains full content.

**Type consistency:** `profile_doc_store.get/upsert/delete`, `memory_store.delete_many`, `memory_compactor.maybe_compact/compact`, `MemoryExtractor._maybe_embed`, and `MemoryConfig.{compaction_threshold,max_facts,dedup_threshold}` are used consistently across tasks; `MemoryProfileDoc` PK is `profile_id` throughout; retriever consumes `profile_doc_store` defined in Task 1.
