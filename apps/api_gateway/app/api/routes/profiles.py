from fastapi import APIRouter, HTTPException, Request

from app.core.actor import current_role, current_user_id
from app.core.errors import AppError
from app.schemas.common import CloneRequest
from app.schemas.profiles import ProfileRequest
from app.services.auth.devices import device_store
from app.services.auth.users import user_store
from app.services.model_registry.gate import check_model_allowed
from app.services.model_registry.store import model_registry_store
from app.services.profile_visibility import profile_visible
from app.services.profiles.models import Profile
from app.services.profiles.store import profile_store
from app.services.stt.model_catalog import STT_MODEL_CATALOGS

router = APIRouter(prefix="/v1/profiles", tags=["profiles"])


def _mask(profile: Profile) -> dict:
    data = profile.model_dump()
    if data.get("llm", {}).get("api_key"):
        data["llm"]["api_key"] = "***"
    return data


async def _label_for(kind: str, engine: str, model_id: str) -> str:
    if not engine:
        return ""
    entry = await model_registry_store.find(kind, engine, model_id or "")
    if entry and entry.get("label"):
        return entry["label"]
    return f"{engine}/{model_id}" if model_id else engine


async def _with_labels(profile: Profile) -> dict:
    """_mask() + resolved friendly labels (stt_label/llm_label/tts_label) for what
    a session would actually use -- the profile's pin, or the server default marked
    "(default)". Additive: raw stt/llm/tts stay (the profile editors read them)."""
    from app.services.stt.model_catalog import resolve_default_stt_model
    from app.services.system_config import system_config_store

    data = _mask(profile)
    eng = system_config_store.get().engines

    stt_label = await _label_for("stt", profile.stt.engine, profile.stt.model)
    if not stt_label:
        base = await _label_for("stt", eng.default_stt_engine, resolve_default_stt_model(eng.default_stt_engine) or "")
        stt_label = f"{base} (default)" if base else "server default"

    llm_label = await _label_for("llm", profile.llm.engine, profile.llm.model)
    if not llm_label:
        d = await model_registry_store.find_default("llm")
        if d:
            base = d.get("label") or (f'{d["engine"]}/{d["model_id"]}' if d.get("model_id") else d["engine"])
            llm_label = f"{base} (default)"
        else:
            llm_label = "server default"

    tts_label = profile.tts.profile_name or (
        f"{eng.default_tts_engine} (default)" if eng.default_tts_engine else "server default"
    )

    data["stt_label"], data["llm_label"], data["tts_label"] = stt_label, llm_label, tts_label
    return data


async def _validate_profile_models(profile: Profile, acting_user) -> None:
    if profile.stt.model:
        # Validation scope is intentionally narrowed to explicit stt.engine:
        # profiles must be self-contained and not depend on mutable system-config
        # default_stt_engine, so a profile's validity is independent of
        # deploy-time config.
        engine = profile.stt.engine
        if not engine:
            raise AppError("stt.model requires stt.engine")
        # STT_MODEL_CATALOGS only covers local engines with selectable
        # sizes/repos (whisper, qwen3_asr) -- it predates the Model Registry
        # options endpoint, which now also lists remote-engine models an
        # admin has registered (qwen3_asr_or, whisper_or, http_stt).
        # Engines with no variant catalog have nothing to shape-validate
        # here; check_model_allowed is the real, generic gate.
        registry = STT_MODEL_CATALOGS.get(engine)
        if registry is not None:
            registry.validate(profile.stt.model)
        await check_model_allowed("stt", engine, profile.stt.model, acting_user)
    if profile.llm.engine and profile.llm.model:
        await check_model_allowed("llm", profile.llm.engine, profile.llm.model, acting_user)


async def _resolve_acting_user(request: Request):
    """None when there's no real logged-in user to resolve (dev mode, auth
    fully disabled -- see app.core.actor.current_user_id). check_model_allowed
    already handles a None user by failing closed only on the testing-stage
    check, never crashing."""
    user_id = current_user_id(request)
    return await user_store.get_by_id(user_id) if user_id else None


def _visible(profile: Profile, user_id: str | None) -> bool:
    # Delegates to the shared predicate (app/services/profile_visibility.py)
    # so every consumer of a ?profile= name -- not just this CRUD router --
    # applies the exact same owner_id rule. Kept as a thin wrapper (rather
    # than switching every call site here to profile_visible directly) so
    # memories.py's existing `from app.api.routes.profiles import _visible`
    # keeps working unchanged.
    return profile_visible(profile, user_id)


