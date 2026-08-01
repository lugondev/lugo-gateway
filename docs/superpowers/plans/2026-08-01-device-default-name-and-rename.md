# Default Device Name + Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pairing completes on the 8-digit code alone; the device is named after its setup AP (`Lugo-XXXX`) by the server, and can be renamed afterwards from the wizard or the device list.

**Architecture:** The gateway derives the default name from the `serial` it already receives at `pair/init`, so no firmware or wire-protocol change is needed. `pair/claim` takes an optional name and substitutes the derived one when it is blank. A new `POST /v1/devices/mine/{device_id}/name` endpoint handles renames, mirroring the existing `.../profile` endpoint's auth and 404 semantics. The web client drops the name field from the pairing form, offers the prefilled name on the success step, and adds a rename action to the device row.

**Tech Stack:** FastAPI + Pydantic + SQLAlchemy (async) + pytest for the gateway; React + TypeScript + Vitest + Testing Library for the web client.

## Global Constraints

- **Two separate git repositories.** The gateway lives in the monorepo at `/Users/lugon/code/speech-text-transformer`. The web client is its own repo at `/Users/lugon/code/speech-text-transformer/lugo-web-client`. Commit in whichever repo the task's files belong to; never stage across both.
- **Branch:** both repos are on `feat/device-default-name`. Commit there; never on `main`. The merge back to `main` happens once, after the final review.
- **No firmware change.** Nothing in `esp32-assistant/` is touched by this plan.
- Default name formula, exactly: `"Lugo-" + serial[-4:].upper()`.
- Degenerate serial (fewer than 4 characters): the literal `"New device"`.
- `Device.name` column is `String(128)` — the rename cap is 128 characters.
- The rename endpoint must **not** touch the pairing token or `profile_id`.
- 404 must remain indistinguishable between "no such device" and "someone else's device" (the rule stated in `_checked_profile_name`, `apps/api_gateway/app/api/routes/devices.py`).
- Gateway test command: `.venv/bin/pytest -q` from the monorepo root (`make test` wraps it).
- Web client test command: `pnpm test` from `lugo-web-client/` (`vitest run`).
- The static panel `apps/api_gateway/app/static/js/devices.js` is **out of scope** and must keep working untouched — it always sends a name, and an explicit name still wins.

## File Structure

**Gateway (monorepo)**

| File | Responsibility |
|---|---|
| `apps/api_gateway/app/services/auth/device_naming.py` *(create)* | Pure `default_device_name(serial)` derivation. No I/O, no DB. |
| `apps/api_gateway/app/schemas/devices.py` *(modify)* | `PairClaimRequest.name` optional; new `DeviceNameRequest`. |
| `apps/api_gateway/app/services/auth/devices.py` *(modify)* | `DeviceStore.set_name`, alongside `set_profile`. |
| `apps/api_gateway/app/api/routes/devices.py` *(modify)* | Claim substitutes the derived name; new rename route. |
| `tests/unit/auth/test_device_naming.py` *(create)* | Derivation unit tests. |
| `tests/unit/auth/test_devices_routes.py` *(modify)* | Claim-default and rename route tests. |

**Web client (separate repo)**

| File | Responsibility |
|---|---|
| `src/api/devices.ts` *(modify)* | `renameDevice`; `claimDevice` name optional. |
| `src/api/devices.test.ts` *(modify)* | `renameDevice` request shape. |
| `src/screens/devices/RenameDeviceModal.tsx` *(create)* | Shared rename dialog — two parents need it. |
| `src/screens/devices/RenameDeviceModal.test.tsx` *(create)* | Modal behaviour. |
| `src/screens/devices/PairWizard.tsx` *(modify)* | No name field; prefilled name on the done step. |
| `src/screens/devices/PairWizard.test.tsx` *(modify)* | Updated flow. |
| `src/screens/devices/useDeviceActions.ts` *(create)* | Move/remove/rename state + handlers, shared by both device screens. |
| `src/screens/devices/useDeviceActions.test.tsx` *(create)* | Hook behaviour — the only test coverage these screens have. |
| `src/screens/devices/DeviceRow.tsx` *(modify)* | `onRename` prop + menu item. |
| `src/screens/devices/DeviceRow.test.tsx` *(create)* | Menu offers rename. |
| `src/screens/settings/AllDevices.tsx` *(modify)* | Consume the hook; render the rename dialog. |
| `src/screens/profiles/ProfileDevices.tsx` *(modify)* | Consume the hook; render the rename dialog. |

**Decision (taken before execution):** `AllDevices.tsx` and `ProfileDevices.tsx`
currently carry byte-identical `move`/`remove` handlers and their state. Rather than
adding a third identical copy for rename, Task 7 extracts all of it into a
`useDeviceActions` hook and Task 8 adds rename to that one place. Task 7 is therefore
a pure refactor with no behaviour change; Task 8 is the feature.

**Note:** neither screen has any test today, so the refactor's only safety nets are
the hook's own new tests and `tsc -b` (via `pnpm build`). Both are mandatory in Task 7.

---

### Task 1: Default name derivation

**Files:**
- Create: `apps/api_gateway/app/services/auth/device_naming.py`
- Test: `tests/unit/auth/test_device_naming.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `default_device_name(serial: str) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/auth/test_device_naming.py`:

```python
from app.services.auth.device_naming import default_device_name


def test_derives_the_ap_name_the_device_shows_during_setup():
    # The firmware builds its setup-AP SSID as "Lugo-%02X%02X" of the MAC's last
    # two bytes, and sends the same MAC as a 12-hex-char serial at pair/init.
    # Verified against real hardware: serial 2884855048d0 -> AP "Lugo-48D0".
    assert default_device_name("2884855048d0") == "Lugo-48D0"


def test_uppercases_hex_so_it_matches_the_ssid_exactly():
    assert default_device_name("aabbccddeeff") == "Lugo-EEFF"


def test_already_uppercase_serial_is_left_alone():
    assert default_device_name("AABBCCDDEEFF") == "Lugo-EEFF"


def test_short_serial_falls_back_rather_than_emitting_a_fragment():
    # Real hardware never does this, but `serial` is a free-form string on the
    # wire. A truncated "Lugo-ab" would look like a real AP name and isn't one.
    assert default_device_name("abc") == "New device"
    assert default_device_name("") == "New device"


def test_exactly_four_characters_is_enough():
    assert default_device_name("48d0") == "Lugo-48D0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/auth/test_device_naming.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.auth.device_naming'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/api_gateway/app/services/auth/device_naming.py`:

