"""The LLM a profile runs against, resolved once.

Three entry points used to build a responder from a profile this way -- the
HTTP /chat route, the livehost socket (before the livehost plugin left this
repo -- its own traffic now reaches ConversationSession.start() the same way
a browser's does, over /v1/conversation/stream), and ConversationSession.start()
-- and each spelled out the same four-step resolution: read
base_url/api_key/model off the profile, then let a Model Registry row for
(llm.engine, llm.model) override the endpoint and credentials, then take the
profile's system prompt. Three copies meant a change to the precedence rule
(which is where provider credentials come from) had to land three times to
be true everywhere.

The registry override is looked up here rather than at the call site because it
is part of the precedence rule, not a decoration on top of it.
"""

from __future__ import annotations

from typing import NamedTuple

from app.services.conversation.responder import resolve_llm_override_from_registry


class LlmConfig(NamedTuple):
    """All four are None when the profile leaves them unset -- build_responder_ex
    treats that as "fall back to the server-wide config"."""

    base_url: str | None
    api_key: str | None
    model: str | None
    system_prompt: str | None


async def resolve_llm_config(profile) -> LlmConfig:
    """What `profile` (which may be None) says to talk to.

    api_key is deliberately gated on `llm.base_url` being set, not on the key
    itself: a key stored against no endpoint belongs to nothing and must not
    leak into a call aimed at the server default.
    """
    base_url = (profile.llm.base_url or None) if (profile and profile.llm.base_url) else None
    api_key = profile.llm.api_key if (profile and profile.llm.base_url) else None
    model = (profile.llm.model or None) if (profile and profile.llm.model) else None
    if profile and profile.llm.engine and profile.llm.model:
        registry_override = await resolve_llm_override_from_registry(
            profile.llm.engine, profile.llm.model
        )
        if registry_override:
            base_url, api_key = registry_override
            model = profile.llm.model
    system_prompt = (profile.system_prompt or None) if (profile and profile.system_prompt) else None
    return LlmConfig(base_url=base_url, api_key=api_key, model=model, system_prompt=system_prompt)