def _can_write(profile: Profile, user_id: str | None, role: str) -> bool:
    """Templates (owner_id is None) may only be modified/deleted by an admin;
    a user's own row may only be modified/deleted by that user. Distinct from
    _visible(), which governs read access -- everyone can SEE a template, but
    only an admin can WRITE to one."""
    if profile.owner_id is None:
        return role == "admin"
    return profile.owner_id == user_id


@router.get("")
async def list_profiles(request: Request) -> dict:
    user_id = current_user_id(request)
    profiles = profile_store.list()
    visible = {k: v for k, v in profiles.items() if _visible(v, user_id)}
    data = {}
    for k, v in visible.items():
        data[k] = await _with_labels(v)
    return {"success": True, "data": data}


@router.post("")
async def create_profile(payload: ProfileRequest, request: Request) -> dict:
    # exists(), not `get() is not None`: a name whose row failed to parse
    # (H4) still occupies it -- get() returns None for that name too, and
    # treating that as "free" would let this create silently overwrite the
    # row and hand its ownership to the caller.
    if profile_store.exists(payload.name):
        raise HTTPException(status_code=409, detail=f"'{payload.name}' already exists")
    is_admin = current_role(request) == "admin"
    # Ownership and sharing are independent now. This used to read
    # `None if is_admin else current_user_id(...)`, which meant an admin could
    # not own a profile at all -- every one they made was a template. See
    # docs/superpowers/specs/2026-08-14-shared-profile-clone-only-design.md.
    owner_id = current_user_id(request)
    data = payload.model_dump()
    if not is_admin:
        # C1: mcp_servers carries an arbitrary url+headers that
        # _build_tool_registry fetches and reflects into the LLM turn -- the
        # same SSRF-with-reflection primitive Task 6 gated on /v1/mcp/servers
        # for admins only. A non-admin setting it here would fully bypass that
        # gate. Silently drop rather than 403: doesn't need a special error path
        # in the profile editor UI -- the field just doesn't take.
        data["mcp_servers"] = []
        # Same "the field just doesn't take" treatment: publishing a template is
        # an admin act.
        data["shared"] = False
    profile = Profile(**data, owner_id=owner_id)
    acting_user = await _resolve_acting_user(request)
    await _validate_profile_models(profile, acting_user)
    profile_store.upsert(profile)
    return {"success": True, "data": await _with_labels(profile)}


@router.get("/{name}")
async def get_profile(name: str, request: Request) -> dict:
    profile = profile_store.get(name)
    if not profile or not _visible(profile, current_user_id(request)):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    return {"success": True, "data": await _with_labels(profile)}


@router.put("/{name}")
async def update_profile(name: str, payload: ProfileRequest, request: Request) -> dict:
    existing = profile_store.get(name)
    # H4: existing is None both when the name is genuinely free AND when its
    # row failed to parse. PUT is upsert-or-create, so falling through in the
    # latter case would skip _can_write entirely (nothing to check ownership
    # against) and silently overwrite the row via the create branch below --
    # must be rejected before that upsert-or-create logic ever runs.
    if existing is None and profile_store.exists(name):
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' exists but its stored data is unreadable; contact an admin",
        )
    # PUT is upsert-or-create (test_update_uses_path_name relies on creating via
    # PUT to a name that doesn't exist yet); ownership scoping only applies when
    # a row already exists and the caller is not authorized to write to it (not
    # merely able to see it -- see _can_write for the template/admin distinction).
    if existing and not _can_write(existing, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    is_admin = current_role(request) == "admin"
    data = payload.model_dump()
    data["name"] = name
    if existing:
        data["owner_id"] = existing.owner_id
        # Preserve the stored API key when the client sends a blank one: the UI's
        # password field is empty on edit ("leave blank to keep existing"), so a save
        # that doesn't re-enter the key must NOT wipe it.
        if not data.get("llm", {}).get("api_key") and existing.llm.api_key:
            data.setdefault("llm", {})["api_key"] = existing.llm.api_key
        if not is_admin:
            # C1: a non-admin PUT must not be able to add/change mcp_servers
            # (same SSRF-with-reflection gate as create_profile below). Silently
            # preserve whatever was already stored rather than 403.
            data["mcp_servers"] = [s.model_dump() for s in existing.mcp_servers]
            # Same for shared: only an admin publishes or withdraws a template.
            # Preserved, not forced False -- ProfileRequest.shared defaults to
            # False, so forcing would let any non-admin edit un-share a row.
            data["shared"] = existing.shared
        elif "shared" not in payload.model_fields_set:
            # DEVIATION from task-2-brief.md's literal code block: the brief only
            # special-cases the non-admin branch, but `shared: bool = False` gives
            # model_dump() no way to distinguish "omitted from the request" from
            # "explicitly set to False" -- both look like False. Without this, an
            # admin PUT from a client built before this feature (which never sends
            # `shared` at all) would silently un-share a template just by editing
            # an unrelated field. model_fields_set is pydantic's per-request
            # tracking of which fields were actually present in the payload, so
            # this only fires on true omission -- an explicit `"shared": false`
            # still flows through untouched (see test_admin_update_can_flip_shared_both_ways).
            data["shared"] = existing.shared
    else:
        data["owner_id"] = current_user_id(request)
        if not is_admin:
            data["mcp_servers"] = []
            data["shared"] = False
    profile = Profile(**data)
    acting_user = await _resolve_acting_user(request)
    await _validate_profile_models(profile, acting_user)
    if profile.shared and existing is not None and not existing.shared:
        # Sharing a profile that devices still point at would leave those
        # devices bound to something they may no longer run -- they would
        # silently fall back to server defaults on the next connect. Make the
        # admin reassign them first. delete_profile has the same concern and
        # solves it by sweeping (clear_profile); sharing must not silently
        # unassign someone's speaker, so it refuses instead.
        bound = [d["id"] for d in await device_store.list_all() if d["profile_id"] == name]
        if bound:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{name}' still has {len(bound)} device(s) bound to it "
                    f"({', '.join(bound)}); reassign them before sharing it"
                ),
            )
    profile_store.upsert(profile)
    return {"success": True, "data": await _with_labels(profile)}