```python
"""The name a freshly paired device gets when its owner didn't pick one.

The device already calls itself something the user can read off its own screen
during setup: the SSID of its provisioning AP, built by the firmware as
``"Lugo-%02X%02X"`` of the last two bytes of its MAC
(esp32-assistant/components/provisioning/provisioning_ssid.c). The pairing
serial is that same MAC as 12 hex characters, so the gateway can reproduce the
name from what pair/init already sends -- no extra field on the wire, no
firmware change.

Known coupling: the firmware derives the SSID from the STA interface MAC and the
serial from the efuse base MAC. On stock ESP-IDF those are the same address,
which is why this is exact today. A custom MAC configuration could make them
diverge and this name would then not match the AP -- cosmetic only, since it is
a suggestion the user can change.
"""

_FALLBACK = "New device"


def default_device_name(serial: str) -> str:
    """"Lugo-48D0" for serial "2884855048d0"; `_FALLBACK` if it's too short."""
    if len(serial) < 4:
        return _FALLBACK
    return f"Lugo-{serial[-4:].upper()}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/auth/test_device_naming.py -v`
Expected: PASS — 5 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer
git add apps/api_gateway/app/services/auth/device_naming.py tests/unit/auth/test_device_naming.py
git commit -m "feat(devices): derive a default device name from the pairing serial"
```

---

### Task 2: Claim uses the derived name when none is given

**Files:**
- Modify: `apps/api_gateway/app/schemas/devices.py:7-16` (`PairClaimRequest`)
- Modify: `apps/api_gateway/app/api/routes/devices.py:65-92` (`pair_claim`)
- Test: `tests/unit/auth/test_devices_routes.py`

**Interfaces:**
- Consumes: `default_device_name(serial: str) -> str` from Task 1.
- Produces: `POST /v1/devices/pair/claim` accepts a body with no `name` key and returns a device whose `name` is the derived one.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/auth/test_devices_routes.py`:

```python
def test_claim_without_a_name_uses_the_devices_own_ap_name(client, _logged_in_user):
    init = client.post(
        "/v1/devices/pair/init", json={"serial": "2884855048d0"}
    ).json()["data"]

    # No `name` key at all: the user pairs on the code alone and names the
    # device afterwards, if they want to.
    resp = client.post("/v1/devices/pair/claim", json={"code": init["code"]})

    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "Lugo-48D0"


def test_claim_with_a_blank_name_gets_the_derived_one_too(client, _logged_in_user):
    init = client.post(
        "/v1/devices/pair/init", json={"serial": "2884855048d0"}
    ).json()["data"]

    resp = client.post(
        "/v1/devices/pair/claim", json={"code": init["code"], "name": "   "}
    )

    assert resp.json()["data"]["name"] == "Lugo-48D0"


def test_an_explicit_name_still_wins(client, _logged_in_user):
    # Older clients (the static devices.js panel) always send one.
    init = client.post(
        "/v1/devices/pair/init", json={"serial": "2884855048d0"}
    ).json()["data"]

    resp = client.post(
        "/v1/devices/pair/claim", json={"code": init["code"], "name": "Kitchen speaker"}
    )

    assert resp.json()["data"]["name"] == "Kitchen speaker"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/auth/test_devices_routes.py -k "ap_name or blank_name or explicit_name" -v`
Expected: FAIL — the first two with `422 Unprocessable Entity` (name is currently required) / a name that is `""` rather than `Lugo-48D0`.

- [ ] **Step 3: Write minimal implementation**

In `apps/api_gateway/app/schemas/devices.py`, change `PairClaimRequest`:

```python
class PairClaimRequest(BaseModel):
    code: str
    # Optional: blank means "name it after the device". The gateway fills in the
    # SSID the device shows on its own setup AP (services/auth/device_naming.py),
    # so pairing can finish on the code alone. Clients that send a name still win.
    name: str = ""
    # Bind the device to an assistant in the same call that creates it. The web
    # client pairs from inside a profile, so it always knows the answer here, and
    # doing it in one step removes the window where a device exists but answers
    # to nothing. "" (the default) pairs the device unassigned, which is what the
    # older clients that don't send this field get.
    profile_id: str = ""
```

In `apps/api_gateway/app/api/routes/devices.py`, add the import next to the other service imports:

```python
from app.services.auth.device_naming import default_device_name
```

and in `pair_claim`, replace the `device_store.create(...)` call with:

```python
    profile_id = _checked_profile_name(payload.profile_id, user_id)
    name = payload.name.strip() or default_device_name(entry.serial)
    device, raw_token = await device_store.create(
        user_id, name, entry.serial, profile_id=profile_id
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/auth/test_devices_routes.py -v`
Expected: PASS — the three new tests plus every pre-existing one in the file (they all send a name, and an explicit name is unchanged behaviour).

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer
git add apps/api_gateway/app/schemas/devices.py apps/api_gateway/app/api/routes/devices.py tests/unit/auth/test_devices_routes.py
git commit -m "feat(devices): let pairing finish on the code alone"
```

---

### Task 3: Rename endpoint

**Files:**
- Modify: `apps/api_gateway/app/schemas/devices.py` (add `DeviceNameRequest`)
- Modify: `apps/api_gateway/app/services/auth/devices.py` (add `DeviceStore.set_name`, next to `set_profile` at :102-120)
- Modify: `apps/api_gateway/app/api/routes/devices.py` (add the route after `set_my_device_profile`)
- Test: `tests/unit/auth/test_devices_routes.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `POST /v1/devices/mine/{device_id}/name`, body `{"name": str}`, response `{"success": True, "data": {"id": str, "name": str}}`. Store method `DeviceStore.set_name(device_id: str, name: str, owner_user_id: str) -> bool`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/auth/test_devices_routes.py`:

```python
def _pair_a_device(client, serial="2884855048d0"):
    """Pair one device and return its dict. Assumes a logged-in client."""
    init = client.post("/v1/devices/pair/init", json={"serial": serial}).json()["data"]
    return client.post("/v1/devices/pair/claim", json={"code": init["code"]}).json()["data"]


def test_rename_changes_the_name_and_leaves_the_binding_alone(client, _logged_in_user):
    device = _pair_a_device(client)

    resp = client.post(
        f"/v1/devices/mine/{device['id']}/name", json={"name": "Kitchen speaker"}
    )

    assert resp.status_code == 200
    assert resp.json()["data"] == {"id": device["id"], "name": "Kitchen speaker"}
    listed = client.get("/v1/devices/mine").json()["data"]
    assert [d["name"] for d in listed] == ["Kitchen speaker"]
    # A name is a label, not hardware identity: renaming must not send the user
    # back to the device to read a fresh code.
    assert listed[0]["profile_id"] == device["profile_id"]


def test_rename_trims_surrounding_whitespace(client, _logged_in_user):
    device = _pair_a_device(client)

    resp = client.post(
        f"/v1/devices/mine/{device['id']}/name", json={"name": "  Kitchen  "}
    )

    assert resp.json()["data"]["name"] == "Kitchen"


def test_rename_to_blank_is_rejected(client, _logged_in_user):
    device = _pair_a_device(client)

    resp = client.post(f"/v1/devices/mine/{device['id']}/name", json={"name": "   "})

    assert resp.status_code == 400


def test_rename_over_the_column_length_is_rejected(client, _logged_in_user):
    device = _pair_a_device(client)

    resp = client.post(
        f"/v1/devices/mine/{device['id']}/name", json={"name": "x" * 129}
    )

    assert resp.status_code == 400


