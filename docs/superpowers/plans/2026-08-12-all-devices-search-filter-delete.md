# All Devices: search/filter + revoked-device delete Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a client-side profile/status/search filter bar to the admin-only All Devices table, and a per-row Delete button that permanently removes a device once it has been revoked.

**Architecture:** Two independent slices sharing one file set. Task 1 adds a hard-delete store method + admin-gated `DELETE /v1/devices/{id}` route (backend, TDD, no auth_guard change needed — `/v1/devices` is already in `_ADMIN_PREFIXES`). Task 2 wires that route into the existing vanilla-JS admin panel (`devices.js` / `index.html`) as a Delete button, disabled until a row is revoked. Task 3 adds the client-side filter bar (profile/status/search selects) over the same table, filtering already-fetched data with no new API calls, following the existing Model Registry filter pattern verbatim.

**Tech Stack:** FastAPI + SQLAlchemy async (gateway backend), vanilla JS + `renderDataTable` helper (static admin panel), pytest + `TestClient` (backend tests).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-12-all-devices-search-filter-delete-design.md`
- Delete is admin-only, All Devices table only. No change to My Devices, no bulk delete, no auth_guard.py edit.
- Delete is only permitted on an already-revoked device (400 otherwise).
- Filter bar is client-side only — no new API calls, no pagination, no persisted filter state across reload.
- Follow existing code style exactly: `devices.js` uses `if (el(id)) el(id).addEventListener(...)` guards at module scope; `model-registry.js`'s `_filteredRegistryData()` is the template for the filter function.

---

### Task 1: Backend — hard-delete a revoked device

**Files:**
- Modify: `apps/api_gateway/app/services/auth/devices.py` (add `DeviceStore.delete`)
- Modify: `apps/api_gateway/app/api/routes/devices.py` (add `DELETE /{device_id}` route)
- Test: `tests/unit/auth/test_devices_routes.py` (add 3 tests)

**Interfaces:**
- Consumes: `Device` model, `db_session` (both already imported in `services/auth/devices.py`); `device_store` singleton, `HTTPException` (both already imported in `api/routes/devices.py`).
- Produces: `DeviceStore.delete(device_id: str) -> bool` (True if a row was deleted, False if no such row) — for later tasks/tests to call directly if needed. Route: `DELETE /v1/devices/{device_id}` → `{"success": True, "data": {"id": device_id, "deleted": True}}` on success, 404 if missing, 400 if not yet revoked.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/auth/test_devices_routes.py`, right after `test_admin_lists_and_revokes_any_device` (after line 102):

```python
def test_delete_nonexistent_device_404(client, _logged_in_user):
    resp = client.delete("/v1/devices/does-not-exist")
    assert resp.status_code == 404


def test_delete_refuses_a_device_that_is_not_revoked(client, _logged_in_user):
    init = client.post("/v1/devices/pair/init", json={"serial": "S1"}).json()["data"]
    device = client.post(
        "/v1/devices/pair/claim", json={"code": init["code"], "name": "dev"}
    ).json()["data"]

    resp = client.delete(f"/v1/devices/{device['id']}")

    assert resp.status_code == 400
    # Still there -- a rejected delete must not have removed the row.
    assert any(d["id"] == device["id"] for d in client.get("/v1/devices").json()["data"])


def test_delete_removes_a_revoked_device(client, _logged_in_user):
    init = client.post("/v1/devices/pair/init", json={"serial": "S1"}).json()["data"]
    device = client.post(
        "/v1/devices/pair/claim", json={"code": init["code"], "name": "dev"}
    ).json()["data"]
    client.post(f"/v1/devices/{device['id']}/revoke")

    resp = client.delete(f"/v1/devices/{device['id']}")

    assert resp.status_code == 200
    assert resp.json()["data"] == {"id": device["id"], "deleted": True}
    assert not any(d["id"] == device["id"] for d in client.get("/v1/devices").json()["data"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api_gateway && python -m pytest tests/unit/auth/test_devices_routes.py -k test_delete -v` (adjust the path prefix to however this repo's pytest is normally invoked from the root — check for a `pytest.ini`/`pyproject.toml` `testpaths` if `tests/unit/...` isn't directly runnable from repo root).

Expected: all 3 new tests FAIL with 405 Method Not Allowed (no `DELETE` route registered yet).

- [ ] **Step 3: Add `DeviceStore.delete`**