@router.delete("/{name}")
async def delete_profile(name: str, request: Request) -> dict:
    existing = profile_store.get(name)
    # H4: distinguish "no such row" (404) from "row exists but is unreadable"
    # (409) -- both leave `existing` None, but only the former is a genuine
    # not-found.
    if existing is None and profile_store.exists(name):
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' exists but its stored data is unreadable; contact an admin",
        )
    if not existing or not _can_write(existing, current_user_id(request), current_role(request)):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    profile_store.delete(name)
    # Devices bound to this profile become "Unassigned" -- NOT revoked. The
    # pairing token is hardware identity and the profile is a soft setting, so
    # deleting an assistant must never cost the user a trip to the device to
    # re-pair it. Sweeping here also keeps devices.profile_id from dangling at a
    # name that no longer resolves: the WS connect paths (lugo.py,
    # conversation.py) now refuse an unbound device's connection rather than
    # tolerating it, and a dangling name would also show up in the UI as an
    # assistant that isn't there.
    unassigned = await device_store.clear_profile(name)
    return {
        "success": True,
        "data": {"name": name, "deleted": True, "devices_unassigned": unassigned},
    }


@router.post("/{name}/clone")
async def clone_profile(name: str, payload: CloneRequest, request: Request) -> dict:
    user_id = current_user_id(request)
    source = profile_store.get(name)
    if not source or not _visible(source, user_id):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
    # H4: same claim-a-free-name gap as create_profile -- exists(), not
    # `get() is not None`.
    if profile_store.exists(payload.new_name):
        raise HTTPException(status_code=409, detail=f"'{payload.new_name}' already exists")
    data = source.model_dump()
    data["name"] = payload.new_name
    data["owner_id"] = user_id
    # A clone is a working profile, never another template -- even when an admin
    # clones a shared row.
    data["shared"] = False
    if current_role(request) != "admin":
        # C1: cloning an admin template that has mcp_servers must not hand the
        # cloning user a live copy of servers they couldn't have set
        # themselves via create/update -- the clone is user-owned from here
        # on, so its mcp_servers must go through the same non-admin gate.
        data["mcp_servers"] = []
    clone = Profile(**data)
    # Clone copies stt/llm engine+model straight from the source without
    # going through create/update's model-registry gate -- without this, a
    # user denied a model could still get it by cloning an admin template
    # pinned to it. Same gate, same call shape as create_profile/update_profile.
    acting_user = await _resolve_acting_user(request)
    await _validate_profile_models(clone, acting_user)
    profile_store.upsert(clone)
    return {"success": True, "data": await _with_labels(clone)}


@router.get("/{name}/health")
async def profile_health(name: str, request: Request) -> dict:
    """Live health of the STT/TTS engines this profile would actually use.

    Same check the WS connect gate runs, exposed so the admin UI can show a
    profile as broken before a user tries to talk to it. Scoped like every
    sibling route (get_profile, clone_profile): 404 rather than leak that
    another user's private profile exists, or its resolved engine/base_url
    (EngineHealth.detail returns the base_url verbatim on both the success
    and failure paths).
    """
    from app.services.health import check_profile_health

    caller_id = current_user_id(request)
    profile = profile_store.get(name)
    if not profile or not _visible(profile, caller_id):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")

    return {"success": True, "data": (await check_profile_health(name, caller_id)).model_dump()}