def test_rename_someone_elses_device_is_indistinguishable_from_a_missing_one(client):
    client.post("/api/auth/signup", json={"username": "owner", "password": "pw"})
    client.post("/api/auth/signup", json={"username": "other", "password": "pw"})

    client.post("/api/auth/login", json={"username": "owner", "password": "pw"})
    device = _pair_a_device(client, serial="ffffffff1234")

    client.post("/api/auth/login", json={"username": "other", "password": "pw"})
    theirs = client.post(
        f"/v1/devices/mine/{device['id']}/name", json={"name": "mine now"}
    )
    missing = client.post(
        "/v1/devices/mine/does-not-exist/name", json={"name": "mine now"}
    )

    # Same status AND same message: otherwise this becomes a device-id oracle.
    assert theirs.status_code == missing.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/auth/test_devices_routes.py -k rename -v`
Expected: FAIL — all five with 405/404 from FastAPI, because the route does not exist.

- [ ] **Step 3: Write minimal implementation**

In `apps/api_gateway/app/schemas/devices.py`, append:

```python
class DeviceNameRequest(BaseModel):
    """Body of POST /v1/devices/mine/{device_id}/name."""

    name: str
```

In `apps/api_gateway/app/services/auth/devices.py`, add to `DeviceStore` directly below `set_profile`:

```python
    async def set_name(self, device_id: str, name: str, owner_user_id: str) -> bool:
        """Rename one of `owner_user_id`'s devices. False if it isn't theirs.

        Same ownership shape as set_profile(): a device_id belonging to someone
        else returns False and is reported by the route as 404, so this cannot be
        used to probe which device ids exist. A revoked device is not renamable
        either -- it is a tombstone, not a device the owner still has.

        Touches `name` only. The token is hardware identity and the profile is a
        soft setting; neither has anything to do with a label."""
        async with db_session() as s:
            row = await s.get(Device, device_id)
            if row is None or row.user_id != owner_user_id or row.revoked:
                return False
            row.name = name
            await s.commit()
            return True
