"""Validation gate: a (kind, engine, model_id) choice is only restricted if an
admin has explicitly catalogued it in the model registry. No matching entry ->
unrestricted, preserving today's bring-your-own-endpoint flexibility for
anything not curated (e.g. a fully custom self-hosted LLM).

`user` may be None (route ran with no resolved acting user -- only possible
when settings.admin_password is unset, dev mode; see app.core.actor). The
`enabled` check still applies unconditionally; the `can_use_testing` check
fails closed (blocks) when there's no real user to check it against, since
that's the safer default for a permission question with no identity behind
it."""

from __future__ import annotations

from app.core.errors import ModelNotAllowedError
from app.services.db.models import User
from app.services.model_registry.store import model_registry_store


async def check_model_allowed(kind: str, engine: str, model_id: str, user: User | None) -> None:
    if not engine or not model_id:
        return
    entry = await model_registry_store.find(kind, engine, model_id)
    if entry is None:
        return
    if not entry["enabled"]:
        raise ModelNotAllowedError(f"{kind} model '{engine}/{model_id}' is currently disabled")
    if entry["stage"] == "testing" and not (user and user.can_use_testing):
        raise ModelNotAllowedError(
            f"{kind} model '{engine}/{model_id}' is in testing and not enabled for your account"
        )