In `apps/api_gateway/app/services/auth/devices.py`, add this method to `DeviceStore`, directly after `set_name` (after line 140, before `clear_profile`):

```python
    async def delete(self, device_id: str) -> bool:
        """Hard-delete a device row. Caller is responsible for checking it's
        revoked first -- this method does not enforce that, it just removes
        the row."""
        async with db_session() as s:
            row = await s.get(Device, device_id)
            if row is None:
                return False
            await s.delete(row)
            await s.commit()
            return True
```

- [ ] **Step 4: Add the route**

In `apps/api_gateway/app/api/routes/devices.py`, add this route at the end of the file, after `revoke_any_device`:

```python
@router.delete("/{device_id}")
async def delete_device(device_id: str) -> dict:
    device = await device_store.get_by_id(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"device '{device_id}' not found")
    if not device.revoked:
        raise HTTPException(status_code=400, detail="device must be revoked before it can be deleted")
    await device_store.delete(device_id)
    return {"success": True, "data": {"id": device_id, "deleted": True}}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd apps/api_gateway && python -m pytest tests/unit/auth/test_devices_routes.py -v`

Expected: all tests in the file PASS, including the 3 new ones and the existing ones (unchanged).

- [ ] **Step 6: Confirm the new route is classified admin-only**

Run: `cd apps/api_gateway && python -m pytest tests/unit/http/test_auth_guard_route_coverage.py -v`

Expected: PASS with no changes to `auth_guard.py` — `/v1/devices/{device_id}` already concretizes under the `_ADMIN_PREFIXES` entry `"/v1/devices"`.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/services/auth/devices.py apps/api_gateway/app/api/routes/devices.py tests/unit/auth/test_devices_routes.py
git commit -m "feat(api_gateway): add admin DELETE endpoint for revoked devices"
```

---

### Task 2: Admin UI — Delete button on the All Devices table

**Files:**
- Modify: `apps/api_gateway/app/static/js/devices.js`

**Interfaces:**
- Consumes: `DELETE /v1/devices/{device_id}` from Task 1 (response shape `{"success": true, "data": {"id": ..., "deleted": true}}` on 200; `{"detail": "..."}` on 400/404). Existing module functions: `el`, `print`, `escapeHtml` (from `./helpers.js`), `confirmDialog` (from `./modal.js`), `renderAllDeviceList()`, `maybeLoadAllDevices()`, `allDeviceData` (all already in this file).
- Produces: `deleteAnyDevice(id: string): Promise<void>` — no other task depends on this.

- [ ] **Step 1: Add the Delete button to the actions column**

In `apps/api_gateway/app/static/js/devices.js`, in `renderAllDeviceList()` (around line 144-150), change the `actions` column's `render` to include a second button:

```js
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (d) => `
          <button class="mini danger" data-device-revoke-any="${escapeHtml(d.id)}" ${d.revoked ? "disabled" : ""}>Revoke</button>
          <button class="mini danger" data-device-delete="${escapeHtml(d.id)}" ${!d.revoked ? "disabled" : ""}>Delete</button>
        `,
      },
```

- [ ] **Step 2: Wire the click handler**

Directly below the existing `table.querySelectorAll("[data-device-revoke-any]")...` block inside `renderAllDeviceList()` (around line 158-160), add:

```js
  table.querySelectorAll("[data-device-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteAnyDevice(btn.getAttribute("data-device-delete")))
  );