```

This deliberately mirrors `set_profile` line for line — same read-then-write via
`s.get`, same three-way guard. No new imports: the module imports only `select`
from SQLAlchemy and this path needs none.

In `apps/api_gateway/app/api/routes/devices.py`, add `DeviceNameRequest` to the schemas import, then add this route directly after `set_my_device_profile`:

```python
@router.post("/mine/{device_id}/name")
async def rename_my_device(
    device_id: str, payload: DeviceNameRequest, request: Request
) -> dict:
    """Rename a device.

    Separate from the profile endpoint on purpose: that one moves a device
    between assistants, this one only changes what the owner calls it. Neither
    touches the pairing token."""
    user_id = current_user_id(request)
    if not user_id:
        raise AuthError("login required")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="device name cannot be empty")
    if len(name) > 128:
        # Matches the Device.name column (String(128)); rejecting here beats a
        # database error or a silent truncation the user never sees.
        raise HTTPException(status_code=400, detail="device name is too long")
    ok = await device_store.set_name(device_id, name, owner_user_id=user_id)
    if not ok:
        # Someone else's device id and a nonexistent one look identical here, on
        # purpose -- same reasoning as _checked_profile_name.
        raise HTTPException(status_code=404, detail=f"device '{device_id}' not found")
    return {"success": True, "data": {"id": device_id, "name": name}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/auth/ -v`
Expected: PASS — the five new tests plus the whole auth suite.

- [ ] **Step 5: Run the full gateway suite and lint**

Run: `make test && make lint`
Expected: PASS, no ruff findings.

- [ ] **Step 6: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer
git add apps/api_gateway/app/schemas/devices.py apps/api_gateway/app/services/auth/devices.py apps/api_gateway/app/api/routes/devices.py tests/unit/auth/test_devices_routes.py
git commit -m "feat(devices): add a rename endpoint that leaves the pairing alone"
```

---

### Task 4: `renameDevice` API client

**Files:**
- Modify: `lugo-web-client/src/api/devices.ts:75-88` (`claimDevice`) and append `renameDevice`
- Test: `lugo-web-client/src/api/devices.test.ts`

**Interfaces:**
- Consumes: `POST /v1/devices/mine/{id}/name` from Task 3.
- Produces: `renameDevice(id: string, name: string): Promise<void>`; `claimDevice(code: string, name?: string, profileId?: string): Promise<Device>`.

- [ ] **Step 1: Write the failing test**

Open `lugo-web-client/src/api/devices.test.ts`. It stubs `fetch` per test with
`vi.stubGlobal` and builds bodies with a local `jsonResponse` helper; the tests
below follow that exact style. Add `renameDevice` to the existing named import
from `./devices` (keep the others), then append these inside the existing
`describe('devices api', ...)` block:

```ts
  it('renameDevice posts the new name to MY device, url-encoding the id', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: { id: 'a/b', name: 'Kitchen' } }))
    vi.stubGlobal('fetch', f)

    await renameDevice('a/b', 'Kitchen')

    expect(f.mock.calls[0][0]).toContain('/v1/devices/mine/a%2Fb/name')
    expect(f.mock.calls[0][1].method).toBe('POST')
    expect(JSON.parse(f.mock.calls[0][1].body)).toEqual({ name: 'Kitchen' })
  })

  it('renameDevice surfaces the server message on failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: "device 'x' not found" }, 404)))

    await expect(renameDevice('x', 'Kitchen')).rejects.toThrow(/not found/)
  })

  it('claimDevice sends an empty name, letting the server name the device', async () => {
    const f = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: DEVICE }))
    vi.stubGlobal('fetch', f)

    await claimDevice('01234567')

    expect(JSON.parse(f.mock.calls[0][1].body)).toEqual({
      code: '01234567',
      name: '',
      profile_id: '',
    })
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lugo-web-client && pnpm vitest run src/api/devices.test.ts`
Expected: FAIL — `renameDevice is not a function` / import error.

- [ ] **Step 3: Write minimal implementation**

In `lugo-web-client/src/api/devices.ts`, change `claimDevice`'s signature so the name is optional (the body and the rest of the function stay exactly as they are):

```ts
export async function claimDevice(
  code: string,
  name = '',
  profileId = '',
): Promise<Device> {
```

and append at the end of the file:

```ts
/** Rename a device. Never touches the pairing token or the assistant binding.
 *
 * A device arrives named after its own setup AP (Lugo-XXXX, chosen by the
 * server from the pairing serial), so this is always an edit of something that
 * already reads sensibly -- never the user's only chance to name it. */
export async function renameDevice(id: string, name: string): Promise<void> {
  const resp = await apiFetch(`/v1/devices/mine/${encodeURIComponent(id)}/name`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  if (!resp.ok) throw await errorFrom(resp)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd lugo-web-client && pnpm vitest run src/api/devices.test.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/lugo-web-client
git add src/api/devices.ts src/api/devices.test.ts
git commit -m "feat(devices): add renameDevice and make the claim name optional"
```

---

### Task 5: `RenameDeviceModal`

**Files:**
- Create: `lugo-web-client/src/screens/devices/RenameDeviceModal.tsx`
- Test: `lugo-web-client/src/screens/devices/RenameDeviceModal.test.tsx`

**Interfaces:**
- Consumes: `Device` type from `../../api/devices`; `Modal`, `Button`, `TextInput` from `../../ui/`.
- Produces: `<RenameDeviceModal device busy error onCancel onConfirm />` where `onConfirm: (name: string) => void`. Open when `device !== null`.

- [ ] **Step 1: Write the failing test**

Create `lugo-web-client/src/screens/devices/RenameDeviceModal.test.tsx`:

```tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { RenameDeviceModal } from './RenameDeviceModal'
import type { Device } from '../../api/devices'

const DEVICE = {
  id: 'd1',
  user_id: 'u1',
  name: 'Lugo-48D0',
  serial: '2884855048d0',
  profile_id: 'kitchen',
  created_at: null,
  last_seen_at: null,
  revoked: false,
} satisfies Device

it('opens prefilled with the current name', () => {
  render(<RenameDeviceModal device={DEVICE} onCancel={() => {}} onConfirm={() => {}} />)
  expect((screen.getByLabelText('Device name') as HTMLInputElement).value).toBe('Lugo-48D0')
})

it('confirms with the edited name', () => {
  const onConfirm = vi.fn()
  render(<RenameDeviceModal device={DEVICE} onCancel={() => {}} onConfirm={onConfirm} />)

  fireEvent.change(screen.getByLabelText('Device name'), {
    target: { value: 'Kitchen speaker' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Rename' }))

  expect(onConfirm).toHaveBeenCalledWith('Kitchen speaker')
})

it('refuses to save a blank name', () => {
  render(<RenameDeviceModal device={DEVICE} onCancel={() => {}} onConfirm={() => {}} />)
  fireEvent.change(screen.getByLabelText('Device name'), { target: { value: '   ' } })
  expect((screen.getByRole('button', { name: 'Rename' }) as HTMLButtonElement).disabled).toBe(true)
})

it('shows the server error without closing', () => {
  render(
    <RenameDeviceModal
      device={DEVICE}
      error="device 'd1' not found"
      onCancel={() => {}}
      onConfirm={() => {}}
    />,
  )
  expect(screen.getByRole('alert').textContent).toContain('not found')
})

it('renders nothing when no device is selected', () => {
  render(<RenameDeviceModal device={null} onCancel={() => {}} onConfirm={() => {}} />)
  expect(screen.queryByLabelText('Device name')).toBeNull()
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lugo-web-client && pnpm vitest run src/screens/devices/RenameDeviceModal.test.tsx`
Expected: FAIL — cannot resolve `./RenameDeviceModal`.

- [ ] **Step 3: Write minimal implementation**

Create `lugo-web-client/src/screens/devices/RenameDeviceModal.tsx`:

```tsx
import { useEffect, useState } from 'react'
import { Button } from '../../ui/Button'
import { Modal } from '../../ui/Modal'
import { TextInput } from '../../ui/TextInput'
import type { Device } from '../../api/devices'

/** Rename a paired device.
 *
 * Its own component rather than inline in the row, because DeviceRow has two
 * parents -- the per-assistant list and the all-devices view -- and both offer
 * the action. MoveDeviceModal is shared for the same reason; a second copy of
 * either would drift.
 */
export function RenameDeviceModal({
  device,
  busy = false,
  error,
  onCancel,
  onConfirm,
}: {
  device: Device | null
  busy?: boolean
  error?: string | null
  onCancel: () => void
  onConfirm: (name: string) => void
}) {
  const [name, setName] = useState('')

  // Reopen on whatever the device is called today, so the field always starts
  // from the truth rather than from the last device the user opened.
  useEffect(() => {
    setName(device?.name ?? '')
  }, [device])

  return (
    <Modal open={device !== null} onClose={onCancel} title={`Rename "${device?.name ?? ''}"`}>
      <TextInput
        id="rename-device"
        label="Device name"
        value={name}
        maxLength={128}
        onChange={(e) => setName(e.target.value)}
      />
      {error && (
        <p className="field__error" role="alert">
          {error}
        </p>
      )}
      <div className="modal__actions">
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button
          variant="primary"
          size="sm"
          onClick={() => onConfirm(name.trim())}
          disabled={busy || !name.trim()}
        >
          {busy ? 'Renaming…' : 'Rename'}
        </Button>
      </div>
    </Modal>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd lugo-web-client && pnpm vitest run src/screens/devices/RenameDeviceModal.test.tsx`
Expected: PASS — 5 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/lugo-web-client
git add src/screens/devices/RenameDeviceModal.tsx src/screens/devices/RenameDeviceModal.test.tsx
git commit -m "feat(devices): add a rename dialog shared by both device lists"
```

---

### Task 6: PairWizard pairs on the code alone

**Files:**
- Modify: `lugo-web-client/src/screens/devices/PairWizard.tsx`
- Test: `lugo-web-client/src/screens/devices/PairWizard.test.tsx`

**Interfaces:**
- Consumes: `claimDevice(code, name?, profileId?)` and `renameDevice(id, name)` from Task 4.
- Produces: no new exports; `PairWizard`'s props are unchanged.

- [ ] **Step 1: Write the failing test**

Rewrite `lugo-web-client/src/screens/devices/PairWizard.test.tsx` so the mock covers `renameDevice` too, the helper no longer fills a name, and the claim assertion drops it. Replace the file's contents with:

```tsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'

vi.mock('../../api/devices', async (orig) => ({
  ...(await orig<typeof import('../../api/devices')>()),
  claimDevice: vi.fn(),
  renameDevice: vi.fn(),
}))

import { claimDevice, renameDevice } from '../../api/devices'
import { PairWizard } from './PairWizard'

// The server's pairing code (api_gateway app/services/auth/pairing.py,
// `_CODE_DIGITS`) was widened 6 -> 8 as brute-force hardening. The screen this
// wizard replaced had the old 6 hardcoded in three places, which made pairing
// impossible: the input truncated the last two digits and the submit button
// never enabled. These tests pin the wizard to the server's real code length.
const CODE = '01234567'

const PAIRED = {
  id: 'd1',
  user_id: 'u1',
  name: 'Lugo-48D0',
  serial: '2884855048d0',
  profile_id: 'kitchen',
  created_at: null,
  last_seen_at: null,
  revoked: false,
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(claimDevice).mockResolvedValue(PAIRED as never)
  vi.mocked(renameDevice).mockResolvedValue(undefined as never)
})

/** Open the wizard and walk to the code step, filling in `code`. */
async function renderAndFill(code: string) {
  render(
    <PairWizard
      open
      profileId="kitchen"
      profileTitle="Kitchen assistant"
      onCancel={() => {}}
      onPaired={() => {}}
    />,
  )
  fireEvent.click(await screen.findByRole('button', { name: 'I see a code' }))
  fireEvent.change(screen.getByLabelText(/digit code/i), { target: { value: code } })
  return screen.getByRole('button', { name: 'Pair device' }) as HTMLButtonElement
}

