# Shared Profiles Become Clone-Only Templates — Design

Date: 2026-08-14

## Problem

A `Profile` row with `owner_id is None` is what this codebase calls a
"template" (`app/services/profile_visibility.py`). Today a template is
*visible to everyone and fully usable by everyone*: any caller can name it in
`?profile=`, run a conversation on it, and bind a device to it.

That conflates two different things:

1. **Who may read it** — a template is a published example, so everyone.
2. **Who may run on it** — today, also everyone, which is wrong. A shared
   profile is meant to be a starting point that a user copies and then owns,
   not a live configuration many unrelated users and devices run against.

Worse, `owner_id is None` currently doubles as "created by an admin"
(`profiles.py:139`, `owner_id = None if is_admin else current_user_id(...)`).
An admin cannot create a profile for their *own* use — every profile they
create is a template. So the two concepts cannot even be distinguished today.

Goal: a shared profile is a **clone-only template**. It may be read and
cloned; it may not be used in a conversation, and no device may be bound to
it. Only an admin may create, edit, delete, or mark a profile as shared.

## Scope

In scope:

- `Profile` (`/v1/profiles`) — the assistant row that carries `stt` + `llm` +
  `tts` + `mcp_servers`.
- `app/services/profile_visibility.py` — the shared predicate module.
- The six consumers that resolve a client-supplied profile name:
  `conversation.py` (HTTP `/chat` and the WS route), `lugo.py`, `stt.py`
  (`/stt/warm`), `services/conversation/session.py`, and `devices.py` (the
  bind choke point).
- The admin static console: `static/js/profiles.js`, `static/js/devices.js`.
- A one-time, idempotent startup migration for existing `owner_id is None`
  rows.

Out of scope (confirmed with user):

- **TtsProfile** (`/v1/tts/profiles`) and **McpServer** (`/v1/mcp/servers`).
  Both have their own `owner_id`/template concept and their own clone route;
  both keep today's behavior. A `TtsProfile` carries no secret (engine, voice,
  speed — see `services/tts/profile_models.py`), and making it clone-only
  would force a cascade: cloning a shared `Profile` whose `tts.profile_name`
  points at a shared `TtsProfile` would have to clone that row too.
- `_can_write` in `profiles.py:109`. Admin-only write on templates already
  exists and is already correct; this design only extends it to the new flag.

## The model

Add one field:

```python
class Profile(BaseModel):
    ...
    shared: bool = False
```

Persisted inside the existing `config_profiles.data` JSON blob — **no DDL
change**.

Today there are two rules. There will be three:

| Predicate | Rule | Applied at |
|---|---|---|
| `visible` | `shared or owner_id == caller` | `GET /v1/profiles`, `GET /{name}`, clone source, `GET /{name}/health` |
| `usable` | `not shared and owner_id == caller` | WS conversation, WS lugo, HTTP `/chat`, `/stt/warm`, session resume, **device bind** |
| `writable` | `shared` → `role == "admin"`; else `owner_id == caller` | `PUT`, `DELETE` |

The load-bearing consequence: a shared profile **keeps** the `owner_id` of the
admin who created it, but `usable` requires `not shared`, so *even that admin*
must clone it before using it. That is the literal requirement — a shared
profile exists only to be cloned.

### Invariant after migration

`owner_id is None` implies `shared is True`.

A row that is neither shared nor owned would be invisible and unusable to
everyone, so the migration must not create one. The single exception is
dev mode (`settings.auth_enabled` false), where `current_user_id()` is `None`
for every caller: a row created there gets `owner_id=None, shared=False`, and
`owner_id == caller` is `None == None` → true, so dev mode keeps working
exactly as it does now. In a real auth-enabled deployment the HTTP CRUD
routes always run behind a resolved session, so `owner_id` is never `None`
for a newly created row.

## Backend changes

### `app/services/profile_visibility.py`

Add alongside the existing `profile_visible` / `visible_profile_or_none`:

```python
def profile_usable(profile, caller_id) -> bool:
    return not profile.shared and profile.owner_id == caller_id

def usable_profile_or_none(profile, caller_id, *, bypass=False): ...
```

`bypass=True` keeps its current meaning and its current single legitimate
caller (`WsIdentity.unauthenticated`, dev mode only) — see that module's
existing docstring. `visible_profile_or_none` stays, unchanged, for the read
paths.

Also add a tiny public helper so call sites can produce an honest message:

```python
def is_shared_template(profile) -> bool:  # profile may be None
    return profile is not None and profile.shared
```

### Error messages: shared rows may be named explicitly

`profile_visibility.py`'s existing contract says "doesn't exist" and "belongs
to someone else" must stay indistinguishable, so the rejection does not become
a profile-name enumeration oracle. **That contract does not apply to shared
rows**: a shared profile is listed to every caller by `GET /v1/profiles`
already, so naming it in an error leaks nothing.

So the split is:

- profile is shared → say so plainly: `"profile 'x' is a shared template;
  clone it before using it"`.
- profile is missing *or* owned by someone else → the existing
  indistinguishable message, unchanged.

### `app/api/routes/profiles.py`

- `create_profile`: `owner_id = current_user_id(request)` for **everyone** —
  drop the `None if is_admin` branch. `shared` is accepted from the payload
  only when `current_role(request) == "admin"`; for a non-admin it is silently
  forced to `False`, the same "the field just doesn't take" pattern
  `mcp_servers` already uses (`profiles.py:150`) so the editor UI needs no new
  error path.
- `update_profile`: on the update branch, `shared` is taken from the payload
  only for an admin, otherwise preserved from `existing`. On the
  create-branch of `PUT` (upsert-or-create) it follows `create_profile`'s rule.
