"""Shared LLM-turn usage recorder (Task 6 / A1 dedup).

Lifts the identical "responder.last_usage -> resolve_llm_pair -> record_usage"
best-effort usage row that used to be duplicated in the livehost plugin's own
``_record_llm_usage`` closure (back when it lived in this repo, as
``api/routes/livehost.py``) and ``services/conversation/session.py``'s
``_record_llm_usage`` method -- and, found later, twice more inside
``api/routes/conversation.py``'s ``/chat``, once per branch of its
tool-registry if/else. The livehost plugin's own traffic now reaches
session.py's copy over /v1/conversation/stream, rather than calling in
directly.
Byte-neutral: writes the same usage row, same fields, same fail-open contract
(metering must never break a turn).
"""

from __future__ import annotations

import logging

from app.services.usage.attribution import resolve_llm_pair
from app.services.usage.recorder import record_usage

logger = logging.getLogger(__name__)


async def record_llm_turn_usage(
    responder,
    *,
    identity_user_id: str | None,
    profile,
    profile_name: str | None,
    llm_model: str | None = None,
) -> None:
    """Best-effort usage row for the LLM call(s) in the turn just completed.

    Called AFTER the responder's reply_stream has been fully consumed (only
    then is `.last_usage` -- set as the stream reads its final SSE chunk --
    populated). Must never raise into the turn: record_usage itself already
    swallows its own errors, but building the args (profile may be None,
    last_usage may be None or missing keys) must not raise either.

    `llm_model` is an already-resolved override, same as
    turn_quota.llm_turn_quota_blocked's parameter of the same name -- pass
    None to resolve purely from `profile.llm.model`.
    """
    try:
        last_usage = getattr(responder, "last_usage", None) or {}
        prompt_tokens = last_usage.get("prompt_tokens")
        completion_tokens = last_usage.get("completion_tokens")
        native_amount = (prompt_tokens or 0) + (completion_tokens or 0)
        pinned_model = llm_model or ((profile.llm.model if profile else "") or "")
        engine, model_id = resolve_llm_pair(
            responder, (profile.llm.engine if profile else "") or "", pinned_model,
        )
        await record_usage(
            user_id=identity_user_id or "", profile_id=profile_name or "",
            kind="llm", engine=engine, model_id=model_id, unit="tokens",
            native_amount=native_amount, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        )
    except Exception as exc:  # noqa: BLE001 - metering must never break a turn
        logger.warning("llm usage metering failed: %s", exc)