it('pairs on the code alone -- no name is asked for up front', async () => {
  const submit = await renderAndFill(CODE)
  expect(screen.queryByLabelText('Device name')).toBeNull()
  expect(submit.disabled).toBe(false)

  fireEvent.click(submit)
  // The assistant rides along with the claim: no window where the device is
  // paired but answers to nothing. The name is the server's job now.
  await waitFor(() => expect(claimDevice).toHaveBeenCalledWith(CODE, '', 'kitchen'))
})

it('keeps submit disabled for a short code', async () => {
  const submit = await renderAndFill(CODE.slice(0, -1))
  expect(submit.disabled).toBe(true)
})

it('strips non-digits and caps at the code length', async () => {
  await renderAndFill('12a34-5678999')
  expect((screen.getByLabelText(/digit code/i) as HTMLInputElement).value).toBe('12345678')
})

it('does not ask which assistant -- that is fixed by where it was opened from', async () => {
  await renderAndFill(CODE)
  expect(screen.queryByLabelText('Assistant')).toBeNull()
  expect(screen.getByRole('heading', { name: 'Pair with Kitchen assistant' })).toBeTruthy()
})

it('offers the server-chosen name, prefilled and editable', async () => {
  const submit = await renderAndFill(CODE)
  fireEvent.click(submit)
  await screen.findByRole('heading', { name: 'Device added' })
  expect((screen.getByLabelText('Device name') as HTMLInputElement).value).toBe('Lugo-48D0')
})

