# Profile-as-hub navigation and device↔profile pairing

Status: approved 2026-07-30. Spans `apps/api_gateway` and `lugo-web-client`.

## Problem

Three symptoms, one root cause.

1. **Nothing on the server binds a device to a profile.** `devices` (services/db/models.py)
   has no profile column. A device declares its own profile as a query param
   (`?profile=kitchen`) sourced from firmware/yaml config. So the control panel cannot
   answer the most obvious question a user has — *"what is this speaker running?"* — and
   changing a speaker's assistant means editing a config file on the device.

2. **The web UI exposes the data model, not the user's model.** `Profiles`, `Devices` and
   `Tools` are three parallel lists the user must join in their head. The nav has six
   peers of which only two (Talk, History) are daily use; the other four are configuration
   with no "Settings" to live in.

3. **Pairing is a permanently-mounted form.** `Devices.tsx` renders the claim form under
   the list at all times, whether or not the user is pairing. A once-in-a-device-lifetime
   action occupies most of the screen forever, and the empty state looks like the full one.

## Model

One central noun: **the assistant** (a Profile). A device *borrows an assistant's voice*.

```
Assistant  1 ──── N  Device
Device     0..1 ──── Assistant      ("Unassigned" is a legal state)
```

A device belongs to at most one assistant and can be moved between assistants without
re-pairing (the pairing token is hardware identity; the assistant is a soft assignment).

## Server (source of truth)

| Concern | Decision |
| --- | --- |
| Column | `Device.profile_id: String(128), default "", index=True` — stores the profile *name*, matching `sessions.profile_id`, because `profile_store` is keyed by name. |
| Migration | `_ensure_column(conn, "devices", "profile_id", "VARCHAR(128) DEFAULT ''")` in `init_db()`. This codebase has no migration framework; that helper is the established idempotent path. |
| Pair | `POST /v1/devices/pair/claim` accepts optional `profile_id`, so pairing binds in one step and there is no "paired but unassigned" window when the user came from a profile. |
| Assign | `POST /v1/devices/mine/{device_id}/profile` with `{"profile_id": "..."}`; `""` unassigns. |
| Read | `GET /v1/devices/mine` includes `profile_id`. |

### Precedence at connect time

For an identity with `via_device` **and** a non-empty binding, the binding wins and the
device's own `?profile=` is ignored; when the two differ the server emits a `warning`
event over the WS so stale firmware config is visible rather than silently void. A device
with no binding keeps using `?profile=` exactly as today, so existing fleets do not break.

### Security invariants

1. Assignment must pass `profile_visible(profile, user_id)`. Without it, user A binds a
   device to user B's private profile and runs on B's `llm.api_key`, `system_prompt` and
   private `mcp_servers` — the exact IDOR that `services/profile_visibility.py` exists to
   close (finding C2).
2. Connect-time resolution re-checks visibility. A binding can outlive the profile's
   ownership or existence.
3. The new route reuses the hardened path shape of `_is_own_device_revoke`
   (`core/auth_guard.py`) rather than a prefix rule. `/v1/devices/{device_id}/revoke` is an
   admin route in the same namespace; the comments at `auth_guard.py:65-70` record how a
   loose rule there previously smuggled admin access.
4. Deleting a profile clears bindings pointing at it. Devices become *Unassigned* — never
   revoked, never needing a re-pair, and never left dangling at a name that no longer
   resolves.

Per-profile history needs **no** server work: `GET /v1/sessions?profile=<name>` already
filters (`api/routes/sessions.py`).

## Web client

Nav drops to three: **Talk · Assistants · Settings**. *Sign out* leaves the nav for
`Settings › Account` — it is a rare action, not a destination. The global History tab is
removed; history is reached per assistant.

**Assistants (hub)** — a card grid:

```
┌──────────────────────────────────┐
│ (H)  Kitchen assistant       ⋯   │   ⋯ = Duplicate · Delete
│ ┌────────┬─────────┬───────────┐ │
│ │ Voice  │  Model  │ Last used │ │
│ │ VN fem │ Qwen3.6 │  2h ago   │ │
│ └────────┴─────────┴───────────┘ │
│ [Configure] [History] [Devices 2]│
└──────────────────────────────────┘
```

- Avatar: initial + gradient derived deterministically from the profile name, so each
  assistant keeps a stable face across sessions and machines.
- The meta strip shows what a non-engineer recognises (voice nickname, model label, last
  used), never engine names or model ids. Those stay in Configure.
- Destructive actions live in the overflow menu, not level with everyday buttons.
  Renaming is not offered there: the nickname is edited in Configure, and the
  slug is the store key (see Out of scope).
- *Last used* is derived client-side from one `listSessions()` call grouped by
  `profile_id`. Profiles absent from that page read **"Not used recently"** — the copy must
  not claim "never", which the truncated page cannot establish.

**Devices panel** (opened from a card, so the profile is already known): the assistant's
devices with status and per-device actions, plus `+ Add device` which opens the pairing
wizard — three steps (prompt for the code → enter code + name → confirmation). The code
length comes from `PAIR_CODE_LENGTH`, never a literal.

**Settings**: Account · All devices · Tools & Voices · Usage. *All devices* is the flat
cross-cutting view, grouped by assistant with an **Unassigned** group — the only place an
orphaned device is visible.

## Out of scope

- **One device bound to several profiles.** Needs a join table, an active-selection UI and
  a protocol for the device to announce a switch. No demand yet.
- **Unifying the dark Talk screen with the cream configuration screens.** A real
  inconsistency, but an independent change; folding it in here would make the diff
  unreviewable.
- **Renaming a profile's slug.** The name is the store key and the binding value; renaming
  is its own migration problem.

## Testing

Server: binding beats `?profile=`; unbound device still honours `?profile=`; cross-user
assignment is refused; deleted profile clears bindings; the auth-guard shape tests extend
the existing `_is_own_device_revoke` cases to the new subresource.

Web: pairing wizard error paths (wrong/expired code, hardware already paired), device
count on the card, assignment round-trip, and the empty/unassigned states.