```

- [ ] **Step 3: Add the `deleteAnyDevice` function**

Directly after `revokeAnyDevice` (after line 226), add:

```js
async function deleteAnyDevice(id) {
  if (!(await confirmDialog("Permanently delete this device? This cannot be undone.", { danger: true }))) return;
  try {
    const resp = await fetch(`/v1/devices/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      print(el("device-status"), body.detail || "Delete failed", true);
      return;
    }
    await maybeLoadAllDevices();
  } catch (error) {
    print(el("device-status"), String(error), true);
  }
}
```

- [ ] **Step 4: Manual verification (no test harness exists for this file)**

Start the gateway locally (check `apps/api_gateway`'s README or `Makefile` for the dev-server command used elsewhere in this repo), log in as an admin, pair a throwaway device from another account or the same one, and in the All Devices table:
- Confirm the Delete button is disabled on an active (non-revoked) row.
- Click Revoke on that row; confirm Delete becomes enabled.
- Click Delete, confirm the dialog, confirm the row disappears from the table after reload.
- Reload the page and confirm the device is gone from a fresh `GET /v1/devices` (i.e. it didn't just disappear from stale client state).

- [ ] **Step 5: Commit**

```bash
git add apps/api_gateway/app/static/js/devices.js
git commit -m "feat(admin-ui): add Delete action for revoked devices on All Devices table"
```

---

### Task 3: Admin UI — search & filter bar for the All Devices table

**Files:**
- Modify: `apps/api_gateway/app/static/index.html` (filter bar markup, around line 442-448)
- Modify: `apps/api_gateway/app/static/js/devices.js` (filter logic + listeners)

**Interfaces:**
- Consumes: `profileData` (from `./profiles.js`, already imported in this file — `{[name]: {owner_id, ...}}`), `allDeviceData` (module-level, already populated by `maybeLoadAllDevices()`), `el` (from `./helpers.js`).
- Produces: nothing consumed by other tasks — this is the last task.

- [ ] **Step 1: Add the filter bar markup**

In `apps/api_gateway/app/static/index.html`, inside `#device-all-section` (around line 442-448), insert the filter bar between the hint `<p>` and `#device-all-list`:

```html
            <section class="card hidden" id="device-all-section">
              <div class="card-head">
                <h2>All Devices</h2>
              </div>
              <p class="hint">Every paired device, across all accounts.</p>
              <div class="row tight">
                <label>
                  Profile
                  <select id="device-all-filter-profile">
                    <option value="">All</option>
                    <option value="__unassigned__">Unassigned</option>
                  </select>
                </label>
                <label>
                  Status
                  <select id="device-all-filter-status">
                    <option value="">All</option>
                    <option value="active">Active</option>
                    <option value="revoked">Revoked</option>
                  </select>
                </label>
                <label>
                  Search
                  <input id="device-all-filter-search" type="text" placeholder="name, serial, or owner…" />
                </label>
              </div>
              <div id="device-all-list" class="model-list"></div>
            </section>
```

- [ ] **Step 2: Add `renderAllDeviceFilterProfileOptions`**

In `apps/api_gateway/app/static/js/devices.js`, directly after `renderDevicePairProfileSelect` (after line 36), add:

```js
function renderAllDeviceFilterProfileOptions() {
  const sel = el("device-all-filter-profile");
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">All</option><option value="__unassigned__">Unassigned</option>';
  Object.keys(profileData).sort().forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = profileData[name]?.owner_id ? `${name} (mine)` : name;
    sel.appendChild(opt);
  });
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}
```

- [ ] **Step 3: Call it from `maybeLoadAllDevices`**

In `maybeLoadAllDevices()` (around line 114-129), call the new function right before `renderAllDeviceList()`:

```js
async function maybeLoadAllDevices() {
  const status = await fetchAuthStatus();
  const section = el("device-all-section");
  if (!(status.authenticated && status.role === "admin")) {
    if (section) section.classList.add("hidden");
    return;
  }
  if (section) section.classList.remove("hidden");
  try {
    const body = await (await fetch("/v1/devices")).json();
    allDeviceData = body.data || [];
    renderAllDeviceFilterProfileOptions();
    renderAllDeviceList();
  } catch {
    /* ignore */
  }
}
```

- [ ] **Step 4: Add `_filteredAllDeviceData` and use it in `renderAllDeviceList`**

Directly above `renderAllDeviceList` (before line 131), add:

```js
function _filteredAllDeviceData() {
  const profile = el("device-all-filter-profile")?.value || "";
  const status = el("device-all-filter-status")?.value || "";
  const search = (el("device-all-filter-search")?.value || "").trim().toLowerCase();
  return allDeviceData.filter((d) => {
    if (profile === "__unassigned__" && d.profile_id) return false;
    if (profile && profile !== "__unassigned__" && d.profile_id !== profile) return false;
    if (status === "active" && d.revoked) return false;
    if (status === "revoked" && !d.revoked) return false;
    if (search) {
      const haystack = `${d.name} ${d.serial} ${d.owner_username}`.toLowerCase();
      if (!haystack.includes(search)) return false;
    }
    return true;
  });
}
```

Then change `renderAllDeviceList()`'s `renderDataTable` call to use it, and adjust the empty message:

```js
function renderAllDeviceList() {
  const host = el("device-all-list");
  if (!host) return;

  const rows = _filteredAllDeviceData();
  const table = renderDataTable({
    container: host,
    rows,
    rowKey: (d) => d.id,
    getRowClass: (d) => (d.revoked ? "dim" : ""),
    emptyMessage: allDeviceData.length ? "No devices match the current filters." : "No devices paired yet.",
    columns: [
      ...deviceColumns(true),
      allDeviceProfileColumn(),
      {
        key: "actions",
        label: "",
        headerClass: "dt-actions-cell",
        cellClass: "dt-actions-cell",
        render: (d) => `
          <button class="mini danger" data-device-revoke-any="${escapeHtml(d.id)}" ${d.revoked ? "disabled" : ""}>Revoke</button>
          <button class="mini danger" data-device-delete="${escapeHtml(d.id)}" ${!d.revoked ? "disabled" : ""}>Delete</button>
        `,
      },
    ],
    bulkActions: [
      { label: "Revoke selected", run: (ids) => bulkRevokeDevices(ids, true) },
    ],
  });
  if (!table) return;

  table.querySelectorAll("[data-device-revoke-any]").forEach((btn) =>
    btn.addEventListener("click", () => revokeAnyDevice(btn.getAttribute("data-device-revoke-any")))
  );
  table.querySelectorAll("[data-device-delete]").forEach((btn) =>
    btn.addEventListener("click", () => deleteAnyDevice(btn.getAttribute("data-device-delete")))
  );
}
```

(This folds in Task 2's button/listener additions so the function is written once, in full, here — if Task 2 already landed, this step is just the `rows`/`emptyMessage` change plus keeping the two buttons that are already there.)

- [ ] **Step 5: Wire the filter control listeners**

At the bottom of the file, next to the existing guarded listeners (around line 275-276), add:

```js
if (el("device-all-filter-profile")) el("device-all-filter-profile").addEventListener("change", renderAllDeviceList);
if (el("device-all-filter-status")) el("device-all-filter-status").addEventListener("change", renderAllDeviceList);
if (el("device-all-filter-search")) el("device-all-filter-search").addEventListener("input", renderAllDeviceList);
```

- [ ] **Step 6: Manual verification (no test harness exists for this file)**

With the gateway running and at least 2-3 devices paired across different profiles/owners/revoked states:
- Type a substring of a device name, serial, and owner username (one at a time) into Search; confirm only matching rows remain each time.
- Select a specific profile in the Profile dropdown; confirm only that profile's devices show. Select "Unassigned"; confirm only unassigned devices show.
- Select "Active" / "Revoked" in Status; confirm the split matches the dimmed rows.
- Combine two filters (e.g. Status=Revoked + a search term) and confirm both apply (AND, not OR).
- Clear all filters back to "All"/empty search; confirm the full list returns.
- Revoke a device while a filter is active that would exclude it once revoked (e.g. Status=Active); confirm the row disappears from the filtered view after the table refreshes, without you having to touch the filter controls again.

- [ ] **Step 7: Commit**

```bash
git add apps/api_gateway/app/static/index.html apps/api_gateway/app/static/js/devices.js
git commit -m "feat(admin-ui): add profile/status/search filters to All Devices table"
```

---

## Self-Review Notes

- **Spec coverage:** §1 (search & filter) → Task 3. §2 (delete) → Tasks 1-2. "Explicitly out of scope" items (My Devices, bulk delete, pagination, persisted filter state) have no tasks, correctly. Testing section's gateway tests → Task 1 Step 1; static-UI manual verification → Task 2 Step 4 and Task 3 Step 6.
- **Task ordering:** Task 2 (button) lands before Task 3 (filter bar) touches the same `renderAllDeviceList` function; Task 3 Step 4 restates the full function post-Task-2 so an implementer working Task 3 alone (e.g. if tasks are dispatched out of order) still has the complete, correct code rather than a diff against an assumed prior state.
- **Type/name consistency checked:** `DeviceStore.delete` (Task 1) ↔ not called directly from JS (route-mediated only, consistent with every other store method). `deleteAnyDevice` (Task 2) name matches its only call site (Task 3's restated `renderAllDeviceList`). `_filteredAllDeviceData` (Task 3) matches its only call site. `renderAllDeviceFilterProfileOptions` (Task 3) matches its call in `maybeLoadAllDevices` (Task 3 Step 3).