- `clone_profile`: the clone is always `shared=False`, `owner_id=caller`
  (the existing `mcp_servers` drop for non-admins is unchanged).
- **New guard**: a `PUT` that flips `shared` from `False` to `True` on a
  profile that still has devices bound to it returns **409** naming those
  devices. Otherwise the admin would create the ambiguous state this design
  exists to prevent (a device bound to a profile it may not run), and the
  device would silently degrade to server defaults on its next connect. The
  admin reassigns the devices first, then shares.
  `delete_profile` already sweeps bindings via `device_store.clear_profile`;
  this is the same concern on the other mutation.

### `app/schemas/profiles.py`

`ProfileRequest` gains `shared: bool = False`.

### Consumers

Switch `visible_profile_or_none` → `usable_profile_or_none` at:

- `conversation.py:172` (HTTP `/chat`)
- `conversation.py:354` (WS connect)
- `lugo.py:105` (`_resolve`)
- `stt.py:152` (`/stt/warm`)
- `services/conversation/session.py:331` (the real C2 choke point)
- `devices.py:41` (`_checked_profile_name`, the bind choke point)

Every `visible_tts_profile_or_none` call stays exactly as it is.

Behavior on rejection follows each site's **existing** shape — this design
does not introduce a new failure mode:

- The WS paths and `/chat` already fall back to server defaults with a
  warning when a profile does not resolve. A shared profile takes that same
  path, with the shared-specific warning text above. Falling back (rather
  than refusing the connection) satisfies "not usable in a conversation" —
  the conversation runs, but never on the shared profile — and avoids turning
  a name typo into a dropped connection.
- `/stt/warm` already falls through to the server default engine.
- `devices.py` is the exception: it is a deliberate action, not a fallback, so
  it returns **400** with the shared-specific message rather than silently
  binding nothing.

### `check_profile_health` / `GET /{name}/health`

Both keep `visible`. The health badge is a read — the admin console shows it
next to a shared profile in the list, and the WS connect gate has already
filtered through `usable` before it gets there. Covered by an explicit test so
the distinction is not re-litigated later.

## Migration

New module `app/services/profiles/shared_migration.py`, exposing
`async def migrate_ownerless_profiles() -> None`, called from `main.py`'s
lifespan next to the existing `migrate_*` calls (`main.py:133-152`), which is
the established precedent for idempotent boot migrations
(`services/model_registry/seed.py`, `services/memory/subject_migration.py`).

For each profile row with `owner_id is None`:

| Devices bound to that name | Result |
|---|---|
| exactly one distinct owner | `owner_id = that owner`, `shared = False` |
| none | `shared = True`, `owner_id` stays `None` |
| two or more distinct owners | `shared = True`, `owner_id` stays `None`, log a WARNING listing the device ids that need reassignment |

Rationale for the first row: an already-deployed device bound to a template
must keep working. Handing the profile to the device's owner preserves the
fleet exactly as it runs today, which is the whole reason this branch is
preferred over the simpler "mark everything shared".

Idempotency falls out of the invariant: after one run, every `owner_id is
None` row also has `shared=True`, and the migration only rewrites rows where
`owner_id is None and shared is False`.

Effect on the current local database (`data/app.db`): `esp32-assistant` and
`bound-profile` go to their bound device's owner; `rpi-assistant`, `dev`,
`fast`, and `host` become shared.

## Admin console

`static/js/profiles.js`:

- `isTemplate` (line 267) changes from `p.owner_id === null` to `p.shared`.
- A shared row shows a "Shared" badge and offers **Clone** only; an admin
  additionally keeps Edit/Delete, and the editor gains a `shared` checkbox
  visible to admins only.
- The two profile dropdowns (`renderProfileSelect` line 110,
  `renderLivehostProfileSelect` line 129) **filter shared rows out** — they
  pick what a conversation runs on.

`static/js/devices.js`: the three profile pickers (lines 32, 46, 72 — pair
form, reassign form, per-device select) filter shared rows out for the same
reason.

The existing `(mine)` suffix, which today keys off `owner_id` being truthy,
is now redundant with the filtering in the pickers and is dropped there; in
the profile *list* it is replaced by the "Shared" badge.

## Testing

- **Predicates** — a table over {shared, owned-by-caller, owned-by-other,
  ownerless} × {visible, usable, writable}, including the dev-mode
  `None == None` case and `bypass=True`.
- **Routes** — admin create yields `owner_id=self, shared=False`; non-admin
  cannot set `shared` on create or update; admin update can; clone of a shared
  row yields `shared=False, owner_id=caller`; sharing a profile with bound
  devices 409s and names them.
- **Consumers** — one test per site (WS conversation, WS lugo, HTTP `/chat`,
  `/stt/warm`, session resume, device bind) proving a shared profile is
  rejected with the shared-specific message, and that a private profile
  belonging to someone else still produces the *old, indistinguishable*
  message. The second half matters: it is the regression guard on the C2
  no-oracle contract.
- **Health** — `GET /{name}/health` still answers for a shared profile.
- **Migration** — the three branches, plus a second run proving idempotency,
  plus the multi-owner WARNING.
- **Static UI** — jsdom, following the existing `tests/unit/**/test_static_*.py`
  pattern: shared row renders Clone-only for a non-admin, and shared names are
  absent from both dropdowns and the device pickers.

## Deployment note

The migration runs on boot and rewrites profile rows. `data/app.db` should be
backed up before the first deploy carrying this change, matching the
convention already visible in `data/` (`app.db.bak-*`).
