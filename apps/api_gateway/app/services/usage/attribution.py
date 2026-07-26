"""Fill in the (engine, model_id) a usage row should be attributed to.

record_usage stores what the caller passed and prices the row by looking up
`find(kind, engine, model_id)`. Several call sites legitimately don't know the
model -- a REST /synthesize without model_id, a session whose profile pins no
LLM, a fast-path STT engine switch -- and used to record "". That blank is not
just an ugly "(none)" in the dashboard: it can never match the registry row
that carries the price, so those requests were structurally uncostable.

This module answers "which model actually served this?" from the same sources
the runtime used to pick it, and NEVER guesses: an engine with two candidate
models resolves to blank rather than to the wrong one.
"""

from __future__ import annotations

import logging

from app.services.stt.model_catalog import resolve_default_stt_model

logger = logging.getLogger(__name__)


def _pick_single(candidates: list[dict]) -> dict | None:
    """The one row these candidates unambiguously point at, else None.
    Enabled rows are preferred: a disabled row is a model the admin took out of
    service, so an enabled sibling is the better answer."""
    if not candidates:
        return None
    enabled = [c for c in candidates if c.get("enabled")]
    pool = enabled or candidates
    return pool[0] if len(pool) == 1 else None



# (kind, engine) pairs already reported as ambiguous. Attribution runs on every
# metered request, so the warning below would otherwise repeat per turn; the
# condition it reports is a registry misconfiguration that changes only when an
# admin edits the registry, so saying it once per process is enough.
_warned_ambiguous: set[tuple[str, str]] = set()


def _warn_ambiguous_once(kind: str, engine: str, candidates: list[dict]) -> None:
    """Explain why rows for this engine stay unattributed, and how to fix it.

    Two enabled rows for one (kind, engine) is the misconfiguration
    model_registry's find_enabled() docstring warns about: the provider picks
    one by an unspecified tie-break, so nothing can tell us afterwards which
    model actually ran. Recording either one would attribute cost to a price
    that may not be the one charged -- so the row stays blank and the admin
    gets told, instead of the dashboard quietly inventing an answer.
    """
    if (kind, engine) in _warned_ambiguous:
        return
    _warned_ambiguous.add((kind, engine))
    logger.warning(
        "usage attribution: %s/%s has %d candidate models (%s); usage rows for it "
        "stay unattributed until exactly one is enabled or the profile pins a model",
        kind, engine, len(candidates),
        ", ".join(sorted(c["model_id"] for c in candidates)),
    )


async def resolve_usage_model(kind: str, engine: str, model_id: str) -> tuple[str, str]:
    """(engine, model_id) for a usage row, with blanks filled where provable.

    Never raises: any lookup failure degrades to the inputs as given, because a
    usage row with imperfect attribution beats no usage row at all.
    """
    engine = engine or ""
    model_id = model_id or ""
    if engine and model_id:
        return engine, model_id

    from app.services.model_registry.store import model_registry_store

    try:
        if not model_id and kind == "stt" and engine:
            # The STT catalog is what the provider itself consults to decide
            # which weights to load, so it's the most accurate answer available.
            catalog_model = resolve_default_stt_model(engine)
            if catalog_model:
                return engine, catalog_model

        entries = await model_registry_store.list_all()
        # Sentinel rows (model_id == "") are engine config, never a model.
        real = [e for e in entries if e["kind"] == kind and e["model_id"]]

        if not model_id and engine:
            candidates = [e for e in real if e["engine"] == engine]
            match = _pick_single(candidates)
            if match:
                return engine, match["model_id"]
            if len(candidates) > 1:
                _warn_ambiguous_once(kind, engine, candidates)

        if not model_id and not engine and kind == "llm":
            # Same entry build_responder_ex() falls back to (responder.py's
            # _active_llm_entry), so this names the model that actually ran.
            default = await model_registry_store.find_default("llm")
            if default and default["enabled"] and default["model_id"]:
                return default["engine"], default["model_id"]

        if not engine and model_id:
            match = _pick_single([e for e in real if e["model_id"] == model_id])
            if match:
                return match["engine"], model_id
    except Exception as exc:  # noqa: BLE001 - attribution must never break metering
        logger.warning("usage attribution lookup failed (%s/%s/%s): %s", kind, engine, model_id, exc)

    return engine, model_id
