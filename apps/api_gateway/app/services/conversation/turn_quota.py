"""Shared LLM-turn quota preflight (Task 6 / A1 dedup).

Lifts the "resolve pinned (engine, model) -> provider_id -> quota_gate"
check that used to be duplicated, near byte-for-byte, in three places:
``api/routes/livehost.py``'s ``_quota_blocked_for``,
``services/conversation/session.py``'s ``_run_turn``, and
``api/routes/conversation.py``'s ``chat()``. All three resolve the same way
(only pair the profile's engine with a model the profile actually pinned --
see ``resolve_llm_pair``'s docstring, which applies the identical rule to the
usage row) and are fail-open the same way: only a genuine
``QuotaExceededError`` blocks a turn; any other failure (registry hiccup,
resolver error) logs and allows, matching ``quota_gate``'s own contract.

Two entry points:

* ``llm_turn_quota_blocked`` -- the normal call shape, given a ``Profile``
  (or None) plus an optional already-resolved model override.
* ``llm_turn_quota_blocked_for_pins`` -- the raw-pin variant, given an
  already-resolved (engine, model) pair rather than a ``Profile`` object.
  ``llm_turn_quota_blocked`` delegates here; ``api/routes/livehost.py`` keeps
  its own ``_quota_blocked_for`` wrapper (same signature as before this
  refactor) delegating here too, so its direct unit-test coverage
  (``test_livehost_quota_gate.py``) keeps working unchanged.
"""

from __future__ import annotations

import logging

from app.services.model_registry.store import model_registry_store
from app.services.quota.gate import QuotaExceededError
from app.services.usage.attribution import resolve_usage_model

logger = logging.getLogger(__name__)


async def llm_turn_quota_blocked(
    *,
    identity_user_id: str | None,
    profile,
    profile_name: str | None,
    llm_model: str | None = None,
    quota_gate=None,
) -> tuple[bool, str]:
    """(blocked, message) for one LLM turn, resolved off ``profile``'s pins.

    ``llm_model`` is an already-resolved override -- livehost's stream
    handler computes one from the profile + a registry lookup before the
    turn starts and passes it here; leave it None to resolve purely from
    ``profile.llm.model`` (session.py's turn loop, and the profile-based
    call shape generally).

    ``quota_gate``: pass explicitly to honor a caller MODULE's own
    monkeypatch of its ``quota_gate`` name (``api/routes/livehost.py`` keeps
    `quota_gate` bound at module scope and its tests reassign it directly --
    see ``test_livehost_quota_gate.py::test_livehost_quota_helper_fails_open``).
    Left None, this imports the live ``app.services.quota.gate.quota_gate``
    function-locally on every call (same as session.py/conversation.py
    already did), so a monkeypatch of THAT module's attribute is still
    observed.
    """
    pinned_model = llm_model or ((profile.llm.model if profile else "") or "")
    pinned_engine = ((profile.llm.engine if profile else "") or "") if pinned_model else ""
    return await llm_turn_quota_blocked_for_pins(
        user_id=identity_user_id, profile_name=profile_name,
        pinned_engine=pinned_engine, pinned_model=pinned_model,
        quota_gate=quota_gate,
    )


async def llm_turn_quota_blocked_for_pins(
    *,
    user_id: str | None,
    profile_name: str | None,
    pinned_engine: str,
    pinned_model: str,
    quota_gate=None,
) -> tuple[bool, str]:
    """Raw-pin variant of ``llm_turn_quota_blocked``: same check, given an
    already-resolved (engine, model) pin pair instead of a ``Profile``.

    Returns the message rather than raising so each turn path can report it
    the way that path reports its own failures (livehost: an `error` +
    `turn_done` event pair; session.py: an `error` event; conversation.py:
    an HTTPException(429) at its own call site).
    """
    try:
        pinned_model = pinned_model or ""
        pinned_engine = (pinned_engine or "") if pinned_model else ""
        usage_engine, usage_model = await resolve_usage_model("llm", pinned_engine, pinned_model)
        provider_id = ""
        try:
            entry = await model_registry_store.find("llm", usage_engine, usage_model)
            provider_id = (entry or {}).get("config", {}).get("provider_id", "") if entry else ""
        except Exception:  # noqa: BLE001 - a registry hiccup must never block a turn
            provider_id = ""
        gate = quota_gate
        if gate is None:
            from app.services.quota.gate import quota_gate as _default_quota_gate

            gate = _default_quota_gate
        await gate(
            user_id=user_id or "", provider_id=provider_id,
            kind="llm", engine=usage_engine, model_id=usage_model,
            profile_id=profile_name or "",
        )
    except QuotaExceededError as exc:
        return True, str(exc)
    except Exception as exc:  # noqa: BLE001 - fail-open, same as quota_gate itself
        logger.warning("llm turn quota check failed open: %s", exc)
    return False, ""
