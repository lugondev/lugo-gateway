"""`GET /agents-docs` — agent-facing documentation bundle.

Serves AGENTS.md plus all of docs/ concatenated as one markdown document, so a coding
agent can fetch the full project context (conventions + API + architecture + device
protocol) in a single request.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["docs"])

# Repo root: apps/api_gateway/app/api/routes/agents_docs.py -> parents[5]
_ROOT = Path(__file__).resolve().parents[5]
_FILES = [
    "AGENTS.md",
    "docs/api.md",
    "docs/architecture.md",
    "docs/runbook.md",
    "docs/device-integration.md",
]


def _bundle() -> str:
    parts: list[str] = []
    for rel in _FILES:
        path = _ROOT / rel
        if path.is_file():
            parts.append(f"\n\n<!-- ===================== {rel} ===================== -->\n\n")
            parts.append(path.read_text(encoding="utf-8"))
    return "".join(parts).strip() + "\n"


@router.get("/agents-docs", response_class=PlainTextResponse)
async def agents_docs() -> PlainTextResponse:
    return PlainTextResponse(_bundle(), media_type="text/markdown; charset=utf-8")
