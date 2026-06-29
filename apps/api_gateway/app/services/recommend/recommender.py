"""Pure scoring/ranking for the model recommender.

`evaluate` turns one Candidate + detected Capabilities + installed-ids into a
ranked entry (status / fit_score / recommended / reason / action). `rank` evaluates
a list and sorts it. No I/O — the real catalog and installed state are wired in by
`service.py`, so this stays trivially testable.
"""

from dataclasses import dataclass

from app.services.recommend.capabilities import Capabilities

# Hardware capability that a chip class hard-requires (None = runs on plain CPU).
_CHIP_HW = {"apple_silicon": "apple_silicon", "nvidia_gpu": "cuda", "cpu": None}
_TIER_POINTS = {"high": 30, "medium": 15, "low": 5}
RECOMMEND_THRESHOLD = 60


@dataclass
class Candidate:
    category: str          # stt | tts | llm | vad
    id: str                # key the download endpoint expects
    engine: str            # provider/engine family
    label: str
    chip: str              # apple_silicon | cpu | nvidia_gpu
    tier: str              # high | medium | low
    vietnamese: bool
    size_gb: float | None
    size_estimate: str
    min_ram_gb: float | None
    requires: list         # capability/module flags resolved via caps.has()
    action: dict           # {kind, method?, path?, payload?, hint?}
    select: dict | None = None  # /select action to activate this model, if supported


def _score(c: Candidate, base: int, installed: bool) -> int:
    score = base + _TIER_POINTS.get(c.tier, 0)
    if c.vietnamese:
        score += 10
    if installed:
        score += 10
    return score


_CHIP_LABEL = {
    "apple_silicon": "Apple Silicon (MLX)",
    "nvidia_gpu": "NVIDIA GPU",
    "cpu": "CPU",
}


def _reason(c: Candidate, status: str, caps: Capabilities) -> str:
    if status.startswith("incompatible:"):
        what = status.split(":", 1)[1]
        if what == "ram":
            return f"Needs ~{c.min_ram_gb} GB RAM; only {caps.ram_total_gb} GB detected"
        return f"Requires {_CHIP_LABEL.get(c.chip, c.chip)}, not available on this host"
    if status.startswith("needs:"):
        what = status.split(":", 1)[1]
        if what == "disk":
            return f"Not enough free disk for {c.size_estimate} ({caps.disk_free_gb} GB free)"
        hint = c.action.get("hint")
        if hint:
            return hint
        return f"Install/enable '{what}' to run this on {_CHIP_LABEL.get(c.chip, c.chip)}"
    lang = "Vietnamese fine-tune; " if c.vietnamese else ""
    if status == "installed":
        return f"{lang}runs on {_CHIP_LABEL.get(c.chip, c.chip)}; already installed"
    return f"{lang}runs on {_CHIP_LABEL.get(c.chip, c.chip)} ({c.size_estimate})"


def evaluate(c: Candidate, caps: Capabilities, installed_ids: set, active_ids: set = frozenset()) -> dict:
    installed = c.id in installed_ids
    active = c.id in active_ids

    def out(status: str, fit: int, runnable: bool) -> dict:
        return {
            "category": c.category,
            "id": c.id,
            "engine": c.engine,
            "label": c.label,
            "chip": c.chip,
            "tier": c.tier,
            "size_estimate": c.size_estimate,
            "status": status,
            "fit_score": fit,
            "runnable": runnable,
            "recommended": runnable and fit >= RECOMMEND_THRESHOLD,
            "reason": _reason(c, status, caps),
            "action": c.action,
            "select": c.select,
            "active": active,
        }

    # 1. Hardware gate (chip class the host cannot satisfy → incompatible).
    hw = _CHIP_HW.get(c.chip)
    if hw and not caps.has(hw):
        return out(f"incompatible:{c.chip}", 0, False)

    # 2. RAM gate (only when RAM is known).
    if c.min_ram_gb and caps.ram_total_gb is not None and caps.ram_total_gb < c.min_ram_gb:
        return out("incompatible:ram", 0, False)

    # 3. Engine/runtime software the model needs (installable → "needs", not fatal).
    missing = [r for r in c.requires if not caps.has(r)]
    if missing:
        return out(f"needs:{missing[0]}", _score(c, 20, installed), False)

    # 4. Disk gate for a not-yet-downloaded model (only when free disk is known).
    if not installed and c.size_gb and caps.disk_free_gb is not None and caps.disk_free_gb < c.size_gb:
        return out("needs:disk", _score(c, 10, installed), False)

    status = "installed" if installed else "runnable"
    return out(status, _score(c, 50, installed), True)


def rank(candidates: list, caps: Capabilities, installed_ids: set, active_ids: set = frozenset()) -> list:
    evaluated = [evaluate(c, caps, installed_ids, active_ids) for c in candidates]
    evaluated.sort(key=lambda r: (-r["fit_score"], r["label"]))
    return evaluated