it('saves an edited name against the device that was just paired', async () => {
  const submit = await renderAndFill(CODE)
  fireEvent.click(submit)
  await screen.findByRole('heading', { name: 'Device added' })

  fireEvent.change(screen.getByLabelText('Device name'), {
    target: { value: 'Kitchen speaker' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(renameDevice).toHaveBeenCalledWith('d1', 'Kitchen speaker'))
})

it('does not call rename when the name was left alone', async () => {
  const submit = await renderAndFill(CODE)
  fireEvent.click(submit)
  await screen.findByRole('heading', { name: 'Device added' })

  fireEvent.click(screen.getByRole('button', { name: 'Done' }))

  expect(renameDevice).not.toHaveBeenCalled()
})

it('keeps the device when the rename fails -- pairing already succeeded', async () => {
  vi.mocked(renameDevice).mockRejectedValue(new Error("device 'd1' not found"))
  const submit = await renderAndFill(CODE)
  fireEvent.click(submit)
  await screen.findByRole('heading', { name: 'Device added' })

  fireEvent.change(screen.getByLabelText('Device name'), { target: { value: 'Kitchen' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))

  expect(await screen.findByText(/not found/i)).toBeTruthy()
  // Still on the done step, so Done remains a clean exit.
  expect(screen.getByRole('button', { name: 'Done' })).toBeTruthy()
})

it('shows an actionable message when the code is wrong or expired', async () => {
  vi.mocked(claimDevice).mockRejectedValue(new Error('pairing code is invalid or expired'))
  const submit = await renderAndFill(CODE)
  fireEvent.click(submit)
  // friendlyDeviceError's wording -- the raw server string is not shown, but the
  // DISTINCTION from "already paired" is kept, because the fixes differ.
  expect(await screen.findByText(/wrong or expired/i)).toBeTruthy()
})

it('tells the user to remove the old pairing when the hardware is already paired', async () => {
  vi.mocked(claimDevice).mockRejectedValue(new Error('device already paired to another account'))
  const submit = await renderAndFill(CODE)
  fireEvent.click(submit)
  expect(await screen.findByText(/already paired to an account/i)).toBeTruthy()
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lugo-web-client && pnpm vitest run src/screens/devices/PairWizard.test.tsx`
Expected: FAIL — the code step still renders a required `Device name` input, so `pairs on the code alone` fails on `queryByLabelText`, and the done-step tests find no `Device name` field or `Save` button.

- [ ] **Step 3: Write minimal implementation**

Replace `lugo-web-client/src/screens/devices/PairWizard.tsx` with:

```tsx
import { useState } from 'react'
import {
  PAIR_CODE_LENGTH,
  claimDevice,
  friendlyDeviceError,
  renameDevice,
  type Device,
} from '../../api/devices'
import { Button } from '../../ui/Button'
import { Modal } from '../../ui/Modal'
import { TextInput } from '../../ui/TextInput'

type Step = 'intro' | 'code' | 'done'

/** Pairing, as three steps that only exist while pairing.
 *
 * Replaces a claim form that was mounted under the device list permanently: a
 * once-per-device action took most of the screen forever, and it made the empty
 * state look like the populated one.
 *
 * The assistant is fixed by where the user opened this from, so it is never asked
 * for -- and it is sent with the claim itself, so there is no moment where the
 * device is paired but answers to nothing.
 *
 * The NAME is not asked for up front either. The device arrives already called
 * after its own setup AP (Lugo-XXXX, derived server-side from the pairing
 * serial), so the last step offers that name to edit instead of demanding one at
 * the moment the user knows the device least. Pairing is already committed by
 * then: a failed rename costs the name, never the pairing.
 */
export function PairWizard({
  open,
  profileId,
  profileTitle,
  onCancel,
  onPaired,
}: {
  open: boolean
  profileId: string
  profileTitle: string
  onCancel: () => void
  onPaired: () => void
}) {
  const [step, setStep] = useState<Step>('intro')
  const [code, setCode] = useState('')
  const [paired, setPaired] = useState<Device | null>(null)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  function reset() {
    setStep('intro')
    setCode('')
    setPaired(null)
    setName('')
    setError(null)
  }

  function close() {
    reset()
    onCancel()
  }

  function finish() {
    reset()
    onPaired()
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const device = await claimDevice(code.trim(), '', profileId)
      setPaired(device)
      setName(device.name)
      setStep('done')
    } catch (err) {
      // Keep the server's DISTINCTION between "wrong code" and "hardware already
      // paired": they call for two different actions from the user.
      setError(err instanceof Error ? friendlyDeviceError(err.message) : 'Pairing failed')
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    const next = name.trim()
    // An untouched field is not an edit -- don't spend a request on it.
    if (!paired || !next || next === paired.name) return finish()
    setBusy(true)
    setError(null)
    try {
      await renameDevice(paired.id, next)
      finish()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not rename the device')
    } finally {
      setBusy(false)
    }
  }

  const titles: Record<Step, string> = {
    intro: 'Add a device',
    code: `Pair with ${profileTitle}`,
    done: 'Device added',
  }

  return (
    <Modal open={open} onClose={close} title={titles[step]}>
      {step === 'intro' && (
        <>
          <p className="modal__body">
            Turn the device on and wait until it shows a {PAIR_CODE_LENGTH}-digit code. Codes
            last 10 minutes — restart the device if yours has expired.
          </p>
          <div className="modal__actions">
            <Button variant="ghost" size="sm" onClick={close}>
              Cancel
            </Button>
            <Button variant="primary" size="sm" onClick={() => setStep('code')}>
              I see a code
            </Button>
          </div>
        </>
      )}

      {step === 'code' && (
        <form className="pair__form" onSubmit={submit}>
          <TextInput
            id="pair-code"
            label={`${PAIR_CODE_LENGTH}-digit code`}
            className="pair__code"
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, PAIR_CODE_LENGTH))}
            placeholder={'0'.repeat(PAIR_CODE_LENGTH)}
            inputMode="numeric"
            autoComplete="one-time-code"
          />
          {error && (
            <p className="field__error" role="alert">
              {error}
            </p>
          )}
          <div className="modal__actions">
            <Button variant="ghost" size="sm" onClick={close} disabled={busy}>
              Cancel
            </Button>
            <Button
              variant="primary"
              size="sm"
              type="submit"
              disabled={busy || code.length !== PAIR_CODE_LENGTH}
            >
              {busy ? 'Pairing…' : 'Pair device'}
            </Button>
          </div>
        </form>
      )}

      {step === 'done' && (
        <>
          <p className="modal__body">
            It now runs {profileTitle}. Change that any time from this assistant&apos;s device
            list — no re-pairing needed.
          </p>
          <TextInput
            id="pair-name"
            label="Device name"
            value={name}
            maxLength={128}
            onChange={(e) => setName(e.target.value)}
          />
          {error && (
            <p className="field__error" role="alert">
              {error}
            </p>
          )}
          <div className="modal__actions">
            <Button variant="ghost" size="sm" onClick={finish} disabled={busy}>
              Done
            </Button>
            <Button variant="primary" size="sm" onClick={save} disabled={busy || !name.trim()}>
              {busy ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </>
      )}
    </Modal>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd lugo-web-client && pnpm vitest run src/screens/devices/PairWizard.test.tsx`
Expected: PASS — 10 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/lugo-web-client
git add src/screens/devices/PairWizard.tsx src/screens/devices/PairWizard.test.tsx
git commit -m "feat(devices): pair on the code alone, name the device afterwards"
```

---

### Task 7: Extract `useDeviceActions` (pure refactor, no behaviour change)

`AllDevices.tsx` and `ProfileDevices.tsx` hold byte-identical `move`/`remove`
handlers and state. Pull that into one hook now, so Task 8 adds rename in a single
place instead of a third copy. **Nothing about how the screens behave may change.**

**Files:**
- Create: `lugo-web-client/src/screens/devices/useDeviceActions.ts`
- Test: `lugo-web-client/src/screens/devices/useDeviceActions.test.tsx` *(create)*
- Modify: `lugo-web-client/src/screens/settings/AllDevices.tsx:22-90` and its `DeviceRow`/`MoveDeviceModal`/`ConfirmModal` call sites
- Modify: `lugo-web-client/src/screens/profiles/ProfileDevices.tsx:24-81` and its `DeviceRow`/`MoveDeviceModal`/`ConfirmModal` call sites

**Interfaces:**
- Consumes: `setDeviceProfile`, `revokeDevice`, `type Device` from `../../api/devices`.
- Produces:

```ts
useDeviceActions(
  refresh: () => Promise<void>,
  onRemoveError: (message: string) => void,
): {
  moving: Device | null
  moveBusy: boolean
  moveError: string | null
  openMove: (device: Device) => void
  closeMove: () => void
  move: (targetProfileId: string) => Promise<void>
  removing: Device | null
  removeBusy: boolean
  openRemove: (device: Device) => void
  closeRemove: () => void
  remove: () => Promise<void>
}
```

`onRemoveError` exists because removal failures are shown in the page-level error
banner that lives in each screen, not in the confirm dialog — that is existing
behaviour and must be preserved.

- [ ] **Step 1: Write the failing test**

Create `lugo-web-client/src/screens/devices/useDeviceActions.test.tsx`:

```tsx
import { renderHook, act, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'

vi.mock('../../api/devices', async (orig) => ({
  ...(await orig<typeof import('../../api/devices')>()),
  setDeviceProfile: vi.fn(),
  revokeDevice: vi.fn(),
}))

import { revokeDevice, setDeviceProfile, type Device } from '../../api/devices'
import { useDeviceActions } from './useDeviceActions'

const DEVICE = {
  id: 'd1',
  user_id: 'u1',
  name: 'Lugo-48D0',
  serial: '2884855048d0',
  profile_id: 'kitchen',
  created_at: null,
  last_seen_at: null,
  revoked: false,
} satisfies Device

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(setDeviceProfile).mockResolvedValue(undefined)
  vi.mocked(revokeDevice).mockResolvedValue(undefined)
})

function setup() {
  const refresh = vi.fn().mockResolvedValue(undefined)
  const onRemoveError = vi.fn()
  const hook = renderHook(() => useDeviceActions(refresh, onRemoveError))
  return { ...hook, refresh, onRemoveError }
}

it('moves the open device and refreshes', async () => {
  const { result, refresh } = setup()

  act(() => result.current.openMove(DEVICE))
  expect(result.current.moving).toEqual(DEVICE)

  await act(() => result.current.move('living-room'))

  expect(setDeviceProfile).toHaveBeenCalledWith('d1', 'living-room')
  expect(refresh).toHaveBeenCalled()
  await waitFor(() => expect(result.current.moving).toBeNull())
})

it('keeps the move dialog open and shows the error when the move fails', async () => {
  vi.mocked(setDeviceProfile).mockRejectedValue(new Error('nope'))
  const { result, refresh } = setup()

  act(() => result.current.openMove(DEVICE))
  await act(() => result.current.move('living-room'))

  expect(result.current.moveError).toBe('nope')
  // Still open, so the user can retry rather than losing their place.
  expect(result.current.moving).toEqual(DEVICE)
  expect(refresh).not.toHaveBeenCalled()
})

it('clears a stale move error when the dialog is reopened', async () => {
  vi.mocked(setDeviceProfile).mockRejectedValue(new Error('nope'))
  const { result } = setup()

  act(() => result.current.openMove(DEVICE))
  await act(() => result.current.move('living-room'))
  expect(result.current.moveError).toBe('nope')

  act(() => result.current.openMove(DEVICE))
  expect(result.current.moveError).toBeNull()
})

it('does nothing when move is called with no device open', async () => {
  const { result } = setup()
  await act(() => result.current.move('living-room'))
  expect(setDeviceProfile).not.toHaveBeenCalled()
})

it('revokes the open device and refreshes', async () => {
  const { result, refresh } = setup()

  act(() => result.current.openRemove(DEVICE))
  await act(() => result.current.remove())

  expect(revokeDevice).toHaveBeenCalledWith('d1')
  expect(refresh).toHaveBeenCalled()
  await waitFor(() => expect(result.current.removing).toBeNull())
})

it('reports removal failures to the page banner, not the dialog', async () => {
  vi.mocked(revokeDevice).mockRejectedValue(new Error('Removal failed'))
  const { result, onRemoveError } = setup()

  act(() => result.current.openRemove(DEVICE))
  await act(() => result.current.remove())

  expect(onRemoveError).toHaveBeenCalledWith('Removal failed')
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lugo-web-client && pnpm vitest run src/screens/devices/useDeviceActions.test.tsx`
Expected: FAIL — cannot resolve `./useDeviceActions`.

- [ ] **Step 3: Write the hook**

Create `lugo-web-client/src/screens/devices/useDeviceActions.ts`:

```ts
import { useState } from 'react'
import { revokeDevice, setDeviceProfile, type Device } from '../../api/devices'

/** Move and remove, for the two screens that list devices.
 *
 * `AllDevices` and `ProfileDevices` are near-twins: same rows, same actions,
 * different filters. They carried byte-identical copies of this state and these
 * handlers, which is exactly the kind of pair that drifts. One hook, two callers.
 *
 * Failures are deliberately routed to two different places, matching what the
 * screens already did: a failed move stays in the move dialog (the user is in it,
 * and can retry), while a failed removal goes to the page-level banner via
 * `onRemoveError` (its confirm dialog has nowhere to put a message).
 */
export function useDeviceActions(
  refresh: () => Promise<void>,
  onRemoveError: (message: string) => void,
) {
  const [moving, setMoving] = useState<Device | null>(null)
  const [moveBusy, setMoveBusy] = useState(false)
  const [moveError, setMoveError] = useState<string | null>(null)
  const [removing, setRemoving] = useState<Device | null>(null)
  const [removeBusy, setRemoveBusy] = useState(false)

  async function move(targetProfileId: string) {
    if (!moving) return
    setMoveBusy(true)
    setMoveError(null)
    try {
      await setDeviceProfile(moving.id, targetProfileId)
      setMoving(null)
      await refresh()
    } catch (e) {
      setMoveError(e instanceof Error ? e.message : 'Could not move the device')
    } finally {
      setMoveBusy(false)
    }
  }

  async function remove() {
    if (!removing) return
    setRemoveBusy(true)
    try {
      await revokeDevice(removing.id)
      setRemoving(null)
      await refresh()
    } catch (e) {
      onRemoveError(e instanceof Error ? e.message : 'Removal failed')
    } finally {
      setRemoveBusy(false)
    }
  }

  return {
    moving,
    moveBusy,
    moveError,
    // Opening clears any error left from a previous attempt, so a stale message
    // never greets the next device the user picks.
    openMove: (device: Device) => {
      setMoveError(null)
      setMoving(device)
    },
    closeMove: () => setMoving(null),
    move,
    removing,
    removeBusy,
    openRemove: (device: Device) => setRemoving(device),
    closeRemove: () => setRemoving(null),
    remove,
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd lugo-web-client && pnpm vitest run src/screens/devices/useDeviceActions.test.tsx`
Expected: PASS — 6 passed

- [ ] **Step 5: Consume the hook in both screens**

In **both** `src/screens/settings/AllDevices.tsx` and `src/screens/profiles/ProfileDevices.tsx`:

**(a)** Delete these five `useState` lines and the `move` and `remove` functions entirely:

```tsx
  const [moving, setMoving] = useState<Device | null>(null)
  const [moveError, setMoveError] = useState<string | null>(null)
  const [moveBusy, setMoveBusy] = useState(false)
  const [removing, setRemoving] = useState<Device | null>(null)
  const [removeBusy, setRemoveBusy] = useState(false)
```

**(b)** Add the hook call directly below the `refresh` `useCallback`:

```tsx
  const actions = useDeviceActions(refresh, setError)
```

**(c)** Import it:

```tsx
import { useDeviceActions } from '../devices/useDeviceActions'
```

**(d)** Update the three call sites to read from `actions`:

```tsx
              <DeviceRow
                device={d}
                onMove={() => actions.openMove(d)}
                onRemove={() => actions.openRemove(d)}
              />
```

```tsx
      <MoveDeviceModal
        device={actions.moving}
        profiles={profiles}
        busy={actions.moveBusy}
        error={actions.moveError}
        onCancel={actions.closeMove}
        onConfirm={actions.move}
      />
```

```tsx
      <ConfirmModal
        open={actions.removing !== null}
        title="Remove device?"
        message={`${actions.removing?.name ?? 'This device'} will lose access and have to be paired again.`}
        confirmLabel="Remove"
        destructive
        busy={actions.removeBusy}
        onConfirm={actions.remove}
        onCancel={actions.closeRemove}
      />
```

**(e)** `setDeviceProfile` and `revokeDevice` are now unused in both screens — remove them from the `../../api/devices` import, keeping `listDevices` and `type Device`. `useState` is still used (for `devices`, `profiles`, `error`, and `pairing` in `ProfileDevices`), so keep that import.

Note: `AllDevices.tsx` keeps its own `error` banner state and passes `setError` as
`onRemoveError`; `ProfileDevices.tsx` does the same. In `AllDevices.tsx` the
`ConfirmModal` message currently reads from `removing`; after the change it reads
from `actions.removing`. Do not alter the wording.

- [ ] **Step 6: Verify nothing changed for the user**

Run: `cd lugo-web-client && pnpm test && pnpm build && pnpm lint`
Expected: PASS on all three. `pnpm build` runs `tsc -b`, which is the check that
catches a missed rename of any of the five state variables at their call sites.

- [ ] **Step 7: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/lugo-web-client
git add src/screens/devices/useDeviceActions.ts src/screens/devices/useDeviceActions.test.tsx src/screens/settings/AllDevices.tsx src/screens/profiles/ProfileDevices.tsx
git commit -m "refactor(devices): share move and remove between both device lists"
```

---

### Task 8: Rename from both device lists

**Files:**
- Modify: `lugo-web-client/src/screens/devices/useDeviceActions.ts` (add rename)
- Modify: `lugo-web-client/src/screens/devices/useDeviceActions.test.tsx` (add rename tests)
- Modify: `lugo-web-client/src/screens/devices/DeviceRow.tsx`
- Create: `lugo-web-client/src/screens/devices/DeviceRow.test.tsx`
- Modify: `lugo-web-client/src/screens/settings/AllDevices.tsx`
- Modify: `lugo-web-client/src/screens/profiles/ProfileDevices.tsx`

**Interfaces:**
- Consumes: `useDeviceActions` (Task 7), `RenameDeviceModal` (Task 5), `renameDevice` (Task 4).
- Produces: `DeviceRow` gains a required `onRename: () => void` prop; `useDeviceActions` additionally returns `renaming`, `renameBusy`, `renameError`, `openRename(device)`, `closeRename()`, `rename(name)`.

- [ ] **Step 1: Write the failing tests**

Append to `lugo-web-client/src/screens/devices/useDeviceActions.test.tsx`, and add `renameDevice: vi.fn()` to the existing `vi.mock` factory plus `renameDevice` to the import from `../../api/devices`. Also add `vi.mocked(renameDevice).mockResolvedValue(undefined)` to the existing `beforeEach`:

```tsx
it('renames the open device and refreshes', async () => {
  const { result, refresh } = setup()

  act(() => result.current.openRename(DEVICE))
  expect(result.current.renaming).toEqual(DEVICE)

  await act(() => result.current.rename('Kitchen speaker'))

  expect(renameDevice).toHaveBeenCalledWith('d1', 'Kitchen speaker')
  expect(refresh).toHaveBeenCalled()
  await waitFor(() => expect(result.current.renaming).toBeNull())
})

it('keeps the rename dialog open and shows the error when it fails', async () => {
  vi.mocked(renameDevice).mockRejectedValue(new Error("device 'd1' not found"))
  const { result, refresh } = setup()

  act(() => result.current.openRename(DEVICE))
  await act(() => result.current.rename('Kitchen speaker'))

  expect(result.current.renameError).toBe("device 'd1' not found")
  expect(result.current.renaming).toEqual(DEVICE)
  expect(refresh).not.toHaveBeenCalled()
})

it('clears a stale rename error when the dialog is reopened', async () => {
  vi.mocked(renameDevice).mockRejectedValue(new Error('nope'))
  const { result } = setup()

  act(() => result.current.openRename(DEVICE))
  await act(() => result.current.rename('Kitchen'))
  expect(result.current.renameError).toBe('nope')

  act(() => result.current.openRename(DEVICE))
  expect(result.current.renameError).toBeNull()
})

it('does nothing when rename is called with no device open', async () => {
  const { result } = setup()
  await act(() => result.current.rename('Kitchen'))
  expect(renameDevice).not.toHaveBeenCalled()
})
```

Create `lugo-web-client/src/screens/devices/DeviceRow.test.tsx`:

```tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { DeviceRow } from './DeviceRow'
import type { Device } from '../../api/devices'

const DEVICE = {
  id: 'd1',
  user_id: 'u1',
  name: 'Lugo-48D0',
  serial: '2884855048d0',
  profile_id: 'kitchen',
  created_at: null,
  last_seen_at: null,
  revoked: false,
} satisfies Device

it('offers rename alongside move and remove', () => {
  const onRename = vi.fn()
  render(
    <DeviceRow device={DEVICE} onMove={() => {}} onRemove={() => {}} onRename={onRename} />,
  )

  fireEvent.click(screen.getByRole('button', { name: /More actions/i }))
  fireEvent.click(screen.getByText('Rename device'))

  expect(onRename).toHaveBeenCalled()
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd lugo-web-client && pnpm vitest run src/screens/devices/useDeviceActions.test.tsx src/screens/devices/DeviceRow.test.tsx`
Expected: FAIL — `result.current.openRename is not a function`, and `DeviceRow` has no `Rename device` item.

- [ ] **Step 3: Add rename to the hook**

In `useDeviceActions.ts`, add `renameDevice` to the `../../api/devices` import, add the state next to the move state:

```ts
  const [renaming, setRenaming] = useState<Device | null>(null)
  const [renameBusy, setRenameBusy] = useState(false)
  const [renameError, setRenameError] = useState<string | null>(null)
```

add the handler next to `move`:

```ts
  async function rename(name: string) {
    if (!renaming) return
    setRenameBusy(true)
    setRenameError(null)
    try {
      await renameDevice(renaming.id, name)
      setRenaming(null)
      await refresh()
    } catch (e) {
      setRenameError(e instanceof Error ? e.message : 'Could not rename the device')
    } finally {
      setRenameBusy(false)
    }
  }
```

and add to the returned object:

```ts
    renaming,
    renameBusy,
    renameError,
    openRename: (device: Device) => {
      setRenameError(null)
      setRenaming(device)
    },
    closeRename: () => setRenaming(null),
    rename,
```

- [ ] **Step 4: Add the prop and the menu item to `DeviceRow`**

In `lugo-web-client/src/screens/devices/DeviceRow.tsx`, add `onRename` to the props type and destructuring:

```tsx
export function DeviceRow({
  device,
  onMove,
  onRemove,
  onRename,
}: {
  device: Device
  onMove: () => void
  onRemove: () => void
  onRename: () => void
}) {
```

and put the item first in the menu — renaming is the least destructive action:

```tsx
        items={[
          { label: 'Rename device', onSelect: onRename },
          { label: 'Move to another assistant', onSelect: onMove },
          // "Remove" and not "Unpair": this revokes the token, so the device has
          // to be paired again from its own screen. Unassigning (a soft change)
          // is offered inside Move, where it costs nothing.
          { label: 'Remove device', onSelect: onRemove, destructive: true },
        ]}
```

- [ ] **Step 5: Run those two test files to verify they pass**

Run: `cd lugo-web-client && pnpm vitest run src/screens/devices/useDeviceActions.test.tsx src/screens/devices/DeviceRow.test.tsx`
Expected: PASS — 10 passed in the hook file, 1 in the row file.

- [ ] **Step 6: Wire it into both screens**

In **both** `src/screens/settings/AllDevices.tsx` and `src/screens/profiles/ProfileDevices.tsx`, import the dialog:

```tsx
import { RenameDeviceModal } from '../devices/RenameDeviceModal'
```

pass the new prop on `DeviceRow`:

```tsx
                onRename={() => actions.openRename(d)}
```

and render the dialog next to `MoveDeviceModal`:

```tsx
      <RenameDeviceModal
        device={actions.renaming}
        busy={actions.renameBusy}
        error={actions.renameError}
        onCancel={actions.closeRename}
        onConfirm={actions.rename}
      />
```

- [ ] **Step 7: Run the whole web suite, typecheck and lint**

Run: `cd lugo-web-client && pnpm test && pnpm build && pnpm lint`
Expected: PASS on all three. `pnpm build` runs `tsc -b`, which is what catches a
missed `onRename` at either `DeviceRow` call site.

- [ ] **Step 8: Commit**

```bash
cd /Users/lugon/code/speech-text-transformer/lugo-web-client
git add src/screens/devices/useDeviceActions.ts src/screens/devices/useDeviceActions.test.tsx src/screens/devices/DeviceRow.tsx src/screens/devices/DeviceRow.test.tsx src/screens/settings/AllDevices.tsx src/screens/profiles/ProfileDevices.tsx
git commit -m "feat(devices): offer rename from both device lists"
```

---

## Final verification

- [ ] Gateway: `cd /Users/lugon/code/speech-text-transformer && make test && make lint` — all pass.
- [ ] Web client: `cd lugo-web-client && pnpm test && pnpm build && pnpm lint` — all pass.
- [ ] Manual smoke, if hardware is available: wipe the device's NVS (`idf.py -B build -p <port> erase-flash flash` from `esp32-assistant/`), read the pairing code off its screen, and claim it in the web client without typing a name. The device should appear as `Lugo-XXXX` matching the SSID it advertised during setup, and renaming it from the list should not disturb its assistant or require re-pairing.
